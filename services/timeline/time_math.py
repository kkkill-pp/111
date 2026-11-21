"""Canonical rational time-semantics engine shared by TimelineService, Web Editor, and FFmpeg RenderPlan.

Eliminates floating-point drift between editor preview and FFmpeg export (v3.0 Risk #4).
All timeline coordinates use exact rational ticks and deterministic round-half-to-even.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class Rational:
    num: int
    den: int

    def __post_init__(self) -> None:
        if self.num <= 0 or self.den <= 0:
            raise ValueError(f"Rational must have positive num and den, got {self.num}/{self.den}")

    def as_fraction(self) -> Fraction:
        return Fraction(self.num, self.den)

    def to_dict(self) -> dict[str, int]:
        return {"num": self.num, "den": self.den}

    @classmethod
    def from_dict(cls, data: dict[str, int]) -> "Rational":
        return cls(num=int(data["num"]), den=int(data["den"]))


def round_half_to_even(value: Fraction) -> int:
    """Deterministic banker's rounding on exact Fraction (Python built-in round on Fraction is exact half-to-even)."""
    return int(round(value))


def compute_clip_duration_ticks(
    source_in: int,
    source_out: int,
    source_timebase: Rational,
    timeline_timebase: Rational,
    playback_rate: float | Fraction = 1.0,
) -> int:
    """Compute exact project-timeline duration in ticks from half-open source [in, out) and playback_rate."""
    if source_in < 0 or source_out <= source_in:
        raise ValueError(f"Invalid source range [{source_in}, {source_out})")
    rate = playback_rate if isinstance(playback_rate, Fraction) else Fraction(str(playback_rate))
    if rate < Fraction(1, 4) or rate > Fraction(4, 1):
        raise ValueError(f"playback_rate out of bounds [0.25, 4.0]: {rate}")
    source_seconds = Fraction(source_out - source_in) * source_timebase.as_fraction()
    project_ticks_exact = (source_seconds / rate) / timeline_timebase.as_fraction()
    ticks = round_half_to_even(project_ticks_exact)
    if ticks < 1:
        raise ValueError("Computed duration_ticks must be >= 1")
    return ticks


def ticks_to_seconds_fraction(ticks: int, timebase: Rational) -> Fraction:
    if ticks < 0:
        raise ValueError(f"ticks must be >= 0, got {ticks}")
    return Fraction(ticks) * timebase.as_fraction()


def ticks_to_ffmpeg_timestamp(ticks: int, timebase: Rational) -> str:
    """Format exact tick count as 6-decimal-place seconds string for FFmpeg -ss / -t / setpts / ASS."""
    sec = float(ticks_to_seconds_fraction(ticks, timebase))
    return f"{sec:.6f}"


def ticks_to_frame_index(ticks: int, timeline_timebase: Rational, frame_rate: Rational) -> int:
    """Convert project ticks to exact frame index using half-to-even rounding."""
    sec = ticks_to_seconds_fraction(ticks, timeline_timebase)
    return round_half_to_even(sec * frame_rate.as_fraction())
