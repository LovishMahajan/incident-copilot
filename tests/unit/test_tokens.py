"""Artifact 13.5 T1.3: bearer-token verification (aisre.api.tokens) and roles from groups
(aisre.api.auth). Route-level refusals are ST-19, in tests/security/test_authz.py."""

from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from aisre.api.auth import EVERY_SERVICE, Caller, check_sign_in, roles_from_groups
from aisre.api.tokens import (
    LEEWAY_S,
    REFETCH_INTERVAL_S,
    InvalidToken,
    TokenVerifier,
    VerifierUnavailable,
    fetch_jwks,
)
from aisre.config import Settings
from aisre.remediation.action import Perm, Role

ISS, AUD = "http://issuer.test", "aisre-api"


def b64url_json(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


@pytest.fixture
def issuer(token_issuer):
    return token_issuer[0]


@pytest.fixture
def verifier(issuer):
    return TokenVerifier(ISS, AUD, "unused", fetch=lambda _: issuer.jwks())


def refused(verifier, token, reason):
    with pytest.raises(InvalidToken) as e:
        verifier.verify(token)
    assert e.value.reason == reason


def test_a_good_token_gives_its_claims(issuer, verifier):
    token = issuer.token("alice", ["aisre:responder"], mfa=True, auth_age_seconds=120)
    c = verifier.verify(token)
    assert c.subject == "alice" and c.groups == ("aisre:responder",) and c.mfa is True
    assert c.org_id == UUID("00000000-0000-7000-8000-000000000001")
    age = datetime.now(UTC) - c.auth_time
    assert 115 <= age.total_seconds() <= 130


def test_no_two_factor_is_read_as_such(issuer, verifier):
    assert verifier.verify(issuer.token("bob", mfa=False)).mfa is False


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"iss": "http://evil.test"}, "wrong_issuer"),
        ({"aud": "someone-else"}, "wrong_audience"),
        ({"aud": ["x", "y"]}, "wrong_audience"),
        (lambda: {"exp": int(time.time()) - LEEWAY_S - 5}, "expired"),
        (lambda: {"iat": int(time.time()) + 600, "exp": int(time.time()) + 900}, "not_yet_valid"),
        (lambda: {"nbf": int(time.time()) + 600}, "not_yet_valid"),
        ({"nbf": "soon"}, "missing_claim:nbf"),
        (lambda: {"exp": int(time.time()) + 7200}, "lifetime_too_long"),
        ({"sub": ""}, "missing_claim:sub"),
        ({"groups": "aisre:responder"}, "missing_claim:groups"),
        ({"org_id": "not-a-uuid"}, "missing_claim:org_id"),
        ({"auth_time": None}, "missing_claim:auth_time"),
        ({"exp": True}, "missing_claim:exp"),
    ],
)
def test_bad_claims_are_refused(issuer, verifier, change, reason):
    # Times are read when the test runs, not at collection: a long suite or a
    # sleeping machine would otherwise let "600 s from now" pass.
    change = change() if callable(change) else change
    refused(verifier, issuer.sign(issuer.claims("alice") | change), reason)


def test_an_audience_list_holding_ours_is_accepted(issuer, verifier):
    assert verifier.verify(issuer.sign(issuer.claims("a") | {"aud": ["x", AUD]})).subject == "a"


def test_altered_payload_fails_the_signature(issuer, verifier):
    head, _, sig = issuer.token("alice", ["aisre:viewer"]).split(".")
    forged = b64url_json(issuer.claims("alice", ["aisre:incident_lead"]))
    refused(verifier, f"{head}.{forged}.{sig}", "bad_signature")


def test_a_token_signed_by_another_key_with_our_kid_is_refused(issuer, verifier, token_issuer):
    other = type(issuer)(ISS, AUD)
    other.kid = issuer.kid
    refused(verifier, other.token("alice"), "bad_signature")


@pytest.mark.parametrize("alg", ["none", "HS256", "RS512", "ES256", None])
def test_only_rs256_is_accepted(issuer, verifier, alg):
    head = b64url_json({"alg": alg, "kid": issuer.kid})
    body = b64url_json(issuer.claims("alice"))
    refused(verifier, f"{head}.{body}.c2ln", "unsupported_alg")


@pytest.mark.parametrize("token", ["", "a.b", "a.b.c.d", "..", "!!!.???.***", "e30.e30."])
def test_malformed_tokens_are_refused(verifier, token):
    with pytest.raises(InvalidToken):
        verifier.verify(token)


def test_unknown_kid_refetches_at_most_every_30_seconds(issuer):
    fetches, now = [], [1000.0]

    def fetch(url):
        fetches.append(url)
        return issuer.jwks()

    v = TokenVerifier(ISS, AUD, "jwks", fetch=fetch, clock=lambda: now[0])
    forged = issuer.sign(issuer.claims("a", now=now[0]), header={"kid": "nope"})
    refused(v, forged, "unknown_key")
    refused(v, forged, "unknown_key")
    assert len(fetches) == 1
    now[0] += REFETCH_INTERVAL_S
    refused(v, forged, "unknown_key")
    assert len(fetches) == 2


def test_a_rotated_key_is_picked_up(issuer):
    keys = [issuer.jwks()]
    v = TokenVerifier(ISS, AUD, "jwks", fetch=lambda _: keys[0])
    assert v.verify(issuer.token("a")).subject == "a"
    rotated = type(issuer)(ISS, AUD)
    keys[0] = rotated.jwks()
    # The new kid is unknown, and the last fetch is under 30 s old: refused until then.
    refused(v, rotated.token("a"), "unknown_key")
    v._fetched_at = None
    assert v.verify(rotated.token("a")).subject == "a"


def test_small_and_non_rsa_keys_are_ignored(issuer):
    jwk = issuer.jwks()["keys"][0]
    junk = {"keys": [{"kty": "EC", "kid": "ec"}, {**jwk, "n": "AQAB", "kid": "tiny"}, "x"]}
    v = TokenVerifier(ISS, AUD, "jwks", fetch=lambda _: junk)
    refused(v, issuer.sign(issuer.claims("a"), header={"kid": "tiny"}), "unknown_key")


def test_an_unreachable_issuer_is_unavailable_not_a_refusal(issuer):
    with pytest.raises(VerifierUnavailable):
        fetch_jwks("http://127.0.0.1:1/jwks")
    v = TokenVerifier(ISS, AUD, "http://127.0.0.1:1/jwks")
    with pytest.raises(VerifierUnavailable):
        v.verify(issuer.token("a"))


def test_fetch_reads_the_issuer_jwks(token_issuer):
    issuer, url = token_issuer
    assert fetch_jwks(url) == json.loads(json.dumps(issuer.jwks()))


# ---------------------------------------------------------------- roles from groups
def test_groups_give_roles_everywhere_or_on_one_service():
    grants = roles_from_groups(
        ["aisre:viewer", "aisre:approver:orders-api", "other:thing", "aisre:wizard", "aisre:"]
    )
    assert grants == {
        EVERY_SERVICE: frozenset({Role.VIEWER}),
        "orders-api": frozenset({Role.APPROVER}),
    }


@pytest.mark.parametrize("group", ["aisre:system:workflow", "aisre:system:dispatcher"])
def test_platform_identities_never_come_from_a_group(group):
    assert roles_from_groups([group]) == {}


def test_caller_permissions_follow_the_matrix_per_service():
    c = Caller("user:a", UUID(int=1), roles_from_groups(["aisre:responder:orders-api"]))
    assert c.can(Perm.CREATE, "orders-api") and not c.can(Perm.CREATE, "payments")
    assert c.can_somewhere(Perm.READ) and not c.can_somewhere(Perm.CONFIRM_SEVERITY)
    assert not c.is_monitoring
    m = Caller("user:m", UUID(int=1), roles_from_groups(["aisre:monitoring"]))
    assert m.is_monitoring and m.can(Perm.CREATE, "x") and not m.can(Perm.READ, "x")


# ---------------------------------------------------------------- settings
def test_oidc_settings_go_together():
    with pytest.raises(ValidationError, match="go together"):
        Settings(oidc_issuer=ISS)


def test_production_needs_an_https_key_set():
    with pytest.raises(ValidationError, match="https"):
        Settings(env="production", oidc_issuer=ISS, oidc_audience=AUD, oidc_jwks_url="http://x")


def test_production_starts_only_with_a_verifier():
    with pytest.raises(RuntimeError, match="development-only"):
        check_sign_in(Settings(env="production"))
    check_sign_in(
        Settings(env="production", oidc_issuer=ISS, oidc_audience=AUD, oidc_jwks_url="https://x")
    )
