"""Phase 4 Stage 4 Tests: Async Render Job Queue, Tenant Isolation & Lease-Token Fencing Cancellation."""
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from apps.api.main import create_app
from services.infrastructure.store import TenantContext
from services.media_engine.adapters.openshorts_adapter import file_sha256


class TestPhase4AsyncRenderJobs(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aivs_p4_jobs_")
        self.sample_video = str(Path(self.tmpdir) / "sample_2s.mp4")
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
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
            json={"filename": "s.mp4", "size_bytes": Path(self.sample_video).stat().st_size, "kind": "video"},
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
            json={"storyboard_id": sb["storyboard_id"], "storyboard_version": 1, "profile": {"width": 320, "height": 240, "frame_rate": {"num": 30, "den": 1}, "timeline_timebase": {"num": 1, "den": 30000}, "audio_sample_rate": 48000}},
            headers=self._hdr(),
        ).json()
        return tl["timeline_id"]

    def test_async_render_job_completes_with_artifact_qa_and_tenant_isolation(self):
        tl_id = self._bootstrap_timeline()
        r_submit = self.client.post(
            f"/api/v1/timelines/{tl_id}/render-jobs",
            json={"timeline_version": 1},
            headers=self._hdr(),
        )
        self.assertEqual(r_submit.status_code, 202)
        job_id = r_submit.json()["job_id"]

        # Cross-tenant GET must return 404
        r_other = self.client.get(f"/api/v1/render-jobs/{job_id}", headers=self._hdr(tenant=self.tenant_b))
        self.assertEqual(r_other.status_code, 404)

        # Poll until succeeded
        final_job = None
        for _ in range(100):
            st = self.client.get(f"/api/v1/render-jobs/{job_id}", headers=self._hdr()).json()
            if st["status"] in ("succeeded", "failed"):
                final_job = st
                break
            time.sleep(0.1)

        self.assertIsNotNone(final_job)
        self.assertEqual(final_job["status"], "succeeded", f"Job failed: {final_job}")
        self.assertEqual(final_job["progress_pct"], 100)
        self.assertTrue(final_job["artifact_id"])
        self.assertTrue(final_job["qa_report_id"])

        # Download artifact works
        dl = self.client.get(f"/api/v1/artifacts/{final_job['artifact_id']}/download", headers=self._hdr())
        self.assertEqual(dl.status_code, 200)
        self.assertGreater(len(dl.content), 1000)

    def test_cancel_bumps_fencing_lease_token_and_rejects_stale_commit(self):
        mgr = self.app.state.render_job_mgr
        ctx = TenantContext(tenant_id=self.tenant_a, user_id=self.user_a, role="editor", trace_id="t-1")
        job = mgr.submit_job(ctx, str(uuid4()), 1, "final", lambda: {"artifact_id": "a1"}, auto_start=False)
        self.assertEqual(job["lease_token"], 1)

        # Cancel via HTTP API
        r_cancel = self.client.post(f"/api/v1/render-jobs/{job['job_id']}/cancel", headers=self._hdr())
        self.assertEqual(r_cancel.status_code, 200)
        self.assertEqual(r_cancel.json()["status"], "canceled")
        self.assertEqual(r_cancel.json()["lease_token"], 2)

        # Stale worker holding lease_token=1 MUST be rejected
        committed = mgr.try_commit_with_fencing(job["job_id"], lease_token=1, result={"artifact_id": "stale-art"})
        self.assertFalse(committed)
        current = mgr.get_job(ctx, job["job_id"])
        self.assertEqual(current["status"], "canceled")
        self.assertIsNone(current["artifact_id"])


if __name__ == "__main__":
    unittest.main()
