"""Phase 4 Stage 2 Tests: Observability (JSON logs, Prometheus /metrics), Rate Limiting, Upload Guard & CI Docker Gate."""
import unittest
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from apps.api.main import create_app
from services.infrastructure.observability import redact_sensitive


REPO_ROOT = Path(__file__).resolve().parents[1]


class TestPhase4ObservabilityAndHardening(unittest.TestCase):
    def _headers(self, tenant_id: str = "tenant-obs-1", trace_id: str | None = None) -> dict[str, str]:
        h = {
            "X-Tenant-Id": tenant_id,
            "X-User-Id": "user-obs-1",
            "X-User-Role": "admin",
        }
        if trace_id:
            h["X-Trace-Id"] = trace_id
        return h

    def test_trace_id_propagation_and_security_headers(self):
        app = create_app(database_url="")
        client = TestClient(app)

        # 1) Echo caller-provided X-Trace-Id
        r1 = client.get("/healthz", headers={"X-Trace-Id": "trace-custom-123"})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.headers.get("X-Trace-Id"), "trace-custom-123")
        self.assertEqual(r1.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r1.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(r1.headers.get("Referrer-Policy"), "no-referrer")

        # 2) Auto-generate X-Trace-Id when absent and set Cache-Control: no-store on /api/
        r2 = client.get("/api/v1/capabilities")
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(len(r2.headers.get("X-Trace-Id", "")) >= 16)
        self.assertEqual(r2.headers.get("Cache-Control"), "no-store")

    def test_prometheus_metrics_endpoint_records_requests(self):
        app = create_app(database_url="")
        client = TestClient(app)

        client.get("/healthz")
        client.get("/api/v1/capabilities")

        m = client.get("/metrics")
        self.assertEqual(m.status_code, 200)
        self.assertIn("text/plain", m.headers.get("content-type", ""))
        body = m.text
        self.assertIn("aivs_uptime_seconds", body)
        self.assertIn('aivs_http_requests_total{method="GET",path="/healthz",status_class="2xx"} 1', body)
        self.assertIn('aivs_http_requests_total{method="GET",path="/api/v1/capabilities",status_class="2xx"} 1', body)
        self.assertIn('aivs_http_request_duration_ms_count{method="GET",path="/api/v1/capabilities"} 1', body)

    def test_structured_json_logging_and_secret_redaction(self):
        app = create_app(database_url="")
        client = TestClient(app)

        client.get("/api/v1/capabilities", headers=self._headers("tenant-log-a", trace_id="trace-log-99"))
        logs = app.state.structured_logger.recent(10)
        self.assertTrue(any(
            rec.get("event") == "http.request"
            and rec.get("trace_id") == "trace-log-99"
            and rec.get("tenant_id") == "tenant-log-a"
            and rec.get("status_code") == 200
            and isinstance(rec.get("duration_ms"), (int, float))
            for rec in logs
        ))

        sanitized = redact_sensitive({
            "authorization": "Bearer secret-jwt",
            "nested": {"api_key": "sk-live-123", "approval_token": "tok-1", "safe": "ok"},
        })
        self.assertEqual(sanitized["authorization"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["api_key"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["approval_token"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["safe"], "ok")

    def test_per_tenant_rate_limiter_enforces_429_and_isolates_tenants(self):
        app = create_app(database_url="", rate_limit_max=2, rate_limit_window_sec=60.0)
        client = TestClient(app)

        # Tenant A uses its 2-request quota
        self.assertEqual(client.get("/api/v1/capabilities", headers=self._headers("tenant-A")).status_code, 200)
        self.assertEqual(client.get("/api/v1/capabilities", headers=self._headers("tenant-A")).status_code, 200)

        # 3rd request for Tenant A is rate limited with 429 + Retry-After
        r_blocked = client.get("/api/v1/capabilities", headers=self._headers("tenant-A"))
        self.assertEqual(r_blocked.status_code, 429)
        self.assertEqual(r_blocked.json()["code"], "RATE_LIMITED")
        self.assertIn("Retry-After", r_blocked.headers)

        # Tenant B is completely unaffected
        r_tenant_b = client.get("/api/v1/capabilities", headers=self._headers("tenant-B"))
        self.assertEqual(r_tenant_b.status_code, 200)

        # /healthz and /metrics remain exempt from rate limiting
        self.assertEqual(client.get("/healthz", headers=self._headers("tenant-A")).status_code, 200)
        m = client.get("/metrics", headers=self._headers("tenant-A"))
        self.assertEqual(m.status_code, 200)
        self.assertIn('aivs_rate_limit_rejected_total{tenant="tenant-A"} 1', m.text)

    def test_upload_size_guard_rejects_oversized_payload_with_413(self):
        app = create_app(database_url="", max_upload_bytes=1024)
        client = TestClient(app)

        r = client.post(
            "/api/v1/workspaces",
            headers={**self._headers("tenant-size"), "Idempotency-Key": "idem-size-0001", "Content-Length": "4096"},
            content=b"x" * 2048,
        )
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.json()["code"], "PAYLOAD_TOO_LARGE")

        m = client.get("/metrics")
        self.assertIn("aivs_upload_size_rejected_total 1", m.text)

    def test_ci_workflow_valid_yaml_with_docker_build_gate(self):
        wf_path = REPO_ROOT / ".github/workflows/sprint0-db-gate.yml"
        data = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
        self.assertIn("jobs", data)
        steps = data["jobs"]["contract-and-db-gate"]["steps"]
        run_cmds = "\n".join(s.get("run", "") for s in steps)
        self.assertIn("infra/docker/Dockerfile.api", run_cmds)
        self.assertIn("infra/docker/Dockerfile.web", run_cmds)


if __name__ == "__main__":
    unittest.main()
