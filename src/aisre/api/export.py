"""Artifact 7.4 (T1.5, T1.20): the signed exports.

`GET /v1/incidents/{id}/timeline/export` (incident:read) reads the Incident Record.
`GET /v1/incidents/{id}/audit/export` (audit:export, ADR-50) reads the write-once audit copy
(aisre.record.audit_copy), never the record: the timeline as copied, every audit line naming
the incident (reads, denials), and the failed sign-ins from the incident's opening to now. It
says whether the copy has caught up with the record (`copy_complete`, by the last seq).

One JSON document: every event as a CloudEvent with its stored `hash` and `prev_hash` (the
audit chain, schema.sql section 12), the chain's head, the faults `verify_timeline_chain`
finds (none, normally), who exported it and when. It is signed with Ed25519 over its
canonical JSON (sorted keys, no spaces), everything but `signature` itself:

    "signature": {"alg": "Ed25519", "key_id": ..., "public_key": <base64>, "value": <base64>}

`verify` checks a document against a public key. The key comes from
`AISRE_EXPORT_SIGNING_KEY`, a base64 32-byte Ed25519 seed. Without it, development and test
sign with a key made at start-up (`key_id` "ephemeral-..."), which proves the file is whole
but not who made it; production refuses to start without one. Where the key is kept, and
how readers learn the public key, is for the key-management work (Security lead, M3).
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import Connection, text

from aisre.config import Settings
from aisre.contracts import IncidentView
from aisre.record import timeline
from aisre.record.audit_copy import LOG, WriteOnceStore, copied_timeline, read_log_lines

KEY_VAR = "AISRE_EXPORT_SIGNING_KEY"
PAGE = 500


def canonical(doc: Mapping[str, Any]) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str).encode()


class ExportSigner:
    def __init__(self, key: Ed25519PrivateKey, key_id: str) -> None:
        self.key, self.key_id = key, key_id
        raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.public_key = base64.b64encode(raw).decode()

    @classmethod
    def from_settings(cls, settings: Settings) -> ExportSigner:
        """The key from AISRE_EXPORT_SIGNING_KEY; without one, a throwaway key outside
        production."""
        seed = settings.export_signing_key
        if seed is not None:
            raw_seed = base64.b64decode(seed.get_secret_value(), validate=True)
            key = Ed25519PrivateKey.from_private_bytes(raw_seed)
            label = "export"
        elif settings.env == "production":
            raise RuntimeError(f"{KEY_VAR} is required in production: exports must be signed")
        else:
            key, label = Ed25519PrivateKey.generate(), "ephemeral"
        raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return cls(key, f"{label}-{hashlib.sha256(raw).hexdigest()[:16]}")

    def sign(self, doc: dict[str, Any]) -> dict[str, Any]:
        value = base64.b64encode(self.key.sign(canonical(doc))).decode()
        return doc | {
            "signature": {
                "alg": "Ed25519",
                "key_id": self.key_id,
                "public_key": self.public_key,
                "value": value,
            }
        }


def verify(doc: Mapping[str, Any], public_key: str) -> bool:
    """True if `doc` is whole and was signed by the key whose base64 public bytes are given."""
    sig = doc.get("signature")
    if not isinstance(sig, Mapping) or sig.get("alg") != "Ed25519":
        return False
    body = {k: v for k, v in doc.items() if k != "signature"}
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        key.verify(base64.b64decode(str(sig.get("value")), validate=True), canonical(body))
    except (InvalidSignature, ValueError):
        return False
    return True


def timeline_document(
    conn: Connection, incident_id: UUID, org_id: UUID, exported_by: str
) -> dict[str, Any]:
    """The unsigned export: events with their chain hashes, the head and any faults."""
    events: list[dict[str, Any]] = []
    after = 0
    while True:
        page = timeline(conn, incident_id, org_id, after_seq=after, limit=PAGE)
        events += [e.model_dump(mode="json") for e in page]
        if len(page) < PAGE:
            break
        after = page[-1].sequence
    hashes = {
        int(r.seq): (r.hash, r.prev_hash)
        for r in conn.execute(
            text("SELECT seq, hash, prev_hash FROM timeline_events WHERE incident_id = :i"),
            {"i": incident_id},
        )
    }
    for e in events:
        e["hash"], e["prev_hash"] = hashes[e["sequence"]]
    faults = [
        {"at_seq": int(r.at_seq), "problem": r.problem}
        for r in conn.execute(
            text("SELECT at_seq, problem FROM verify_timeline_chain(:i)"), {"i": incident_id}
        )
    ]
    return {
        "kind": "aisre.timeline_export.v1",
        "incident_id": str(incident_id),
        "exported_at": datetime.now(UTC).isoformat(),
        "exported_by": exported_by,
        "events": events,
        "chain_head": events[-1]["hash"] if events else None,
        "chain_faults": faults,
    }


def audit_document(
    store: WriteOnceStore, view: IncidentView, exported_by: str, now: datetime | None = None
) -> dict[str, Any]:
    """The unsigned audit export, read back from the write-once copy."""
    now = now or datetime.now(UTC)
    opened = view.created_at.astimezone(UTC)
    events = copied_timeline(store, view.incident_id)
    access = read_log_lines(store, f"{LOG}incident/{view.incident_id}/")
    sign_ins: list[dict[str, Any]] = []
    day = opened.date()
    while day <= now.date():
        for line in read_log_lines(store, f"{LOG}auth/{day.isoformat()}/"):
            if str(line.get("time", "")) >= opened.isoformat():
                sign_ins.append(line)
        day += timedelta(days=1)
    copied_to = max((int(e["seq"]) for e in events), default=0)
    return {
        "kind": "aisre.audit_export.v1",
        "incident_id": str(view.incident_id),
        "exported_at": now.isoformat(),
        "exported_by": exported_by,
        "source": "write-once audit copy",
        "copy_complete": copied_to >= view.last_event_seq,
        "timeline": events,
        "access_log": sorted(access, key=lambda r: str(r.get("time", ""))),
        "failed_sign_ins": sorted(sign_ins, key=lambda r: str(r.get("time", ""))),
    }
