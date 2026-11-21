-- 000_roles_and_extensions.sql
-- PostgreSQL 15+ | AI Video Studio HITL v2.1
-- Strictly separates normal runtime/worker privileges (FORCE RLS enforced)
-- from the minimal cross-tenant task scheduler role (SECURITY DEFINER owner only).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$
BEGIN
  -- Normal API & Worker execution role: NEVER bypasses RLS.
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
    CREATE ROLE app_runtime NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS NOLOGIN;
  END IF;

  -- Isolated scheduler role: ONLY owns SECURITY DEFINER dispatch/reclaim functions.
  -- Normal workers CANNOT assume this role and have ZERO direct cross-tenant table access.
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'task_scheduler_role') THEN
    CREATE ROLE task_scheduler_role NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS NOLOGIN;
  END IF;
END $$;
