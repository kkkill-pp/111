"""Controlled HITL MCP Server (JSON-RPC 2.0 over stdio & HTTP — Phase 2 Stage 4).

Inspired by Cutlass `crates/agent/src/mcp_server.rs` and braincut `src/lib/agent/protocol.ts`,
strictly enforcing AI Video Studio v3.0 security boundaries:
  - ZERO direct DB / shell / FFmpeg access
  - ZERO autonomous timeline mutation (proposals MUST pass human approval + single-use ApprovalToken)
  - Enforces TenantContext + L0-L3 permissions on every `tools/call`
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from services.agent_runtime.harness import AgentHarness
from services.application.timeline_service import TimelineService
from services.infrastructure.errors import AppError
from services.infrastructure.store import TenantContext

MCP_TOOLS_CATALOG: list[dict[str, Any]] = [
    {
        "name": "get_timeline",
        "description": "Read-only (L0+): Fetch an immutable timeline snapshot by timeline_id and optional version.",
        "inputSchema": {
            "type": "object",
            "required": ["timeline_id"],
            "properties": {
                "timeline_id": {"type": "string", "format": "uuid"},
                "version": {"type": "integer", "minimum": 1},
            },
        },
    },
    {
        "name": "list_timeline_versions",
        "description": "Read-only (L0+): List immutable version history of a timeline.",
        "inputSchema": {
            "type": "object",
            "required": ["timeline_id"],
            "properties": {"timeline_id": {"type": "string", "format": "uuid"}},
        },
    },
    {
        "name": "validate_operations",
        "description": "Read-only dry-run (L0+): Simulate canonical EditOperations against base_version and return diff/impact without writing.",
        "inputSchema": {
            "type": "object",
            "required": ["timeline_id", "base_version", "operations"],
            "properties": {
                "timeline_id": {"type": "string", "format": "uuid"},
                "base_version": {"type": "integer", "minimum": 1},
                "operations": {"type": "array", "minItems": 1},
            },
        },
    },
    {
        "name": "propose_edit_operations",
        "description": "HITL Proposal (L1+): Generate an explainable EditProposal awaiting human approval; NEVER mutates timeline directly.",
        "inputSchema": {
            "type": "object",
            "required": ["timeline_id", "base_version", "user_intent"],
            "properties": {
                "timeline_id": {"type": "string", "format": "uuid"},
                "base_version": {"type": "integer", "minimum": 1},
                "user_intent": {"type": "string", "minLength": 1},
                "permission_level": {"type": "string", "enum": ["L0", "L1", "L2", "L3"], "default": "L1"},
            },
        },
    },
    {
        "name": "inspect_rendered_frames",
        "description": "Read-only visual self-check (L0+): Sample rendered artifact frames to verify luma, black-frame absence, and PNG snapshots.",
        "inputSchema": {
            "type": "object",
            "required": ["artifact_id"],
            "properties": {"artifact_id": {"type": "string", "format": "uuid"}},
        },
    },
]


class AIVSMCPServer:
    """Tenant-scoped JSON-RPC 2.0 MCP Server routing strictly through TimelineService & AgentHarness."""

    def __init__(
        self,
        timeline_service: TimelineService,
        agent_harness: AgentHarness,
        store: Any,
        plugin_registry: Any = None,
    ) -> None:
        self._timeline = timeline_service
        self._agent = agent_harness
        self._store = store
        self._plugins = plugin_registry

    def _dynamic_plugin_tools(self, ctx: TenantContext) -> list[dict[str, Any]]:
        if self._plugins is None:
            return []
        enabled_ids = {
            p["plugin_id"] for p in self._plugins.list_plugins_for_tenant(ctx.tenant_id)
            if p["effective_enabled"]
        }
        tools: list[dict[str, Any]] = [
            {
                "name": "aivs_list_plugins",
                "description": "Read-only (L0+): List installed platform plugins, licenses, and enabled state.",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ]
        if "motion_template_plugin" in enabled_ids:
            tools.append(
                {
                    "name": "aivs_compile_motion_md",
                    "description": "Read-only compiler (L0+): Parse Motion.md + BrandKit into candidate v3.0 EditOperations[] without mutating timeline.",
                    "inputSchema": {
                        "type": "object",
                        "required": ["motion_md"],
                        "properties": {
                            "motion_md": {"type": "string"},
                            "brand_kit": {"type": "object"},
                            "subtitle_track_id": {"type": "string"},
                            "target_clip_id": {"type": "string"},
                        },
                    },
                }
            )
        if "anchored_review_plugin" in enabled_ids:
            tools.append(
                {
                    "name": "aivs_list_review_comments",
                    "description": "Read-only (L0+): List timecode-anchored human review comments on a timeline.",
                    "inputSchema": {
                        "type": "object",
                        "required": ["timeline_id"],
                        "properties": {"timeline_id": {"type": "string", "format": "uuid"}},
                    },
                }
            )
        return tools

    def handle_rpc(self, ctx: TenantContext, request: dict[str, Any]) -> dict[str, Any]:
        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "ai-video-studio-mcp", "version": "3.0.0"},
                    "capabilities": {"tools": {"listChanged": False}},
                },
            }

        if method == "tools/list":
            tools = list(MCP_TOOLS_CATALOG)
            if params.get("include_plugins"):
                tools.extend(self._dynamic_plugin_tools(ctx))
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": tools},
            }

        if method == "tools/call":
            tool_name = params.get("name")
            args = params.get("arguments") or {}
            try:
                output = self._dispatch_tool(ctx, tool_name, args)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}],
                        "structuredContent": output,
                        "isError": False,
                    },
                }
            except AppError as exc:
                code = -32003 if exc.status_code in (403, 503) else (-32022 if exc.status_code == 422 else -32000)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": code, "message": exc.message, "data": {"http_status": exc.status_code, "error_code": exc.code}},
                }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    def _dispatch_tool(self, ctx: TenantContext, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "get_timeline":
            self._agent.check_tool_permission(ctx, "get_timeline")
            return self._timeline.get_timeline(ctx, args["timeline_id"], version=args.get("version"))
        if tool_name == "list_timeline_versions":
            self._agent.check_tool_permission(ctx, "get_timeline")
            return self._timeline.list_versions(ctx, args["timeline_id"])
        if tool_name == "validate_operations":
            self._agent.check_tool_permission(ctx, "get_timeline")
            return self._timeline.validate_operations(ctx, args["timeline_id"], int(args["base_version"]), args["operations"])
        if tool_name == "propose_edit_operations":
            idem = str(args.get("idempotency_key") or f"mcp-prop-{uuid4().hex}")
            return self._agent.propose_edit_operations(
                ctx=ctx,
                timeline_id=args["timeline_id"],
                base_version=int(args["base_version"]),
                user_intent=str(args["user_intent"]),
                idempotency_key=idem,
                permission_level=str(args.get("permission_level", "L1")),
            )
        if tool_name == "inspect_rendered_frames":
            art = self._store.get(ctx, "artifacts", args["artifact_id"])
            snap_dir = str(Path(art["storage_key"]).parent / f"mcp_vis_{args['artifact_id'][:8]}")
            return self._agent.inspect_rendered_frames(ctx, art["storage_key"], save_dir=snap_dir)
        if self._plugins is not None:
            if tool_name == "aivs_list_plugins":
                return {"plugins": self._plugins.list_plugins_for_tenant(ctx.tenant_id)}
            if tool_name == "aivs_compile_motion_md":
                plugin, _ = self._plugins.require_enabled_for_tenant("motion_template_plugin", ctx.tenant_id)
                return plugin.compile_to_operations(
                    args["motion_md"],
                    brand_kit=args.get("brand_kit"),
                    subtitle_track_id=args.get("subtitle_track_id"),
                    target_clip_id=args.get("target_clip_id"),
                )
            if tool_name == "aivs_list_review_comments":
                self._store.get(ctx, "timelines", args["timeline_id"])
                plugin, _ = self._plugins.require_enabled_for_tenant("anchored_review_plugin", ctx.tenant_id)
                return {"comments": plugin.list_comments(ctx, args["timeline_id"])}
        raise AppError(403, "FORBIDDEN", f"Unknown or disallowed MCP tool: {tool_name}")


def run_stdio_server(ctx: TenantContext, server: AIVSMCPServer) -> None:
    """Line-delimited JSON-RPC 2.0 stdio loop for Claude/Cursor/Codex MCP clients."""
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        req = json.loads(line)
        resp = server.handle_rpc(ctx, req)
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()
