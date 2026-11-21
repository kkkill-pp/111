"""Phase 3 Stage 1 — OpenTimelineIO (.otio) Export & Real FFmpeg Fade-In/Fade-Out Transition Tests."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np
from fastapi.testclient import TestClient

from apps.api.main import create_app
from services.infrastructure.errors import AppError
from services.media_engine.adapters.openshorts_adapter import file_sha256
from services.media_engine.ffmpeg_renderer import FFmpegRendererAdapter, RenderPlanCompiler


class Phase3OtioAndTransitionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="aivs_p3_")
        self.app = create_app(media_root=self.tmpdir, database_url="")
        self.client = TestClient(self.app)
        self.tenant = str(uuid4())
        self.user = str(uuid4())
        self.bright_video = str(Path(self.tmpdir) / "bright_2s.mp4")
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=0x38BDF8:s=320x180:r=30:d=2",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ac", "2", "-ar", "48000", self.bright_video,
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

    def test_add_remove_transition_and_otio_export_endpoint(self) -> None:
        ws = self.client.post("/api/v1/workspaces", json={"name": "P3 WS"}, headers=self._hdr("owner", f"idem-{uuid4().hex}")).json()
        proj = self.client.post("/api/v1/projects", json={"workspace_id": ws["id"], "name": "P3 Proj"}, headers=self._hdr("editor", f"idem-{uuid4().hex}")).json()
        raw = Path(self.bright_video).read_bytes()
        up = self.client.post(f"/api/v1/projects/{proj['id']}/upload-sessions", json={"filename": "bright_2s.mp4", "size_bytes": len(raw), "kind": "video"}, headers=self._hdr("editor", f"idem-{uuid4().hex}")).json()
        self.client.put(f"/api/v1/upload-sessions/{up['id']}/content", content=raw, headers=self._hdr("editor"))
        comp = self.client.post(f"/api/v1/upload-sessions/{up['id']}/complete", json={"checksum_sha256": file_sha256(self.bright_video)}, headers=self._hdr("editor", f"idem-{uuid4().hex}")).json()
        profile = {"width": 320, "height": 180, "frame_rate": {"num": 30, "den": 1}, "timeline_timebase": {"num": 1, "den": 30000}, "audio_sample_rate": 48000}
        tl = self.client.post(f"/api/v1/projects/{proj['id']}/timelines", json={"profile": profile}, headers=self._hdr("editor", f"idem-{uuid4().hex}")).json()
        tl_id = tl["timeline_id"]
        v_track = tl["tracks"][0]["id"]

        self.client.post(
            f"/api/v1/timelines/{tl_id}/operations",
            json={"base_version": 1, "operations": [{"operation_id": str(uuid4()), "operation_type": "ADD_CLIP", "payload": {"track_id": v_track, "asset_id": comp["asset_id"], "source_in": 0, "source_out": 60000, "timeline_start": 0}}]},
            headers=self._hdr("editor", f"idem-{uuid4().hex}"),
        )
        clip_id = self.client.get(f"/api/v1/timelines/{tl_id}", headers=self._hdr("editor")).json()["tracks"][0]["clips"][0]["id"]

        # Add fade_in transition + speed 1.5x -> v3
        r_tr = self.client.post(
            f"/api/v1/timelines/{tl_id}/operations",
            json={
                "base_version": 2,
                "operations": [
                    {"operation_id": str(uuid4()), "operation_type": "ADD_TRANSITION", "payload": {"clip_id": clip_id, "type": "fade_in", "duration_ticks": 15000, "position": "in"}},
                    {"operation_id": str(uuid4()), "operation_type": "UPDATE_SPEED", "payload": {"clip_id": clip_id, "speed": 1.5}},
                ],
            },
            headers=self._hdr("editor", f"idem-{uuid4().hex}"),
        )
        self.assertEqual(r_tr.status_code, 200, r_tr.text)

        # Export OpenTimelineIO (.otio) JSON via HTTP API
        r_otio = self.client.get(f"/api/v1/timelines/{tl_id}/export/otio", headers=self._hdr("editor"))
        self.assertEqual(r_otio.status_code, 200, r_otio.text)
        otio = r_otio.json()
        self.assertEqual(otio["OTIO_SCHEMA"], "Timeline.1")
        self.assertEqual(otio["metadata"]["aivs_version"], 3)
        v_otio = otio["tracks"]["children"][0]
        self.assertEqual(v_otio["OTIO_SCHEMA"], "Track.1")
        self.assertEqual(v_otio["kind"], "Video")
        self.assertEqual(v_otio["children"][0]["effects"][0]["OTIO_SCHEMA"], "LinearTimeWarp.1")
        self.assertAlmostEqual(v_otio["children"][0]["effects"][0]["time_scalar"], 1.5)

        # Remove transition -> v4
        tr_id = self.client.get(f"/api/v1/timelines/{tl_id}", headers=self._hdr("editor")).json()["tracks"][0]["clips"][0]["transitions"][0]["id"]
        r_rm = self.client.post(
            f"/api/v1/timelines/{tl_id}/operations",
            json={"base_version": 3, "operations": [{"operation_id": str(uuid4()), "operation_type": "REMOVE_TRANSITION", "payload": {"transition_id": tr_id}}]},
            headers=self._hdr("editor", f"idem-{uuid4().hex}"),
        )
        self.assertEqual(r_rm.status_code, 200, r_rm.text)
        self.assertEqual(self.client.get(f"/api/v1/timelines/{tl_id}", headers=self._hdr("editor")).json()["tracks"][0]["clips"][0]["transitions"], [])

    def test_ffmpeg_fade_in_pixel_proof_and_crossfade_still_fail_fast(self) -> None:
        compiler = RenderPlanCompiler()
        renderer = FFmpegRendererAdapter()
        asset_id = str(uuid4())
        asset = {"id": asset_id, "storage_key": self.bright_video, "checksum_sha256": file_sha256(self.bright_video), "is_vfr": False}

        def snap_with_tr(tr_type: str) -> dict:
            return {
                "schema_version": "2.0",
                "timeline_id": str(uuid4()),
                "project_id": str(uuid4()),
                "version": 1,
                "parent_version": None,
                "profile": {"width": 320, "height": 180, "frame_rate": {"num": 30, "den": 1}, "timeline_timebase": {"num": 1, "den": 30000}, "audio_sample_rate": 48000},
                "tracks": [{
                    "id": str(uuid4()), "type": "video", "order": 0,
                    "clips": [{
                        "id": str(uuid4()), "asset_id": asset_id,
                        "source_range": {"in": 0, "out": 60000, "timebase": {"num": 1, "den": 30000}},
                        "timeline_start": 0, "duration": 60000, "speed": 1.0,
                        "transform": {"mode": "fit"}, "audio": {"enabled": True, "volume": 1.0},
                        "transitions": [{"id": str(uuid4()), "type": tr_type, "duration_ticks": 30000, "position": "in"}],
                    }],
                }],
            }

        # Real pixel check on fade_in: frame 0 dark (< 15 luma), frame 45 bright (> 60 luma)
        plan_fi, _ = compiler.compile(snap_with_tr("fade_in"), {asset_id: asset}, "final")
        out_fi = str(Path(self.tmpdir) / "fade_in.mp4")
        renderer.render(plan_fi, out_fi)
        cap = cv2.VideoCapture(out_fi)
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        _, f0 = cap.read()
        cap.set(cv2.CAP_PROP_POS_FRAMES, 45)
        _, f45 = cap.read()
        cap.release()
        luma0 = float(np.mean(cv2.cvtColor(f0, cv2.COLOR_BGR2GRAY)))
        luma45 = float(np.mean(cv2.cvtColor(f45, cv2.COLOR_BGR2GRAY)))
        self.assertLess(luma0, 15.0, f"fade_in frame 0 should be near-black, got luma={luma0}")
        self.assertGreater(luma45, 60.0, f"fade_in frame 45 should be bright, got luma={luma45}")

        # Unimplemented crossfade MUST still fail loudly 422 (ADR-004)
        plan_cf, _ = compiler.compile(snap_with_tr("crossfade"), {asset_id: asset}, "final")
        with self.assertRaises(AppError) as err:
            renderer.render(plan_cf, str(Path(self.tmpdir) / "cf.mp4"))
        self.assertEqual(err.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
