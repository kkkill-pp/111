"""Reference Third-Party Drop-In Plugin for AI Video Studio v3.0 (Apache-2.0)."""
from services.plugins.plugin_manager import AIVSPlugin, PluginManifest


class SampleBrandWatermarkPlugin(AIVSPlugin):
    def __init__(self) -> None:
        self.manifest = PluginManifest(
            plugin_id="sample_brand_watermark_plugin",
            name="Sample Brand Watermark Preset",
            version="1.0.0",
            category="template",
            license="Apache-2.0",
            description="Reference third-party drop-in plugin providing brand-safe corner watermark placement presets.",
            capabilities=["watermark.preset.top_right", "watermark.preset.bottom_right"],
            enabled=True,
        )

    def get_corner_preset(self, corner: str = "top_right") -> dict[str, float]:
        presets = {
            "top_left": {"position_x": 0.08, "position_y": 0.08, "opacity": 0.85, "scale": 0.25},
            "top_right": {"position_x": 0.88, "position_y": 0.08, "opacity": 0.85, "scale": 0.25},
            "bottom_right": {"position_x": 0.88, "position_y": 0.88, "opacity": 0.85, "scale": 0.25},
        }
        return presets.get(corner, presets["top_right"])


def create_plugin() -> AIVSPlugin:
    return SampleBrandWatermarkPlugin()
