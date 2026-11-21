"""Phase 2 Stage 3 — Pluggable LLMProvider & Anti-Hallucination Schema Guard Tests."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from apps.api.main import create_app
from services.agent_runtime.llm_provider import (
    OpenAICompatibleProvider,
    PlannedProposal,
    RuleBasedDeterministicProvider,
)
from services.infrastructure.errors import AppError
from services.media_engine.adapters.openshorts_adapter import file_sha256


class MaliciousHallucinatingProvider:
    provider_name = "mock_bad_llm"
    model_id = "bad-llm-0"

    def plan_edit_operations(self, timeline_snapshot: dict, user_intent: str) -> PlannedProposal:
        return PlannedProposal(
            provider_name=self.provider_name,
            model_id=self.model_id,
            rationale="Attempt forbidden UNDO inside EditOperation",
            risk_level="high",
            operations=[{"operation_id": str(uuid4()), "operation_type": "UNDO", "payload": {"target_version": 1}}],
            evidence_refs=["mock:bad"],
        )


class Phase2LLMProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="aivs_llm_")
        self.app = create_app(media_root=self.tmpdir, database_url="")
        self.client = TestClient(self.app)
        self.tenant = str(uuid4())
        self.user = str(uuid4())
        self.sample_video = str(Path(self.tmpdir) / "s.mp4")
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=2",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ac", "2", "-ar", "48000", self.sample_video,
            ],
            check=True, timeout=30,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _hdr(self, role: str = "editor", idem: str | None = None) -> dict[str, str]:
        h = {"X-Tenant-Id": self.tenant, "X-User-Id": self.user, "X-User-Role": role, "X-Trace-Id": f"t-{uuid4().hex[:8]}"}
        if idem:
            h["Idempotency-Key"] = idem
        return h

    def _setup_timeline_with_clip(self) -> tuple[str, str]:
        ws = self.client.post("/api/v1/workspaces", json={"name": "W"}, headers=self._hdr("owner", f"idem-{uuid4().hex}")).json()
        proj = self.client.post("/api/v1/projects", json={"workspace_id": ws["id"], "name": "P"}, headers=self._hdr("editor", f"idem-{uuid4().hex}")).json()
        raw = Path(self.sample_video).read_bytes()
        up = self.client.post(f"/api/v1/projects/{proj['id']}/upload-sessions", json={"filename": "s.mp4", "size_bytes": len(raw), "kind": "video"}, headers=self._hdr("editor", f"idem-{uuid4().hex}")).json()
        self.client.put(f"/api/v1/upload-sessions/{up['id']}/content", content=raw, headers=self._hdr("editor"))
        comp = self.client.post(f"/api/v1/upload-sessions/{up['id']}/complete", json={"checksum_sha256": file_sha256(self.sample_video)}, headers=self._hdr("editor", f"idem-{uuid4().hex}")).json()
        profile = {"width": 320, "height": 180, "frame_rate": {"num": 30, "den": 1}, "timeline_timebase": {"num": 1, "den": 30000}, "audio_sample_rate": 48000}
        tl = self.client.post(f"/api/v1/projects/{proj['id']}/timelines", json={"profile": profile}, headers=self._hdr("editor", f"idem-{uuid4().hex}")).json()
        tl_id = tl["timeline_id"]
        v_track = tl["tracks"][0]["id"]
        self.client.post(
            f"/api/v1/timelines/{tl_id}/operations",
            json={"base_version": 1, "operations": [{"operation_id": str(uuid4()), "operation_type": "ADD_CLIP", "payload": {"track_id": v_track, "asset_id": comp["asset_id"], "source_in": 0, "source_out": 60000, "timeline_start": 0}}]},
            headers=self._hdr("editor", f"idem-{uuid4().hex}"),
        )
        clip_id = self.client.get(f"/api/v1/timelines/{tl_id}", headers=self._hdr("editor")).json()["tracks"][0]["clips"][0]["id"]
        return tl_id, clip_id

    def test_deterministic_provider_parses_speed_and_mute_intents(self) -> None:
        tl_id, _ = self._setup_timeline_with_clip()
        r = self.client.post(
            f"/api/v1/timelines/{tl_id}/agent-proposals",
            json={"base_version": 2, "user_intent": "2.0x 倍速并静音原声", "permission_level": "L1"},
            headers=self._hdr("editor", f"idem-{uuid4().hex}"),
        )
        self.assertEqual(r.status_code, 200, r.text)
        op_types = [c["type"] for c in r.json()["diff"]["changes"]]
        self.assertIn("UPDATE_SPEED", op_types)
        self.assertIn("UPDATE_VOLUME", op_types)

    def test_anti_hallucination_guard_blocks_forbidden_undo_from_llm_provider(self) -> None:
        tl_id, _ = self._setup_timeline_with_clip()
        self.app.state.agent_harness.set_provider(MaliciousHallucinatingProvider())
        r = self.client.post(
            f"/api/v1/timelines/{tl_id}/agent-proposals",
            json={"base_version": 2, "user_intent": "Undo everything", "permission_level": "L1"},
            headers=self._hdr("editor", f"idem-{uuid4().hex}"),
        )
        self.assertEqual(r.status_code, 422, r.text)

    def test_openai_compatible_provider_parses_structured_json_response(self) -> None:
        clip_id = str(uuid4())
        prov = OpenAICompatibleProvider(base_url="http://localhost:11434/v1", model_id="qwen2.5:14b")
        prov._post_json = lambda url, body, headers: {  # type: ignore[method-assign]
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "rationale": "Speed up hook to 1.5x",
                        "risk_level": "low",
                        "operations": [{"operation_type": "UPDATE_SPEED", "payload": {"clip_id": clip_id, "speed": 1.5}}],
                    })
                }
            }]
        }
        planned = prov.plan_edit_operations({"version": 1, "tracks": []}, "1.5x speed")
        self.assertEqual(planned.provider_name, "openai_compatible")
        self.assertEqual(planned.model_id, "qwen2.5:14b")
        self.assertEqual(planned.operations[0]["operation_type"], "UPDATE_SPEED")
        self.assertEqual(planned.operations[0]["payload"]["speed"], 1.5)


if __name__ == "__main__":
    unittest.main()
