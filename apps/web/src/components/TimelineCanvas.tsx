import React, { useRef, useState } from "react";
import type { TimelineV3, TrackV3 } from "../../../../packages/shared-types/src/v3-contracts";
import type { SelectionSnapshot } from "../editorStore";
import { collectSnapTargets, snapRange } from "../engine/snapping";
import { pixelsToTicks, ticksToPixels, ticksToSeconds } from "../engine/time";

interface TimelineCanvasProps {
  timeline: TimelineV3;
  playheadTick: number;
  zoomPxPerSec: number;
  snapEnabled: boolean;
  selection: SelectionSnapshot;
  onSeek: (tick: number) => void;
  onSelect: (sel: SelectionSnapshot) => void;
  onMoveClip: (clipId: string, newStartTick: number) => void;
  onTrimClip: (clipId: string, newSourceIn: number, newSourceOut: number) => void;
  onMoveSubtitle: (subId: string, newStartTick: number, newDuration: number) => void;
}

type DragState =
  | { kind: "move_clip"; id: string; startX: number; origStart: number; duration: number }
  | { kind: "trim_right_clip"; id: string; startX: number; origIn: number; origOut: number; rate: number }
  | { kind: "move_sub"; id: string; startX: number; origStart: number; duration: number };

export function TimelineCanvas({
  timeline,
  playheadTick,
  zoomPxPerSec,
  snapEnabled,
  selection,
  onSeek,
  onSelect,
  onMoveClip,
  onTrimClip,
  onMoveSubtitle,
}: TimelineCanvasProps): React.ReactElement {
  const tb = timeline.time_semantics.timeline_timebase;
  const tracksAreaRef = useRef<HTMLDivElement | null>(null);
  const [drag, setDrag] = useState<DragState | null>(null);
  const [previewTick, setPreviewTick] = useState<number | null>(null);
  const [snapGuideTick, setSnapGuideTick] = useState<number | null>(null);

  const totalSec = Math.max(15, Math.ceil(ticksToSeconds(playheadTick, tb)) + 10);
  const rulerTicks = Array.from({ length: totalSec + 1 }, (_, i) => i);

  const handlePointerMove = (e: React.PointerEvent) => {
    if (!drag) return;
    const dx = e.clientX - drag.startX;
    const deltaTicks = Math.round((dx / zoomPxPerSec) * (tb.den / tb.num));

    if (drag.kind === "move_clip" || drag.kind === "move_sub") {
      const rawStart = Math.max(0, drag.origStart + deltaTicks);
      if (snapEnabled) {
        const targets = collectSnapTargets(timeline, playheadTick, drag.id);
        const res = snapRange(rawStart, drag.duration, targets, zoomPxPerSec, tb, 10);
        setPreviewTick(res.snappedTick);
        setSnapGuideTick(res.guideTick);
      } else {
        setPreviewTick(rawStart);
        setSnapGuideTick(null);
      }
    } else if (drag.kind === "trim_right_clip") {
      const srcDelta = Math.round(deltaTicks * drag.rate);
      setPreviewTick(Math.max(drag.origIn + 3000, drag.origOut + srcDelta));
      setSnapGuideTick(null);
    }
  };

  const handlePointerUp = () => {
    if (!drag) return;
    if (previewTick !== null) {
      if (drag.kind === "move_clip") {
        onMoveClip(drag.id, previewTick);
      } else if (drag.kind === "trim_right_clip") {
        onTrimClip(drag.id, drag.origIn, previewTick);
      } else if (drag.kind === "move_sub") {
        onMoveSubtitle(drag.id, previewTick, drag.duration);
      }
    }
    setDrag(null);
    setPreviewTick(null);
    setSnapGuideTick(null);
  };

  const seekFromClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = Math.max(0, e.clientX - rect.left);
    onSeek(pixelsToTicks(x, zoomPxPerSec, tb));
  };

  const renderTrackContent = (track: TrackV3) => {
    if (track.type === "video" || track.type === "audio") {
      return track.clips.map((c) => {
        const isMoving = drag?.kind === "move_clip" && drag.id === c.id && previewTick !== null;
        const isTrimming = drag?.kind === "trim_right_clip" && drag.id === c.id && previewTick !== null;
        const startTick = isMoving ? previewTick : c.timeline_start;
        const durTicks = isTrimming
          ? Math.max(3000, Math.round((previewTick - c.source_range.in) / (c.playback_rate || 1)))
          : c.duration;
        const isSel = selection.kind === "clip" && selection.id === c.id;
        return (
          <div
            key={c.id}
            onPointerDown={(e) => {
              e.stopPropagation();
              onSelect({ kind: "clip", id: c.id });
              setDrag({ kind: "move_clip", id: c.id, startX: e.clientX, origStart: c.timeline_start, duration: c.duration });
            }}
            style={{
              position: "absolute",
              left: ticksToPixels(startTick, zoomPxPerSec, tb),
              width: Math.max(28, ticksToPixels(durTicks, zoomPxPerSec, tb)),
              top: 4,
              height: 36,
              background: track.type === "video" ? "#2563EB" : "#059669",
              border: isSel ? "2px solid #FACC15" : "1px solid rgba(255,255,255,0.2)",
              borderRadius: 6,
              color: "#fff",
              padding: "4px 10px",
              fontSize: 11,
              cursor: "grab",
              userSelect: "none",
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              boxSizing: "border-box",
            }}
          >
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {track.type === "video" ? "🎬" : "🔊"} {c.id.slice(0, 6)} · {c.playback_rate}x
            </span>
            {/* Right Edge Drag-to-Trim Handle */}
            <div
              title="拖动裁剪片尾"
              onPointerDown={(e) => {
                e.stopPropagation();
                onSelect({ kind: "clip", id: c.id });
                setDrag({
                  kind: "trim_right_clip",
                  id: c.id,
                  startX: e.clientX,
                  origIn: c.source_range.in,
                  origOut: c.source_range.out,
                  rate: c.playback_rate || 1,
                });
              }}
              style={{
                width: 7,
                height: "100%",
                background: "rgba(255,255,255,0.45)",
                borderRadius: 3,
                cursor: "ew-resize",
                flexShrink: 0,
              }}
            />
          </div>
        );
      });
    }

    return track.items.map((it) => {
      const isMoving = drag?.kind === "move_sub" && drag.id === it.id && previewTick !== null;
      const startTick = isMoving ? previewTick : it.timeline_start;
      const isSel = selection.kind === "subtitle" && selection.id === it.id;
      return (
        <div
          key={it.id}
          onPointerDown={(e) => {
            e.stopPropagation();
            if (track.type === "subtitle") {
              onSelect({ kind: "subtitle", id: it.id });
              setDrag({ kind: "move_sub", id: it.id, startX: e.clientX, origStart: it.timeline_start, duration: it.duration });
            }
          }}
          style={{
            position: "absolute",
            left: ticksToPixels(startTick, zoomPxPerSec, tb),
            width: Math.max(28, ticksToPixels(it.duration, zoomPxPerSec, tb)),
            top: 6,
            height: 30,
            background: track.type === "subtitle" ? "#D97706" : "#7C3AED",
            border: isSel ? "2px solid #FACC15" : "1px solid rgba(255,255,255,0.2)",
            borderRadius: 5,
            color: "#fff",
            padding: "4px 8px",
            fontSize: 11,
            cursor: "grab",
            userSelect: "none",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            boxSizing: "border-box",
          }}
        >
          {"text" in it && it.text ? `💬 ${it.text}` : `✨ ${it.id.slice(0, 6)}`}
        </div>
      );
    });
  };

  const playheadX = ticksToPixels(playheadTick, zoomPxPerSec, tb);

  return (
    <div
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerLeave={handlePointerUp}
      style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden", background: "#0B1120" }}
    >
      {/* Time Ruler */}
      <div style={{ display: "flex", borderBottom: "1px solid #1E293B", height: 28 }}>
        <div style={{ width: 136, padding: "4px 10px", fontSize: 11, color: "#64748B", borderRight: "1px solid #1E293B" }}>
          Tracks ({timeline.tracks.length})
        </div>
        <div
          onClick={seekFromClick}
          style={{ position: "relative", flex: 1, cursor: "pointer", overflow: "hidden", background: "#0F172A" }}
        >
          {rulerTicks.map((s) => (
            <div
              key={s}
              style={{
                position: "absolute",
                left: s * zoomPxPerSec,
                top: 0,
                height: "100%",
                borderLeft: "1px solid #334155",
                paddingLeft: 4,
                fontSize: 10,
                color: "#94A3B8",
              }}
            >
              {s}s
            </div>
          ))}
          {(timeline.markers || []).map((m) => (
            <div
              key={m.id}
              title={`Marker: ${m.label}`}
              style={{
                position: "absolute",
                left: ticksToPixels(m.timeline_tick, zoomPxPerSec, tb) - 4,
                bottom: 2,
                width: 8,
                height: 8,
                borderRadius: 2,
                background: m.color || "#F59E0B",
              }}
            />
          ))}
          <div style={{ position: "absolute", left: playheadX - 1, top: 0, bottom: 0, width: 2, background: "#EF4444" }} />
        </div>
      </div>

      {/* Multi-Track Lanes */}
      <div ref={tracksAreaRef} style={{ flex: 1, overflowY: "auto", overflowX: "auto", position: "relative" }}>
        {timeline.tracks.map((track) => (
          <div key={track.id} style={{ display: "flex", borderBottom: "1px solid #1E293B", height: 46 }}>
            <div
              style={{
                width: 136,
                padding: "0 10px",
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                fontSize: 12,
                color: "#CBD5E1",
                borderRight: "1px solid #1E293B",
                background: "#0F172A",
              }}
            >
              <span>{track.name || track.type}</span>
              <span style={{ fontSize: 10, color: "#64748B", textTransform: "uppercase" }}>{track.type}</span>
            </div>
            <div
              onClick={seekFromClick}
              style={{ position: "relative", flex: 1, minWidth: totalSec * zoomPxPerSec, background: "#111827" }}
            >
              {renderTrackContent(track)}
              {/* Magnetic Snap Guide Line */}
              {snapGuideTick !== null && (
                <div
                  style={{
                    position: "absolute",
                    left: ticksToPixels(snapGuideTick, zoomPxPerSec, tb),
                    top: 0,
                    bottom: 0,
                    width: 2,
                    background: "#22D3EE",
                    pointerEvents: "none",
                    zIndex: 15,
                  }}
                />
              )}
              {/* Red Playhead */}
              <div
                style={{
                  position: "absolute",
                  left: playheadX - 1,
                  top: 0,
                  bottom: 0,
                  width: 2,
                  background: "#EF4444",
                  pointerEvents: "none",
                  zIndex: 20,
                }}
              />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
