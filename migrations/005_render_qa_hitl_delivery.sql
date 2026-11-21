-- 005_render_qa_hitl_delivery.sql
-- PostgreSQL 15+ | AI Video Studio HITL v2.1
-- Adds atomic publish_artifact_with_fencing() and consume_approval_token_for_timeline()
-- enforcing active lease + fencing token + version-bound single-use approval inside DB.

CREATE TABLE render_plans (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  project_id uuid NOT NULL,
  timeline_id uuid NOT NULL,
  timeline_version integer NOT NULL CHECK (timeline_version > 0),
  render_mode text NOT NULL CHECK (render_mode IN ('preview', 'final')),
  renderer text NOT NULL DEFAULT 'ffmpeg' CHECK (renderer IN ('ffmpeg')),
  plan_version text NOT NULL DEFAULT '2.1',
  plan_json jsonb NOT NULL,
  plan_checksum char(64) NOT NULL,
  created_by uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, id),
  FOREIGN KEY (tenant_id, timeline_id, timeline_version)
    REFERENCES timeline_versions (tenant_id, timeline_id, version)
);
CREATE INDEX idx_render_plans_timeline ON render_plans (tenant_id, timeline_id, timeline_version);

CREATE TABLE render_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  project_id uuid NOT NULL,
  render_plan_id uuid NOT NULL,
  task_id uuid,
  render_mode text NOT NULL CHECK (render_mode IN ('preview', 'final')),
  status text NOT NULL DEFAULT 'queued' CHECK (status IN (
    'queued', 'claimed', 'running', 'qa_pending',
    'qa_passed', 'qa_warning', 'qa_failed',
    'retry_wait', 'failed', 'cancel_requested', 'cancelled'
  )),
  budget_estimate_usd numeric(18,8),
  budget_acknowledged boolean NOT NULL DEFAULT false,
  renderer_version text,
  encoder_used text,
  idempotency_key text NOT NULL,
  created_by uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, idempotency_key),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, id),
  FOREIGN KEY (tenant_id, render_plan_id) REFERENCES render_plans (tenant_id, id),
  FOREIGN KEY (tenant_id, task_id) REFERENCES tasks (tenant_id, id)
);
CREATE INDEX idx_render_jobs_project ON render_jobs (tenant_id, project_id, created_at DESC);

CREATE TABLE artifacts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  project_id uuid NOT NULL,
  render_job_id uuid,
  timeline_id uuid,
  timeline_version integer,
  artifact_type text NOT NULL CHECK (artifact_type IN (
    'proxy_video', 'thumbnail', 'preview_video', 'final_video',
    'subtitle_file', 'qa_attachment', 'delivery_package'
  )),
  storage_key text NOT NULL,
  checksum_sha256 char(64) NOT NULL,
  size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'temporary' CHECK (status IN (
    'temporary', 'ready', 'quarantined', 'failed', 'expired', 'deleted'
  )),
  created_by_lease_token bigint,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, id),
  FOREIGN KEY (tenant_id, render_job_id) REFERENCES render_jobs (tenant_id, id),
  FOREIGN KEY (tenant_id, timeline_id) REFERENCES timelines (tenant_id, id)
);
CREATE INDEX idx_artifacts_project ON artifacts (tenant_id, project_id, artifact_type, created_at DESC);

CREATE TABLE qa_reports (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  project_id uuid NOT NULL,
  artifact_id uuid NOT NULL,
  render_job_id uuid,
  status text NOT NULL CHECK (status IN ('pending', 'running', 'passed', 'warning', 'failed')),
  technical_checks_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  content_checks_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  blockers_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  warnings_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, id),
  FOREIGN KEY (tenant_id, artifact_id) REFERENCES artifacts (tenant_id, id),
  FOREIGN KEY (tenant_id, render_job_id) REFERENCES render_jobs (tenant_id, id)
);
CREATE INDEX idx_qa_reports_artifact ON qa_reports (tenant_id, artifact_id, created_at DESC);

CREATE TABLE human_reviews (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  project_id uuid NOT NULL,
  subject_type text NOT NULL CHECK (subject_type IN ('storyboard', 'edit_proposal', 'render_artifact', 'delivery')),
  subject_id uuid NOT NULL,
  subject_version text NOT NULL,
  subject_checksum char(64) NOT NULL,
  qa_report_id uuid,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN (
    'pending', 'approved', 'approved_with_warnings', 'rejected', 'changes_requested', 'expired', 'superseded'
  )),
  decision text CHECK (decision IS NULL OR decision IN ('approve', 'approve_with_warnings', 'reject', 'request_changes')),
  reviewer_id uuid REFERENCES users(id),
  requested_by_type text NOT NULL DEFAULT 'system' CHECK (requested_by_type IN ('user', 'agent', 'system')),
  requested_by_id text NOT NULL,
  review_reason text,
  comment text,
  selected_items_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  rejected_items_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  idempotency_key text NOT NULL,
  expires_at timestamptz,
  decided_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, idempotency_key),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, id),
  FOREIGN KEY (tenant_id, qa_report_id) REFERENCES qa_reports (tenant_id, id),
  CHECK (
    (status = 'pending' AND decision IS NULL AND decided_at IS NULL)
    OR (status IN ('expired', 'superseded'))
    OR (status IN ('approved', 'approved_with_warnings', 'rejected', 'changes_requested')
        AND decision IS NOT NULL AND reviewer_id IS NOT NULL AND decided_at IS NOT NULL)
  )
);
CREATE INDEX idx_human_reviews_subject ON human_reviews (tenant_id, subject_type, subject_id, subject_version, status);

CREATE TABLE approval_tokens (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  human_review_id uuid NOT NULL,
  subject_type text NOT NULL CHECK (subject_type IN ('edit_proposal', 'render_artifact', 'delivery')),
  subject_id uuid NOT NULL,
  bound_version text NOT NULL,
  bound_checksum char(64) NOT NULL,
  token_hash char(64) NOT NULL UNIQUE,
  issued_to_user_id uuid NOT NULL REFERENCES users(id),
  expires_at timestamptz NOT NULL,
  consumed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, human_review_id) REFERENCES human_reviews (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_approval_tokens_active ON approval_tokens (tenant_id, subject_id) WHERE consumed_at IS NULL;

CREATE TABLE deliveries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  project_id uuid NOT NULL,
  artifact_id uuid NOT NULL,
  artifact_checksum char(64) NOT NULL,
  qa_report_id uuid NOT NULL,
  human_review_id uuid NOT NULL,
  destination_type text NOT NULL CHECK (destination_type IN ('signed_download', 'webhook', 's3_export', 'social_platform')),
  destination_config_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'blocked' CHECK (status IN (
    'blocked', 'ready', 'running', 'delivered', 'failed', 'cancelled', 'revoked'
  )),
  idempotency_key text NOT NULL,
  created_by uuid NOT NULL REFERENCES users(id),
  delivered_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, idempotency_key),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, id),
  FOREIGN KEY (tenant_id, artifact_id) REFERENCES artifacts (tenant_id, id),
  FOREIGN KEY (tenant_id, qa_report_id) REFERENCES qa_reports (tenant_id, id),
  FOREIGN KEY (tenant_id, human_review_id) REFERENCES human_reviews (tenant_id, id)
);

-- ---------------------------------------------------------------------------
-- Atomic Fencing-Guarded Artifact Publication (rejects expired lease!)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION publish_artifact_with_fencing(
  p_tenant_id uuid,
  p_task_id uuid,
  p_worker_id text,
  p_lease_token bigint,
  p_artifact_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
  v_valid boolean;
  v_updated integer;
BEGIN
  -- Lock the task row so lease reclaim/reassignment cannot race publication.
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

  UPDATE artifacts
     SET status = 'ready',
         created_by_lease_token = p_lease_token
   WHERE tenant_id = p_tenant_id
     AND id = p_artifact_id
     AND status = 'temporary';

  GET DIAGNOSTICS v_updated = ROW_COUNT;
  RETURN v_updated = 1;
END;
$$;

-- ---------------------------------------------------------------------------
-- Atomic Single-Use Approval Token Consumption Bound to Live Timeline Version
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION consume_approval_token_for_timeline(
  p_tenant_id uuid,
  p_timeline_id uuid,
  p_proposal_id uuid,
  p_expected_base_version integer,
  p_expected_checksum char(64),
  p_token_hash char(64),
  p_issued_to_user_id uuid
)
RETURNS uuid
LANGUAGE plpgsql
AS $$
DECLARE
  v_current_version integer;
  v_review_id uuid;
BEGIN
  -- Serialize against timeline advancement first.
  SELECT current_version
    INTO v_current_version
    FROM timelines
   WHERE tenant_id = p_tenant_id
     AND id = p_timeline_id
   FOR UPDATE;

  IF NOT FOUND OR v_current_version <> p_expected_base_version THEN
    RETURN NULL;
  END IF;

  -- Token, review, and proposal must all describe the same approved immutable object.
  SELECT tok.human_review_id
    INTO v_review_id
    FROM approval_tokens AS tok
    JOIN human_reviews AS rev
      ON rev.tenant_id = tok.tenant_id
     AND rev.id = tok.human_review_id
    JOIN edit_proposals AS prop
      ON prop.tenant_id = tok.tenant_id
     AND prop.id = tok.subject_id
   WHERE tok.tenant_id = p_tenant_id
     AND tok.subject_type = 'edit_proposal'
     AND tok.subject_id = p_proposal_id
     AND tok.bound_version = p_expected_base_version::text
     AND tok.bound_checksum = p_expected_checksum
     AND tok.token_hash = p_token_hash
     AND tok.issued_to_user_id = p_issued_to_user_id
     AND tok.consumed_at IS NULL
     AND tok.expires_at > clock_timestamp()
     AND rev.status IN ('approved', 'approved_with_warnings')
     AND rev.subject_type = 'edit_proposal'
     AND rev.subject_id = p_proposal_id
     AND rev.subject_version = p_expected_base_version::text
     AND rev.subject_checksum = p_expected_checksum
     AND prop.timeline_id = p_timeline_id
     AND prop.base_version = p_expected_base_version
     AND prop.proposal_checksum = p_expected_checksum
     AND prop.status = 'approved'
   FOR UPDATE OF tok, rev, prop;

  IF NOT FOUND THEN
    RETURN NULL;
  END IF;

  UPDATE approval_tokens
     SET consumed_at = clock_timestamp()
   WHERE tenant_id = p_tenant_id
     AND human_review_id = v_review_id
     AND subject_type = 'edit_proposal'
     AND subject_id = p_proposal_id
     AND bound_version = p_expected_base_version::text
     AND bound_checksum = p_expected_checksum
     AND token_hash = p_token_hash
     AND issued_to_user_id = p_issued_to_user_id
     AND consumed_at IS NULL
     AND expires_at > clock_timestamp();

  IF NOT FOUND THEN
    RETURN NULL;
  END IF;

  RETURN v_review_id;
END;
$$;
