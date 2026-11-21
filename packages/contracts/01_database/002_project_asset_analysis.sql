-- 002_project_asset_analysis.sql
-- PostgreSQL 15+ | AI Video Studio HITL v2.0
-- Projects, Upload Sessions, Assets, Media Probes, Analysis Runs, and separated
-- understanding entities (Transcript, Scene, SemanticSegment, HighlightCandidate).

CREATE TABLE projects (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  workspace_id uuid NOT NULL,
  name text NOT NULL CHECK (length(trim(name)) > 0),
  description text,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived', 'deleted')),
  default_profile_json jsonb NOT NULL DEFAULT '{
    "width": 1080,
    "height": 1920,
    "frame_rate": {"num": 30, "den": 1},
    "timeline_timebase": {"num": 1, "den": 30000},
    "audio_sample_rate": 48000
  }'::jsonb,
  creative_defaults_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  settings_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_by uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  archived_at timestamptz,
  deleted_at timestamptz,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces (tenant_id, id)
);
CREATE INDEX idx_projects_tenant_ws ON projects (tenant_id, workspace_id, created_at DESC);
CREATE INDEX idx_projects_tenant_status ON projects (tenant_id, status);

CREATE TABLE upload_sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  project_id uuid NOT NULL,
  created_by uuid NOT NULL REFERENCES users(id),
  filename text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('video', 'audio', 'image')),
  expected_size_bytes bigint NOT NULL CHECK (expected_size_bytes > 0),
  expected_checksum_sha256 char(64),
  storage_key text NOT NULL,
  multipart_upload_id text,
  status text NOT NULL DEFAULT 'initiated' CHECK (status IN ('initiated', 'uploading', 'completed', 'aborted', 'expired')),
  idempotency_key text NOT NULL,
  expires_at timestamptz NOT NULL,
  completed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, idempotency_key),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, id)
);
CREATE INDEX idx_upload_sessions_project ON upload_sessions (tenant_id, project_id, created_at DESC);

CREATE TABLE assets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  project_id uuid NOT NULL,
  upload_session_id uuid,
  kind text NOT NULL CHECK (kind IN ('video', 'audio', 'image')),
  status text NOT NULL CHECK (status IN (
    'uploading', 'uploaded', 'probing', 'ready',
    'analyzing', 'analyzed', 'failed', 'quarantined', 'deleted'
  )),
  original_filename text NOT NULL,
  storage_key text NOT NULL,
  checksum_sha256 char(64),
  size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
  duration_num bigint,
  duration_den bigint CHECK (duration_den IS NULL OR duration_den > 0),
  is_vfr boolean,
  probe_json jsonb,
  created_by uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, id),
  FOREIGN KEY (tenant_id, upload_session_id) REFERENCES upload_sessions (tenant_id, id)
);
CREATE INDEX idx_assets_project ON assets (tenant_id, project_id, created_at DESC);
CREATE INDEX idx_assets_checksum ON assets (tenant_id, checksum_sha256);

CREATE TABLE media_probes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  asset_id uuid NOT NULL,
  probe_tool_version text NOT NULL,
  parser_version text NOT NULL,
  container_format text NOT NULL,
  duration_num bigint NOT NULL,
  duration_den bigint NOT NULL CHECK (duration_den > 0),
  width integer,
  height integer,
  frame_rate_num integer,
  frame_rate_den integer CHECK (frame_rate_den IS NULL OR frame_rate_den > 0),
  time_base_num integer,
  time_base_den integer CHECK (time_base_den IS NULL OR time_base_den > 0),
  is_vfr boolean NOT NULL DEFAULT false,
  pts_map_storage_key text,
  audio_sample_rate integer,
  audio_channels integer,
  rotation integer NOT NULL DEFAULT 0,
  color_space text,
  raw_probe_json jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, asset_id) REFERENCES assets (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_media_probes_asset ON media_probes (tenant_id, asset_id, created_at DESC);

CREATE TABLE analysis_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  project_id uuid NOT NULL,
  asset_id uuid NOT NULL,
  run_type text NOT NULL CHECK (run_type IN (
    'probe', 'proxy', 'thumbnail', 'asr', 'scene_detection',
    'keyframes', 'person_detection', 'visual_analysis',
    'semantic_fusion', 'candidate_generation', 'full_pipeline'
  )),
  status text NOT NULL CHECK (status IN ('pending', 'running', 'succeeded', 'partial', 'failed', 'cancelled')),
  provider text,
  model text,
  model_version text,
  prompt_version text,
  pipeline_version text NOT NULL,
  input_checksum char(64) NOT NULL,
  output_json jsonb,
  error_json jsonb,
  cost_json jsonb,
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, id),
  FOREIGN KEY (tenant_id, asset_id) REFERENCES assets (tenant_id, id)
);
CREATE INDEX idx_analysis_runs_asset ON analysis_runs (tenant_id, asset_id, run_type, created_at DESC);

-- Separated media understanding tables (no conflation of Scene / Speaker / Segment / Candidate)
CREATE TABLE transcripts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  asset_id uuid NOT NULL,
  analysis_run_id uuid NOT NULL,
  language text NOT NULL,
  timebase_num integer NOT NULL DEFAULT 1,
  timebase_den integer NOT NULL DEFAULT 30000 CHECK (timebase_den > 0),
  full_text text NOT NULL,
  words_storage_key text,
  word_count integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, asset_id) REFERENCES assets (tenant_id, id),
  FOREIGN KEY (tenant_id, analysis_run_id) REFERENCES analysis_runs (tenant_id, id)
);

CREATE TABLE scenes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  asset_id uuid NOT NULL,
  analysis_run_id uuid NOT NULL,
  scene_index integer NOT NULL CHECK (scene_index >= 0),
  start_tick bigint NOT NULL CHECK (start_tick >= 0),
  end_tick bigint NOT NULL CHECK (end_tick > start_tick),
  timebase_num integer NOT NULL DEFAULT 1,
  timebase_den integer NOT NULL DEFAULT 30000 CHECK (timebase_den > 0),
  keyframe_storage_key text,
  visual_summary_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, analysis_run_id, scene_index),
  FOREIGN KEY (tenant_id, asset_id) REFERENCES assets (tenant_id, id),
  FOREIGN KEY (tenant_id, analysis_run_id) REFERENCES analysis_runs (tenant_id, id)
);

CREATE TABLE semantic_segments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  asset_id uuid NOT NULL,
  analysis_run_id uuid NOT NULL,
  start_tick bigint NOT NULL CHECK (start_tick >= 0),
  end_tick bigint NOT NULL CHECK (end_tick > start_tick),
  timebase_num integer NOT NULL DEFAULT 1,
  timebase_den integer NOT NULL DEFAULT 30000 CHECK (timebase_den > 0),
  topic text NOT NULL,
  summary text NOT NULL,
  speaker_label text,
  context_dependency_flag boolean NOT NULL DEFAULT false,
  evidence_refs_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, asset_id) REFERENCES assets (tenant_id, id),
  FOREIGN KEY (tenant_id, analysis_run_id) REFERENCES analysis_runs (tenant_id, id)
);
CREATE INDEX idx_semantic_segments_asset ON semantic_segments (tenant_id, asset_id, start_tick);

CREATE TABLE highlight_candidates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  project_id uuid NOT NULL,
  asset_id uuid NOT NULL,
  analysis_run_id uuid NOT NULL,
  start_tick bigint NOT NULL CHECK (start_tick >= 0),
  end_tick bigint NOT NULL CHECK (end_tick > start_tick),
  timebase_num integer NOT NULL DEFAULT 1,
  timebase_den integer NOT NULL DEFAULT 30000 CHECK (timebase_den > 0),
  title text NOT NULL,
  hook_text text,
  score_breakdown_json jsonb NOT NULL,
  evidence_refs_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  risk_flags_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  confidence numeric(4,3) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects (tenant_id, id),
  FOREIGN KEY (tenant_id, asset_id) REFERENCES assets (tenant_id, id),
  FOREIGN KEY (tenant_id, analysis_run_id) REFERENCES analysis_runs (tenant_id, id)
);
CREATE INDEX idx_highlight_candidates_project ON highlight_candidates (tenant_id, project_id, created_at DESC);
