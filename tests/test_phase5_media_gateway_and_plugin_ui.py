"""Phase 5 Stage 2 Tests: BYOK Media Gateway Plugin & Web PluginCenterPanel Wiring."""
import shutil
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from apps.api.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestPhase5MediaGatewayAndPluginUI(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aivs_p5_mgw_")
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

    def _create_project(self):
        ws = self.client.post("/api/v1/workspaces", json={"name": "W"}, headers=self._hdr(role="owner")).json()
        proj = self.client.post("/api/v1/projects", json={"workspace_id": ws["id"], "name": "P"}, headers=self._hdr()).json()
        return proj["id"]

    def test_byok_key_masking_and_missing_key_fail_loud(self):
        proj_id = self._create_project()

        # Calling remote provider without configuring a key MUST fail loudly 422 API_KEY_REQUIRED
        r_nokey = self.client.post(
            "/api/v1/plugins/media-gateway/generate",
            json={"project_id": proj_id, "modality": "image", "prompt": "studio lighting", "provider": "fal_ai"},
            headers=self._hdr(),
        )
        self.assertEqual(r_nokey.status_code, 422)
        self.assertEqual(r_nokey.json()["code"], "API_KEY_REQUIRED")

        # Configure BYOK provider key -> returned masked, raw secret NEVER leaked
        raw_secret = "sk-live-supersecret-9876"
        r_cfg = self.client.post(
            "/api/v1/plugins/media-gateway/providers",
            json={"provider": "fal_ai", "api_key": raw_secret},
            headers=self._hdr(role="admin"),
        )
        self.assertEqual(r_cfg.status_code, 200)
        self.assertNotIn(raw_secret, r_cfg.text)
        self.assertEqual(r_cfg.json()["api_key_masked"], "sk-***9876")

        # Catalog shows masked provider for Tenant A, empty for Tenant B
        cat_a = self.client.get("/api/v1/plugins/media-gateway/models", headers=self._hdr()).json()
        self.assertEqual(len(cat_a["configured_providers"]), 1)
        self.assertNotIn(raw_secret, str(cat_a))
        cat_b = self.client.get("/api/v1/plugins/media-gateway/models", headers=self._hdr(tenant=self.tenant_b)).json()
        self.assertEqual(len(cat_b["configured_providers"]), 0)

    def test_generate_image_and_bgm_candidates_then_human_adopt_with_isolation(self):
        proj_id = self._create_project()

        # 1) Generate local deterministic PNG image candidate
        r_img = self.client.post(
            "/api/v1/plugins/media-gateway/generate",
            json={"project_id": proj_id, "modality": "image", "prompt": "minimalist bathroom studio"},
            headers=self._hdr(),
        )
        self.assertEqual(r_img.status_code, 201)
        img_cand = r_img.json()
        self.assertEqual(img_cand["status"], "candidate")
        self.assertGreater(img_cand["size_bytes"], 500)

        # 2) Generate local deterministic BGM audio candidate via FFmpeg
        r_bgm = self.client.post(
            "/api/v1/plugins/media-gateway/generate",
            json={"project_id": proj_id, "modality": "audio_bgm", "prompt": "upbeat synth", "duration_sec": 1.0},
            headers=self._hdr(),
        )
        self.assertEqual(r_bgm.status_code, 201)
        bgm_cand = r_bgm.json()
        self.assertGreater(bgm_cand["size_bytes"], 1000)

        # 3) Cross-tenant adopt MUST return 404
        r_cross = self.client.post(
            f"/api/v1/plugins/media-gateway/candidates/{img_cand['candidate_id']}/adopt",
            headers=self._hdr(tenant=self.tenant_b),
        )
        self.assertEqual(r_cross.status_code, 404)

        # 4) Tenant A explicitly adopts image candidate -> enters project assets
        r_adopt = self.client.post(
            f"/api/v1/plugins/media-gateway/candidates/{img_cand['candidate_id']}/adopt",
            headers=self._hdr(),
        )
        self.assertEqual(r_adopt.status_code, 200)
        asset_id = r_adopt.json()["asset_id"]
        self.assertIn(asset_id, self.app.state.store._tables["assets"])

    def test_hot_disable_media_gateway_plugin_blocks_generation_with_503(self):
        proj_id = self._create_project()
        self.assertEqual(
            self.client.post("/api/v1/plugins/media_gateway_plugin/disable", headers=self._hdr(role="admin")).status_code,
            200,
        )
        r_blocked = self.client.post(
            "/api/v1/plugins/media-gateway/generate",
            json={"project_id": proj_id, "modality": "image", "prompt": "blocked test"},
            headers=self._hdr(),
        )
        self.assertEqual(r_blocked.status_code, 503)
        self.assertEqual(r_blocked.json()["code"], "PLUGIN_DISABLED")

    def test_web_plugin_center_panel_wired_in_app(self):
        panel_tsx = (REPO_ROOT / "apps/web/src/components/PluginCenterPanel.tsx").read_text(encoding="utf-8")
        app_tsx = (REPO_ROOT / "apps/web/src/App.tsx").read_text(encoding="utf-8")
        self.assertIn("/tenant/plugins/${plugin.plugin_id}", panel_tsx)
        self.assertIn("PluginCenterPanel", app_tsx)


if __name__ == "__main__":
    unittest.main()
