"""T4.8 (Artifact 12.7; NFR-20, NFR-45; ADR-57): the retention job, as aisre_retention, against the
stack's PostgreSQL and MinIO.

Planted in a fresh organisation: incident A closed two years ago, incident C closed today and
incident B still open, each with an object in the store whose `retain_until` has passed; plus an
expired dead letter and an expired idempotency key. Back-dating uses the superuser in replica
mode, as db/schema-tests.sql does (J9): nothing in the product can write the past.

After one run: A's object and its whole record are gone (purged); C's object and every version
of it are gone, with an `artifact_deletions` row, while C's record stays (a year); B's object is
kept and counted as held (an open incident holds what it references, Q12.48); the dead letter
and the key are gone; and the audit query finds nothing overdue but what is held. Deleting
early is refused by the schema whoever asks (NFR-45's "deleted early: 0").
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import add_catalogue
from pydantic import SecretStr
from sqlalchemy import create_engine, func, text, update
from sqlalchemy.exc import DBAPIError

from aisre.contracts import IncidentCreate
from aisre.evidence.snapshots import S3Snapshots, SnapshotFailed
from aisre.record import retention, uuid7
from aisre.record.incidents import create_incident
from aisre.record.tables import outbox

SUPERUSER = "postgresql+psycopg://postgres:fake-superuser-password@127.0.0.1:55432/aisre"
RETENTION = "postgresql+psycopg://aisre_retention:fake-retention-password@127.0.0.1:55432/aisre"


@pytest.fixture
def engines():
    su, ret = create_engine(SUPERUSER), create_engine(RETENTION)
    try:
        for e in (su, ret):
            with e.connect() as conn:
                conn.execute(text("SELECT 1"))
    except DBAPIError as e:
        pytest.skip(f"needs the stack's superuser and aisre_retention (README): {e.orig}")
    yield su, ret
    su.dispose()
    ret.dispose()


@pytest.fixture
def store():
    s = S3Snapshots(
        "http://127.0.0.1:59000",
        "aisre-artifacts",
        "fake-minio-admin",
        SecretStr("fake-minio-password"),
    )
    try:
        s.put(f"retention-probe/{uuid.uuid4()}", b"x", "text/plain")
    except SnapshotFailed as e:
        pytest.skip(f"the stack's object store isn't answering: {e}")
    return s


def plant(app_engine, store, org, n: int) -> tuple[str, str]:
    """An incident (withheld from the relay) with one stored, expired evidence snapshot."""
    with app_engine.begin() as conn:
        iid = create_incident(
            conn,
            org_id=org,
            caller_id="dev:retention",
            body=IncidentCreate(
                service="retention-svc",
                environment="production",
                started_at=datetime.now(UTC),
                title=f"retention {n}",
                source="monitoring",
                fingerprint=f"retention-{uuid.uuid4()}",
            ),
            grouping_window=timedelta(0),
        ).incident_id
        conn.execute(
            update(outbox).where(outbox.c.incident_id == iid).values(delivered_at=func.now())
        )
        key = f"retention-test/{iid}/snapshot-{n}"
        body = f"raw evidence {n}".encode()
        store.put(key, body, "text/plain")
        store.put(key, body + b" v2", "text/plain")  # a second version, which must go too
        conn.execute(
            text(
                "INSERT INTO artifacts (artifact_id, org_id, incident_id, kind, storage_key, "
                "content_sha256, size_bytes, media_type, redacted, retain_until) VALUES "
                "(:a, :o, :i, 'evidence_snapshot', :k, :h, :s, 'text/plain', true, "
                "now() - interval '1 day')"
            ),
            {
                "a": uuid7(),
                "o": org,
                "i": iid,
                "k": key,
                "h": hashlib.sha256(body).hexdigest(),
                "s": len(body),
            },
        )
    return str(iid), key


def gone(store, key: str) -> bool:
    try:
        store.get(key)
    except SnapshotFailed:
        return True
    return False


def test_the_retention_job_deletes_what_has_expired_and_nothing_held(app_engine, engines, store):
    su, ret = engines
    org = uuid7()
    add_catalogue(app_engine, org, "retention-svc")
    a, a_key = plant(app_engine, store, org, 1)
    b, b_key = plant(app_engine, store, org, 2)
    c, c_key = plant(app_engine, store, org, 3)
    with su.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        for iid, closed in ((a, "now() - interval '2 years'"), (c, "now()")):
            conn.execute(
                text(
                    f"UPDATE incidents SET status = 'closed', closed_at = {closed} "  # noqa: S608
                    "WHERE incident_id = :i"
                ),  # fmt: skip
                {"i": iid},
            )
    caller = f"dev:retention-{uuid.uuid4().hex[:8]}"
    with app_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO dead_letters (dead_letter_id, org_id, caller_id, reason, raw_payload, "
                "retain_until) VALUES (:d, :o, :c, 'unknown_service', '{}', now() - interval '1 s')"
            ),
            {"d": uuid7(), "o": org, "c": caller},
        )
        conn.execute(
            text(
                "INSERT INTO idempotency_keys (caller_id, operation, idem_key, request_sha256, "
                "state, expires_at) VALUES (:c, 'POST /v1/incidents', 'retention-key-1', :h, "
                "'in_progress', now() - interval '1 s')"
            ),
            {"c": caller, "h": "0" * 64},
        )

    # Early: C's record is a year from due, so nobody may delete it, the retention role included.
    with pytest.raises(DBAPIError, match="not yet due"), ret.begin() as conn:
        conn.execute(text("DELETE FROM timeline_events WHERE incident_id = :i"), {"i": c})

    report = retention.run(ret, store.delete)
    print(f"retention run: {report}")
    assert report.failed == []
    assert report.objects_deleted >= 2 and report.held >= 1 and report.incidents_purged >= 1

    with app_engine.connect() as conn:

        def count(sql: str, **kw: object) -> int:
            return int(conn.execute(text(sql), kw).scalar_one())

        assert count("SELECT count(*) FROM incidents WHERE incident_id = :i", i=a) == 0
        assert count("SELECT count(*) FROM timeline_events WHERE incident_id = :i", i=a) == 0
        assert count("SELECT count(*) FROM incidents WHERE incident_id = :i", i=c) == 1
        assert count("SELECT count(*) FROM artifact_deletions WHERE incident_id = :i", i=c) == 1
        assert count("SELECT count(*) FROM artifact_deletions WHERE incident_id = :i", i=b) == 0
        assert count("SELECT count(*) FROM dead_letters WHERE caller_id = :c", c=caller) == 0
        assert count("SELECT count(*) FROM idempotency_keys WHERE caller_id = :c", c=caller) == 0
    with ret.connect() as conn:  # the audit query runs as the job does
        left = retention.overdue(conn)
    assert gone(store, a_key) and gone(store, c_key) and not gone(store, b_key)
    assert left["objects"] == left["incidents"] == 0
    assert left["dead_letters"] == left["idempotency_keys"] == 0
    assert left["held"] >= 1
