-- Phase 6 Stage 2: Plugin Platform Multi-Tenant Persistence (PostgreSQL 15+)
-- Enforces composite (tenant_id, ...) keys/FKs and ENABLE + FORCE ROW LEVEL SECURITY.

BEGIN;

CREATE TABLE IF NOT EXISTS public.tenant_plugin_policies (
    tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    plugin_id VARCHAR(120) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, plugin_id)
);

CREATE TABLE IF NOT EXISTS public.anchored_review_comments (
    id UUID NOT NULL,
    tenant_id UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    timeline_id UUID NOT NULL,
    timeline_version INTEGER NOT NULL CHECK (timeline_version >= 1),
    at_tick BIGINT NOT NULL CHECK (at_tick >= 0),
    author_id UUID NOT NULL,
    comment TEXT NOT NULL CHECK (length(trim(comment)) > 0),
    suggested_action VARCHAR(120),
    status VARCHAR(24) NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    resolved_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, id),
    CONSTRAINT fk_anchored_review_comments_timeline
        FOREIGN KEY (tenant_id, timeline_id)
        REFERENCES public.timelines(tenant_id, id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_anchored_review_comments_timeline
    ON public.anchored_review_comments (tenant_id, timeline_id, at_tick);

-- Mandatory Multi-Tenant Row-Level Security (ENABLE + FORCE RLS)
ALTER TABLE public.tenant_plugin_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenant_plugin_policies FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_tenant_plugin_policies ON public.tenant_plugin_policies;
CREATE POLICY tenant_isolation_tenant_plugin_policies
    ON public.tenant_plugin_policies
    FOR ALL
    TO app_runtime
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE public.anchored_review_comments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.anchored_review_comments FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_anchored_review_comments ON public.anchored_review_comments;
CREATE POLICY tenant_isolation_anchored_review_comments
    ON public.anchored_review_comments
    FOR ALL
    TO app_runtime
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON public.tenant_plugin_policies TO app_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.anchored_review_comments TO app_runtime;

COMMIT;
