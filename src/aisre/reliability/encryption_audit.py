"""Artifact 13.5 T4.8 (NFR-19): the encryption audit, `python -m aisre.reliability.encryption_audit`.

NFR-19 asks that evidence be encrypted at rest and in transit, for every store and connection,
proved by a configuration audit. This reads what a process can see from its own settings and
connections, and says `pass`, `fail` or `unknown` for each:

- In transit: every configured URL the platform calls (database, object stores, Temporal, the
  model provider, the token issuer, the release process, the telemetry collector) uses TLS;
  each PostgreSQL role's own connection is encrypted (`pg_stat_ssl`); the server has `ssl` on.
- At rest: each object-store bucket has default server-side encryption (S3
  GetBucketEncryption). PostgreSQL's and Temporal's storage encryption is the disk's or the
  provider's, which no SQL can see, so it is `unknown` here and needs the platform's own
  evidence (the cloud volume or KMS settings).

What it can't prove anywhere: certificate validity and cipher strength (it checks that TLS is
asked for, not how good it is), key management, and anything outside these settings. On the
local Compose stack every in-transit check fails (plain HTTP and PostgreSQL without TLS) and
the buckets have no default encryption: NFR-19 is not met locally, and this audit says so.
Exit status 1 when any check fails.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from aisre.config import Settings
from aisre.evidence.snapshots import S3Snapshots, SnapshotFailed


@dataclass(frozen=True)
class Finding:
    target: str
    check: str  # in_transit | at_rest
    status: str  # pass | fail | unknown
    evidence: str


def url_tls(name: str, url: str | None) -> Finding | None:
    if not url:
        return None
    scheme = urlsplit(url if "://" in url else f"//{url}").scheme
    ok = scheme in ("https", "grpcs")
    return Finding(name, "in_transit", "pass" if ok else "fail", f"scheme {scheme or 'none'}")


def database(
    name: str, url: SecretStr, connect: Callable[[str], tuple[bool, bool]]
) -> list[Finding]:
    """`connect(url)` -> (server ssl on, this connection encrypted)."""
    try:
        server, mine = connect(url.get_secret_value())
    except SQLAlchemyError as e:
        return [Finding(name, "in_transit", "unknown", f"can't connect: {type(e).__name__}")]
    return [
        Finding(
            name,
            "in_transit",
            "pass" if mine else "fail",
            f"this connection {'is' if mine else 'is not'} encrypted; server ssl "
            f"{'on' if server else 'off'}",
        ),  # fmt: skip
        Finding(
            name, "at_rest", "unknown", "storage encryption is the volume's: not visible in SQL"
        ),
    ]


def pg_ssl(url: str) -> tuple[bool, bool]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            server = conn.execute(text("SHOW ssl")).scalar_one() == "on"
            mine = bool(
                conn.execute(
                    text("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
                ).scalar_one()
            )
        return server, mine
    finally:
        engine.dispose()


def bucket(name: str, store: S3Snapshots) -> Finding:
    """GetBucketEncryption: a configuration means objects are encrypted by default."""
    try:
        store._call("GET", "/" + store.bucket, {"encryption": ""})
    except SnapshotFailed as e:
        if "HTTP 404" in str(e):
            return Finding(name, "at_rest", "fail", "no default bucket encryption")
        return Finding(name, "at_rest", "unknown", str(e))
    return Finding(name, "at_rest", "pass", "default bucket encryption configured")


def audit(s: Settings, connect: Callable[[str], tuple[bool, bool]] = pg_ssl) -> list[Finding]:
    found: list[Finding | None] = []
    for name, url in (
        ("postgresql (aisre_app)", s.database_url),
        ("postgresql (aisre_dispatcher)", s.dispatcher_database_url),
    ):
        found += database(name, url, connect)
    found += [
        url_tls("temporal", s.temporal_address and f"grpc://{s.temporal_address}"),
        url_tls("object store", s.object_store_url),
        url_tls("audit store", s.audit_store_url),
        url_tls("model provider", s.provider_url or "https://api.anthropic.com"),
        url_tls("token issuer keys", s.oidc_jwks_url),
        url_tls("release process", s.release_url),
        url_tls("telemetry collector", s.otlp_endpoint),
    ]
    stores: list[tuple[str, str | None, str | None, str | None, SecretStr | None]] = [
        ("object store", s.object_store_url, s.object_store_bucket, s.object_store_access_key,
         s.object_store_secret_key),
        ("audit store", s.audit_store_url, s.audit_store_bucket, s.audit_store_access_key,
         s.audit_store_secret_key),
    ]  # fmt: skip
    for store_name, store_url, bkt, key, secret in stores:
        if store_url and bkt and key and secret:
            found.append(bucket(store_name, S3Snapshots(store_url, bkt, key, secret)))
    return [f for f in found if f is not None]


def main(argv: Sequence[str] | None = None) -> int:
    findings = audit(Settings.from_env())
    for f in findings:
        print(json.dumps(asdict(f)))
    return 1 if any(f.status == "fail" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
