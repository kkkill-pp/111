#!/usr/bin/env bash
set -euo pipefail

# One-click local full-stack dev launcher:
#   1. Starts PostgreSQL 15 via docker-compose (port 5433)
#   2. Applies migrations 000..007 if empty
#   3. Starts FastAPI backend on http://localhost:8000 (backed by PostgresTenantStore)
#   4. Starts Vite React+TS Multi-Track Editor on http://localhost:5173

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export DATABASE_URL="${DATABASE_URL:-postgres://postgres:postgres_dev_password@localhost:5433/aivs_sprint0_empty}"

echo "==> [1/3] Starting PostgreSQL 15 container..."
docker compose -f "$ROOT/infra/compose/docker-compose.yml" up -d postgres-sprint0

echo "==> [2/3] Starting FastAPI backend on :8000 (DATABASE_URL=$DATABASE_URL)..."
(cd "$ROOT" && uvicorn apps.api.main:create_app --factory --host 0.0.0.0 --port 8000 --reload) &
API_PID=$!

echo "==> [3/3] Starting Vite Web Editor on :5173..."
trap 'kill $API_PID 2>/dev/null || true' EXIT
cd "$ROOT/apps/web"
npm install --no-audit --no-fund
npm run dev
