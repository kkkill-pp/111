"""Phase 6 Stage 3 Tests: PostgresTenantStore 18-Table Coverage & CI Migration 008 Round-Trip Gate."""
import unittest
from pathlib import Path

import yaml

from services.infrastructure.pg_store import ALLOWED_TABLES, PostgresTenantStore

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestPhase6PgStoreAndCI008(unittest.TestCase):
    def test_pg_store_whitelist_includes_plugin_platform_tables_and_methods(self):
        self.assertEqual(len(ALLOWED_TABLES), 18)
        self.assertIn("tenant_plugin_policies", ALLOWED_TABLES)
        self.assertIn("anchored_review_comments", ALLOWED_TABLES)
        self.assertTrue(callable(getattr(PostgresTenantStore, "upsert_tenant_plugin_policy", None)))
        self.assertTrue(callable(getattr(PostgresTenantStore, "list_tenant_plugin_policies", None)))

    def test_ci_workflow_executes_migration_008_and_symmetric_rollback_roundtrip(self):
        wf = yaml.safe_load((REPO_ROOT / ".github/workflows/sprint0-db-gate.yml").read_text(encoding="utf-8"))
        steps = wf["jobs"]["contract-and-db-gate"]["steps"]
        run_cmds = "\n".join(s.get("run", "") for s in steps)
        self.assertIn("migrations/008_plugin_platform_rls.sql", run_cmds)
        self.assertIn("migrations/008_plugin_platform_rollback.sql", run_cmds)


if __name__ == "__main__":
    unittest.main()
