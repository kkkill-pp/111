"""Phase 5 Stage 1 Tests: Pluggable Platform Kernel + OpenMotion-Inspired Plugins.

Verifies:
  1. LIC-001 license enforcement (GPL/AGPL plugins rejected 422; MIT/Apache-2.0 accepted)
  2. Hot enable/disable lifecycle (disabled plugin fails loudly with 503 PLUGIN_DISABLED)
  3. MotionTemplatePlugin: parses Motion.md + BrandKit -> compiles v3.0 EditOperations[] -> applies to Timeline
  4. AnchoredReviewPlugin: tenant-isolated timecode-anchored comments + Agent intent compilation + resolve
  5. MotionOverlayRendererPlugin: renders BrandKit RGBA PNG banner -> FFmpeg ADD_OVERLAY -> OpenCV pixel proof
"""
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
from services.plugins.plugin_manager import AIVSPlugin, PluginManifest


class _ForbiddenGPLPlugin(AIVSPlugin):
    def __init__(self):
        self.manifest = PluginManifest(
            plugin_id="bad_gpl_plugin",
            name="Forbidden GPL Plugin",
            version="0.1.0",
            category="renderer",
            license="GPL-3.0",
            description="Must be blocked by LIC-001 redline.",
        )


SAMPLE_MOTION_MD = """# {{brand_name}} Launch Teaser

## Brand Kit
- brand_name: DefaultBrand
- primary_color: #F59E0B
- cta_text: Try Now

## Shots

### Shot 1: Hook Opening
- start_sec: 0.0
- duration_sec: 1.5
- subtitle: {{brand_name}} — {{cta_text}}
- transition: fade_in

### Shot 2: Feature Reveal
- start_sec: 1.5
- duration_sec: 1.5
- subtitle: Built with Pluggable Plugins
- speed: 1.25
"""


class TestPhase5PluginPlatform(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aivs_p5_plugins_")
        self.sample_video = str(Path(self.tmpdir) / "dark_3s.mp4")
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=640x360:d=3:r=30",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", self.sample_video,
            ],
            check=True, capture_output=True,
        )
        self.app = create_app(media_root=self.tmpdir, database_url="")
        self.client = TestClient(self.app)
        self.tenant_a = str(uuid4())
        self.tenant_b = str(uuid4())
        self.user_a = str(uuid4())

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _hdr(self, tenant=None, role="editor"):
        return {
            "X-Tenant-Id": tenant or self.tenant_a,
            "X-User-Id": self.user_a,
            "X-User-Role": role,
            "Idempotency-Key": f"idem-{uuid4().hex}",
        }

    def _bootstrap_timeline(self):
        ws = self.client.post("/api/v1/workspaces", json={"name": "W"}, headers=self._hdr(role="owner")).json()
        proj = self.client.post("/api/v1/projects", json={"workspace_id": ws["id"], "name": "P"}, headers=self._hdr()).json()
        up = self.client.post(
            f"/api/v1/projects/{proj['id']}/upload-sessions",
            json={"filename": "dark.mp4", "size_bytes": Path(self.sample_video).stat().st_size, "kind": "video"},
            headers=self._hdr(),
        ).json()
        shutil.copyfile(self.sample_video, up["storage_key"])
        comp = self.client.post(
            f"/api/v1/upload-sessions/{up['id']}/complete",
            json={"checksum_sha256": file_sha256(self.sample_video)},
            headers=self._hdr(),
        ).json()
        br = self.client.post(
            f"/api/v1/projects/{proj['id']}/briefs",
            json={"title": "B", "brief": {"target_platform": "youtube_shorts", "audience": "creators", "objective": "education", "language": "zh", "duration_range_sec": {"min": 5, "max": 60}, "aspect_ratio": "16:9"}},
            headers=self._hdr(),
        ).json()
        sb = self.client.post(
            f"/api/v1/projects/{proj['id']}/storyboards",
            json={"brief_version_id": br["brief_version_id"], "asset_id": comp["asset_id"]},
            headers=self._hdr(),
        ).json()
        self.client.post(
            f"/api/v1/storyboards/{sb['storyboard_id']}/reviews",
            json={"subject_version": "1", "subject_checksum": sb["proposal_checksum"], "decision": "approve"},
            headers=self._hdr(role="reviewer"),
        )
        tl = self.client.post(
            f"/api/v1/projects/{proj['id']}/timelines",
            json={"storyboard_id": sb["storyboard_id"], "storyboard_version": 1, "profile": {"width": 640, "height": 360, "frame_rate": {"num": 30, "den": 1}, "timeline_timebase": {"num": 1, "den": 30000}, "audio_sample_rate": 48000}},
            headers=self._hdr(),
        ).json()
        return proj["id"], tl["timeline_id"]

    def test_lic001_blocks_gpl_plugin_and_hot_toggle_enforces_503(self):
        # 1) GPL-3.0 plugin registration MUST fail loudly with 422 LICENSE_VIOLATION
        with self.assertRaises(AppError) as err:
            self.app.state.plugin_registry.register(_ForbiddenGPLPlugin())
        self.assertEqual(err.exception.status_code, 422)
        self.assertEqual(err.exception.code, "LICENSE_VIOLATION")

        # 2) GET /api/v1/plugins lists all 3 built-in Apache-2.0 plugins
        r_list = self.client.get("/api/v1/plugins")
        self.assertEqual(r_list.status_code, 200)
        ids = {p["plugin_id"] for p in r_list.json()["plugins"]}
        self.assertEqual(ids, {"motion_template_plugin", "anchored_review_plugin", "motion_overlay_renderer_plugin", "media_gateway_plugin", "html_gsap_motion_plugin"})

        # 3) Disable motion_template_plugin -> compile endpoint MUST fail 503 PLUGIN_DISABLED
        r_dis = self.client.post("/api/v1/plugins/motion_template_plugin/disable", headers=self._hdr(role="admin"))
        self.assertEqual(r_dis.status_code, 200)
        self.assertFalse(r_dis.json()["enabled"])

        r_blocked = self.client.post(
            "/api/v1/plugins/motion-template/compile",
            json={"motion_md": SAMPLE_MOTION_MD},
            headers=self._hdr(),
        )
        self.assertEqual(r_blocked.status_code, 503)
        self.assertEqual(r_blocked.json()["code"], "PLUGIN_DISABLED")

        # 4) Re-enable -> works immediately
        r_en = self.client.post("/api/v1/plugins/motion_template_plugin/enable", headers=self._hdr(role="admin"))
        self.assertEqual(r_en.status_code, 200)
        self.assertTrue(r_en.json()["enabled"])

    def test_motion_md_template_compiles_and_applies_to_timeline(self):
        _, tl_id = self._bootstrap_timeline()
        v1 = self.client.get(f"/api/v1/timelines/{tl_id}", headers=self._hdr()).json()
        clip_id = next(t for t in v1["tracks"] if t["type"] == "video")["clips"][0]["id"]
        sub_track_id = next(t for t in v1["tracks"] if t["type"] == "subtitle")["id"]

        r_comp = self.client.post(
            "/api/v1/plugins/motion-template/compile",
            json={
                "motion_md": SAMPLE_MOTION_MD,
                "brand_kit": {"brand_name": "HOROW Studio", "cta_text": "立即体验"},
                "subtitle_track_id": sub_track_id,
                "target_clip_id": clip_id,
            },
            headers=self._hdr(),
        )
        self.assertEqual(r_comp.status_code, 200)
        data = r_comp.json()
        self.assertEqual(data["template"]["title"], "HOROW Studio Launch Teaser")
        self.assertEqual(data["operation_count"], 4)  # 2 subtitles + 1 transition + 1 speed

        # Apply compiled operations directly via TimelineService
        r_apply = self.client.post(
            f"/api/v1/timelines/{tl_id}/operations",
            json={"base_version": 1, "operations": data["operations"]},
            headers=self._hdr(),
        )
        self.assertEqual(r_apply.status_code, 200)
        v2 = self.client.get(f"/api/v1/timelines/{tl_id}", headers=self._hdr()).json()
        subs = next(t for t in v2["tracks"] if t["type"] == "subtitle")["items"]
        self.assertEqual(len(subs), 2)
        self.assertEqual(subs[0]["text"], "HOROW Studio — 立即体验")

    def test_anchored_review_comments_isolation_and_agent_intent(self):
        _, tl_id = self._bootstrap_timeline()
        r_add = self.client.post(
            f"/api/v1/timelines/{tl_id}/review-comments",
            json={"timeline_version": 1, "at_tick": 45000, "comment": "这里停顿多留一拍", "suggested_action": "extend_shot"},
            headers=self._hdr(role="reviewer"),
        )
        self.assertEqual(r_add.status_code, 201)
        cid = r_add.json()["comment_id"]

        # Tenant A sees the comment
        r_list = self.client.get(f"/api/v1/timelines/{tl_id}/review-comments", headers=self._hdr())
        self.assertEqual(r_list.status_code, 200)
        self.assertEqual(len(r_list.json()["comments"]), 1)

        # Tenant B gets 404 on timeline comments and on resolve
        self.assertEqual(
            self.client.get(f"/api/v1/timelines/{tl_id}/review-comments", headers=self._hdr(tenant=self.tenant_b)).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(f"/api/v1/review-comments/{cid}/resolve", headers=self._hdr(tenant=self.tenant_b)).status_code,
            404,
        )

        # Compile to Agent intent & resolve
        plugin = self.app.state.plugin_registry.require_enabled("anchored_review_plugin")
        from services.infrastructure.store import TenantContext
        ctx_a = TenantContext(tenant_id=self.tenant_a, user_id=self.user_a, role="reviewer", trace_id="t1")
        intent = plugin.compile_comment_to_agent_intent(ctx_a, cid)
        self.assertEqual(intent["at_sec"], 1.5)
        self.assertIn("这里停顿多留一拍", intent["agent_intent"])

        r_res = self.client.post(f"/api/v1/review-comments/{cid}/resolve", headers=self._hdr(role="reviewer"))
        self.assertEqual(r_res.status_code, 200)
        self.assertEqual(r_res.json()["status"], "resolved")

    def test_motion_overlay_plugin_renders_banner_and_composites_with_pixel_proof(self):
        proj_id, tl_id = self._bootstrap_timeline()
        r_ov = self.client.post(
            "/api/v1/plugins/motion-overlay/render-banner",
            json={
                "project_id": proj_id,
                "kind": "lower_third",
                "title": "AI VIDEO STUDIO",
                "subtitle": "PLUGGABLE ENGINE",
                "brand_kit": {"bg_color": "#FFFFFF", "primary_color": "#F59E0B", "text_color": "#000000"},
            },
            headers=self._hdr(),
        )
        self.assertEqual(r_ov.status_code, 201)
        ov_asset_id = r_ov.json()["asset_id"]

        # Add rendered banner via ADD_OVERLAY (kind='image') from 1.0s to 2.5s
        ov_track_id = str(uuid4())
        v1_row = next(r for r in self.app.state.store._tables["timeline_versions"].values() if r["timeline_id"] == tl_id and r["version"] == 1)
        v1_row["snapshot_json"]["tracks"].append({"id": ov_track_id, "type": "overlay", "order": 2, "items": []})
        r_add = self.client.post(
            f"/api/v1/timelines/{tl_id}/operations",
            json={
                "base_version": 1,
                "operations": [{
                    "operation_id": str(uuid4()),
                    "operation_type": "ADD_OVERLAY",
                    "payload": {
                        "track_id": ov_track_id,
                        "kind": "image",
                        "asset_id": ov_asset_id,
                        "timeline_start": 30000,
                        "duration": 45000,
                        "position_x": 0.1,
                        "position_y": 0.1,
                    },
                }],
            },
            headers=self._hdr(),
        )
        self.assertEqual(r_add.status_code, 200)

        # Render via FFmpeg and verify pixel brightness at 0.3s (black) vs 1.5s (bright white/gold banner)
        r_ren = self.client.post(f"/api/v1/timelines/{tl_id}/renders", json={"timeline_version": 2}, headers=self._hdr())
        self.assertEqual(r_ren.status_code, 202)
        out_mp4 = self.app.state.store._tables["artifacts"][r_ren.json()["artifact_id"]]["storage_key"]

        cap = cv2.VideoCapture(out_mp4)
        cap.set(cv2.CAP_PROP_POS_MSEC, 300)
        ok1, f_before = cap.read()
        cap.set(cv2.CAP_PROP_POS_MSEC, 1500)
        ok2, f_during = cap.read()
        cap.release()
        self.assertTrue(ok1 and ok2)
        mean_before = float(np.mean(f_before))
        mean_during = float(np.mean(f_during))
        self.assertLess(mean_before, 10.0)
        self.assertGreater(mean_during, 30.0)


if __name__ == "__main__":
    unittest.main()
