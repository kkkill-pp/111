"""Built-in Apache-2.0 / MIT Pluggable Modules for AI Video Studio."""
from services.plugins.builtin.anchored_review_plugin import AnchoredReviewPlugin
from services.plugins.builtin.html_gsap_motion_plugin import HtmlGsapMotionPlugin
from services.plugins.builtin.media_gateway_plugin import MediaGatewayPlugin
from services.plugins.builtin.motion_overlay_renderer_plugin import MotionOverlayRendererPlugin
from services.plugins.builtin.motion_template_plugin import MotionTemplatePlugin

__all__ = [
    "AnchoredReviewPlugin",
    "HtmlGsapMotionPlugin",
    "MediaGatewayPlugin",
    "MotionOverlayRendererPlugin",
    "MotionTemplatePlugin",
]
