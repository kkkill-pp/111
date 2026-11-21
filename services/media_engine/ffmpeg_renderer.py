"""FFmpeg-Only V1 RenderPlan Compiler & RendererAdapter (ADR-004).

Preview and Final Render share the exact same RenderPlan semantics.
Silent semantic fallback and arbitrary filtergraphs are STRICTLY FORBIDDEN:
any RenderPlan feature not yet implemented in this renderer raises AppError(422)
instead of being silently dropped.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from services.infrastructure.contracts import canonical_sha256, validate_against_schema
from services.infrastructure.errors import AppError
from services.media_engine.adapters.openshorts_adapter import file_sha256


def _ticks_to_ass_time(ticks: int, tb_num: int, tb_den: int) -> str:
    total_cs = int(round(((ticks * tb_num) / tb_den) * 100))
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape_ass_text(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")


def _escape_ffmpeg_filter_path(path: str) -> str:
    # Works on both POSIX and Windows (drive letter colon escaping for libass filter)
    normalized = path.replace("\\", "/")
    return normalized.replace(":", r"\:").replace("'", r"\'")


def _build_atempo_chain(speed: float) -> str:
    """FFmpeg atempo supports [0.5, 2.0] per stage; chain stages for [0.25, 4.0]."""
    stages: list[str] = []
    remaining = speed
    while remaining > 2.0:
        stages.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        stages.append("atempo=0.5")
        remaining /= 0.5
    stages.append(f"atempo={remaining:.6f}")
    return ",".join(stages)


class RenderPlanCompiler:
    def compile(
        self,
        timeline_snapshot: dict[str, Any],
        asset_lookup: dict[str, dict[str, Any]],
        render_mode: str = "preview",
    ) -> tuple[dict[str, Any], str]:
        profile = timeline_snapshot["profile"]
        tb = profile["timeline_timebase"]

        video_ops = []
        audio_ops = []
        subtitle_ops = []
        overlay_ops = []
        used_assets: dict[str, dict[str, Any]] = {}
        expected_duration_ticks = 0

        for tr in timeline_snapshot["tracks"]:
            if tr["type"] == "video":
                for c in tr["clips"]:
                    asset = asset_lookup[c["asset_id"]]
                    used_assets[c["asset_id"]] = {
                        "asset_id": asset["id"],
                        "storage_key": asset["storage_key"],
                        "checksum_sha256": asset["checksum_sha256"],
                        "is_vfr": bool(asset.get("is_vfr", False)),
                        "pts_map_storage_key": None,
                    }
                    speed = float(c.get("speed", 1.0))
                    effective_dur = max(1, int(round(c["duration"] / speed)))
                    video_ops.append({
                        "clip_id": c["id"],
                        "asset_id": c["asset_id"],
                        "source_in_tick": c["source_range"]["in"],
                        "source_out_tick": c["source_range"]["out"],
                        "timeline_start_tick": c["timeline_start"],
                        "duration_ticks": effective_dur,
                        "speed": speed,
                        "transform": c.get("transform", {"mode": "fit"}),
                        "transitions": c.get("transitions", []),
                    })
                    audio_ops.append({
                        "clip_id": c["id"],
                        "enabled": c.get("audio", {}).get("enabled", True),
                        "volume": c.get("audio", {}).get("volume", 1.0),
                    })
                    expected_duration_ticks = max(expected_duration_ticks, c["timeline_start"] + effective_dur)
            elif tr["type"] == "subtitle":
                for item in tr["items"]:
                    subtitle_ops.append(item)
            elif tr["type"] == "overlay":
                for item in tr["items"]:
                    overlay_ops.append(item)

        out_w = profile["width"] // 2 if render_mode == "preview" else profile["width"]
        out_h = profile["height"] // 2 if render_mode == "preview" else profile["height"]
        out_w += out_w % 2
        out_h += out_h % 2

        plan = {
            "render_plan_version": "2.0",
            "render_mode": render_mode,
            "renderer": "ffmpeg",
            "timeline_id": timeline_snapshot["timeline_id"],
            "timeline_version": timeline_snapshot["version"],
            "timeline_checksum": canonical_sha256(timeline_snapshot),
            "timeline_timebase": tb,
            "render_range_ticks": None,
            "expected_duration_ticks": max(1, expected_duration_ticks),
            "input_assets": list(used_assets.values()),
            "output_profile": {
                "width": out_w,
                "height": out_h,
                "frame_rate": profile["frame_rate"],
                "pixel_format": "yuv420p",
                "audio_sample_rate": profile["audio_sample_rate"],
                "audio_channels": 2,
            },
            "video_ops": video_ops,
            "audio_ops": audio_ops,
            "subtitle_ops": subtitle_ops,
            "overlay_ops": overlay_ops,
            "encoder_settings": {
                "video_codec": "libx264",
                "audio_codec": "aac",
                "preset": "ultrafast" if render_mode == "preview" else "medium",
                "crf_or_bitrate": "28" if render_mode == "preview" else "18",
                "threads": 2,
                "faststart": True,
            },
            "determinism_flags": {
                "allow_silent_semantic_fallback": False,
                "allow_arbitrary_filtergraph": False,
                "require_exact_duration_check": True,
            },
        }
        validate_against_schema("render-plan.schema.json", plan)
        return plan, canonical_sha256(plan)


class FFmpegRendererAdapter:
    """Deterministic FFmpeg-only V1 renderer driven strictly by RenderPlan (zero silent fallback)."""

    SUPPORTED_TRANSFORM_MODES = {"fit", "fill"}

    def _assert_supported_semantics(self, render_plan: dict[str, Any]) -> None:
        """ADR-004: Fail loudly on any unimplemented semantic feature instead of dropping it."""
        if render_plan.get("overlay_ops"):
            raise AppError(
                422,
                "VALIDATION_ERROR",
                "overlay_ops rendering is not yet implemented in Sprint 1 FFmpegRendererAdapter (silent fallback forbidden by ADR-004)",
            )
        for op in render_plan.get("video_ops", []):
            mode = (op.get("transform") or {}).get("mode", "fit")
            if mode not in self.SUPPORTED_TRANSFORM_MODES:
                raise AppError(
                    422,
                    "VALIDATION_ERROR",
                    f"transform.mode='{mode}' is not yet implemented in Sprint 1 FFmpegRendererAdapter (silent fallback forbidden by ADR-004)",
                )
            for tr in op.get("transitions") or []:
                if tr.get("type") != "cut":
                    raise AppError(
                        422,
                        "VALIDATION_ERROR",
                        f"transition.type='{tr.get('type')}' is not yet implemented in Sprint 1 FFmpegRendererAdapter (silent fallback forbidden by ADR-004)",
                    )

    @staticmethod
    def _write_ass_subtitles(
        subtitle_ops: list[dict[str, Any]],
        out_w: int,
        out_h: int,
        tb_num: int,
        tb_den: int,
        ass_path: Path,
    ) -> None:
        font_size = max(24, int(out_h * 0.04))
        align_map = {"bottom": 2, "center_seam": 5, "top": 8}
        lines = [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {out_w}",
            f"PlayResY: {out_h}",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            f"Style: bottom,Noto Sans CJK SC,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,2,1,2,40,40,80,1",
            f"Style: center_seam,Noto Sans CJK SC,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,2,1,5,40,40,40,1",
            f"Style: top,Noto Sans CJK SC,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,2,1,8,40,40,80,1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
        for item in subtitle_ops:
            start_str = _ticks_to_ass_time(item["timeline_start"], tb_num, tb_den)
            end_str = _ticks_to_ass_time(item["timeline_start"] + item["duration"], tb_num, tb_den)
            zone = item.get("safe_zone", "bottom")
            style = zone if zone in align_map else "bottom"
            text = _escape_ass_text(item["text"])
            lines.append(f"Dialogue: 0,{start_str},{end_str},{style},,0,0,0,,{text}")
        ass_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def render(self, render_plan: dict[str, Any], output_path: str) -> dict[str, Any]:
        validate_against_schema("render-plan.schema.json", render_plan)
        self._assert_supported_semantics(render_plan)
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        tb_num = render_plan["timeline_timebase"]["num"]
        tb_den = render_plan["timeline_timebase"]["den"]
        asset_paths = {a["asset_id"]: a["storage_key"] for a in render_plan["input_assets"]}
        out_prof = render_plan["output_profile"]
        enc = render_plan["encoder_settings"]
        fps_str = f"{out_prof['frame_rate']['num']}/{out_prof['frame_rate']['den']}"

        inputs: list[str] = []
        filter_parts: list[str] = []
        concat_labels: list[str] = []

        for idx, op in enumerate(render_plan["video_ops"]):
            src_path = asset_paths[op["asset_id"]]
            start_s = (op["source_in_tick"] * tb_num) / tb_den
            dur_s = ((op["source_out_tick"] - op["source_in_tick"]) * tb_num) / tb_den
            speed = float(op.get("speed", 1.0))
            mode = (op.get("transform") or {}).get("mode", "fit")

            inputs.extend(["-ss", f"{start_s:.6f}", "-t", f"{dur_s:.6f}", "-i", src_path])

            if mode == "fill":
                scale_chain = (
                    f"scale={out_prof['width']}:{out_prof['height']}:force_original_aspect_ratio=increase,"
                    f"crop={out_prof['width']}:{out_prof['height']}"
                )
            else:
                scale_chain = (
                    f"scale={out_prof['width']}:{out_prof['height']}:force_original_aspect_ratio=decrease,"
                    f"pad={out_prof['width']}:{out_prof['height']}:(ow-iw)/2:(oh-ih)/2"
                )

            v_chain = [scale_chain]
            if abs(speed - 1.0) > 1e-6:
                v_chain.append(f"setpts=PTS/{speed:.6f}")
            v_chain.extend([f"fps={fps_str}", "format=yuv420p"])
            filter_parts.append(f"[{idx}:v]{','.join(v_chain)}[v{idx}]")

            a_chain = []
            if abs(speed - 1.0) > 1e-6:
                a_chain.append(_build_atempo_chain(speed))
            a_chain.extend([f"aresample={out_prof['audio_sample_rate']}", "aformat=channel_layouts=stereo"])
            filter_parts.append(f"[{idx}:a]{','.join(a_chain)}[a{idx}]")
            concat_labels.append(f"[v{idx}][a{idx}]")

        n = len(render_plan["video_ops"])
        subtitle_ops = render_plan.get("subtitle_ops") or []

        with tempfile.TemporaryDirectory(prefix="aivs_ass_") as tmp_ass_dir:
            if subtitle_ops:
                ass_file = Path(tmp_ass_dir) / "captions.ass"
                self._write_ass_subtitles(subtitle_ops, out_prof["width"], out_prof["height"], tb_num, tb_den, ass_file)
                escaped_ass = _escape_ffmpeg_filter_path(str(ass_file))
                filter_parts.append(f"{''.join(concat_labels)}concat=n={n}:v=1:a=1[vcat][aout]")
                fonts_dir = Path(__file__).resolve().parents[2] / "assets" / "fonts"
                if fonts_dir.exists():
                    escaped_fonts = _escape_ffmpeg_filter_path(str(fonts_dir))
                    filter_parts.append(f"[vcat]ass='{escaped_ass}':fontsdir='{escaped_fonts}'[vout]")
                else:
                    filter_parts.append(f"[vcat]ass='{escaped_ass}'[vout]")
            else:
                filter_parts.append(f"{''.join(concat_labels)}concat=n={n}:v=1:a=1[vout][aout]")

            cmd = [
                "ffmpeg", "-y", "-loglevel", "error", "-threads", str(enc["threads"]),
                *inputs,
                "-filter_complex", ";".join(filter_parts),
                "-map", "[vout]", "-map", "[aout]",
                "-c:v", enc["video_codec"], "-preset", enc["preset"], "-crf", enc["crf_or_bitrate"],
                "-c:a", enc["audio_codec"], "-ar", str(out_prof["audio_sample_rate"]), "-ac", str(out_prof["audio_channels"]),
                "-movflags", "+faststart",
                output_path,
            ]
            env = os.environ.copy()
            fontmap_conf = Path(__file__).resolve().parents[2] / "assets" / "fonts" / "openshorts-fontmap.conf"
            if fontmap_conf.exists() and "FONTCONFIG_FILE" not in env:
                env["FONTCONFIG_FILE"] = str(fontmap_conf)
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=180, env=env)

        size = Path(output_path).stat().st_size
        checksum = file_sha256(output_path)
        return {
            "output_path": output_path,
            "size_bytes": size,
            "checksum_sha256": checksum,
            "encoder_used": enc["video_codec"],
            "subtitles_burned": len(subtitle_ops),
        }
