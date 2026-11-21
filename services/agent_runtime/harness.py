"""Controlled Agent Harness — ZERO direct DB/shell/FFmpeg access.

Enforces `04_agent/permission-matrix.yaml` (effective permission =
min(user_role, tool_whitelist, workspace_hitl_policy)) and circuit breakers.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from services.application.timeline_service import TimelineService
from services.agent_runtime.llm_provider import LLMProvider, create_llm_provider
from services.infrastructure.contracts import load_permission_matrix, load_tool_contracts, validate_against_schema
from services.infrastructure.errors import AppError
from services.infrastructure.store import TenantContext
from services.media_engine.visual_frame_inspector import VisualFrameInspector


IRREVERSIBLE_ACTIONS = frozenset({"export_replace", "delete_media", "external_delivery", "enable_cloud_model", "enable_remote_mcp"})


class AgentHarness:
    def __init__(self, timeline_service: TimelineService, llm_provider: LLMProvider | None = None) -> None:
        self._timeline_service = timeline_service
        self._matrix = load_permission_matrix()
        self._tool_contracts = load_tool_contracts()
        self._llm_provider: LLMProvider = llm_provider or create_llm_provider()

    def set_provider(self, provider: LLMProvider) -> None:
        self._llm_provider = provider

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
        planned = self._llm_provider.plan_edit_operations(tl, user_intent)
        if not planned.operations:
            raise AppError(422, "VALIDATION_ERROR", "Planner produced zero operations for timeline")

        # Strict anti-hallucination gate: EVERY LLM-emitted operation MUST pass EditOperation JSON Schema
        for op in planned.operations:
            validate_against_schema("edit-operation.schema.json", op, sub_ref="#/$defs/operation_input")

        return self._timeline_service.create_proposal(
            ctx=ctx,
            timeline_id=timeline_id,
            base_version=base_version,
            operations=planned.operations,
            actor_type="agent",
            actor_id=f"agent:{planned.provider_name}:{planned.model_id}",
            rationale=planned.rationale,
            evidence_refs=[f"timeline:{timeline_id}@v{base_version}", *planned.evidence_refs],
            risk_level=planned.risk_level,
            idempotency_key=idempotency_key,
            user_intent=user_intent,
        )
