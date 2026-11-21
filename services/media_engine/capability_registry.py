"""Extension & Evolution Engine — CapabilityRegistry (AI Video Studio v3.0 Engine #7).

Implements Cutaway's "plugins use the same road as core" principle under ADR-004's
`allow_arbitrary_filtergraph = False` security boundary: every registered effect
compiles strictly clamped, parameterized FFmpeg filter expressions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from services.infrastructure.errors import AppError


@dataclass(frozen=True)
class EffectCapability:
    effect_type: str
    category: str  # "video" | "audio"
    description: str
    default_params: dict[str, Any]
    compile_ffmpeg: Callable[[dict[str, Any]], str]

    def metadata(self) -> dict[str, Any]:
        return {
            "effect_type": self.effect_type,
            "category": self.category,
            "description": self.description,
            "default_params": self.default_params,
        }


def _clamp(val: Any, lo: float, hi: float, default: float) -> float:
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, f))


def _compile_brightness_contrast(params: dict[str, Any]) -> str:
    b = _clamp(params.get("brightness", 0.0), -1.0, 1.0, 0.0)
    c = _clamp(params.get("contrast", 1.0), 0.0, 3.0, 1.0)
    return f"eq=brightness={b:.4f}:contrast={c:.4f}"


def _compile_saturation(params: dict[str, Any]) -> str:
    s = _clamp(params.get("saturation", 1.0), 0.0, 3.0, 1.0)
    return f"eq=saturation={s:.4f}"


def _compile_gaussian_blur(params: dict[str, Any]) -> str:
    sigma = _clamp(params.get("sigma", 2.0), 0.1, 20.0, 2.0)
    return f"gblur=sigma={sigma:.4f}"


class CapabilityRegistry:
    def __init__(self) -> None:
        self._effects: dict[str, EffectCapability] = {}
        self._register_builtins()

    def _register_builtins(self) -> None:
        self.register_effect(EffectCapability(
            effect_type="brightness_contrast",
            category="video",
            description="Deterministic brightness (-1..1) and contrast (0..3) adjustment",
            default_params={"brightness": 0.0, "contrast": 1.0},
            compile_ffmpeg=_compile_brightness_contrast,
        ))
        self.register_effect(EffectCapability(
            effect_type="saturation",
            category="video",
            description="Deterministic color saturation scaling (0..3)",
            default_params={"saturation": 1.0},
            compile_ffmpeg=_compile_saturation,
        ))
        self.register_effect(EffectCapability(
            effect_type="gaussian_blur",
            category="video",
            description="Deterministic Gaussian blur (sigma 0.1..20.0)",
            default_params={"sigma": 2.0},
            compile_ffmpeg=_compile_gaussian_blur,
        ))

    def register_effect(self, cap: EffectCapability) -> None:
        self._effects[cap.effect_type] = cap

    def has_effect(self, effect_type: str) -> bool:
        return effect_type in self._effects

    def compile_video_effect(self, effect_type: str, params: dict[str, Any]) -> str:
        cap = self._effects.get(effect_type)
        if cap is None or cap.category != "video":
            raise AppError(
                422,
                "VALIDATION_ERROR",
                f"effect_type='{effect_type}' is not registered in CapabilityRegistry (silent fallback forbidden by ADR-004)",
            )
        return cap.compile_ffmpeg(params or {})

    def list_effects(self) -> list[dict[str, Any]]:
        return [cap.metadata() for cap in self._effects.values()]


DEFAULT_CAPABILITY_REGISTRY = CapabilityRegistry()
