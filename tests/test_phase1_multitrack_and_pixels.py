"""Phase 1 Multi-Track Operations, Optimistic Lock, Undo/Redo, afade Reachability & Pixel-Level Subtitle Tests."""
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
from services.media_engine.adapters.openshorts_adapter import file_sha256
from services.media_engine.ffmpeg_renderer import FFmpegRendererAdapter, RenderPlanCompiler


class Phase1MultiTrackAndPixelsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="aivs_p1_")
        self.app = create_app(media_root=self.tmpdir)
        self.client = TestClient(self.app)
        self.tenant = str(uuid4())
        self.user = str(uuid4())

        # Synthesize a solid dark-blue 2s 640x360 video + 440Hz sine audio for deterministic pixel diff
        self.sample_video = str(Path(self.tmpdir) / "dark_2s.mp4")
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=0x101828:s=640x360:r=30:d=2",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ac", "2", "-ar", "48000",
                self.sample_video,
            ],
            check=True,
            timeout=60,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _hdr(self, role: str = "editor", idem: str | None = None) -> dict[str, str]:
        h = {"X-Tenant-Id": self.tenant, "X-User-Id": self.user, "X-User-Role": role, "X-Trace-Id": f"t-{uuid4().hex[:8]}"}
        if idem:
            h["Idempotency-Key"] = idem
        return h

    def test_pixel_level_subtitle_burnin_and_afade_reachability(self) -> None:
        """Closes v5 §4.1 (afade reachable) and §4.2 (pixel-level subtitle visibility on identical duration clips)."""
        compiler = RenderPlanCompiler()
        renderer = FFmpegRendererAdapter()
        asset_id = str(uuid4())
        clip_id = str(uuid4())
        asset = {"id": asset_id, "storage_key": self.sample_video, "checksum_sha256": file_sha256(self.sample_video), "is_vfr": False, "pts_map_storage_key": None}

        snap_no_sub = {
            "schema_version": "2.0",
            "timeline_id": str(uuid4()),
            "project_id": str(uuid4()),
            "version": 1,
            "parent_version": None,
            "profile": {"width": 640, "height": 360, "frame_rate": {"num": 30, "den": 1}, "timeline_timebase": {"num": 1, "den": 30000}, "audio_sample_rate": 48000},
            "tracks": [
                {
                    "id": str(uuid4()),
                    "type": "video",
                    "order": 0,
                    "clips": [
                        {
                            "id": clip_id,
                            "asset_id": asset_id,
                            "source_range": {"in": 0, "out": 60000, "timebase": {"num": 1, "den": 30000}},
                            "timeline_start": 0,
                            "duration": 60000,
                            "speed": 1.0,
                            "transform": {"mode": "fit"},
                            "audio": {"enabled": True, "volume": 1.0, "fade_in_ticks": 15000, "fade_out_ticks": 15000},
                            "transitions": [],
                        }
                    ],
                },
                {"id": str(uuid4()), "type": "subtitle", "order": 1, "items": []},
            ],
        }

        plan_no_sub, _ = compiler.compile(snap_no_sub, {asset_id: asset}, "final")
        # Verify afade fields are genuinely compiled into audio_ops (not dead code!)
        self.assertEqual(plan_no_sub["audio_ops"][0]["fade_in_ticks"], 15000)
        self.assertEqual(plan_no_sub["audio_ops"][0]["fade_out_ticks"], 15000)

        snap_with_sub = {
            **snap_no_sub,
            "tracks": [
                snap_no_sub["tracks"][0],
                {
                    "id": snap_no_sub["tracks"][1]["id"],
                    "type": "subtitle",
                    "order": 1,
                    "items": [
                        {
                            "id": str(uuid4()),
                            "timeline_start": 0,
                            "duration": 60000,
                            "text": "AI VIDEO STUDIO SUBTITLE TEST",
                            "style_preset": "default",
                            "safe_zone": "bottom",
                        }
                    ],
                },
            ],
        }
        plan_with_sub, _ = compiler.compile(snap_with_sub, {asset_id: asset}, "final")

        out_plain = str(Path(self.tmpdir) / "plain.mp4")
        out_sub = str(Path(self.tmpdir) / "with_sub.mp4")
        renderer.render(plan_no_sub, out_plain)
        renderer.render(plan_with_sub, out_sub)

        # Extract frame at t=1.0s from both identical-duration outputs and compare bottom subtitle band
        cap_a = cv2.VideoCapture(out_plain)
        cap_b = cv2.VideoCapture(out_sub)
        cap_a.set(cv2.CAP_PROP_POS_FRAMES, 30)
        cap_b.set(cv2.CAP_PROP_POS_FRAMES, 30)
        ok_a, frame_a = cap_a.read()
        ok_b, frame_b = cap_b.read()
        cap_a.release()
        cap_b.release()
        self.assertTrue(ok_a and ok_b)

        # Compare bottom 40% where bottom-aligned ASS subtitle renders
        h = frame_a.shape[0]
        diff = np.abs(frame_a[int(h * 0.6):].astype(np.int16) - frame_b[int(h * 0.6):].astype(np.int16))
        changed_pixels = int(np.sum(np.max(diff, axis=2) > 25))
        self.assertGreater(changed_pixels, 300, f"Expected visible subtitle pixels (>300 changed), got {changed_pixels}")

    def test_phase1_multitrack_split_move_subtitle_optimistic_lock_undo_redo_and_final_export(self) -> None:
        """Full Phase 1 human multi-track editing loop: Upload -> Split -> Move -> Subtitle CRUD -> 409 Lock -> Undo -> Redo -> Final 1080p Export -> QA."""
        ws = self.client.post("/api/v1/workspaces", json={"name": "P1 Studio"}, headers=self._hdr("owner", f"idem-{uuid4().hex}")).json()
        proj = self.client.post("/api/v1/projects", json={"workspace_id": ws["id"], "name": "P1 Edit"}, headers=self._hdr("editor", f"idem-{uuid4().hex}")).json()
        up = self.client.post(f"/api/v1/projects/{proj['id']}/upload-sessions", json={"filename": "dark_2s.mp4", "size_bytes": Path(self.sample_video).stat().st_size, "kind": "video"}, headers=self._hdr("editor", f"idem-{uuid4().hex}")).json()
        shutil.copyfile(self.sample_video, up["storage_key"])
        comp = self.client.post(f"/api/v1/upload-sessions/{up['id']}/complete", json={"checksum_sha256": file_sha256(self.sample_video)}, headers=self._hdr("editor", f"idem-{uuid4().hex}")).json()
        br = self.client.post(f"/api/v1/projects/{proj['id']}/briefs", json={"title": "B", "brief": {"target_platform": "youtube_shorts", "audience": "creators", "objective": "education", "language": "zh", "duration_range_sec": {"min": 5, "max": 60}, "aspect_ratio": "9:16"}}, headers=self._hdr("editor", f"idem-{uuid4().hex}")).json()
        sb = self.client.post(f"/api/v1/projects/{proj['id']}/storyboards", json={"brief_version_id": br["brief_version_id"], "asset_id": comp["asset_id"]}, headers=self._hdr("editor", f"idem-{uuid4().hex}")).json()
        self.client.post(f"/api/v1/storyboards/{sb['storyboard_id']}/reviews", json={"subject_version": "1", "subject_checksum": sb["proposal_checksum"], "decision": "approve"}, headers=self._hdr("reviewer", f"idem-{uuid4().hex}"))
        tl = self.client.post(f"/api/v1/projects/{proj['id']}/timelines", json={"storyboard_id": sb["storyboard_id"], "storyboard_version": 1, "profile": {"width": 640, "height": 360, "frame_rate": {"num": 30, "den": 1}, "timeline_timebase": {"num": 1, "den": 30000}, "audio_sample_rate": 48000}}, headers=self._hdr("editor", f"idem-{uuid4().hex}")).json()
        tl_id = tl["timeline_id"]

        tl_v1 = self.client.get(f"/api/v1/timelines/{tl_id}", headers=self._hdr("editor")).json()
        clip_id = tl_v1["tracks"][0]["clips"][0]["id"]
        sub_track_id = next(t["id"] for t in tl_v1["tracks"] if t["type"] == "subtitle")

        # 1. Dry-run /validate SPLIT_CLIP + ADD_SUBTITLE
        r_val = self.client.post(
            f"/api/v1/timelines/{tl_id}/validate",
            json={
                "base_version": 1,
                "operations": [
                    {"operation_id": str(uuid4()), "operation_type": "SPLIT_CLIP", "payload": {"clip_id": clip_id, "split_at_timeline_tick": 30000}},
                    {"operation_id": str(uuid4()), "operation_type": "ADD_SUBTITLE", "payload": {"track_id": sub_track_id, "timeline_start": 0, "duration": 30000, "text": "Part 1"}},
                ],
            },
            headers=self._hdr("editor"),
        )
        self.assertEqual(r_val.status_code, 200, r_val.text)
        self.assertTrue(r_val.json()["valid"])

        # 2. Direct human commit -> v2 (splits 1 clip into 2 clips + adds subtitle)
        r_v2 = self.client.post(
            f"/api/v1/timelines/{tl_id}/operations",
            json={
                "base_version": 1,
                "operations": [
                    {"operation_id": str(uuid4()), "operation_type": "SPLIT_CLIP", "payload": {"clip_id": clip_id, "split_at_timeline_tick": 30000}},
                    {"operation_id": str(uuid4()), "operation_type": "ADD_SUBTITLE", "payload": {"track_id": sub_track_id, "timeline_start": 0, "duration": 30000, "text": "Part 1"}},
                ],
            },
            headers=self._hdr("editor", f"idem-{uuid4().hex}"),
        )
        self.assertEqual(r_v2.status_code, 200, r_v2.text)
        self.assertEqual(r_v2.json()["new_version"], 2)

        # 3. Optimistic concurrency conflict: stale base_version=1 MUST return 409
        r_conflict = self.client.post(
            f"/api/v1/timelines/{tl_id}/operations",
            json={"base_version": 1, "operations": [{"operation_id": str(uuid4()), "operation_type": "MOVE_CLIP", "payload": {"clip_id": clip_id, "target_timeline_start": 1000}}]},
            headers=self._hdr("editor", f"idem-{uuid4().hex}"),
        )
        self.assertEqual(r_conflict.status_code, 409)

        # 4. Verify v2 has 2 clips and 1 subtitle; UPDATE_SUBTITLE -> v3
        tl_v2 = self.client.get(f"/api/v1/timelines/{tl_id}", headers=self._hdr("editor")).json()
        self.assertEqual(len(tl_v2["tracks"][0]["clips"]), 2)
        sub_id = next(t for t in tl_v2["tracks"] if t["type"] == "subtitle")["items"][0]["id"]
        r_v3 = self.client.post(
            f"/api/v1/timelines/{tl_id}/operations",
            json={"base_version": 2, "operations": [{"operation_id": str(uuid4()), "operation_type": "UPDATE_SUBTITLE", "payload": {"subtitle_id": sub_id, "text": "Part 1 Edited"}}]},
            headers=self._hdr("editor", f"idem-{uuid4().hex}"),
        )
        self.assertEqual(r_v3.status_code, 200, r_v3.text)
        self.assertEqual(r_v3.json()["new_version"], 3)

        # 5. Undo v3 -> v4, then Redo v4 -> v5
        r_undo = self.client.post(f"/api/v1/timelines/{tl_id}/undo", json={"base_version": 3}, headers=self._hdr("editor", f"idem-{uuid4().hex}"))
        self.assertEqual(r_undo.status_code, 200, r_undo.text)
        self.assertEqual(r_undo.json()["new_version"], 4)
        r_redo = self.client.post(f"/api/v1/timelines/{tl_id}/redo", json={"base_version": 4}, headers=self._hdr("editor", f"idem-{uuid4().hex}"))
        self.assertEqual(r_redo.status_code, 200, r_redo.text)
        self.assertEqual(r_redo.json()["new_version"], 5)
        self.assertEqual(r_redo.json()["origin_command"], "redo")

        # 6. Final Export RenderPlan + Real ffprobe QA on v5
        r_fin = self.client.post(f"/api/v1/timelines/{tl_id}/renders", json={"timeline_version": 5}, headers=self._hdr("editor", f"idem-{uuid4().hex}"))
        self.assertEqual(r_fin.status_code, 202, r_fin.text)
        r_qa = self.client.post(f"/api/v1/artifacts/{r_fin.json()['artifact_id']}/qa", headers=self._hdr("reviewer", f"idem-{uuid4().hex}"))
        self.assertEqual(r_qa.status_code, 202, r_qa.text)
        self.assertEqual(r_qa.json()["qa_status"], "passed")


if __name__ == "__main__":
    unittest.main()
