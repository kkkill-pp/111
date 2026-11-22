# AI Video Studio — Sprint 4 Engineering Drop

## Implemented in this drop

1. Added migration 010 for the platform-global plugin catalog, immutable version records, and append-only governance events.
2. Added a dedicated `plugin_registry_runtime` database role with narrow column-level permissions; `app_runtime` is explicitly denied access to the global catalog.
3. Added a persistent platform registry service with SemVer/manifest/digest input validation, license approval gate, global enable/disable, and event recording.
4. Added token-protected platform administration endpoints and actor attribution.
5. Separated tenant plugin toggles from global platform toggles. Tenant endpoints now modify only tenant policy and audit the operation.
6. Connected runtime plugin checks and MCP tool discovery/calls to tenant-effective state; persistent global state is queried rather than relying on a stale process-local enable flag.
7. External plugin packages discovered from disk default to disabled. The existing LIC-001 pre-import license check and path traversal guard remain in place.
8. Added migration up/down/up to the PostgreSQL 15 CI workflow, tests, and operator documentation.

## Verification performed in this environment

- Python test suite: **70 passed, 3 skipped**.
- Cross-contract verification: **61/61 passed**.
- Preflight: **10/10 passed**.
- Contract manifest: **70/70 files match**.
- Python compilation checks passed for changed API/plugin/MCP modules.

## Not verified here — do not treat as production sign-off

- PostgreSQL 15 execution of migration 010 and rollback round-trip: unavailable locally because `psql`/PostgreSQL/Docker are not installed. CI is configured to perform this check.
- Dedicated non-superuser registry login privilege test and cross-instance live revocation test: not run against a live PostgreSQL instance.
- Frontend TypeScript/Vite build: dependency installation did not complete in this environment, so no frontend build result is claimed.
- Platform admin UI: backend API and operator documentation are included; a dedicated browser UI for global catalog/review/activation is not yet implemented.
- Artifact provenance: the registry stores the submitted SHA-256 but this sprint does not yet fetch the artifact and verify its bytes against that digest at load time. Do not treat a registered digest as independently verified provenance.
- Third-party plugin process sandboxing: plugin code is still imported into the API process after manifest/license/path checks. A separate isolated worker/container execution boundary remains required before running untrusted third-party code in production.

## Deployment prerequisites

Apply migrations 000–008 and then 010. Configure `AIVS_PLATFORM_ADMIN_TOKEN` and a dedicated `AIVS_PLATFORM_REGISTRY_DATABASE_URL` whose login is granted `plugin_registry_runtime`. Do not use a database superuser DSN for the registry service in production. See `docs/SPRINT4_PLATFORM_REGISTRY.md`.
