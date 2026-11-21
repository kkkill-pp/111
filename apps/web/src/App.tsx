import React, { useCallback, useEffect, useRef, useState } from "react";
import type {
  EditOperationInputV3,
  SubtitleItemV3,
  TimelineV3,
  VideoClipV3,
} from "../../../packages/shared-types/src/v3-contracts";
import { AgentProposalPanel } from "./components/AgentProposalPanel";
import { TimelineCanvas } from "./components/TimelineCanvas";
import {
  type ApiConnectionConfig,
  type HistoryEntry,
  type SelectionSnapshot,
  commitHumanOperations,
  invokeUndoRedoApi,
  triggerRenderAndQa,
} from "./editorStore";
import { applyLocalOp, buildOp, findVideoOrAudioClip } from "./engine/operations";
import { DEFAULT_FPS, formatTimecode, frameStepTicks, ticksToSeconds } from "./engine/time";

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
      name: "V1 主视频轨",
      order: 0,
      clips: [
        {
          id: "44444444-4444-4444-8444-444444444444",
          asset_id: "55555555-5555-4555-8555-555555555555",
          source_range: { in: 0, out: 180000, timebase: { num: 1, den: 30000 } },
          timeline_start: 0,
          duration: 180000,
          playback_rate: 1.0,
          audio: { enabled: true, volume: 1.0, fade_in_ticks: 0, fade_out_ticks: 0 },
          keyframes: [],
          effects: [],
        },
      ],
    },
    {
      id: "66666666-6666-4666-8666-666666666666",
      type: "audio",
      name: "A1 音频轨",
      order: 1,
      clips: [],
    },
    {
      id: "77777777-7777-4777-8777-777777777777",
      type: "subtitle",
      name: "S1 字幕轨",
      order: 2,
      items: [
        {
          id: "88888888-8888-4888-8888-888888888888",
          timeline_start: 15000,
          duration: 90000,
          text: "AI Video Studio v3.0 多轨编辑器",
          style_preset: "default",
          safe_zone: "bottom",
        },
      ],
    },
    {
      id: "99999999-9999-4999-8999-999999999999",
      type: "overlay",
      name: "O1 贴纸/画中画",
      order: 3,
      items: [],
    },
  ],
  markers: [
    {
      id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      timeline_tick: 60000,
      label: "黄金 2 秒钩子",
      kind: "chapter",
      color: "#F59E0B",
    },
  ],
};

export function App(): React.ReactElement {
  const [history, setHistory] = useState<HistoryEntry[]>([
    { timeline: INITIAL_TIMELINE, selection: { kind: "clip", id: "44444444-4444-4444-8444-444444444444" }, command: "init", scope: "user_edit", label: "v1 初始化" },
  ]);
  const [historyIdx, setHistoryIdx] = useState<number>(0);
  const [selection, setSelection] = useState<SelectionSnapshot>({ kind: "clip", id: "44444444-4444-4444-8444-444444444444" });
  const [playheadTick, setPlayheadTick] = useState<number>(60000);
  const [isPlaying, setIsPlaying] = useState<boolean>(false);
  const [zoomPxPerSec, setZoomPxPerSec] = useState<number>(90);
  const [snapEnabled, setSnapEnabled] = useState<boolean>(true);
  const [rippleEnabled, setRippleEnabled] = useState<boolean>(false);
  const [rightTab, setRightTab] = useState<"inspector" | "agent">("inspector");
  const [statusLog, setStatusLog] = useState<string>("就绪（支持导入本地 MP4 实时预览；勾选连接后端可实跑 FFmpeg 导出与下载）");
  const [videoUrl, setVideoUrl] = useState<string | null>(null);
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [apiCfg, setApiCfg] = useState<ApiConnectionConfig>({
    enabled: false,
    apiBase: "/api/v1",
    tenantId: "11111111-1111-4111-8111-111111111111",
    userId: "33333333-3333-4333-8333-333333333333",
    role: "editor",
  });

  const current = history[historyIdx];
  const timeline = current.timeline;
  const tb = timeline.time_semantics.timeline_timebase;

  // Sync HTML5 <video> element with rational playheadTick
  useEffect(() => {
    const v = videoRef.current;
    if (!v || !videoUrl) return;
    const targetSec = ticksToSeconds(playheadTick, tb);
    if (Math.abs(v.currentTime - targetSec) > 0.08) {
      v.currentTime = targetSec;
    }
    if (isPlaying && v.paused) {
      void v.play().catch(() => {});
    } else if (!isPlaying && !v.paused) {
      v.pause();
    }
  }, [playheadTick, isPlaying, videoUrl, tb]);

  // Playback loop
  useEffect(() => {
    if (!isPlaying) return;
    const step = frameStepTicks(DEFAULT_FPS, tb);
    const id = window.setInterval(() => {
      setPlayheadTick((t) => (t + step > 300000 ? 0 : t + step));
    }, 33);
    return () => window.clearInterval(id);
  }, [isPlaying, tb]);

  const executeOp = useCallback(
    async (op: EditOperationInputV3, label: string) => {
      const next = applyLocalOp(timeline, op, rippleEnabled);
      if (!next) {
        setStatusLog(`跳过无效/空操作: ${op.operation_type}`);
        return;
      }
      if (apiCfg.enabled) {
        try {
          await commitHumanOperations(apiCfg, timeline.timeline_id, timeline.version, [op], `idem-web-${op.operation_id}`);
        } catch (err) {
          setStatusLog(`后端拒绝: ${(err as Error).message}`);
          return;
        }
      }
      const trimmed = history.slice(0, historyIdx + 1);
      trimmed.push({ timeline: next, selection, command: "edit", scope: "user_edit", label: `v${next.version} ${label}` });
      setHistory(trimmed);
      setHistoryIdx(trimmed.length - 1);
      setStatusLog(`已提交 ${op.operation_type} → v${next.version}`);
    },
    [timeline, rippleEnabled, apiCfg, history, historyIdx, selection]
  );

  const handleUndo = useCallback(async () => {
    if (historyIdx <= 0) return;
    if (apiCfg.enabled) {
      try {
        await invokeUndoRedoApi(apiCfg, timeline.timeline_id, "undo", timeline.version, "user_edit", `idem-undo-${Date.now()}`);
      } catch (err) {
        setStatusLog(`Undo 失败: ${(err as Error).message}`);
        return;
      }
    }
    const prevIdx = historyIdx - 1;
    setHistoryIdx(prevIdx);
    setSelection(history[prevIdx].selection);
    setStatusLog(`Undo → 恢复至 ${history[prevIdx].label} (scope: user_edit)`);
  }, [historyIdx, history, apiCfg, timeline]);

  const handleRedo = useCallback(async () => {
    if (historyIdx >= history.length - 1) return;
    if (apiCfg.enabled) {
      try {
        await invokeUndoRedoApi(apiCfg, timeline.timeline_id, "redo", timeline.version, "user_edit", `idem-redo-${Date.now()}`);
      } catch (err) {
        setStatusLog(`Redo 失败: ${(err as Error).message}`);
        return;
      }
    }
    const nextIdx = historyIdx + 1;
    setHistoryIdx(nextIdx);
    setSelection(history[nextIdx].selection);
    setStatusLog(`Redo → 前进至 ${history[nextIdx].label} (scope: user_edit)`);
  }, [historyIdx, history, apiCfg, timeline]);

  const handleSplitAtPlayhead = useCallback(() => {
    if (selection.kind !== "clip" || !selection.id) {
      setStatusLog("请先选中一个视频/音频片段再按 S 剪切");
      return;
    }
    void executeOp(buildOp("SPLIT_CLIP", { clip_id: selection.id, split_at_timeline_tick: playheadTick }), "剪切片段");
  }, [selection, playheadTick, executeOp]);

  const handleDeleteSelected = useCallback(() => {
    if (selection.kind === "clip" && selection.id) {
      void executeOp(buildOp("REMOVE_CLIP", { clip_id: selection.id, ripple: rippleEnabled }), rippleEnabled ? "波纹删除片段" : "删除片段");
      setSelection({ kind: null, id: null });
    } else if (selection.kind === "subtitle" && selection.id) {
      void executeOp(buildOp("REMOVE_SUBTITLE", { subtitle_id: selection.id }), "删除字幕");
      setSelection({ kind: null, id: null });
    }
  }, [selection, rippleEnabled, executeOp]);

  // Global keyboard shortcuts (Space / S / N / R / Delete / Ctrl+Z / Ctrl+Shift+Z / Left / Right)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement)?.tagName === "INPUT" || (e.target as HTMLElement)?.tagName === "TEXTAREA") return;
      if (e.code === "Space") {
        e.preventDefault();
        setIsPlaying((p) => !p);
      } else if (e.key === "s" || e.key === "S") {
        handleSplitAtPlayhead();
      } else if (e.key === "n" || e.key === "N") {
        setSnapEnabled((v) => !v);
      } else if (e.key === "r" || e.key === "R") {
        setRippleEnabled((v) => !v);
      } else if (e.key === "Delete" || e.key === "Backspace") {
        handleDeleteSelected();
      } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z") {
        e.preventDefault();
        if (e.shiftKey) void handleRedo();
        else void handleUndo();
      } else if (e.key === "ArrowLeft") {
        setPlayheadTick((t) => Math.max(0, t - frameStepTicks(DEFAULT_FPS, tb)));
      } else if (e.key === "ArrowRight") {
        setPlayheadTick((t) => t + frameStepTicks(DEFAULT_FPS, tb));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [handleSplitAtPlayhead, handleDeleteSelected, handleUndo, handleRedo, tb]);

  const selectedClip: VideoClipV3 | null = selection.kind === "clip" && selection.id ? findVideoOrAudioClip(timeline, selection.id) : null;
  let selectedSub: SubtitleItemV3 | null = null;
  if (selection.kind === "subtitle" && selection.id) {
    for (const tr of timeline.tracks) {
      if (tr.type === "subtitle") {
        selectedSub = tr.items.find((s) => s.id === selection.id) || null;
      }
    }
  }

  // Active subtitles at playhead for live Preview Monitor overlay
  const activeSubtitles: SubtitleItemV3[] = [];
  for (const tr of timeline.tracks) {
    if (tr.type === "subtitle") {
      for (const s of tr.items) {
        if (playheadTick >= s.timeline_start && playheadTick < s.timeline_start + s.duration) {
          activeSubtitles.push(s);
        }
      }
    }
  }

  const handleExport = async (mode: "preview" | "final") => {
    if (!apiCfg.enabled) {
      setStatusLog(`演示模式：已生成 ${mode.toUpperCase()} RenderPlan v3.0（v${timeline.version}）；勾选右上角"连接后端"可实跑 FFmpeg + ffprobe QA。`);
      return;
    }
    setStatusLog(`正在提交 ${mode} FFmpeg 渲染与 ffprobe QA...`);
    try {
      const res = await triggerRenderAndQa(apiCfg, timeline.timeline_id, timeline.version, mode);
      setDownloadUrl(`${apiCfg.apiBase}/artifacts/${res.artifact_id}/download`);
      setStatusLog(`导出完成 (${mode}): artifact=${res.artifact_id.slice(0, 8)} · QA=${res.qa_status}`);
    } catch (err) {
      setStatusLog(`渲染失败: ${(err as Error).message}`);
    }
  };

  const subTrackId = timeline.tracks.find((t) => t.type === "subtitle")?.id || "";

  return (
    <div style={{ display: "grid", gridTemplateRows: "48px 1fr 270px", height: "100vh", background: "#0F172A", color: "#F8FAFC", fontFamily: "system-ui, sans-serif" }}>
      {/* Top Toolbar */}
      <header style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "0 14px", borderBottom: "1px solid #1E293B", background: "#0B1120" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <strong>AI Video Studio v3.0</strong>
          <span style={{ fontSize: 11, background: "#1E293B", padding: "2px 8px", borderRadius: 4, color: "#38BDF8" }}>
            v{timeline.version} · {timeline.undo_scope}
          </span>
          <button onClick={() => void handleUndo()} disabled={historyIdx <= 0}>↩ Undo</button>
          <button onClick={() => void handleRedo()} disabled={historyIdx >= history.length - 1}>↪ Redo</button>
          <button onClick={handleSplitAtPlayhead}>✂ 剪切 (S)</button>
          <button onClick={() => setSnapEnabled((v) => !v)} style={{ background: snapEnabled ? "#0E7490" : undefined, color: "#fff" }}>
            🧲 磁吸: {snapEnabled ? "开" : "关"}
          </button>
          <button onClick={() => setRippleEnabled((v) => !v)} style={{ background: rippleEnabled ? "#B45309" : undefined, color: "#fff" }}>
            🌊 波纹: {rippleEnabled ? "开" : "关"}
          </button>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <label style={{ fontSize: 11, color: "#94A3B8" }}>
            <input type="checkbox" checked={apiCfg.enabled} onChange={(e) => setApiCfg({ ...apiCfg, enabled: e.target.checked })} /> 连接后端 API
          </label>
          <button onClick={() => void handleExport("preview")}>预览渲染</button>
          <button onClick={() => void handleExport("final")} style={{ background: "#2563EB", color: "#fff", border: "none", padding: "4px 10px", borderRadius: 4 }}>
            导出 MP4 (FFmpeg)
          </button>
        </div>
      </header>

      {/* Main 3-Pane Workspace */}
      <main style={{ display: "grid", gridTemplateColumns: "250px 1fr 290px", borderBottom: "1px solid #1E293B", overflow: "hidden" }}>
        {/* Left: Assets, Quick Add & Immutable Version History */}
        <aside style={{ padding: 12, borderRight: "1px solid #1E293B", overflowY: "auto" }}>
          <h4 style={{ margin: "0 0 8px", fontSize: 13 }}>导入本地媒体</h4>
          <input
            type="file"
            accept="video/*,audio/*"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (!f) return;
              setVideoUrl(URL.createObjectURL(f));
              setStatusLog(`已载入本地视频: ${f.name} (${(f.size / 1048576).toFixed(2)} MB)`);
            }}
            style={{ width: "100%", fontSize: 11, marginBottom: 14 }}
          />
          {downloadUrl && (
            <a href={downloadUrl} download style={{ display: "block", marginBottom: 12, padding: "6px 8px", background: "#059669", color: "#fff", borderRadius: 4, textAlign: "center", fontSize: 12, textDecoration: "none" }}>
              ⬇ 下载导出成片 (MP4)
            </a>
          )}
          <h4 style={{ margin: "0 0 8px", fontSize: 13 }}>快捷插入</h4>
          <div style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: 16 }}>
            <button
              onClick={() =>
                void executeOp(
                  buildOp("ADD_SUBTITLE", { track_id: subTrackId, timeline_start: playheadTick, duration: 60000, text: "新字幕片段", safe_zone: "bottom" }),
                  "新增字幕"
                )
              }
            >
              + 在播放头添加字幕
            </button>
            <button
              onClick={() =>
                void executeOp(buildOp("ADD_MARKER", { timeline_tick: playheadTick, label: `标记 ${ticksToSeconds(playheadTick, tb).toFixed(1)}s`, kind: "chapter" }), "新增标记")
              }
            >
              + 在播放头添加标记点
            </button>
          </div>

          <h4 style={{ margin: "0 0 8px", fontSize: 13 }}>不可变版本链 ({history.length})</h4>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {history.map((h, i) => (
              <div
                key={i}
                onClick={() => {
                  setHistoryIdx(i);
                  setSelection(h.selection);
                }}
                style={{
                  padding: "4px 8px",
                  fontSize: 11,
                  borderRadius: 4,
                  cursor: "pointer",
                  background: i === historyIdx ? "#1D4ED8" : "#1E293B",
                  color: i === historyIdx ? "#fff" : "#CBD5E1",
                }}
              >
                {h.label}
              </div>
            ))}
          </div>
        </aside>

        {/* Center: Preview Monitor + Transport */}
        <section style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", background: "#020617", padding: 12 }}>
          <div
            style={{
              position: "relative",
              width: 210,
              height: 360,
              background: "linear-gradient(160deg, #1E293B 0%, #0F172A 100%)",
              border: "1px solid #334155",
              borderRadius: 8,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              overflow: "hidden",
            }}
          >
            {videoUrl ? (
              <video ref={videoRef} src={videoUrl} muted={false} playsInline style={{ width: "100%", height: "100%", objectFit: "contain" }} />
            ) : (
            <div style={{ textAlign: "center", color: "#94A3B8", fontSize: 12 }}>
              <div>9:16 预览画布 ({timeline.profile.width}×{timeline.profile.height})</div>
              <div style={{ marginTop: 6, fontSize: 18, fontFamily: "monospace", color: "#F8FAFC" }}>
                {formatTimecode(playheadTick, tb, DEFAULT_FPS)}
              </div>
            </div>
            )}
            {/* Live ASS-style Subtitle Overlay */}
            {activeSubtitles.map((s) => (
              <div
                key={s.id}
                style={{
                  position: "absolute",
                  left: 10,
                  right: 10,
                  bottom: s.safe_zone === "top" ? undefined : s.safe_zone === "center_seam" ? "45%" : 24,
                  top: s.safe_zone === "top" ? 24 : undefined,
                  textAlign: "center",
                  background: "rgba(0,0,0,0.68)",
                  color: "#FFFFFF",
                  padding: "4px 8px",
                  borderRadius: 4,
                  fontSize: 13,
                  fontWeight: 600,
                }}
              >
                {s.text}
              </div>
            ))}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 10 }}>
            <button onClick={() => setPlayheadTick((t) => Math.max(0, t - frameStepTicks(DEFAULT_FPS, tb)))}>◀ 1帧</button>
            <button onClick={() => setIsPlaying((p) => !p)}>{isPlaying ? "⏸ 暂停" : "▶ 播放"}</button>
            <button onClick={() => setPlayheadTick((t) => t + frameStepTicks(DEFAULT_FPS, tb))}>1帧 ▶</button>
            <span style={{ fontSize: 11, color: "#94A3B8" }}>缩放:</span>
            <button onClick={() => setZoomPxPerSec((z) => Math.max(30, z - 20))}>-</button>
            <button onClick={() => setZoomPxPerSec((z) => Math.min(240, z + 20))}>+</button>
          </div>
        </section>

        {/* Right: Inspector Panel */}
        <aside style={{ padding: 12, borderLeft: "1px solid #1E293B", overflowY: "auto" }}>
          <div style={{ display: "flex", gap: 6, marginBottom: 12 }}>
            <button onClick={() => setRightTab("inspector")} style={{ flex: 1, background: rightTab === "inspector" ? "#1D4ED8" : "#1E293B", color: "#fff", border: "none", padding: "5px 6px", borderRadius: 4, fontSize: 12 }}>
              属性检查器
            </button>
            <button onClick={() => setRightTab("agent")} style={{ flex: 1, background: rightTab === "agent" ? "#4F46E5" : "#1E293B", color: "#fff", border: "none", padding: "5px 6px", borderRadius: 4, fontSize: 12 }}>
              🤖 AI 提案审阅
            </button>
          </div>
          {rightTab === "agent" ? (
            <AgentProposalPanel
              timeline={timeline}
              apiCfg={apiCfg}
              onApplyLocalOps={(ops, label) => {
                for (const op of ops) void executeOp(op, label);
              }}
              onStatus={setStatusLog}
            />
          ) : (<>
          {selectedClip && (
            <div style={{ display: "flex", flexDirection: "column", gap: 10, fontSize: 12 }}>
              <div>片段 ID: <code>{selectedClip.id.slice(0, 8)}</code></div>
              <label>
                倍速 (`playback_rate`): <strong>{selectedClip.playback_rate}x</strong>
                <select
                  value={selectedClip.playback_rate}
                  onChange={(e) => void executeOp(buildOp("UPDATE_PLAYBACK_RATE", { clip_id: selectedClip.id, playback_rate: Number(e.target.value) }), `倍速 ${e.target.value}x`)}
                  style={{ width: "100%", marginTop: 4 }}
                >
                  {[0.5, 1.0, 1.5, 2.0, 3.0].map((r) => (
                    <option key={r} value={r}>{r}x</option>
                  ))}
                </select>
              </label>
              <label>
                音量: <strong>{Math.round(((selectedClip.audio?.volume ?? 1) * 100))}%</strong>
                <input
                  type="range"
                  min={0}
                  max={2}
                  step={0.1}
                  value={selectedClip.audio?.volume ?? 1}
                  onChange={(e) =>
                    void executeOp(
                      buildOp("UPDATE_VOLUME", { clip_id: selectedClip.id, volume: Number(e.target.value), enabled: Number(e.target.value) > 0 }),
                      `音量 ${Math.round(Number(e.target.value) * 100)}%`
                    )
                  }
                  style={{ width: "100%" }}
                />
              </label>
              <button onClick={handleDeleteSelected} style={{ background: "#991B1B", color: "#fff", border: "none", padding: 6, borderRadius: 4 }}>
                删除选中片段 (Delete)
              </button>
            </div>
          )}

          {selectedSub && (
            <div style={{ display: "flex", flexDirection: "column", gap: 10, fontSize: 12 }}>
              <div>字幕 ID: <code>{selectedSub.id.slice(0, 8)}</code></div>
              <label>
                字幕文案:
                <input
                  type="text"
                  value={selectedSub.text}
                  onChange={(e) => void executeOp(buildOp("UPDATE_SUBTITLE", { subtitle_id: selectedSub.id, text: e.target.value }), "修改字幕")}
                  style={{ width: "100%", marginTop: 4, padding: 4 }}
                />
              </label>
              <button onClick={handleDeleteSelected} style={{ background: "#991B1B", color: "#fff", border: "none", padding: 6, borderRadius: 4 }}>
                删除字幕
              </button>
            </div>
          )}

          {!selectedClip && !selectedSub && <p style={{ fontSize: 12, color: "#64748B" }}>在下方时间线点击任意片段或字幕以编辑属性。</p>}
          </>)}

          <div style={{ marginTop: 18, padding: 8, background: "#0B1120", borderRadius: 6, fontSize: 11, color: "#94A3B8" }}>
            <strong style={{ color: "#E2E8F0" }}>状态日志</strong>
            <div style={{ marginTop: 4 }}>{statusLog}</div>
          </div>
        </aside>
      </main>

      {/* Bottom Interactive Multi-Track Timeline */}
      <TimelineCanvas
        timeline={timeline}
        playheadTick={playheadTick}
        zoomPxPerSec={zoomPxPerSec}
        snapEnabled={snapEnabled}
        selection={selection}
        onSeek={setPlayheadTick}
        onSelect={setSelection}
        onMoveClip={(clipId, newStart) => void executeOp(buildOp("MOVE_CLIP", { clip_id: clipId, target_timeline_start: newStart }), "移动片段")}
        onTrimClip={(clipId, newIn, newOut) => void executeOp(buildOp("TRIM_CLIP", { clip_id: clipId, new_source_in: newIn, new_source_out: newOut }), "裁剪片段")}
        onMoveSubtitle={(subId, newStart, dur) => void executeOp(buildOp("UPDATE_SUBTITLE", { subtitle_id: subId, timeline_start: newStart, duration: dur }), "移动字幕")}
      />
    </div>
  );
}

export default App;
