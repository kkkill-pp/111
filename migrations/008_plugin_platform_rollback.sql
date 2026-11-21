-- Phase 6 Stage 2: Symmetric Rollback for 008_plugin_platform_rls.sql

BEGIN;

DROP POLICY IF EXISTS tenant_isolation_anchored_review_comments ON public.anchored_review_comments;
DROP POLICY IF EXISTS tenant_isolation_tenant_plugin_policies ON public.tenant_plugin_policies;

DROP TABLE IF EXISTS public.anchored_review_comments;
DROP TABLE IF EXISTS public.tenant_plugin_policies;

COMMIT;
