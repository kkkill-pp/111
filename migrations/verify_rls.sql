-- verify_rls.sql (CR-001 Fixed)
-- Automated PostgreSQL 15+ verification suite for:
-- 1. Every tenant-owned table in public has ENABLE + FORCE ROW LEVEL SECURITY
-- 2. Non-superuser (app_runtime, NOBYPASSRLS) fail-closed check on NON-EMPTY projects table
-- 3. Strict cross-tenant isolation between Tenant A and Tenant B under app_runtime
-- Runs inside a rolled-back transaction so target DB state is never polluted.

BEGIN;

-- Test 1: Assert every tenant-owned table in public has relrowsecurity = true AND relforcerowsecurity = true
DO $$
DECLARE
  missing_count integer;
BEGIN
  SELECT count(*) INTO missing_count
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public'
     AND c.relkind = 'r'
     AND c.relname NOT IN ('tenants', 'users', 'workers', 'prompt_versions')
     AND (NOT c.relrowsecurity OR NOT c.relforcerowsecurity);

  IF missing_count > 0 THEN
    RAISE EXCEPTION 'RLS enforcement check failed: % tenant tables missing ENABLE/FORCE RLS', missing_count;
  END IF;
END $$;

-- Seed 2 tenants, 2 users, 2 workspaces, and 2 projects BEFORE switching to app_runtime
-- so Test 2 & Test 3 prove non-vacuous fail-closed and cross-tenant isolation.
DO $$
DECLARE
  v_t_a uuid := '11111111-1111-4111-8111-111111111111';
  v_t_b uuid := '22222222-2222-4222-8222-222222222222';
  v_u_a uuid := '33333333-3333-4333-8333-333333333333';
  v_u_b uuid := '44444444-4444-4444-8444-444444444444';
  v_w_a uuid := '55555555-5555-4555-8555-555555555555';
  v_w_b uuid := '66666666-6666-4666-8666-666666666666';
BEGIN
  INSERT INTO public.tenants (id, name) VALUES
    (v_t_a, 'RLS Tenant A'),
    (v_t_b, 'RLS Tenant B')
  ON CONFLICT DO NOTHING;

  INSERT INTO public.users (id, auth_subject, email, display_name) VALUES
    (v_u_a, 'auth|rls-a', 'rls-a@example.com', 'RLS User A'),
    (v_u_b, 'auth|rls-b', 'rls-b@example.com', 'RLS User B')
  ON CONFLICT DO NOTHING;

  PERFORM set_config('app.tenant_id', v_t_a::text, true);
  INSERT INTO public.workspaces (id, tenant_id, name, created_by)
  VALUES (v_w_a, v_t_a, 'WS A', v_u_a) ON CONFLICT DO NOTHING;
  INSERT INTO public.projects (id, tenant_id, workspace_id, name, created_by)
  VALUES ('77777777-7777-4777-8777-777777777777', v_t_a, v_w_a, 'Proj A', v_u_a) ON CONFLICT DO NOTHING;

  PERFORM set_config('app.tenant_id', v_t_b::text, true);
  INSERT INTO public.workspaces (id, tenant_id, name, created_by)
  VALUES (v_w_b, v_t_b, 'WS B', v_u_b) ON CONFLICT DO NOTHING;
  INSERT INTO public.projects (id, tenant_id, workspace_id, name, created_by)
  VALUES ('88888888-8888-4888-8888-888888888888', v_t_b, v_w_b, 'Proj B', v_u_b) ON CONFLICT DO NOTHING;
END $$;

-- CR-001: Explicitly switch to non-superuser NOBYPASSRLS role before testing fail-closed & isolation
SET LOCAL ROLE app_runtime;

-- Test 2: Missing app.tenant_id context returns zero rows on NON-EMPTY projects table (fail-closed)
RESET app.tenant_id;
DO $$
DECLARE
  cnt integer;
BEGIN
  SELECT count(*) INTO cnt FROM public.projects;
  IF cnt <> 0 THEN
    RAISE EXCEPTION 'Fail-closed RLS check failed under app_runtime: expected 0 projects without tenant context, got %', cnt;
  END IF;
END $$;

-- Test 3: Tenant A sees strictly 1 project (its own) and zero Tenant B rows
SELECT set_config('app.tenant_id', '11111111-1111-4111-8111-111111111111', true);
DO $$
DECLARE
  cnt integer;
  leaked integer;
BEGIN
  SELECT count(*) INTO cnt FROM public.projects;
  SELECT count(*) INTO leaked FROM public.projects WHERE tenant_id <> '11111111-1111-4111-8111-111111111111'::uuid;
  IF cnt <> 1 OR leaked <> 0 THEN
    RAISE EXCEPTION 'Tenant A RLS isolation failed under app_runtime: cnt=%, leaked=%', cnt, leaked;
  END IF;
END $$;

-- Test 4: Tenant B sees strictly 1 project (its own) and zero Tenant A rows
SELECT set_config('app.tenant_id', '22222222-2222-4222-8222-222222222222', true);
DO $$
DECLARE
  cnt integer;
  leaked integer;
BEGIN
  SELECT count(*) INTO cnt FROM public.projects;
  SELECT count(*) INTO leaked FROM public.projects WHERE tenant_id <> '22222222-2222-4222-8222-222222222222'::uuid;
  IF cnt <> 1 OR leaked <> 0 THEN
    RAISE EXCEPTION 'Tenant B RLS isolation failed under app_runtime: cnt=%, leaked=%', cnt, leaked;
  END IF;
END $$;

RESET ROLE;
ROLLBACK;
