"""Built-in Plugin 3: Declarative Motion Overlay Renderer (text_hook / lower_third).

Generates RGBA transparent PNG overlay graphics styled by BrandKit (background
color, accent bar, text) ready for zero-fallback compositing via ADD_OVERLAY
(kind='image') in FFmpegRendererAdapter. The plugin contract is backend-agnostic
so a Chromium/GSAP HTML renderer can be swapped in later behind the same API.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFont

from services.infrastructure.errors import AppError
from services.plugins.plugin_manager import AIVSPlugin, PluginManifest


def _parse_hex_rgba(hex_color: str, alpha: int = 230) -> tuple[int, int, int, int]:
    h = (hex_color or "#2563EB").lstrip("#")
    if len(h) == 6:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)
    return (37, 99, 235, alpha)


class MotionOverlayRendererPlugin(AIVSPlugin):
    PLUGIN_ID = "motion_overlay_renderer_plugin"

    def __init__(self) -> None:
        self.manifest = PluginManifest(
            plugin_id=self.PLUGIN_ID,
            name="Declarative Motion Overlay Renderer",
            version="1.0.0",
            category="renderer",
            license="Apache-2.0",
            description="Renders BrandKit-styled text_hook and lower_third RGBA overlay PNGs for deterministic FFmpeg compositing.",
            capabilities=["overlay.render.text_hook", "overlay.render.lower_third"],
            enabled=True,
        )

    def render_banner_png(
        self,
        kind: str,
        title: str,
        output_path: str,
        subtitle: Optional[str] = None,
        brand_kit: Optional[dict[str, Any]] = None,
        width: int = 480,
        height: int = 110,
    ) -> dict[str, Any]:
        if kind not in ("text_hook", "lower_third"):
            raise AppError(422, "VALIDATION_ERROR", f"Unsupported banner overlay kind '{kind}'")
        if not title or not title.strip():
            raise AppError(422, "VALIDATION_ERROR", "Overlay title cannot be empty")

        kit = brand_kit or {}
        bg_rgba = _parse_hex_rgba(kit.get("bg_color", "#0F172A"), alpha=225)
        accent_rgba = _parse_hex_rgba(kit.get("primary_color", "#F59E0B"), alpha=255)
        text_rgba = _parse_hex_rgba(kit.get("text_color", "#FFFFFF"), alpha=255)

        img = Image.new("RGBA", (int(width), int(height)), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Draw rounded-style card + left accent bar
        draw.rectangle([0, 0, width - 1, height - 1], fill=bg_rgba)
        draw.rectangle([0, 0, 14, height - 1], fill=accent_rgba)

        font = ImageFont.load_default()
        draw.text((28, 22), title.strip(), fill=text_rgba, font=font)
        if subtitle:
            draw.text((28, 60), subtitle.strip(), fill=accent_rgba, font=font)

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(out), format="PNG")
        return {
            "kind": kind,
            "output_path": str(out),
            "width": int(width),
            "height": int(height),
            "size_bytes": out.stat().st_size,
        }
