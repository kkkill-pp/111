"""Phase 4 Stage 3 Tests: 18/18 Full EditOperation Coverage (MERGE_CLIP, REORDER_CLIP, SET_CANVAS_PROFILE + real FFmpeg ffprobe)."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from apps.api.main import create_app
from services.media_engine.adapters.openshorts_adapter import OpenShortsAdapter, file_sha256


class TestPhase4FullOpCoverage(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aivs_p4_ops_")
        self.sample_video = str(Path(self.tmpdir) / "sample_4s.mp4")
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=4:size=640x360:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", self.sample_video,
            ],
            check=True, capture_output=True,
        )
        self.app = create_app(media_root=self.tmpdir, database_url="")
        self.client = TestClient(self.app)
        self.tenant = str(uuid4())
        self.user = str(uuid4())

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _hdr(self, role="editor"):
        return {
            "X-Tenant-Id": self.tenant,
            "X-User-Id": self.user,
            "X-User-Role": role,
            "Idempotency-Key": f"idem-{uuid4().hex}",
        }

    def _bootstrap_timeline(self):
        ws = self.client.post("/api/v1/workspaces", json={"name": "W"}, headers=self._hdr("owner")).json()
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
            json={"title": "B", "brief": {"target_platform": "youtube_shorts", "audience": "creators", "objective": "education", "language": "zh", "duration_range_sec": {"min": 5, "max": 60}, "aspect_ratio": "9:16"}},
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
            headers=self._hdr("reviewer"),
        )
        tl = self.client.post(
            f"/api/v1/projects/{proj['id']}/timelines",
            json={"storyboard_id": sb["storyboard_id"], "storyboard_version": 1, "profile": {"width": 640, "height": 360, "frame_rate": {"num": 30, "den": 1}, "timeline_timebase": {"num": 1, "den": 30000}, "audio_sample_rate": 48000}},
            headers=self._hdr(),
        ).json()
        return tl["timeline_id"]

    def test_split_reorder_merge_and_set_canvas_profile_with_real_ffmpeg_probe(self):
        tl_id = self._bootstrap_timeline()
        v1 = self.client.get(f"/api/v1/timelines/{tl_id}", headers=self._hdr()).json()
        vtrack = next(t for t in v1["tracks"] if t["type"] == "video")
        orig_clip = vtrack["clips"][0]
        orig_dur = orig_clip["duration"]

        # 1) SPLIT_CLIP into two halves
        r_split = self.client.post(
            f"/api/v1/timelines/{tl_id}/operations",
            json={"base_version": 1, "operations": [{"operation_id": str(uuid4()), "operation_type": "SPLIT_CLIP", "payload": {"clip_id": orig_clip["id"], "split_at_timeline_tick": orig_dur // 2}}]},
            headers=self._hdr(),
        )
        self.assertEqual(r_split.status_code, 200)
        v2 = self.client.get(f"/api/v1/timelines/{tl_id}", headers=self._hdr()).json()
        clips_v2 = next(t for t in v2["tracks"] if t["type"] == "video")["clips"]
        self.assertEqual(len(clips_v2), 2)
        c1, c2 = clips_v2[0], clips_v2[1]

        # 2) REORDER_CLIP reverses [c2, c1]
        r_reord = self.client.post(
            f"/api/v1/timelines/{tl_id}/operations",
            json={"base_version": 2, "operations": [{"operation_id": str(uuid4()), "operation_type": "REORDER_CLIP", "payload": {"track_id": vtrack["id"], "ordered_clip_ids": [c2["id"], c1["id"]]}}]},
            headers=self._hdr(),
        )
        self.assertEqual(r_reord.status_code, 200)
        v3 = self.client.get(f"/api/v1/timelines/{tl_id}", headers=self._hdr()).json()
        clips_v3 = next(t for t in v3["tracks"] if t["type"] == "video")["clips"]
        self.assertEqual([c["id"] for c in clips_v3], [c2["id"], c1["id"]])
        self.assertEqual(clips_v3[0]["timeline_start"], 0)
        self.assertEqual(clips_v3[1]["timeline_start"], c2["duration"])

        # Attempting MERGE_CLIP on reversed non-contiguous source bounds MUST fail 422
        r_bad_merge = self.client.post(
            f"/api/v1/timelines/{tl_id}/operations",
            json={"base_version": 3, "operations": [{"operation_id": str(uuid4()), "operation_type": "MERGE_CLIP", "payload": {"first_clip_id": c2["id"], "second_clip_id": c1["id"]}}]},
            headers=self._hdr(),
        )
        self.assertEqual(r_bad_merge.status_code, 422)

        # 3) Reorder back to [c1, c2], then MERGE_CLIP restores single original clip
        self.client.post(
            f"/api/v1/timelines/{tl_id}/operations",
            json={"base_version": 3, "operations": [{"operation_id": str(uuid4()), "operation_type": "REORDER_CLIP", "payload": {"track_id": vtrack["id"], "ordered_clip_ids": [c1["id"], c2["id"]]}}]},
            headers=self._hdr(),
        )
        r_merge = self.client.post(
            f"/api/v1/timelines/{tl_id}/operations",
            json={"base_version": 4, "operations": [{"operation_id": str(uuid4()), "operation_type": "MERGE_CLIP", "payload": {"first_clip_id": c1["id"], "second_clip_id": c2["id"]}}]},
            headers=self._hdr(),
        )
        self.assertEqual(r_merge.status_code, 200)
        v5 = self.client.get(f"/api/v1/timelines/{tl_id}", headers=self._hdr()).json()
        clips_v5 = next(t for t in v5["tracks"] if t["type"] == "video")["clips"]
        self.assertEqual(len(clips_v5), 1)
        self.assertEqual(clips_v5[0]["duration"], orig_dur)

        # 4) SET_CANVAS_PROFILE to 360x640 (9:16 portrait) + real FFmpeg render + ffprobe proof
        r_prof = self.client.post(
            f"/api/v1/timelines/{tl_id}/operations",
            json={"base_version": 5, "operations": [{"operation_id": str(uuid4()), "operation_type": "SET_CANVAS_PROFILE", "payload": {"width": 360, "height": 640, "frame_rate": {"num": 30, "den": 1}}}]},
            headers=self._hdr(),
        )
        self.assertEqual(r_prof.status_code, 200)
        render_resp = self.client.post(
            f"/api/v1/timelines/{tl_id}/renders",
            json={"timeline_version": 6},
            headers=self._hdr(),
        )
        self.assertEqual(render_resp.status_code, 202)
        art_id = render_resp.json()["artifact_id"]
        art_rec = self.app.state.store._tables["artifacts"][art_id]
        import json as _json
        pr = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", art_rec["storage_key"]],
            check=True, capture_output=True, text=True,
        )
        stream = _json.loads(pr.stdout)["streams"][0]
        self.assertEqual(stream["width"], 360)
        self.assertEqual(stream["height"], 640)


if __name__ == "__main__":
    unittest.main()
