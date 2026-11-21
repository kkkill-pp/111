"""Technical & Content QA Engine backed by real ffprobe verification.

Honesty rule (P1-B): Unimplemented AI semantic content checks MUST report
`passed: False` with `code: CONTENT_SEMANTIC_QA_NOT_IMPLEMENTED` — never
impersonate a passing check with hardcoded `True`.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from services.infrastructure.contracts import validate_against_schema
from services.infrastructure.store import iso_now
from services.media_engine.adapters.openshorts_adapter import OpenShortsAdapter, file_sha256
from services.media_engine.visual_frame_inspector import VisualFrameInspector


class QAEngine:
    def __init__(self, adapter: OpenShortsAdapter) -> None:
        self.adapter = adapter

    def run_qa(
        self,
        artifact_id: str,
        artifact_path: str,
        expected_checksum: str,
        render_plan: dict[str, Any],
    ) -> dict[str, Any]:
        technical_checks: list[dict[str, Any]] = []
        content_checks: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        # 1. File integrity checksum check
        actual_checksum = file_sha256(artifact_path)
        checksum_ok = actual_checksum == expected_checksum
        chk_item = {
            "code": "TECH_CHECKSUM_INTEGRITY",
            "severity": "blocker",
            "passed": checksum_ok,
            "message": "Artifact SHA-256 matches recorded checksum" if checksum_ok else "Artifact SHA-256 mismatch",
        }
        technical_checks.append(chk_item)
        if not checksum_ok:
            blockers.append(chk_item)

        # 2. Real ffprobe decodability, resolution, audio presence, and duration check
        try:
            probe = self.adapter.probe_asset(artifact_path)
            out_prof = render_plan["output_profile"]
            res_ok = (probe.width == out_prof["width"] and probe.height == out_prof["height"])
            res_item = {
                "code": "TECH_RESOLUTION_MATCH",
                "severity": "blocker",
                "passed": res_ok,
                "message": f"Output {probe.width}x{probe.height} matches plan {out_prof['width']}x{out_prof['height']}",
                "metrics": {"width": probe.width, "height": probe.height},
            }
            technical_checks.append(res_item)
            if not res_ok:
                blockers.append(res_item)

            audio_ok = probe.audio_channels is not None and probe.audio_channels >= 1
            audio_item = {
                "code": "TECH_AUDIO_STREAM_PRESENT",
                "severity": "blocker",
                "passed": audio_ok,
                "message": "Audio stream present" if audio_ok else "Missing audio stream",
            }
            technical_checks.append(audio_item)
            if not audio_ok:
                blockers.append(audio_item)

            tb = render_plan["timeline_timebase"]
            expected_sec = (render_plan["expected_duration_ticks"] * tb["num"]) / tb["den"]
            actual_sec = probe.duration_num / probe.duration_den
            drift_ms = abs(actual_sec - expected_sec) * 1000.0
            dur_ok = drift_ms <= 120.0
            dur_item = {
                "code": "TECH_DURATION_TOLERANCE",
                "severity": "blocker",
                "passed": dur_ok,
                "message": f"Actual {actual_sec:.3f}s vs expected {expected_sec:.3f}s (drift {drift_ms:.1f}ms)",
                "metrics": {"actual_sec": round(actual_sec, 4), "expected_sec": round(expected_sec, 4), "drift_ms": round(drift_ms, 2)},
            }
            technical_checks.append(dur_item)
            if not dur_ok:
                blockers.append(dur_item)
        except Exception as exc:
            dec_item = {
                "code": "TECH_DECODE_FAILED",
                "severity": "blocker",
                "passed": False,
                "message": f"ffprobe failed to decode artifact: {exc}",
            }
            technical_checks.append(dec_item)
            blockers.append(dec_item)

        # 2b. Real OpenCV Visual Frame Black-Screen & Subtitle-Band Pixel Check
        try:
            vis = VisualFrameInspector.inspect_video_frames(artifact_path)
            all_black = vis.frames_sampled > 0 and vis.black_frames == vis.frames_sampled
            vis_item = {
                "code": "TECH_VISUAL_BLACK_FRAME_CHECK",
                "severity": "blocker",
                "passed": not all_black,
                "message": (
                    f"Sampled {vis.frames_sampled} frames (avg_luma={vis.avg_luma:.1f}, black={vis.black_frames})"
                    if not all_black
                    else f"All {vis.frames_sampled} sampled frames are black (avg_luma={vis.avg_luma:.1f})"
                ),
                "metrics": vis.to_dict(),
            }
            technical_checks.append(vis_item)
            if all_black:
                blockers.append(vis_item)
        except Exception as exc:
            technical_checks.append({
                "code": "TECH_VISUAL_INSPECT_WARNING",
                "severity": "warning",
                "passed": False,
                "message": f"Visual frame inspection error: {exc}",
            })

        # 3. Deterministic subtitle metadata check + explicit NOT_IMPLEMENTED marker for AI semantic QA
        subs = render_plan.get("subtitle_ops") or []
        if subs:
            valid_zones = {"bottom", "center_seam", "top"}
            bad_subs = [s for s in subs if s.get("safe_zone", "bottom") not in valid_zones or len(s.get("text", "")) > 120]
            sub_item = {
                "code": "CONTENT_SUBTITLE_LAYOUT_BOUNDS",
                "severity": "warning",
                "passed": len(bad_subs) == 0,
                "message": f"Checked {len(subs)} subtitle items; {len(bad_subs)} exceed layout bounds",
            }
            content_checks.append(sub_item)
            if bad_subs:
                warnings.append(sub_item)

        content_checks.append({
            "code": "CONTENT_SEMANTIC_QA_NOT_IMPLEMENTED",
            "severity": "info",
            "passed": False,
            "message": "AI semantic content QA (ASR alignment, framing crop, brand safety) is not yet implemented in Sprint 1; manual reviewer inspection required",
        })

        status = "failed" if blockers else ("warning" if warnings else "passed")
        report = {
            "qa_report_id": str(uuid4()),
            "artifact_id": artifact_id,
            "artifact_checksum": actual_checksum,
            "status": status,
            "technical_checks": technical_checks,
            "content_checks": content_checks,
            "blockers": blockers,
            "warnings": warnings,
            "completed_at": iso_now(),
        }
        validate_against_schema("qa-report.schema.json", report)
        return report
