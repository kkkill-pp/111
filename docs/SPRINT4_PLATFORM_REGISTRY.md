# Sprint 4 — Global Platform Plugin Registry

## Boundary

- `platform_plugins`, `platform_plugin_versions`, and `platform_plugin_events` are platform-global catalog tables. They are deliberately separate from tenant-scoped `tenant_plugin_policies` in migration 008.
- Tenant owners/admins can only change their own tenant policy through `/api/v1/plugins/{plugin_id}/enable|disable` or `/api/v1/tenant/plugins/{plugin_id}`. They cannot approve a license or change global availability.
- Third-party packages discovered on disk are registered disabled. Their runtime is gated against the persistent global catalog on every invocation; all API instances observe a platform disable/revoke without waiting for a process-local cache refresh.
- Built-in first-party plugins remain governed by their in-process signed/trusted application release; catalog-managed third-party plugins require both platform approval/global enablement and tenant enablement.
- Platform registry API operations require `X-Platform-Admin-Token` and `X-Platform-Admin-Actor`. The actor value is written to the append-only event log.
- A newly registered plugin is `license_review_pending` and `globally_enabled=false`. Enabling requires `license_status=approved`; approvals are additionally constrained by the current LIC-001 allowlist in code.
- Release rows are immutable. A changed artifact requires a new SemVer version and a new SHA-256 digest.

## Migration

Apply after migrations 000–008:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/010_platform_plugin_registry.sql
```

Rollback is destructive to catalog data:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/010_platform_plugin_registry_rollback.sql
```

The CI workflow executes up → down → up against PostgreSQL 15.

## Runtime role

Migration 010 creates `plugin_registry_runtime` as `NOLOGIN NOBYPASSRLS` and grants it only the catalog privileges. It is not granted to `app_runtime`. Create a dedicated login using the deployment's secret-management process and grant the runtime role to it:

```sql
GRANT plugin_registry_runtime TO aivs_platform_registry_login;
```

Set `AIVS_PLATFORM_REGISTRY_DATABASE_URL` to that dedicated login's DSN. Do not reuse a superuser DSN in production. The ordinary tenant API continues to use `DATABASE_URL`.

## API

All endpoints require both platform headers:

- `GET /api/v1/platform/plugins`
- `POST /api/v1/platform/plugins/releases`
- `POST /api/v1/platform/plugins/{plugin_id}/license-review`
- `POST /api/v1/platform/plugins/{plugin_id}/enable`
- `POST /api/v1/platform/plugins/{plugin_id}/disable`

The legacy `/api/v1/plugins/{plugin_id}/enable|disable` routes are tenant-scoped policy operations only; they no longer mutate process-global plugin state.

Release registration requires plugin identity, SemVer, manifest, artifact URI (`s3://` or `https://`), and lowercase SHA-256. Manifest identity/category/license fields must match the submitted release metadata.

## Current verification boundary

Python tests and contract/preflight checks are run locally. A real PostgreSQL 15 migration round-trip and a production dedicated-login privilege test must pass in CI/deployment before this is considered production-ready.
