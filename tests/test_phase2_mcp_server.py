"""Phase 2 Stage 4 — Controlled HITL MCP Server (JSON-RPC 2.0) Tests."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from apps.api.main import create_app
from services.media_engine.adapters.openshorts_adapter import file_sha256


class Phase2MCPServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="aivs_mcp_")
        self.app = create_app(media_root=self.tmpdir, database_url="")
        self.client = TestClient(self.app)
        self.tenant = str(uuid4())
        self.user = str(uuid4())
        self.sample_video = str(Path(self.tmpdir) / "clip.mp4")
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=2",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ac", "2", "-ar", "48000", self.sample_video,
            ],
            check=True, timeout=30,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _hdr(self, role: str = "editor", idem: str | None = None) -> dict[str, str]:
        h = {"X-Tenant-Id": self.tenant, "X-User-Id": self.user, "X-User-Role": role, "X-Trace-Id": f"t-{uuid4().hex[:8]}"}
        if idem:
            h["Idempotency-Key"] = idem
        return h

    def _rpc(self, method: str, params: dict | None = None, role: str = "editor") -> dict:
        r = self.client.post(
            "/api/v1/mcp/rpc",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            headers=self._hdr(role),
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_mcp_initialize_tools_list_and_hitl_guarded_tool_calls(self) -> None:
        # 1. initialize & tools/list
        init_res = self._rpc("initialize")
        self.assertEqual(init_res["result"]["serverInfo"]["name"], "ai-video-studio-mcp")

        list_res = self._rpc("tools/list")
        tool_names = [t["name"] for t in list_res["result"]["tools"]]
        self.assertEqual(
            tool_names,
            ["get_timeline", "list_timeline_versions", "validate_operations", "propose_edit_operations", "inspect_rendered_frames"],
        )

        # 2. Setup timeline with 1 video clip
        ws = self.client.post("/api/v1/workspaces", json={"name": "MCP WS"}, headers=self._hdr("owner", f"idem-{uuid4().hex}")).json()
        proj = self.client.post("/api/v1/projects", json={"workspace_id": ws["id"], "name": "MCP Proj"}, headers=self._hdr("editor", f"idem-{uuid4().hex}")).json()
        raw = Path(self.sample_video).read_bytes()
        up = self.client.post(f"/api/v1/projects/{proj['id']}/upload-sessions", json={"filename": "clip.mp4", "size_bytes": len(raw), "kind": "video"}, headers=self._hdr("editor", f"idem-{uuid4().hex}")).json()
        self.client.put(f"/api/v1/upload-sessions/{up['id']}/content", content=raw, headers=self._hdr("editor"))
        comp = self.client.post(f"/api/v1/upload-sessions/{up['id']}/complete", json={"checksum_sha256": file_sha256(self.sample_video)}, headers=self._hdr("editor", f"idem-{uuid4().hex}")).json()
        profile = {"width": 320, "height": 180, "frame_rate": {"num": 30, "den": 1}, "timeline_timebase": {"num": 1, "den": 30000}, "audio_sample_rate": 48000}
        tl = self.client.post(f"/api/v1/projects/{proj['id']}/timelines", json={"profile": profile}, headers=self._hdr("editor", f"idem-{uuid4().hex}")).json()
        tl_id = tl["timeline_id"]
        v_track = tl["tracks"][0]["id"]
        self.client.post(
            f"/api/v1/timelines/{tl_id}/operations",
            json={"base_version": 1, "operations": [{"operation_id": str(uuid4()), "operation_type": "ADD_CLIP", "payload": {"track_id": v_track, "asset_id": comp["asset_id"], "source_in": 0, "source_out": 60000, "timeline_start": 0}}]},
            headers=self._hdr("editor", f"idem-{uuid4().hex}"),
        )

        # 3. MCP get_timeline read-only succeeds
        r_get = self._rpc("tools/call", {"name": "get_timeline", "arguments": {"timeline_id": tl_id}})
        self.assertEqual(r_get["result"]["structuredContent"]["version"], 2)

        # 4. MCP propose_edit_operations with L0 MUST fail -32003 (FORBIDDEN)
        r_l0 = self._rpc(
            "tools/call",
            {"name": "propose_edit_operations", "arguments": {"timeline_id": tl_id, "base_version": 2, "user_intent": "trim", "permission_level": "L0"}},
        )
        self.assertEqual(r_l0["error"]["code"], -32003)

        # 5. MCP propose_edit_operations with L1 creates proposal WITHOUT mutating timeline (still v2!)
        r_l1 = self._rpc(
            "tools/call",
            {"name": "propose_edit_operations", "arguments": {"timeline_id": tl_id, "base_version": 2, "user_intent": "2.0x speed", "permission_level": "L1"}},
        )
        self.assertFalse(r_l1["result"]["isError"])
        self.assertIn("proposal_id", r_l1["result"]["structuredContent"])
        r_ver = self._rpc("tools/call", {"name": "get_timeline", "arguments": {"timeline_id": tl_id}})
        self.assertEqual(r_ver["result"]["structuredContent"]["version"], 2)

        # 6. Unknown/disallowed tool MUST fail -32003
        r_bad = self._rpc("tools/call", {"name": "execute_shell", "arguments": {"cmd": "rm -rf /"}})
        self.assertEqual(r_bad["error"]["code"], -32003)


if __name__ == "__main__":
    unittest.main()
