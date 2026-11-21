#!/usr/bin/env bash
set -euo pipefail

# One-command PostgreSQL 15+ migration, CR-001 RLS, rollback round-trip & fault-injection verifier.
# Usage: DATABASE_URL="postgres://postgres:postgres@localhost:5432/aivs_test" ./verify_all_pg15.sh
: "${DATABASE_URL:?Set DATABASE_URL to an empty PostgreSQL 15+ database}"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PG_VERSION_NUM="$(psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -Atqc "SHOW server_version_num")"
if [[ "$PG_VERSION_NUM" -lt 150000 ]]; then
  echo "ERROR: PostgreSQL 15+ required; detected server_version_num=$PG_VERSION_NUM" >&2
  exit 2
fi

PUBLIC_TABLE_COUNT="$(psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -Atqc \
  "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S')")"
if [[ "$PUBLIC_TABLE_COUNT" -ne 0 ]]; then
  echo "ERROR: verification requires an EMPTY database (public objects found: $PUBLIC_TABLE_COUNT)." >&2
  echo "Use a disposable test database; this script is not an upgrade migration runner." >&2
  exit 3
fi

for f in \
  000_roles_and_extensions.sql \
  001_identity_workspace.sql \
  002_project_asset_analysis.sql \
  003_creative_timeline.sql \
  004_tasks_outbox_workers.sql \
  005_render_qa_hitl_delivery.sql \
  006_governance_usage_rls.sql \
  007_v3_protocol_upgrade.sql; do
  echo "==> Applying $f"
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$DIR/$f"
done

echo "==> Running verify_rls.sql (CR-001 seeded non-superuser app_runtime isolation)"
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$DIR/verify_rls.sql"

echo "==> Running fault_injection_tests.sql (FI-1 Cross-Tenant, FI-2 Expired Worker, FI-3 Stale v3->v4 Approval)"
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$DIR/fault_injection_tests.sql"

echo "==> Running CR-001 rollback & re-upgrade round-trip verification (007 rollback -> 007 upgrade -> RLS)"
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$DIR/007_v3_protocol_rollback.sql"
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$DIR/007_v3_protocol_upgrade.sql"
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$DIR/verify_rls.sql"

echo "ALL POSTGRESQL 15+ MIGRATIONS, CR-001 ROLLBACK ROUND-TRIP, AND FAULT-INJECTION TESTS PASSED."
