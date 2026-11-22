-- Sprint 4: Global, tenant-independent plugin catalog and immutable release registry.
-- The registry is deliberately separate from tenant_plugin_policies (008).
BEGIN;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plugin_registry_runtime') THEN
    CREATE ROLE plugin_registry_runtime NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS NOLOGIN;
  END IF;
END $$;
ALTER ROLE plugin_registry_runtime NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS NOLOGIN;

CREATE TABLE IF NOT EXISTS public.platform_plugins (
  plugin_id VARCHAR(120) PRIMARY KEY,
  publisher VARCHAR(160) NOT NULL,
  display_name VARCHAR(200) NOT NULL,
  category VARCHAR(40) NOT NULL CHECK (category IN ('template','review','renderer','agent_tool','media_gen','export')),
  license_id VARCHAR(80) NOT NULL,
  license_status VARCHAR(24) NOT NULL DEFAULT 'license_review_pending'
    CHECK (license_status IN ('license_review_pending','approved','rejected')),
  globally_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  created_by VARCHAR(160) NOT NULL,
  CHECK (NOT globally_enabled OR license_status = 'approved')
);

CREATE TABLE IF NOT EXISTS public.platform_plugin_versions (
  plugin_id VARCHAR(120) NOT NULL REFERENCES public.platform_plugins(plugin_id) ON DELETE RESTRICT,
  version VARCHAR(64) NOT NULL,
  sha256 CHAR(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  manifest_json JSONB NOT NULL CHECK (jsonb_typeof(manifest_json) = 'object'),
  artifact_uri TEXT NOT NULL CHECK (length(trim(artifact_uri)) > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  created_by VARCHAR(160) NOT NULL,
  PRIMARY KEY (plugin_id, version),
  UNIQUE (plugin_id, version, sha256)
);

CREATE TABLE IF NOT EXISTS public.platform_plugin_events (
  event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  plugin_id VARCHAR(120) NOT NULL,
  version VARCHAR(64),
  event_type VARCHAR(48) NOT NULL CHECK (event_type IN ('registered','license_approved','license_rejected','enabled','disabled')),
  actor VARCHAR(160) NOT NULL,
  details_json JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(details_json) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_platform_plugin_events_plugin_time
  ON public.platform_plugin_events(plugin_id, created_at DESC);

-- Release records and governance events are append-only. A new release means a new version row.
CREATE OR REPLACE FUNCTION public.reject_platform_plugin_immutable_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'platform plugin release/event rows are immutable' USING ERRCODE = '55000';
END $$;
DROP TRIGGER IF EXISTS trg_plugin_versions_immutable ON public.platform_plugin_versions;
CREATE TRIGGER trg_plugin_versions_immutable BEFORE UPDATE OR DELETE ON public.platform_plugin_versions
  FOR EACH ROW EXECUTE FUNCTION public.reject_platform_plugin_immutable_mutation();
DROP TRIGGER IF EXISTS trg_plugin_events_immutable ON public.platform_plugin_events;
CREATE TRIGGER trg_plugin_events_immutable BEFORE UPDATE OR DELETE ON public.platform_plugin_events
  FOR EACH ROW EXECUTE FUNCTION public.reject_platform_plugin_immutable_mutation();

-- This role is used only by the platform-registry service. It is not granted to app_runtime.
GRANT SELECT ON public.platform_plugins TO plugin_registry_runtime;
GRANT INSERT (plugin_id,publisher,display_name,category,license_id,created_by) ON public.platform_plugins TO plugin_registry_runtime;
GRANT UPDATE (license_status,globally_enabled,updated_at) ON public.platform_plugins TO plugin_registry_runtime;
GRANT SELECT, INSERT ON public.platform_plugin_versions, public.platform_plugin_events TO plugin_registry_runtime;
GRANT USAGE, SELECT ON SEQUENCE public.platform_plugin_events_event_id_seq TO plugin_registry_runtime;
REVOKE ALL ON public.platform_plugins, public.platform_plugin_versions, public.platform_plugin_events FROM app_runtime;

COMMIT;
