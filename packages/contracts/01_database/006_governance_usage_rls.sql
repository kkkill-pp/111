-- 006_governance_usage_rls.sql
-- PostgreSQL 15+ | AI Video Studio HITL v2.1
-- Agent Governance, Fencing-Guarded Usage Metering, Audit Events,
-- MANDATORY Enforced Row-Level Security (RLS), and Role Grants.

CREATE TABLE prompt_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  version text NOT NULL,
  template_text text NOT NULL,
  checksum_sha256 char(64) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (name, version)
);

CREATE TABLE agent_executions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  project_id uuid NOT NULL,
  acting_user_id uuid NOT NULL REFERENCES users(id),
  acting_role text NOT NULL CHECK (acting_role IN ('owner', 'admin', 'editor', 'reviewer', 'viewer')),
  agent_name text NOT NULL,
  agent_version text NOT NULL,
  hitl_mode text NOT NULL CHECK (hitl_mode IN ('copilot', 'supervised_autopilot', 'batch_operator')),
  status text NOT NULL CHECK (status IN ('running', 'awaiting_human', 'succeeded', 'failed', 'budget_exceeded', 'cancelled')),
  step_count integer NOT NULL DEFAULT 0 CHECK (step_count >= 0),
  max_steps integer NOT NULL DEFAULT 12 CHECK (max_steps > 0),
  tool_call_count integer NOT NULL DEFAULT 0 CHECK (tool_call_count >= 0),
  max_tool_calls integer NOT NULL DEFAULT 20 CHECK (max_tool_calls > 0),
  token_usage_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  cost_usd numeric(18,8) NOT NULL DEFAULT 0,
  max_cost_usd numeric(18,8) NOT NULL DEFAULT 5.0,
  wall_time_ms integer,
  trace_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, id)
);

CREATE TABLE agent_tool_calls (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  agent_execution_id uuid NOT NULL,
  tool_name text NOT NULL,
  access_tier text NOT NULL CHECK (access_tier IN ('read', 'propose', 'validate', 'controlled_write', 'execute', 'workflow')),
  input_json jsonb NOT NULL,
  output_json jsonb,
  decision text NOT NULL CHECK (decision IN ('allowed', 'denied', 'requires_approval')),
  denial_reason text,
  duration_ms integer,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, agent_execution_id) REFERENCES agent_executions (tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE model_invocations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  project_id uuid,
  agent_execution_id uuid,
  task_id uuid,
  provider text NOT NULL,
  model_id text NOT NULL,
  capability text NOT NULL,
  prompt_version_id uuid REFERENCES prompt_versions(id),
  input_tokens integer NOT NULL DEFAULT 0,
  output_tokens integer NOT NULL DEFAULT 0,
  latency_ms integer,
  cost_usd numeric(18,8) NOT NULL DEFAULT 0,
  status text NOT NULL CHECK (status IN ('succeeded', 'failed', 'timeout', 'rate_limited')),
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, id),
  FOREIGN KEY (tenant_id, agent_execution_id) REFERENCES agent_executions (tenant_id, id),
  FOREIGN KEY (tenant_id, task_id) REFERENCES tasks (tenant_id, id)
);

CREATE TABLE usage_ledger (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  project_id uuid,
  task_id uuid,
  lease_token bigint,
  usage_type text NOT NULL,
  provider text,
  model text,
  quantity numeric NOT NULL CHECK (quantity >= 0),
  unit text NOT NULL,
  estimated_cost numeric(18,8) NOT NULL DEFAULT 0,
  actual_cost numeric(18,8) NOT NULL DEFAULT 0,
  currency char(3) NOT NULL DEFAULT 'USD',
  idempotency_key text NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, idempotency_key),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, id),
  FOREIGN KEY (tenant_id, task_id) REFERENCES tasks (tenant_id, id)
);
CREATE INDEX idx_usage_ledger_tenant_time ON usage_ledger (tenant_id, occurred_at DESC);

CREATE TABLE feedbacks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  project_id uuid NOT NULL,
  subject_type text NOT NULL CHECK (subject_type IN ('highlight_candidate', 'storyboard', 'edit_proposal', 'render_artifact')),
  subject_id uuid NOT NULL,
  action text NOT NULL CHECK (action IN ('accepted', 'modified', 'rejected', 'undone')),
  user_id uuid NOT NULL REFERENCES users(id),
  reason_code text,
  payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, id)
);

CREATE TABLE audit_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  actor_type text NOT NULL CHECK (actor_type IN ('user', 'agent', 'system', 'worker')),
  actor_id text NOT NULL,
  originating_user_id uuid REFERENCES users(id),
  action text NOT NULL,
  resource_type text NOT NULL,
  resource_id text NOT NULL,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  trace_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_events_tenant_time ON audit_events (tenant_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Atomic Fencing-Guarded Usage Recording (rejects expired / stale workers!)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION record_usage_with_fencing(
  p_tenant_id uuid,
  p_project_id uuid,
  p_task_id uuid,
  p_worker_id text,
  p_lease_token bigint,
  p_usage_type text,
  p_provider text,
  p_model text,
  p_quantity numeric,
  p_unit text,
  p_actual_cost numeric,
  p_idempotency_key text
)
RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
  v_valid boolean;
BEGIN
  -- Serialize usage recording against lease reclaim/reassignment.
  PERFORM 1
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

  INSERT INTO usage_ledger (
    tenant_id, project_id, task_id, lease_token,
    usage_type, provider, model, quantity, unit,
    actual_cost, idempotency_key
  )
  VALUES (
    p_tenant_id, p_project_id, p_task_id, p_lease_token,
    p_usage_type, p_provider, p_model, p_quantity, p_unit,
    p_actual_cost, p_idempotency_key
  )
  ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;

  RETURN true;
END;
$$;

-- ---------------------------------------------------------------------------
-- MANDATORY PostgreSQL Row-Level Security (RLS) on ALL 39 Tenant-Owned Tables
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION current_app_tenant_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
  SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid;
$$;

DO $$
DECLARE
  tbl text;
  tenant_tables text[] := ARRAY[
    'workspaces', 'workspace_members', 'api_credentials',
    'projects', 'upload_sessions', 'assets', 'media_probes',
    'analysis_runs', 'transcripts', 'scenes', 'semantic_segments', 'highlight_candidates',
    'creative_briefs', 'creative_brief_versions', 'storyboards', 'storyboard_versions',
    'timelines', 'timeline_versions', 'edit_proposals', 'edit_operations', 'timeline_comments',
    'workflow_runs', 'tasks', 'task_dependencies', 'task_attempts', 'outbox_events',
    'render_plans', 'render_jobs', 'artifacts', 'qa_reports',
    'human_reviews', 'approval_tokens', 'deliveries',
    'agent_executions', 'agent_tool_calls', 'model_invocations',
    'usage_ledger', 'feedbacks', 'audit_events'
  ];
BEGIN
  FOREACH tbl IN ARRAY tenant_tables LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY;', tbl);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY;', tbl);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation_policy ON %I;', tbl);
    EXECUTE format(
      'CREATE POLICY tenant_isolation_policy ON %I USING (tenant_id = current_app_tenant_id()) WITH CHECK (tenant_id = current_app_tenant_id());',
      tbl
    );
  END LOOP;
END $$;

-- Minimal privilege grants: app_runtime operates strictly under FORCE RLS.
GRANT USAGE ON SCHEMA public TO app_runtime, task_scheduler_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_runtime;
GRANT SELECT, INSERT, UPDATE ON tasks, task_dependencies, task_attempts, workers TO task_scheduler_role;
