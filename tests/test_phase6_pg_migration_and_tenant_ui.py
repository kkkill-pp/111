"""Phase 6 Stage 2 Tests: PostgreSQL 15+ Migration 008 RLS Audit & Tenant Plugin UI Wiring."""
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestPhase6Migration008AndTenantUI(unittest.TestCase):
    def test_migration_008_enforces_force_rls_composite_fk_and_symmetric_rollback(self):
        up_sql = (REPO_ROOT / "migrations/008_plugin_platform_rls.sql").read_text(encoding="utf-8")
        down_sql = (REPO_ROOT / "migrations/008_plugin_platform_rollback.sql").read_text(encoding="utf-8")

        for tbl in ("public.tenant_plugin_policies", "public.anchored_review_comments"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {tbl}", up_sql)
            self.assertIn(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY", up_sql)
            self.assertIn(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY", up_sql)
            self.assertIn(f"DROP TABLE IF EXISTS {tbl}", down_sql)

        self.assertIn("FOREIGN KEY (tenant_id, timeline_id)", up_sql)
        self.assertIn("REFERENCES public.timelines(tenant_id, id)", up_sql)
        self.assertIn("current_setting('app.tenant_id', true)", up_sql)
        self.assertIn("TO app_runtime", up_sql)

    def test_plugin_center_panel_wires_tenant_policy_and_brand_kit(self):
        panel_tsx = (REPO_ROOT / "apps/web/src/components/PluginCenterPanel.tsx").read_text(encoding="utf-8")
        self.assertIn("/tenant/plugins", panel_tsx)
        self.assertIn('method: "PUT"', panel_tsx)
        self.assertIn("brand_kit", panel_tsx)
        self.assertIn("handleSaveBrandKit", panel_tsx)


if __name__ == "__main__":
    unittest.main()
