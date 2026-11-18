"""Artifact 7.4, ADR-49 (T1.3): bearer-token verification against the issuer's JWKS.

Every `/v1` call carries a short-lived token signed by the identity system. `TokenVerifier`
checks it with the standard library and `cryptography`, no JWT package:

    signature   RS256 only. "none", HMAC and every other algorithm are refused, so a token
                can't choose how it is checked. The key is the issuer's, found by `kid` in
                its JWK set; a key under 2048 bits is ignored.
    issuer      `iss` equals the configured issuer exactly.
    audience    `aud` is, or contains, the configured audience.
    time        `exp` in the future and `nbf`/`iat` not in it, with 30 s of clock leeway;
                `exp - iat` at most an hour, so a long-lived token is refused.
    identity    `sub`; `org_id`, a UUID naming the organisation; `groups`, a list of strings
                (roles come from these, aisre.api.auth); `auth_time`, when the person signed in;
                `amr`, whose "mfa" entry is the two-factor claim (RFC 8176). Sign-in age and
                two-factor are read here and enforced where ADR-49 asks for them, on approval.

The JWK set is fetched on first use and again when a token names a `kid` not in it (the
issuer rotated its key), at most once every 30 seconds, so a flood of made-up `kid`s can't
turn into a flood of fetches. An unreachable issuer is `VerifierUnavailable` (503), not a
refusal of the caller.
"""

from __future__ import annotations

import base64
import binascii
import json
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey, RSAPublicNumbers

ALGORITHM = "RS256"
LEEWAY_S = 30
MAX_LIFETIME_S = 3600
MIN_KEY_BITS = 2048
REFETCH_INTERVAL_S = 30.0
JWKS_MAX_BYTES = 64 * 1024
JWKS_TIMEOUT_S = 3.0

JwksFetcher = Callable[[str], Mapping[str, Any]]


class InvalidToken(Exception):
    """The token is refused. `reason` is a stable code for the log; callers see only 401."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class VerifierUnavailable(Exception):
    """The issuer's keys couldn't be fetched."""


@dataclass(frozen=True)
class Claims:
    subject: str
    org_id: UUID
    groups: tuple[str, ...]
    auth_time: datetime
    mfa: bool
    expires_at: datetime


def fetch_jwks(url: str) -> Mapping[str, Any]:
    """GET the JWK set. Raises VerifierUnavailable on any failure."""
    try:
        with urllib.request.urlopen(url, timeout=JWKS_TIMEOUT_S) as r:  # noqa: S310 (configured URL)
            body = r.read(JWKS_MAX_BYTES + 1)
        if len(body) > JWKS_MAX_BYTES:
            raise ValueError("JWK set too large")
        doc = json.loads(body)
    except (OSError, ValueError) as e:
        raise VerifierUnavailable(f"JWK set unavailable: {type(e).__name__}") from None
    if not isinstance(doc, dict):
        raise VerifierUnavailable("JWK set is not an object")
    return doc


def _b64(segment: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (binascii.Error, ValueError):
        raise InvalidToken("malformed") from None


def _json_object(segment: str) -> dict[str, Any]:
    try:
        value = json.loads(_b64(segment))
    except ValueError:
        raise InvalidToken("malformed") from None
    if not isinstance(value, dict):
        raise InvalidToken("malformed")
    return value


def _uint(value: Any) -> int:
    if not isinstance(value, str):
        raise ValueError("not a string")
    return int.from_bytes(_b64(value), "big")


def rsa_keys(jwks: Mapping[str, Any]) -> dict[str, RSAPublicKey]:
    """RS256 signing keys by `kid`. Anything else in the set is skipped."""
    keys: dict[str, RSAPublicKey] = {}
    for jwk in jwks.get("keys", []) if isinstance(jwks.get("keys"), list) else []:
        if not isinstance(jwk, dict) or jwk.get("kty") != "RSA" or not jwk.get("kid"):
            continue
        if jwk.get("use", "sig") != "sig" or jwk.get("alg", ALGORITHM) != ALGORITHM:
            continue
        try:
            key = RSAPublicNumbers(_uint(jwk["e"]), _uint(jwk["n"])).public_key()
        except (KeyError, ValueError, InvalidToken):
            continue
        if key.key_size >= MIN_KEY_BITS:
            keys[str(jwk["kid"])] = key
    return keys


def _int_claim(claims: Mapping[str, Any], name: str) -> int:
    value = claims.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidToken(f"missing_claim:{name}")
    return value


class TokenVerifier:
    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_url: str,
        *,
        fetch: JwksFetcher = fetch_jwks,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.issuer, self.audience, self.jwks_url = issuer, audience, jwks_url
        self._fetch, self._clock = fetch, clock
        self._keys: dict[str, RSAPublicKey] = {}
        self._fetched_at: float | None = None
        self._lock = threading.Lock()

    def verify(self, token: str) -> Claims:
        """The token's claims, or InvalidToken. VerifierUnavailable if keys can't be had."""
        parts = token.split(".")
        if len(parts) != 3 or not all(parts):
            raise InvalidToken("malformed")
        header = _json_object(parts[0])
        if header.get("alg") != ALGORITHM:
            raise InvalidToken("unsupported_alg")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise InvalidToken("unknown_key")
        key = self._key(kid)
        try:
            key.verify(
                _b64(parts[2]),
                f"{parts[0]}.{parts[1]}".encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except InvalidSignature:
            raise InvalidToken("bad_signature") from None
        return self._check(_json_object(parts[1]))

    def _check(self, c: Mapping[str, Any]) -> Claims:
        now = self._clock()
        if c.get("iss") != self.issuer:
            raise InvalidToken("wrong_issuer")
        aud = c.get("aud")
        if not (aud == self.audience or (isinstance(aud, list) and self.audience in aud)):
            raise InvalidToken("wrong_audience")
        exp, iat = _int_claim(c, "exp"), _int_claim(c, "iat")
        if exp <= now - LEEWAY_S:
            raise InvalidToken("expired")
        nbf = _int_claim(c, "nbf") if "nbf" in c else iat
        if iat > now + LEEWAY_S or nbf > now + LEEWAY_S:
            raise InvalidToken("not_yet_valid")
        if exp - iat > MAX_LIFETIME_S:
            raise InvalidToken("lifetime_too_long")
        sub, groups, amr = c.get("sub"), c.get("groups", []), c.get("amr", [])
        if not isinstance(sub, str) or not sub:
            raise InvalidToken("missing_claim:sub")
        if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
            raise InvalidToken("missing_claim:groups")
        if not isinstance(amr, list):
            raise InvalidToken("missing_claim:amr")
        try:
            org_id = UUID(str(c["org_id"]))
        except (KeyError, ValueError):
            raise InvalidToken("missing_claim:org_id") from None
        return Claims(
            subject=sub,
            org_id=org_id,
            groups=tuple(groups),
            auth_time=datetime.fromtimestamp(_int_claim(c, "auth_time"), UTC),
            mfa="mfa" in amr,
            expires_at=datetime.fromtimestamp(exp, UTC),
        )

    def _key(self, kid: str) -> RSAPublicKey:
        with self._lock:
            key = self._keys.get(kid)
            if key is not None:
                return key
            now = self._clock()
            if self._fetched_at is not None and now - self._fetched_at < REFETCH_INTERVAL_S:
                raise InvalidToken("unknown_key")
            keys = rsa_keys(self._fetch(self.jwks_url))
            self._keys, self._fetched_at = keys, now
            if kid not in keys:
                raise InvalidToken("unknown_key")
            return keys[kid]
