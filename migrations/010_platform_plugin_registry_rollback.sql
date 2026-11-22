-- Sprint 4 rollback. Run only when platform plugin catalog data is intentionally discarded.
BEGIN;
DROP TABLE IF EXISTS public.platform_plugin_events;
DROP TABLE IF EXISTS public.platform_plugin_versions;
DROP TABLE IF EXISTS public.platform_plugins;
DROP FUNCTION IF EXISTS public.reject_platform_plugin_immutable_mutation();
-- Keep the NOLOGIN role: it may be shared by a separately deployed registry service.
COMMIT;
