-- fault_injection_tests.sql
-- Executable PostgreSQL 15+ Fault Injection Suite (Stage B & C Gate)
-- Verifies ALL 3 mandatory fault-injection scenarios under non-superuser app_runtime:
--   FI-1: Cross-tenant read & composite FK write rejected by PostgreSQL
--   FI-2: Expired Worker A (BEFORE re-claim) rejected on complete / publish / usage
--   FI-3: Approved Timeline v3 proposal rejected once Timeline advances to v4

BEGIN;

-- Seed two tenants, two users, workspaces, projects, and a GPU worker
INSERT INTO tenants (id, name) VALUES
  ('11111111-1111-1111-1111-111111111111', 'Tenant A'),
  ('22222222-2222-2222-2222-222222222222', 'Tenant B');

INSERT INTO users (id, auth_subject, email) VALUES
  ('aaaa1111-1111-1111-1111-111111111111', 'auth|user_a', 'a@example.com'),
  ('bbbb2222-2222-2222-2222-222222222222', 'auth|user_b', 'b@example.com');

INSERT INTO workers (id, pool, status, gpu_model, gpu_vram_total_mb, gpu_vram_free_mb, has_nvenc, max_concurrency)
VALUES ('worker-gpu-a', 'gpu', 'online', 'RTX-4090', 24576, 24576, true, 2);

-- Switch to non-superuser app_runtime role (FORCE RLS strictly active)
SET LOCAL ROLE app_runtime;

-- Create Tenant A workspace & project
SET LOCAL app.tenant_id = '11111111-1111-1111-1111-111111111111';
INSERT INTO workspaces (id, tenant_id, name, created_by)
VALUES ('1111aaaa-1111-1111-1111-111111111111', '11111111-1111-1111-1111-111111111111', 'WS A', 'aaaa1111-1111-1111-1111-111111111111');
INSERT INTO projects (id, tenant_id, workspace_id, name, created_by)
VALUES ('1111bbbb-1111-1111-1111-111111111111', '11111111-1111-1111-1111-111111111111', '1111aaaa-1111-1111-1111-111111111111', 'Proj A', 'aaaa1111-1111-1111-1111-111111111111');

-- ===========================================================================
-- FAULT INJECTION 1: Cross-Tenant Access & Composite FK Rejection
-- ===========================================================================
SET LOCAL app.tenant_id = '22222222-2222-2222-2222-222222222222';
DO $$
DECLARE
  v_cnt integer;
  v_fk_rejected boolean := false;
BEGIN
  -- 1a. Tenant B MUST see 0 projects belonging to Tenant A
  SELECT count(*) INTO v_cnt FROM projects WHERE id = '1111bbbb-1111-1111-1111-111111111111';
  IF v_cnt <> 0 THEN
    RAISE EXCEPTION 'FI-1a FAILED: Tenant B read Tenant A project!';
  END IF;

  -- 1b. Tenant B attempting to attach an Asset to Tenant A's Project MUST fail composite FK
  BEGIN
    INSERT INTO assets (tenant_id, project_id, kind, status, original_filename, storage_key, size_bytes)
    VALUES ('22222222-2222-2222-2222-222222222222', '1111bbbb-1111-1111-1111-111111111111', 'video', 'uploaded', 'x.mp4', 't2/x.mp4', 100);
  EXCEPTION WHEN foreign_key_violation THEN
    v_fk_rejected := true;
  END;

  IF NOT v_fk_rejected THEN
    RAISE EXCEPTION 'FI-1b FAILED: Composite FK did not block cross-tenant asset insert!';
  END IF;
END $$;

-- ===========================================================================
-- FAULT INJECTION 2: Expired Worker A Rejected BEFORE Lease Scanner / Re-Claim
-- ===========================================================================
SET LOCAL app.tenant_id = '11111111-1111-1111-1111-111111111111';
INSERT INTO tasks (id, tenant_id, project_id, task_type, status, payload_json, resource_requirements_json, idempotency_key)
VALUES (
  '1111cccc-1111-1111-1111-111111111111',
  '11111111-1111-1111-1111-111111111111',
  '1111bbbb-1111-1111-1111-111111111111',
  'final_render',
  'queued',
  '{"timeline_version": 1}'::jsonb,
  '{"pool": "gpu", "required_gpu_memory_mb": 4096, "required_nvenc": true}'::jsonb,
  'idem-fi2-task-1'
);

INSERT INTO artifacts (id, tenant_id, project_id, artifact_type, storage_key, checksum_sha256, size_bytes, status)
VALUES (
  '1111dddd-1111-1111-1111-111111111111',
  '11111111-1111-1111-1111-111111111111',
  '1111bbbb-1111-1111-1111-111111111111',
  'final_video',
  'tmp/t1/out.mp4',
  repeat('a', 64),
  1024,
  'temporary'
);

DO $$
DECLARE
  v_claim RECORD;
  v_ok boolean;
BEGIN
  -- Claim with a valid lease, then expire it explicitly before scanner/re-claim.
  SELECT * INTO v_claim FROM claim_next_task('worker-gpu-a', 'gpu', 60);
  IF v_claim.task_id IS NULL OR v_claim.lease_token <> 1 THEN
    RAISE EXCEPTION 'FI-2 setup FAILED: claim_next_task did not return expected task/token';
  END IF;

  UPDATE tasks
     SET lease_expires_at = clock_timestamp() - interval '1 second'
   WHERE tenant_id = v_claim.tenant_id AND id = v_claim.task_id;

  -- Worker A tries to complete with expired lease -> MUST return false
  SELECT complete_task_with_fencing(v_claim.tenant_id, v_claim.task_id, 'worker-gpu-a', v_claim.lease_token, '{}'::jsonb) INTO v_ok;
  IF v_ok THEN
    RAISE EXCEPTION 'FI-2a FAILED: Expired worker was allowed to complete task!';
  END IF;

  -- Worker A tries to publish artifact with expired lease -> MUST return false
  SELECT publish_artifact_with_fencing(v_claim.tenant_id, v_claim.task_id, 'worker-gpu-a', v_claim.lease_token, '1111dddd-1111-1111-1111-111111111111') INTO v_ok;
  IF v_ok THEN
    RAISE EXCEPTION 'FI-2b FAILED: Expired worker was allowed to publish artifact!';
  END IF;

  -- Worker A tries to record usage with expired lease -> MUST return false
  SELECT record_usage_with_fencing(v_claim.tenant_id, '1111bbbb-1111-1111-1111-111111111111', v_claim.task_id, 'worker-gpu-a', v_claim.lease_token, 'gpu_render', 'local', 'nvenc', 10, 'sec', 0.05, 'idem-usage-fi2') INTO v_ok;
  IF v_ok THEN
    RAISE EXCEPTION 'FI-2c FAILED: Expired worker was allowed to record usage!';
  END IF;

  -- Cancellation confirmation must reject expired leases too.
  UPDATE tasks
     SET status = 'cancel_requested'
   WHERE tenant_id = v_claim.tenant_id AND id = v_claim.task_id;
  SELECT confirm_cancel_with_fencing(v_claim.tenant_id, v_claim.task_id, 'worker-gpu-a', v_claim.lease_token) INTO v_ok;
  IF v_ok THEN
    RAISE EXCEPTION 'FI-2d FAILED: Expired worker was allowed to confirm cancellation!';
  END IF;
END $$;

-- ===========================================================================
-- FAULT INJECTION 3: Timeline v3 Approval Rejected After Timeline Advances to v4
-- ===========================================================================
INSERT INTO timelines (id, tenant_id, project_id, current_version, profile_json)
VALUES ('1111eeee-1111-1111-1111-111111111111', '11111111-1111-1111-1111-111111111111', '1111bbbb-1111-1111-1111-111111111111', 3, '{}'::jsonb);

INSERT INTO timeline_versions (tenant_id, timeline_id, version, snapshot_json, snapshot_checksum, created_by_type, created_by_id)
VALUES ('11111111-1111-1111-1111-111111111111', '1111eeee-1111-1111-1111-111111111111', 3, '{}'::jsonb, repeat('3', 64), 'user', 'aaaa1111-1111-1111-1111-111111111111');

INSERT INTO edit_proposals (id, tenant_id, timeline_id, base_version, status, actor_type, actor_id, originating_request_id, rationale, risk_level, proposal_checksum, idempotency_key)
VALUES ('1111ffff-1111-1111-1111-111111111111', '11111111-1111-1111-1111-111111111111', '1111eeee-1111-1111-1111-111111111111', 3, 'approved', 'agent', 'editor-agent', gen_random_uuid(), 'Trim intro', 'high', repeat('c', 64), 'idem-prop-v3');

INSERT INTO human_reviews (id, tenant_id, project_id, subject_type, subject_id, subject_version, subject_checksum, status, decision, reviewer_id, requested_by_type, requested_by_id, idempotency_key, decided_at)
VALUES ('2222ffff-1111-1111-1111-111111111111', '11111111-1111-1111-1111-111111111111', '1111bbbb-1111-1111-1111-111111111111', 'edit_proposal', '1111ffff-1111-1111-1111-111111111111', '3', repeat('c', 64), 'approved', 'approve', 'aaaa1111-1111-1111-1111-111111111111', 'agent', 'editor-agent', 'idem-rev-v3', now());

INSERT INTO approval_tokens (tenant_id, human_review_id, subject_type, subject_id, bound_version, bound_checksum, token_hash, issued_to_user_id, expires_at)
VALUES ('11111111-1111-1111-1111-111111111111', '2222ffff-1111-1111-1111-111111111111', 'edit_proposal', '1111ffff-1111-1111-1111-111111111111', '3', repeat('c', 64), repeat('d', 64), 'aaaa1111-1111-1111-1111-111111111111', now() + interval '15 minutes');

-- Timeline concurrently advances to v4!
INSERT INTO timeline_versions (tenant_id, timeline_id, version, parent_version, snapshot_json, snapshot_checksum, created_by_type, created_by_id)
VALUES ('11111111-1111-1111-1111-111111111111', '1111eeee-1111-1111-1111-111111111111', 4, 3, '{}'::jsonb, repeat('4', 64), 'user', 'aaaa1111-1111-1111-1111-111111111111');
UPDATE timelines SET current_version = 4 WHERE id = '1111eeee-1111-1111-1111-111111111111';

DO $$
DECLARE
  v_review_id uuid;
BEGIN
  SELECT consume_approval_token_for_timeline(
    '11111111-1111-1111-1111-111111111111',
    '1111eeee-1111-1111-1111-111111111111',
    '1111ffff-1111-1111-1111-111111111111',
    3,
    repeat('c', 64),
    repeat('d', 64),
    'aaaa1111-1111-1111-1111-111111111111'
  ) INTO v_review_id;

  IF v_review_id IS NOT NULL THEN
    RAISE EXCEPTION 'FI-3 FAILED: Stale v3 approval token was consumed against Timeline v4!';
  END IF;
END $$;

ROLLBACK;
