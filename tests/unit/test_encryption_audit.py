"""T4.8 (NFR-19): the encryption audit's rules, offline. On the stack: README, M4 record."""

from __future__ import annotations

from aisre.config import Settings
from aisre.reliability.encryption_audit import audit, url_tls


def test_plain_urls_fail_and_tls_passes():
    assert url_tls("x", "http://minio:9000").status == "fail"  # type: ignore[union-attr]
    assert url_tls("x", "https://s3.example").status == "pass"  # type: ignore[union-attr]
    assert url_tls("x", None) is None  # not configured: nothing to audit


def test_the_database_connection_must_itself_be_encrypted():
    s = Settings(env="test", otlp_endpoint="https://otel")
    plain = {f.target + f.check: f for f in audit(s, lambda url: (True, False))}
    assert plain["postgresql (aisre_app)in_transit"].status == "fail"  # server on, mine not
    tls = {f.target + f.check: f for f in audit(s, lambda url: (True, True))}
    assert tls["postgresql (aisre_app)in_transit"].status == "pass"
    # Storage encryption is the volume's: never claimed from inside the database.
    assert tls["postgresql (aisre_app)at_rest"].status == "unknown"
    assert tls["temporal" + "in_transit"].status == "fail"  # no TLS setting for Temporal yet
