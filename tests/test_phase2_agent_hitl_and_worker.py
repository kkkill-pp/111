"""Phase 2 Agent L0-L3 Permissions, Irreversible Action Guard, Visual Frame Self-Check & FencingWorker Tests."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from apps.api.main import create_app
from services.infrastructure.errors import AppError
from services.media_engine.adapters.openshorts_adapter import file_sha256
from services.workers.fencing_worker import ClaimedTask, FencingRejectionError, FencingWorker


class Phase2AgentAndWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="aivs_p2_")
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

    def test_l0_agent_blocked_irreversible_guard_and_visual_inspect_endpoint(self) -> None:
        ws = self.client.post("/api/v1/workspaces", json={"name": "P2 WS"}, headers=self._hdr("owner", f"idem-{uuid4().hex}")).json()
        proj = self.client.post("/api/v1/projects", json={"workspace_id": ws["id"], "name": "P2 Proj"}, headers=self._hdr("editor", f"idem-{uuid4().hex}")).json()
        raw = Path(self.sample_video).read_bytes()
        up = self.client.post(f"/api/v1/projects/{proj['id']}/upload-sessions", json={"filename": "clip.mp4", "size_bytes": len(raw), "kind": "video"}, headers=self._hdr("editor", f"idem-{uuid4().hex}")).json()
        self.client.put(f"/api/v1/upload-sessions/{up['id']}/content", content=raw, headers=self._hdr("editor"))
        comp = self.client.post(f"/api/v1/upload-sessions/{up['id']}/complete", json={"checksum_sha256": file_sha256(self.sample_video)}, headers=self._hdr("editor", f"idem-{uuid4().hex}")).json()
        profile = {"width": 320, "height": 180, "frame_rate": {"num": 30, "den": 1}, "timeline_timebase": {"num": 1, "den": 30000}, "audio_sample_rate": 48000}
        tl = self.client.post(f"/api/v1/projects/{proj['id']}/timelines", json={"profile": profile}, headers=self._hdr("editor", f"idem-{uuid4().hex}")).json()
        tl_id = tl["timeline_id"]
        v_track = tl["tracks"][0]["id"]

        # Add clip + UPDATE_TRANSFORM (Phase 2 implemented op #11)
        r_add = self.client.post(
            f"/api/v1/timelines/{tl_id}/operations",
            json={"base_version": 1, "operations": [{"operation_id": str(uuid4()), "operation_type": "ADD_CLIP", "payload": {"track_id": v_track, "asset_id": comp["asset_id"], "source_in": 0, "source_out": 60000, "timeline_start": 0}}]},
            headers=self._hdr("editor", f"idem-{uuid4().hex}"),
        )
        self.assertEqual(r_add.status_code, 200, r_add.text)
        clip_id = self.client.get(f"/api/v1/timelines/{tl_id}", headers=self._hdr("editor")).json()["tracks"][0]["clips"][0]["id"]

        r_tr = self.client.post(
            f"/api/v1/timelines/{tl_id}/operations",
            json={"base_version": 2, "operations": [{"operation_id": str(uuid4()), "operation_type": "UPDATE_TRANSFORM", "payload": {"clip_id": clip_id, "transform": {"mode": "fill", "scale": 1.2}}}]},
            headers=self._hdr("editor", f"idem-{uuid4().hex}"),
        )
        self.assertEqual(r_tr.status_code, 200, r_tr.text)
        tl_v3 = self.client.get(f"/api/v1/timelines/{tl_id}", headers=self._hdr("editor")).json()
        self.assertEqual(tl_v3["tracks"][0]["clips"][0]["transform"]["mode"], "fill")

        # L0 read-only Agent MUST be blocked (403 FORBIDDEN)
        r_l0 = self.client.post(
            f"/api/v1/timelines/{tl_id}/agent-proposals",
            json={"base_version": 3, "user_intent": "Try editing as L0", "permission_level": "L0"},
            headers=self._hdr("editor", f"idem-{uuid4().hex}"),
        )
        self.assertEqual(r_l0.status_code, 403, r_l0.text)

        # Cutlass-inspired irreversible action guard MUST block without human_review_id
        with self.assertRaises(AppError) as ir_err:
            self.app.state.agent_harness.require_human_approval_for_irreversible("delete_media", human_review_id=None)
        self.assertEqual(ir_err.exception.status_code, 409)

        # Render preview & invoke Agent Visual Frame Inspection endpoint (Cutlass picture.rs pattern)
        r_prev = self.client.post(f"/api/v1/timelines/{tl_id}/previews", json={"timeline_version": 3}, headers=self._hdr("editor", f"idem-{uuid4().hex}"))
        self.assertEqual(r_prev.status_code, 202, r_prev.text)
        art_id = r_prev.json()["artifact_id"]

        r_vis = self.client.post(f"/api/v1/artifacts/{art_id}/visual-inspect", headers=self._hdr("reviewer", f"idem-{uuid4().hex}"))
        self.assertEqual(r_vis.status_code, 200, r_vis.text)
        vis_data = r_vis.json()
        self.assertEqual(vis_data["frames_sampled"], 3)
        self.assertEqual(vis_data["black_frames"], 0)
        self.assertEqual(len(vis_data["snapshot_paths"]), 3)

    def test_fencing_worker_validates_lease_bounds_and_propagates_stale_rejection(self) -> None:
        with self.assertRaises(ValueError):
            FencingWorker("postgresql://localhost/db", "w-1", lease_seconds=2)

        w = FencingWorker("postgresql://localhost/db", "w-1", lease_seconds=60)
        fake_task = ClaimedTask(task_id=str(uuid4()), tenant_id=self.tenant, task_type="render", payload_json={}, lease_token=7)
        w.claim_next = lambda: fake_task  # type: ignore[method-assign]
        w.complete = lambda t, r: (_ for _ in ()).throw(FencingRejectionError("stale token"))  # type: ignore[method-assign]
        with self.assertRaises(FencingRejectionError):
            w.execute_once(lambda t: {"ok": True})


if __name__ == "__main__":
    unittest.main()
