#!/usr/bin/env bash
set -euo pipefail

# One-command local Sprint 0 PostgreSQL 15+ gate runner with raw log capture.
# Requires: docker (or an existing empty PostgreSQL 15+ DATABASE_URL) and psql.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_FILE="$LOG_DIR/sprint0_pg15_${TS}.log"

if [ -z "${DATABASE_URL:-}" ]; then
  echo "DATABASE_URL not set. Starting ephemeral PostgreSQL 15 container..."
  CID=$(docker run -d --rm -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=aivs_sprint0 -p 55432:5432 postgres:15-alpine)
  trap 'docker stop "$CID" >/dev/null 2>&1 || true' EXIT
  for i in $(seq 1 30); do
    if pg_isready -h localhost -p 55432 -U postgres >/dev/null 2>&1; then break; fi
    sleep 1
  done
  export DATABASE_URL="postgres://postgres:postgres@localhost:55432/aivs_sprint0"
fi

{
  echo "=== [1/3] HITL v2.2 Contract Verification ==="
  python3 "$ROOT/packages/contracts/09_tests_and_adr/verify_contracts.py"
  echo ""
  echo "=== [2/3] PostgreSQL 15+ Empty DB Migrations + RLS + FI-1/FI-2/FI-3 ==="
  bash "$ROOT/migrations/verify_all_pg15.sh"
  echo ""
  echo "=== [3/3] Sprint 1 Vertical Slice Automated Tests ==="
  python3 -m unittest discover -s "$ROOT/tests" -v
} 2>&1 | tee "$LOG_FILE"

echo "Raw Sprint 0 log saved to: $LOG_FILE"
