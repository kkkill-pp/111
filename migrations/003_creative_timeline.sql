-- 003_creative_timeline.sql
-- PostgreSQL 15+ | AI Video Studio HITL v2.1
-- Creative Briefs, Storyboards, Canonical Timelines, Immutable Versions,
-- Batch Edit Proposals (HITL), and Atomic Edit Operations (18 canonical types;
-- UNDO/REDO are independent version commands recorded via origin_command).

CREATE TABLE creative_briefs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  project_id uuid NOT NULL,
  title text NOT NULL,
  current_version integer NOT NULL DEFAULT 1 CHECK (current_version > 0),
  created_by uuid NOT NULL REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, id)
);

CREATE TABLE creative_brief_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  brief_id uuid NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  brief_json jsonb NOT NULL,
  brief_checksum char(64) NOT NULL,
  created_by uuid NOT NULL REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, brief_id, version),
  FOREIGN KEY (tenant_id, brief_id) REFERENCES creative_briefs (tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE storyboards (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  project_id uuid NOT NULL,
  brief_version_id uuid NOT NULL,
  current_version integer NOT NULL DEFAULT 1 CHECK (current_version > 0),
  status text NOT NULL DEFAULT 'generating' CHECK (status IN (
    'generating', 'proposed', 'accepted', 'rejected', 'revision_requested', 'superseded', 'stale'
  )),
  created_by_type text NOT NULL CHECK (created_by_type IN ('user', 'agent', 'system')),
  created_by_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, id),
  FOREIGN KEY (tenant_id, brief_version_id) REFERENCES creative_brief_versions (tenant_id, id)
);
CREATE INDEX idx_storyboards_project ON storyboards (tenant_id, project_id, created_at DESC);

CREATE TABLE storyboard_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  storyboard_id uuid NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  brief_version_id uuid NOT NULL,
  source_analysis_run_ids uuid[] NOT NULL DEFAULT ARRAY[]::uuid[],
  proposal_json jsonb NOT NULL,
  proposal_checksum char(64) NOT NULL,
  confidence numeric(4,3) CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  model_provider text,
  model_id text,
  prompt_version text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, storyboard_id, version),
  FOREIGN KEY (tenant_id, storyboard_id) REFERENCES storyboards (tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, brief_version_id) REFERENCES creative_brief_versions (tenant_id, id)
);

CREATE TABLE timelines (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  project_id uuid NOT NULL,
  source_storyboard_id uuid,
  source_storyboard_version integer,
  name text NOT NULL DEFAULT 'Main Timeline',
  current_version integer NOT NULL DEFAULT 1 CHECK (current_version > 0),
  profile_json jsonb NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'locked', 'archived')),
  created_by uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, id),
  FOREIGN KEY (tenant_id, source_storyboard_id) REFERENCES storyboards (tenant_id, id)
);
CREATE INDEX idx_timelines_project ON timelines (tenant_id, project_id, created_at DESC);

CREATE TABLE timeline_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  timeline_id uuid NOT NULL,
  version integer NOT NULL CHECK (version > 0),
  parent_version integer CHECK (parent_version IS NULL OR parent_version < version),
  origin_command text NOT NULL DEFAULT 'edit' CHECK (origin_command IN ('init', 'storyboard_compile', 'edit', 'undo', 'redo')),
  restored_from_version integer CHECK (restored_from_version IS NULL OR restored_from_version < version),
  snapshot_json jsonb NOT NULL,
  snapshot_checksum char(64) NOT NULL,
  created_by_type text NOT NULL CHECK (created_by_type IN ('user', 'agent', 'system')),
  created_by_id text NOT NULL,
  originating_request_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, timeline_id, version),
  FOREIGN KEY (tenant_id, timeline_id) REFERENCES timelines (tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE edit_proposals (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  timeline_id uuid NOT NULL,
  base_version integer NOT NULL CHECK (base_version > 0),
  result_version integer CHECK (result_version IS NULL OR result_version > base_version),
  status text NOT NULL DEFAULT 'draft' CHECK (status IN (
    'draft', 'validating', 'awaiting_review', 'approved', 'applied',
    'rejected', 'expired', 'superseded', 'invalid'
  )),
  actor_type text NOT NULL CHECK (actor_type IN ('user', 'agent', 'system')),
  actor_id text NOT NULL,
  originating_user_id uuid REFERENCES users(id),
  originating_request_id uuid NOT NULL,
  user_intent text,
  rationale text NOT NULL,
  risk_level text NOT NULL CHECK (risk_level IN ('low', 'medium', 'high', 'critical')),
  evidence_refs_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  validation_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  diff_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  impact_summary_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  proposal_checksum char(64) NOT NULL,
  idempotency_key text NOT NULL,
  expires_at timestamptz,
  applied_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, idempotency_key),
  FOREIGN KEY (tenant_id, timeline_id) REFERENCES timelines (tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, timeline_id, base_version) REFERENCES timeline_versions (tenant_id, timeline_id, version)
);
CREATE INDEX idx_edit_proposals_timeline ON edit_proposals (tenant_id, timeline_id, status, created_at DESC);

-- Strictly 18 canonical EditOperation types (matching edit-operation.schema.json & openapi.yaml).
-- UNDO/REDO are independent version commands on /timelines/{id}/undo and /redo.
CREATE TABLE edit_operations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  timeline_id uuid NOT NULL,
  proposal_id uuid,
  sequence_index integer NOT NULL DEFAULT 0 CHECK (sequence_index >= 0),
  base_version integer NOT NULL CHECK (base_version > 0),
  result_version integer CHECK (result_version IS NULL OR result_version > base_version),
  actor_type text NOT NULL CHECK (actor_type IN ('user', 'agent', 'system')),
  actor_id text NOT NULL,
  originating_user_id uuid REFERENCES users(id),
  originating_request_id uuid,
  operation_type text NOT NULL CHECK (operation_type IN (
    'ADD_CLIP', 'REMOVE_CLIP', 'TRIM_CLIP', 'MOVE_CLIP', 'SPLIT_CLIP',
    'MERGE_CLIP', 'REORDER_CLIP', 'UPDATE_TRANSFORM', 'UPDATE_SPEED',
    'UPDATE_VOLUME', 'ADD_SUBTITLE', 'UPDATE_SUBTITLE', 'REMOVE_SUBTITLE',
    'ADD_TRANSITION', 'REMOVE_TRANSITION', 'ADD_OVERLAY', 'REMOVE_OVERLAY',
    'SET_CANVAS_PROFILE'
  )),
  payload_json jsonb NOT NULL,
  inverse_payload_json jsonb,
  reason text,
  evidence_refs_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  risk_level text NOT NULL DEFAULT 'medium' CHECK (risk_level IN ('low', 'medium', 'high', 'critical')),
  validation_json jsonb,
  status text NOT NULL CHECK (status IN ('proposed', 'validated', 'selected', 'applied', 'rejected', 'superseded', 'invalid')),
  idempotency_key text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, idempotency_key),
  FOREIGN KEY (tenant_id, timeline_id) REFERENCES timelines (tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, proposal_id) REFERENCES edit_proposals (tenant_id, id) ON DELETE SET NULL
);
CREATE INDEX idx_edit_operations_proposal ON edit_operations (tenant_id, proposal_id, sequence_index);

CREATE TABLE timeline_comments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  timeline_id uuid NOT NULL,
  timeline_version integer NOT NULL,
  timecode_tick bigint CHECK (timecode_tick IS NULL OR timecode_tick >= 0),
  author_id uuid NOT NULL REFERENCES users(id),
  body text NOT NULL,
  resolved_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, timeline_id) REFERENCES timelines (tenant_id, id) ON DELETE CASCADE
);
