"""LOCAL USE ONLY (Artifact 13.5 T1.1): a fake OIDC token issuer for the Compose stack and tests.

Stands in for the identity system (ADR-49), which has no owner yet. It signs short-lived RS256
tokens with a key it generates at start-up and never stores, so a token from one run is
refused after a restart. Anyone who can reach it can mint any identity: Compose publishes it
on 127.0.0.1 only, and it refuses to start when AISRE_ENV is production.

    GET  /.well-known/openid-configuration   issuer, jwks_uri, token_endpoint
    GET  /jwks                               the public key, as a JWK set
    POST /token                              {"sub": ..., optional below} -> {"access_token", ...}
    GET  /healthz

POST /token takes `sub` (required), `groups` (list of strings, default []), `org_id`
(default the development organisation), `mfa` (default true: `amr` holds "mfa"),
`auth_age_seconds` (how long ago the person signed in, default 0) and `ttl_seconds`
(1 to 900, default 300). The token carries iss, aud, sub, iat, nbf, exp, auth_time, amr,
groups and org_id: the claims aisre.api.tokens reads.

Settings, from the environment: FAKE_ISSUER_URL (the `iss` value, and the base of the URLs
it advertises), FAKE_ISSUER_AUDIENCE (default "aisre-api"), FAKE_ISSUER_PORT (default 8080).
Tests import `Issuer` and `serve` from this file; it needs only the standard library and
`cryptography`.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

DEV_ORG_ID = "00000000-0000-7000-8000-000000000001"  # aisre.api.auth.DEV_ORG_ID
DEFAULT_TTL = 300
MAX_TTL = 900


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _uint(n: int) -> str:
    return b64url(n.to_bytes((n.bit_length() + 7) // 8, "big"))


class Issuer:
    """An RSA key and the claims around it. `sign` takes any claims, so tests can forge."""

    def __init__(self, url: str, audience: str = "aisre-api") -> None:
        self.url = url.rstrip("/")
        self.audience = audience
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = uuid.uuid4().hex[:16]

    def jwks(self) -> dict[str, Any]:
        pub = self.key.public_key().public_numbers()
        jwk = {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": self.kid}
        return {"keys": [jwk | {"n": _uint(pub.n), "e": _uint(pub.e)}]}

    def sign(self, claims: dict[str, Any], header: dict[str, Any] | None = None) -> str:
        head = {"alg": "RS256", "typ": "JWT", "kid": self.kid} | (header or {})
        signing_input = (
            b64url(json.dumps(head).encode()) + "." + b64url(json.dumps(claims).encode())
        )
        sig = self.key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
        return signing_input + "." + b64url(sig)

    def claims(
        self,
        sub: str,
        groups: list[str] | tuple[str, ...] = (),
        *,
        org_id: str = DEV_ORG_ID,
        mfa: bool = True,
        auth_age_seconds: int = 0,
        ttl_seconds: int = DEFAULT_TTL,
        now: float | None = None,
    ) -> dict[str, Any]:
        t = int(time.time() if now is None else now)
        return {
            "iss": self.url,
            "aud": self.audience,
            "sub": sub,
            "iat": t,
            "nbf": t,
            "exp": t + ttl_seconds,
            "auth_time": t - auth_age_seconds,
            "amr": ["pwd", "mfa"] if mfa else ["pwd"],
            "groups": list(groups),
            "org_id": org_id,
            "jti": uuid.uuid4().hex,
        }

    def token(self, sub: str, groups: list[str] | tuple[str, ...] = (), **kw: Any) -> str:
        return self.sign(self.claims(sub, groups, **kw))


def handler_for(issuer: Issuer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # no tokens in logs
            sys.stderr.write(f"fake-issuer {self.command} {self.path.split('?')[0]}\n")

        def reply(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            path = self.path.split("?")[0]
            if path == "/.well-known/openid-configuration":
                self.reply(
                    200,
                    {
                        "issuer": issuer.url,
                        "jwks_uri": issuer.url + "/jwks",
                        "token_endpoint": issuer.url + "/token",
                        "id_token_signing_alg_values_supported": ["RS256"],
                    },
                )
            elif path == "/jwks":
                self.reply(200, issuer.jwks())
            elif path == "/healthz":
                self.reply(200, {"status": "ok"})
            else:
                self.reply(404, {"error": "not_found"})

        def do_POST(self) -> None:
            if self.path.split("?")[0] != "/token":
                return self.reply(404, {"error": "not_found"})
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                sub, groups = req["sub"], req.get("groups", [])
                ttl = int(req.get("ttl_seconds", DEFAULT_TTL))
                if not isinstance(sub, str) or not sub or not isinstance(groups, list):
                    raise ValueError("sub must be a string and groups a list")
                if not 1 <= ttl <= MAX_TTL:
                    raise ValueError(f"ttl_seconds must be 1 to {MAX_TTL}")
                token = issuer.token(
                    sub,
                    [str(g) for g in groups],
                    org_id=str(req.get("org_id", DEV_ORG_ID)),
                    mfa=bool(req.get("mfa", True)),
                    auth_age_seconds=int(req.get("auth_age_seconds", 0)),
                    ttl_seconds=ttl,
                )
            except (KeyError, ValueError, TypeError) as e:
                return self.reply(400, {"error": "invalid_request", "detail": str(e)})
            self.reply(200, {"access_token": token, "token_type": "Bearer", "expires_in": ttl})

    return Handler


def serve(issuer: Issuer, port: int = 0, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Start serving on a daemon thread; `server.server_address` has the port."""
    server = ThreadingHTTPServer((host, port), handler_for(issuer))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    if os.environ.get("AISRE_ENV") == "production":
        sys.exit("fake token issuer: refusing to run with AISRE_ENV=production")
    port = int(os.environ.get("FAKE_ISSUER_PORT", "8080"))
    issuer = Issuer(
        os.environ.get("FAKE_ISSUER_URL", f"http://127.0.0.1:{port}"),
        os.environ.get("FAKE_ISSUER_AUDIENCE", "aisre-api"),
    )
    print(f"fake token issuer {issuer.url} on :{port} (local use only)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), handler_for(issuer)).serve_forever()


if __name__ == "__main__":
    main()
