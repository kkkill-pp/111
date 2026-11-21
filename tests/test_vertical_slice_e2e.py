"""End-to-End & Regression Test Suite — Sprint 1 Vertical Slice (Real FastAPI + Real FFmpeg/ffprobe)."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from apps.api.main import create_app
from services.infrastructure.errors import AppError
from services.media_engine.adapters.openshorts_adapter import OpenShortsAdapter, file_sha256
from services.media_engine.ffmpeg_renderer import FFmpegRendererAdapter, RenderPlanCompiler


class VerticalSliceE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="aivs_e2e_")
        self.app = create_app(media_root=self.tmpdir)
        self.client = TestClient(self.app)

        self.tenant_a = str(uuid4())
        self.tenant_b = str(uuid4())
        self.user_a = str(uuid4())
        self.user_b = str(uuid4())

        # Synthesize a REAL 4-second 1280x720 30fps video + 48kHz stereo audio MP4 via ffmpeg
        self.sample_video = str(Path(self.tmpdir) / "source_4s.mp4")
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=4",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ac", "2", "-ar", "48000",
                self.sample_video,
            ],
            check=True,
            timeout=60,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _hdr(self, tenant: str, user: str, role: str, idem: str | None = None) -> dict[str, str]:
        h = {"X-Tenant-Id": tenant, "X-User-Id": user, "X-User-Role": role, "X-Trace-Id": f"trace-{uuid4().hex[:8]}"}
        if idem:
            h["Idempotency-Key"] = idem
        return h

    def test_full_hitl_vertical_slice_with_real_subtitle_burnin_and_honest_qa(self) -> None:
        # 1. Create Workspace & Project + Idempotency replay check (TASK-02)
        ws_idem = f"idem-ws-{uuid4().hex}"
        r_ws1 = self.client.post("/api/v1/workspaces", json={"name": "Studio A"}, headers=self._hdr(self.tenant_a, self.user_a, "owner", ws_idem))
        self.assertEqual(r_ws1.status_code, 201, r_ws1.text)
        r_ws2 = self.client.post("/api/v1/workspaces", json={"name": "Studio A"}, headers=self._hdr(self.tenant_a, self.user_a, "owner", ws_idem))
        self.assertEqual(r_ws2.json()["id"], r_ws1.json()["id"])
        ws_id = r_ws1.json()["id"]

        r_proj = self.client.post("/api/v1/projects", json={"workspace_id": ws_id, "name": "Podcast Ep 1"}, headers=self._hdr(self.tenant_a, self.user_a, "editor", f"idem-proj-{uuid4().hex}"))
        self.assertEqual(r_proj.status_code, 201, r_proj.text)
        proj_id = r_proj.json()["id"]

        # Cross-tenant isolation check (SEC-01)
        r_cross = self.client.get(f"/api/v1/projects/{proj_id}", headers=self._hdr(self.tenant_b, self.user_b, "owner"))
        self.assertEqual(r_cross.status_code, 404)

        # 2. Upload -> Real ffprobe Probe -> Proxy/Thumbnail/Analysis
        size_bytes = Path(self.sample_video).stat().st_size
        checksum = file_sha256(self.sample_video)
        r_up = self.client.post(
            f"/api/v1/projects/{proj_id}/upload-sessions",
            json={"filename": "source_4s.mp4", "size_bytes": size_bytes, "kind": "video"},
            headers=self._hdr(self.tenant_a, self.user_a, "editor", f"idem-up-{uuid4().hex}"),
        )
        self.assertEqual(r_up.status_code, 201)
        sess = r_up.json()
        shutil.copyfile(self.sample_video, sess["storage_key"])

        r_comp = self.client.post(
            f"/api/v1/upload-sessions/{sess['id']}/complete",
            json={"checksum_sha256": checksum},
            headers=self._hdr(self.tenant_a, self.user_a, "editor", f"idem-comp-{uuid4().hex}"),
        )
        self.assertEqual(r_comp.status_code, 202)
        asset_id = r_comp.json()["asset_id"]

        r_an = self.client.post(f"/api/v1/assets/{asset_id}/analyze", headers=self._hdr(self.tenant_a, self.user_a, "editor", f"idem-an-{uuid4().hex}"))
        self.assertEqual(r_an.status_code, 202)

        # 3. Creative Brief -> Storyboard -> Unaccepted Blocked -> Approved -> Timeline v1
        r_br = self.client.post(
            f"/api/v1/projects/{proj_id}/briefs",
            json={
                "title": "Shorts Brief",
                "brief": {
                    "target_platform": "youtube_shorts",
                    "audience": "creators",
                    "objective": "education",
                    "language": "zh",
                    "duration_range_sec": {"min": 5, "max": 60},
                    "aspect_ratio": "9:16",
                },
            },
            headers=self._hdr(self.tenant_a, self.user_a, "editor", f"idem-br-{uuid4().hex}"),
        )
        self.assertEqual(r_br.status_code, 201)
        brief_version_id = r_br.json()["brief_version_id"]

        r_sb = self.client.post(
            f"/api/v1/projects/{proj_id}/storyboards",
            json={"brief_version_id": brief_version_id, "asset_id": asset_id},
            headers=self._hdr(self.tenant_a, self.user_a, "editor", f"idem-sb-{uuid4().hex}"),
        )
        self.assertEqual(r_sb.status_code, 202)
        sb_id, sb_chk = r_sb.json()["storyboard_id"], r_sb.json()["proposal_checksum"]

        profile = {
            "width": 1080,
            "height": 1920,
            "frame_rate": {"num": 30, "den": 1},
            "timeline_timebase": {"num": 1, "den": 30000},
            "audio_sample_rate": 48000,
        }
        r_tl_blocked = self.client.post(
            f"/api/v1/projects/{proj_id}/timelines",
            json={"storyboard_id": sb_id, "storyboard_version": 1, "profile": profile},
            headers=self._hdr(self.tenant_a, self.user_a, "editor", f"idem-tl-blk-{uuid4().hex}"),
        )
        self.assertEqual(r_tl_blocked.status_code, 409)

        self.assertEqual(
            self.client.post(
                f"/api/v1/storyboards/{sb_id}/reviews",
                json={"subject_version": "1", "subject_checksum": sb_chk, "decision": "approve"},
                headers=self._hdr(self.tenant_a, self.user_a, "reviewer", f"idem-sbrev-{uuid4().hex}"),
            ).status_code,
            201,
        )

        r_tl = self.client.post(
            f"/api/v1/projects/{proj_id}/timelines",
            json={"storyboard_id": sb_id, "storyboard_version": 1, "profile": profile},
            headers=self._hdr(self.tenant_a, self.user_a, "editor", f"idem-tl-{uuid4().hex}"),
        )
        self.assertEqual(r_tl.status_code, 201)
        tl_id = r_tl.json()["timeline_id"]

        # 4. Agent Proposes TRIM_CLIP + ADD_SUBTITLE (viewer 403; timeline stays v1)
        self.assertEqual(
            self.client.post(
                f"/api/v1/timelines/{tl_id}/agent-proposals",
                json={"base_version": 1, "user_intent": "Trim opening 1s"},
                headers=self._hdr(self.tenant_a, self.user_a, "viewer", f"idem-ag-deny-{uuid4().hex}"),
            ).status_code,
            403,
        )
        r_ag = self.client.post(
            f"/api/v1/timelines/{tl_id}/agent-proposals",
            json={"base_version": 1, "user_intent": "Trim opening 1s and add caption"},
            headers=self._hdr(self.tenant_a, self.user_a, "editor", f"idem-ag-{uuid4().hex}"),
        )
        self.assertEqual(r_ag.status_code, 200)
        prop_id, prop_chk = r_ag.json()["proposal_id"], r_ag.json()["proposal_checksum"]
        all_op_ids = [c["operation_id"] for c in r_ag.json()["diff"]["changes"]]
        self.assertEqual(len(all_op_ids), 2)

        # 5. Human Approves BOTH operations (TRIM_CLIP + ADD_SUBTITLE) -> Single-use Token -> Apply -> Timeline v2
        r_pr_rev = self.client.post(
            f"/api/v1/proposals/{prop_id}/reviews",
            json={"subject_version": "1", "subject_checksum": prop_chk, "decision": "approve", "selected_items": all_op_ids},
            headers=self._hdr(self.tenant_a, self.user_a, "editor", f"idem-prrev-{uuid4().hex}"),
        )
        self.assertEqual(r_pr_rev.status_code, 201)
        token = r_pr_rev.json()["approval_token"]

        r_apply = self.client.post(
            f"/api/v1/timelines/{tl_id}/operations",
            json={"base_version": 1, "proposal_id": prop_id, "approval_token": token},
            headers=self._hdr(self.tenant_a, self.user_a, "editor", f"idem-apply-{uuid4().hex}"),
        )
        self.assertEqual(r_apply.status_code, 200)
        self.assertEqual(r_apply.json()["new_version"], 2)

        # Replaying consumed token MUST fail 409
        self.assertEqual(
            self.client.post(
                f"/api/v1/timelines/{tl_id}/operations",
                json={"base_version": 1, "proposal_id": prop_id, "approval_token": token},
                headers=self._hdr(self.tenant_a, self.user_a, "editor", f"idem-replay-{uuid4().hex}"),
            ).status_code,
            409,
        )

        # 6. Render Preview from v1 (no subtitle) vs v2 (trimmed + REAL ASS subtitle burned in)
        r_prev_v1 = self.client.post(
            f"/api/v1/timelines/{tl_id}/previews",
            json={"timeline_version": 1},
            headers=self._hdr(self.tenant_a, self.user_a, "editor", f"idem-prev-v1-{uuid4().hex}"),
        )
        self.assertEqual(r_prev_v1.status_code, 202, r_prev_v1.text)
        r_prev_v2 = self.client.post(
            f"/api/v1/timelines/{tl_id}/previews",
            json={"timeline_version": 2},
            headers=self._hdr(self.tenant_a, self.user_a, "editor", f"idem-prev-v2-{uuid4().hex}"),
        )
        self.assertEqual(r_prev_v2.status_code, 202, r_prev_v2.text)
        self.assertNotEqual(r_prev_v1.json()["artifact_checksum"], r_prev_v2.json()["artifact_checksum"])
        art_id, art_chk = r_prev_v2.json()["artifact_id"], r_prev_v2.json()["artifact_checksum"]

        # 7. Real ffprobe QA + verify P1-B honest NOT_IMPLEMENTED content marker
        r_qa = self.client.post(f"/api/v1/artifacts/{art_id}/qa", headers=self._hdr(self.tenant_a, self.user_a, "reviewer", f"idem-qa-{uuid4().hex}"))
        self.assertEqual(r_qa.status_code, 202)
        self.assertEqual(r_qa.json()["qa_status"], "passed")
        qa_id = r_qa.json()["qa_report_id"]

        # Inspect stored QA report to prove AI semantic QA is NOT faked as passed: True
        qa_rec = self.app.state.store._tables["qa_reports"][qa_id]
        sem_check = next(c for c in qa_rec["content_checks"] if c["code"] == "CONTENT_SEMANTIC_QA_NOT_IMPLEMENTED")
        self.assertFalse(sem_check["passed"], "Unimplemented AI semantic QA must NOT report passed=True")

        # 8. Approve Artifact -> Delivery -> Undo v2 -> immutable v3
        r_art_rev = self.client.post(
            f"/api/v1/artifacts/{art_id}/reviews",
            json={"subject_version": "2", "subject_checksum": art_chk, "qa_report_id": qa_id, "decision": "approve"},
            headers=self._hdr(self.tenant_a, self.user_a, "reviewer", f"idem-artrev-{uuid4().hex}"),
        )
        self.assertEqual(r_art_rev.status_code, 201)
        r_deliv = self.client.post(
            f"/api/v1/artifacts/{art_id}/deliveries",
            json={"human_review_id": r_art_rev.json()["id"], "destination_type": "signed_download"},
            headers=self._hdr(self.tenant_a, self.user_a, "owner", f"idem-deliv-{uuid4().hex}"),
        )
        self.assertEqual(r_deliv.status_code, 202)

        r_undo = self.client.post(
            f"/api/v1/timelines/{tl_id}/undo",
            json={"base_version": 2, "target_version": 1},
            headers=self._hdr(self.tenant_a, self.user_a, "editor", f"idem-undo-{uuid4().hex}"),
        )
        self.assertEqual(r_undo.status_code, 200)
        self.assertEqual(r_undo.json()["new_version"], 3)

    def test_renderer_forbids_silent_semantic_fallback_and_applies_speed(self) -> None:
        """P0-A regression test: unimplemented overlay/transition MUST fail loudly; speed MUST take effect."""
        adapter = OpenShortsAdapter()
        compiler = RenderPlanCompiler()
        renderer = FFmpegRendererAdapter()
        asset_id = str(uuid4())
        asset_lookup = {
            asset_id: {
                "id": asset_id,
                "storage_key": self.sample_video,
                "checksum_sha256": file_sha256(self.sample_video),
                "is_vfr": False,
            }
        }
        base_snap = {
            "schema_version": "2.0",
            "timeline_id": str(uuid4()),
            "project_id": str(uuid4()),
            "version": 1,
            "parent_version": None,
            "profile": {
                "width": 1080,
                "height": 1920,
                "frame_rate": {"num": 30, "den": 1},
                "timeline_timebase": {"num": 1, "den": 30000},
                "audio_sample_rate": 48000,
            },
            "tracks": [
                {
                    "id": str(uuid4()),
                    "type": "video",
                    "order": 0,
                    "muted": False,
                    "clips": [{
                        "id": str(uuid4()),
                        "asset_id": asset_id,
                        "source_range": {"in": 0, "out": 120000, "timebase": {"num": 1, "den": 30000}, "vfr_pts_map_ref": None},
                        "timeline_start": 0,
                        "duration": 120000,
                        "speed": 2.0,
                        "transform": {"mode": "fit"},
                        "audio": {"enabled": True, "volume": 1.0},
                        "transitions": [],
                    }],
                }
            ],
        }

        # 1. Speed 2.0x on 4s source MUST render ~2.0s output (NOT silently ignored!)
        plan_speed, _ = compiler.compile(base_snap, asset_lookup, render_mode="preview")
        out_2x = str(Path(self.tmpdir) / "out_2x.mp4")
        renderer.render(plan_speed, out_2x)
        probed = adapter.probe_asset(out_2x)
        actual_sec = probed.duration_num / probed.duration_den
        self.assertLess(abs(actual_sec - 2.0), 0.15, f"Expected ~2.0s at 2x speed, got {actual_sec:.3f}s")

        # 2. Non-empty overlay_ops MUST fail loudly (ADR-004)
        snap_overlay = {
            **base_snap,
            "tracks": [
                *base_snap["tracks"],
                {
                    "id": str(uuid4()),
                    "type": "overlay",
                    "order": 1,
                    "items": [{"id": str(uuid4()), "kind": "text_hook", "timeline_start": 0, "duration": 30000, "text": "Hook"}],
                },
            ],
        }
        plan_ov, _ = compiler.compile(snap_overlay, asset_lookup, render_mode="preview")
        with self.assertRaises(AppError) as ctx_err:
            renderer.render(plan_ov, str(Path(self.tmpdir) / "out_ov.mp4"))
        self.assertEqual(ctx_err.exception.status_code, 422)
        self.assertIn("ADR-004", ctx_err.exception.message)


if __name__ == "__main__":
    unittest.main()
