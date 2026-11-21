"""Integration & Compliance Tests for OpenShortsAdapter + Vendored MIT Modules (ADR-002/003)."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from services.media_engine.adapters.openshorts_adapter import OpenShortsAdapter
from services.media_engine.openshorts_vendor import clip_selection as os_clips
from services.media_engine.openshorts_vendor import ffmpeg_utils as os_ff


class OpenShortsAdapterIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="aivs_os_adapter_")
        self.sample_video = str(Path(self.tmpdir) / "sample.mp4")
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=2",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", self.sample_video,
            ],
            check=True,
            timeout=60,
        )
        self.adapter = OpenShortsAdapter()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_vendored_frame_sampler_and_commercial_safe_person_detector(self) -> None:
        res = self.adapter.sample_keyframes_and_detect_persons(self.sample_video, [0, 30])
        self.assertEqual(res["sampled_count"], 2)
        self.assertEqual(res["code_license"], "Apache-2.0")
        self.assertIn("Apache-2.0", res["weights_license"])
        self.assertIn(0, res["detections"])
        self.assertIn(30, res["detections"])

    def test_vendored_clip_selection_and_ffmpeg_utils(self) -> None:
        words = [{"w": "hello", "s": 0.1, "e": 0.5}, {"w": "world", "s": 15.2, "e": 15.8}]
        s, e = os_clips.snap_clip_to_words(0.2, 15.7, words, 30.0, min_duration=10.0, max_duration=60.0)
        self.assertLess(s, e)
        escaped = os_ff.escape_filter_value("C:\\tmp\\a'b:c.ass")
        self.assertIn("\\:", escaped)

    def test_vendored_ssrf_guard_blocks_internal_and_metadata_ips(self) -> None:
        for bad_url in [
            "http://127.0.0.1:8000/secret",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/internal",
        ]:
            with self.assertRaises(ValueError):
                self.adapter.validate_import_url(bad_url)

    def test_license_redline_zero_cloud_and_zero_ultralytics(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        self.assertFalse((repo_root / "cloud").exists(), "Forbidden cloud/ directory present")
        for py in repo_root.rglob("*.py"):
            if "packages" in py.parts or "tests" in py.parts:
                continue
            txt = py.read_text(encoding="utf-8", errors="ignore")
            self.assertNotIn("import ultralytics", txt, f"Forbidden ultralytics import in {py}")
            self.assertNotIn("from ultralytics", txt, f"Forbidden ultralytics import in {py}")


if __name__ == "__main__":
    unittest.main()
