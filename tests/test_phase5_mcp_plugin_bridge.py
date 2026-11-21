"""Phase 5 Stage 4 & 5 Tests: Plugin-to-MCP Dynamic Tool Bridge, SDK Docs & Preflight 10/10."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from apps.api.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]

MINI_MOTION_MD = """# {{brand_name}} Clip
## Brand Kit
- brand_name: DemoBrand
## Shots
### Shot 1: Intro
- start_sec: 0.0
- duration_sec: 2.0
- subtitle: Hello {{brand_name}}
"""


class TestPhase5MCPPluginBridgeAndDocs(unittest.TestCase):
    def setUp(self):
        self.app = create_app(database_url="")
        self.client = TestClient(self.app)
        self.tenant = str(uuid4())
        self.user = str(uuid4())

    def _hdr(self, role="editor"):
        return {
            "X-Tenant-Id": self.tenant,
            "X-User-Id": self.user,
            "X-User-Role": role,
            "Idempotency-Key": f"idem-{uuid4().hex}",
        }

    def _rpc(self, method: str, params: dict | None = None):
        r = self.client.post(
            "/api/v1/mcp/rpc",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            headers=self._hdr(),
        )
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_mcp_dynamic_plugin_tool_discovery_and_hot_disable_enforcement(self):
        # 1) tools/list with include_plugins=True lists core + plugin tools
        res = self._rpc("tools/list", {"include_plugins": True})
        names = [t["name"] for t in res["result"]["tools"]]
        self.assertIn("aivs_list_plugins", names)
        self.assertIn("aivs_compile_motion_md", names)
        self.assertIn("aivs_list_review_comments", names)

        # 2) Call aivs_compile_motion_md via MCP JSON-RPC
        call_res = self._rpc(
            "tools/call",
            {
                "name": "aivs_compile_motion_md",
                "arguments": {
                    "motion_md": MINI_MOTION_MD,
                    "brand_kit": {"brand_name": "StudioX"},
                    "subtitle_track_id": str(uuid4()),
                },
            },
        )
        self.assertFalse(call_res["result"]["isError"])
        sc = call_res["result"]["structuredContent"]
        self.assertEqual(sc["template"]["title"], "StudioX Clip")
        self.assertEqual(sc["operation_count"], 1)

        # 3) Hot-disable motion_template_plugin -> tool disappears from tools/list AND tools/call returns -32003
        self.client.post("/api/v1/plugins/motion_template_plugin/disable", headers=self._hdr(role="admin"))
        res_after = self._rpc("tools/list", {"include_plugins": True})
        names_after = [t["name"] for t in res_after["result"]["tools"]]
        self.assertNotIn("aivs_compile_motion_md", names_after)

        blocked = self._rpc(
            "tools/call",
            {"name": "aivs_compile_motion_md", "arguments": {"motion_md": MINI_MOTION_MD}},
        )
        self.assertIn("error", blocked)
        self.assertEqual(blocked["error"]["code"], -32003)

    def test_sdk_guide_and_preflight_10_of_10(self):
        guide = (REPO_ROOT / "docs/PLUGIN_SDK_GUIDE.md").read_text(encoding="utf-8")
        self.assertIn("LIC-001", guide)
        self.assertIn("AIVS_PLUGINS_DIR", guide)
        self.assertIn("503 PLUGIN_DISABLED", guide)

        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts/preflight_check.py"), "--json"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        report = json.loads(proc.stdout)
        self.assertTrue(report["ready"])
        self.assertEqual(report["total"], 10)
        self.assertEqual(report["passed"], 10)


if __name__ == "__main__":
    unittest.main()
