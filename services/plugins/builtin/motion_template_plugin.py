"""Built-in Plugin 1: Motion.md + BrandKit Declarative Template Compiler.

Clean-room implementation inspired by OpenMotion's Motion.md workflow (Apache-2.0).
Parses Markdown shot scripts + BrandKit variables and compiles them into canonical
v3.0 EditOperations[] for HITL review and TimelineService execution.
"""
from __future__ import annotations

import re
from typing import Any, Optional
from uuid import uuid4

from services.infrastructure.errors import AppError
from services.plugins.plugin_manager import AIVSPlugin, PluginManifest

_SHOT_HEADER_RE = re.compile(r"^###\s+Shot\s+(\d+)\s*:\s*(.+)$", re.IGNORECASE)
_KV_RE = re.compile(r"^-\s*([a-zA-Z0-9_]+)\s*:\s*(.+)$")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def _substitute_brand_vars(text: str, brand_kit: dict[str, Any]) -> str:
    def _repl(match: re.Match) -> str:
        key = match.group(1)
        return str(brand_kit.get(key, match.group(0)))

    return _PLACEHOLDER_RE.sub(_repl, text)


class MotionTemplatePlugin(AIVSPlugin):
    PLUGIN_ID = "motion_template_plugin"

    def __init__(self) -> None:
        self.manifest = PluginManifest(
            plugin_id=self.PLUGIN_ID,
            name="Motion.md Template & BrandKit Compiler",
            version="1.0.0",
            category="template",
            license="Apache-2.0",
            description="Parses declarative Motion.md shot guides with BrandKit substitution into canonical v3.0 EditOperations.",
            capabilities=["motion_md.parse", "motion_md.compile_operations", "brand_kit.substitute"],
            enabled=True,
        )

    def parse_motion_md(self, markdown_text: str, brand_kit: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if not markdown_text or not markdown_text.strip():
            raise AppError(422, "VALIDATION_ERROR", "motion_md content cannot be empty")

        kit = dict(brand_kit or {})
        title = "Untitled Motion Template"
        section = ""
        parsed_brand: dict[str, Any] = {}
        shots: list[dict[str, Any]] = []
        current_shot: Optional[dict[str, Any]] = None

        for raw_line in markdown_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("# ") and not line.startswith("## "):
                title = line[2:].strip()
                continue
            if line.startswith("## "):
                section = line[3:].strip().lower()
                current_shot = None
                continue

            m_shot = _SHOT_HEADER_RE.match(line)
            if m_shot:
                current_shot = {
                    "index": int(m_shot.group(1)),
                    "label": m_shot.group(2).strip(),
                    "start_sec": 0.0,
                    "duration_sec": 2.0,
                }
                shots.append(current_shot)
                continue

            m_kv = _KV_RE.match(line)
            if m_kv:
                k, v = m_kv.group(1).strip().lower(), m_kv.group(2).strip()
                if "brand" in section and current_shot is None:
                    parsed_brand[k] = v
                elif current_shot is not None:
                    if k in ("start_sec", "duration_sec", "speed"):
                        try:
                            current_shot[k] = float(v)
                        except ValueError:
                            raise AppError(422, "VALIDATION_ERROR", f"Invalid numeric value '{v}' for '{k}'")
                    else:
                        current_shot[k] = v

        merged_kit = {**parsed_brand, **kit}
        title = _substitute_brand_vars(title, merged_kit)
        for shot in shots:
            shot["label"] = _substitute_brand_vars(shot["label"], merged_kit)
            if "subtitle" in shot:
                shot["subtitle"] = _substitute_brand_vars(shot["subtitle"], merged_kit)
            if "text_hook" in shot:
                shot["text_hook"] = _substitute_brand_vars(shot["text_hook"], merged_kit)

        if not shots:
            raise AppError(422, "VALIDATION_ERROR", "Motion.md must define at least one '### Shot N: ...' block")

        return {"title": title, "brand_kit": merged_kit, "shots": shots}

    def compile_to_operations(
        self,
        markdown_text: str,
        brand_kit: Optional[dict[str, Any]] = None,
        subtitle_track_id: Optional[str] = None,
        target_clip_id: Optional[str] = None,
        ticks_per_second: int = 30000,
    ) -> dict[str, Any]:
        parsed = self.parse_motion_md(markdown_text, brand_kit=brand_kit)
        ops: list[dict[str, Any]] = []

        for shot in parsed["shots"]:
            start_tick = max(0, int(round(shot["start_sec"] * ticks_per_second)))
            dur_tick = max(1, int(round(shot["duration_sec"] * ticks_per_second)))

            if shot.get("subtitle") and subtitle_track_id:
                ops.append(
                    {
                        "operation_id": str(uuid4()),
                        "operation_type": "ADD_SUBTITLE",
                        "payload": {
                            "track_id": subtitle_track_id,
                            "timeline_start": start_tick,
                            "duration": dur_tick,
                            "text": shot["subtitle"],
                        },
                    }
                )

            if shot.get("speed") and target_clip_id and shot["speed"] != 1.0:
                ops.append(
                    {
                        "operation_id": str(uuid4()),
                        "operation_type": "UPDATE_SPEED",
                        "payload": {
                            "clip_id": target_clip_id,
                            "speed": float(shot["speed"]),
                        },
                    }
                )

            if shot.get("transition") in ("fade_in", "fade_out") and target_clip_id:
                ops.append(
                    {
                        "operation_id": str(uuid4()),
                        "operation_type": "ADD_TRANSITION",
                        "payload": {
                            "clip_id": target_clip_id,
                            "type": shot["transition"],
                            "duration_ticks": min(dur_tick, int(round(0.5 * ticks_per_second))),
                            "position": "in" if shot["transition"] == "fade_in" else "out",
                        },
                    }
                )

        return {
            "template": parsed,
            "operations": ops,
            "operation_count": len(ops),
        }
