-- 007_v3_protocol_upgrade.sql
-- PostgreSQL 15+ | AI Video Studio CR-001: Non-Destructive In-Place v2.2 -> v3.0 Protocol Upgrade
-- Preserves all 39 tenant-owned tables, composite FKs, FORCE RLS, and 6-way Fencing Token guards.
-- NO database rebuild required. Safe backup stored in `migration_backup` schema for rollback.

BEGIN;

CREATE SCHEMA IF NOT EXISTS migration_backup;

-- 1. Pre-migration snapshots for safe rollback (stored outside public schema so public RLS parity is preserved)
CREATE TABLE IF NOT EXISTS migration_backup.v22_timeline_versions AS
  SELECT id, tenant_id, timeline_id, version, snapshot_json, snapshot_checksum, origin_command
    FROM public.timeline_versions
   WHERE false;

INSERT INTO migration_backup.v22_timeline_versions
SELECT id, tenant_id, timeline_id, version, snapshot_json, snapshot_checksum, origin_command
  FROM public.timeline_versions tv
 WHERE NOT EXISTS (SELECT 1 FROM migration_backup.v22_timeline_versions b WHERE b.id = tv.id);

CREATE TABLE IF NOT EXISTS migration_backup.v22_edit_proposals AS
  SELECT id, tenant_id, status, validation_json
    FROM public.edit_proposals
   WHERE false;

INSERT INTO migration_backup.v22_edit_proposals
SELECT id, tenant_id, status, validation_json
  FROM public.edit_proposals ep
 WHERE NOT EXISTS (SELECT 1 FROM migration_backup.v22_edit_proposals b WHERE b.id = ep.id);

CREATE TABLE IF NOT EXISTS migration_backup.v22_edit_operations AS
  SELECT id, tenant_id, operation_type, payload_json
    FROM public.edit_operations
   WHERE false;

INSERT INTO migration_backup.v22_edit_operations
SELECT id, tenant_id, operation_type, payload_json
  FROM public.edit_operations eo
 WHERE NOT EXISTS (SELECT 1 FROM migration_backup.v22_edit_operations b WHERE b.id = eo.id);

-- 2. Timeline v3.0 in-place snapshot migration helper (speed -> playback_rate, default keyframes/effects/markers, time_semantics)
CREATE OR REPLACE FUNCTION public.migrate_timeline_snapshot_v2_to_v3(p_snap jsonb)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
  v_tracks jsonb := '[]'::jsonb;
  v_track jsonb;
  v_new_track jsonb;
  v_clips jsonb;
  v_clip jsonb;
  v_new_clip jsonb;
  v_rate jsonb;
  v_time_semantics jsonb;
BEGIN
  IF p_snap IS NULL OR (p_snap->>'schema_version') = '3.0' THEN
    RETURN p_snap;
  END IF;

  v_time_semantics := jsonb_build_object(
    'unit', 'ticks',
    'timeline_timebase', COALESCE(p_snap->'profile'->'timeline_timebase', '{"num":1,"den":30000}'::jsonb),
    'frame_rate', COALESCE(p_snap->'profile'->'frame_rate', '{"num":30,"den":1}'::jsonb),
    'rounding_policy', 'half_to_even',
    'coordinate_model', 'half_open_source_and_project_ticks'
  );

  FOR v_track IN SELECT value FROM jsonb_array_elements(COALESCE(p_snap->'tracks', '[]'::jsonb))
  LOOP
    IF (v_track->>'type') IN ('video', 'audio') THEN
      v_clips := '[]'::jsonb;
      FOR v_clip IN SELECT value FROM jsonb_array_elements(COALESCE(v_track->'clips', '[]'::jsonb))
      LOOP
        v_rate := COALESCE(v_clip->'playback_rate', v_clip->'speed', '1.0'::jsonb);
        v_new_clip := (v_clip - 'speed')
          || jsonb_build_object(
               'playback_rate', v_rate,
               'keyframes', COALESCE(v_clip->'keyframes', '[]'::jsonb),
               'effects', COALESCE(v_clip->'effects', '[]'::jsonb)
             );
        v_clips := v_clips || jsonb_build_array(v_new_clip);
      END LOOP;
      v_new_track := jsonb_set(v_track, '{clips}', v_clips, true);
    ELSE
      v_new_track := v_track;
    END IF;
    v_tracks := v_tracks || jsonb_build_array(v_new_track);
  END LOOP;

  RETURN p_snap
    || jsonb_build_object(
         'schema_version', '3.0',
         'time_semantics', v_time_semantics,
         'tracks', v_tracks,
         'markers', COALESCE(p_snap->'markers', '[]'::jsonb)
       );
END;
$$;

-- 3. Upgrade timeline_versions (origin_command + undo_scope + snapshot v2->v3)
ALTER TABLE public.timeline_versions
  ADD COLUMN IF NOT EXISTS undo_scope text NOT NULL DEFAULT 'user_edit';

ALTER TABLE public.timeline_versions
  DROP CONSTRAINT IF EXISTS timeline_versions_origin_command_check,
  ADD CONSTRAINT timeline_versions_origin_command_check
    CHECK (origin_command IN ('init', 'storyboard_compile', 'edit', 'proposal_apply', 'undo', 'redo'));

ALTER TABLE public.timeline_versions
  DROP CONSTRAINT IF EXISTS timeline_versions_undo_scope_check,
  ADD CONSTRAINT timeline_versions_undo_scope_check
    CHECK (undo_scope IN ('user_edit', 'proposal_batch', 'non_undoable_system'));

UPDATE public.timeline_versions
   SET snapshot_json = public.migrate_timeline_snapshot_v2_to_v3(snapshot_json)
 WHERE snapshot_json->>'schema_version' = '2.0';

-- 4. Upgrade edit_operations to 21 canonical v3.0 operations (UNDO/REDO strictly excluded)
ALTER TABLE public.edit_operations
  ADD COLUMN IF NOT EXISTS undo_scope text NOT NULL DEFAULT 'user_edit',
  ADD COLUMN IF NOT EXISTS agent_permission_level text;

ALTER TABLE public.edit_operations
  DROP CONSTRAINT IF EXISTS edit_operations_operation_type_check;

UPDATE public.edit_operations
   SET operation_type = 'UPDATE_PLAYBACK_RATE',
       payload_json = CASE
         WHEN payload_json ? 'speed' AND NOT (payload_json ? 'playback_rate')
           THEN (payload_json - 'speed') || jsonb_build_object('playback_rate', payload_json->'speed')
         ELSE payload_json
       END
 WHERE operation_type = 'UPDATE_SPEED';

ALTER TABLE public.edit_operations
  ADD CONSTRAINT edit_operations_operation_type_check CHECK (operation_type IN (
    'ADD_CLIP', 'REMOVE_CLIP', 'TRIM_CLIP', 'MOVE_CLIP', 'SPLIT_CLIP',
    'MERGE_CLIP', 'REORDER_CLIP', 'UPDATE_TRANSFORM', 'UPDATE_PLAYBACK_RATE',
    'UPDATE_VOLUME', 'ADD_SUBTITLE', 'UPDATE_SUBTITLE', 'REMOVE_SUBTITLE',
    'ADD_TRANSITION', 'REMOVE_TRANSITION', 'ADD_OVERLAY', 'REMOVE_OVERLAY',
    'SET_CANVAS_PROFILE', 'ADD_KEYFRAME', 'APPLY_EFFECT', 'ADD_MARKER'
  ));

ALTER TABLE public.edit_operations
  DROP CONSTRAINT IF EXISTS edit_operations_undo_scope_check,
  ADD CONSTRAINT edit_operations_undo_scope_check
    CHECK (undo_scope IN ('user_edit', 'proposal_batch', 'non_undoable_system'));

ALTER TABLE public.edit_operations
  DROP CONSTRAINT IF EXISTS edit_operations_agent_permission_level_check,
  ADD CONSTRAINT edit_operations_agent_permission_level_check
    CHECK (agent_permission_level IS NULL OR agent_permission_level IN ('L0', 'L1', 'L2', 'L3'));

-- 5. Upgrade edit_proposals to v3.0 snake_case lifecycle (10 states) + L1-L3 permission + base_timeline_checksum
ALTER TABLE public.edit_proposals
  ADD COLUMN IF NOT EXISTS agent_permission_level text NOT NULL DEFAULT 'L1',
  ADD COLUMN IF NOT EXISTS base_timeline_checksum char(64);

ALTER TABLE public.edit_proposals
  DROP CONSTRAINT IF EXISTS edit_proposals_status_check;

UPDATE public.edit_proposals
   SET validation_json = validation_json || jsonb_build_object('legacy_v22_status', status),
       status = CASE status
         WHEN 'validating' THEN 'submitted'
         WHEN 'awaiting_review' THEN 'reviewing'
         WHEN 'superseded' THEN 'cancelled'
         WHEN 'invalid' THEN 'cancelled'
         ELSE status
       END
 WHERE status IN ('validating', 'awaiting_review', 'superseded', 'invalid');

ALTER TABLE public.edit_proposals
  ADD CONSTRAINT edit_proposals_status_check CHECK (status IN (
    'draft', 'submitted', 'reviewing', 'approved', 'applied',
    'rejected', 'revision_requested', 'conflicted', 'expired', 'cancelled'
  ));

ALTER TABLE public.edit_proposals
  DROP CONSTRAINT IF EXISTS edit_proposals_agent_permission_level_check,
  ADD CONSTRAINT edit_proposals_agent_permission_level_check
    CHECK (agent_permission_level IN ('L1', 'L2', 'L3'));

-- 6. Enforce RenderPlan immutability once locked/queued (Risk #3)
ALTER TABLE public.render_plans
  ADD COLUMN IF NOT EXISTS immutable_locked_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE public.render_plans
  ALTER COLUMN plan_version SET DEFAULT '3.0';

CREATE OR REPLACE FUNCTION public.enforce_render_plan_immutability()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'RenderPlan % is immutable once created/locked; create a new RenderPlan for timeline changes', OLD.id
    USING ERRCODE = 'check_violation';
END;
$$;

DROP TRIGGER IF EXISTS trg_render_plans_immutable ON public.render_plans;
CREATE TRIGGER trg_render_plans_immutable
  BEFORE UPDATE OR DELETE ON public.render_plans
  FOR EACH ROW
  EXECUTE FUNCTION public.enforce_render_plan_immutability();

-- 7. Upgrade approval_tokens to 5-dimensional binding (tenant, project, proposal, timeline_version, proposal_checksum + timeline_checksum)
ALTER TABLE public.approval_tokens
  ADD COLUMN IF NOT EXISTS project_id uuid,
  ADD COLUMN IF NOT EXISTS timeline_checksum char(64);

CREATE OR REPLACE FUNCTION public.consume_approval_token_for_timeline_v3(
  p_tenant_id uuid,
  p_project_id uuid,
  p_timeline_id uuid,
  p_proposal_id uuid,
  p_expected_base_version integer,
  p_expected_proposal_checksum char(64),
  p_expected_timeline_checksum char(64),
  p_token_hash char(64),
  p_issued_to_user_id uuid
)
RETURNS uuid
LANGUAGE plpgsql
AS $$
DECLARE
  v_current_version integer;
  v_live_checksum char(64);
  v_review_id uuid;
BEGIN
  -- Serialize against timeline advancement first.
  SELECT t.current_version, tv.snapshot_checksum
    INTO v_current_version, v_live_checksum
    FROM public.timelines AS t
    JOIN public.timeline_versions AS tv
      ON tv.tenant_id = t.tenant_id
     AND tv.timeline_id = t.id
     AND tv.version = t.current_version
   WHERE t.tenant_id = p_tenant_id
     AND t.project_id = p_project_id
     AND t.id = p_timeline_id
   FOR UPDATE OF t;

  IF NOT FOUND
     OR v_current_version <> p_expected_base_version
     OR v_live_checksum <> p_expected_timeline_checksum THEN
    RETURN NULL;
  END IF;

  -- Lock token, human_review, and edit_proposal atomically.
  SELECT tok.human_review_id
    INTO v_review_id
    FROM public.approval_tokens AS tok
    JOIN public.human_reviews AS rev
      ON rev.tenant_id = tok.tenant_id
     AND rev.id = tok.human_review_id
    JOIN public.edit_proposals AS prop
      ON prop.tenant_id = tok.tenant_id
     AND prop.id = tok.subject_id
   WHERE tok.tenant_id = p_tenant_id
     AND (tok.project_id IS NULL OR tok.project_id = p_project_id)
     AND tok.subject_type = 'edit_proposal'
     AND tok.subject_id = p_proposal_id
     AND tok.bound_version = p_expected_base_version::text
     AND tok.bound_checksum = p_expected_proposal_checksum
     AND (tok.timeline_checksum IS NULL OR tok.timeline_checksum = p_expected_timeline_checksum)
     AND tok.token_hash = p_token_hash
     AND tok.issued_to_user_id = p_issued_to_user_id
     AND tok.consumed_at IS NULL
     AND tok.expires_at > clock_timestamp()
     AND rev.status IN ('approved', 'approved_with_warnings')
     AND rev.project_id = p_project_id
     AND rev.subject_type = 'edit_proposal'
     AND rev.subject_id = p_proposal_id
     AND rev.subject_version = p_expected_base_version::text
     AND rev.subject_checksum = p_expected_proposal_checksum
     AND prop.timeline_id = p_timeline_id
     AND prop.base_version = p_expected_base_version
     AND prop.proposal_checksum = p_expected_proposal_checksum
     AND prop.status = 'approved'
   FOR UPDATE OF tok, rev, prop;

  IF NOT FOUND THEN
    RETURN NULL;
  END IF;

  UPDATE public.approval_tokens
     SET consumed_at = clock_timestamp()
   WHERE tenant_id = p_tenant_id
     AND human_review_id = v_review_id
     AND subject_id = p_proposal_id
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

COMMIT;
