"""Unified Time-Semantics & FFmpeg Preview/Export Equivalence Tests (v3.0 Risk #4)."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from services.timeline.time_math import (
    Rational,
    compute_clip_duration_ticks,
    ticks_to_ffmpeg_timestamp,
    ticks_to_frame_index,
    ticks_to_seconds_fraction,
)


class TestTimeSemanticsV3(unittest.TestCase):
    def test_rational_exact_math_standard_and_ntsc_framerates(self) -> None:
        tb_30k = Rational(1, 30000)
        tb_90k = Rational(1, 90000)

        # 4.0s at 30k ticks, 2.0x playback -> exact 2.0s = 60000 ticks
        dur_2x = compute_clip_duration_ticks(0, 120000, tb_30k, tb_30k, playback_rate=2.0)
        self.assertEqual(dur_2x, 60000)
        self.assertEqual(ticks_to_ffmpeg_timestamp(dur_2x, tb_30k), "2.000000")

        # 90k source timebase -> 30k project timebase at 0.5x playback
        dur_half = compute_clip_duration_ticks(0, 90000, tb_90k, tb_30k, playback_rate=0.5)
        self.assertEqual(dur_half, 60000)

        # NTSC 29.97fps (30000/1001): 30 frames = 1001/1000s = 30030 ticks @ 1/30000s
        fps_2997 = Rational(30000, 1001)
        self.assertEqual(ticks_to_frame_index(30030, tb_30k, fps_2997), 30)

        # 23.976fps (24000/1001): 24 frames = 1001/1000s = 30030 ticks @ 1/30000s
        fps_23976 = Rational(24000, 1001)
        self.assertEqual(ticks_to_frame_index(30030, tb_30k, fps_23976), 24)

    def test_rejects_invalid_bounds(self) -> None:
        tb = Rational(1, 30000)
        with self.assertRaises(ValueError):
            compute_clip_duration_ticks(100, 100, tb, tb, playback_rate=1.0)
        with self.assertRaises(ValueError):
            compute_clip_duration_ticks(0, 30000, tb, tb, playback_rate=0.1)
        with self.assertRaises(ValueError):
            compute_clip_duration_ticks(0, 30000, tb, tb, playback_rate=5.0)

    def test_ffmpeg_export_duration_matches_rational_ticks(self) -> None:
        """Real FFmpeg + ffprobe check: 4.0s source at 2.0x playback_rate with exact tick-bounded -t exports 2.00s."""
        tb = Rational(1, 30000)
        expected_ticks = compute_clip_duration_ticks(0, 120000, tb, tb, playback_rate=2.0)
        expected_sec = float(ticks_to_seconds_fraction(expected_ticks, tb))
        exact_t = ticks_to_ffmpeg_timestamp(expected_ticks, tb)
        self.assertEqual(exact_t, "2.000000")

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src_4s.mp4"
            out = Path(tmp) / "out_2x.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=duration=4:size=320x180:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=4:sample_rate=48000",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(src),
                    "-filter_complex", "[0:v]setpts=PTS/2.0[v];[0:a]atempo=2.0[a]",
                    "-map", "[v]", "-map", "[a]",
                    "-t", exact_t,
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out),
                ],
                check=True,
            )
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,duration", "-of", "json", str(out)],
                check=True,
                capture_output=True,
                text=True,
            )
            info = json.loads(probe.stdout)
            actual_sec = float(info["format"]["duration"])
            # Strict < 1 frame (33.3ms) tolerance when tick-bounded -t is applied
            self.assertLessEqual(abs(actual_sec - expected_sec), 0.034, f"FFmpeg drift: {actual_sec} vs {expected_sec}")


if __name__ == "__main__":
    unittest.main()
