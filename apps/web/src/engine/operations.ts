/**
 * Pure-TS Timeline EditOperation Builders & Local Optimistic Simulator (zero DOM, zero React).
 * Follows Cutaway's pure-function principle: returns null on no-op so empty edits never pollute history.
 * Strictly emits frozen v3.0 EditOperationInputV3 payloads matching TimelineService.
 */
import type {
  EditOperationInputV3,
  TimelineV3,
  VideoClipV3,
} from "../../../../packages/shared-types/src/v3-contracts";

export const MIN_CLIP_TICKS = 1000; // 1/30s at 1/30000 timebase

function genUuid(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

export function findVideoOrAudioClip(timeline: TimelineV3, clipId: string): VideoClipV3 | null {
  for (const tr of timeline.tracks) {
    if (tr.type === "video" || tr.type === "audio") {
      const found = tr.clips.find((c) => c.id === clipId);
      if (found) return found as VideoClipV3;
    }
  }
  return null;
}

/** Pure optimistic simulation + canonical EditOperationInputV3 creation. Returns null on no-op. */
export function applyLocalOp(
  timeline: TimelineV3,
  op: EditOperationInputV3,
  ripple = false
): TimelineV3 | null {
  const next: TimelineV3 = JSON.parse(JSON.stringify(timeline));
  next.parent_version = timeline.version;
  next.version = timeline.version + 1;
  next.origin_command = "edit";
  next.undo_scope = "user_edit";
  const p = op.payload as Record<string, unknown>;

  switch (op.operation_type) {
    case "MOVE_CLIP": {
      const clipId = String(p.clip_id);
      const targetStart = Math.max(0, Math.round(Number(p.target_timeline_start)));
      const clip = findVideoOrAudioClip(next, clipId);
      if (!clip || clip.timeline_start === targetStart) return null;
      clip.timeline_start = targetStart;
      return next;
    }
    case "SPLIT_CLIP": {
      const clipId = String(p.clip_id);
      const splitTick = Math.round(Number(p.split_at_timeline_tick));
      for (const tr of next.tracks) {
        if (tr.type === "video" || tr.type === "audio") {
          const idx = tr.clips.findIndex((c) => c.id === clipId);
          if (idx === -1) continue;
          const orig = tr.clips[idx];
          if (splitTick <= orig.timeline_start + MIN_CLIP_TICKS || splitTick >= orig.timeline_start + orig.duration - MIN_CLIP_TICKS) {
            return null;
          }
          const leftDur = splitTick - orig.timeline_start;
          const rightDur = orig.duration - leftDur;
          const srcSpan = orig.source_range.out - orig.source_range.in;
          const srcSplit = orig.source_range.in + Math.round(srcSpan * (leftDur / orig.duration));
          const rightClip = JSON.parse(JSON.stringify(orig));
          rightClip.id = genUuid();
          orig.duration = leftDur;
          orig.source_range.out = srcSplit;
          rightClip.timeline_start = splitTick;
          rightClip.duration = rightDur;
          rightClip.source_range.in = srcSplit;
          tr.clips.splice(idx + 1, 0, rightClip);
          return next;
        }
      }
      return null;
    }
    case "TRIM_CLIP": {
      const clipId = String(p.clip_id);
      const newIn = Math.max(0, Math.round(Number(p.new_source_in)));
      const newOut = Math.round(Number(p.new_source_out));
      if (newOut <= newIn + MIN_CLIP_TICKS) return null;
      const clip = findVideoOrAudioClip(next, clipId);
      if (!clip) return null;
      if (clip.source_range.in === newIn && clip.source_range.out === newOut) return null;
      clip.source_range.in = newIn;
      clip.source_range.out = newOut;
      clip.duration = Math.max(MIN_CLIP_TICKS, Math.round((newOut - newIn) / (clip.playback_rate || 1.0)));
      return next;
    }
    case "REMOVE_CLIP": {
      const clipId = String(p.clip_id);
      for (const tr of next.tracks) {
        if (tr.type === "video" || tr.type === "audio") {
          const idx = tr.clips.findIndex((c) => c.id === clipId);
          if (idx === -1) continue;
          const removed = tr.clips[idx];
          tr.clips.splice(idx, 1);
          if (ripple) {
            for (const c of tr.clips) {
              if (c.timeline_start > removed.timeline_start) {
                c.timeline_start = Math.max(0, c.timeline_start - removed.duration);
              }
            }
          }
          return next;
        }
      }
      return null;
    }
    case "UPDATE_PLAYBACK_RATE": {
      const clipId = String(p.clip_id);
      const rate = Math.min(4.0, Math.max(0.25, Number(p.playback_rate)));
      const clip = findVideoOrAudioClip(next, clipId);
      if (!clip || Math.abs(clip.playback_rate - rate) < 1e-6) return null;
      clip.playback_rate = rate;
      clip.duration = Math.max(MIN_CLIP_TICKS, Math.round((clip.source_range.out - clip.source_range.in) / rate));
      return next;
    }
    case "UPDATE_VOLUME": {
      const clipId = String(p.clip_id);
      const volume = Math.min(4.0, Math.max(0.0, Number(p.volume)));
      const clip = findVideoOrAudioClip(next, clipId);
      if (!clip) return null;
      clip.audio = { ...(clip.audio || { enabled: true, volume: 1.0 }), volume };
      if ("enabled" in p) clip.audio.enabled = Boolean(p.enabled);
      return next;
    }
    case "ADD_SUBTITLE": {
      const tr = next.tracks.find((t) => t.id === String(p.track_id) && t.type === "subtitle");
      if (!tr || tr.type !== "subtitle") return null;
      tr.items.push({
        id: genUuid(),
        timeline_start: Math.max(0, Math.round(Number(p.timeline_start))),
        duration: Math.max(MIN_CLIP_TICKS, Math.round(Number(p.duration))),
        text: String(p.text || "New Caption"),
        style_preset: "default",
        safe_zone: (p.safe_zone as "bottom" | "center_seam" | "top") || "bottom",
      });
      return next;
    }
    case "UPDATE_SUBTITLE": {
      const subId = String(p.subtitle_id);
      for (const tr of next.tracks) {
        if (tr.type === "subtitle") {
          const it = tr.items.find((s) => s.id === subId);
          if (!it) continue;
          if (typeof p.text === "string") it.text = p.text;
          if (typeof p.timeline_start === "number") it.timeline_start = Math.max(0, Math.round(p.timeline_start));
          if (typeof p.duration === "number") it.duration = Math.max(MIN_CLIP_TICKS, Math.round(p.duration));
          return next;
        }
      }
      return null;
    }
    case "REMOVE_SUBTITLE": {
      const subId = String(p.subtitle_id);
      for (const tr of next.tracks) {
        if (tr.type === "subtitle") {
          const before = tr.items.length;
          tr.items = tr.items.filter((s) => s.id !== subId);
          if (tr.items.length < before) return next;
        }
      }
      return null;
    }
    case "ADD_MARKER": {
      next.markers = next.markers || [];
      next.markers.push({
        id: genUuid(),
        timeline_tick: Math.max(0, Math.round(Number(p.timeline_tick))),
        label: String(p.label || "Marker"),
        kind: (p.kind as "chapter" | "beat" | "review_note" | "cut_point" | "ai_highlight") || "chapter",
        color: String(p.color || "#F59E0B"),
      });
      return next;
    }
    default:
      return null;
  }
}

export function buildOp(
  operation_type: EditOperationInputV3["operation_type"],
  payload: Record<string, unknown>
): EditOperationInputV3 {
  return { operation_id: genUuid(), operation_type, payload, reversible: true };
}
