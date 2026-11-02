-- schema.sql (proposed, 2026-11-02): the canonical PostgreSQL schema (ADR-64).
--
-- Status: proposed, pending team sign-off. Not one of the team's Room 7 tabs.
-- Supersedes schema-tables.sql and schema.mmd, which stay as the team's originals.
-- The application generates UUIDv7 keys (ADR-20); the database doesn't create them.
-- Target: PostgreSQL 16 (Technical specification section 4). Tested on 17: see
-- artifact 7.2 and schema-tests.sql.
--
-- Load as the owning role in an empty database:
--   CREATE ROLE aisre_owner LOGIN CREATEROLE;
--   CREATE DATABASE aisre OWNER aisre_owner;
--   psql -U aisre_owner -d aisre -v ON_ERROR_STOP=1 -f schema.sql
--
-- Contents:
--    1 Types                      8 Retrieval (ADR-62)
--    2 Catalogue                  9 Model attempts and budget
--    3 Incidents and intake      10 Timeline, outbox, idempotency
--    4 Artifacts                 11 Indexes
--    5 Evidence and reasoning    12 Rule functions and triggers
--    6 Actions and tests         13 Retention
--    7 Approvals, execution,     14 Views
--      verification              15 Roles

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Types
-- ---------------------------------------------------------------------------
CREATE DOMAIN sha256_hex AS char(64) CHECK (VALUE ~ '^[0-9a-f]{64}$');

-- ADR-46: exactly the 14 states of layer-08 files/states.py.
CREATE TYPE incident_status AS ENUM (
  'open', 'investigating', 'diagnosed', 'inconclusive', 'testing', 'awaiting_approval',
  'executing', 'reconciling', 'verifying', 'recovered', 'not_recovered', 'handed_over',
  'cancelled', 'closed');
CREATE TYPE action_status AS ENUM (
  'proposed', 'testing', 'test_failed', 'awaiting_approval', 'approved', 'rejected',
  'expired', 'applying', 'outcome_unknown', 'executed', 'not_applied', 'verified',
  'undone', 'withdrawn');

CREATE TYPE severity AS ENUM ('sev1', 'sev2', 'sev3', 'sev4');
CREATE TYPE evidence_kind AS ENUM (
  'alert', 'metric', 'log', 'trace', 'error_group', 'change', 'dependency',
  'past_incident', 'runbook', 'test_result', 'gap');
CREATE TYPE hypothesis_status AS ENUM ('open', 'supported', 'ruled_out', 'confirmed');
CREATE TYPE hypothesis_category AS ENUM (      -- agent_contracts.HypothesisDraft (ADR-64)
  'deployment_code', 'deployment_config', 'dependency', 'capacity', 'data',
  'infrastructure', 'unknown');
CREATE TYPE evidence_relation AS ENUM ('supports', 'contradicts');
CREATE TYPE confidence AS ENUM ('low', 'medium', 'high');
CREATE TYPE diagnosis_outcome AS ENUM ('established', 'inconclusive');
CREATE TYPE action_kind AS ENUM (              -- 'rollback' added (ADR-48, ADR-64)
  'config_revert', 'commit_revert', 'release_rollback', 'rollback');
CREATE TYPE test_outcome AS ENUM (             -- ADR-64
  'passed', 'failed', 'not_reproduced', 'inconclusive', 'crashed', 'timeout');
CREATE TYPE approval_decision AS ENUM ('approved', 'rejected', 'revoked');  -- ADR-64
CREATE TYPE execution_outcome AS ENUM ('started', 'applied', 'not_applied', 'unknown', 'failed');
CREATE TYPE verification_result AS ENUM ('verified', 'failed', 'inconclusive');  -- ADR-64
CREATE TYPE model_task AS ENUM ('hypothesise', 'diagnose', 'critique', 'draft_patch');
CREATE TYPE model_attempt_status AS ENUM (     -- gateway_contracts.ResultStatus
  'ok', 'invalid_output', 'truncated', 'incomplete', 'refused', 'timeout', 'rate_limited',
  'provider_error', 'auth_error', 'bad_request', 'capability_unsupported',
  'budget_refused', 'deadline_passed');
CREATE TYPE model_stop_reason AS ENUM (        -- gateway_contracts.StopReason
  'complete', 'tool_use', 'max_tokens', 'context_exceeded', 'stop_sequence', 'refusal',
  'paused', 'incomplete', 'unknown');
CREATE TYPE reservation_status AS ENUM ('reserved', 'settled', 'released');
CREATE TYPE artifact_kind AS ENUM (
  'evidence_snapshot', 'patch_diff', 'test_output', 'model_input', 'model_output',
  'execution_response', 'report');

-- The transition tables are data, so the database and states.py can be compared row by row.
CREATE TABLE incident_transitions (
  from_status incident_status NOT NULL,
  to_status   incident_status NOT NULL,
  PRIMARY KEY (from_status, to_status)
);
INSERT INTO incident_transitions (from_status, to_status) VALUES
  ('open', 'investigating'), ('open', 'cancelled'),
  ('investigating', 'diagnosed'), ('investigating', 'inconclusive'),
  ('investigating', 'handed_over'), ('investigating', 'cancelled'),
  ('diagnosed', 'testing'), ('diagnosed', 'handed_over'), ('diagnosed', 'cancelled'),
  ('inconclusive', 'handed_over'), ('inconclusive', 'cancelled'),
  ('testing', 'awaiting_approval'), ('testing', 'handed_over'), ('testing', 'cancelled'),
  ('awaiting_approval', 'executing'), ('awaiting_approval', 'handed_over'),
  ('awaiting_approval', 'cancelled'),
  ('executing', 'verifying'), ('executing', 'reconciling'), ('executing', 'handed_over'),
  ('reconciling', 'verifying'), ('reconciling', 'handed_over'),
  ('verifying', 'recovered'), ('verifying', 'not_recovered'), ('verifying', 'handed_over'),
  ('recovered', 'closed'),
  ('not_recovered', 'handed_over'),
  ('handed_over', 'closed'),
  -- ADR-46 additions
  ('handed_over', 'investigating'),   -- a person requests re-investigation
  ('not_recovered', 'reconciling');   -- a rollback's outcome is unknown

CREATE TABLE action_transitions (
  from_status action_status NOT NULL,
  to_status   action_status NOT NULL,
  PRIMARY KEY (from_status, to_status)
);
INSERT INTO action_transitions (from_status, to_status) VALUES
  ('proposed', 'testing'), ('proposed', 'withdrawn'),
  ('testing', 'awaiting_approval'), ('testing', 'test_failed'), ('testing', 'withdrawn'),
  ('awaiting_approval', 'approved'), ('awaiting_approval', 'rejected'),
  ('awaiting_approval', 'expired'), ('awaiting_approval', 'withdrawn'),
  ('approved', 'applying'), ('approved', 'expired'), ('approved', 'withdrawn'),
  ('applying', 'executed'), ('applying', 'not_applied'), ('applying', 'outcome_unknown'),
  ('outcome_unknown', 'executed'), ('outcome_unknown', 'not_applied'),
  ('executed', 'verified'), ('executed', 'undone'),
  -- ADR-46 addition, rollback actions only (enforced in check_action_transition)
  ('proposed', 'awaiting_approval');

-- ---------------------------------------------------------------------------
-- 2. Catalogue
-- ---------------------------------------------------------------------------
CREATE TABLE services (
  service_id     uuid PRIMARY KEY,
  org_id         uuid NOT NULL,
  slug           text NOT NULL,             -- matches the service catalogue
  display_name   text NOT NULL,
  owner_group    text NOT NULL,             -- identity-system group
  critical       boolean NOT NULL DEFAULT false,   -- high-impact rule (ADR-49)
  hold_seconds   integer NOT NULL DEFAULT 600 CHECK (hold_seconds >= 600),  -- ADR-66: 10 min or longer
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
-- 3. Incidents and intake
-- ---------------------------------------------------------------------------
CREATE TABLE incidents (
  incident_id        uuid PRIMARY KEY,
  org_id             uuid NOT NULL,
  display_ref        text NOT NULL UNIQUE,  -- e.g. INC-2026-000123, for people
  service_id         uuid NOT NULL REFERENCES services,
  environment_id     uuid NOT NULL REFERENCES environments,
  dedup_key          text NOT NULL,
  title              text NOT NULL,
  source             text NOT NULL CHECK (source IN ('monitoring', 'engineer')),
  started_at         timestamptz NOT NULL,
  status             incident_status NOT NULL DEFAULT 'open',
  proposed_severity  severity,
  confirmed_severity severity,
  severity_confirmed_by text,
  investigation_run  smallint NOT NULL DEFAULT 1,  -- 2 means workflow incident-<id>-r2 (ADR-46)
  version            integer NOT NULL DEFAULT 1,   -- optimistic concurrency
  last_event_seq     bigint  NOT NULL DEFAULT 0,   -- per-incident event sequence
  created_by         text NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  closed_at          timestamptz,
  CHECK ((status = 'closed') = (closed_at IS NOT NULL)),
  CHECK ((confirmed_severity IS NULL) = (severity_confirmed_by IS NULL))
);

-- Every alert received, including duplicates (FR-03). `fingerprint` is never null:
-- it falls back to service plus alert name (ADR-58).
CREATE TABLE incident_alerts (
  alert_id          uuid PRIMARY KEY,
  incident_id       uuid NOT NULL REFERENCES incidents,
  source_alert_id   text,                   -- monitoring's own identifier
  fingerprint       text NOT NULL,
  received_at       timestamptz NOT NULL DEFAULT now(),
  payload           jsonb NOT NULL,
  UNIQUE (incident_id, source_alert_id, received_at)
);

-- Rejected alerts: stored raw, with no incident (ADR-58).
CREATE TABLE dead_letters (
  dead_letter_id    uuid PRIMARY KEY,
  org_id            uuid NOT NULL,
  received_at       timestamptz NOT NULL DEFAULT now(),
  caller_id         text NOT NULL,
  reason            text NOT NULL,          -- e.g. 'unknown_service', 'forbidden_service'
  detail            text,
  service_slug      text,                   -- as sent, unverified
  environment_slug  text,
  raw_payload       jsonb NOT NULL,
  retain_until      timestamptz NOT NULL
);

-- ---------------------------------------------------------------------------
-- 4. Artifacts (large items live in the object store; this is the reference)
-- ---------------------------------------------------------------------------
CREATE TABLE artifacts (
  artifact_id    uuid PRIMARY KEY,
  org_id         uuid NOT NULL,
  incident_id    uuid NOT NULL REFERENCES incidents,
  kind           artifact_kind NOT NULL,
  storage_key    text NOT NULL UNIQUE,      -- internal only; never returned by the API
  content_sha256 sha256_hex NOT NULL,
  size_bytes     bigint NOT NULL CHECK (size_bytes >= 0),
  media_type     text NOT NULL,
  redacted       boolean NOT NULL,
  retain_until   timestamptz NOT NULL,      -- the shortest period; see artifacts_due_for_deletion
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- Written by the retention job when object-store content is deleted. The artifact row
-- stays, because append-only records point at it, until the whole incident is purged.
CREATE TABLE artifact_deletions (
  artifact_id    uuid PRIMARY KEY REFERENCES artifacts,
  incident_id    uuid NOT NULL REFERENCES incidents,
  deleted_at     timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 5. Evidence and reasoning
-- ---------------------------------------------------------------------------
CREATE TABLE evidence (
  evidence_id     uuid PRIMARY KEY,
  incident_id     uuid NOT NULL REFERENCES incidents,
  kind            evidence_kind NOT NULL,
  source_system   text NOT NULL,            -- which system was read
  tool            text NOT NULL,            -- registry name of the tool (ADR-64)
  collected_by    text NOT NULL,            -- component or person
  query           text,                     -- the query used, if any
  window_start    timestamptz,
  window_end      timestamptz,
  collected_at    timestamptz NOT NULL DEFAULT now(),
  source_as_of    timestamptz,              -- the source's own version time (ADR-64)
  source_link     text,                     -- link to the original
  summary         text NOT NULL,            -- short, redacted extract
  attributes      jsonb NOT NULL DEFAULT '{}'::jsonb,  -- kind-specific fields
  artifact_id     uuid REFERENCES artifacts,           -- snapshot of the raw data
  content_sha256  sha256_hex NOT NULL,
  is_partial      boolean NOT NULL DEFAULT false,
  flagged_injection boolean NOT NULL DEFAULT false,
  gap_reason      text,
  strength_tier   smallint CHECK (strength_tier BETWEEN 1 AND 8),  -- ADR-53, set by code
  content_removed_at timestamptz,           -- a legal or safety deletion (Q10.55); see protect_evidence
  content_removed_reason text,
  CHECK (window_end IS NULL OR window_start IS NULL OR window_end >= window_start),
  CHECK ((kind = 'gap') = (gap_reason IS NOT NULL)),
  CHECK ((content_removed_at IS NULL) = (content_removed_reason IS NULL))
);

CREATE TABLE hypotheses (
  hypothesis_id   uuid PRIMARY KEY,
  incident_id     uuid NOT NULL REFERENCES incidents,
  ordinal         smallint NOT NULL CHECK (ordinal >= 1),  -- H1, H2 ... within the incident
  statement       text NOT NULL,
  category        hypothesis_category NOT NULL,
  claims          jsonb NOT NULL DEFAULT '[]'::jsonb,      -- agent_contracts.Claim list
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
  revision          smallint NOT NULL CHECK (revision >= 1),  -- a new row per revision
  outcome           diagnosis_outcome NOT NULL,
  leading_hypothesis_id uuid,
  category          hypothesis_category NOT NULL,
  confidence        confidence NOT NULL,
  summary           text NOT NULL,
  claims            jsonb NOT NULL DEFAULT '[]'::jsonb,
  ruled_out         text[] NOT NULL DEFAULT '{}',
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
  CHECK ((outcome = 'established') = (leading_hypothesis_id IS NOT NULL)),
  CHECK (outcome = 'inconclusive' OR confidence <> 'low')  -- ADR-53: low is never established
);

-- ---------------------------------------------------------------------------
-- 6. Actions (proposed operations), patches and tests
-- ---------------------------------------------------------------------------
-- An action never changes after creation (ADR-47), apart from status and version.
-- `action_digest` and `test_bundle_digest` are the one exception: the digest covers the
-- passing test bundle, so both are written once, when testing ends. A rollback carries
-- them from creation (ADR-48).
CREATE TABLE actions (
  action_id        uuid PRIMARY KEY,        -- also the execution key (ADR-47)
  incident_id      uuid NOT NULL REFERENCES incidents,
  diagnosis_id     uuid NOT NULL,
  parent_action_id uuid,                    -- rollback only (ADR-48)
  kind             action_kind NOT NULL,
  target_service_id uuid NOT NULL REFERENCES services,
  target_environment_id uuid NOT NULL REFERENCES environments,
  target_deployment text NOT NULL,          -- release-process name, e.g. checkout-prod-eu1
  base_revision    text NOT NULL,           -- what the target must run (Q11.56)
  expected_after_revision text NOT NULL,   -- what the target runs afterwards (Layer 11 C17)
  description      text NOT NULL,
  expected_effect  text NOT NULL,
  risk             text NOT NULL,
  undo_plan        text NOT NULL,
  recovery_criteria jsonb NOT NULL,         -- predicates and baseline, set before execution (FR-30, ADR-66)
  recovery_predicates_digest sha256_hex NOT NULL,
  hold_seconds     integer NOT NULL DEFAULT 600 CHECK (hold_seconds >= 60),  -- ADR-64, ADR-66
  high_impact      boolean NOT NULL,        -- ADR-49
  policy_version   text NOT NULL,           -- policies in force at creation (Q11.57)
  test_bundle_digest sha256_hex,            -- write-once; copies the parent's for a rollback
  action_digest    sha256_hex,              -- write-once; what an approval names (ADR-49, ADR-64)
  status           action_status NOT NULL DEFAULT 'proposed',
  version          integer NOT NULL DEFAULT 1,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (incident_id, action_id),
  FOREIGN KEY (incident_id, diagnosis_id) REFERENCES diagnoses (incident_id, diagnosis_id),
  FOREIGN KEY (incident_id, parent_action_id) REFERENCES actions (incident_id, action_id),
  CHECK ((kind = 'rollback') = (parent_action_id IS NOT NULL)),
  CHECK (status IN ('proposed', 'testing', 'test_failed', 'withdrawn')
         OR (action_digest IS NOT NULL AND test_bundle_digest IS NOT NULL))
);

CREATE TABLE patches (
  patch_id          uuid PRIMARY KEY,
  action_id         uuid NOT NULL UNIQUE REFERENCES actions,  -- one patch per action
  base_revision     text NOT NULL,          -- commit or config version it applies to
  diff_artifact_id  uuid NOT NULL REFERENCES artifacts,
  patch_sha256      sha256_hex NOT NULL,    -- renamed from diff_sha256 (ADR-64)
  files_changed     smallint NOT NULL CHECK (files_changed >= 0),
  lines_added       integer NOT NULL CHECK (lines_added >= 0),
  lines_removed     integer NOT NULL CHECK (lines_removed >= 0),
  review_ref        text,                   -- pull request in the review system
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE test_runs (
  test_run_id      uuid PRIMARY KEY,
  action_id        uuid NOT NULL REFERENCES actions,
  patch_sha256     sha256_hex NOT NULL,
  reproduced       boolean NOT NULL,
  outcome          test_outcome NOT NULL,
  output_artifact_id uuid REFERENCES artifacts,
  started_at       timestamptz NOT NULL,
  finished_at      timestamptz NOT NULL,
  CHECK (finished_at >= started_at)
);

-- ---------------------------------------------------------------------------
-- 7. Approvals, execution and verification
-- ---------------------------------------------------------------------------
CREATE TABLE approval_requests (
  approval_request_id uuid PRIMARY KEY,
  action_id           uuid NOT NULL REFERENCES actions,
  requested_by        text NOT NULL,        -- nobody approves their own request (ADR-49)
  patch_sha256        sha256_hex,           -- for reading; null if the action has no patch
  requested_at        timestamptz NOT NULL DEFAULT now(),
  escalate_at         timestamptz NOT NULL, -- 15 minutes
  expires_at          timestamptz NOT NULL, -- 60 minutes
  approvers_needed    smallint NOT NULL DEFAULT 1 CHECK (approvers_needed IN (1, 2)),
  closed_at           timestamptz,
  close_reason        text CHECK (close_reason IN ('decided', 'expired', 'withdrawn', 'voided')),
  UNIQUE (approval_request_id, action_id),
  CHECK (escalate_at <= expires_at),
  CHECK ((closed_at IS NULL) = (close_reason IS NULL))
);

-- Each decision is a new, permanent row (FR-24, Q7.21, Q7.23).
CREATE TABLE approval_decisions (
  approval_id         uuid PRIMARY KEY,
  approval_request_id uuid NOT NULL,
  action_id           uuid NOT NULL,
  decision            approval_decision NOT NULL,
  decided_by          text NOT NULL,        -- identity-system subject
  decided_by_role     text NOT NULL,
  auth_time           timestamptz NOT NULL, -- last sign-in
  mfa                 boolean NOT NULL,
  policy_version      text NOT NULL,
  action_digest       sha256_hex NOT NULL,  -- what the decision names (ADR-49, ADR-64)
  patch_sha256        sha256_hex,           -- for reading
  target_service_id   uuid NOT NULL,
  target_environment_id uuid NOT NULL,
  reason              text,
  decided_at          timestamptz NOT NULL DEFAULT now(),
  valid_until         timestamptz,          -- approvals only
  signature           bytea,                -- signed by the Approval Service
  signing_key_id      text,
  revokes_approval_id uuid REFERENCES approval_decisions,        -- revocations only
  conditional         boolean NOT NULL DEFAULT false,            -- rollback approvals (ADR-48)
  conditional_on_approval_id uuid REFERENCES approval_decisions, -- the fix's approval
  UNIQUE (approval_id, action_id),
  UNIQUE (approval_request_id, decided_by, decision),  -- once each per person per request
  FOREIGN KEY (approval_request_id, action_id)
    REFERENCES approval_requests (approval_request_id, action_id),
  CHECK ((decision = 'approved') = (valid_until IS NOT NULL)),
  CHECK (decision <> 'approved' OR (signature IS NOT NULL AND signing_key_id IS NOT NULL)),
  CHECK (decision = 'approved' OR reason IS NOT NULL),
  CHECK ((decision = 'revoked') = (revokes_approval_id IS NOT NULL)),
  CHECK (conditional = (conditional_on_approval_id IS NOT NULL)),
  CHECK (NOT conditional OR decision = 'approved'),
  -- A recent sign-in with two-factor applies to approvals, not to rejections (ADR-49).
  CHECK (decision <> 'approved' OR (mfa AND decided_at - auth_time <= interval '15 minutes')),
  CHECK (decision <> 'approved' OR conditional
         OR valid_until <= decided_at + interval '15 minutes'),
  CHECK (decision <> 'approved' OR decided_by NOT LIKE 'system:%')  -- the platform never approves
);

CREATE TABLE execution_attempts (
  attempt_id        uuid PRIMARY KEY,
  action_id         uuid NOT NULL,
  approval_id       uuid NOT NULL,          -- the deciding approval, for audit (ADR-47)
  action_digest     sha256_hex NOT NULL,
  execution_key     text NOT NULL,          -- equals action_id on every attempt (ADR-47)
  attempt_no        smallint NOT NULL CHECK (attempt_no >= 1),
  reconciliation    boolean NOT NULL DEFAULT false,  -- a looked-up result, nothing was sent
  started_at        timestamptz NOT NULL DEFAULT now(),
  finished_at       timestamptz,
  outcome           execution_outcome NOT NULL DEFAULT 'started',
  external_ref      text,                   -- release process's own reference
  response_artifact_id uuid REFERENCES artifacts,
  error             text,
  -- A person's outcome resolution (11.11, ADR-47): `outcome` is what they recorded. The row
  -- stays with the attempts, so it is as frozen as they are.
  resolved_by       text,                   -- the Incident lead; POST .../outcome-resolutions
  observed_revision text,
  resolution_reason text,
  UNIQUE (execution_key, attempt_no),
  UNIQUE (attempt_id, action_id),
  FOREIGN KEY (action_id) REFERENCES actions,
  FOREIGN KEY (approval_id, action_id) REFERENCES approval_decisions (approval_id, action_id),
  CHECK (execution_key = action_id::text),
  CHECK ((outcome = 'started') = (finished_at IS NULL)),
  CHECK (NOT reconciliation OR outcome IN ('applied', 'not_applied')),
  CHECK (resolved_by IS NULL OR reconciliation),
  CHECK ((resolved_by IS NULL) = (observed_revision IS NULL)),
  CHECK ((resolved_by IS NULL) = (resolution_reason IS NULL)),
  CHECK (resolved_by NOT LIKE 'system:%')   -- a person, never the platform
);

CREATE TABLE verifications (
  verification_id   uuid PRIMARY KEY,
  action_id         uuid NOT NULL,
  attempt_id        uuid NOT NULL,
  criteria          jsonb NOT NULL,         -- copy of the criteria recorded before execution
  hold_seconds      integer NOT NULL CHECK (hold_seconds >= 60),
  window_start      timestamptz NOT NULL,
  window_end        timestamptz NOT NULL,
  measurements      jsonb NOT NULL,         -- observed values per criterion
  result            verification_result NOT NULL,
  verified_by       text NOT NULL,          -- the Recovery Verifier's identity
  accepted_by       text,                   -- the incident lead (ADR-66); set once
  accepted_at       timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (attempt_id, action_id) REFERENCES execution_attempts (attempt_id, action_id),
  CHECK (window_end >= window_start),
  CHECK ((accepted_by IS NULL) = (accepted_at IS NULL)),
  CHECK (accepted_by IS NULL OR result = 'verified')
);

-- ---------------------------------------------------------------------------
-- 8. Retrieval (ADR-62): PostgreSQL stores the corpus, the worker ranks it
-- ---------------------------------------------------------------------------
CREATE TABLE documents (
  document_id     uuid PRIMARY KEY,
  org_id          uuid NOT NULL,
  doc_ref         text NOT NULL,            -- retrieval.Document.doc_id
  doc_version     text NOT NULL,            -- source version, e.g. commit or wiki revision
  doc_type        text NOT NULL CHECK (doc_type IN ('runbook', 'past_incident')),  -- retrieval.Document
  title           text NOT NULL,
  body            text NOT NULL,
  services        text[] NOT NULL,          -- '*' means all
  allowed_groups  text[] NOT NULL,
  reviewed_at     timestamptz NOT NULL,
  owner           text NOT NULL,
  deprecated      boolean NOT NULL DEFAULT false,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, doc_ref, doc_version)
);

CREATE TABLE chunks (
  chunk_id        uuid PRIMARY KEY,
  document_id     uuid NOT NULL REFERENCES documents,
  ordinal         integer NOT NULL CHECK (ordinal >= 0),
  heading_path    text[] NOT NULL,
  body            text NOT NULL,
  span_start      integer NOT NULL CHECK (span_start >= 0),  -- character span in the source
  span_end        integer NOT NULL,
  content_sha256  sha256_hex NOT NULL,
  UNIQUE (document_id, ordinal),
  CHECK (span_end >= span_start)
);

CREATE TABLE index_revisions (
  index_revision_id uuid PRIMARY KEY,
  org_id          uuid NOT NULL,
  revision_hash   sha256_hex NOT NULL,      -- document versions, permitted groups, build date
  built_at        timestamptz NOT NULL,
  UNIQUE (org_id, revision_hash)
);

CREATE TABLE index_revision_documents (
  index_revision_id uuid NOT NULL REFERENCES index_revisions,
  document_id     uuid NOT NULL REFERENCES documents,
  stale           boolean NOT NULL,         -- as judged at build time, so a reload gets the same set (10.9)
  PRIMARY KEY (index_revision_id, document_id)
);

-- ---------------------------------------------------------------------------
-- 9. Model attempts and budget
-- ---------------------------------------------------------------------------
-- Limits and rates come from one configuration object (ADR-52, ADR-56).
-- used_calls and used_* tokens include what is held by open reservations.
CREATE TABLE incident_budgets (
  incident_id        uuid PRIMARY KEY REFERENCES incidents,
  max_calls          integer NOT NULL CHECK (max_calls >= 0),         -- 30 (NFR-13)
  max_input_tokens   integer NOT NULL CHECK (max_input_tokens >= 0),  -- 200,000 (NFR-36)
  max_output_tokens  integer NOT NULL CHECK (max_output_tokens >= 0), -- 20,000 (NFR-36)
  max_cost_micros    bigint  NOT NULL CHECK (max_cost_micros >= 0),   -- $2.00 = 2,000,000 (ADR-56)
  used_calls         integer NOT NULL DEFAULT 0 CHECK (used_calls >= 0),
  reserved_cost_micros bigint NOT NULL DEFAULT 0 CHECK (reserved_cost_micros >= 0),
  spent_cost_micros  bigint  NOT NULL DEFAULT 0 CHECK (spent_cost_micros >= 0),
  used_input_tokens  integer NOT NULL DEFAULT 0 CHECK (used_input_tokens >= 0),
  used_output_tokens integer NOT NULL DEFAULT 0 CHECK (used_output_tokens >= 0),
  CHECK (used_calls <= max_calls),
  CHECK (reserved_cost_micros + spent_cost_micros <= max_cost_micros),
  CHECK (used_input_tokens <= max_input_tokens),
  CHECK (used_output_tokens <= max_output_tokens)
);

-- Inserting a reservation holds the budget; settling or releasing moves it (triggers below).
-- Sent with unknown usage: settled at the reserved maximum. Never sent: released (ADR-61).
-- `sent_at` is what tells the two apart at expiry, so it is written before the call leaves.
CREATE TABLE budget_reservations (
  reservation_id     uuid PRIMARY KEY,
  incident_id        uuid NOT NULL REFERENCES incident_budgets,
  reserved_cost_micros bigint NOT NULL CHECK (reserved_cost_micros > 0),
  reserved_input_tokens integer NOT NULL CHECK (reserved_input_tokens >= 0),
  reserved_output_tokens integer NOT NULL CHECK (reserved_output_tokens >= 0),
  status             reservation_status NOT NULL DEFAULT 'reserved',
  actual_cost_micros bigint CHECK (actual_cost_micros >= 0),
  actual_input_tokens integer CHECK (actual_input_tokens >= 0),   -- null keeps the held estimate
  actual_output_tokens integer CHECK (actual_output_tokens >= 0),
  created_at         timestamptz NOT NULL DEFAULT now(),
  expires_at         timestamptz NOT NULL,  -- released automatically if never sent
  sent_at            timestamptz,           -- write-once
  settled_at         timestamptz,
  UNIQUE (reservation_id, incident_id),
  CHECK ((status = 'settled') = (actual_cost_micros IS NOT NULL)),
  CHECK ((status = 'reserved') = (settled_at IS NULL)),
  CHECK (status <> 'settled' OR sent_at IS NOT NULL),
  CHECK (status <> 'released' OR sent_at IS NULL)
);

-- A settled reservation never changes. The reconciliation job (Q10.35) records the provider's
-- figures here instead, at most once and only for usage that was unknown when it settled, and
-- a trigger moves the difference in the budget row. Corrected usage overrides the reservation's.
CREATE TABLE reservation_corrections (
  reservation_id     uuid PRIMARY KEY,
  incident_id        uuid NOT NULL,
  actual_cost_micros bigint NOT NULL CHECK (actual_cost_micros >= 0),
  actual_input_tokens integer NOT NULL CHECK (actual_input_tokens >= 0),
  actual_output_tokens integer NOT NULL CHECK (actual_output_tokens >= 0),
  corrected_by       text NOT NULL,
  reason             text NOT NULL,
  corrected_at       timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (reservation_id, incident_id) REFERENCES budget_reservations (reservation_id, incident_id)
);

CREATE TABLE model_attempts (
  model_attempt_id   uuid PRIMARY KEY,
  incident_id        uuid NOT NULL REFERENCES incidents,
  reservation_id     uuid REFERENCES budget_reservations,  -- null if refused before reserving
  task               model_task NOT NULL,
  step_attempt_no    smallint NOT NULL CHECK (step_attempt_no BETWEEN 1 AND 3),
  provider           text NOT NULL,
  model              text NOT NULL,
  checkpoint         text,                  -- Tinker sampler path
  adapter_version    text NOT NULL,
  prompt_template_version text NOT NULL,
  prompt_sha256      sha256_hex NOT NULL,   -- text fingerprint (ADR-63)
  output_schema_sha256 sha256_hex,
  index_revision_id  uuid REFERENCES index_revisions,  -- the corpus searched, if any
  input_sha256       sha256_hex NOT NULL,   -- reuse key within an incident (ADR-61)
  input_artifact_id  uuid REFERENCES artifacts,
  output_artifact_id uuid REFERENCES artifacts,
  status             model_attempt_status NOT NULL,
  stop_reason        model_stop_reason NOT NULL DEFAULT 'unknown',
  validation_errors  jsonb,
  provider_request_id text,
  input_tokens       integer NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
  output_tokens      integer NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
  cache_read_input_tokens integer NOT NULL DEFAULT 0 CHECK (cache_read_input_tokens >= 0),
  cache_write_input_tokens integer NOT NULL DEFAULT 0 CHECK (cache_write_input_tokens >= 0),
  usage_reported     boolean NOT NULL DEFAULT true,   -- false means estimated (ADR-61)
  cost_micros        bigint  NOT NULL DEFAULT 0 CHECK (cost_micros >= 0),
  latency_ms         integer CHECK (latency_ms >= 0),
  started_at         timestamptz NOT NULL,
  finished_at        timestamptz,
  trace_id           text
);

-- ---------------------------------------------------------------------------
-- 10. Timeline events (the public history), outbox and idempotency
-- ---------------------------------------------------------------------------
-- `seq`, `prev_hash`, `hash` and `recorded_at` are assigned by the database (section 12).
-- CloudEvents `orgid` and `subject` are derived: see the timeline_cloudevents view.
CREATE TABLE timeline_events (
  event_id        uuid PRIMARY KEY,         -- CloudEvents 'id'
  incident_id     uuid NOT NULL REFERENCES incidents,
  seq             bigint NOT NULL CHECK (seq >= 1),  -- 'sequence'; per-incident, gapless
  type            text NOT NULL CHECK (type ~ '^aisre\.[a-z_]+\.[a-z_]+\.v[0-9]+$'),
  source          text NOT NULL,            -- CloudEvents 'source'
  actor           text NOT NULL,            -- component or person
  occurred_at     timestamptz NOT NULL,     -- CloudEvents 'time'
  recorded_at     timestamptz NOT NULL DEFAULT now(),
  causation_id    uuid,                     -- the event or request that caused it
  correlation_id  uuid NOT NULL,            -- usually the incident ID
  traceparent     text,                     -- ADR-64
  dataschema      text,                     -- ADR-64
  data            jsonb NOT NULL,
  schema_version  smallint NOT NULL DEFAULT 1,
  prev_hash       sha256_hex,               -- audit chain (Artifact 5, Q5.20); null at seq 1
  hash            sha256_hex NOT NULL,
  UNIQUE (incident_id, seq),
  CHECK ((seq = 1) = (prev_hash IS NULL))
);

CREATE TABLE outbox (
  outbox_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  message_id      uuid NOT NULL UNIQUE,     -- stable ID for idempotent delivery
  incident_id     uuid NOT NULL REFERENCES incidents,
  kind            text NOT NULL CHECK (kind IN ('start_workflow', 'signal_workflow')),
  workflow_id     text NOT NULL,            -- 'incident-<id>', or 'incident-<id>-r2' (ADR-46)
  payload         jsonb NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  available_at    timestamptz NOT NULL DEFAULT now(),
  attempts        integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  last_error      text,
  delivered_at    timestamptz,
  CHECK (workflow_id LIKE 'incident-' || incident_id::text || '%')
);

CREATE TABLE idempotency_keys (
  caller_id       text NOT NULL,            -- authenticated caller
  operation       text NOT NULL,            -- e.g. 'POST /v1/incidents'
  idem_key        text NOT NULL CHECK (length(idem_key) BETWEEN 8 AND 128),
  request_sha256  sha256_hex NOT NULL,      -- fingerprint of the canonical request body
  state           text NOT NULL CHECK (state IN ('in_progress', 'completed')),
  response_status smallint,
  response_body   jsonb,
  resource_id     uuid,
  created_at      timestamptz NOT NULL DEFAULT now(),
  expires_at      timestamptz NOT NULL,     -- 24 hours
  PRIMARY KEY (caller_id, operation, idem_key),
  CHECK (state <> 'completed' OR response_status IS NOT NULL)
);

-- Investigation slots (Artifact 8, 8.1): at most 5 granted at once, the rest wait by severity.
-- A row leaves when the incident leaves `open` or `investigating`.
CREATE TABLE investigation_queue (
  incident_id  uuid PRIMARY KEY REFERENCES incidents ON DELETE CASCADE,
  severity     severity,                    -- NULL until proposed; sorts last
  queued_at    timestamptz NOT NULL DEFAULT now(),
  granted_at   timestamptz
);

-- Write fence (Artifact 12, 12.5). One row holds the epoch, which only ever rises.
-- Reasons are kept once cleared, for audit.
CREATE TABLE fence_state (
  id         smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  epoch      bigint NOT NULL DEFAULT 1 CHECK (epoch >= 1),
  updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO fence_state DEFAULT VALUES;

CREATE TABLE fence_reasons (
  fence_id    uuid PRIMARY KEY,
  scope       text NOT NULL,                -- 'global', or a deployment name
  reason      text NOT NULL,                -- a FenceReason value
  why         text,                         -- required for a manual fence (ADR-50)
  set_by      text NOT NULL,
  set_at      timestamptz NOT NULL DEFAULT now(),
  cleared_by  text,
  cleared_at  timestamptz,
  CHECK ((cleared_by IS NULL) = (cleared_at IS NULL)),
  CHECK (reason <> 'manual' OR COALESCE(why, '') <> '')
);
CREATE UNIQUE INDEX fence_reasons_one_active ON fence_reasons (scope, reason)
  WHERE cleared_at IS NULL;

CREATE FUNCTION fence_epoch_only_rises() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.epoch < OLD.epoch THEN
    RAISE EXCEPTION 'the fence epoch never goes down' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER fence_epoch_only_rises BEFORE UPDATE ON fence_state
  FOR EACH ROW EXECUTE FUNCTION fence_epoch_only_rises();

-- ---------------------------------------------------------------------------
-- 11. Indexes
-- ---------------------------------------------------------------------------
-- Partial unique indexes: the final guards.
-- One open incident per deduplication key (FR-03, Q7.20). Open: not closed or cancelled.
CREATE UNIQUE INDEX uq_incidents_open_dedup ON incidents (org_id, dedup_key)
  WHERE status NOT IN ('closed', 'cancelled');
-- At most one open request per action.
CREATE UNIQUE INDEX uq_approval_requests_open ON approval_requests (action_id)
  WHERE closed_at IS NULL;
-- At most one attempt per execution key may end in 'applied' (ADR-47).
CREATE UNIQUE INDEX uq_execution_applied ON execution_attempts (execution_key)
  WHERE outcome = 'applied';
-- At most one attempt per execution key may be in flight.
CREATE UNIQUE INDEX uq_execution_open ON execution_attempts (execution_key)
  WHERE outcome = 'started';

-- Q7.22: an incident's records load quickly, by kind and by time.
CREATE INDEX ix_evidence_incident_kind ON evidence (incident_id, kind);
CREATE INDEX ix_evidence_incident_time ON evidence (incident_id, collected_at);
CREATE INDEX ix_hypothesis_evidence_evidence ON hypothesis_evidence (evidence_id);
CREATE INDEX ix_actions_incident ON actions (incident_id, created_at);
CREATE INDEX ix_actions_parent ON actions (parent_action_id) WHERE parent_action_id IS NOT NULL;
CREATE INDEX ix_actions_deployment_active ON actions (target_deployment)
  WHERE status IN ('applying', 'outcome_unknown');   -- the ADR-47 block looks here
CREATE INDEX ix_test_runs_action ON test_runs (action_id, started_at);
CREATE INDEX ix_attempts_action ON execution_attempts (action_id, attempt_no);
CREATE INDEX ix_verifications_action ON verifications (action_id, created_at);
CREATE INDEX ix_alerts_incident ON incident_alerts (incident_id, received_at);
CREATE INDEX ix_artifacts_incident ON artifacts (incident_id);
CREATE INDEX ix_incidents_status ON incidents (status, created_at);
CREATE INDEX ix_incidents_service ON incidents (service_id, created_at);

-- Q7.23: approval history by action, approver, incident (through actions) or time.
CREATE INDEX ix_decisions_action_time ON approval_decisions (action_id, decided_at);
CREATE INDEX ix_decisions_person_time ON approval_decisions (decided_by, decided_at);
CREATE INDEX ix_decisions_time ON approval_decisions (decided_at);
CREATE INDEX ix_requests_action ON approval_requests (action_id, requested_at);

-- Q7.26: JSONB is indexed where it's searched.
CREATE INDEX gin_evidence_attributes ON evidence USING gin (attributes jsonb_path_ops);

-- Support.
CREATE INDEX ix_timeline_recorded ON timeline_events (recorded_at);  -- cross-incident views (Q7.48)
CREATE INDEX ix_timeline_incident_type ON timeline_events (incident_id, type);
CREATE INDEX ix_model_attempts_incident ON model_attempts (incident_id, started_at);
CREATE INDEX ix_model_attempts_reuse ON model_attempts (incident_id, input_sha256);  -- ADR-61
CREATE INDEX ix_reservations_open ON budget_reservations (expires_at) WHERE status = 'reserved';
CREATE INDEX ix_outbox_pending ON outbox (available_at, outbox_id) WHERE delivered_at IS NULL;
CREATE INDEX ix_outbox_delivered ON outbox (delivered_at) WHERE delivered_at IS NOT NULL;
CREATE INDEX ix_idempotency_expiry ON idempotency_keys (expires_at);
CREATE INDEX ix_artifacts_retention ON artifacts (retain_until);
CREATE INDEX ix_dead_letters_time ON dead_letters (received_at);
CREATE INDEX ix_dead_letters_retention ON dead_letters (retain_until);
CREATE INDEX ix_chunks_document ON chunks (document_id, ordinal);

-- ---------------------------------------------------------------------------
-- 12. Rule functions and triggers
-- ---------------------------------------------------------------------------
-- Errors: illegal changes raise restrict_violation (23001) or check_violation (23514).
-- Row triggers don't fire on TRUNCATE, so protected tables also block it.
-- Trigger names set the firing order.

-- Retention: true when an incident's whole record may be purged. The one-year value is
-- provisional (ADR-57).
CREATE FUNCTION incident_retention_due(p_incident_id uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT i.status IN ('closed', 'cancelled')
     AND COALESCE(i.closed_at, i.updated_at) < now() - interval '1 year'
     AND NOT EXISTS (SELECT 1 FROM artifacts a
                     WHERE a.incident_id = i.incident_id AND a.retain_until >= now())
  FROM incidents i WHERE i.incident_id = p_incident_id
$$;

-- Rejects UPDATE and DELETE for everyone, the owner and superusers included. The one exception
-- is DELETE by aisre_retention on an expired record. TG_ARGV[0] says how to tell:
-- 'incident_id', 'action_id' or 'retain_until'; no argument means never.
CREATE FUNCTION protect_row() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  how text := TG_ARGV[0];
  v_id uuid;
  v_due boolean := false;
BEGIN
  IF TG_OP = 'DELETE' AND current_user = 'aisre_retention' AND how IS NOT NULL THEN
    IF how = 'retain_until' THEN
      v_due := (to_jsonb(OLD) ->> 'retain_until')::timestamptz < now();
    ELSE
      v_id := (to_jsonb(OLD) ->> how)::uuid;
      IF how = 'action_id' THEN
        SELECT incident_id INTO v_id FROM actions WHERE action_id = v_id;
      END IF;
      v_due := incident_retention_due(v_id);
    END IF;
    IF v_due THEN
      RETURN OLD;
    END IF;
    RAISE EXCEPTION '%: record is not yet due for deletion', TG_TABLE_NAME
      USING ERRCODE = 'restrict_violation';
  END IF;
  RAISE EXCEPTION '% is append-only: % rejected', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'restrict_violation';
END $$;

-- Evidence is append-only, with one exception: a legal or safety deletion of a retrieved
-- document (Q10.55) sets content_removed_at and its reason, once. The extract goes and only the
-- document reference stays, whatever the caller sent; t20_freeze holds the other columns.
CREATE FUNCTION protect_evidence() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.content_removed_at IS NOT NULL OR NEW.content_removed_at IS NULL
     OR NOT OLD.attributes ? 'doc_id' THEN
    RAISE EXCEPTION '% is append-only: % rejected', TG_TABLE_NAME, TG_OP
      USING ERRCODE = 'restrict_violation';
  END IF;
  NEW.summary := 'content removed';
  NEW.attributes := jsonb_build_object('doc_id', OLD.attributes -> 'doc_id');
  RETURN NEW;
END $$;

CREATE FUNCTION protect_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only: TRUNCATE rejected', TG_TABLE_NAME
    USING ERRCODE = 'restrict_violation';
END $$;

-- Permits changes only to the columns named in TG_ARGV. A name ending in '?' is
-- write-once: it may go from null to a value, then never changes.
CREATE FUNCTION freeze_except() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  o jsonb := to_jsonb(OLD);
  n jsonb := to_jsonb(NEW);
  arg text;
  col text;
BEGIN
  FOREACH arg IN ARRAY TG_ARGV LOOP
    col := rtrim(arg, '?');
    IF right(arg, 1) = '?' AND o -> col <> 'null'::jsonb AND o -> col IS DISTINCT FROM n -> col THEN
      RAISE EXCEPTION '%.% is write-once', TG_TABLE_NAME, col USING ERRCODE = 'restrict_violation';
    END IF;
    o := o - col;
    n := n - col;
  END LOOP;
  IF o IS DISTINCT FROM n THEN
    RAISE EXCEPTION '% rows are frozen except for: %', TG_TABLE_NAME, array_to_string(TG_ARGV, ', ')
      USING ERRCODE = 'restrict_violation';
  END IF;
  RETURN NEW;
END $$;

-- Any change outside the columns named in TG_ARGV must raise version by exactly 1; a change
-- only to those columns must leave it alone.
CREATE FUNCTION check_version() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  o jsonb := to_jsonb(OLD) - 'version';
  n jsonb := to_jsonb(NEW) - 'version';
BEGIN
  FOR i IN 0 .. TG_NARGS - 1 LOOP
    o := o - TG_ARGV[i];
    n := n - TG_ARGV[i];
  END LOOP;
  IF NEW.version <> OLD.version + (CASE WHEN o IS DISTINCT FROM n THEN 1 ELSE 0 END) THEN
    RAISE EXCEPTION '%: version must rise by 1 on each change', TG_TABLE_NAME
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION check_incident_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'open' THEN
      RAISE EXCEPTION 'an incident starts as open, not %', NEW.status USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.status <> OLD.status THEN
    IF NOT EXISTS (SELECT 1 FROM incident_transitions t
                   WHERE t.from_status = OLD.status AND t.to_status = NEW.status) THEN
      RAISE EXCEPTION 'illegal incident transition: % -> %', OLD.status, NEW.status
        USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status = 'handed_over' AND NEW.status = 'investigating' THEN
      NEW.investigation_run := OLD.investigation_run + 1;  -- workflow incident-<id>-r2 (ADR-46)
    END IF;
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END $$;

CREATE FUNCTION check_action_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  p actions%ROWTYPE;
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'proposed' THEN
      RAISE EXCEPTION 'an action starts as proposed, not %', NEW.status USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.kind = 'rollback' THEN                       -- ADR-48
      SELECT * INTO p FROM actions WHERE action_id = NEW.parent_action_id;
      IF NEW.action_digest IS NULL OR NEW.test_bundle_digest IS DISTINCT FROM p.test_bundle_digest
         OR NEW.recovery_predicates_digest <> p.recovery_predicates_digest THEN
        RAISE EXCEPTION 'a rollback has a digest and copies its parent''s test and predicate digests'
          USING ERRCODE = 'check_violation';
      END IF;
      IF NEW.base_revision <> p.expected_after_revision THEN
        RAISE EXCEPTION 'a rollback starts from the revision its parent produced'
          USING ERRCODE = 'check_violation';
      END IF;
    END IF;
    RETURN NEW;
  END IF;
  -- The column grant gives the dispatcher status and version on any row, so this is what
  -- limits it to `applying` and its outcomes (ADR-51). Runs as the caller, like
  -- check_timeline_writer.
  IF current_user = 'aisre_dispatcher' AND (OLD.status, NEW.status) NOT IN (
       ('approved'::action_status, 'applying'::action_status),
       ('applying', 'executed'), ('applying', 'not_applied'), ('applying', 'outcome_unknown'),
       ('outcome_unknown', 'executed'), ('outcome_unknown', 'not_applied')) THEN
    RAISE EXCEPTION 'permission denied: the dispatcher sets only applying and its outcomes, not % -> %',
      OLD.status, NEW.status USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF NEW.status <> OLD.status THEN
    IF NOT EXISTS (SELECT 1 FROM action_transitions t
                   WHERE t.from_status = OLD.status AND t.to_status = NEW.status) THEN
      RAISE EXCEPTION 'illegal action transition: % -> %', OLD.status, NEW.status
        USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status = 'proposed' AND NEW.status = 'awaiting_approval' AND NEW.kind <> 'rollback' THEN
      RAISE EXCEPTION 'only rollback actions skip testing (ADR-48)' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  -- The two write-once digests are set when testing ends, not before and not after.
  IF OLD.action_digest IS NULL AND NEW.action_digest IS NOT NULL
     AND NOT (OLD.status = 'testing' AND NEW.status = 'awaiting_approval') THEN
    RAISE EXCEPTION 'action_digest is set when testing ends' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

-- ADR-49, derived: the stored flag can add to the rule and never lower it. action.is_high_impact
-- is the same rule. Severity not yet confirmed counts as high-impact.
CREATE FUNCTION action_is_high_impact(a actions) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT a.high_impact OR a.kind = 'release_rollback'
    OR EXISTS (SELECT 1 FROM actions p WHERE p.action_id = a.parent_action_id AND p.kind = 'release_rollback')
    OR EXISTS (SELECT 1 FROM incidents i WHERE i.incident_id = a.incident_id
               AND (i.confirmed_severity IS NULL OR i.confirmed_severity = 'sev1'))
    OR EXISTS (SELECT 1 FROM services s WHERE s.service_id = a.target_service_id AND s.critical)
    OR EXISTS (SELECT 1 FROM patches mine JOIN patches other ON other.patch_sha256 = mine.patch_sha256
               JOIN actions o ON o.action_id = other.action_id
               WHERE mine.action_id = a.action_id AND o.incident_id = a.incident_id
                 AND o.target_deployment <> a.target_deployment)
$$;

-- Approval requests.
CREATE FUNCTION check_approval_request() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  a actions%ROWTYPE;
BEGIN
  SELECT * INTO a FROM actions WHERE action_id = NEW.action_id;
  IF action_is_high_impact(a) AND NEW.approvers_needed <> 2 THEN
    RAISE EXCEPTION 'a high-impact action needs two approvers (ADR-49)' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

-- Approval decisions: everything a row must agree with.
CREATE FUNCTION check_approval_decision() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  a actions%ROWTYPE;
  r approval_requests%ROWTYPE;
  v_patch sha256_hex;
  v_prior approval_decisions%ROWTYPE;
  v_parent actions%ROWTYPE;
BEGIN
  SELECT * INTO a FROM actions WHERE action_id = NEW.action_id;
  SELECT * INTO r FROM approval_requests WHERE approval_request_id = NEW.approval_request_id;
  IF NEW.action_digest IS DISTINCT FROM a.action_digest THEN
    RAISE EXCEPTION 'action_digest does not match the action' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.target_service_id <> a.target_service_id
     OR NEW.target_environment_id <> a.target_environment_id THEN
    RAISE EXCEPTION 'target does not match the action' USING ERRCODE = 'check_violation';
  END IF;
  SELECT patch_sha256 INTO v_patch FROM patches WHERE action_id = NEW.action_id;
  IF NEW.patch_sha256 IS DISTINCT FROM v_patch THEN
    RAISE EXCEPTION 'patch_sha256 does not match the action''s patch' USING ERRCODE = 'check_violation';
  END IF;

  IF NEW.decision = 'approved' THEN
    IF NEW.decided_by = r.requested_by THEN
      RAISE EXCEPTION 'nobody approves their own request (ADR-49)' USING ERRCODE = 'check_violation';
    END IF;
    -- A conditional approval is signed with the fix, while its rollback is still proposed (ADR-48).
    IF r.closed_at IS NOT NULL
       OR NOT (a.status = 'awaiting_approval' OR (NEW.conditional AND a.status = 'proposed')) THEN
      RAISE EXCEPTION 'the approval request is not open' USING ERRCODE = 'check_violation';
    END IF;
  ELSIF EXISTS (SELECT 1 FROM execution_attempts e WHERE e.action_id = NEW.action_id) THEN
    -- Rejection and revocation stay valid until the dispatcher records intent (ADR-49).
    RAISE EXCEPTION 'too late: the dispatcher has recorded intent' USING ERRCODE = 'check_violation';
  END IF;

  IF NEW.decision = 'revoked' THEN
    SELECT * INTO v_prior FROM approval_decisions WHERE approval_id = NEW.revokes_approval_id;
    IF v_prior.decision IS DISTINCT FROM 'approved' OR v_prior.action_id <> NEW.action_id
       OR v_prior.decided_by <> NEW.decided_by THEN
      RAISE EXCEPTION 'a revocation withdraws the person''s own approval of this action'
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;

  IF NEW.conditional THEN                               -- ADR-48
    SELECT * INTO v_prior FROM approval_decisions WHERE approval_id = NEW.conditional_on_approval_id;
    SELECT * INTO v_parent FROM actions WHERE action_id = v_prior.action_id;
    IF a.kind <> 'rollback' OR a.parent_action_id IS DISTINCT FROM v_prior.action_id
       OR v_prior.decision <> 'approved' OR v_prior.decided_by <> NEW.decided_by THEN
      RAISE EXCEPTION 'a conditional approval covers the rollback of the fix the same person approved'
        USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.valid_until <> v_prior.valid_until
         + make_interval(secs => v_parent.hold_seconds) + interval '15 minutes' THEN
      RAISE EXCEPTION 'conditional valid_until must equal the parent''s + hold + 15 minutes'
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;

-- Execution attempts: the checks made as a row is inserted (ADR-47).
-- Reconciliation rows record a result already looked up, so they send nothing and need no
-- approval check. Any other row is intent, written before the release process is called.
CREATE FUNCTION check_execution_attempt() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  a actions%ROWTYPE;
  v_latest execution_attempts%ROWTYPE;
  v_needed smallint;
  v_valid integer;
BEGIN
  SELECT * INTO a FROM actions WHERE action_id = NEW.action_id;
  -- Serialises intent per deployment, so the block below can't be raced.
  PERFORM pg_advisory_xact_lock(hashtextextended('deployment:' || a.target_deployment, 0));

  IF NEW.action_digest IS DISTINCT FROM a.action_digest THEN
    RAISE EXCEPTION 'action_digest does not match the action' USING ERRCODE = 'check_violation';
  END IF;

  SELECT * INTO v_latest FROM execution_attempts
    WHERE execution_key = NEW.execution_key ORDER BY attempt_no DESC LIMIT 1;
  IF NEW.attempt_no <> COALESCE(v_latest.attempt_no, 0) + 1 THEN
    RAISE EXCEPTION 'attempt_no must be % for this key', COALESCE(v_latest.attempt_no, 0) + 1
      USING ERRCODE = 'check_violation';
  END IF;

  IF NEW.reconciliation THEN
    IF v_latest.outcome IS NULL OR v_latest.outcome NOT IN ('unknown', 'failed')
       OR a.status NOT IN ('applying', 'outcome_unknown') THEN
      RAISE EXCEPTION 'nothing to reconcile for this key' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.resolved_by IS NOT NULL AND a.status <> 'outcome_unknown' THEN
      RAISE EXCEPTION 'a person resolves only an outcome_unknown action, not %', a.status
        USING ERRCODE = 'check_violation';
    END IF;
    NEW.finished_at := COALESCE(NEW.finished_at, now());
    RETURN NEW;
  END IF;

  IF NEW.outcome <> 'started' THEN
    RAISE EXCEPTION 'an attempt starts as started, not %', NEW.outcome USING ERRCODE = 'check_violation';
  END IF;
  IF a.status <> 'applying' THEN
    RAISE EXCEPTION 'action is %, not applying', a.status USING ERRCODE = 'check_violation';
  END IF;
  IF v_latest.outcome IS NOT NULL AND v_latest.outcome <> 'not_applied' THEN
    RAISE EXCEPTION 'retry refused: the latest attempt for this key is %, not a confirmed not_applied',
      v_latest.outcome USING ERRCODE = 'check_violation';
  END IF;

  -- The approval must still stand: enough valid, unrevoked approvals and no rejection.
  IF EXISTS (SELECT 1 FROM approval_decisions d
             WHERE d.action_id = NEW.action_id AND d.decision = 'rejected') THEN
    RAISE EXCEPTION 'action was rejected' USING ERRCODE = 'check_violation';
  END IF;
  SELECT r.approvers_needed INTO v_needed FROM approval_requests r
    WHERE r.action_id = NEW.action_id ORDER BY r.requested_at DESC LIMIT 1;
  SELECT count(*) INTO v_valid FROM approval_decisions d
    WHERE d.action_id = NEW.action_id AND d.decision = 'approved' AND d.valid_until >= now()
      AND NOT EXISTS (SELECT 1 FROM approval_decisions x WHERE x.revokes_approval_id = d.approval_id);
  IF v_needed IS NULL OR v_valid < v_needed THEN
    RAISE EXCEPTION 'approval no longer stands: % valid of % needed', v_valid, v_needed
      USING ERRCODE = 'check_violation';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM approval_decisions d
                 WHERE d.approval_id = NEW.approval_id AND d.decision = 'approved'
                   AND d.valid_until >= now()
                   AND NOT EXISTS (SELECT 1 FROM approval_decisions x
                                   WHERE x.revokes_approval_id = d.approval_id)) THEN
    RAISE EXCEPTION 'approval_id is not a standing approval' USING ERRCODE = 'check_violation';
  END IF;

  -- An unknown outcome blocks every further change on that deployment (ADR-47), and only
  -- one change runs on a deployment at a time. The action's status clears when a person or
  -- reconciliation settles it.
  IF EXISTS (
    SELECT 1 FROM actions o
    WHERE o.target_deployment = a.target_deployment AND o.action_id <> a.action_id
      AND (o.status = 'outcome_unknown'
           OR (o.status = 'applying' AND EXISTS (
                 SELECT 1 FROM execution_attempts e
                 WHERE e.action_id = o.action_id AND e.outcome IN ('started', 'unknown')
                   AND NOT EXISTS (SELECT 1 FROM execution_attempts l
                                   WHERE l.action_id = e.action_id AND l.attempt_no > e.attempt_no))))
  ) THEN
    RAISE EXCEPTION 'deployment % is blocked by an unresolved change', a.target_deployment
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

-- An attempt changes once, from 'started' to its final outcome; never again.
CREATE FUNCTION freeze_execution_attempt() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.outcome <> 'started' THEN
    RAISE EXCEPTION 'execution attempt % is final (%): no further change', OLD.attempt_id, OLD.outcome
      USING ERRCODE = 'restrict_violation';
  END IF;
  IF NEW.outcome = 'started' THEN
    RAISE EXCEPTION 'an attempt update must set the final outcome' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

-- Budget: a reservation moves money and tokens inside the budget row. The UPDATE takes the
-- row lock, and the table's CHECK constraints reject any overrun, so a refused call rolls back.
-- SECURITY DEFINER because no application role may update incident_budgets directly.
CREATE FUNCTION apply_reservation() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'reserved' THEN
      RAISE EXCEPTION 'a reservation starts as reserved' USING ERRCODE = 'check_violation';
    END IF;
    UPDATE incident_budgets
       SET used_calls = used_calls + 1,
           reserved_cost_micros = reserved_cost_micros + NEW.reserved_cost_micros,
           used_input_tokens = used_input_tokens + NEW.reserved_input_tokens,
           used_output_tokens = used_output_tokens + NEW.reserved_output_tokens
     WHERE incident_id = NEW.incident_id;
    RETURN NEW;
  END IF;

  IF OLD.status = 'reserved' AND NEW.status = 'reserved'
     AND OLD.sent_at IS NULL AND NEW.sent_at IS NOT NULL THEN
    RETURN NEW;                                         -- marked sent: no money moves
  END IF;
  IF OLD.status <> 'reserved' OR NEW.status = 'reserved' THEN
    RAISE EXCEPTION 'illegal reservation transition: % -> %', OLD.status, NEW.status
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.status = 'settled' THEN
    UPDATE incident_budgets
       SET reserved_cost_micros = reserved_cost_micros - OLD.reserved_cost_micros,
           spent_cost_micros = spent_cost_micros + NEW.actual_cost_micros,
           used_input_tokens = used_input_tokens - OLD.reserved_input_tokens
             + COALESCE(NEW.actual_input_tokens, OLD.reserved_input_tokens),
           used_output_tokens = used_output_tokens - OLD.reserved_output_tokens
             + COALESCE(NEW.actual_output_tokens, OLD.reserved_output_tokens)
     WHERE incident_id = NEW.incident_id;
  ELSE                                                  -- released: never sent (ADR-61)
    UPDATE incident_budgets
       SET used_calls = used_calls - 1,
           reserved_cost_micros = reserved_cost_micros - OLD.reserved_cost_micros,
           used_input_tokens = used_input_tokens - OLD.reserved_input_tokens,
           used_output_tokens = used_output_tokens - OLD.reserved_output_tokens
     WHERE incident_id = NEW.incident_id;
  END IF;
  RETURN NEW;
END $$;

-- The controlled path for a settled reservation: see reservation_corrections.
CREATE FUNCTION apply_reservation_correction() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  r budget_reservations%ROWTYPE;
BEGIN
  SELECT * INTO r FROM budget_reservations WHERE reservation_id = NEW.reservation_id FOR UPDATE;
  IF r.status <> 'settled'
     OR (r.actual_input_tokens IS NOT NULL AND r.actual_output_tokens IS NOT NULL) THEN
    RAISE EXCEPTION 'only a reservation settled with unknown usage can be corrected'
      USING ERRCODE = 'check_violation';
  END IF;
  UPDATE incident_budgets
     SET spent_cost_micros = spent_cost_micros + NEW.actual_cost_micros - r.actual_cost_micros,
         used_input_tokens = used_input_tokens + NEW.actual_input_tokens
           - COALESCE(r.actual_input_tokens, r.reserved_input_tokens),
         used_output_tokens = used_output_tokens + NEW.actual_output_tokens
           - COALESCE(r.actual_output_tokens, r.reserved_output_tokens)
   WHERE incident_id = NEW.incident_id;
  RETURN NEW;
END $$;

-- The timeline. Hashing happens here, so the application can't choose a hash.
-- `seq` comes from a counter on the incident row, not a sequence: a rolled-back transaction
-- consumes no number, and the row lock serialises writers, so sequences stay gapless.
CREATE FUNCTION timeline_event_hash(e timeline_events) RETURNS sha256_hex
LANGUAGE sql STABLE AS $$
  SELECT encode(sha256(convert_to(concat_ws(chr(31),
    COALESCE(e.prev_hash::text, ''), e.event_id::text, e.incident_id::text, e.seq::text,
    e.type, e.source, e.actor,
    to_char(e.occurred_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
    to_char(e.recorded_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
    COALESCE(e.causation_id::text, ''), e.correlation_id::text,
    COALESCE(e.traceparent, ''), COALESCE(e.dataschema, ''),
    e.schema_version::text, e.data::text), 'UTF8')), 'hex')::sha256_hex
$$;

CREATE FUNCTION assign_timeline_event() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  v_seq bigint;
BEGIN
  UPDATE incidents SET last_event_seq = last_event_seq + 1
   WHERE incident_id = NEW.incident_id
   RETURNING last_event_seq INTO v_seq;
  IF v_seq IS NULL THEN
    RAISE EXCEPTION 'unknown incident %', NEW.incident_id USING ERRCODE = 'foreign_key_violation';
  END IF;
  IF NEW.seq IS NOT NULL AND NEW.seq <> v_seq THEN
    RAISE EXCEPTION 'next sequence is %, not %', v_seq, NEW.seq USING ERRCODE = 'check_violation';
  END IF;
  NEW.seq := v_seq;
  NEW.recorded_at := now();
  SELECT hash INTO NEW.prev_hash FROM timeline_events
   WHERE incident_id = NEW.incident_id AND seq = v_seq - 1;
  NEW.hash := timeline_event_hash(NEW);
  RETURN NEW;
END $$;

-- The dispatcher writes only its own events (ADR-51). This runs as the caller, which is
-- why it is separate from the SECURITY DEFINER function above.
CREATE FUNCTION check_timeline_writer() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF current_user = 'aisre_dispatcher' AND NEW.type NOT LIKE 'aisre.execution.%' THEN
    RAISE EXCEPTION 'the dispatcher writes only aisre.execution.* events'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;

-- Walks one incident's chain. Returns one row per fault; no rows means the chain is intact.
CREATE FUNCTION verify_timeline_chain(p_incident_id uuid)
RETURNS TABLE (at_seq bigint, problem text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
  e timeline_events%ROWTYPE;
  v_expected bigint := 0;
  v_prev sha256_hex;
  v_last bigint;
BEGIN
  FOR e IN SELECT * FROM timeline_events WHERE incident_id = p_incident_id ORDER BY seq LOOP
    v_expected := v_expected + 1;
    IF e.seq <> v_expected THEN
      at_seq := e.seq; problem := 'gap: expected ' || v_expected; RETURN NEXT;
      v_expected := e.seq;
    END IF;
    IF e.prev_hash IS DISTINCT FROM v_prev THEN
      at_seq := e.seq; problem := 'prev_hash does not match the previous event'; RETURN NEXT;
    END IF;
    IF e.hash <> timeline_event_hash(e) THEN
      at_seq := e.seq; problem := 'hash does not match the event'; RETURN NEXT;
    END IF;
    v_prev := e.hash;
  END LOOP;
  SELECT last_event_seq INTO v_last FROM incidents WHERE incident_id = p_incident_id;
  IF v_last <> v_expected THEN
    at_seq := v_last; problem := 'last_event_seq is ' || v_last || ', events end at ' || v_expected;
    RETURN NEXT;
  END IF;
END $$;

-- Append-only tables, and the key that tells retention when a row is due.
DO $$
DECLARE
  t record;
BEGIN
  FOR t IN SELECT * FROM (VALUES
    ('timeline_events', 'incident_id'), ('model_attempts', 'incident_id'),
    ('artifacts', 'incident_id'), ('artifact_deletions', 'incident_id'),
    ('incident_alerts', 'incident_id'), ('reservation_corrections', 'incident_id'),
    ('approval_decisions', 'action_id'), ('patches', 'action_id'), ('test_runs', 'action_id'),
    ('dead_letters', 'retain_until'),
    ('documents', NULL), ('chunks', NULL), ('index_revisions', NULL),
    ('index_revision_documents', NULL)
  ) AS v (tbl, how) LOOP
    EXECUTE format(
      'CREATE TRIGGER t10_append_only BEFORE UPDATE OR DELETE ON %I
         FOR EACH ROW EXECUTE FUNCTION protect_row(%s)',
      t.tbl, COALESCE(quote_literal(t.how), ''));
  END LOOP;

  -- Tables that change in controlled ways still refuse DELETE, except expired purges.
  FOR t IN SELECT * FROM (VALUES
    ('evidence', 'incident_id'), ('execution_attempts', 'action_id'),
    ('verifications', 'action_id'), ('approval_requests', 'action_id'), ('actions', 'incident_id'),
    ('hypotheses', 'incident_id'), ('diagnoses', 'incident_id'), ('incidents', 'incident_id'),
    ('incident_budgets', 'incident_id'), ('budget_reservations', 'incident_id')
  ) AS v (tbl, how) LOOP
    EXECUTE format(
      'CREATE TRIGGER t10_no_delete BEFORE DELETE ON %I
         FOR EACH ROW EXECUTE FUNCTION protect_row(%L)', t.tbl, t.how);
  END LOOP;

  FOR t IN SELECT unnest(ARRAY[
    'timeline_events', 'evidence', 'model_attempts', 'artifacts', 'artifact_deletions',
    'incident_alerts', 'approval_decisions', 'patches', 'test_runs', 'dead_letters',
    'documents', 'chunks', 'index_revisions', 'index_revision_documents',
    'execution_attempts', 'verifications', 'approval_requests', 'actions', 'hypotheses',
    'diagnoses', 'incidents', 'incident_budgets', 'budget_reservations',
    'reservation_corrections']) AS tbl LOOP
    EXECUTE format(
      'CREATE TRIGGER t00_no_truncate BEFORE TRUNCATE ON %I
         FOR EACH STATEMENT EXECUTE FUNCTION protect_truncate()', t.tbl);
  END LOOP;
END $$;

CREATE TRIGGER t10_append_only BEFORE UPDATE ON evidence FOR EACH ROW
  EXECUTE FUNCTION protect_evidence();
CREATE TRIGGER t20_freeze BEFORE UPDATE ON evidence FOR EACH ROW
  EXECUTE FUNCTION freeze_except('summary', 'attributes', 'content_removed_at?', 'content_removed_reason?');

CREATE TRIGGER t20_freeze BEFORE UPDATE ON incidents FOR EACH ROW
  EXECUTE FUNCTION freeze_except('status', 'proposed_severity', 'confirmed_severity',
    'severity_confirmed_by', 'investigation_run', 'version', 'last_event_seq', 'updated_at', 'closed_at');
CREATE TRIGGER t30_transition BEFORE INSERT OR UPDATE ON incidents FOR EACH ROW
  EXECUTE FUNCTION check_incident_transition();
CREATE TRIGGER t40_version BEFORE UPDATE ON incidents FOR EACH ROW
  EXECUTE FUNCTION check_version('last_event_seq', 'updated_at');

-- Actions: status and version only, plus the two write-once digests.
CREATE TRIGGER t20_freeze BEFORE UPDATE ON actions FOR EACH ROW
  EXECUTE FUNCTION freeze_except('status', 'version', 'action_digest?', 'test_bundle_digest?');
CREATE TRIGGER t30_transition BEFORE INSERT OR UPDATE ON actions FOR EACH ROW
  EXECUTE FUNCTION check_action_transition();
CREATE TRIGGER t40_version BEFORE UPDATE ON actions FOR EACH ROW
  EXECUTE FUNCTION check_version();

CREATE TRIGGER t20_freeze BEFORE UPDATE ON hypotheses FOR EACH ROW
  EXECUTE FUNCTION freeze_except('status', 'confidence', 'status_reason', 'version');
CREATE TRIGGER t40_version BEFORE UPDATE ON hypotheses FOR EACH ROW
  EXECUTE FUNCTION check_version();

CREATE TRIGGER t20_freeze BEFORE UPDATE ON diagnoses FOR EACH ROW
  EXECUTE FUNCTION freeze_except('reviewed', 'review_notes?');
CREATE TRIGGER t20_freeze BEFORE UPDATE ON verifications FOR EACH ROW
  EXECUTE FUNCTION freeze_except('accepted_by?', 'accepted_at?');
CREATE TRIGGER t20_freeze BEFORE UPDATE ON approval_requests FOR EACH ROW
  EXECUTE FUNCTION freeze_except('closed_at?', 'close_reason?');
CREATE TRIGGER t20_guard BEFORE INSERT ON approval_requests FOR EACH ROW
  EXECUTE FUNCTION check_approval_request();
CREATE TRIGGER t20_guard BEFORE INSERT ON approval_decisions FOR EACH ROW
  EXECUTE FUNCTION check_approval_decision();

CREATE TRIGGER t20_guard BEFORE INSERT ON execution_attempts FOR EACH ROW
  EXECUTE FUNCTION check_execution_attempt();
CREATE TRIGGER t20_once BEFORE UPDATE ON execution_attempts FOR EACH ROW
  EXECUTE FUNCTION freeze_execution_attempt();
CREATE TRIGGER t30_freeze BEFORE UPDATE ON execution_attempts FOR EACH ROW
  EXECUTE FUNCTION freeze_except('outcome', 'finished_at', 'external_ref', 'response_artifact_id', 'error');

CREATE TRIGGER t20_freeze BEFORE UPDATE ON budget_reservations FOR EACH ROW
  EXECUTE FUNCTION freeze_except('status', 'actual_cost_micros', 'actual_input_tokens',
    'actual_output_tokens', 'settled_at', 'sent_at?');
CREATE TRIGGER t30_apply AFTER INSERT OR UPDATE ON budget_reservations FOR EACH ROW
  EXECUTE FUNCTION apply_reservation();
CREATE TRIGGER t30_apply AFTER INSERT ON reservation_corrections FOR EACH ROW
  EXECUTE FUNCTION apply_reservation_correction();

CREATE TRIGGER t20_writer BEFORE INSERT ON timeline_events FOR EACH ROW
  EXECUTE FUNCTION check_timeline_writer();
CREATE TRIGGER t30_assign BEFORE INSERT ON timeline_events FOR EACH ROW
  EXECUTE FUNCTION assign_timeline_event();

-- ---------------------------------------------------------------------------
-- 13. Retention (Technical specification section 10, ADR-57)
-- ---------------------------------------------------------------------------
-- Removes one incident's whole record, once every part of it has expired. Runs as the
-- caller, so aisre_retention's own grants and the protect_row checks apply on each table.
CREATE FUNCTION purge_incident(p_incident_id uuid) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  IF NOT COALESCE(incident_retention_due(p_incident_id), false) THEN
    RAISE EXCEPTION 'incident % is not due for purge', p_incident_id
      USING ERRCODE = 'restrict_violation';
  END IF;
  DELETE FROM model_attempts WHERE incident_id = p_incident_id;
  DELETE FROM reservation_corrections WHERE incident_id = p_incident_id;
  DELETE FROM budget_reservations WHERE incident_id = p_incident_id;
  DELETE FROM incident_budgets WHERE incident_id = p_incident_id;
  DELETE FROM verifications v USING actions a
    WHERE v.action_id = a.action_id AND a.incident_id = p_incident_id;
  DELETE FROM execution_attempts e USING actions a
    WHERE e.action_id = a.action_id AND a.incident_id = p_incident_id;
  DELETE FROM approval_decisions d USING actions a
    WHERE d.action_id = a.action_id AND a.incident_id = p_incident_id;
  DELETE FROM approval_requests r USING actions a
    WHERE r.action_id = a.action_id AND a.incident_id = p_incident_id;
  DELETE FROM test_runs t USING actions a
    WHERE t.action_id = a.action_id AND a.incident_id = p_incident_id;
  DELETE FROM patches p USING actions a
    WHERE p.action_id = a.action_id AND a.incident_id = p_incident_id;
  DELETE FROM actions WHERE incident_id = p_incident_id;
  DELETE FROM diagnoses WHERE incident_id = p_incident_id;
  DELETE FROM hypothesis_evidence l USING hypotheses h
    WHERE l.hypothesis_id = h.hypothesis_id AND h.incident_id = p_incident_id;
  DELETE FROM hypotheses WHERE incident_id = p_incident_id;
  DELETE FROM evidence WHERE incident_id = p_incident_id;
  DELETE FROM artifact_deletions WHERE incident_id = p_incident_id;
  DELETE FROM artifacts WHERE incident_id = p_incident_id;
  DELETE FROM timeline_events WHERE incident_id = p_incident_id;
  DELETE FROM outbox WHERE incident_id = p_incident_id;
  DELETE FROM incident_alerts WHERE incident_id = p_incident_id;
  DELETE FROM incidents WHERE incident_id = p_incident_id;
END $$;

-- ---------------------------------------------------------------------------
-- 14. Views
-- ---------------------------------------------------------------------------
-- The stored event in CloudEvents wire names (ADR-64). Storage stays snake_case.
CREATE VIEW timeline_cloudevents WITH (security_invoker = true) AS
SELECT e.event_id AS id, '1.0'::text AS specversion, e.source, e.type,
       'incidents/' || e.incident_id::text AS subject,
       e.occurred_at AS "time", 'application/json'::text AS datacontenttype, e.dataschema,
       e.incident_id AS incidentid, e.seq AS sequence, i.org_id AS orgid, e.actor,
       e.correlation_id AS correlationid, e.causation_id AS causationid, e.traceparent, e.data
FROM timeline_events e JOIN incidents i USING (incident_id);

-- Object-store content the retention job should delete next (ADR-57, provisional periods):
-- past retain_until, not deleted yet, and not an evidence snapshot still cited by a diagnosis.
-- Cited evidence, meaning linked to a hypothesis, is kept for a year after collection.
-- Evidence marked "content removed" takes its snapshot with it, whatever its retention says.
CREATE VIEW artifacts_due_for_deletion WITH (security_invoker = true) AS
SELECT a.artifact_id, a.incident_id, a.storage_key
FROM artifacts a
WHERE (a.retain_until < now()
       OR EXISTS (SELECT 1 FROM evidence e
                  WHERE e.artifact_id = a.artifact_id AND e.content_removed_at IS NOT NULL))
  AND NOT EXISTS (SELECT 1 FROM artifact_deletions d WHERE d.artifact_id = a.artifact_id)
  AND NOT EXISTS (SELECT 1 FROM evidence e JOIN hypothesis_evidence l USING (evidence_id)
                  WHERE e.artifact_id = a.artifact_id AND e.content_removed_at IS NULL
                    AND e.collected_at >= now() - interval '1 year');

-- ---------------------------------------------------------------------------
-- 15. Roles
-- ---------------------------------------------------------------------------
-- Separate role block: review it on its own. Each service connects directly as its role,
-- because protect_row and check_timeline_writer compare current_user. Set passwords outside
-- this file. Only the owner runs migrations.
DO $$
DECLARE
  r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['aisre_app', 'aisre_dispatcher', 'aisre_retention'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE', r);
    END IF;
  END LOOP;
END $$;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO aisre_app, aisre_dispatcher, aisre_retention;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC;

-- API and worker. No DELETE except freed investigation slots; no write to execution_attempts, ever.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO aisre_app;
GRANT INSERT ON
  services, environments, incidents, incident_alerts, dead_letters, artifacts, evidence,
  hypotheses, hypothesis_evidence, diagnoses, actions, patches, test_runs,
  approval_requests, approval_decisions, verifications, documents, chunks, index_revisions,
  index_revision_documents, incident_budgets, budget_reservations, reservation_corrections,
  model_attempts, timeline_events, outbox, idempotency_keys, investigation_queue, fence_reasons
  TO aisre_app;
GRANT UPDATE ON
  services, environments, incidents, hypotheses, diagnoses, actions, approval_requests,
  budget_reservations, outbox, idempotency_keys, investigation_queue, fence_state
  TO aisre_app;
GRANT UPDATE (cleared_by, cleared_at) ON fence_reasons TO aisre_app;
GRANT UPDATE (content_removed_at, content_removed_reason) ON evidence TO aisre_app;
GRANT DELETE ON investigation_queue TO aisre_app;
GRANT UPDATE (accepted_by, accepted_at) ON verifications TO aisre_app;
GRANT EXECUTE ON FUNCTION verify_timeline_chain(uuid), timeline_event_hash(timeline_events),
  action_is_high_impact(actions) TO aisre_app;

-- Trusted dispatcher (ADR-51): reads actions, patches, approvals and verification results;
-- writes attempts, action status for `applying` and its outcomes, and its own timeline
-- events. It stores the release process's raw response as an artifact (Q7.12).
-- UPDATE on actions covers status and version only; check_action_transition limits which
-- status changes, and needs action_transitions to do it.
GRANT SELECT ON actions, patches, approval_requests, approval_decisions, execution_attempts,
  verifications, action_transitions
  TO aisre_dispatcher;
GRANT INSERT ON execution_attempts, timeline_events, artifacts TO aisre_dispatcher;
GRANT UPDATE (outcome, finished_at, external_ref, response_artifact_id, error)
  ON execution_attempts TO aisre_dispatcher;
GRANT UPDATE (status, version) ON actions TO aisre_dispatcher;

-- Retention job (Technical specification section 10): deletes what has expired, and nothing
-- else. Protected tables also make protect_row confirm each row is due.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO aisre_retention;
GRANT DELETE ON
  incidents, incident_alerts, dead_letters, artifacts, artifact_deletions, evidence, hypotheses,
  hypothesis_evidence, diagnoses, actions, patches, test_runs, approval_requests,
  approval_decisions, execution_attempts, verifications, incident_budgets, budget_reservations,
  reservation_corrections, model_attempts, timeline_events, outbox, idempotency_keys
  TO aisre_retention;
GRANT INSERT ON artifact_deletions TO aisre_retention;
GRANT EXECUTE ON FUNCTION incident_retention_due(uuid), purge_incident(uuid) TO aisre_retention;

COMMIT;
