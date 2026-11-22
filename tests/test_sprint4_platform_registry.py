"""Sprint 4 platform registry contract and security boundary tests."""
import unittest
from pathlib import Path

from services.infrastructure.errors import AppError
from services.plugins.platform_registry import PlatformPluginRegistry, verify_platform_token
from services.plugins.plugin_manager import AIVSPlugin, PluginManifest, PluginRegistry as RuntimePluginRegistry
from fastapi.testclient import TestClient
from apps.api.main import create_app
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class TestSprint4PlatformRegistry(unittest.TestCase):
    def test_platform_admin_token_is_fail_closed_and_constant_time_helper(self):
        self.assertFalse(verify_platform_token(None, "anything"))
        self.assertFalse(verify_platform_token("secret", None))
        self.assertFalse(verify_platform_token("secret", "wrong"))
        self.assertTrue(verify_platform_token("secret", "secret"))

    def test_release_identity_validation_happens_before_database_access(self):
        registry = PlatformPluginRegistry("postgresql://unreachable.invalid/db")
        with self.assertRaises(AppError) as err:
            registry.register_release(
                plugin_id="Bad-ID", publisher="p", display_name="n", category="renderer",
                license_id="MIT", version="1.0.0", manifest={"plugin_id":"Bad-ID","version":"1.0.0"},
                artifact_uri="s3://bucket/plugin.zip", artifact_sha256="a" * 64, actor="test")
        self.assertEqual(err.exception.code, "VALIDATION_ERROR")

    def test_semver_and_manifest_binding_validation_happens_before_database_access(self):
        registry = PlatformPluginRegistry("postgresql://unreachable.invalid/db")
        with self.assertRaises(AppError) as err:
            registry.register_release(
                plugin_id="sample_plugin", publisher="p", display_name="n", category="renderer",
                license_id="MIT", version="latest", manifest={"plugin_id":"sample_plugin","version":"latest"},
                artifact_uri="s3://bucket/plugin.zip", artifact_sha256="a" * 64, actor="test")
        self.assertEqual(err.exception.code, "VALIDATION_ERROR")

    def test_platform_api_rejects_missing_or_invalid_platform_token(self):
        with patch.dict("os.environ", {"AIVS_PLATFORM_ADMIN_TOKEN": "expected-secret"}):
            client = TestClient(create_app())
            denied = client.get("/api/v1/platform/plugins", headers={"X-Platform-Admin-Actor": "operator"})
            self.assertEqual(denied.status_code, 403)
            wrong = client.get("/api/v1/platform/plugins", headers={
                "X-Platform-Admin-Token": "wrong", "X-Platform-Admin-Actor": "operator"})
            self.assertEqual(wrong.status_code, 403)

    def test_persistent_catalog_state_overrides_process_local_plugin_default(self):
        class FakePlugin(AIVSPlugin):
            manifest = PluginManifest(
                plugin_id="catalog_plugin", name="Catalog", version="1.0.0",
                category="renderer", license="MIT", description="test", enabled=False)

        class FakeCatalog:
            state = False
            def get_runtime_state(self, plugin_id):
                return self.state

        runtime = RuntimePluginRegistry()
        runtime.platform_state_provider = FakeCatalog()
        runtime.register(FakePlugin())
        with self.assertRaises(AppError) as denied:
            runtime.require_enabled("catalog_plugin")
        self.assertEqual(denied.exception.code, "PLUGIN_DISABLED")

        runtime.platform_state_provider.state = True
        self.assertEqual(runtime.require_enabled("catalog_plugin").manifest.plugin_id, "catalog_plugin")
        tenant_disabled = runtime.set_tenant_policy("tenant-a", "catalog_plugin", enabled=False)
        self.assertFalse(tenant_disabled["effective_enabled"])
        tenant_enabled = runtime.set_tenant_policy("tenant-b", "catalog_plugin", enabled=True)
        self.assertTrue(tenant_enabled["effective_enabled"])

    def test_migration_has_append_only_versions_and_events_and_separate_role(self):
        sql = (ROOT / "migrations/010_platform_plugin_registry.sql").read_text()
        self.assertIn("plugin_registry_runtime", sql)
        self.assertIn("platform_plugins", sql)
        self.assertIn("platform_plugin_versions", sql)
        self.assertIn("platform_plugin_events", sql)
        self.assertIn("BEFORE UPDATE OR DELETE", sql)
        self.assertIn("CHECK (NOT globally_enabled OR license_status = 'approved')", sql)
        self.assertIn("REVOKE ALL", sql)

    def test_ci_runs_migration_up_down_up(self):
        wf = (ROOT / ".github/workflows/sprint0-db-gate.yml").read_text()
        self.assertIn("migrations/010_platform_plugin_registry.sql", wf)
        self.assertIn("migrations/010_platform_plugin_registry_rollback.sql", wf)


if __name__ == "__main__":
    unittest.main()
