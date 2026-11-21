/**
 * Magnetic Snapping Engine (MIT clean-room synthesis inspired by Cutaway & braincut).
 * Zero DOM, zero React — deterministic tick snapping for drag-to-move & drag-to-trim.
 */
import type { Rational, TimelineV3 } from "../../../../packages/shared-types/src/v3-contracts";
import { pixelsToTicks } from "./time";

export interface SnapResult {
  snappedTick: number;
  guideTick: number | null;
}

/** Collect all magnetic anchor ticks (0, playhead, markers, and non-excluded clip/item edges). */
export function collectSnapTargets(
  timeline: TimelineV3,
  playheadTick: number,
  excludeItemId?: string
): number[] {
  const targets = new Set<number>([0, Math.max(0, playheadTick)]);
  for (const m of timeline.markers || []) {
    targets.add(m.timeline_tick);
  }
  for (const track of timeline.tracks) {
    if (track.type === "video" || track.type === "audio") {
      for (const c of track.clips) {
        if (c.id === excludeItemId) continue;
        targets.add(c.timeline_start);
        targets.add(c.timeline_start + c.duration);
      }
    } else {
      for (const it of track.items) {
        if (it.id === excludeItemId) continue;
        targets.add(it.timeline_start);
        targets.add(it.timeline_start + it.duration);
      }
    }
  }
  return Array.from(targets).sort((a, b) => a - b);
}

/** Snap a candidate clip range [candidateStart, candidateStart + duration) against anchor ticks. */
export function snapRange(
  candidateStart: number,
  duration: number,
  targets: number[],
  zoomPxPerSec: number,
  tb: Rational,
  thresholdPx = 10
): SnapResult {
  const safeStart = Math.max(0, candidateStart);
  const thresholdTicks = Math.max(1, pixelsToTicks(thresholdPx, zoomPxPerSec, tb));

  let bestDelta = thresholdTicks + 1;
  let bestStart = safeStart;
  let guide: number | null = null;

  for (const t of targets) {
    // Snap head edge
    const dHead = Math.abs(safeStart - t);
    if (dHead <= thresholdTicks && dHead < bestDelta) {
      bestDelta = dHead;
      bestStart = t;
      guide = t;
    }
    // Snap tail edge
    const dTail = Math.abs(safeStart + duration - t);
    if (dTail <= thresholdTicks && dTail < bestDelta) {
      bestDelta = dTail;
      bestStart = Math.max(0, t - duration);
      guide = t;
    }
  }
  return { snappedTick: bestStart, guideTick: guide };
}
