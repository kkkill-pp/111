/* eslint-disable */
/**
 * AUTO-GENERATED FROM FROZEN AI VIDEO STUDIO v3.0 JSON SCHEMAS (Draft 2020-12).
 * DO NOT EDIT MANUALLY. Regenerate via: python3 scripts/generate_ts_types.py
 *
 * Source Schema SHA-256 Digests:
 * - timeline.v3.schema.json: sha256:e0ef3fcd8e197fde6652720c34725aff5ea8522d3f2b5b4f6e2ba3d8fcef864a
 * - edit-operation.v3.schema.json: sha256:5c28eac775405ed654c3710d3314cd0d822dfeeedf3eb07f516823dc0b1ad866
 * - render-plan.v3.schema.json: sha256:ddfd0c88e2e2cae47d9a3517ecd6261f1a62ed4e2186f6fabd30b7eed030064b
 * - agent-proposal.v3.schema.json: sha256:8d5c58abb69628c002a56edbae419960e8c03e5dca7f11c86b727622d582d60d
 */

export interface Rational {
  num: number;
  den: number;
}

export interface TimeSemanticsV3 {
  unit: "ticks";
  timeline_timebase: Rational;
  frame_rate: Rational;
  rounding_policy: "half_to_even";
  coordinate_model: "half_open_source_and_project_ticks";
}

export interface SourceRangeV3 {
  in: number;
  out: number;
  timebase: Rational;
  vfr_pts_map_ref?: string | null;
}

export type KeyframePropertyV3 = "position_x" | "position_y" | "scale" | "rotation_deg" | "opacity" | "volume" | "pan";
export type KeyframeEasingV3 = "linear" | "ease_in" | "ease_out" | "ease_in_out" | "hold";
export type EffectTypeV3 = "brightness_contrast" | "saturation" | "gaussian_blur" | "lut_3d" | "audio_eq" | "audio_denoise" | "loudnorm";
export type MarkerKindV3 = "chapter" | "beat" | "review_note" | "cut_point" | "ai_highlight";
export type AudioRoleV3 = "dialogue" | "music" | "sfx" | "voiceover" | "ambient";
export type UndoScopeV3 = "user_edit" | "proposal_batch" | "non_undoable_system";
export type AgentPermissionLevel = "L0" | "L1" | "L2" | "L3";
export type ProposalPermissionLevel = "L1" | "L2" | "L3";
export type ProposalStatusV3 = "draft" | "submitted" | "reviewing" | "approved" | "applied" | "rejected" | "revision_requested" | "conflicted" | "expired" | "cancelled";

export type EditOperationTypeV3 =
  | "ADD_CLIP"
  | "REMOVE_CLIP"
  | "TRIM_CLIP"
  | "MOVE_CLIP"
  | "SPLIT_CLIP"
  | "MERGE_CLIP"
  | "REORDER_CLIP"
  | "UPDATE_TRANSFORM"
  | "UPDATE_PLAYBACK_RATE"
  | "UPDATE_VOLUME"
  | "ADD_SUBTITLE"
  | "UPDATE_SUBTITLE"
  | "REMOVE_SUBTITLE"
  | "ADD_TRANSITION"
  | "REMOVE_TRANSITION"
  | "ADD_OVERLAY"
  | "REMOVE_OVERLAY"
  | "SET_CANVAS_PROFILE"
  | "ADD_KEYFRAME"
  | "APPLY_EFFECT"
  | "ADD_MARKER";

export interface KeyframeV3 {
  id: string;
  property: KeyframePropertyV3;
  timeline_tick: number;
  value: number;
  easing: KeyframeEasingV3;
}

export interface EffectV3 {
  id: string;
  effect_type: EffectTypeV3;
  enabled: boolean;
  params: Record<string, unknown>;
}

export interface MarkerV3 {
  id: string;
  timeline_tick: number;
  label: string;
  kind: MarkerKindV3;
  color?: string;
  metadata?: Record<string, unknown>;
}

export interface VideoClipV3 {
  id: string;
  asset_id: string;
  source_range: SourceRangeV3;
  timeline_start: number;
  duration: number;
  playback_rate: number;
  transform?: {
    mode: "fit" | "fill" | "manual" | "face_track" | "split" | "screencast" | "wide" | "inset" | "general";
    scale?: number;
    position_x?: number;
    position_y?: number;
    rotation_deg?: number;
    opacity?: number;
    crop_override_json?: Record<string, unknown> | null;
  };
  audio?: {
    enabled?: boolean;
    volume?: number;
    fade_in_ticks?: number;
    fade_out_ticks?: number;
  };
  transitions?: Array<{
    id: string;
    type: "cut" | "fade_in" | "fade_out" | "crossfade";
    duration_ticks: number;
    position: "in" | "out" | "between";
  }>;
  keyframes?: KeyframeV3[];
  effects?: EffectV3[];
}

export interface AudioItemV3 {
  id: string;
  asset_id: string;
  source_range: SourceRangeV3;
  timeline_start: number;
  duration: number;
  playback_rate: number;
  role?: AudioRoleV3;
  volume?: number;
  pan?: number;
  muted?: boolean;
  fade_in_ticks?: number;
  fade_out_ticks?: number;
  keyframes?: KeyframeV3[];
  effects?: EffectV3[];
}

export interface SubtitleItemV3 {
  id: string;
  timeline_start: number;
  duration: number;
  text: string;
  speaker_label?: string | null;
  style_preset?: string;
  safe_zone?: "bottom" | "center_seam" | "top";
  word_timings?: Array<{ word: string; start_tick: number; end_tick: number }>;
}

export interface OverlayItemV3 {
  id: string;
  kind: "text_hook" | "image" | "logo" | "lower_third";
  timeline_start: number;
  duration: number;
  asset_id?: string | null;
  text?: string | null;
  position_x?: number;
  position_y?: number;
  scale?: number;
  opacity?: number;
  keyframes?: KeyframeV3[];
  effects?: EffectV3[];
}

export type TrackV3 =
  | { id: string; type: "video"; name?: string; order: number; muted?: boolean; locked?: boolean; clips: VideoClipV3[] }
  | { id: string; type: "audio"; name?: string; order: number; muted?: boolean; locked?: boolean; clips: AudioItemV3[] }
  | { id: string; type: "subtitle"; name?: string; order: number; locked?: boolean; items: SubtitleItemV3[] }
  | { id: string; type: "overlay"; name?: string; order: number; locked?: boolean; items: OverlayItemV3[] };

export interface TimelineV3 {
  schema_version: "3.0";
  timeline_id: string;
  project_id: string;
  version: number;
  parent_version?: number | null;
  origin_command?: "init" | "storyboard_compile" | "edit" | "proposal_apply" | "undo" | "redo";
  undo_scope?: UndoScopeV3;
  snapshot_checksum?: string | null;
  time_semantics: TimeSemanticsV3;
  profile: {
    width: number;
    height: number;
    frame_rate: Rational;
    timeline_timebase: Rational;
    audio_sample_rate: 44100 | 48000;
    pixel_aspect_ratio?: Rational;
    aspect_preset?: "9:16" | "16:9" | "1:1" | "custom";
  };
  tracks: TrackV3[];
  markers: MarkerV3[];
  metadata?: Record<string, unknown>;
}

export interface EditOperationInputV3 {
  operation_id: string;
  operation_type: EditOperationTypeV3;
  payload: Record<string, unknown>;
  reason?: string;
  evidence_refs?: string[];
  risk_level?: "low" | "medium" | "high" | "critical";
  reversible?: boolean;
}

export interface EditOperationV3 extends EditOperationInputV3 {
  schema_version: "3.0";
  proposal_id?: string | null;
  timeline_id: string;
  base_version: number;
  result_version?: number | null;
  actor: {
    type: "user" | "agent" | "system";
    id: string;
    permission_level?: AgentPermissionLevel;
  };
  undo_scope: UndoScopeV3;
  originating_user_id?: string | null;
  originating_request_id: string;
  idempotency_key: string;
}

export interface RenderPlanV3 {
  render_plan_version: "3.0";
  plan_id: string;
  render_mode: "preview" | "final";
  renderer: "ffmpeg";
  timeline_id: string;
  timeline_version: number;
  timeline_checksum: string;
  plan_checksum: string;
  immutability_lock: {
    locked: true;
    locked_at: string;
    source_timeline_version: number;
    source_timeline_checksum: string;
  };
  time_semantics: {
    unit: "ticks";
    timeline_timebase: Rational;
    frame_rate: Rational;
    rounding_policy: "half_to_even";
  };
  render_range_ticks?: { start_tick: number; end_tick: number } | null;
  expected_duration_ticks: number;
  input_assets: Array<{
    asset_id: string;
    storage_key: string;
    checksum_sha256: string;
    is_vfr: boolean;
    pts_map_storage_key?: string | null;
  }>;
  output_profile: {
    width: number;
    height: number;
    frame_rate: Rational;
    pixel_format: "yuv420p";
    audio_sample_rate: 44100 | 48000;
    audio_channels: 1 | 2;
  };
  video_ops: Array<Record<string, unknown>>;
  audio_ops: Array<Record<string, unknown>>;
  subtitle_ops: Array<Record<string, unknown>>;
  overlay_ops: Array<Record<string, unknown>>;
  effect_ops: Array<Record<string, unknown>>;
  encoder_settings: {
    video_codec: "libx264" | "h264_nvenc";
    audio_codec: "aac";
    preset: string;
    crf_or_bitrate: string;
    threads: number;
    faststart?: boolean;
  };
  determinism_flags: {
    allow_silent_semantic_fallback: false;
    allow_arbitrary_filtergraph: false;
    require_exact_duration_check: true;
    immutable_once_queued: true;
  };
}

export interface ApprovalTokenBindingV3 {
  token_id: string;
  tenant_id: string;
  project_id: string;
  proposal_id: string;
  timeline_version: number;
  proposal_checksum: string;
  timeline_checksum: string;
  issued_to_user_id: string;
  expires_at: string;
  consumed_at?: string | null;
  single_use: true;
  atomic_transaction_required: true;
}

export interface AgentProposalV3 {
  schema_version: "3.0";
  proposal_id: string;
  tenant_id: string;
  project_id: string;
  timeline_id: string;
  base_version: number;
  base_timeline_checksum: string;
  result_version?: number | null;
  status: ProposalStatusV3;
  agent_permission_level: ProposalPermissionLevel;
  actor: { type: "user" | "agent" | "system"; id: string };
  originating_user_id?: string | null;
  originating_request_id: string;
  user_intent?: string | null;
  rationale: string;
  risk_level: "low" | "medium" | "high" | "critical";
  evidence_refs: string[];
  operations: EditOperationInputV3[];
  diff?: Record<string, unknown>;
  impact_summary: {
    duration_delta_ticks: number;
    clips_affected: number;
    subtitles_affected: number;
    reversible: boolean;
    warnings?: string[];
  };
  proposal_checksum: string;
  expires_at: string;
  approval_token_binding?: ApprovalTokenBindingV3 | null;
}
