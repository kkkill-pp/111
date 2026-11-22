"""Phase 6 Stage 1 Tests: Per-Tenant Plugin Policy Isolation and Default BrandKit Config."""
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from apps.api.main import create_app

MINI_MOTION_MD = """# {{brand_name}} Promo
## Brand Kit
- brand_name: FallbackBrand
- cta_text: Learn More
## Shots
### Shot 1: Opening
- start_sec: 0.0
- duration_sec: 2.0
- subtitle: {{brand_name}} - {{cta_text}}
"""


class TestPhase6TenantPluginPolicies(unittest.TestCase):
    def setUp(self):
        self.app = create_app(database_url="")
        self.client = TestClient(self.app)
        self.tenant_a = str(uuid4())
        self.tenant_b = str(uuid4())
        self.user = str(uuid4())

    def _hdr(self, tenant, role="editor"):
        return {
            "X-Tenant-Id": tenant,
            "X-User-Id": self.user,
            "X-User-Role": role,
            "Idempotency-Key": f"idem-{uuid4().hex}",
        }

    def test_tenant_level_disable_isolates_tenant_a_without_affecting_tenant_b(self):
        # Tenant A disables motion_template_plugin for itself only
        r_put = self.client.put(
            "/api/v1/tenant/plugins/motion_template_plugin",
            json={"enabled": False},
            headers=self._hdr(self.tenant_a, role="admin"),
        )
        self.assertEqual(r_put.status_code, 200)
        self.assertFalse(r_put.json()["effective_enabled"])

        # Tenant A is blocked with 403 PLUGIN_DISABLED_FOR_TENANT
        r_a = self.client.post(
            "/api/v1/plugins/motion-template/compile",
            json={"motion_md": MINI_MOTION_MD},
            headers=self._hdr(self.tenant_a),
        )
        self.assertEqual(r_a.status_code, 403)
        self.assertEqual(r_a.json()["code"], "PLUGIN_DISABLED_FOR_TENANT")

        # Tenant B is completely unaffected (200 OK)
        r_b = self.client.post(
            "/api/v1/plugins/motion-template/compile",
            json={"motion_md": MINI_MOTION_MD},
            headers=self._hdr(self.tenant_b),
        )
        self.assertEqual(r_b.status_code, 200)

        # GET /api/v1/tenant/plugins shows effective_enabled=False for A, True for B
        list_a = {p["plugin_id"]: p for p in self.client.get("/api/v1/tenant/plugins", headers=self._hdr(self.tenant_a)).json()["plugins"]}
        list_b = {p["plugin_id"]: p for p in self.client.get("/api/v1/tenant/plugins", headers=self._hdr(self.tenant_b)).json()["plugins"]}
        self.assertFalse(list_a["motion_template_plugin"]["effective_enabled"])
        self.assertTrue(list_b["motion_template_plugin"]["effective_enabled"])

    def test_tenant_default_brand_kit_config_is_auto_injected_and_isolated(self):
        # Tenant B configures default BrandKit on motion_template_plugin
        r_cfg = self.client.put(
            "/api/v1/tenant/plugins/motion_template_plugin",
            json={
                "enabled": True,
                "config": {"brand_kit": {"brand_name": "HOROW 希箭", "cta_text": "立即选购"}},
            },
            headers=self._hdr(self.tenant_b, role="admin"),
        )
        self.assertEqual(r_cfg.status_code, 200)

        # Tenant B compiles WITHOUT passing brand_kit -> inherits Tenant B's saved BrandKit
        r_b = self.client.post(
            "/api/v1/plugins/motion-template/compile",
            json={"motion_md": MINI_MOTION_MD, "subtitle_track_id": str(uuid4())},
            headers=self._hdr(self.tenant_b),
        )
        self.assertEqual(r_b.status_code, 200)
        self.assertEqual(r_b.json()["template"]["title"], "HOROW 希箭 Promo")
        self.assertEqual(r_b.json()["operations"][0]["payload"]["text"], "HOROW 希箭 - 立即选购")

        # Tenant A does NOT see Tenant B's BrandKit (falls back to Motion.md default)
        r_a = self.client.post(
            "/api/v1/plugins/motion-template/compile",
            json={"motion_md": MINI_MOTION_MD, "subtitle_track_id": str(uuid4())},
            headers=self._hdr(self.tenant_a),
        )
        self.assertEqual(r_a.status_code, 200)
        self.assertEqual(r_a.json()["template"]["title"], "FallbackBrand Promo")

    def test_tenant_disable_route_does_not_disable_other_tenants(self):
        self.client.put(
            "/api/v1/tenant/plugins/motion_template_plugin",
            json={"enabled": True},
            headers=self._hdr(self.tenant_b, role="admin"),
        )
        disabled = self.client.post("/api/v1/plugins/motion_template_plugin/disable", headers=self._hdr(self.tenant_b, role="admin"))
        self.assertFalse(disabled.json()["tenant_enabled"])
        self.assertTrue(disabled.json()["global_enabled"])

        r_b = self.client.post(
            "/api/v1/plugins/motion-template/compile",
            json={"motion_md": MINI_MOTION_MD},
            headers=self._hdr(self.tenant_b),
        )
        self.assertEqual(r_b.status_code, 403)
        self.assertEqual(r_b.json()["code"], "PLUGIN_DISABLED_FOR_TENANT")

        r_a = self.client.post(
            "/api/v1/plugins/motion-template/compile",
            json={"motion_md": MINI_MOTION_MD},
            headers=self._hdr(self.tenant_a),
        )
        self.assertEqual(r_a.status_code, 200)


if __name__ == "__main__":
    unittest.main()
