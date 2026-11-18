"""Artifact 5 (Q5.20, ST-31, ST-32), ADR-57 (T1.20, FR-37): the write-once audit copy.

PostgreSQL holds the timeline and refuses to change it (append-only triggers, schema.sql), and
each event's hash covers the one before it. A person who can switch those triggers off, or
rewrite every hash after an edit, could still change history; the copy is what they can't
reach. Every timeline event, and every audit log line the API writes (sign-in failures,
denials, reads: aisre.api.audit), goes to a write-once store, and the weekly check compares
the two (aisre.reliability.audit_jobs).

**The store** (`WriteOnceStore`): `put_once` writes a key that doesn't exist, accepts the same
bytes again (a retried copy), and raises `AlreadyWritten` for different bytes. Nothing
overwrites or deletes. `MemoryStore` is the tests' store; `S3Store` is the object store
(MinIO in Compose): a conditional PUT (`If-None-Match: *`) per object, signed with SigV4, and,
when `lock_days` is set, S3 Object Lock in COMPLIANCE mode so not even the store's own
administrator can delete it before then (the bucket must have been created with locking).

**Layout:**

    timeline/<incident_id>/<seq, 12 digits>.json   one event, canonical JSON, with its hashes
    log/incident/<incident_id>/<uuid7>.jsonl        audit lines naming an incident
    log/auth/<YYYY-MM-DD>/<uuid7>.jsonl             failed sign-ins (no incident to name)
    log/other/<YYYY-MM-DD>/<uuid7>.jsonl            everything else

**The copier** (`copy_timelines`) keeps a per-incident cursor, the last copied seq
(`consumer_cursors`, `audit_copy:<incident_id>`), and copies the events after it.
**The check** (`check_chain`) runs `verify_timeline_chain` on every incident and compares
each copied event with the record: a missing, extra or different event is a fault.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from aisre.record.incidents import uuid7
from aisre.record.notifications import advance, cursor, cursors

CURSOR_PREFIX = "audit_copy:"
TIMELINE = "timeline/"
LOG = "log/"
BATCH = 500


class AlreadyWritten(Exception):
    """The key holds different bytes: the copy is never overwritten."""


class StoreUnavailable(Exception):
    """The store didn't answer, or refused the request."""


class WriteOnceStore(Protocol):
    def put_once(self, key: str, data: bytes) -> None: ...

    def get(self, key: str) -> bytes | None: ...

    def keys(self, prefix: str) -> list[str]: ...


class MemoryStore:
    """In memory, for tests: the same contract as S3Store, and no way to change an object."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put_once(self, key: str, data: bytes) -> None:
        with self._lock:
            held = self._objects.setdefault(key, data)
        if held != data:
            raise AlreadyWritten(key)

    def get(self, key: str) -> bytes | None:
        return self._objects.get(key)

    def keys(self, prefix: str) -> list[str]:
        return sorted(k for k in list(self._objects) if k.startswith(prefix))


def canonical(doc: Any) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str).encode()


# ---------------------------------------------------------------------------
# S3 (MinIO): standard library only, path-style URLs, Signature Version 4
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class S3Config:
    endpoint: str  # e.g. http://minio:9000
    bucket: str
    access_key: str
    secret_key: str
    region: str = "us-east-1"
    lock_days: int | None = None  # Object Lock retention, COMPLIANCE mode


ENV_VARS = {
    "endpoint": "AISRE_AUDIT_STORE_URL",
    "bucket": "AISRE_AUDIT_STORE_BUCKET",
    "access_key": "AISRE_AUDIT_STORE_ACCESS_KEY",
    "secret_key": "AISRE_AUDIT_STORE_SECRET_KEY",
    "lock_days": "AISRE_AUDIT_STORE_LOCK_DAYS",
}


def store_from_env(environ: Mapping[str, str]) -> S3Store | None:
    """The S3 store from the AISRE_AUDIT_STORE_* variables, or None if the URL is unset."""
    if not environ.get(ENV_VARS["endpoint"]):
        return None
    missing = [v for k, v in ENV_VARS.items() if k != "lock_days" and not environ.get(v)]
    if missing:
        raise RuntimeError(f"audit store: set {', '.join(missing)}")
    lock = environ.get(ENV_VARS["lock_days"])
    return S3Store(
        S3Config(
            environ[ENV_VARS["endpoint"]],
            environ[ENV_VARS["bucket"]],
            environ[ENV_VARS["access_key"]],
            environ[ENV_VARS["secret_key"]],
            lock_days=int(lock) if lock else None,
        )
    )


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


class S3Store:
    TIMEOUT_S = 10

    def __init__(self, config: S3Config) -> None:
        self.c = config
        self.host = urlsplit(config.endpoint).netloc

    def _request(
        self,
        method: str,
        key: str = "",
        *,
        query: Mapping[str, str] | None = None,
        body: bytes = b"",
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, bytes]:
        path = f"/{self.c.bucket}/{key}" if key else f"/{self.c.bucket}"
        q = dict(query or {})
        h = {k.lower(): v for k, v in (headers or {}).items()}
        now = datetime.now(UTC)
        amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        payload = hashlib.sha256(body).hexdigest()
        h |= {"host": self.host, "x-amz-date": amz_date, "x-amz-content-sha256": payload}
        signed = sorted(h)
        canonical_query = "&".join(
            f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(q.items())
        )
        canonical_request = "\n".join(
            [
                method,
                quote(path, safe="/-_.~"),
                canonical_query,
                "".join(f"{k}:{h[k].strip()}\n" for k in signed),
                ";".join(signed),
                payload,
            ]
        )
        scope = f"{day}/{self.c.region}/s3/aws4_request"
        to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        k = _hmac(("AWS4" + self.c.secret_key).encode(), day)
        for part in (self.c.region, "s3", "aws4_request"):
            k = _hmac(k, part)
        signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
        h["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.c.access_key}/{scope}, "
            f"SignedHeaders={';'.join(signed)}, Signature={signature}"
        )
        url = self.c.endpoint.rstrip("/") + quote(path, safe="/-_.~")
        if canonical_query:
            url += "?" + canonical_query
        del h["host"]
        req = Request(url, data=body if method == "PUT" else None, method=method, headers=h)
        try:
            with urlopen(req, timeout=self.TIMEOUT_S) as resp:  # noqa: S310 (configured URL)
                return int(resp.status), bytes(resp.read())
        except HTTPError as e:
            return int(e.code), bytes(e.read())
        except OSError as e:
            raise StoreUnavailable(str(e)) from e

    def put_once(self, key: str, data: bytes) -> None:
        headers = {"If-None-Match": "*", "Content-Type": "application/json"}
        if self.c.lock_days:
            until = datetime.now(UTC) + timedelta(days=self.c.lock_days)
            headers |= {
                "Content-MD5": base64.b64encode(hashlib.md5(data).digest()).decode(),  # noqa: S324 (S3 requires it)
                "x-amz-object-lock-mode": "COMPLIANCE",
                "x-amz-object-lock-retain-until-date": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        status, body = self._request("PUT", key, body=data, headers=headers)
        if status in (409, 412):  # the key exists: the same bytes are a retried copy
            if self.get(key) != data:
                raise AlreadyWritten(key)
            return
        if status >= 300:
            raise StoreUnavailable(f"PUT {key}: {status} {body[:200]!r}")

    def get(self, key: str) -> bytes | None:
        status, body = self._request("GET", key)
        if status == 404:
            return None
        if status >= 300:
            raise StoreUnavailable(f"GET {key}: {status}")
        return body

    def keys(self, prefix: str) -> list[str]:
        found: list[str] = []
        token: str | None = None
        while True:
            query = {"list-type": "2", "prefix": prefix}
            if token:
                query["continuation-token"] = token
            status, body = self._request("GET", query=query)
            if status >= 300:
                raise StoreUnavailable(f"LIST {prefix}: {status}")
            root = ET.fromstring(body)
            ns = root.tag[: root.tag.index("}") + 1] if root.tag.startswith("{") else ""
            found += [e.text or "" for e in root.iter(f"{ns}Key")]
            token = root.findtext(f"{ns}NextContinuationToken")
            if root.findtext(f"{ns}IsTruncated") != "true" or not token:
                return sorted(found)


# ---------------------------------------------------------------------------
# Copying and checking the timeline
# ---------------------------------------------------------------------------
EVENT_COLUMNS = (
    "event_id, incident_id, seq, type, source, actor, occurred_at, recorded_at, causation_id, "
    "correlation_id, traceparent, dataschema, data, schema_version, prev_hash, hash"
)


def event_key(incident_id: UUID | str, seq: int) -> str:
    return f"{TIMELINE}{incident_id}/{seq:012d}.json"


def _event_doc(row: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(canonical(dict(row)))  # type: ignore[no-any-return]


def copy_timelines(engine: Engine, store: WriteOnceStore, *, limit: int = BATCH) -> int:
    """Copy every event not yet copied; returns how many. One transaction per incident, the
    cursor moved only after its objects are written, so a crash copies again (harmlessly)."""
    copied = 0
    with engine.connect() as conn:
        done = {k[len(CURSOR_PREFIX) :]: int(v) for k, v in cursors(conn, CURSOR_PREFIX).items()}
        behind = [
            str(r[0])
            for r in conn.execute(text("SELECT incident_id, last_event_seq FROM incidents"))
            if int(r[1]) > done.get(str(r[0]), 0)
        ]
    for incident_id in behind:
        with engine.begin() as conn:
            consumer = CURSOR_PREFIX + incident_id
            after = int(cursor(conn, consumer) or 0)
            rows = conn.execute(
                text(
                    f"SELECT {EVENT_COLUMNS} FROM timeline_events WHERE incident_id = :i "
                    "AND seq > :after ORDER BY seq LIMIT :limit"
                ),
                {"i": incident_id, "after": after, "limit": limit},
            ).mappings()
            last = after
            for row in rows:
                store.put_once(event_key(incident_id, int(row["seq"])), canonical(dict(row)))
                last = int(row["seq"])
                copied += 1
            if last > after:
                advance(conn, consumer, str(last))
    return copied


@dataclass(frozen=True)
class Fault:
    incident_id: str
    at_seq: int
    problem: str


def compare(
    incident_id: str, record: Iterable[Mapping[str, Any]], copy: Mapping[int, bytes], upto: int
) -> list[Fault]:
    """The record's events against their copies, up to `upto` (the last copied seq)."""
    faults = []
    seen = set()
    for row in record:
        seq = int(row["seq"])
        seen.add(seq)
        if seq > upto:
            continue
        held = copy.get(seq)
        if held is None:
            faults.append(Fault(incident_id, seq, "missing from the write-once copy"))
        elif json.loads(held) != _event_doc(row):
            faults.append(Fault(incident_id, seq, "differs from the write-once copy"))
    for seq in sorted(set(copy) - seen):
        faults.append(Fault(incident_id, seq, "in the write-once copy but not in the record"))
    return faults


def check_chain(conn: Connection, store: WriteOnceStore | None) -> list[Fault]:
    """verify_timeline_chain on every incident, and the record against the copy (if any)."""
    faults: list[Fault] = []
    done = {k[len(CURSOR_PREFIX) :]: int(v) for k, v in cursors(conn, CURSOR_PREFIX).items()}
    for (incident_id,) in conn.execute(text("SELECT incident_id FROM incidents ORDER BY 1")):
        iid = str(incident_id)
        faults += [
            Fault(iid, int(r[0]), str(r[1]))
            for r in conn.execute(
                text("SELECT at_seq, problem FROM verify_timeline_chain(:i)"), {"i": iid}
            )
        ]
        if store is None:
            continue
        upto = done.get(iid, 0)
        prefix = f"{TIMELINE}{iid}/"
        copy: dict[int, bytes] = {}
        for key in store.keys(prefix):
            data = store.get(key)
            if data is not None:
                copy[int(key[len(prefix) : -len(".json")])] = data
        if not upto and not copy:
            continue
        record = conn.execute(
            text(
                f"SELECT {EVENT_COLUMNS} FROM timeline_events WHERE incident_id = :i ORDER BY seq"
            ),
            {"i": iid},
        ).mappings()
        faults += compare(iid, [dict(r) for r in record], copy, upto)
    return faults


def copied_timeline(store: WriteOnceStore, incident_id: UUID | str) -> list[dict[str, Any]]:
    prefix = f"{TIMELINE}{incident_id}/"
    out = []
    for key in store.keys(prefix):
        data = store.get(key)
        if data is not None:
            out.append(json.loads(data))
    return out


# ---------------------------------------------------------------------------
# Audit log lines
# ---------------------------------------------------------------------------
def log_key(record: Mapping[str, Any], now: datetime | None = None) -> str:
    day = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    if record.get("incident_id"):
        return f"{LOG}incident/{record['incident_id']}/"
    if record.get("event") == "aisre.auth.failed":
        return f"{LOG}auth/{day}/"
    return f"{LOG}other/{day}/"


def write_log_lines(store: WriteOnceStore, records: Sequence[Mapping[str, Any]]) -> None:
    """One object per group of lines, under a fresh UUIDv7 name."""
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for r in records:
        groups.setdefault(log_key(r), []).append(r)
    for prefix, lines in groups.items():
        body = b"".join(canonical(line) + b"\n" for line in lines)
        store.put_once(f"{prefix}{uuid7()}.jsonl", body)


def read_log_lines(store: WriteOnceStore, prefix: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in store.keys(prefix):
        data = store.get(key) or b""
        out += [json.loads(line) for line in data.splitlines() if line.strip()]
    return out
