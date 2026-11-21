-- 007_v3_protocol_rollback.sql
-- PostgreSQL 15+ | Safe In-Place Rollback for 007_v3_protocol_upgrade.sql (CR-001)
-- Restores v2.2 snapshots, proposal statuses, and operation types from migration_backup without rebuilding DB.

BEGIN;

DROP TRIGGER IF EXISTS trg_render_plans_immutable ON public.render_plans;
DROP FUNCTION IF EXISTS public.enforce_render_plan_immutability();
DROP FUNCTION IF EXISTS public.consume_approval_token_for_timeline_v3(uuid, uuid, uuid, uuid, integer, char, char, char, uuid);

-- Restore backed-up timeline snapshots
UPDATE public.timeline_versions tv
   SET snapshot_json = b.snapshot_json,
       origin_command = b.origin_command
  FROM migration_backup.v22_timeline_versions b
 WHERE tv.id = b.id AND tv.tenant_id = b.tenant_id;

-- Restore backed-up proposal statuses
ALTER TABLE public.edit_proposals DROP CONSTRAINT IF EXISTS edit_proposals_status_check;
UPDATE public.edit_proposals ep
   SET status = b.status,
       validation_json = b.validation_json
  FROM migration_backup.v22_edit_proposals b
 WHERE ep.id = b.id AND ep.tenant_id = b.tenant_id;

ALTER TABLE public.edit_proposals
  ADD CONSTRAINT edit_proposals_status_check CHECK (status IN (
    'draft', 'validating', 'awaiting_review', 'approved', 'applied',
    'rejected', 'expired', 'superseded', 'invalid'
  ));

-- Restore backed-up operation types
ALTER TABLE public.edit_operations DROP CONSTRAINT IF EXISTS edit_operations_operation_type_check;
UPDATE public.edit_operations eo
   SET operation_type = b.operation_type,
       payload_json = b.payload_json
  FROM migration_backup.v22_edit_operations b
 WHERE eo.id = b.id AND eo.tenant_id = b.tenant_id;

ALTER TABLE public.edit_operations
  ADD CONSTRAINT edit_operations_operation_type_check CHECK (operation_type IN (
    'ADD_CLIP', 'REMOVE_CLIP', 'TRIM_CLIP', 'MOVE_CLIP', 'SPLIT_CLIP',
    'MERGE_CLIP', 'REORDER_CLIP', 'UPDATE_TRANSFORM', 'UPDATE_SPEED',
    'UPDATE_VOLUME', 'ADD_SUBTITLE', 'UPDATE_SUBTITLE', 'REMOVE_SUBTITLE',
    'ADD_TRANSITION', 'REMOVE_TRANSITION', 'ADD_OVERLAY', 'REMOVE_OVERLAY',
    'SET_CANVAS_PROFILE'
  ));

COMMIT;
