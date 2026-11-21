"""Visual Frame Inspector & Black-Frame Blocker QA Tests (Phase 2 Precursor — Cutlass picture.rs pattern)."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from services.media_engine.adapters.openshorts_adapter import OpenShortsAdapter, file_sha256
from services.media_engine.qa_engine import QAEngine
from services.media_engine.visual_frame_inspector import VisualFrameInspector


class VisualFrameInspectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="aivs_vis_")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_clip(self, name: str, color: str) -> str:
        out = str(Path(self.tmpdir) / name)
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", f"color=c={color}:s=320x180:r=30:d=1",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ac", "2", "-ar", "48000",
                out,
            ],
            check=True,
            timeout=30,
        )
        return out

    def test_catches_all_black_video_as_blocker_and_exports_snapshots_for_visible_video(self) -> None:
        black_mp4 = self._make_clip("black.mp4", "black")
        color_mp4 = self._make_clip("visible.mp4", "0x3B82F6")

        vis_black = VisualFrameInspector.inspect_video_frames(black_mp4)
        self.assertEqual(vis_black.black_frames, vis_black.frames_sampled)

        snap_dir = str(Path(self.tmpdir) / "snaps")
        vis_ok = VisualFrameInspector.inspect_video_frames(color_mp4, save_dir=snap_dir)
        self.assertEqual(vis_ok.black_frames, 0)
        self.assertEqual(len(vis_ok.snapshot_paths), 3)
        for sp in vis_ok.snapshot_paths:
            self.assertTrue(Path(sp).exists())

        # QAEngine MUST fail with TECH_VISUAL_BLACK_FRAME_CHECK blocker on all-black video
        qa = QAEngine(OpenShortsAdapter())
        plan = {
            "timeline_timebase": {"num": 1, "den": 30000},
            "expected_duration_ticks": 30000,
            "output_profile": {"width": 320, "height": 180},
            "subtitle_ops": [],
        }
        rep_black = qa.run_qa(str(uuid4()), black_mp4, file_sha256(black_mp4), plan)
        self.assertEqual(rep_black["status"], "failed")
        self.assertTrue(any(b["code"] == "TECH_VISUAL_BLACK_FRAME_CHECK" for b in rep_black["blockers"]))

        rep_ok = qa.run_qa(str(uuid4()), color_mp4, file_sha256(color_mp4), plan)
        self.assertEqual(rep_ok["status"], "passed")


if __name__ == "__main__":
    unittest.main()
