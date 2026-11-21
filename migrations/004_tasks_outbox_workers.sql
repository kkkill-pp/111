-- 004_tasks_outbox_workers.sql
-- PostgreSQL 15+ | AI Video Studio HITL v2.1
-- Fixes P0-1 (full lease_expires_at + lease_owner + lease_token fencing),
-- P0-2 (SECURITY DEFINER task_scheduler_role vs FORCE RLS app_runtime separation),
-- P0-3 (PostgreSQL LIMIT 1 FOR UPDATE SKIP LOCKED clause order),
-- P1-5 (complete atomic heartbeat / complete / fail / cancel / reclaim functions + queued dispatch),
-- P1-6 (GPU VRAM, NVENC, and worker concurrency reservation & release).

CREATE TABLE workflow_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  project_id uuid NOT NULL,
  workflow_name text NOT NULL,
  workflow_version integer NOT NULL DEFAULT 1,
  status text NOT NULL DEFAULT 'running' CHECK (status IN (
    'pending', 'running', 'awaiting_human', 'succeeded', 'partial', 'failed', 'cancelled'
  )),
  context_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_by uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, id)
);

CREATE TABLE workers (
  id text PRIMARY KEY,
  pool text NOT NULL CHECK (pool IN ('cpu', 'gpu', 'qa', 'delivery', 'general')),
  status text NOT NULL DEFAULT 'online' CHECK (status IN ('online', 'draining', 'offline')),
  capabilities_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  gpu_model text,
  gpu_vram_total_mb integer NOT NULL DEFAULT 0 CHECK (gpu_vram_total_mb >= 0),
  gpu_vram_free_mb integer NOT NULL DEFAULT 0 CHECK (gpu_vram_free_mb >= 0 AND gpu_vram_free_mb <= gpu_vram_total_mb),
  has_nvenc boolean NOT NULL DEFAULT false,
  active_tasks integer NOT NULL DEFAULT 0 CHECK (active_tasks >= 0),
  max_concurrency integer NOT NULL DEFAULT 1 CHECK (max_concurrency >= 1),
  last_heartbeat_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Outbox Dispatcher MUST insert ready-to-run tasks with status = 'queued'
-- (or 'blocked' when waiting on external gates). 'pending' is reserved solely
-- for pre-dispatch staging and is NEVER claimable.
CREATE TABLE tasks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  project_id uuid,
  workflow_run_id uuid,
  task_type text NOT NULL CHECK (task_type IN (
    'asset_probe', 'proxy_generate', 'thumbnail_generate',
    'asr_transcribe', 'scene_detect', 'keyframe_sample',
    'person_detect', 'vision_analyze', 'semantic_fuse',
    'candidate_generate', 'storyboard_generate',
    'preview_render', 'final_render', 'qa_check', 'delivery', 'cleanup'
  )),
  status text NOT NULL DEFAULT 'queued' CHECK (status IN (
    'pending', 'queued', 'claimed', 'running', 'retry_wait',
    'succeeded', 'failed', 'cancel_requested', 'cancelled', 'blocked', 'expired'
  )),
  priority integer NOT NULL DEFAULT 100,
  payload_json jsonb NOT NULL,
  result_json jsonb,
  resource_requirements_json jsonb NOT NULL DEFAULT '{
    "pool": "cpu",
    "required_gpu_memory_mb": 0,
    "required_nvenc": false,
    "max_runtime_sec": 900
  }'::jsonb,
  reserved_vram_mb integer NOT NULL DEFAULT 0 CHECK (reserved_vram_mb >= 0),
  attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
  next_retry_at timestamptz,
  -- Fencing Token: monotonically incremented on EVERY claim.
  lease_token bigint NOT NULL DEFAULT 0 CHECK (lease_token >= 0),
  lease_owner text REFERENCES workers(id),
  lease_expires_at timestamptz,
  heartbeat_at timestamptz,
  cancel_requested_at timestamptz,
  cancelled_by uuid REFERENCES users(id),
  idempotency_key text NOT NULL,
  last_error_json jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, idempotency_key),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, id),
  FOREIGN KEY (tenant_id, workflow_run_id) REFERENCES workflow_runs (tenant_id, id)
);
CREATE INDEX idx_tasks_claim ON tasks (status, priority, next_retry_at, created_at)
  WHERE status IN ('queued', 'retry_wait');
CREATE INDEX idx_tasks_lease_expiry ON tasks (lease_expires_at)
  WHERE status IN ('claimed', 'running');
CREATE INDEX idx_tasks_tenant_project ON tasks (tenant_id, project_id, created_at DESC);

CREATE TABLE task_dependencies (
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  task_id uuid NOT NULL,
  depends_on_task_id uuid NOT NULL,
  dependency_mode text NOT NULL DEFAULT 'strict' CHECK (dependency_mode IN ('strict', 'allow_partial')),
  PRIMARY KEY (tenant_id, task_id, depends_on_task_id),
  CHECK (task_id <> depends_on_task_id),
  FOREIGN KEY (tenant_id, task_id) REFERENCES tasks (tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, depends_on_task_id) REFERENCES tasks (tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE task_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  task_id uuid NOT NULL,
  attempt_number integer NOT NULL CHECK (attempt_number > 0),
  lease_token bigint NOT NULL CHECK (lease_token > 0),
  worker_id text NOT NULL,
  status text NOT NULL CHECK (status IN ('claimed', 'running', 'succeeded', 'failed', 'expired', 'cancelled')),
  error_json jsonb,
  metrics_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE (tenant_id, task_id, lease_token),
  FOREIGN KEY (tenant_id, task_id) REFERENCES tasks (tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE outbox_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  aggregate_type text NOT NULL,
  aggregate_id uuid NOT NULL,
  event_type text NOT NULL,
  schema_version integer NOT NULL DEFAULT 1,
  payload_json jsonb NOT NULL,
  trace_id text NOT NULL,
  retry_count integer NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
  published_at timestamptz,
  dead_lettered_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_outbox_unpublished ON outbox_events (created_at)
  WHERE published_at IS NULL AND dead_lettered_at IS NULL;

-- ---------------------------------------------------------------------------
-- 1. Cross-Tenant Scheduler Function: claim_next_task (SECURITY DEFINER)
--    Owned by task_scheduler_role. Enforces GPU VRAM, NVENC, and concurrency.
--    Uses valid PostgreSQL order: ORDER BY ... LIMIT 1 FOR UPDATE SKIP LOCKED.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION claim_next_task(
  p_worker_id text,
  p_pool text,
  p_lease_seconds integer DEFAULT 60
)
RETURNS TABLE (
  task_id uuid,
  tenant_id uuid,
  task_type text,
  lease_token bigint,
  lease_expires_at timestamptz,
  attempt integer,
  payload_json jsonb
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  v_worker workers%ROWTYPE;
  v_task tasks%ROWTYPE;
  v_req_vram integer;
BEGIN
  IF p_lease_seconds < 5 OR p_lease_seconds > 3600 THEN
    RAISE EXCEPTION 'lease_seconds must be between 5 and 3600';
  END IF;

  -- Lock worker row to atomically verify and reserve concurrency + GPU VRAM.
  SELECT w.*
    INTO v_worker
    FROM workers w
   WHERE w.id = p_worker_id
     AND w.pool = p_pool
     AND w.status = 'online'
     AND w.active_tasks < w.max_concurrency
   FOR UPDATE;

  IF NOT FOUND THEN
    RETURN;
  END IF;

  SELECT t.*
    INTO v_task
    FROM tasks t
   WHERE t.status IN ('queued', 'retry_wait')
     AND (t.next_retry_at IS NULL OR t.next_retry_at <= now())
     AND COALESCE(t.resource_requirements_json->>'pool', 'cpu') = p_pool
     AND COALESCE((t.resource_requirements_json->>'required_gpu_memory_mb')::integer, 0) <= v_worker.gpu_vram_free_mb
     AND (
       NOT COALESCE((t.resource_requirements_json->>'required_nvenc')::boolean, false)
       OR v_worker.has_nvenc
     )
     AND NOT EXISTS (
       SELECT 1
         FROM task_dependencies d
         JOIN tasks dep ON dep.tenant_id = d.tenant_id AND dep.id = d.depends_on_task_id
        WHERE d.tenant_id = t.tenant_id
          AND d.task_id = t.id
          AND (
            (d.dependency_mode = 'strict' AND dep.status <> 'succeeded')
            OR (d.dependency_mode = 'allow_partial' AND dep.status NOT IN ('succeeded', 'failed', 'cancelled'))
          )
     )
   ORDER BY t.priority ASC, t.created_at ASC
   LIMIT 1
   FOR UPDATE SKIP LOCKED;

  IF NOT FOUND THEN
    RETURN;
  END IF;

  v_req_vram := COALESCE((v_task.resource_requirements_json->>'required_gpu_memory_mb')::integer, 0);

  UPDATE workers
     SET active_tasks = active_tasks + 1,
         gpu_vram_free_mb = GREATEST(0, gpu_vram_free_mb - v_req_vram),
         last_heartbeat_at = now()
   WHERE id = v_worker.id;

  UPDATE tasks
     SET status = 'claimed',
         attempt = tasks.attempt + 1,
         lease_token = tasks.lease_token + 1,
         lease_owner = v_worker.id,
         reserved_vram_mb = v_req_vram,
         lease_expires_at = clock_timestamp() + make_interval(secs => p_lease_seconds),
         heartbeat_at = now(),
         updated_at = now()
   WHERE id = v_task.id
  RETURNING * INTO v_task;

  INSERT INTO task_attempts (tenant_id, task_id, attempt_number, lease_token, worker_id, status)
  VALUES (v_task.tenant_id, v_task.id, v_task.attempt, v_task.lease_token, v_worker.id, 'claimed');

  RETURN QUERY
  SELECT v_task.id, v_task.tenant_id, v_task.task_type, v_task.lease_token,
         v_task.lease_expires_at, v_task.attempt, v_task.payload_json;
END;
$$;

-- ---------------------------------------------------------------------------
-- 2. Heartbeat & Lease Extension with Full Fencing Guard (INVOKER under RLS)
--    Rejects expired leases (lease_expires_at <= now()) and wrong lease_owner.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION heartbeat_task_with_fencing(
  p_tenant_id uuid,
  p_task_id uuid,
  p_worker_id text,
  p_lease_token bigint,
  p_lease_seconds integer DEFAULT 60
)
RETURNS TABLE (
  accepted boolean,
  cancel_requested boolean,
  new_lease_expires_at timestamptz
)
LANGUAGE plpgsql
AS $$
DECLARE
  v_status text;
  v_new_exp timestamptz;
BEGIN
  UPDATE tasks
     SET status = CASE WHEN status = 'claimed' THEN 'running' ELSE status END,
         heartbeat_at = now(),
         lease_expires_at = clock_timestamp() + make_interval(secs => p_lease_seconds),
         updated_at = now()
   WHERE tenant_id = p_tenant_id
     AND id = p_task_id
     AND lease_owner = p_worker_id
     AND lease_token = p_lease_token
     AND lease_expires_at > clock_timestamp()
     AND status IN ('claimed', 'running', 'cancel_requested')
  RETURNING status, lease_expires_at INTO v_status, v_new_exp;

  IF NOT FOUND THEN
    RETURN QUERY SELECT false, false, NULL::timestamptz;
    RETURN;
  END IF;

  UPDATE workers SET last_heartbeat_at = now() WHERE id = p_worker_id;

  RETURN QUERY SELECT true, (v_status = 'cancel_requested'), v_new_exp;
END;
$$;

-- ---------------------------------------------------------------------------
-- 3. Complete Task with Full Fencing Guard (rejects expired lease BEFORE re-claim!)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION complete_task_with_fencing(
  p_tenant_id uuid,
  p_task_id uuid,
  p_worker_id text,
  p_lease_token bigint,
  p_result_json jsonb
)
RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
  v_vram integer;
BEGIN
  SELECT reserved_vram_mb
    INTO v_vram
    FROM tasks
   WHERE tenant_id = p_tenant_id
     AND id = p_task_id
     AND lease_owner = p_worker_id
     AND lease_token = p_lease_token
     AND lease_expires_at > clock_timestamp()
     AND status IN ('claimed', 'running')
   FOR UPDATE;

  IF NOT FOUND THEN
    RETURN false;
  END IF;

  UPDATE tasks
     SET status = 'succeeded',
         result_json = p_result_json,
         lease_expires_at = NULL,
         reserved_vram_mb = 0,
         updated_at = now()
   WHERE tenant_id = p_tenant_id AND id = p_task_id;

  UPDATE task_attempts
     SET status = 'succeeded',
         finished_at = now()
   WHERE tenant_id = p_tenant_id
     AND task_id = p_task_id
     AND lease_token = p_lease_token;

  UPDATE workers
     SET active_tasks = GREATEST(0, active_tasks - 1),
         gpu_vram_free_mb = LEAST(gpu_vram_total_mb, gpu_vram_free_mb + COALESCE(v_vram, 0))
   WHERE id = p_worker_id;

  RETURN true;
END;
$$;

-- ---------------------------------------------------------------------------
-- 4. Fail / Retry Task with Full Fencing Guard & Exponential Backoff + Jitter
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fail_task_with_fencing(
  p_tenant_id uuid,
  p_task_id uuid,
  p_worker_id text,
  p_lease_token bigint,
  p_error_json jsonb,
  p_retryable boolean DEFAULT true
)
RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
  v_task tasks%ROWTYPE;
  v_new_status text;
  v_backoff_sec integer;
BEGIN
  SELECT *
    INTO v_task
    FROM tasks
   WHERE tenant_id = p_tenant_id
     AND id = p_task_id
     AND lease_owner = p_worker_id
     AND lease_token = p_lease_token
     AND lease_expires_at > clock_timestamp()
     AND status IN ('claimed', 'running')
   FOR UPDATE;

  IF NOT FOUND THEN
    RETURN 'rejected_stale_lease';
  END IF;

  IF p_retryable AND v_task.attempt < v_task.max_attempts THEN
    v_new_status := 'retry_wait';
    v_backoff_sec := LEAST(600, (10 * power(2, GREATEST(0, v_task.attempt - 1)))::integer + floor(random() * 5)::integer);
  ELSE
    v_new_status := 'failed';
    v_backoff_sec := NULL;
  END IF;

  UPDATE tasks
     SET status = v_new_status,
         last_error_json = p_error_json,
         lease_expires_at = NULL,
         reserved_vram_mb = 0,
         next_retry_at = CASE WHEN v_backoff_sec IS NOT NULL THEN now() + make_interval(secs => v_backoff_sec) ELSE NULL END,
         updated_at = now()
   WHERE tenant_id = p_tenant_id AND id = p_task_id;

  UPDATE task_attempts
     SET status = 'failed',
         error_json = p_error_json,
         finished_at = now()
   WHERE tenant_id = p_tenant_id
     AND task_id = p_task_id
     AND lease_token = p_lease_token;

  UPDATE workers
     SET active_tasks = GREATEST(0, active_tasks - 1),
         gpu_vram_free_mb = LEAST(gpu_vram_total_mb, gpu_vram_free_mb + v_task.reserved_vram_mb)
   WHERE id = p_worker_id;

  RETURN v_new_status;
END;
$$;

-- ---------------------------------------------------------------------------
-- 5. Cooperative Cancellation Confirmation with Fencing Guard
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION confirm_cancel_with_fencing(
  p_tenant_id uuid,
  p_task_id uuid,
  p_worker_id text,
  p_lease_token bigint
)
RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
  v_vram integer;
BEGIN
  SELECT reserved_vram_mb
    INTO v_vram
    FROM tasks
   WHERE tenant_id = p_tenant_id
     AND id = p_task_id
     AND lease_owner = p_worker_id
     AND lease_token = p_lease_token
     AND lease_expires_at > clock_timestamp()
     AND status = 'cancel_requested'
   FOR UPDATE;

  IF NOT FOUND THEN
    RETURN false;
  END IF;

  UPDATE tasks
     SET status = 'cancelled',
         lease_expires_at = NULL,
         reserved_vram_mb = 0,
         updated_at = now()
   WHERE tenant_id = p_tenant_id AND id = p_task_id;

  UPDATE task_attempts
     SET status = 'cancelled',
         finished_at = now()
   WHERE tenant_id = p_tenant_id
     AND task_id = p_task_id
     AND lease_token = p_lease_token;

  UPDATE workers
     SET active_tasks = GREATEST(0, active_tasks - 1),
         gpu_vram_free_mb = LEAST(gpu_vram_total_mb, gpu_vram_free_mb + COALESCE(v_vram, 0))
   WHERE id = p_worker_id;

  RETURN true;
END;
$$;

-- ---------------------------------------------------------------------------
-- 6. Cross-Tenant Lease Scanner: reclaim_expired_leases (SECURITY DEFINER)
--    Owned by task_scheduler_role. Reclaims expired leases and frees worker slots.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION reclaim_expired_leases(p_batch_size integer DEFAULT 50)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  r RECORD;
  v_count integer := 0;
  v_new_status text;
  v_backoff_sec integer;
BEGIN
  FOR r IN
    SELECT id, tenant_id, lease_owner, lease_token, attempt, max_attempts, reserved_vram_mb
      FROM tasks
     WHERE status IN ('claimed', 'running')
       AND lease_expires_at <= now()
     ORDER BY lease_expires_at ASC
     LIMIT p_batch_size
     FOR UPDATE SKIP LOCKED
  LOOP
    IF r.attempt < r.max_attempts THEN
      v_new_status := 'retry_wait';
      v_backoff_sec := LEAST(600, (10 * power(2, GREATEST(0, r.attempt - 1)))::integer);
    ELSE
      v_new_status := 'failed';
      v_backoff_sec := NULL;
    END IF;

    UPDATE tasks
       SET status = v_new_status,
           lease_expires_at = NULL,
           reserved_vram_mb = 0,
           next_retry_at = CASE WHEN v_backoff_sec IS NOT NULL THEN now() + make_interval(secs => v_backoff_sec) ELSE NULL END,
           last_error_json = jsonb_build_object('code', 'LEASE_EXPIRED', 'expired_worker', r.lease_owner, 'expired_token', r.lease_token),
           updated_at = now()
     WHERE tenant_id = r.tenant_id AND id = r.id;

    UPDATE task_attempts
       SET status = 'expired',
           finished_at = now()
     WHERE tenant_id = r.tenant_id
       AND task_id = r.id
       AND lease_token = r.lease_token;

    IF r.lease_owner IS NOT NULL THEN
      UPDATE workers
         SET active_tasks = GREATEST(0, active_tasks - 1),
             gpu_vram_free_mb = LEAST(gpu_vram_total_mb, gpu_vram_free_mb + r.reserved_vram_mb)
       WHERE id = r.lease_owner;
    END IF;

    v_count := v_count + 1;
  END LOOP;

  RETURN v_count;
END;
$$;

-- Bind ownership of cross-tenant scheduler functions strictly to task_scheduler_role
ALTER FUNCTION claim_next_task(text, text, integer) OWNER TO task_scheduler_role;
ALTER FUNCTION reclaim_expired_leases(integer) OWNER TO task_scheduler_role;
REVOKE ALL ON FUNCTION claim_next_task(text, text, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION reclaim_expired_leases(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_next_task(text, text, integer) TO app_runtime;
GRANT EXECUTE ON FUNCTION reclaim_expired_leases(integer) TO task_scheduler_role;
