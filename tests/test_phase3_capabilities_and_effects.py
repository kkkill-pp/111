"""Phase 3 Stage 3 — CapabilityRegistry Plugin Extensibility & Real FFmpeg Effect Pixel Proof Tests."""
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
from services.media_engine.capability_registry import CapabilityRegistry, EffectCapability
from services.media_engine.ffmpeg_renderer import FFmpegRendererAdapter, RenderPlanCompiler


class Phase3CapabilitiesAndEffectsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="aivs_fx_")
        self.app = create_app(media_root=self.tmpdir, database_url="")
        self.client = TestClient(self.app)
        self.gray_video = str(Path(self.tmpdir) / "gray_1s.mp4")
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=0x505050:s=320x180:r=30:d=1",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ac", "2", "-ar", "48000", self.gray_video,
            ],
            check=True, timeout=30,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_capabilities_endpoint_and_custom_plugin_registration(self) -> None:
        r = self.client.get("/api/v1/capabilities")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["renderer"], "ffmpeg")
        names = [e["effect_type"] for e in data["effects"]]
        self.assertIn("brightness_contrast", names)
        self.assertIn("saturation", names)
        self.assertIn("gaussian_blur", names)

        # Custom plugin registration uses the exact same road as built-in effects
        reg = CapabilityRegistry()
        reg.register_effect(EffectCapability(
            effect_type="vintage_sepia",
            category="video",
            description="Custom plugin sepia matrix",
            default_params={},
            compile_ffmpeg=lambda p: "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131",
        ))
        self.assertTrue(reg.has_effect("vintage_sepia"))
        self.assertIn("colorchannelmixer", reg.compile_video_effect("vintage_sepia", {}))

    def test_brightness_effect_pixel_proof_and_unknown_effect_fail_fast(self) -> None:
        compiler = RenderPlanCompiler()
        renderer = FFmpegRendererAdapter()
        asset_id = str(uuid4())
        assets = {asset_id: {"id": asset_id, "storage_key": self.gray_video, "checksum_sha256": file_sha256(self.gray_video), "is_vfr": False}}

        def make_plan(effects: list[dict]) -> dict:
            snap = {
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
                        "source_range": {"in": 0, "out": 30000, "timebase": {"num": 1, "den": 30000}},
                        "timeline_start": 0, "duration": 30000, "speed": 1.0,
                        "transform": {"mode": "fit"}, "audio": {"enabled": True, "volume": 1.0}, "transitions": [],
                    }],
                }],
            }
            plan, _ = compiler.compile(snap, assets, "final")
            if effects:
                plan["video_ops"][0]["effects"] = effects
            return plan

        out_base = str(Path(self.tmpdir) / "base.mp4")
        out_bright = str(Path(self.tmpdir) / "bright.mp4")
        renderer.render(make_plan([]), out_base)
        renderer.render(make_plan([{"effect_type": "brightness_contrast", "enabled": True, "params": {"brightness": 0.45, "contrast": 1.0}}]), out_bright)

        def avg_luma(path: str) -> float:
            cap = cv2.VideoCapture(path)
            cap.set(cv2.CAP_PROP_POS_FRAMES, 10)
            _, frame = cap.read()
            cap.release()
            return float(np.mean(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)))

        luma_base = avg_luma(out_base)
        luma_bright = avg_luma(out_bright)
        self.assertGreater(luma_bright - luma_base, 60.0, f"Expected brightness effect +60 luma jump: base={luma_base}, bright={luma_bright}")

        # Unknown effect_type MUST fail 422 (ADR-004)
        with self.assertRaises(AppError) as err:
            renderer.render(make_plan([{"effect_type": "magic_unregistered_vfx", "enabled": True, "params": {}}]), str(Path(self.tmpdir) / "bad.mp4"))
        self.assertEqual(err.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
