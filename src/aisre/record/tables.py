"""Artifact 7.2: the Incident Record's tables, as SQLAlchemy Core metadata (ADR-04).

Only the tables and columns this package reads or writes, declared to match db/schema.sql,
which stays the definition: nothing here creates or alters a table (migrations do, ADR-05).
Columns the database fills (defaults, and what the timeline trigger assigns) are listed so
they can be read back. Enum columns use the schema's own types, so a bound value is cast to
`incident_status` and not to text.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Integer,
    MetaData,
    SmallInteger,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB, TIMESTAMP, UUID

from aisre.contracts import ActionStatus, IncidentStatus, Severity

metadata = MetaData()

Timestamp = TIMESTAMP(timezone=True)
incident_status = ENUM(
    *(s.value for s in IncidentStatus), name="incident_status", create_type=False
)
action_status = ENUM(*(s.value for s in ActionStatus), name="action_status", create_type=False)
severity = ENUM(*(s.value for s in Severity), name="severity", create_type=False)

services = Table(
    "services",
    metadata,
    Column("service_id", UUID, primary_key=True),
    Column("org_id", UUID, nullable=False),
    Column("slug", Text, nullable=False),
    Column("display_name", Text, nullable=False),
    Column("owner_group", Text, nullable=False),
    Column("critical", Boolean, nullable=False),
    Column("hold_seconds", Integer, nullable=False),
)

environments = Table(
    "environments",
    metadata,
    Column("environment_id", UUID, primary_key=True),
    Column("org_id", UUID, nullable=False),
    Column("slug", Text, nullable=False),
)

incidents = Table(
    "incidents",
    metadata,
    Column("incident_id", UUID, primary_key=True),
    Column("org_id", UUID, nullable=False),
    Column("display_ref", Text, nullable=False),
    Column("service_id", UUID, nullable=False),
    Column("environment_id", UUID, nullable=False),
    Column("dedup_key", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("started_at", Timestamp, nullable=False),
    Column("status", incident_status, nullable=False),
    Column("proposed_severity", severity),
    Column("confirmed_severity", severity),
    Column("investigation_run", SmallInteger, nullable=False),
    Column("version", Integer, nullable=False),
    Column("last_event_seq", BigInteger, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("created_at", Timestamp, nullable=False),
    Column("updated_at", Timestamp, nullable=False),
)

incident_alerts = Table(
    "incident_alerts",
    metadata,
    Column("alert_id", UUID, primary_key=True),
    Column("incident_id", UUID, nullable=False),
    Column("source_alert_id", Text),
    Column("fingerprint", Text, nullable=False),
    Column("received_at", Timestamp, nullable=False),
    Column("payload", JSONB, nullable=False),
)

# seq, prev_hash, hash and recorded_at are assigned by the t30_assign trigger.
timeline_events = Table(
    "timeline_events",
    metadata,
    Column("event_id", UUID, primary_key=True),
    Column("incident_id", UUID, nullable=False),
    Column("seq", BigInteger),
    Column("type", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("actor", Text, nullable=False),
    Column("occurred_at", Timestamp, nullable=False),
    Column("recorded_at", Timestamp),
    Column("causation_id", UUID),
    Column("correlation_id", UUID, nullable=False),
    Column("traceparent", Text),
    Column("dataschema", Text),
    Column("data", JSONB, nullable=False),
    Column("schema_version", SmallInteger),
    Column("prev_hash", Text),
    Column("hash", Text),
)

# The CloudEvents view of the timeline (ADR-64): envelope names as on the wire.
timeline_cloudevents = Table(
    "timeline_cloudevents",
    metadata,
    Column("id", UUID),
    Column("specversion", Text),
    Column("source", Text),
    Column("type", Text),
    Column("subject", Text),
    Column("time", Timestamp),
    Column("datacontenttype", Text),
    Column("dataschema", Text),
    Column("incidentid", UUID),
    Column("sequence", BigInteger),
    Column("orgid", UUID),
    Column("actor", Text),
    Column("correlationid", UUID),
    Column("causationid", UUID),
    Column("traceparent", Text),
    Column("data", JSONB),
)

outbox = Table(
    "outbox",
    metadata,
    Column("outbox_id", BigInteger, primary_key=True),  # GENERATED ALWAYS: never inserted
    Column("message_id", UUID, nullable=False),
    Column("incident_id", UUID, nullable=False),
    Column("kind", Text, nullable=False),
    Column("workflow_id", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("created_at", Timestamp, nullable=False),
    Column("available_at", Timestamp, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("last_error", Text),
    Column("delivered_at", Timestamp),
)

idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    Column("caller_id", Text, primary_key=True),
    Column("operation", Text, primary_key=True),
    Column("idem_key", Text, primary_key=True),
    Column("request_sha256", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("response_status", SmallInteger),
    Column("response_body", JSONB),
    Column("resource_id", UUID),
    Column("created_at", Timestamp, nullable=False),
    Column("expires_at", Timestamp, nullable=False),
)

incident_budgets = Table(
    "incident_budgets",
    metadata,
    Column("incident_id", UUID, primary_key=True),
    Column("max_calls", Integer, nullable=False),
    Column("used_calls", Integer, nullable=False),
    Column("max_cost_micros", BigInteger, nullable=False),
    Column("spent_cost_micros", BigInteger, nullable=False),
    Column("reserved_cost_micros", BigInteger, nullable=False),
)

diagnoses = Table(
    "diagnoses",
    metadata,
    Column("diagnosis_id", UUID, primary_key=True),
    Column("incident_id", UUID, nullable=False),
    Column("revision", SmallInteger, nullable=False),
)

actions = Table(
    "actions",
    metadata,
    Column("action_id", UUID, primary_key=True),
    Column("incident_id", UUID, nullable=False),
    Column("status", action_status, nullable=False),
    Column("version", Integer, nullable=False),
    Column("created_at", Timestamp, nullable=False),
)

investigation_queue = Table(
    "investigation_queue",
    metadata,
    Column("incident_id", UUID, primary_key=True),
    Column("run_id", Text, nullable=False),
    Column("severity", severity),
    Column("queued_at", Timestamp, nullable=False),
    Column("granted_at", Timestamp),
)
