"""Artifact 7.4 (T1.5): the signed timeline export, `GET /v1/incidents/{id}/timeline/export`.

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
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import Connection, text

from aisre.record import timeline

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
    def from_env(cls, env: str, environ: Mapping[str, str] | None = None) -> ExportSigner:
        seed = (os.environ if environ is None else environ).get(KEY_VAR)
        if seed:
            key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed, validate=True))
            label = "export"
        elif env == "production":
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
