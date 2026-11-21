"""Extension B Tests: Real FFmpeg Crossfade (xfade + acrossfade) OpenCV Pixel Proof & HTML/GSAP Motion Plugin."""
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
from services.plugins.builtin.html_gsap_motion_plugin import detect_browser_backend


class TestExtensionBCrossfadeAndHtmlGsap(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aivs_ext_b_")
        self.red_mp4 = str(Path(self.tmpdir) / "red_2s.mp4")
        self.blue_mp4 = str(Path(self.tmpdir) / "blue_2s.mp4")
        for color, out_p, freq in (("red", self.red_mp4, 440), ("blue", self.blue_mp4, 660)):
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"color=c={color}:s=320x180:r=30:d=2",
                    "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration=2",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-ac", "2", "-ar", "48000", out_p,
                ],
                check=True, timeout=30,
            )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ffmpeg_crossfade_renders_real_blended_midpoint_pixels(self):
        compiler = RenderPlanCompiler()
        renderer = FFmpegRendererAdapter()
        a1, a2 = str(uuid4()), str(uuid4())
        assets = {
            a1: {"id": a1, "storage_key": self.red_mp4, "checksum_sha256": file_sha256(self.red_mp4), "is_vfr": False},
            a2: {"id": a2, "storage_key": self.blue_mp4, "checksum_sha256": file_sha256(self.blue_mp4), "is_vfr": False},
        }
        snap = {
            "schema_version": "2.0",
            "timeline_id": str(uuid4()),
            "version": 1,
            "profile": {
                "width": 320, "height": 180,
                "frame_rate": {"num": 30, "den": 1},
                "timeline_timebase": {"num": 1, "den": 30000},
                "audio_sample_rate": 48000,
            },
            "tracks": [
                {
                    "id": str(uuid4()), "type": "video", "order": 0,
                    "clips": [
                        {
                            "id": str(uuid4()), "asset_id": a1,
                            "timeline_start": 0, "duration": 60000,
                            "source_range": {"in": 0, "out": 60000},
                            "speed": 1.0,
                            "transitions": [{"id": str(uuid4()), "type": "crossfade", "duration_ticks": 30000, "position": "out"}],
                        },
                        {
                            "id": str(uuid4()), "asset_id": a2,
                            "timeline_start": 60000, "duration": 60000,
                            "source_range": {"in": 0, "out": 60000},
                            "speed": 1.0,
                        },
                    ],
                }
            ],
        }
        plan, _ = compiler.compile(snap, assets, "final")
        out_mp4 = str(Path(self.tmpdir) / "crossfade_out.mp4")
        renderer.render(plan, out_mp4)

        cap = cv2.VideoCapture(out_mp4)
        # 0.3s -> pure Red (BGR: low B, high R)
        cap.set(cv2.CAP_PROP_POS_MSEC, 300)
        ok1, f_red = cap.read()
        # 1.5s -> midpoint of 1.0s crossfade (offset 1.0s..2.0s) -> blended Purple (both B and R elevated!)
        cap.set(cv2.CAP_PROP_POS_MSEC, 1500)
        ok2, f_mid = cap.read()
        # 2.6s -> pure Blue (BGR: high B, low R)
        cap.set(cv2.CAP_PROP_POS_MSEC, 2600)
        ok3, f_blue = cap.read()
        cap.release()
        self.assertTrue(ok1 and ok2 and ok3)

        b1, _, r1 = [float(x) for x in np.mean(f_red, axis=(0, 1))]
        bm, _, rm = [float(x) for x in np.mean(f_mid, axis=(0, 1))]
        b3, _, r3 = [float(x) for x in np.mean(f_blue, axis=(0, 1))]

        self.assertGreater(r1, 180.0)
        self.assertLess(b1, 30.0)
        # Blended midpoint MUST show both red and blue channels active
        self.assertGreater(rm, 70.0, f"Midpoint red channel too low: {rm}")
        self.assertGreater(bm, 70.0, f"Midpoint blue channel too low: {bm}")
        self.assertGreater(b3, 180.0)
        self.assertLess(r3, 30.0)

    def test_html_gsap_plugin_builds_seekable_bundle_and_enforces_chromium_guard(self):
        app = create_app(media_root=self.tmpdir, database_url="")
        client = TestClient(app)
        hdr = {
            "X-Tenant-Id": str(uuid4()),
            "X-User-Id": str(uuid4()),
            "X-User-Role": "editor",
            "Idempotency-Key": f"idem-{uuid4().hex}",
        }

        # 1) Runtime check endpoint
        rt = client.get("/api/v1/plugins/html-gsap/runtime", headers=hdr)
        self.assertEqual(rt.status_code, 200)
        self.assertIn("kinetic_title", rt.json()["presets"])

        # 2) Build runnable HTML/GSAP bundle
        r_build = client.post(
            "/api/v1/plugins/html-gsap/build-scene",
            json={
                "preset": "kinetic_title",
                "headline": "AI Video Studio v3.0",
                "subheadline": "Pluggable GSAP Engine",
                "duration_sec": 3.0,
                "fps": 30,
                "render_mode": "html_bundle",
            },
            headers=hdr,
        )
        self.assertEqual(r_build.status_code, 201)
        data = r_build.json()
        self.assertEqual(data["total_frames"], 90)
        html_txt = Path(data["html_path"]).read_text(encoding="utf-8")
        self.assertIn("window.__seekToSec", html_txt)
        self.assertIn("gsap.min.js", html_txt)
        self.assertTrue(Path(data["poster_path"]).is_file())

        # 3) If host lacks Chromium, chromium_gsap MUST fail loudly 503 CHROMIUM_UNAVAILABLE (ADR-004)
        if not detect_browser_backend()["available"]:
            r_no_chr = client.post(
                "/api/v1/plugins/html-gsap/build-scene",
                json={"preset": "kinetic_title", "headline": "Strict Guard", "render_mode": "chromium_gsap"},
                headers=hdr,
            )
            self.assertEqual(r_no_chr.status_code, 503)
            self.assertEqual(r_no_chr.json()["code"], "CHROMIUM_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
