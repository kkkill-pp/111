import React, { useState } from "react";
import type { TimelineV3, TrackV3 } from "../../../packages/shared-types/src/v3-contracts";
import { formatTimecode, ticksToSeconds } from "./editorStore";

const INITIAL_TIMELINE: TimelineV3 = {
  schema_version: "3.0",
  timeline_id: "11111111-1111-4111-8111-111111111111",
  project_id: "22222222-2222-4222-8222-222222222222",
  version: 1,
  parent_version: null,
  origin_command: "init",
  undo_scope: "user_edit",
  time_semantics: {
    unit: "ticks",
    timeline_timebase: { num: 1, den: 30000 },
    frame_rate: { num: 30, den: 1 },
    rounding_policy: "half_to_even",
    coordinate_model: "half_open_source_and_project_ticks",
  },
  profile: {
    width: 1080,
    height: 1920,
    frame_rate: { num: 30, den: 1 },
    timeline_timebase: { num: 1, den: 30000 },
    audio_sample_rate: 48000,
    aspect_preset: "9:16",
  },
  tracks: [
    {
      id: "33333333-3333-4333-8333-333333333333",
      type: "video",
      name: "V1 Main",
      order: 0,
      clips: [
        {
          id: "44444444-4444-4444-8444-444444444444",
          asset_id: "55555555-5555-4555-8555-555555555555",
          source_range: { in: 0, out: 120000, timebase: { num: 1, den: 30000 } },
          timeline_start: 0,
          duration: 120000,
          playback_rate: 1.0,
          keyframes: [],
          effects: [],
        },
      ],
    },
    {
      id: "66666666-6666-4666-8666-666666666666",
      type: "audio",
      name: "A1 Dialogue",
      order: 1,
      clips: [],
    },
    {
      id: "77777777-7777-4777-8777-777777777777",
      type: "subtitle",
      name: "S1 Captions",
      order: 2,
      items: [
        {
          id: "88888888-8888-4888-8888-888888888888",
          timeline_start: 15000,
          duration: 60000,
          text: "AI Video Studio v3.0 Multi-Track Prototype",
          style_preset: "default",
          safe_zone: "bottom",
        },
      ],
    },
    {
      id: "99999999-9999-4999-8999-999999999999",
      type: "overlay",
      name: "O1 Graphics",
      order: 3,
      items: [],
    },
  ],
  markers: [
    {
      id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      timeline_tick: 30000,
      label: "Hook Point",
      kind: "chapter",
      color: "#F59E0B",
    },
  ],
};

export function App(): React.ReactElement {
  const [timeline, setTimeline] = useState<TimelineV3>(INITIAL_TIMELINE);
  const [playheadTick, setPlayheadTick] = useState<number>(0);
  const [zoomPxPerSec, setZoomPxPerSec] = useState<number>(80);

  const tb = timeline.time_semantics.timeline_timebase;

  const renderTrackItems = (track: TrackV3) => {
    if (track.type === "video" || track.type === "audio") {
      return track.clips.map((c) => (
        <div
          key={c.id}
          style={{
            position: "absolute",
            left: ticksToSeconds(c.timeline_start, tb.num, tb.den) * zoomPxPerSec,
            width: Math.max(24, ticksToSeconds(c.duration, tb.num, tb.den) * zoomPxPerSec),
            height: 36,
            background: track.type === "video" ? "#2563EB" : "#059669",
            color: "#fff",
            borderRadius: 4,
            padding: "4px 8px",
            fontSize: 12,
            overflow: "hidden",
          }}
        >
          Clip {c.id.slice(0, 6)} ({c.playback_rate}x)
        </div>
      ));
    }
    return track.items.map((it) => (
      <div
        key={it.id}
        style={{
          position: "absolute",
          left: ticksToSeconds(it.timeline_start, tb.num, tb.den) * zoomPxPerSec,
          width: Math.max(24, ticksToSeconds(it.duration, tb.num, tb.den) * zoomPxPerSec),
          height: 30,
          background: track.type === "subtitle" ? "#D97706" : "#7C3AED",
          color: "#fff",
          borderRadius: 4,
          padding: "4px 8px",
          fontSize: 11,
          overflow: "hidden",
        }}
      >
        {"text" in it && it.text ? it.text : it.id.slice(0, 6)}
      </div>
    ));
  };

  return (
    <div style={{ display: "grid", gridTemplateRows: "48px 1fr 280px", height: "100vh", background: "#0F172A", color: "#F8FAFC", fontFamily: "system-ui, sans-serif" }}>
      <header style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "0 16px", borderBottom: "1px solid #1E293B" }}>
        <strong>AI Video Studio v3.0 — Multi-Track Editor (v{timeline.version})</strong>
        <div style={{ display: "flex", gap: 8 }}>
          <span>Playhead: {formatTimecode(playheadTick, tb.num, tb.den)}</span>
          <button onClick={() => setZoomPxPerSec((z) => Math.max(20, z - 20))}>-</button>
          <button onClick={() => setZoomPxPerSec((z) => Math.min(240, z + 20))}>+</button>
        </div>
      </header>
      <main style={{ display: "grid", gridTemplateColumns: "260px 1fr 280px", borderBottom: "1px solid #1E293B" }}>
        <aside style={{ padding: 12, borderRight: "1px solid #1E293B" }}>
          <h4 style={{ marginTop: 0 }}>Media Assets</h4>
          <p style={{ fontSize: 12, color: "#94A3B8" }}>Upload & proxy pipeline wired to /projects/&#123;id&#125;/upload-sessions</p>
        </aside>
        <section style={{ display: "flex", alignItems: "center", justifyContent: "center", background: "#020617" }}>
          <div style={{ width: 220, height: 390, border: "1px solid #334155", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 12, color: "#94A3B8" }}>
            Preview Canvas ({timeline.profile.width}x{timeline.profile.height} @ 30fps)
          </div>
        </section>
        <aside style={{ padding: 12, borderLeft: "1px solid #1E293B" }}>
          <h4 style={{ marginTop: 0 }}>Inspector & HITL Proposals</h4>
          <p style={{ fontSize: 12, color: "#94A3B8" }}>Optimistic Lock v{timeline.version} · Scope: {timeline.undo_scope}</p>
        </aside>
      </main>
      <section style={{ padding: 12, overflowX: "auto" }}>
        {timeline.tracks.map((track) => (
          <div key={track.id} style={{ display: "flex", alignItems: "center", marginBottom: 8 }}>
            <div style={{ width: 120, fontSize: 12, color: "#CBD5E1" }}>{track.name || track.type}</div>
            <div
              onClick={(e) => {
                const rect = e.currentTarget.getBoundingClientRect();
                const sec = Math.max(0, (e.clientX - rect.left) / zoomPxPerSec);
                setPlayheadTick(Math.round((sec * tb.den) / tb.num));
              }}
              style={{ position: "relative", flex: 1, height: 42, background: "#1E293B", borderRadius: 4, cursor: "pointer" }}
            >
              {renderTrackItems(track)}
            </div>
          </div>
        ))}
      </section>
    </div>
  );
}

export default App;
