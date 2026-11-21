"""Phase 4 Stage 5 Tests: Production Preflight CLI & Web Editor OTIO/Canvas Integration."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestPhase4PreflightAndWebFeatures(unittest.TestCase):
    def test_preflight_cli_passes_all_local_blockers_with_json_output(self):
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts/preflight_check.py"), "--json"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, f"Preflight failed:\n{proc.stdout}\n{proc.stderr}")
        report = json.loads(proc.stdout)
        self.assertTrue(report["ready"])
        self.assertEqual(report["failed"], 0)
        names = {c["name"]: c["status"] for c in report["checks"]}
        self.assertEqual(names["manifest_sha256_integrity"], "pass")
        self.assertEqual(names["v3_contract_verification"], "pass")
        self.assertEqual(names["license_redline_lic001"], "pass")
        self.assertEqual(names["deployment_artifacts_present"], "pass")

    def test_web_editor_wires_otio_export_capabilities_and_canvas_profile(self):
        app_tsx = (REPO_ROOT / "apps/web/src/App.tsx").read_text(encoding="utf-8")
        self.assertIn("/export/otio", app_tsx)
        self.assertIn("/capabilities", app_tsx)
        self.assertIn("SET_CANVAS_PROFILE", app_tsx)
        self.assertIn("handleExportOtio", app_tsx)
        self.assertIn("handleSetCanvasProfile", app_tsx)


if __name__ == "__main__":
    unittest.main()
