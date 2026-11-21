"""Phase 4 Stage 1 — Production Container & Deployment Artifact Verification Tests."""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


class Phase4DeploymentArtifactsTest(unittest.TestCase):
    def test_production_compose_is_valid_yaml_with_no_hardcoded_passwords(self) -> None:
        compose_path = ROOT / "infra/compose/docker-compose.prod.yml"
        self.assertTrue(compose_path.exists())
        raw = compose_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
        self.assertIn("services", data)
        for svc in ("postgres", "api", "web"):
            self.assertIn(svc, data["services"])
        self.assertEqual(data["services"]["postgres"]["image"], "postgres:15-alpine")
        self.assertNotIn("postgres_dev_password", raw)
        self.assertIn("${POSTGRES_PASSWORD:?", raw)

    def test_dockerfiles_and_nginx_enforce_security_cjk_and_upload_limits(self) -> None:
        api_df = (ROOT / "infra/docker/Dockerfile.api").read_text(encoding="utf-8")
        self.assertIn("ffmpeg", api_df)
        self.assertIn("fonts-noto-cjk", api_df)
        self.assertIn("USER aivs", api_df)
        self.assertIn("HEALTHCHECK", api_df)
        self.assertNotIn("ultralytics", api_df.lower())

        web_df = (ROOT / "infra/docker/Dockerfile.web").read_text(encoding="utf-8")
        self.assertIn("node:20-alpine", web_df)
        self.assertIn("nginx:1.27-alpine", web_df)
        self.assertIn("npm run build", web_df)

        nginx_cfg = (ROOT / "infra/nginx/nginx.conf").read_text(encoding="utf-8")
        self.assertIn("client_max_body_size 2G", nginx_cfg)
        self.assertIn("proxy_pass http://api:8000/api/", nginx_cfg)


if __name__ == "__main__":
    unittest.main()
