/**
 * Canonical Rational Tick Time Engine for Web Editor (zero DOM, zero React).
 * Mirrors services/timeline/time_math.py so Web preview and FFmpeg RenderPlan v3.0
 * share identical tick <-> second <-> frame <-> pixel math.
 */
import type { Rational } from "../../../../packages/shared-types/src/v3-contracts";

export const DEFAULT_TIMEBASE: Rational = { num: 1, den: 30000 };
export const DEFAULT_FPS: Rational = { num: 30, den: 1 };

export function ticksToSeconds(ticks: number, tb: Rational = DEFAULT_TIMEBASE): number {
  return (ticks * tb.num) / tb.den;
}

export function secondsToTicks(seconds: number, tb: Rational = DEFAULT_TIMEBASE): number {
  return Math.max(0, Math.round((seconds * tb.den) / tb.num));
}

export function ticksToPixels(ticks: number, zoomPxPerSec: number, tb: Rational = DEFAULT_TIMEBASE): number {
  return ticksToSeconds(ticks, tb) * zoomPxPerSec;
}

export function pixelsToTicks(px: number, zoomPxPerSec: number, tb: Rational = DEFAULT_TIMEBASE): number {
  if (zoomPxPerSec <= 0) return 0;
  return secondsToTicks(px / zoomPxPerSec, tb);
}

export function frameStepTicks(fps: Rational = DEFAULT_FPS, tb: Rational = DEFAULT_TIMEBASE): number {
  const secPerFrame = fps.den / fps.num;
  return Math.max(1, secondsToTicks(secPerFrame, tb));
}

export function formatTimecode(
  ticks: number,
  tb: Rational = DEFAULT_TIMEBASE,
  fps: Rational = DEFAULT_FPS
): string {
  const safeTicks = Math.max(0, Math.round(ticks));
  const totalSec = ticksToSeconds(safeTicks, tb);
  const fpsVal = fps.num / fps.den;
  const hrs = Math.floor(totalSec / 3600);
  const mins = Math.floor((totalSec % 3600) / 60);
  const secs = Math.floor(totalSec % 60);
  const frames = Math.floor((totalSec - Math.floor(totalSec)) * fpsVal + 1e-6);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(hrs)}:${pad(mins)}:${pad(secs)}:${pad(frames)}`;
}
