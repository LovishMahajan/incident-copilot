-- Tables, types and keys only, for importing into a DB diagram tool.
-- Full schema with triggers and indexes: schema.sql

-- ---------------------------------------------------------------------------
-- Enumerations
-- ---------------------------------------------------------------------------
CREATE TYPE incident_status AS ENUM (
  'open', 'investigating', 'diagnosed', 'inconclusive', 'awaiting_approval',
  'executing', 'verifying', 'recovered', 'not_recovered', 'handed_over',
  'cancelled', 'closed');
CREATE TYPE severity AS ENUM ('sev1', 'sev2', 'sev3', 'sev4');
CREATE TYPE evidence_kind AS ENUM (
  'alert', 'metric', 'log', 'trace', 'error_group', 'change', 'dependency',
  'past_incident', 'runbook', 'test_result', 'gap');
CREATE TYPE hypothesis_status AS ENUM ('open', 'supported', 'ruled_out', 'confirmed');
CREATE TYPE evidence_relation AS ENUM ('supports', 'contradicts');
CREATE TYPE confidence AS ENUM ('low', 'medium', 'high');
CREATE TYPE diagnosis_outcome AS ENUM ('established', 'inconclusive');
CREATE TYPE action_kind AS ENUM ('config_revert', 'commit_revert', 'release_rollback');
CREATE TYPE action_status AS ENUM (
  'proposed', 'tested', 'test_failed', 'awaiting_approval', 'approved',
  'rejected', 'expired', 'executed', 'undone', 'withdrawn');
CREATE TYPE test_outcome AS ENUM ('passed', 'failed', 'not_reproduced', 'error');
CREATE TYPE approval_decision AS ENUM ('approved', 'rejected');
CREATE TYPE execution_outcome AS ENUM ('started', 'applied', 'not_applied', 'unknown', 'failed');
CREATE TYPE verification_result AS ENUM ('recovered', 'not_recovered', 'inconclusive');
CREATE TYPE model_attempt_status AS ENUM ('succeeded', 'invalid_output', 'timeout', 'provider_error', 'refused_by_budget');
CREATE TYPE reservation_status AS ENUM ('reserved', 'settled', 'released');
CREATE TYPE artifact_kind AS ENUM (
  'evidence_snapshot', 'patch_diff', 'test_output', 'model_input', 'model_output',
  'execution_response', 'report');

-- ---------------------------------------------------------------------------
-- Catalogue
-- ---------------------------------------------------------------------------
CREATE TABLE services (
  service_id     uuid PRIMARY KEY,
  org_id         uuid NOT NULL,
  slug           text NOT NULL,             -- matches the service catalogue
  display_name   text NOT NULL,
  owner_group    text NOT NULL,             -- identity-system group
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, slug)
);

CREATE TABLE environments (
  environment_id uuid PRIMARY KEY,
  org_id         uuid NOT NULL,
  slug           text NOT NULL,             -- e.g. 'production'
  UNIQUE (org_id, slug)
);

-- ---------------------------------------------------------------------------
-- Incidents
-- ---------------------------------------------------------------------------
CREATE TABLE incidents (
  incident_id        uuid PRIMARY KEY,
  org_id             uuid NOT NULL,
  display_ref        text NOT NULL UNIQUE,  -- e.g. INC-2026-000123, for people
  service_id         uuid NOT NULL REFERENCES services,
  environment_id     uuid NOT NULL REFERENCES environments,
  dedup_key          text NOT NULL,
  title              text NOT NULL,
  source             text NOT NULL,         -- 'monitoring' or 'engineer'
  started_at         timestamptz NOT NULL,
  status             incident_status NOT NULL DEFAULT 'open',
  proposed_severity  severity,
  confirmed_severity severity,
  severity_confirmed_by text,
  version            integer NOT NULL DEFAULT 1,   -- optimistic concurrency
  last_event_seq     bigint  NOT NULL DEFAULT 0,   -- per-incident event sequence
  created_by         text NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  closed_at          timestamptz
);

-- One open incident per deduplication key (FR-03).

-- Every alert received, including duplicates (FR-03).
CREATE TABLE incident_alerts (
  alert_id          uuid PRIMARY KEY,
  incident_id       uuid NOT NULL REFERENCES incidents,
  source_alert_id   text,                   -- monitoring's own identifier
  fingerprint       text NOT NULL,
  received_at       timestamptz NOT NULL DEFAULT now(),
  payload           jsonb NOT NULL,
  UNIQUE (incident_id, source_alert_id, received_at)
);

-- ---------------------------------------------------------------------------
-- Artifacts (large items live in the object store; this is the reference)
-- ---------------------------------------------------------------------------
CREATE TABLE artifacts (
  artifact_id    uuid PRIMARY KEY,
  org_id         uuid NOT NULL,
  incident_id    uuid NOT NULL REFERENCES incidents,
  kind           artifact_kind NOT NULL,
  storage_key    text NOT NULL UNIQUE,      -- internal only; never returned by the API
  content_sha256 char(64) NOT NULL,
  size_bytes     bigint NOT NULL CHECK (size_bytes >= 0),
  media_type     text NOT NULL,
  redacted       boolean NOT NULL,
  retain_until   timestamptz NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Evidence
-- ---------------------------------------------------------------------------
CREATE TABLE evidence (
  evidence_id     uuid PRIMARY KEY,
  incident_id     uuid NOT NULL REFERENCES incidents,
  kind            evidence_kind NOT NULL,
  source_system   text NOT NULL,            -- which tool
  collected_by    text NOT NULL,            -- component or person
  query           text,                     -- the query used, if any
  window_start    timestamptz,
  window_end      timestamptz,
  collected_at    timestamptz NOT NULL DEFAULT now(),
  source_link     text,                     -- link to the original
  summary         text NOT NULL,            -- short, redacted extract
  attributes      jsonb NOT NULL DEFAULT '{}'::jsonb,  -- kind-specific fields
  artifact_id     uuid REFERENCES artifacts,          -- snapshot of the raw data
  content_sha256  char(64) NOT NULL,
  is_partial      boolean NOT NULL DEFAULT false,
  flagged_injection boolean NOT NULL DEFAULT false,
  CHECK (window_end IS NULL OR window_start IS NULL OR window_end >= window_start)
);

-- ---------------------------------------------------------------------------
-- Hypotheses and diagnoses
-- ---------------------------------------------------------------------------
CREATE TABLE hypotheses (
  hypothesis_id   uuid PRIMARY KEY,
  incident_id     uuid NOT NULL REFERENCES incidents,
  ordinal         smallint NOT NULL,        -- H1, H2 ... within the incident
  statement       text NOT NULL,
  confirm_test    text NOT NULL,            -- what would confirm it
  refute_test     text NOT NULL,            -- what would rule it out
  status          hypothesis_status NOT NULL DEFAULT 'open',
  confidence      confidence NOT NULL,
  status_reason   text,
  version         integer NOT NULL DEFAULT 1,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (incident_id, ordinal),
  UNIQUE (incident_id, hypothesis_id)       -- target for composite foreign keys
);

CREATE TABLE hypothesis_evidence (
  hypothesis_id   uuid NOT NULL REFERENCES hypotheses,
  evidence_id     uuid NOT NULL REFERENCES evidence,
  relation        evidence_relation NOT NULL,
  note            text,
  PRIMARY KEY (hypothesis_id, evidence_id, relation)
);

CREATE TABLE diagnoses (
  diagnosis_id      uuid PRIMARY KEY,
  incident_id       uuid NOT NULL REFERENCES incidents,
  revision          smallint NOT NULL,      -- 1, 2 ... a new row per revision
  outcome           diagnosis_outcome NOT NULL,
  leading_hypothesis_id uuid,
  confidence        confidence NOT NULL,
  summary           text NOT NULL,
  missing_evidence  text[] NOT NULL DEFAULT '{}',
  stop_reason       text,                   -- for inconclusive results
  reviewed          boolean NOT NULL DEFAULT false,
  review_notes      text,
  produced_by       text NOT NULL,          -- component and model attempt
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (incident_id, revision),
  UNIQUE (incident_id, diagnosis_id),
  -- The leading hypothesis must belong to the same incident.
  FOREIGN KEY (incident_id, leading_hypothesis_id)
    REFERENCES hypotheses (incident_id, hypothesis_id),
  CHECK ((outcome = 'established') = (leading_hypothesis_id IS NOT NULL))
);

-- ---------------------------------------------------------------------------
-- Actions (proposed operations) and patches (their content)
-- ---------------------------------------------------------------------------
CREATE TABLE actions (
  action_id        uuid PRIMARY KEY,
  incident_id      uuid NOT NULL REFERENCES incidents,
  diagnosis_id     uuid NOT NULL,
  kind             action_kind NOT NULL,
  target_service_id uuid NOT NULL REFERENCES services,
  target_environment_id uuid NOT NULL REFERENCES environments,
  description      text NOT NULL,
  expected_effect  text NOT NULL,
  risk             text NOT NULL,
  undo_plan        text NOT NULL,
  recovery_criteria jsonb NOT NULL,         -- recorded before execution (FR-30)
  status           action_status NOT NULL DEFAULT 'proposed',
  version          integer NOT NULL DEFAULT 1,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (incident_id, action_id),
  FOREIGN KEY (incident_id, diagnosis_id) REFERENCES diagnoses (incident_id, diagnosis_id)
);

CREATE TABLE patches (
  patch_id          uuid PRIMARY KEY,
  action_id         uuid NOT NULL UNIQUE REFERENCES actions,  -- one patch per action
  base_revision     text NOT NULL,          -- commit or config version it applies to
  diff_artifact_id  uuid NOT NULL REFERENCES artifacts,
  diff_sha256       char(64) NOT NULL,      -- the fingerprint approvals bind to
  files_changed     smallint NOT NULL,
  lines_added       integer NOT NULL,
  lines_removed     integer NOT NULL,
  review_ref        text,                   -- pull request in the review system
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE test_runs (
  test_run_id      uuid PRIMARY KEY,
  action_id        uuid NOT NULL REFERENCES actions,
  patch_sha256     char(64) NOT NULL,
  reproduced       boolean NOT NULL,
  outcome          test_outcome NOT NULL,
  output_artifact_id uuid REFERENCES artifacts,
  started_at       timestamptz NOT NULL,
  finished_at      timestamptz NOT NULL
);

-- ---------------------------------------------------------------------------
-- Approvals
-- ---------------------------------------------------------------------------
CREATE TABLE approval_requests (
  approval_request_id uuid PRIMARY KEY,
  action_id           uuid NOT NULL REFERENCES actions,
  patch_sha256        char(64) NOT NULL,
  requested_at        timestamptz NOT NULL DEFAULT now(),
  escalate_at         timestamptz NOT NULL,
  expires_at          timestamptz NOT NULL,
  approvers_needed    smallint NOT NULL DEFAULT 1 CHECK (approvers_needed IN (1, 2)),
  closed_at           timestamptz,
  close_reason        text                  -- decided, expired, withdrawn, voided
);
-- At most one open request per action.

-- Each decision is a new, permanent row (FR-24, Q7.21, Q7.23).
CREATE TABLE approval_decisions (
  approval_id         uuid PRIMARY KEY,
  approval_request_id uuid NOT NULL REFERENCES approval_requests,
  action_id           uuid NOT NULL REFERENCES actions,
  decision            approval_decision NOT NULL,
  decided_by          text NOT NULL,        -- identity-system subject
  decided_by_role     text NOT NULL,
  auth_time           timestamptz NOT NULL, -- last sign-in, checked against 15 min
  patch_sha256        char(64) NOT NULL,
  target_service_id   uuid NOT NULL,
  target_environment_id uuid NOT NULL,
  reason              text,
  decided_at          timestamptz NOT NULL DEFAULT now(),
  valid_until         timestamptz,          -- approvals only: execute within 15 min
  signature           bytea,                -- approvals only: signed by the Approval Service
  signing_key_id      text,
  CHECK ((decision = 'approved') = (signature IS NOT NULL AND valid_until IS NOT NULL)),
  CHECK (decision = 'approved' OR reason IS NOT NULL),
  UNIQUE (approval_request_id, decided_by)  -- one decision per person per request
);

-- ---------------------------------------------------------------------------
-- Execution and verification
-- ---------------------------------------------------------------------------
CREATE TABLE execution_attempts (
  attempt_id        uuid PRIMARY KEY,
  action_id         uuid NOT NULL REFERENCES actions,
  approval_id       uuid NOT NULL REFERENCES approval_decisions,
  execution_key     text NOT NULL,          -- same for every attempt of one approval
  attempt_no        smallint NOT NULL CHECK (attempt_no >= 1),
  is_undo           boolean NOT NULL DEFAULT false,
  started_at        timestamptz NOT NULL DEFAULT now(),
  finished_at       timestamptz,
  outcome           execution_outcome NOT NULL DEFAULT 'started',
  external_ref      text,                   -- release process's own reference
  response_artifact_id uuid REFERENCES artifacts,
  error             text,
  UNIQUE (execution_key, attempt_no)
);
-- At most one attempt per execution key may end in 'applied'.

-- An attempt may change once, from 'started' to its final outcome; never again.

CREATE TABLE verifications (
  verification_id   uuid PRIMARY KEY,
  action_id         uuid NOT NULL REFERENCES actions,
  attempt_id        uuid NOT NULL REFERENCES execution_attempts,
  criteria          jsonb NOT NULL,         -- copy of the criteria recorded before execution
  hold_seconds      integer NOT NULL CHECK (hold_seconds > 0),
  window_start      timestamptz NOT NULL,
  window_end        timestamptz NOT NULL,
  measurements      jsonb NOT NULL,         -- observed values per criterion
  result            verification_result NOT NULL,
  verified_by       text NOT NULL,          -- the Recovery Verifier's identity
  accepted_by       text,                   -- the person who accepted recovery
  created_at        timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Model attempts and budget
-- ---------------------------------------------------------------------------
CREATE TABLE incident_budgets (
  incident_id        uuid PRIMARY KEY REFERENCES incidents,
  max_calls          integer NOT NULL,      -- 30 (NFR-13)
  max_input_tokens   integer NOT NULL,      -- 200,000 (NFR-36)
  max_output_tokens  integer NOT NULL,      -- 20,000 (NFR-36)
  max_cost_micros    bigint  NOT NULL,      -- money in millionths of the currency unit
  used_calls         integer NOT NULL DEFAULT 0,
  reserved_cost_micros bigint NOT NULL DEFAULT 0,
  spent_cost_micros  bigint  NOT NULL DEFAULT 0,
  used_input_tokens  integer NOT NULL DEFAULT 0,
  used_output_tokens integer NOT NULL DEFAULT 0,
  CHECK (used_calls <= max_calls),
  CHECK (reserved_cost_micros + spent_cost_micros <= max_cost_micros),
  CHECK (used_input_tokens <= max_input_tokens),
  CHECK (used_output_tokens <= max_output_tokens)
);

CREATE TABLE budget_reservations (
  reservation_id     uuid PRIMARY KEY,
  incident_id        uuid NOT NULL REFERENCES incident_budgets,
  reserved_cost_micros bigint NOT NULL CHECK (reserved_cost_micros > 0),
  reserved_input_tokens integer NOT NULL,
  reserved_output_tokens integer NOT NULL,
  status             reservation_status NOT NULL DEFAULT 'reserved',
  actual_cost_micros bigint,
  created_at         timestamptz NOT NULL DEFAULT now(),
  expires_at         timestamptz NOT NULL,  -- released automatically if never settled
  settled_at         timestamptz
);

CREATE TABLE model_attempts (
  model_attempt_id   uuid PRIMARY KEY,
  incident_id        uuid NOT NULL REFERENCES incidents,
  reservation_id     uuid REFERENCES budget_reservations,
  task               text NOT NULL,         -- e.g. 'diagnose', 'review', 'draft_patch'
  step_attempt_no    smallint NOT NULL CHECK (step_attempt_no BETWEEN 1 AND 3),
  provider           text NOT NULL,
  model              text NOT NULL,
  prompt_template_version text NOT NULL,
  input_sha256       char(64) NOT NULL,
  input_artifact_id  uuid REFERENCES artifacts,
  output_artifact_id uuid REFERENCES artifacts,
  status             model_attempt_status NOT NULL,
  validation_errors  jsonb,
  input_tokens       integer NOT NULL DEFAULT 0,
  output_tokens      integer NOT NULL DEFAULT 0,
  cost_micros        bigint  NOT NULL DEFAULT 0,
  latency_ms         integer,
  started_at         timestamptz NOT NULL,
  finished_at        timestamptz,
  trace_id           text
);

-- ---------------------------------------------------------------------------
-- Timeline events (the public history) and the outbox (delivery)
-- ---------------------------------------------------------------------------
CREATE TABLE timeline_events (
  event_id        uuid PRIMARY KEY,         -- CloudEvents 'id'
  incident_id     uuid NOT NULL REFERENCES incidents,
  seq             bigint NOT NULL CHECK (seq >= 1),  -- per-incident, gapless
  type            text NOT NULL,            -- CloudEvents 'type'
  source          text NOT NULL,            -- CloudEvents 'source'
  actor           text NOT NULL,            -- component or person
  occurred_at     timestamptz NOT NULL,     -- when it happened
  recorded_at     timestamptz NOT NULL DEFAULT now(),
  causation_id    uuid,                     -- the event or request that caused it
  correlation_id  uuid NOT NULL,            -- usually the incident ID
  data            jsonb NOT NULL,
  schema_version  smallint NOT NULL DEFAULT 1,
  prev_hash       char(64),                 -- audit chain (Artifact 5, Q5.20)
  hash            char(64) NOT NULL,
  UNIQUE (incident_id, seq)
);

CREATE TABLE outbox (
  outbox_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  message_id      uuid NOT NULL UNIQUE,     -- stable ID for idempotent delivery
  incident_id     uuid NOT NULL REFERENCES incidents,
  kind            text NOT NULL,            -- 'start_workflow', 'signal_workflow'
  workflow_id     text NOT NULL,            -- stable: 'incident-<incident_id>'
  payload         jsonb NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  available_at    timestamptz NOT NULL DEFAULT now(),
  attempts        integer NOT NULL DEFAULT 0,
  last_error      text,
  delivered_at    timestamptz
);

-- ---------------------------------------------------------------------------
-- Idempotency keys for API writes
-- ---------------------------------------------------------------------------
CREATE TABLE idempotency_keys (
  caller_id       text NOT NULL,            -- authenticated caller
  operation       text NOT NULL,            -- e.g. 'POST /v1/incidents'
  idem_key        text NOT NULL CHECK (length(idem_key) BETWEEN 8 AND 128),
  request_sha256  char(64) NOT NULL,        -- fingerprint of the canonical request body
  state           text NOT NULL CHECK (state IN ('in_progress', 'completed')),
  response_status smallint,
  response_body   jsonb,
  resource_id     uuid,
  created_at      timestamptz NOT NULL DEFAULT now(),
  expires_at      timestamptz NOT NULL,     -- 24 hours
  PRIMARY KEY (caller_id, operation, idem_key)
);
