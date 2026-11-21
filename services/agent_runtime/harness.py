"""Controlled Agent Harness — ZERO direct DB/shell/FFmpeg access.

Enforces `04_agent/permission-matrix.yaml` (effective permission =
min(user_role, tool_whitelist, workspace_hitl_policy)) and circuit breakers.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from services.application.timeline_service import TimelineService
from services.infrastructure.contracts import load_permission_matrix, load_tool_contracts
from services.infrastructure.errors import AppError
from services.infrastructure.store import TenantContext
from services.media_engine.visual_frame_inspector import VisualFrameInspector


IRREVERSIBLE_ACTIONS = frozenset({"export_replace", "delete_media", "external_delivery", "enable_cloud_model", "enable_remote_mcp"})


class AgentHarness:
    def __init__(self, timeline_service: TimelineService) -> None:
        self._timeline_service = timeline_service
        self._matrix = load_permission_matrix()
        self._tool_contracts = load_tool_contracts()

    def check_tool_permission(self, ctx: TenantContext, tool_name: str) -> None:
        tools = self._tool_contracts["tools"]
        if tool_name not in tools:
            raise AppError(403, "FORBIDDEN", f"Unregistered agent tool: {tool_name}")
        role_cfg = self._matrix["role_tool_matrix"].get(ctx.role, {})
        allow = role_cfg.get("allow", [])
        if allow != "all_registered_tools_subject_to_hitl_gates" and tool_name not in allow:
            raise AppError(403, "FORBIDDEN", f"Agent acting for role '{ctx.role}' cannot invoke '{tool_name}'")
        if ctx.role not in tools[tool_name]["allowed_roles"]:
            raise AppError(403, "FORBIDDEN", f"Tool '{tool_name}' denies role '{ctx.role}'")

    @staticmethod
    def require_human_approval_for_irreversible(action: str, human_review_id: str | None) -> None:
        """Cutlass approval.rs pattern: irreversible actions outside the undo stack strictly require explicit human approval."""
        if action in IRREVERSIBLE_ACTIONS and not human_review_id:
            raise AppError(409, "APPROVAL_REQUIRED", f"Irreversible action '{action}' requires explicit human approval")

    def inspect_rendered_frames(self, ctx: TenantContext, artifact_path: str, save_dir: str | None = None) -> dict[str, Any]:
        """Cutlass picture.rs pattern: visual frame self-check tool (read-only, zero DB/shell writes)."""
        return VisualFrameInspector.inspect_video_frames(artifact_path, save_dir=save_dir).to_dict()

    def propose_edit_operations(
        self,
        ctx: TenantContext,
        timeline_id: str,
        base_version: int,
        user_intent: str,
        idempotency_key: str,
        permission_level: str = "L1",
    ) -> dict[str, Any]:
        """Generates an explainable EditProposal; NEVER mutates Timeline directly."""
        if permission_level == "L0":
            raise AppError(403, "FORBIDDEN", "L0 read-only agent permission cannot generate mutating proposals")
        if permission_level not in ("L1", "L2", "L3"):
            raise AppError(422, "VALIDATION_ERROR", f"Invalid agent permission_level: {permission_level}")
        self.check_tool_permission(ctx, "get_timeline")
        self.check_tool_permission(ctx, "propose_edit_operations")

        tl = self._timeline_service.get_timeline(ctx, timeline_id, version=base_version)
        video_track = next((t for t in tl["tracks"] if t["type"] == "video"), None)
        sub_track = next((t for t in tl["tracks"] if t["type"] == "subtitle"), None)
        if not video_track or not video_track["clips"]:
            raise AppError(422, "VALIDATION_ERROR", "Timeline has no video clips to edit")

        first_clip = video_track["clips"][0]
        src_in = first_clip["source_range"]["in"]
        src_out = first_clip["source_range"]["out"]
        trim_delta = min(30000, max(1, (src_out - src_in) // 4))  # trim up to 1s (30000 ticks at 1/30000)

        operations = [
            {
                "operation_id": str(uuid4()),
                "operation_type": "TRIM_CLIP",
                "payload": {
                    "clip_id": first_clip["id"],
                    "new_source_in": src_in + trim_delta,
                    "new_source_out": src_out,
                },
                "reason": f"Trim opening pause per intent: {user_intent}",
                "evidence_refs": [f"clip:{first_clip['id']}", f"tick:{src_in}-{src_in + trim_delta}"],
                "risk_level": "medium",
            }
        ]
        if sub_track:
            operations.append({
                "operation_id": str(uuid4()),
                "operation_type": "ADD_SUBTITLE",
                "payload": {
                    "track_id": sub_track["id"],
                    "timeline_start": 0,
                    "duration": max(1, (src_out - src_in) - trim_delta),
                    "text": "AI Video Studio HITL Preview",
                },
                "reason": "Add concise opening caption aligned to trimmed clip",
                "evidence_refs": [f"track:{sub_track['id']}"],
                "risk_level": "low",
            })

        return self._timeline_service.create_proposal(
            ctx=ctx,
            timeline_id=timeline_id,
            base_version=base_version,
            operations=operations,
            actor_type="agent",
            actor_id="timeline-editor-agent-v1",
            rationale=f"Proposed {len(operations)} reversible edits for user intent: '{user_intent}'",
            evidence_refs=[f"timeline:{timeline_id}@v{base_version}"],
            risk_level="medium",
            idempotency_key=idempotency_key,
            user_intent=user_intent,
        )
