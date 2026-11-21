"""Phase 2 Stage 2 — Multi-Track Standalone Audio Track Mixing (FFmpeg amix + adelay) Tests."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from services.media_engine.adapters.openshorts_adapter import file_sha256
from services.media_engine.ffmpeg_renderer import FFmpegRendererAdapter, RenderPlanCompiler


class MultiTrackAudioAmixTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="aivs_amix_")
        self.video_path = str(Path(self.tmpdir) / "vid_2s.mp4")
        self.bgm_path = str(Path(self.tmpdir) / "bgm_2s.m4a")
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=2",
                "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=2",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ac", "2", "-ar", "48000", self.video_path,
            ],
            check=True, timeout=30,
        )
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=2",
                "-c:a", "aac", "-ac", "2", "-ar", "48000", self.bgm_path,
            ],
            check=True, timeout=30,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @staticmethod
    def _mean_volume_db(path: str) -> float:
        proc = subprocess.run(["ffmpeg", "-i", path, "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True, check=True)
        m = re.search(r"mean_volume:\s*([-\d.]+)\s*dB", proc.stderr)
        assert m is not None, f"volumedetect missing mean_volume: {proc.stderr}"
        return float(m.group(1))

    def test_standalone_audio_track_mixes_via_amix_and_respects_volume(self) -> None:
        compiler = RenderPlanCompiler()
        renderer = FFmpegRendererAdapter()
        v_asset_id, a_asset_id = str(uuid4()), str(uuid4())
        assets = {
            v_asset_id: {"id": v_asset_id, "storage_key": self.video_path, "checksum_sha256": file_sha256(self.video_path), "is_vfr": False},
            a_asset_id: {"id": a_asset_id, "storage_key": self.bgm_path, "checksum_sha256": file_sha256(self.bgm_path), "is_vfr": False},
        }

        def make_snap(bgm_volume: float) -> dict:
            return {
                "schema_version": "2.0",
                "timeline_id": str(uuid4()),
                "project_id": str(uuid4()),
                "version": 1,
                "parent_version": None,
                "profile": {"width": 320, "height": 180, "frame_rate": {"num": 30, "den": 1}, "timeline_timebase": {"num": 1, "den": 30000}, "audio_sample_rate": 48000},
                "tracks": [
                    {
                        "id": str(uuid4()),
                        "type": "video",
                        "order": 0,
                        "clips": [
                            {
                                "id": str(uuid4()),
                                "asset_id": v_asset_id,
                                "source_range": {"in": 0, "out": 60000, "timebase": {"num": 1, "den": 30000}},
                                "timeline_start": 0,
                                "duration": 60000,
                                "speed": 1.0,
                                "transform": {"mode": "fit"},
                                "audio": {"enabled": False, "volume": 0.0},
                                "transitions": [],
                            }
                        ],
                    },
                    {
                        "id": str(uuid4()),
                        "type": "audio",
                        "order": 1,
                        "clips": [
                            {
                                "id": str(uuid4()),
                                "asset_id": a_asset_id,
                                "source_range": {"in": 0, "out": 60000, "timebase": {"num": 1, "den": 30000}},
                                "timeline_start": 15000,
                                "duration": 45000,
                                "speed": 1.0,
                                "audio": {"enabled": True, "volume": bgm_volume, "fade_in_ticks": 6000, "fade_out_ticks": 6000},
                            }
                        ],
                    },
                ],
            }

        plan_loud, _ = compiler.compile(make_snap(0.9), assets, "final")
        plan_quiet, _ = compiler.compile(make_snap(0.1), assets, "final")
        out_loud = str(Path(self.tmpdir) / "mix_loud.mp4")
        out_quiet = str(Path(self.tmpdir) / "mix_quiet.mp4")
        renderer.render(plan_loud, out_loud)
        renderer.render(plan_quiet, out_quiet)

        db_loud = self._mean_volume_db(out_loud)
        db_quiet = self._mean_volume_db(out_quiet)
        # Main video audio is muted; audible output proves standalone audio track was mixed via amix!
        self.assertGreater(db_loud, -35.0, f"Expected audible mixed BGM (> -20 dB), got {db_loud} dB")
        self.assertGreater(db_loud - db_quiet, 10.0, f"Expected >= 10 dB drop from 0.9 to 0.1 volume: loud={db_loud}, quiet={db_quiet}")


if __name__ == "__main__":
    unittest.main()
