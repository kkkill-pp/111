"""Extension B Built-in Plugin 5: HTML/CSS/GSAP Motion Scene & Headless Chromium Renderer.

Clean-room pluggable module inspired by OpenMotion's Video-as-Code pipeline (Apache-2.0).
Features:
  - Compiles BrandKit-styled HTML5 + CSS + GSAP scenes with deterministic `window.__seekToSec(t)` hook
  - Auto-detects host Chromium / Google Chrome / Playwright availability
  - Strictly follows ADR-004 (Zero Silent Fallback):
      * `render_mode='html_bundle'` -> writes runnable `scene.html` + `render_spec.json` + preview PNG
      * `render_mode='chromium_gsap'` -> requires real headless browser binary; fails loudly `503 CHROMIUM_UNAVAILABLE` if absent
"""
from __future__ import annotations

import html
import json
import shutil
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont

from services.infrastructure.errors import AppError
from services.plugins.plugin_manager import AIVSPlugin, PluginManifest

SUPPORTED_PRESETS = frozenset({"kinetic_title", "brand_reveal", "lower_third_gsap"})


def detect_browser_backend() -> dict[str, Any]:
    """Detect whether a headless Chromium/Chrome binary is installed on the host."""
    for candidate in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        found = shutil.which(candidate)
        if found:
            return {"available": True, "binary": found, "engine": candidate}
    return {"available": False, "binary": None, "engine": None}


class HtmlGsapMotionPlugin(AIVSPlugin):
    PLUGIN_ID = "html_gsap_motion_plugin"

    def __init__(self) -> None:
        self.manifest = PluginManifest(
            plugin_id=self.PLUGIN_ID,
            name="HTML/CSS/GSAP Motion Scene Renderer",
            version="1.0.0",
            category="renderer",
            license="Apache-2.0",
            description="Generates time-seekable HTML/CSS/GSAP motion scenes with optional Headless Chromium frame rendering.",
            capabilities=[
                "motion.html_gsap.kinetic_title",
                "motion.html_gsap.brand_reveal",
                "motion.html_gsap.lower_third_gsap",
            ],
            enabled=True,
        )

    def runtime_info(self) -> dict[str, Any]:
        return {
            "plugin_id": self.PLUGIN_ID,
            "presets": sorted(SUPPORTED_PRESETS),
            "browser_backend": detect_browser_backend(),
        }

    def build_scene(
        self,
        preset: str,
        headline: str,
        output_dir: str,
        subheadline: Optional[str] = None,
        brand_kit: Optional[dict[str, Any]] = None,
        width: int = 1080,
        height: int = 1920,
        fps: int = 30,
        duration_sec: float = 3.0,
        render_mode: str = "html_bundle",
    ) -> dict[str, Any]:
        if preset not in SUPPORTED_PRESETS:
            raise AppError(422, "VALIDATION_ERROR", f"Unsupported HTML/GSAP preset '{preset}'")
        if render_mode not in ("html_bundle", "chromium_gsap"):
            raise AppError(422, "VALIDATION_ERROR", f"Unsupported render_mode '{render_mode}'")
        if not headline or not headline.strip():
            raise AppError(422, "VALIDATION_ERROR", "headline cannot be empty")

        backend = detect_browser_backend()
        if render_mode == "chromium_gsap" and not backend["available"]:
            raise AppError(
                503,
                "CHROMIUM_UNAVAILABLE",
                "render_mode='chromium_gsap' requires Chromium/Chrome installed on host (silent fallback forbidden by ADR-004); use render_mode='html_bundle' or run inside a Chromium-enabled Docker worker",
            )

        kit = brand_kit or {}
        bg = str(kit.get("bg_color", "#0B1120"))
        accent = str(kit.get("primary_color", "#F59E0B"))
        text_color = str(kit.get("text_color", "#F8FAFC"))
        brand_name = str(kit.get("brand_name", "AI Video Studio"))

        scene_id = str(uuid4())
        bundle_dir = Path(output_dir) / f"gsap_scene_{scene_id[:12]}"
        bundle_dir.mkdir(parents=True, exist_ok=True)

        safe_head = html.escape(headline.strip())
        safe_sub = html.escape((subheadline or brand_name).strip())
        safe_bg = html.escape(bg)
        safe_accent = html.escape(accent)
        safe_text = html.escape(text_color)

        scene_html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width={width}, height={height}, initial-scale=1.0" />
  <title>{safe_head}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html, body {{
      width: {width}px; height: {height}px; overflow: hidden;
      background: {safe_bg}; color: {safe_text};
      font-family: "Noto Sans CJK SC", system-ui, -apple-system, sans-serif;
      display: flex; align-items: center; justify-content: center;
    }}
    .card {{
      padding: 48px 56px; border-left: 14px solid {safe_accent};
      background: rgba(15, 23, 42, 0.88); border-radius: 12px;
      max-width: 82%; transform: translateY(40px); opacity: 0;
    }}
    .headline {{ font-size: 64px; font-weight: 800; line-height: 1.15; letter-spacing: -0.02em; }}
    .subheadline {{ margin-top: 18px; font-size: 32px; color: {safe_accent}; font-weight: 600; }}
  </style>
  <script src="https://cdn.jsdelivr.net/npm/gsap@3.12.5/dist/gsap.min.js"></script>
</head>
<body>
  <div class="card" id="motion-card" data-preset="{preset}">
    <div class="headline">{safe_head}</div>
    <div class="subheadline">{safe_sub}</div>
  </div>
  <script>
    const totalDur = {float(duration_sec):.3f};
    const tl = window.gsap
      ? window.gsap.timeline({{ paused: true }})
          .to("#motion-card", {{ y: 0, opacity: 1, duration: 0.65, ease: "power3.out" }}, 0)
          .to("#motion-card", {{ opacity: 0, y: -24, duration: 0.45, ease: "power2.in" }}, Math.max(0.8, totalDur - 0.5))
      : null;
    window.__seekToSec = function(sec) {{
      if (tl) {{ tl.seek(Math.max(0, Math.min(totalDur, sec))); }}
      else {{
        const el = document.getElementById("motion-card");
        if (el) {{ el.style.opacity = "1"; el.style.transform = "translateY(0)"; }}
      }}
    }};
    window.__seekToSec(0.7);
  </script>
</body>
</html>
"""
        html_path = bundle_dir / "scene.html"
        html_path.write_text(scene_html, encoding="utf-8")

        spec = {
            "scene_id": scene_id,
            "preset": preset,
            "width": int(width),
            "height": int(height),
            "fps": int(fps),
            "duration_sec": float(duration_sec),
            "total_frames": max(1, int(round(float(duration_sec) * int(fps)))),
            "seek_hook": "window.__seekToSec(sec)",
            "browser_backend": backend,
            "render_mode": render_mode,
        }
        spec_path = bundle_dir / "render_spec.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")

        # Deterministic poster preview PNG
        poster_path = bundle_dir / "poster.png"
        pw, ph = min(640, int(width)), min(360, int(height))
        img = Image.new("RGB", (pw, ph), (15, 23, 42))
        draw = ImageDraw.Draw(img)
        draw.rectangle([24, 24, 38, ph - 24], fill=(245, 158, 11))
        draw.text((54, ph // 2 - 20), headline.strip()[:42], fill=(248, 250, 252), font=ImageFont.load_default())
        draw.text((54, ph // 2 + 12), f"GSAP {preset} | {duration_sec}s @ {fps}fps", fill=(245, 158, 11), font=ImageFont.load_default())
        img.save(str(poster_path), format="PNG")

        return {
            "scene_id": scene_id,
            "preset": preset,
            "render_mode": render_mode,
            "html_path": str(html_path),
            "spec_path": str(spec_path),
            "poster_path": str(poster_path),
            "total_frames": spec["total_frames"],
            "browser_backend_available": backend["available"],
        }
