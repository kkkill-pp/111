#!/usr/bin/env python3
"""Deterministic TypeScript type generator from frozen AI Video Studio v3.0 JSON Schemas.

Zero network dependencies; reads packages/contracts/02_schemas/*.v3.schema.json
and emits packages/shared-types/src/v3-contracts.ts for the React + TypeScript editor.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "packages/contracts/02_schemas"
OUT_FILE = ROOT / "packages/shared-types/src/v3-contracts.ts"

V3_FILES = [
    "timeline.v3.schema.json",
    "edit-operation.v3.schema.json",
    "render-plan.v3.schema.json",
    "agent-proposal.v3.schema.json",
]


def ts_union(values: list[str | int]) -> str:
    return " | ".join(json.dumps(v) for v in values)


def generate() -> str:
    loaded = {}
    hashes = {}
    for fn in V3_FILES:
        raw = (SCHEMA_DIR / fn).read_bytes()
        hashes[fn] = hashlib.sha256(raw).hexdigest()
        loaded[fn] = json.loads(raw.decode("utf-8"))

    tl = loaded["timeline.v3.schema.json"]
    op = loaded["edit-operation.v3.schema.json"]
    rp = loaded["render-plan.v3.schema.json"]
    pr = loaded["agent-proposal.v3.schema.json"]

    op_types = op["$defs"]["operation_type"]["enum"]
    perm_levels = op["$defs"]["agent_permission_level"]["enum"]
    undo_scopes = op["$defs"]["undo_scope"]["enum"]
    prop_statuses = pr["properties"]["status"]["enum"]
    prop_perm_levels = pr["properties"]["agent_permission_level"]["enum"]
    kf_props = tl["$defs"]["keyframe"]["properties"]["property"]["enum"]
    kf_easings = tl["$defs"]["keyframe"]["properties"]["easing"]["enum"]
    eff_types = tl["$defs"]["effect"]["properties"]["effect_type"]["enum"]
    marker_kinds = tl["$defs"]["marker"]["properties"]["kind"]["enum"]
    audio_roles = tl["$defs"]["audio_item"]["properties"]["role"]["enum"]

    header_hashes = "\n".join(f" * - {fn}: sha256:{hashes[fn]}" for fn in V3_FILES)

    return f"""/* eslint-disable */
/**
 * AUTO-GENERATED FROM FROZEN AI VIDEO STUDIO v3.0 JSON SCHEMAS (Draft 2020-12).
 * DO NOT EDIT MANUALLY. Regenerate via: python3 scripts/generate_ts_types.py
 *
 * Source Schema SHA-256 Digests:
{header_hashes}
 */

export interface Rational {{
  num: number;
  den: number;
}}

export interface TimeSemanticsV3 {{
  unit: "ticks";
  timeline_timebase: Rational;
  frame_rate: Rational;
  rounding_policy: "half_to_even";
  coordinate_model: "half_open_source_and_project_ticks";
}}

export interface SourceRangeV3 {{
  in: number;
  out: number;
  timebase: Rational;
  vfr_pts_map_ref?: string | null;
}}

export type KeyframePropertyV3 = {ts_union(kf_props)};
export type KeyframeEasingV3 = {ts_union(kf_easings)};
export type EffectTypeV3 = {ts_union(eff_types)};
export type MarkerKindV3 = {ts_union(marker_kinds)};
export type AudioRoleV3 = {ts_union(audio_roles)};
export type UndoScopeV3 = {ts_union(undo_scopes)};
export type AgentPermissionLevel = {ts_union(perm_levels)};
export type ProposalPermissionLevel = {ts_union(prop_perm_levels)};
export type ProposalStatusV3 = {ts_union(prop_statuses)};

export type EditOperationTypeV3 =
{chr(10).join(f'  | "{t}"' for t in op_types)};

export interface KeyframeV3 {{
  id: string;
  property: KeyframePropertyV3;
  timeline_tick: number;
  value: number;
  easing: KeyframeEasingV3;
}}

export interface EffectV3 {{
  id: string;
  effect_type: EffectTypeV3;
  enabled: boolean;
  params: Record<string, unknown>;
}}

export interface MarkerV3 {{
  id: string;
  timeline_tick: number;
  label: string;
  kind: MarkerKindV3;
  color?: string;
  metadata?: Record<string, unknown>;
}}

export interface VideoClipV3 {{
  id: string;
  asset_id: string;
  source_range: SourceRangeV3;
  timeline_start: number;
  duration: number;
  playback_rate: number;
  transform?: {{
    mode: "fit" | "fill" | "manual" | "face_track" | "split" | "screencast" | "wide" | "inset" | "general";
    scale?: number;
    position_x?: number;
    position_y?: number;
    rotation_deg?: number;
    opacity?: number;
    crop_override_json?: Record<string, unknown> | null;
  }};
  audio?: {{
    enabled?: boolean;
    volume?: number;
    fade_in_ticks?: number;
    fade_out_ticks?: number;
  }};
  transitions?: Array<{{
    id: string;
    type: "cut" | "fade_in" | "fade_out" | "crossfade";
    duration_ticks: number;
    position: "in" | "out" | "between";
  }}>;
  keyframes?: KeyframeV3[];
  effects?: EffectV3[];
}}

export interface AudioItemV3 {{
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
}}

export interface SubtitleItemV3 {{
  id: string;
  timeline_start: number;
  duration: number;
  text: string;
  speaker_label?: string | null;
  style_preset?: string;
  safe_zone?: "bottom" | "center_seam" | "top";
  word_timings?: Array<{{ word: string; start_tick: number; end_tick: number }}>;
}}

export interface OverlayItemV3 {{
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
}}

export type TrackV3 =
  | {{ id: string; type: "video"; name?: string; order: number; muted?: boolean; locked?: boolean; clips: VideoClipV3[] }}
  | {{ id: string; type: "audio"; name?: string; order: number; muted?: boolean; locked?: boolean; clips: AudioItemV3[] }}
  | {{ id: string; type: "subtitle"; name?: string; order: number; locked?: boolean; items: SubtitleItemV3[] }}
  | {{ id: string; type: "overlay"; name?: string; order: number; locked?: boolean; items: OverlayItemV3[] }};

export interface TimelineV3 {{
  schema_version: "3.0";
  timeline_id: string;
  project_id: string;
  version: number;
  parent_version?: number | null;
  origin_command?: "init" | "storyboard_compile" | "edit" | "proposal_apply" | "undo" | "redo";
  undo_scope?: UndoScopeV3;
  snapshot_checksum?: string | null;
  time_semantics: TimeSemanticsV3;
  profile: {{
    width: number;
    height: number;
    frame_rate: Rational;
    timeline_timebase: Rational;
    audio_sample_rate: 44100 | 48000;
    pixel_aspect_ratio?: Rational;
    aspect_preset?: "9:16" | "16:9" | "1:1" | "custom";
  }};
  tracks: TrackV3[];
  markers: MarkerV3[];
  metadata?: Record<string, unknown>;
}}

export interface EditOperationInputV3 {{
  operation_id: string;
  operation_type: EditOperationTypeV3;
  payload: Record<string, unknown>;
  reason?: string;
  evidence_refs?: string[];
  risk_level?: "low" | "medium" | "high" | "critical";
  reversible?: boolean;
}}

export interface EditOperationV3 extends EditOperationInputV3 {{
  schema_version: "3.0";
  proposal_id?: string | null;
  timeline_id: string;
  base_version: number;
  result_version?: number | null;
  actor: {{
    type: "user" | "agent" | "system";
    id: string;
    permission_level?: AgentPermissionLevel;
  }};
  undo_scope: UndoScopeV3;
  originating_user_id?: string | null;
  originating_request_id: string;
  idempotency_key: string;
}}

export interface RenderPlanV3 {{
  render_plan_version: "3.0";
  plan_id: string;
  render_mode: "preview" | "final";
  renderer: "ffmpeg";
  timeline_id: string;
  timeline_version: number;
  timeline_checksum: string;
  plan_checksum: string;
  immutability_lock: {{
    locked: true;
    locked_at: string;
    source_timeline_version: number;
    source_timeline_checksum: string;
  }};
  time_semantics: {{
    unit: "ticks";
    timeline_timebase: Rational;
    frame_rate: Rational;
    rounding_policy: "half_to_even";
  }};
  render_range_ticks?: {{ start_tick: number; end_tick: number }} | null;
  expected_duration_ticks: number;
  input_assets: Array<{{
    asset_id: string;
    storage_key: string;
    checksum_sha256: string;
    is_vfr: boolean;
    pts_map_storage_key?: string | null;
  }}>;
  output_profile: {{
    width: number;
    height: number;
    frame_rate: Rational;
    pixel_format: "yuv420p";
    audio_sample_rate: 44100 | 48000;
    audio_channels: 1 | 2;
  }};
  video_ops: Array<Record<string, unknown>>;
  audio_ops: Array<Record<string, unknown>>;
  subtitle_ops: Array<Record<string, unknown>>;
  overlay_ops: Array<Record<string, unknown>>;
  effect_ops: Array<Record<string, unknown>>;
  encoder_settings: {{
    video_codec: "libx264" | "h264_nvenc";
    audio_codec: "aac";
    preset: string;
    crf_or_bitrate: string;
    threads: number;
    faststart?: boolean;
  }};
  determinism_flags: {{
    allow_silent_semantic_fallback: false;
    allow_arbitrary_filtergraph: false;
    require_exact_duration_check: true;
    immutable_once_queued: true;
  }};
}}

export interface ApprovalTokenBindingV3 {{
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
}}

export interface AgentProposalV3 {{
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
  actor: {{ type: "user" | "agent" | "system"; id: string }};
  originating_user_id?: string | null;
  originating_request_id: string;
  user_intent?: string | null;
  rationale: string;
  risk_level: "low" | "medium" | "high" | "critical";
  evidence_refs: string[];
  operations: EditOperationInputV3[];
  diff?: Record<string, unknown>;
  impact_summary: {{
    duration_delta_ticks: number;
    clips_affected: number;
    subtitles_affected: number;
    reversible: boolean;
    warnings?: string[];
  }};
  proposal_checksum: string;
  expires_at: string;
  approval_token_binding?: ApprovalTokenBindingV3 | null;
}}
"""


def main() -> None:
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    content = generate()
    OUT_FILE.write_text(content, encoding="utf-8", newline="\n")
    print(f"Generated {OUT_FILE} ({len(content)} bytes) from 4 frozen v3.0 JSON Schemas.")


if __name__ == "__main__":
    main()
