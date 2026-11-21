"""Phase 3 Stage 2 — Real Image/Logo Overlay Compositing (FFmpeg overlay + enable window) & API Tests."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np

from services.media_engine.adapters.openshorts_adapter import file_sha256
from services.media_engine.ffmpeg_renderer import FFmpegRendererAdapter, RenderPlanCompiler


class Phase3OverlaysTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="aivs_ov_")
        self.video_path = str(Path(self.tmpdir) / "dark_2s.mp4")
        self.logo_path = str(Path(self.tmpdir) / "logo_64.png")

        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=0x0F172A:s=320x180:r=30:d=2",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ac", "2", "-ar", "48000", self.video_path,
            ],
            check=True, timeout=30,
        )
        # Create a solid bright yellow 64x64 PNG logo via OpenCV
        logo = np.zeros((64, 64, 3), dtype=np.uint8)
        logo[:, :] = (0, 235, 255)  # BGR bright yellow
        cv2.imwrite(self.logo_path, logo)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_logo_overlay_renders_only_during_active_time_window(self) -> None:
        compiler = RenderPlanCompiler()
        renderer = FFmpegRendererAdapter()
        v_id, l_id = str(uuid4()), str(uuid4())
        assets = {
            v_id: {"id": v_id, "storage_key": self.video_path, "checksum_sha256": file_sha256(self.video_path), "is_vfr": False},
            l_id: {"id": l_id, "storage_key": self.logo_path, "checksum_sha256": file_sha256(self.logo_path), "is_vfr": False},
        }
        snap = {
            "schema_version": "2.0",
            "timeline_id": str(uuid4()),
            "project_id": str(uuid4()),
            "version": 1,
            "parent_version": None,
            "profile": {"width": 320, "height": 180, "frame_rate": {"num": 30, "den": 1}, "timeline_timebase": {"num": 1, "den": 30000}, "audio_sample_rate": 48000},
            "tracks": [
                {
                    "id": str(uuid4()), "type": "video", "order": 0,
                    "clips": [{
                        "id": str(uuid4()), "asset_id": v_id,
                        "source_range": {"in": 0, "out": 60000, "timebase": {"num": 1, "den": 30000}},
                        "timeline_start": 0, "duration": 60000, "speed": 1.0,
                        "transform": {"mode": "fit"}, "audio": {"enabled": True, "volume": 1.0}, "transitions": [],
                    }],
                },
                {
                    "id": str(uuid4()), "type": "overlay", "order": 1,
                    "items": [{
                        "id": str(uuid4()), "kind": "logo", "asset_id": l_id,
                        "timeline_start": 0, "duration": 30000,  # Active 0.0s .. 1.0s ONLY
                        "position_x": 0.1, "position_y": 0.1, "scale": 1.0, "opacity": 1.0,
                    }],
                },
            ],
        }
        plan, _ = compiler.compile(snap, assets, "final")
        out_path = str(Path(self.tmpdir) / "with_logo.mp4")
        renderer.render(plan, out_path)

        cap = cv2.VideoCapture(out_path)
        cap.set(cv2.CAP_PROP_POS_FRAMES, 15)  # t = 0.5s (logo active)
        _, f_on = cap.read()
        cap.set(cv2.CAP_PROP_POS_FRAMES, 45)  # t = 1.5s (logo inactive)
        _, f_off = cap.read()
        cap.release()

        # Check top-left patch (x=32..96, y=18..82)
        roi_on = f_on[18:82, 32:96]
        roi_off = f_off[18:82, 32:96]
        luma_on = float(np.mean(cv2.cvtColor(roi_on, cv2.COLOR_BGR2GRAY)))
        luma_off = float(np.mean(cv2.cvtColor(roi_off, cv2.COLOR_BGR2GRAY)))
        self.assertGreater(luma_on, 150.0, f"Logo region at t=0.5s should be bright yellow (>150), got {luma_on}")
        self.assertLess(luma_off, 40.0, f"Logo region at t=1.5s should return to dark bg (<40), got {luma_off}")


if __name__ == "__main__":
    unittest.main()
