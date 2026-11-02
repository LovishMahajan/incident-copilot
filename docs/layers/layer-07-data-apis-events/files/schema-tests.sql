-- schema-tests.sql (proposed, 2026-11-02): proves the rules in schema.sql on a real server.
--
-- Run as a superuser, in a database where schema.sql has just been loaded by aisre_owner:
--   psql -d aisre -v ON_ERROR_STOP=1 -f schema-tests.sql
-- A superuser is needed only for SET ROLE and for the trigger-bypass and tamper tests.
-- The script prints one line per check and exits non-zero if any check fails.

\set QUIET on
\pset format unaligned
\pset tuples_only on
\o /dev/null

CREATE TEMP TABLE test_results (n serial, label text, ok boolean, detail text);

-- must_fail: stmt must raise an error whose message contains pattern. All its effects roll back.
CREATE FUNCTION pg_temp.must_fail(label text, stmt text, pattern text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  msg text;
  good boolean := false;
BEGIN
  BEGIN
    EXECUTE stmt;
    RAISE EXCEPTION 'TEST_NO_ERROR';
  EXCEPTION WHEN OTHERS THEN
    msg := SQLERRM;
  END;
  good := msg <> 'TEST_NO_ERROR' AND position(lower(pattern) IN lower(msg)) > 0;
  INSERT INTO test_results (label, ok, detail) VALUES (label, good, msg);
END $$;

-- must_pass: stmt must succeed, and its effects stay.
CREATE FUNCTION pg_temp.must_pass(label text, stmt text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  msg text;
BEGIN
  BEGIN
    EXECUTE stmt;
    RESET ROLE;
    INSERT INTO test_results (label, ok, detail) VALUES (label, true, 'ok');
  EXCEPTION WHEN OTHERS THEN
    msg := SQLERRM;
    RESET ROLE;
    INSERT INTO test_results (label, ok, detail) VALUES (label, false, msg);
  END;
END $$;

-- must_equal: a scalar query must return the expected text.
CREATE FUNCTION pg_temp.must_equal(label text, query text, expected text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  got text;
BEGIN
  EXECUTE query INTO got;
  INSERT INTO test_results (label, ok, detail)
    VALUES (label, got IS NOT DISTINCT FROM expected, 'got ' || COALESCE(got, 'NULL'));
END $$;

-- Timeline helper: appends one event as the given role. seq and hashes come from the database.
CREATE FUNCTION pg_temp.ev(inc text, typ text, who text DEFAULT 'aisre_app') RETURNS text
LANGUAGE sql AS $$
  SELECT format($f$SET LOCAL ROLE %I; INSERT INTO timeline_events
    (event_id, incident_id, type, source, actor, occurred_at, correlation_id, data)
    VALUES (gen_random_uuid(), %L, %L, '/aisre/test', 'test', now(), %L, '{}')$f$,
    who, inc, typ, inc)
$$;

-- ---------------------------------------------------------------------------
-- Fixtures: fixed IDs. 1xx incidents, 2xx actions.
-- ---------------------------------------------------------------------------
\set org '''00000000-0000-7000-8000-000000000001'''
\set svc '''00000000-0000-7000-8000-000000000002'''
\set env '''00000000-0000-7000-8000-000000000003'''
\set inc '''00000000-0000-7000-8000-000000000101'''
\set inc2 '''00000000-0000-7000-8000-000000000102'''
\set hyp1 '''00000000-0000-7000-8000-000000000301'''
\set hyp2 '''00000000-0000-7000-8000-000000000302'''
\set dia '''00000000-0000-7000-8000-000000000401'''
\set act '''00000000-0000-7000-8000-000000000201'''
\set rbk '''00000000-0000-7000-8000-000000000202'''
\set act3 '''00000000-0000-7000-8000-000000000203'''
\set act4 '''00000000-0000-7000-8000-000000000204'''
\set act5 '''00000000-0000-7000-8000-000000000205'''
\set req '''00000000-0000-7000-8000-000000000501'''
\set req2 '''00000000-0000-7000-8000-000000000502'''
\set req3 '''00000000-0000-7000-8000-000000000503'''
\set req4 '''00000000-0000-7000-8000-000000000504'''
\set req5 '''00000000-0000-7000-8000-000000000505'''
\set apv '''00000000-0000-7000-8000-000000000601'''
\set apv2 '''00000000-0000-7000-8000-000000000602'''
\set apv3 '''00000000-0000-7000-8000-000000000603'''
\set apv4 '''00000000-0000-7000-8000-000000000604'''
\set apv5 '''00000000-0000-7000-8000-000000000605'''
\set apv6 '''00000000-0000-7000-8000-000000000606'''
\set att1 '''00000000-0000-7000-8000-000000000701'''
\set att2 '''00000000-0000-7000-8000-000000000702'''
\set att3 '''00000000-0000-7000-8000-000000000703'''
\set att4 '''00000000-0000-7000-8000-000000000704'''
\set att5 '''00000000-0000-7000-8000-000000000705'''
\set res '''00000000-0000-7000-8000-000000000801'''
\set res2 '''00000000-0000-7000-8000-000000000803'''
\set res3 '''00000000-0000-7000-8000-000000000804'''
\set art '''00000000-0000-7000-8000-000000000901'''
\set art2 '''00000000-0000-7000-8000-000000000902'''
\set art3 '''00000000-0000-7000-8000-000000000903'''
\set evd '''00000000-0000-7000-8000-000000000a01'''
\set evd2 '''00000000-0000-7000-8000-000000000a02'''
\set evd3 '''00000000-0000-7000-8000-000000000a03'''
\set doc '''00000000-0000-7000-8000-000000000b01'''
\set rev '''00000000-0000-7000-8000-000000000b02'''
\set h_a '''aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'''
\set h_b '''bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'''
\set h_c '''cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'''
\set h_d '''dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd'''

-- ---------------------------------------------------------------------------
-- A. The state machines match ADR-46
-- ---------------------------------------------------------------------------
SELECT pg_temp.must_equal('A1 incident enum has 14 states', $$SELECT count(*) FROM unnest(enum_range(NULL::incident_status))$$, '14');
SELECT pg_temp.must_equal('A2 action enum has 14 states', $$SELECT count(*) FROM unnest(enum_range(NULL::action_status))$$, '14');
SELECT pg_temp.must_equal('A3 30 incident transitions', 'SELECT count(*) FROM incident_transitions', '30');
SELECT pg_temp.must_equal('A4 20 action transitions', 'SELECT count(*) FROM action_transitions', '20');

-- ---------------------------------------------------------------------------
-- B. Ordinary flow, end to end, using the application's and dispatcher's own roles
-- ---------------------------------------------------------------------------
SET ROLE aisre_app;
INSERT INTO services (service_id, org_id, slug, display_name, owner_group)
  VALUES (:svc, :org, 'checkout', 'Checkout', 'team-checkout');
INSERT INTO environments (environment_id, org_id, slug) VALUES (:env, :org, 'production');

BEGIN;
INSERT INTO incidents (incident_id, org_id, display_ref, service_id, environment_id, dedup_key,
                       title, source, started_at, created_by, confirmed_severity, severity_confirmed_by)
  VALUES (:inc, :org, 'INC-2026-000001', :svc, :env, 'checkout:http_5xx', 'HTTP 500s',
          'monitoring', now(), 'system:intake', 'sev3', 'user:sam');
INSERT INTO incident_budgets (incident_id, max_calls, max_input_tokens, max_output_tokens, max_cost_micros)
  VALUES (:inc, 30, 200000, 20000, 2000000);
INSERT INTO incident_alerts (alert_id, incident_id, source_alert_id, fingerprint, payload)
  VALUES (gen_random_uuid(), :inc, 'mon-1', 'checkout:http_5xx', '{"value": 12}');
INSERT INTO timeline_events (event_id, incident_id, type, source, actor, occurred_at, correlation_id, data)
  VALUES (gen_random_uuid(), :inc, 'aisre.incident.created.v1', '/aisre/api', 'system:intake', now(), :inc, '{}');
INSERT INTO outbox (message_id, incident_id, kind, workflow_id, payload)
  VALUES (gen_random_uuid(), :inc, 'start_workflow', 'incident-' || :inc, '{}');
INSERT INTO idempotency_keys (caller_id, operation, idem_key, request_sha256, state, response_status, expires_at)
  VALUES ('monitoring', 'POST /v1/incidents', 'key-00000001', :h_a, 'completed', 201, now() + interval '24 hours');
COMMIT;

UPDATE incidents SET status = 'investigating', version = version + 1 WHERE incident_id = :inc AND version = 1;

INSERT INTO artifacts (artifact_id, org_id, incident_id, kind, storage_key, content_sha256, size_bytes,
                       media_type, redacted, retain_until)
  VALUES (:art, :org, :inc, 'evidence_snapshot', 'obj/1', :h_a, 10, 'text/plain', true, now() + interval '30 days');
INSERT INTO evidence (evidence_id, incident_id, kind, source_system, tool, collected_by, summary,
                      artifact_id, content_sha256, strength_tier, attributes)
  VALUES (:evd, :inc, 'change', 'release-log', 'read_changes', 'evidence-collector', 'Deploy at 09:10',
          :art, :h_a, 2, '{"revision": "r42"}');
INSERT INTO evidence (evidence_id, incident_id, kind, source_system, tool, collected_by, summary,
                      content_sha256, gap_reason)
  VALUES (gen_random_uuid(), :inc, 'gap', 'traces', 'read_traces', 'evidence-collector', 'No traces',
          :h_b, 'source unavailable');

INSERT INTO hypotheses (hypothesis_id, incident_id, ordinal, statement, category, confirm_test, refute_test, confidence)
  VALUES (:hyp1, :inc, 1, 'Config change', 'deployment_config', 'revert clears', 'still fails', 'medium'),
         (:hyp2, :inc, 2, 'Dependency down', 'dependency', 'dep errors', 'dep healthy', 'low');
INSERT INTO hypothesis_evidence (hypothesis_id, evidence_id, relation) VALUES (:hyp1, :evd, 'supports');

INSERT INTO budget_reservations (reservation_id, incident_id, reserved_cost_micros, reserved_input_tokens,
                                 reserved_output_tokens, expires_at)
  VALUES (:res, :inc, 100000, 1000, 500, now() + interval '5 minutes');
UPDATE budget_reservations SET sent_at = now() WHERE reservation_id = :res;
INSERT INTO model_attempts (model_attempt_id, incident_id, reservation_id, task, step_attempt_no, provider, model,
                            adapter_version, prompt_template_version, prompt_sha256, input_sha256, status,
                            stop_reason, input_tokens, output_tokens, cost_micros, started_at)
  VALUES (gen_random_uuid(), :inc, :res, 'diagnose', 1, 'anthropic', 'model-x', 'a1', 'v1', :h_a, :h_b, 'ok',
          'complete', 900, 400, 80000, now());
UPDATE budget_reservations SET status = 'settled', actual_cost_micros = 80000, actual_input_tokens = 900,
       actual_output_tokens = 400, settled_at = now() WHERE reservation_id = :res;
RESET ROLE;
SELECT pg_temp.must_equal('B1 settling moves reserved to spent',
  $$SELECT used_calls || '/' || reserved_cost_micros || '/' || spent_cost_micros || '/' || used_input_tokens || '/' || used_output_tokens
    FROM incident_budgets WHERE incident_id = '00000000-0000-7000-8000-000000000101'$$, '1/0/80000/900/400');
SET ROLE aisre_app;

INSERT INTO diagnoses (diagnosis_id, incident_id, revision, outcome, leading_hypothesis_id, category, confidence,
                       summary, ruled_out, produced_by)
  VALUES (:dia, :inc, 1, 'established', :hyp1, 'deployment_config', 'medium', 'Bad config', '{H2: dep healthy}', 'diagnosis-engine');
UPDATE diagnoses SET reviewed = true, review_notes = 'accepted' WHERE diagnosis_id = :dia;
UPDATE incidents SET status = 'diagnosed', version = version + 1 WHERE incident_id = :inc;
UPDATE incidents SET status = 'testing', version = version + 1 WHERE incident_id = :inc;

INSERT INTO actions (action_id, incident_id, diagnosis_id, kind, target_service_id, target_environment_id,
                     target_deployment, base_revision, expected_after_revision, description, expected_effect, risk, undo_plan,
                     recovery_criteria, recovery_predicates_digest, hold_seconds, high_impact, policy_version)
  VALUES (:act, :inc, :dia, 'config_revert', :svc, :env, 'checkout-prod-eu1', 'r42', 'r43', 'Revert', 'Errors stop',
          'Low', 'Roll back', '[{"metric": "http_5xx_rate", "comparison": "<=", "threshold": 0.01}]', :h_b, 600, false, 'p@1');
INSERT INTO artifacts (artifact_id, org_id, incident_id, kind, storage_key, content_sha256, size_bytes,
                       media_type, redacted, retain_until)
  VALUES (:art2, :org, :inc, 'patch_diff', 'obj/2', :h_c, 10, 'text/x-diff', true, now() + interval '1 year');
INSERT INTO patches (patch_id, action_id, base_revision, diff_artifact_id, patch_sha256, files_changed, lines_added, lines_removed)
  VALUES (gen_random_uuid(), :act, 'r42', :art2, :h_c, 1, 1, 1);
UPDATE actions SET status = 'testing', version = version + 1 WHERE action_id = :act;
INSERT INTO test_runs (test_run_id, action_id, patch_sha256, reproduced, outcome, started_at, finished_at)
  VALUES (gen_random_uuid(), :act, :h_c, true, 'passed', now(), now());
UPDATE actions SET status = 'awaiting_approval', version = version + 1, action_digest = :h_d, test_bundle_digest = :h_a
  WHERE action_id = :act;
UPDATE incidents SET status = 'awaiting_approval', version = version + 1 WHERE incident_id = :inc;

INSERT INTO approval_requests (approval_request_id, action_id, requested_by, patch_sha256, escalate_at, expires_at)
  VALUES (:req, :act, 'system:workflow', :h_c, now() + interval '15 minutes', now() + interval '60 minutes');
INSERT INTO approval_decisions (approval_id, approval_request_id, action_id, decision, decided_by, decided_by_role,
                                auth_time, mfa, policy_version, action_digest, patch_sha256, target_service_id,
                                target_environment_id, valid_until, signature, signing_key_id)
  VALUES (:apv, :req, :act, 'approved', 'user:priya', 'approver', now(), true, 'p@1', :h_d, :h_c, :svc, :env,
          now() + interval '15 minutes', '\x01', 'k1');
UPDATE approval_requests SET closed_at = now(), close_reason = 'decided' WHERE approval_request_id = :req;
UPDATE actions SET status = 'approved', version = version + 1 WHERE action_id = :act;
UPDATE incidents SET status = 'executing', version = version + 1 WHERE incident_id = :inc;

-- The dispatcher moves the action to applying, records intent, then the outcome (ADR-51).
SET ROLE aisre_dispatcher;
UPDATE actions SET status = 'applying', version = version + 1 WHERE action_id = :act;
INSERT INTO execution_attempts (attempt_id, action_id, approval_id, action_digest, execution_key, attempt_no)
  VALUES (:att1, :act, :apv, :h_d, :act, 1);
INSERT INTO timeline_events (event_id, incident_id, type, source, actor, occurred_at, correlation_id, data)
  VALUES (gen_random_uuid(), :inc, 'aisre.execution.started.v1', '/aisre/dispatcher', 'system:dispatcher', now(), :inc, '{}');
UPDATE execution_attempts SET outcome = 'not_applied', finished_at = now() WHERE attempt_id = :att1;
-- While the action is still applying, a confirmed not_applied attempt may be retried with
-- the same key (ADR-47).
INSERT INTO execution_attempts (attempt_id, action_id, approval_id, action_digest, execution_key, attempt_no)
  VALUES (:att2, :act, :apv, :h_d, :act, 2);
UPDATE execution_attempts SET outcome = 'applied', finished_at = now(), external_ref = 'rel-9' WHERE attempt_id = :att2;
UPDATE actions SET status = 'executed', version = version + 1 WHERE action_id = :act;
RESET ROLE;

SELECT pg_temp.must_equal('B2 legal flow reached applying, 2 attempts, one applied',
  $$SELECT count(*) || '/' || count(*) FILTER (WHERE outcome = 'applied') FROM execution_attempts$$, '2/1');

SET ROLE aisre_app;
UPDATE incidents SET status = 'verifying', version = version + 1 WHERE incident_id = :inc;
INSERT INTO verifications (verification_id, action_id, attempt_id, criteria, hold_seconds, window_start, window_end,
                           measurements, result, verified_by)
  VALUES (gen_random_uuid(), :act, :att2, '[]', 600, now(), now(), '{}', 'verified', 'recovery-verifier');
UPDATE verifications SET accepted_by = 'user:lead', accepted_at = now() WHERE action_id = :act;
UPDATE actions SET status = 'verified', version = version + 1 WHERE action_id = :act;
UPDATE incidents SET status = 'recovered', version = version + 1 WHERE incident_id = :inc;
UPDATE incidents SET status = 'closed', version = version + 1, closed_at = now() WHERE incident_id = :inc;
RESET ROLE;

SELECT pg_temp.must_equal('B3 end state closed and verified',
  $$SELECT i.status || '/' || a.status FROM incidents i JOIN actions a USING (incident_id)$$, 'closed/verified');
SELECT pg_temp.must_equal('B4 an org-derived CloudEvent reads back',
  $$SELECT orgid::text || '/' || sequence FROM timeline_cloudevents WHERE sequence = 1$$,
  '00000000-0000-7000-8000-000000000001/1');

-- ---------------------------------------------------------------------------
-- C. State transitions: the database rejects illegal ones
-- ---------------------------------------------------------------------------
-- A second incident for the negative tests.
SET ROLE aisre_app;
INSERT INTO incidents (incident_id, org_id, display_ref, service_id, environment_id, dedup_key, title, source, started_at, created_by,
                       confirmed_severity, severity_confirmed_by)
  VALUES (:inc2, :org, 'INC-2026-000002', :svc, :env, 'checkout:latency', 'Slow', 'engineer', now(), 'user:sam', 'sev3', 'user:sam');
RESET ROLE;

SELECT pg_temp.must_fail('C1 illegal incident open -> executing', format(
  $$UPDATE incidents SET status = 'executing', version = version + 1 WHERE incident_id = %L$$, :inc2),
  'illegal incident transition: open -> executing');
SELECT pg_temp.must_pass('C3 legal incident open -> investigating', format(
  $$SET LOCAL ROLE aisre_app; UPDATE incidents SET status = 'investigating', version = version + 1 WHERE incident_id = %L$$, :inc2));
SELECT pg_temp.must_fail('C2 ADR-46 removed diagnosed -> awaiting_approval', format(
  $$UPDATE incidents SET status = 'diagnosed', version = version + 1 WHERE incident_id = %1$L;
    UPDATE incidents SET status = 'awaiting_approval', version = version + 1 WHERE incident_id = %1$L$$, :inc2),
  'illegal incident transition: diagnosed -> awaiting_approval');
SELECT pg_temp.must_fail('C4 version must rise write is refused', format(
  $$UPDATE incidents SET status = 'diagnosed', version = version WHERE incident_id = %L$$, :inc2), 'version must rise');
SELECT pg_temp.must_fail('C5 incident cannot start past open', format(
  $$INSERT INTO incidents (incident_id, org_id, display_ref, service_id, environment_id, dedup_key, title, source, started_at, created_by, status)
    VALUES (gen_random_uuid(), %L, 'INC-X', %L, %L, 'x', 't', 'engineer', now(), 'u', 'closed')$$, :org, :svc, :env), 'an incident starts as open');
SELECT pg_temp.must_fail('C6 terminal closed -> investigating', format(
  $$UPDATE incidents SET status = 'investigating', version = version + 1, closed_at = NULL WHERE incident_id = %L$$, :inc), 'illegal incident transition');
SELECT pg_temp.must_fail('C7 action verified -> applying', format(
  $$UPDATE actions SET status = 'applying', version = version + 1 WHERE action_id = %L$$, :act), 'illegal action transition');
SELECT pg_temp.must_fail('C8 action fields are frozen', format(
  $$UPDATE actions SET description = 'changed', version = version + 1 WHERE action_id = %L$$, :act), 'frozen except');
SELECT pg_temp.must_fail('C9 action_digest is write-once', format(
  $$UPDATE actions SET action_digest = %L, version = version + 1 WHERE action_id = %L$$, :h_a, :act), 'write-once');
SELECT pg_temp.must_pass('C10 re-investigation: handed_over -> investigating raises the run', format(
  $$SET LOCAL ROLE aisre_app;
    UPDATE incidents SET status = 'handed_over', version = version + 1 WHERE incident_id = %1$L;
    UPDATE incidents SET status = 'investigating', version = version + 1 WHERE incident_id = %1$L$$, :inc2));
SELECT pg_temp.must_equal('C11 second run recorded',
  format('SELECT investigation_run FROM incidents WHERE incident_id = %L', :inc2), '2');
SELECT pg_temp.must_pass('C12 outbox accepts workflow incident-<id>-r2', format(
  $$SET LOCAL ROLE aisre_app; INSERT INTO outbox (message_id, incident_id, kind, workflow_id, payload)
    VALUES (gen_random_uuid(), %1$L, 'start_workflow', 'incident-' || %1$L || '-r2', '{}')$$, :inc2));

-- ---------------------------------------------------------------------------
-- D. Append-only tables
-- ---------------------------------------------------------------------------
SELECT pg_temp.must_fail('D1 UPDATE on timeline_events', $$UPDATE timeline_events SET actor = 'x'$$, 'append-only');
SELECT pg_temp.must_fail('D2 DELETE on timeline_events', $$DELETE FROM timeline_events$$, 'append-only');
SELECT pg_temp.must_fail('D3 TRUNCATE on timeline_events', $$TRUNCATE timeline_events CASCADE$$, 'append-only');
SELECT pg_temp.must_fail('D4 UPDATE on approval_decisions', $$UPDATE approval_decisions SET reason = 'x'$$, 'append-only');
SELECT pg_temp.must_fail('D5 DELETE on approval_decisions', $$DELETE FROM approval_decisions$$, 'append-only');
SELECT pg_temp.must_fail('D6 UPDATE on evidence', $$UPDATE evidence SET summary = 'x'$$, 'append-only');
SELECT pg_temp.must_fail('D7 DELETE on evidence', $$DELETE FROM evidence$$, 'append-only');
SELECT pg_temp.must_fail('D8 UPDATE on patches', $$UPDATE patches SET review_ref = 'x'$$, 'append-only');
SELECT pg_temp.must_fail('D9 DELETE on patches', $$DELETE FROM patches$$, 'append-only');
SELECT pg_temp.must_fail('D10 UPDATE on test_runs', $$UPDATE test_runs SET reproduced = false$$, 'append-only');
SELECT pg_temp.must_fail('D11 DELETE on test_runs', $$DELETE FROM test_runs$$, 'append-only');
SELECT pg_temp.must_fail('D12 UPDATE on model_attempts', $$UPDATE model_attempts SET model = 'x'$$, 'append-only');
SELECT pg_temp.must_fail('D13 DELETE on model_attempts', $$DELETE FROM model_attempts$$, 'append-only');
SELECT pg_temp.must_fail('D14 UPDATE on artifacts', $$UPDATE artifacts SET redacted = false$$, 'append-only');
SELECT pg_temp.must_fail('D15 DELETE on artifacts', $$DELETE FROM artifacts$$, 'append-only');
SELECT pg_temp.must_fail('D16 verifications result frozen', $$UPDATE verifications SET result = 'failed'$$, 'frozen except');
SELECT pg_temp.must_fail('D17 DELETE on verifications', $$DELETE FROM verifications$$, 'append-only');
SELECT pg_temp.must_fail('D18 acceptance is write-once', $$UPDATE verifications SET accepted_by = 'user:other'$$, 'write-once');
SELECT pg_temp.must_fail('D19 app role has no DELETE', $$SET LOCAL ROLE aisre_app; DELETE FROM timeline_events$$, 'permission denied');
SELECT pg_temp.must_fail('D20 app role has no DELETE on incidents', $$SET LOCAL ROLE aisre_app; DELETE FROM incidents$$, 'permission denied');

-- ---------------------------------------------------------------------------
-- E. Execution: one applied, one open, once then frozen, identity rules
-- ---------------------------------------------------------------------------
SELECT pg_temp.must_fail('E1 a further attempt after applied is refused', format(
  $$SET LOCAL ROLE aisre_dispatcher;
    INSERT INTO execution_attempts (attempt_id, action_id, approval_id, action_digest, execution_key, attempt_no)
    VALUES (gen_random_uuid(), %1$L, %2$L, %3$L, %1$L, 3)$$, :act, :apv, :h_d), 'action is verified');
-- The index alone, with triggers bypassed. Replica mode needs a superuser.
SELECT pg_temp.must_fail('E2 second applied for a key fails on the partial unique index', format(
  $$SET LOCAL session_replication_role = replica;
    INSERT INTO execution_attempts (attempt_id, action_id, approval_id, action_digest, execution_key, attempt_no, outcome, finished_at)
    VALUES (gen_random_uuid(), %1$L, %2$L, %3$L, %1$L, 3, 'applied', now())$$, :act, :apv, :h_d), 'uq_execution_applied');
SELECT pg_temp.must_fail('E3 an attempt changes once then freezes', format(
  $$UPDATE execution_attempts SET outcome = 'failed' WHERE attempt_id = %L$$, :att2), 'is final');
SELECT pg_temp.must_fail('E4 DELETE on execution_attempts', $$DELETE FROM execution_attempts$$, 'append-only');
SELECT pg_temp.must_fail('E5 app role can never write attempts', format(
  $$SET LOCAL ROLE aisre_app; UPDATE execution_attempts SET error = 'x' WHERE attempt_id = %L$$, :att2), 'permission denied');
SELECT pg_temp.must_fail('E6 execution key must equal the action ID', format(
  $$SET LOCAL session_replication_role = replica;
    INSERT INTO execution_attempts (attempt_id, action_id, approval_id, action_digest, execution_key, attempt_no)
    VALUES (gen_random_uuid(), %L, %L, %L, 'exec:legacy', 1)$$, :act, :apv, :h_d), 'execution_attempts_check');
SELECT pg_temp.must_fail('E7 dispatcher writes only its own events',
  pg_temp.ev(:inc2, 'aisre.incident.closed.v1', 'aisre_dispatcher'), 'only aisre.execution');
SELECT pg_temp.must_fail('E8 dispatcher has no update right on other action columns', format(
  $$SET LOCAL ROLE aisre_dispatcher; UPDATE actions SET description = 'x', version = version + 1 WHERE action_id = %L$$, :act), 'permission denied');
SELECT pg_temp.must_fail('E10 dispatcher cannot touch a verified action''s status', format(
  $$SET LOCAL ROLE aisre_dispatcher; UPDATE actions SET status = 'undone', version = version + 1 WHERE action_id = %L$$, :act), 'permission denied');
SELECT pg_temp.must_pass('E11 dispatcher reads verification results',
  $$SET LOCAL ROLE aisre_dispatcher; SELECT count(*) FROM verifications$$);

-- A third action, for open-attempt, unknown-outcome, revocation and rollback paths.
SET ROLE aisre_app;
INSERT INTO diagnoses (diagnosis_id, incident_id, revision, outcome, category, confidence, summary, produced_by)
  VALUES ('00000000-0000-7000-8000-000000000402', :inc2, 1, 'inconclusive', 'unknown', 'low', 'Unclear', 'diagnosis-engine');
INSERT INTO hypotheses (hypothesis_id, incident_id, ordinal, statement, category, confirm_test, refute_test, confidence)
  VALUES ('00000000-0000-7000-8000-000000000303', :inc2, 1, 'Config', 'deployment_config', 'a', 'b', 'medium');
INSERT INTO diagnoses (diagnosis_id, incident_id, revision, outcome, leading_hypothesis_id, category, confidence, summary, produced_by)
  VALUES ('00000000-0000-7000-8000-000000000403', :inc2, 2, 'established', '00000000-0000-7000-8000-000000000303',
          'deployment_config', 'high', 'Config', 'diagnosis-engine');
INSERT INTO actions (action_id, incident_id, diagnosis_id, kind, target_service_id, target_environment_id, target_deployment,
                     base_revision, expected_after_revision, description, expected_effect, risk, undo_plan, recovery_criteria,
                     recovery_predicates_digest, high_impact, policy_version)
  VALUES (:act3, :inc2, '00000000-0000-7000-8000-000000000403', 'config_revert', :svc, :env, 'checkout-prod-us1', 'r7', 'r8',
          'd', 'e', 'r', 'u', '[]', :h_b, true, 'p@1');
UPDATE actions SET status = 'testing', version = version + 1 WHERE action_id = :act3;
UPDATE actions SET status = 'awaiting_approval', version = version + 1, action_digest = :h_a, test_bundle_digest = :h_c WHERE action_id = :act3;
RESET ROLE;

SELECT pg_temp.must_fail('E9 high-impact request needs two approvers', format(
  $$INSERT INTO approval_requests (approval_request_id, action_id, requested_by, escalate_at, expires_at, approvers_needed)
    VALUES (gen_random_uuid(), %L, 'system:workflow', now(), now(), 1)$$, :act3), 'two approvers');
-- The flag is false on each of these: the database derives high impact, as action.is_high_impact does.
CREATE FUNCTION pg_temp.one_approver(kind text, inc text, svc text, env text, h text) RETURNS text
LANGUAGE sql AS $$
  SELECT format($f$INSERT INTO actions (action_id, incident_id, diagnosis_id, kind, target_service_id, target_environment_id, target_deployment,
      base_revision, expected_after_revision, description, expected_effect, risk, undo_plan, recovery_criteria, recovery_predicates_digest, high_impact, policy_version)
    VALUES ('00000000-0000-7000-8000-000000000299', %L, '00000000-0000-7000-8000-000000000403', %L, %L, %L, 'd', 'r', 's', 'd', 'e', 'r', 'u', '[]', %L, false, 'p@1');
    INSERT INTO approval_requests (approval_request_id, action_id, requested_by, escalate_at, expires_at, approvers_needed)
    VALUES (gen_random_uuid(), '00000000-0000-7000-8000-000000000299', 'system:workflow', now(), now(), 1)$f$, inc, kind, svc, env, h)
$$;
SELECT pg_temp.must_fail('E12 a release rollback needs two approvers whatever the flag says',
  pg_temp.one_approver('release_rollback', :inc2, :svc, :env, :h_b), 'two approvers');
SELECT pg_temp.must_fail('E13 severity not confirmed needs two approvers', format(
  $$UPDATE incidents SET confirmed_severity = NULL, severity_confirmed_by = NULL, version = version + 1 WHERE incident_id = %L; %s$$,
  :inc2, pg_temp.one_approver('config_revert', :inc2, :svc, :env, :h_b)), 'two approvers');
SELECT pg_temp.must_fail('E14 a critical service needs two approvers', format(
  $$UPDATE services SET critical = true WHERE service_id = %L; %s$$,
  :svc, pg_temp.one_approver('config_revert', :inc2, :svc, :env, :h_b)), 'two approvers');
-- Reaching the division proves both inserts passed, and the error rolls them back.
SELECT pg_temp.must_fail('E15 an ordinary change on a confirmed incident takes one approver', format(
  $$%s; SELECT 1 / 0$$, pg_temp.one_approver('config_revert', :inc2, :svc, :env, :h_b)), 'division by zero');
INSERT INTO approval_requests (approval_request_id, action_id, requested_by, escalate_at, expires_at, approvers_needed)
  VALUES (:req3, :act3, 'user:sam', now() + interval '15 minutes', now() + interval '60 minutes', 2);

-- ---------------------------------------------------------------------------
-- F. Approvals
-- ---------------------------------------------------------------------------
SELECT pg_temp.must_fail('F1 second open request for an action', format(
  $$INSERT INTO approval_requests (approval_request_id, action_id, requested_by, escalate_at, expires_at, approvers_needed)
    VALUES (gen_random_uuid(), %L, 'system:workflow', now(), now(), 2)$$, :act3), 'uq_approval_requests_open');

CREATE FUNCTION pg_temp.decide(id text, req text, act text, who text, what text, digest text, extra_cols text DEFAULT '', extra_vals text DEFAULT '') RETURNS text
LANGUAGE sql AS $$
  SELECT format($f$INSERT INTO approval_decisions (approval_id, approval_request_id, action_id, decision, decided_by, decided_by_role,
      auth_time, mfa, policy_version, action_digest, target_service_id, target_environment_id, reason, valid_until, signature, signing_key_id%s)
    VALUES (%L, %L, %L, %L, %L, 'approver', now(), true, 'p@1', %L, '00000000-0000-7000-8000-000000000002', '00000000-0000-7000-8000-000000000003',
      %s, %s, '\x01', 'k1'%s)$f$,
    extra_cols, id, req, act, what, who, digest,
    CASE WHEN what = 'approved' THEN 'NULL' ELSE '''reason text''' END,
    CASE WHEN what = 'approved' THEN 'now() + interval ''15 minutes''' ELSE 'NULL' END, extra_vals)
$$;

SELECT pg_temp.must_fail('F2 wrong digest', pg_temp.decide(gen_random_uuid()::text, :req3, :act3, 'user:a', 'approved', :h_b), 'action_digest does not match');
SELECT pg_temp.must_fail('F3 self-approval', pg_temp.decide(gen_random_uuid()::text, :req3, :act3, 'user:sam', 'approved', :h_a), 'own request');
SELECT pg_temp.must_fail('F4 the platform never approves', pg_temp.decide(gen_random_uuid()::text, :req3, :act3, 'system:workflow', 'approved', :h_a), 'approval_decisions_check');
SELECT pg_temp.must_fail('F5 stale sign-in', replace(pg_temp.decide(gen_random_uuid()::text, :req3, :act3, 'user:a', 'approved', :h_a),
  'now(), true', 'now() - interval ''16 minutes'', true'), 'approval_decisions_check');
SELECT pg_temp.must_fail('F6 approval valid for over 15 minutes', replace(pg_temp.decide(gen_random_uuid()::text, :req3, :act3, 'user:a', 'approved', :h_a),
  'interval ''15 minutes''', 'interval ''16 minutes'''), 'approval_decisions_check');
SELECT pg_temp.must_pass('F7 first approver', pg_temp.decide(:apv3, :req3, :act3, 'user:a', 'approved', :h_a));
SELECT pg_temp.must_fail('F8 same person approves twice', pg_temp.decide(gen_random_uuid()::text, :req3, :act3, 'user:a', 'approved', :h_a), 'duplicate key');
SELECT pg_temp.must_fail('F9 revoke someone else''s approval', pg_temp.decide(gen_random_uuid()::text, :req3, :act3, 'user:b', 'revoked', :h_a,
  ', revokes_approval_id', format(', %L', :apv3)), 'own approval');
SELECT pg_temp.must_pass('F10 second approver', pg_temp.decide(:apv4, :req3, :act3, 'user:b', 'approved', :h_a));
UPDATE approval_requests SET closed_at = now(), close_reason = 'decided' WHERE approval_request_id = :req3;
SELECT pg_temp.must_fail('F11 a closed request stays closed', format(
  $$UPDATE approval_requests SET close_reason = 'voided' WHERE approval_request_id = %L$$, :req3), 'write-once');
UPDATE actions SET status = 'approved', version = version + 1 WHERE action_id = :act3;
SELECT pg_temp.must_fail('F15 a withdrawn action cannot move to applying', format(
  $$UPDATE actions SET status = 'withdrawn', version = version + 1 WHERE action_id = %1$L;
    UPDATE actions SET status = 'applying', version = version + 1 WHERE action_id = %1$L$$, :act3), 'illegal action transition');
SELECT pg_temp.must_fail('F16 a withdrawn action takes no attempt', format(
  $$UPDATE actions SET status = 'withdrawn', version = version + 1 WHERE action_id = %1$L;
    SET LOCAL ROLE aisre_dispatcher;
    INSERT INTO execution_attempts (attempt_id, action_id, approval_id, action_digest, execution_key, attempt_no)
    VALUES (gen_random_uuid(), %1$L, %2$L, %3$L, %1$L, 1)$$, :act3, :apv3, :h_a), 'action is withdrawn, not applying');
SELECT pg_temp.must_fail('F13 dispatcher cannot withdraw an approved action', format(
  $$SET LOCAL ROLE aisre_dispatcher; UPDATE actions SET status = 'withdrawn', version = version + 1 WHERE action_id = %L$$, :act3), 'permission denied');
SELECT pg_temp.must_pass('F14 dispatcher moves approved -> applying', format(
  $$SET LOCAL ROLE aisre_dispatcher; UPDATE actions SET status = 'applying', version = version + 1 WHERE action_id = %L$$, :act3));

-- Revocation: one approver revokes, so the standing approval falls below two, and the attempt is refused.
SELECT pg_temp.must_fail('F12 attempt with a revoked second approval', format(
  $$%s; SET LOCAL ROLE aisre_dispatcher;
    INSERT INTO execution_attempts (attempt_id, action_id, approval_id, action_digest, execution_key, attempt_no)
    VALUES (gen_random_uuid(), %L, %L, %L, %L, 1)$$,
    pg_temp.decide(gen_random_uuid()::text, :req3, :act3, 'user:b', 'revoked', :h_a, ', revokes_approval_id', format(', %L', :apv4)),
    :act3, :apv3, :h_a, :act3), 'approval no longer stands');

-- ---------------------------------------------------------------------------
-- G. Unknown outcome and the one-open-attempt rule
-- ---------------------------------------------------------------------------
SET ROLE aisre_dispatcher;
INSERT INTO execution_attempts (attempt_id, action_id, approval_id, action_digest, execution_key, attempt_no)
  VALUES (:att3, :act3, :apv3, :h_a, :act3, 1);
RESET ROLE;
SELECT pg_temp.must_fail('G1 second open attempt refused by the guard', format(
  $$INSERT INTO execution_attempts (attempt_id, action_id, approval_id, action_digest, execution_key, attempt_no)
    VALUES (gen_random_uuid(), %1$L, %2$L, %3$L, %1$L, 2)$$, :act3, :apv3, :h_a), 'retry refused');
SELECT pg_temp.must_fail('G2 second open attempt fails on the partial unique index', format(
  $$SET LOCAL session_replication_role = replica;
    INSERT INTO execution_attempts (attempt_id, action_id, approval_id, action_digest, execution_key, attempt_no)
    VALUES (gen_random_uuid(), %1$L, %2$L, %3$L, %1$L, 2)$$, :act3, :apv3, :h_a), 'uq_execution_open');
UPDATE execution_attempts SET outcome = 'unknown', finished_at = now() WHERE attempt_id = :att3;
SELECT pg_temp.must_fail('G3 no retry after unknown without reconciliation', format(
  $$INSERT INTO execution_attempts (attempt_id, action_id, approval_id, action_digest, execution_key, attempt_no)
    VALUES (gen_random_uuid(), %1$L, %2$L, %3$L, %1$L, 2)$$, :act3, :apv3, :h_a), 'retry refused');
SELECT pg_temp.must_pass('G9 dispatcher moves applying -> outcome_unknown', format(
  $$SET LOCAL ROLE aisre_dispatcher; UPDATE actions SET status = 'outcome_unknown', version = version + 1 WHERE action_id = %L$$, :act3));

-- A different action on the same deployment is blocked while the outcome is unknown (ADR-47).
SET ROLE aisre_app;
INSERT INTO actions (action_id, incident_id, diagnosis_id, kind, target_service_id, target_environment_id, target_deployment,
                     base_revision, expected_after_revision, description, expected_effect, risk, undo_plan, recovery_criteria,
                     recovery_predicates_digest, high_impact, policy_version)
  VALUES (:act4, :inc2, '00000000-0000-7000-8000-000000000403', 'config_revert', :svc, :env, 'checkout-prod-us1', 'r7', 'r9',
          'd', 'e', 'r', 'u', '[]', :h_b, false, 'p@1');
UPDATE actions SET status = 'testing', version = version + 1 WHERE action_id = :act4;
UPDATE actions SET status = 'awaiting_approval', version = version + 1, action_digest = :h_b, test_bundle_digest = :h_c WHERE action_id = :act4;
INSERT INTO approval_requests (approval_request_id, action_id, requested_by, escalate_at, expires_at)
  VALUES (:req4, :act4, 'user:sam', now() + interval '15 minutes', now() + interval '60 minutes');
RESET ROLE;
SELECT pg_temp.must_pass('G4 approval on the other action', pg_temp.decide(:apv2, :req4, :act4, 'user:a', 'approved', :h_b));
UPDATE actions SET status = 'approved', version = version + 1 WHERE action_id = :act4;
UPDATE actions SET status = 'applying', version = version + 1 WHERE action_id = :act4;
SELECT pg_temp.must_fail('G5 unknown outcome blocks the whole deployment', format(
  $$SET LOCAL ROLE aisre_dispatcher;
    INSERT INTO execution_attempts (attempt_id, action_id, approval_id, action_digest, execution_key, attempt_no)
    VALUES (gen_random_uuid(), %1$L, %2$L, %3$L, %1$L, 1)$$, :act4, :apv2, :h_b), 'is blocked');
SELECT pg_temp.must_pass('G6 reconciliation records the looked-up result', format(
  $$SET LOCAL ROLE aisre_dispatcher;
    INSERT INTO execution_attempts (attempt_id, action_id, approval_id, action_digest, execution_key, attempt_no, reconciliation, outcome)
    VALUES (gen_random_uuid(), %1$L, %2$L, %3$L, %1$L, 2, true, 'applied')$$, :act3, :apv3, :h_a));
SELECT pg_temp.must_pass('G10 dispatcher settles outcome_unknown -> executed', format(
  $$SET LOCAL ROLE aisre_dispatcher; UPDATE actions SET status = 'executed', version = version + 1 WHERE action_id = %L$$, :act3));
SELECT pg_temp.must_fail('G11 verdict states stay out of the dispatcher''s reach', format(
  $$SET LOCAL ROLE aisre_dispatcher; UPDATE actions SET status = 'verified', version = version + 1 WHERE action_id = %L$$, :act3), 'permission denied');
SELECT pg_temp.must_pass('G7 deployment unblocked once settled', format(
  $$SET LOCAL ROLE aisre_dispatcher;
    INSERT INTO execution_attempts (attempt_id, action_id, approval_id, action_digest, execution_key, attempt_no)
    VALUES (gen_random_uuid(), %1$L, %2$L, %3$L, %1$L, 1)$$, :act4, :apv2, :h_b));
SELECT pg_temp.must_fail('G8 revocation too late once intent exists', pg_temp.decide(gen_random_uuid()::text, :req4, :act4, 'user:a', 'revoked', :h_b,
  ', revokes_approval_id', format(', %L', :apv2)), 'recorded intent');
-- not_applied on the action is final: any further try is a new action (ADR-47).
SELECT pg_temp.must_pass('G12 dispatcher records not_applied on attempt and action', format(
  $$SET LOCAL ROLE aisre_dispatcher;
    UPDATE execution_attempts SET outcome = 'not_applied', finished_at = now() WHERE action_id = %1$L AND outcome = 'started';
    UPDATE actions SET status = 'not_applied', version = version + 1 WHERE action_id = %1$L$$, :act4));
SELECT pg_temp.must_fail('G13 no further attempt once the action is not_applied', format(
  $$SET LOCAL ROLE aisre_dispatcher;
    INSERT INTO execution_attempts (attempt_id, action_id, approval_id, action_digest, execution_key, attempt_no)
    VALUES (gen_random_uuid(), %1$L, %2$L, %3$L, %1$L, 2)$$, :act4, :apv2, :h_b), 'action is not_applied');

-- ---------------------------------------------------------------------------
-- H. Rollback (ADR-48)
-- ---------------------------------------------------------------------------
SELECT pg_temp.must_fail('H1 a non-rollback cannot skip testing', format(
  $$INSERT INTO actions (action_id, incident_id, diagnosis_id, kind, target_service_id, target_environment_id, target_deployment,
     base_revision, expected_after_revision, description, expected_effect, risk, undo_plan, recovery_criteria, recovery_predicates_digest, high_impact, policy_version)
   VALUES (%1$L, %2$L, %3$L, 'commit_revert', %4$L, %5$L, 'd', 'r', 'r2', 'd', 'e', 'r', 'u', '[]', %6$L, false, 'p@1');
   UPDATE actions SET status = 'awaiting_approval', version = version + 1, action_digest = %6$L, test_bundle_digest = %6$L WHERE action_id = %1$L$$,
   gen_random_uuid(), :inc2, '00000000-0000-7000-8000-000000000403', :svc, :env, :h_a), 'only rollback actions');
SELECT pg_temp.must_fail('H2 a rollback must copy its parent''s digests', format(
  $$INSERT INTO actions (action_id, incident_id, diagnosis_id, parent_action_id, kind, target_service_id, target_environment_id, target_deployment,
     base_revision, expected_after_revision, description, expected_effect, risk, undo_plan, recovery_criteria, recovery_predicates_digest, high_impact, policy_version,
     action_digest, test_bundle_digest)
   VALUES (gen_random_uuid(), %L, %L, %L, 'rollback', %L, %L, 'checkout-prod-us1', 'r8', 'r7', 'd', 'e', 'r', 'u', '[]', %L, false, 'p@1', %L, %L)$$,
   :inc2, '00000000-0000-7000-8000-000000000403', :act3, :svc, :env, :h_b, :h_d, :h_d), 'copies its parent');
-- The order ADR-48 and 11.13 give: the rollback stays proposed while its conditional approval
-- is signed with the fix, and leaves proposed only on a failed verdict.
SELECT pg_temp.must_pass('H3 a proposed rollback takes a request row', format(
  $$INSERT INTO actions (action_id, incident_id, diagnosis_id, parent_action_id, kind, target_service_id, target_environment_id, target_deployment,
     base_revision, expected_after_revision, description, expected_effect, risk, undo_plan, recovery_criteria, recovery_predicates_digest, high_impact, policy_version,
     action_digest, test_bundle_digest)
   VALUES (%1$L, %2$L, %3$L, %4$L, 'rollback', %5$L, %6$L, 'checkout-prod-us1', 'r8', 'r7', 'd', 'e', 'r', 'u', '[]', %7$L, false, 'p@1', %8$L, %9$L);
   INSERT INTO approval_requests (approval_request_id, action_id, requested_by, escalate_at, expires_at)
     VALUES (%10$L, %1$L, 'system:workflow', now(), now() + interval '60 minutes')$$,
   :rbk, :inc2, '00000000-0000-7000-8000-000000000403', :act3, :svc, :env, :h_b, :h_d, :h_c, :req2));
SELECT pg_temp.must_equal('H3a the rollback is still proposed',
  format('SELECT status FROM actions WHERE action_id = %L', :rbk), 'proposed');
SELECT pg_temp.must_fail('H3b a plain approval on a proposed rollback is refused',
  pg_temp.decide(gen_random_uuid()::text, :req2, :rbk, 'user:a', 'approved', :h_d), 'request is not open');
SELECT pg_temp.must_fail('H3c the exemption is for rollbacks only', format(
  $$INSERT INTO actions (action_id, incident_id, diagnosis_id, kind, target_service_id, target_environment_id, target_deployment,
     base_revision, expected_after_revision, description, expected_effect, risk, undo_plan, recovery_criteria, recovery_predicates_digest, high_impact, policy_version, action_digest)
   VALUES (%1$L, %2$L, %3$L, 'commit_revert', %4$L, %5$L, 'd', 'r', 'r2', 'd', 'e', 'r', 'u', '[]', %6$L, false, 'p@1', %6$L);
   INSERT INTO approval_requests (approval_request_id, action_id, requested_by, escalate_at, expires_at)
     VALUES (%7$L, %1$L, 'system:workflow', now(), now() + interval '60 minutes');
   %8$s$$,
   :act5, :inc2, '00000000-0000-7000-8000-000000000403', :svc, :env, :h_a, :req5,
   pg_temp.decide(gen_random_uuid()::text, :req5, :act5, 'user:a', 'approved', :h_a,
     ', conditional, conditional_on_approval_id', format(', true, %L', :apv3))), 'rollback of the fix');
SELECT pg_temp.must_fail('H4 conditional approval with the wrong lifetime', replace(pg_temp.decide(gen_random_uuid()::text, :req2, :rbk, 'user:a', 'approved', :h_d,
  ', conditional, conditional_on_approval_id', format(', true, %L', :apv3)), 'interval ''15 minutes''', 'interval ''20 minutes'''), 'hold + 15 minutes');
SELECT pg_temp.must_pass('H5 conditional approval on the proposed rollback: parent valid_until + hold + 15 minutes', replace(pg_temp.decide(:apv5, :req2, :rbk, 'user:a', 'approved', :h_d,
  ', conditional, conditional_on_approval_id', format(', true, %L', :apv3)), 'now() + interval ''15 minutes''',
  format('(SELECT valid_until FROM approval_decisions WHERE approval_id = %L) + interval ''25 minutes''', :apv3)));
SELECT pg_temp.must_pass('H6 the verdict moves the rollback on, and its approval stands', format(
  $$UPDATE approval_requests SET closed_at = now(), close_reason = 'decided' WHERE approval_request_id = %3$L;
    UPDATE actions SET status = 'awaiting_approval', version = version + 1 WHERE action_id = %1$L;
    UPDATE actions SET status = 'approved', version = version + 1 WHERE action_id = %1$L;
    UPDATE actions SET status = 'applying', version = version + 1 WHERE action_id = %1$L;
    SET LOCAL ROLE aisre_dispatcher;
    INSERT INTO execution_attempts (attempt_id, action_id, approval_id, action_digest, execution_key, attempt_no)
    VALUES (gen_random_uuid(), %1$L, %2$L, %4$L, %1$L, 1);
    UPDATE execution_attempts SET outcome = 'applied', finished_at = now() WHERE action_id = %1$L;
    UPDATE actions SET status = 'executed', version = version + 1 WHERE action_id = %1$L$$, :rbk, :apv5, :req2, :h_d));
SELECT pg_temp.must_fail('H7 a closed request takes no conditional approval', replace(pg_temp.decide(gen_random_uuid()::text, :req2, :rbk, 'user:c', 'approved', :h_d,
  ', conditional, conditional_on_approval_id', format(', true, %L', :apv3)), 'now() + interval ''15 minutes''',
  format('(SELECT valid_until FROM approval_decisions WHERE approval_id = %L) + interval ''25 minutes''', :apv3)), 'request is not open');

-- ---------------------------------------------------------------------------
-- I. Deduplication, budget, timeline
-- ---------------------------------------------------------------------------
SELECT pg_temp.must_fail('I1 second open incident for a dedup key', format(
  $$INSERT INTO incidents (incident_id, org_id, display_ref, service_id, environment_id, dedup_key, title, source, started_at, created_by)
    VALUES (gen_random_uuid(), %L, 'INC-DUP', %L, %L, 'checkout:latency', 't', 'monitoring', now(), 'u')$$, :org, :svc, :env), 'uq_incidents_open_dedup');
SELECT pg_temp.must_pass('I2 a closed incident frees its dedup key', format(
  $$SET LOCAL ROLE aisre_app; INSERT INTO incidents (incident_id, org_id, display_ref, service_id, environment_id, dedup_key, title, source, started_at, created_by)
    VALUES ('00000000-0000-7000-8000-000000000103', %L, 'INC-2026-000003', %L, %L, 'checkout:http_5xx', 't', 'monitoring', now(), 'u')$$, :org, :svc, :env));

SELECT pg_temp.must_fail('I3 reservation over the cost cap', format(
  $$SET LOCAL ROLE aisre_app; INSERT INTO budget_reservations (reservation_id, incident_id, reserved_cost_micros, reserved_input_tokens, reserved_output_tokens, expires_at)
    VALUES (gen_random_uuid(), %L, 1920001, 1, 1, now())$$, :inc), 'incident_budgets_check');
SELECT pg_temp.must_pass('I4 reservation exactly at the cap', format(
  $$SET LOCAL ROLE aisre_app; INSERT INTO budget_reservations (reservation_id, incident_id, reserved_cost_micros, reserved_input_tokens, reserved_output_tokens, expires_at)
    VALUES ('00000000-0000-7000-8000-000000000802', %L, 1920000, 1, 1, now())$$, :inc));
SELECT pg_temp.must_fail('I5 no room left', format(
  $$INSERT INTO budget_reservations (reservation_id, incident_id, reserved_cost_micros, reserved_input_tokens, reserved_output_tokens, expires_at)
    VALUES (gen_random_uuid(), %L, 1, 1, 1, now())$$, :inc), 'incident_budgets_check');
SELECT pg_temp.must_pass('I6 releasing frees it again', format(
  $$SET LOCAL ROLE aisre_app; UPDATE budget_reservations SET status = 'released', settled_at = now() WHERE reservation_id = %L$$, '00000000-0000-7000-8000-000000000802'));
SELECT pg_temp.must_equal('I7 budget restored after release',
  format('SELECT used_calls || ''/'' || reserved_cost_micros || ''/'' || spent_cost_micros FROM incident_budgets WHERE incident_id = %L', :inc), '1/0/80000');
SELECT pg_temp.must_fail('I8 a settled reservation cannot change', format(
  $$UPDATE budget_reservations SET status = 'reserved', actual_cost_micros = NULL, settled_at = NULL WHERE reservation_id = %L$$, :res), 'illegal reservation transition');
SELECT pg_temp.must_fail('I9 token cap', format(
  $$INSERT INTO budget_reservations (reservation_id, incident_id, reserved_cost_micros, reserved_input_tokens, reserved_output_tokens, expires_at)
    VALUES (gen_random_uuid(), %L, 1, 199101, 1, now())$$, :inc), 'incident_budgets_check');
SELECT pg_temp.must_fail('I10 call cap', format(
  $$UPDATE incident_budgets SET used_calls = 31 WHERE incident_id = %L$$, :inc), 'incident_budgets_check');
SELECT pg_temp.must_fail('I11 direct budget edit is denied to the app', format(
  $$SET LOCAL ROLE aisre_app; UPDATE incident_budgets SET spent_cost_micros = 0 WHERE incident_id = %L$$, :inc), 'permission denied');

-- ADR-61: expiry tells sent from unsent by sent_at.
SELECT pg_temp.must_fail('I11a a reservation never sent cannot settle', format(
  $$INSERT INTO budget_reservations (reservation_id, incident_id, reserved_cost_micros, reserved_input_tokens, reserved_output_tokens, expires_at)
      VALUES (gen_random_uuid(), %1$L, 1, 1, 1, now());
    UPDATE budget_reservations SET status = 'settled', actual_cost_micros = 1, settled_at = now()
      WHERE incident_id = %1$L AND status = 'reserved'$$, :inc), 'budget_reservations_check');
SELECT pg_temp.must_pass('I11b sent_at is recorded and moves no money', format(
  $$SET LOCAL ROLE aisre_app;
    INSERT INTO budget_reservations (reservation_id, incident_id, reserved_cost_micros, reserved_input_tokens, reserved_output_tokens, expires_at)
      VALUES (%1$L, %2$L, 100000, 1000, 500, now());
    UPDATE budget_reservations SET sent_at = now() WHERE reservation_id = %1$L$$, :res2, :inc));
SELECT pg_temp.must_equal('I11c the hold is unchanged by sent_at',
  format('SELECT used_calls || ''/'' || reserved_cost_micros || ''/'' || spent_cost_micros FROM incident_budgets WHERE incident_id = %L', :inc), '2/100000/80000');
SELECT pg_temp.must_fail('I11d sent_at is write-once', format(
  $$UPDATE budget_reservations SET sent_at = now() + interval '1 second' WHERE reservation_id = %L$$, :res2), 'write-once');
SELECT pg_temp.must_fail('I11e a sent reservation is never released', format(
  $$SET LOCAL ROLE aisre_app; UPDATE budget_reservations SET status = 'released', settled_at = now() WHERE reservation_id = %L$$, :res2), 'budget_reservations_check');
SELECT pg_temp.must_pass('I11f a sent reservation expires settled at the maximum, usage unknown', format(
  $$SET LOCAL ROLE aisre_app; UPDATE budget_reservations SET status = 'settled', actual_cost_micros = reserved_cost_micros, settled_at = now()
    WHERE reservation_id = %L$$, :res2));
-- Reconciliation corrects through reservation_corrections; the settled row still refuses change.
SELECT pg_temp.must_fail('I11g the settled row itself still refuses a correction', format(
  $$SET LOCAL ROLE aisre_app; UPDATE budget_reservations SET actual_cost_micros = 60000 WHERE reservation_id = %L$$, :res2), 'illegal reservation transition');
CREATE FUNCTION pg_temp.correct(id text, inc text, cost bigint) RETURNS text
LANGUAGE sql AS $$
  SELECT format($f$SET LOCAL ROLE aisre_app; INSERT INTO reservation_corrections
    (reservation_id, incident_id, actual_cost_micros, actual_input_tokens, actual_output_tokens, corrected_by, reason)
    VALUES (%L, %L, %s, 700, 300, 'system:reconciliation', 'provider usage records')$f$, id, inc, cost)
$$;
SELECT pg_temp.must_fail('I11h usage already known cannot be corrected', pg_temp.correct(:res, :inc, 1), 'unknown usage');
SELECT pg_temp.must_fail('I11i a correction names the reservation''s own incident', pg_temp.correct(:res2, :inc2, 1), 'foreign key');
SELECT pg_temp.must_pass('I11j reconciliation corrects a settled reservation', pg_temp.correct(:res2, :inc, 60000));
SELECT pg_temp.must_equal('I11k the difference moved in the budget',
  format($$SELECT spent_cost_micros || '/' || used_input_tokens || '/' || used_output_tokens FROM incident_budgets WHERE incident_id = %L$$, :inc), '140000/1600/700');
SELECT pg_temp.must_fail('I11l a reservation is corrected once', pg_temp.correct(:res2, :inc, 50000), 'duplicate key');
SELECT pg_temp.must_fail('I11m corrections are append-only', $$UPDATE reservation_corrections SET reason = 'x'$$, 'append-only');
SELECT pg_temp.must_fail('I11n an open reservation cannot be corrected', format(
  $$INSERT INTO budget_reservations (reservation_id, incident_id, reserved_cost_micros, reserved_input_tokens, reserved_output_tokens, expires_at)
      VALUES (%1$L, %2$L, 1, 1, 1, now());
    %3$s$$, :res3, :inc, pg_temp.correct(:res3, :inc, 1)), 'unknown usage');

SELECT pg_temp.must_fail('I12 caller cannot pick a sequence number', format(
  $$INSERT INTO timeline_events (event_id, incident_id, seq, type, source, actor, occurred_at, correlation_id, data)
    VALUES (gen_random_uuid(), %1$L, 99, 'aisre.incident.note_added.v1', 's', 'a', now(), %1$L, '{}')$$, :inc2), 'next sequence is');
SELECT pg_temp.must_fail('I13 event type must match the CloudEvents pattern', pg_temp.ev(:inc2, 'bad.type'), 'timeline_events_type_check');
SELECT pg_temp.must_fail('I14 a rolled-back event consumes no number', pg_temp.ev(:inc2, 'aisre.incident.note_added.v1') || '; SELECT 1 / (SELECT count(*) FROM pg_class WHERE false)', 'division by zero');
SELECT pg_temp.must_pass('I15 event 1 on incident 2', pg_temp.ev(:inc2, 'aisre.approval.refused.v1'));
SELECT pg_temp.must_pass('I16 event 2 on incident 2', pg_temp.ev(:inc2, 'aisre.incident.note_added.v1'));
SELECT pg_temp.must_equal('I17 sequence stayed gapless',
  format('SELECT string_agg(seq::text, '','' ORDER BY seq) FROM timeline_events WHERE incident_id = %L', :inc2), '1,2');
SELECT pg_temp.must_equal('I18 hash chain intact on incident 1',
  format('SELECT count(*) FROM verify_timeline_chain(%L)', :inc), '0');
SELECT pg_temp.must_equal('I19 prev_hash links seq 2 to seq 1',
  format($$SELECT (SELECT hash FROM timeline_events WHERE incident_id = %1$L AND seq = 1) = (SELECT prev_hash FROM timeline_events WHERE incident_id = %1$L AND seq = 2)$$, :inc), 'true');

-- Tampering, with the trigger bypassed as an attacker with superuser would, is detected.
BEGIN;
SET LOCAL session_replication_role = replica;
UPDATE timeline_events SET actor = 'forged' WHERE incident_id = :inc AND seq = 1;
SET LOCAL session_replication_role = origin;
SELECT pg_temp.must_equal('I20 tampering breaks the chain',
  format('SELECT min(problem) FROM verify_timeline_chain(%L)', :inc), 'hash does not match the event');
COMMIT;
SELECT pg_temp.must_pass('I21 restore the forged row', format(
  $$SET LOCAL session_replication_role = replica; UPDATE timeline_events SET actor = 'system:intake' WHERE incident_id = %L AND seq = 1$$, :inc));

-- ---------------------------------------------------------------------------
-- J. Retention role and intake record
-- ---------------------------------------------------------------------------
SELECT pg_temp.must_fail('J1 retention cannot delete what has not expired', format(
  $$SET LOCAL ROLE aisre_retention; SELECT purge_incident(%L)$$, :inc), 'not due for purge');
SELECT pg_temp.must_fail('J2 retention row check holds without the function',
  $$SET LOCAL ROLE aisre_retention; DELETE FROM evidence$$, 'not yet due');
SELECT pg_temp.must_fail('J3 retention cannot update', $$SET LOCAL ROLE aisre_retention; UPDATE timeline_events SET actor = 'x'$$, 'permission denied');
SELECT pg_temp.must_fail('J4 retention cannot touch the catalogue', $$SET LOCAL ROLE aisre_retention; DELETE FROM services$$, 'permission denied');
SELECT pg_temp.must_pass('J5 dead-lettered alert stored raw', format(
  $$SET LOCAL ROLE aisre_app; INSERT INTO dead_letters (dead_letter_id, org_id, caller_id, reason, service_slug, raw_payload, retain_until)
    VALUES (gen_random_uuid(), %L, 'monitoring', 'unknown_service', 'ghost', '{"a": 1}', now() - interval '1 day')$$, :org));
SELECT pg_temp.must_fail('J6 dead letters are append-only', $$UPDATE dead_letters SET reason = 'x'$$, 'append-only');
SELECT pg_temp.must_pass('J7 retention removes an expired dead letter', $$SET LOCAL ROLE aisre_retention; DELETE FROM dead_letters$$);

-- Age incident 1 past the retention period. Replica mode bypasses the checks to back-date it.
SELECT pg_temp.must_pass('J9 back-date incident 1 for the purge test', format(
  $$SET LOCAL session_replication_role = replica;
    UPDATE incidents SET closed_at = now() - interval '2 years' WHERE incident_id = %1$L;
    UPDATE artifacts SET retain_until = now() - interval '1 day' WHERE incident_id = %1$L$$, :inc));
SELECT pg_temp.must_equal('J10 expired patch diff is due; the cited snapshot is held for a year',
  $$SELECT string_agg(storage_key, ',') FROM artifacts_due_for_deletion$$, 'obj/2');
SELECT pg_temp.must_pass('J11 retention records the object-store deletion', format(
  $$SET LOCAL ROLE aisre_retention; INSERT INTO artifact_deletions (artifact_id, incident_id) VALUES (%L, %L)$$, :art2, :inc));
SELECT pg_temp.must_pass('J12 purge removes the whole expired record', format(
  $$SET LOCAL ROLE aisre_retention; SELECT purge_incident(%L)$$, :inc));
SELECT pg_temp.must_equal('J13 nothing of incident 1 remains',
  format($$SELECT (SELECT count(*) FROM incidents WHERE incident_id = %1$L) + (SELECT count(*) FROM timeline_events WHERE incident_id = %1$L)
    + (SELECT count(*) FROM evidence WHERE incident_id = %1$L) + (SELECT count(*) FROM actions WHERE incident_id = %1$L)$$, :inc), '0');

-- ---------------------------------------------------------------------------
-- K. Investigation slots and the write fence (Artifacts 8 and 12)
-- ---------------------------------------------------------------------------
SELECT pg_temp.must_fail('K1 the fence epoch never goes down',
  'UPDATE fence_state SET epoch = 0', 'never goes down');
SELECT pg_temp.must_pass('K2 the fence epoch can rise',
  'UPDATE fence_state SET epoch = epoch + 1');
SELECT pg_temp.must_pass('K3 a fence reason can be set', $q$
  INSERT INTO fence_reasons (fence_id, scope, reason, why, set_by)
  VALUES ('01890000-0000-7000-8000-00000000f001', 'global', 'manual', 'Bad rollout in progress', 'lead') $q$);
SELECT pg_temp.must_fail('K4 the same active reason can not be set twice', $q$
  INSERT INTO fence_reasons (fence_id, scope, reason, why, set_by)
  VALUES ('01890000-0000-7000-8000-00000000f002', 'global', 'manual', 'Second reason', 'lead') $q$, 'fence_reasons_one_active');
SELECT pg_temp.must_fail('K5 a clear needs both who and when', $q$
  UPDATE fence_reasons SET cleared_at = now() WHERE reason = 'manual' $q$, 'fence_reasons_check');
SELECT pg_temp.must_fail('K6 the dispatcher can not change the fence', $q$
  SET LOCAL ROLE aisre_dispatcher; UPDATE fence_state SET epoch = epoch + 1 $q$, 'permission denied');
SELECT pg_temp.must_fail('K7 a manual fence records why', $q$
  INSERT INTO fence_reasons (fence_id, scope, reason, set_by)
  VALUES ('01890000-0000-7000-8000-00000000f003', 'eu1', 'manual', 'lead') $q$, 'fence_reasons_check');
SELECT pg_temp.must_fail('K8 an empty why is no reason', $q$
  INSERT INTO fence_reasons (fence_id, scope, reason, why, set_by)
  VALUES ('01890000-0000-7000-8000-00000000f003', 'eu1', 'manual', '', 'lead') $q$, 'fence_reasons_check');
SELECT pg_temp.must_pass('K9 other fences need no why', $q$
  INSERT INTO fence_reasons (fence_id, scope, reason, set_by)
  VALUES ('01890000-0000-7000-8000-00000000f003', 'global', 'clock_drift', 'system:watchdog') $q$);

-- ---------------------------------------------------------------------------
-- L. Content removal: the narrow path through append-only evidence (Q10.55)
-- ---------------------------------------------------------------------------
SET ROLE aisre_app;
INSERT INTO artifacts (artifact_id, org_id, incident_id, kind, storage_key, content_sha256, size_bytes,
                       media_type, redacted, retain_until)
  VALUES (:art3, :org, :inc2, 'evidence_snapshot', 'obj/3', :h_a, 10, 'text/plain', true, now() + interval '30 days');
INSERT INTO evidence (evidence_id, incident_id, kind, source_system, tool, collected_by, summary, artifact_id, content_sha256, attributes)
  VALUES (:evd2, :inc2, 'runbook', 'wiki', 'search_runbooks', 'evidence-collector', 'Restart the worker', :art3, :h_a,
          '{"doc_id": "rb-1", "text": "Restart the worker, in full"}');
INSERT INTO evidence (evidence_id, incident_id, kind, source_system, tool, collected_by, summary, content_sha256)
  VALUES (:evd3, :inc2, 'log', 'logs', 'read_logs', 'evidence-collector', 'Error burst', :h_b);
INSERT INTO hypothesis_evidence (hypothesis_id, evidence_id, relation)
  VALUES ('00000000-0000-7000-8000-000000000303', :evd2, 'supports');
RESET ROLE;

SELECT pg_temp.must_fail('L1 the app has no direct write to the extract', format(
  $$SET LOCAL ROLE aisre_app; UPDATE evidence SET summary = 'x' WHERE evidence_id = %L$$, :evd2), 'permission denied');
SELECT pg_temp.must_fail('L2 removal needs a reason', format(
  $$SET LOCAL ROLE aisre_app; UPDATE evidence SET content_removed_at = now() WHERE evidence_id = %L$$, :evd2), 'evidence_check');
SELECT pg_temp.must_fail('L3 only evidence from a retrieved document', format(
  $$UPDATE evidence SET content_removed_at = now(), content_removed_reason = 'legal' WHERE evidence_id = %L$$, :evd3), 'append-only');
SELECT pg_temp.must_fail('L4 the mark cannot carry another change', format(
  $$UPDATE evidence SET source_system = 'x', content_removed_at = now(), content_removed_reason = 'legal' WHERE evidence_id = %L$$, :evd2), 'frozen except');
SELECT pg_temp.must_equal('L5 cited snapshot is held before removal',
  $$SELECT count(*) FROM artifacts_due_for_deletion$$, '0');
SELECT pg_temp.must_pass('L6 the app marks content removed', format(
  $$SET LOCAL ROLE aisre_app; UPDATE evidence SET content_removed_at = now(), content_removed_reason = 'legal request' WHERE evidence_id = %L$$, :evd2));
SELECT pg_temp.must_equal('L7 the extract is gone and the document reference stays',
  format($$SELECT summary || '/' || attributes::text FROM evidence WHERE evidence_id = %L$$, :evd2), 'content removed/{"doc_id": "rb-1"}');
SELECT pg_temp.must_fail('L8 removal happens once', format(
  $$UPDATE evidence SET content_removed_reason = 'again' WHERE evidence_id = %L$$, :evd2), 'append-only');
SELECT pg_temp.must_fail('L9 removal is never undone', format(
  $$UPDATE evidence SET content_removed_at = NULL, content_removed_reason = NULL WHERE evidence_id = %L$$, :evd2), 'append-only');
SELECT pg_temp.must_equal('L10 the snapshot is now due, despite citation and retention',
  $$SELECT string_agg(storage_key, ',') FROM artifacts_due_for_deletion$$, 'obj/3');
SELECT pg_temp.must_pass('L11 the event is accepted', pg_temp.ev(:inc2, 'aisre.evidence.content_removed.v1'));

-- ---------------------------------------------------------------------------
-- M. Retrieval storage: document type and stale flag
-- ---------------------------------------------------------------------------
SELECT pg_temp.must_fail('M1 doc_type is required', format(
  $$INSERT INTO documents (document_id, org_id, doc_ref, doc_version, title, body, services, allowed_groups, reviewed_at, owner)
    VALUES (%L, %L, 'rb-1', 'v1', 't', 'b', '{*}', '{sre}', now(), 'sre')$$, :doc, :org), 'doc_type');
SELECT pg_temp.must_fail('M2 doc_type is runbook or past_incident', format(
  $$INSERT INTO documents (document_id, org_id, doc_ref, doc_version, doc_type, title, body, services, allowed_groups, reviewed_at, owner)
    VALUES (%L, %L, 'rb-1', 'v1', 'wiki', 't', 'b', '{*}', '{sre}', now(), 'sre')$$, :doc, :org), 'documents_doc_type_check');
SELECT pg_temp.must_pass('M3 a document and a revision', format(
  $$SET LOCAL ROLE aisre_app;
    INSERT INTO documents (document_id, org_id, doc_ref, doc_version, doc_type, title, body, services, allowed_groups, reviewed_at, owner)
      VALUES (%1$L, %2$L, 'rb-1', 'v1', 'past_incident', 't', 'b', '{*}', '{sre}', now(), 'sre');
    INSERT INTO index_revisions (index_revision_id, org_id, revision_hash, built_at) VALUES (%3$L, %2$L, %4$L, now())$$,
  :doc, :org, :rev, :h_a));
SELECT pg_temp.must_fail('M4 the stale flag is required', format(
  $$INSERT INTO index_revision_documents (index_revision_id, document_id) VALUES (%L, %L)$$, :rev, :doc), 'stale');
SELECT pg_temp.must_pass('M5 the stale flag is stored per revision', format(
  $$SET LOCAL ROLE aisre_app; INSERT INTO index_revision_documents (index_revision_id, document_id, stale) VALUES (%L, %L, true)$$, :rev, :doc));
SELECT pg_temp.must_fail('M6 the stale flag is as immutable as the revision', $$UPDATE index_revision_documents SET stale = false$$, 'append-only');

-- ---------------------------------------------------------------------------
-- N. A person's outcome resolution, and every action names its expected revision (Layer 11)
-- ---------------------------------------------------------------------------
SELECT pg_temp.must_fail('N1 expected_after_revision is required', format(
  $$INSERT INTO actions (action_id, incident_id, diagnosis_id, kind, target_service_id, target_environment_id, target_deployment,
     base_revision, description, expected_effect, risk, undo_plan, recovery_criteria, recovery_predicates_digest, high_impact, policy_version)
   VALUES (gen_random_uuid(), %L, '00000000-0000-7000-8000-000000000403', 'config_revert', %L, %L, 'd', 'r', 'd', 'e', 'r', 'u', '[]', %L, false, 'p@1')$$,
  :inc2, :svc, :env, :h_b), 'expected_after_revision');

SET ROLE aisre_app;
INSERT INTO actions (action_id, incident_id, diagnosis_id, kind, target_service_id, target_environment_id, target_deployment,
                     base_revision, expected_after_revision, description, expected_effect, risk, undo_plan, recovery_criteria,
                     recovery_predicates_digest, high_impact, policy_version)
  VALUES (:act5, :inc2, '00000000-0000-7000-8000-000000000403', 'config_revert', :svc, :env, 'checkout-prod-ap1', 'r1', 'r2',
          'd', 'e', 'r', 'u', '[]', :h_b, false, 'p@1');
UPDATE actions SET status = 'testing', version = version + 1 WHERE action_id = :act5;
UPDATE actions SET status = 'awaiting_approval', version = version + 1, action_digest = :h_c, test_bundle_digest = :h_a WHERE action_id = :act5;
INSERT INTO approval_requests (approval_request_id, action_id, requested_by, escalate_at, expires_at)
  VALUES (:req5, :act5, 'user:sam', now() + interval '15 minutes', now() + interval '60 minutes');
RESET ROLE;
SELECT pg_temp.must_pass('N2 approval on the action to resolve', pg_temp.decide(:apv6, :req5, :act5, 'user:a', 'approved', :h_c));
UPDATE actions SET status = 'approved', version = version + 1 WHERE action_id = :act5;
SET ROLE aisre_dispatcher;
UPDATE actions SET status = 'applying', version = version + 1 WHERE action_id = :act5;
INSERT INTO execution_attempts (attempt_id, action_id, approval_id, action_digest, execution_key, attempt_no)
  VALUES (:att4, :act5, :apv6, :h_c, :act5, 1);
UPDATE execution_attempts SET outcome = 'unknown', finished_at = now() WHERE attempt_id = :att4;
RESET ROLE;

CREATE FUNCTION pg_temp.resolve(id text, act text, digest text, n int, who text, seen text, why text, recon boolean DEFAULT true,
                                result text DEFAULT 'not_applied') RETURNS text
LANGUAGE sql AS $$
  SELECT format($f$SET LOCAL ROLE aisre_dispatcher; INSERT INTO execution_attempts
    (attempt_id, action_id, approval_id, action_digest, execution_key, attempt_no, reconciliation, outcome,
     finished_at, resolved_by, observed_revision, resolution_reason)
    VALUES (%L, %L, '00000000-0000-7000-8000-000000000606', %L, %L, %s, %L, %L, now(), %L, %L, %L)$f$,
    id, act, digest, act, n, recon, result, who, seen, why)
$$;
SELECT pg_temp.must_fail('N3 only an outcome_unknown action is resolved',
  pg_temp.resolve(:att5, :act5, :h_c, 2, 'user:lead', 'r1', 'Checked the target'), 'resolves only an outcome_unknown');
SELECT pg_temp.must_pass('N4 dispatcher moves applying -> outcome_unknown', format(
  $$SET LOCAL ROLE aisre_dispatcher; UPDATE actions SET status = 'outcome_unknown', version = version + 1 WHERE action_id = %L$$, :act5));
SELECT pg_temp.must_fail('N5 a resolution needs the observed revision',
  pg_temp.resolve(:att5, :act5, :h_c, 2, 'user:lead', NULL, 'Checked the target'), 'execution_attempts_check');
SELECT pg_temp.must_fail('N6 a resolution needs a reason',
  pg_temp.resolve(:att5, :act5, :h_c, 2, 'user:lead', 'r1', NULL), 'execution_attempts_check');
SELECT pg_temp.must_fail('N7 the platform never resolves an outcome',
  pg_temp.resolve(:att5, :act5, :h_c, 2, 'system:dispatcher', 'r1', 'Checked the target'), 'resolved_by_check');
SELECT pg_temp.must_fail('N8 only a reconciliation row carries a resolution',
  'SET LOCAL session_replication_role = replica; ' || pg_temp.resolve(:att5, :act5, :h_c, 2, 'user:lead', 'r1', 'Checked the target', false), 'execution_attempts_check');
SELECT pg_temp.must_fail('N9 the app never writes the resolution',
  replace(pg_temp.resolve(:att5, :act5, :h_c, 2, 'user:lead', 'r1', 'Checked the target'), 'aisre_dispatcher', 'aisre_app'), 'permission denied');
SELECT pg_temp.must_pass('N10 the resolution lands on a reconciliation row, and the action leaves outcome_unknown',
  pg_temp.resolve(:att5, :act5, :h_c, 2, 'user:lead', 'r1', 'Checked the target') || format(
  $$; UPDATE actions SET status = 'not_applied', version = version + 1 WHERE action_id = %L$$, :act5));
SELECT pg_temp.must_equal('N11 actor, outcome, observed revision and reason are stored', format(
  $$SELECT concat_ws('/', resolved_by, outcome, observed_revision, resolution_reason, reconciliation) FROM execution_attempts WHERE attempt_id = %L$$, :att5),
  'user:lead/not_applied/r1/Checked the target/t');
SELECT pg_temp.must_pass('N12 the event is one the dispatcher may write',
  pg_temp.ev(:inc2, 'aisre.execution.outcome_resolved.v1', 'aisre_dispatcher'));
SELECT pg_temp.must_fail('N13 a resolution is final', format(
  $$UPDATE execution_attempts SET resolution_reason = 'changed' WHERE attempt_id = %L$$, :att5), 'is final');
SELECT pg_temp.must_fail('N14 nothing left to resolve',
  pg_temp.resolve(gen_random_uuid()::text, :act5, :h_c, 3, 'user:lead', 'r1', 'Again'), 'nothing to reconcile');
SELECT pg_temp.must_equal('N15 hash chain intact on incident 2',
  format('SELECT count(*) FROM verify_timeline_chain(%L)', :inc2), '0');

-- ---------------------------------------------------------------------------
-- Results
-- ---------------------------------------------------------------------------

\o
SELECT CASE WHEN ok THEN 'PASS ' ELSE 'FAIL ' END || label || CASE WHEN ok THEN '' ELSE '  -> ' || COALESCE(detail, 'no detail') END
FROM test_results ORDER BY n;
SELECT count(*) FILTER (WHERE ok) || ' passed, ' || count(*) FILTER (WHERE NOT ok) || ' failed, on ' || split_part(version(), ',', 1)
FROM test_results;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM test_results WHERE NOT ok) THEN
    RAISE EXCEPTION 'schema tests failed';
  END IF;
END $$;
