"""Phase 5 Stage 3 Tests: External Drop-In Plugin Loader SDK, Pre-Import LIC-001 Guard & Path Traversal Protection."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app
from services.infrastructure.errors import AppError
from services.plugins.plugin_loader import load_plugin_package
from services.plugins.plugin_manager import PluginRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestPhase5PluginLoaderSDK(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aivs_p5_loader_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_drop_in_sample_plugin_loads_into_fastapi_app(self):
        app = create_app(database_url="", plugins_dir=str(REPO_ROOT / "plugins_contrib"))
        client = TestClient(app)

        r = client.get("/api/v1/plugins")
        self.assertEqual(r.status_code, 200)
        ids = {p["plugin_id"] for p in r.json()["plugins"]}
        self.assertIn("sample_brand_watermark_plugin", ids)

        plugin = app.state.plugin_registry.get_plugin("sample_brand_watermark_plugin")
        self.assertFalse(plugin.manifest.enabled, "third-party plugins must be disabled until platform approval")
        with self.assertRaises(AppError) as err:
            app.state.plugin_registry.require_enabled("sample_brand_watermark_plugin")
        self.assertEqual(err.exception.code, "PLUGIN_DISABLED")

    def test_pre_import_lic001_blocks_gpl_plugin_before_executing_code(self):
        bad_dir = Path(self.tmpdir) / "evil_gpl"
        bad_dir.mkdir()
        marker = bad_dir / "WAS_EXECUTED.txt"
        (bad_dir / "plugin.json").write_text(
            json.dumps({
                "plugin_id": "evil_gpl",
                "name": "Evil GPL",
                "version": "1.0.0",
                "category": "renderer",
                "license": "GPL-3.0",
                "entrypoint": "plugin.py:create_plugin",
            }),
            encoding="utf-8",
        )
        (bad_dir / "plugin.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('pwned')\n",
            encoding="utf-8",
        )

        reg = PluginRegistry()
        with self.assertRaises(AppError) as err:
            load_plugin_package(bad_dir, reg)
        self.assertEqual(err.exception.status_code, 422)
        self.assertEqual(err.exception.code, "LICENSE_VIOLATION")
        # Crucial proof: plugin.py was NEVER imported or executed!
        self.assertFalse(marker.exists())

    def test_path_traversal_in_entrypoint_rejected(self):
        bad_dir = Path(self.tmpdir) / "traversal_plugin"
        bad_dir.mkdir()
        (bad_dir / "plugin.json").write_text(
            json.dumps({
                "plugin_id": "traversal_plugin",
                "name": "Traversal",
                "version": "1.0.0",
                "category": "template",
                "license": "MIT",
                "entrypoint": "../outside.py:create_plugin",
            }),
            encoding="utf-8",
        )
        reg = PluginRegistry()
        with self.assertRaises(AppError) as err:
            load_plugin_package(bad_dir, reg)
        self.assertEqual(err.exception.status_code, 422)
        self.assertEqual(err.exception.code, "VALIDATION_ERROR")


if __name__ == "__main__":
    unittest.main()
