"""Built-in Plugin 2: Timecode-Anchored Video Review Comments.

Inspired by OpenMotion's anchored review annotations (Apache-2.0).
Stores tenant-scoped timecode-anchored review comments on timeline versions
and compiles open comments into targeted AgentHarness revision intents.
"""
from __future__ import annotations

import threading
from typing import Any, Optional
from uuid import uuid4

from services.infrastructure.errors import AppError
from services.infrastructure.store import TenantContext, iso_now
from services.plugins.plugin_manager import AIVSPlugin, PluginManifest


class AnchoredReviewPlugin(AIVSPlugin):
    PLUGIN_ID = "anchored_review_plugin"

    def __init__(self) -> None:
        self.manifest = PluginManifest(
            plugin_id=self.PLUGIN_ID,
            name="Timecode-Anchored Review Comments",
            version="1.0.0",
            category="review",
            license="Apache-2.0",
            description="Attaches timecode-anchored human review comments to timeline versions and converts them into targeted HITL agent intents.",
            capabilities=["review.anchored_comment", "review.to_agent_intent"],
            enabled=True,
        )
        self._lock = threading.Lock()
        self._comments: dict[str, dict[str, Any]] = {}

    def add_comment(
        self,
        ctx: TenantContext,
        timeline_id: str,
        timeline_version: int,
        at_tick: int,
        comment: str,
        suggested_action: Optional[str] = None,
    ) -> dict[str, Any]:
        if at_tick < 0:
            raise AppError(422, "VALIDATION_ERROR", "at_tick must be >= 0")
        if not comment or not comment.strip():
            raise AppError(422, "VALIDATION_ERROR", "comment cannot be empty")
        cid = str(uuid4())
        rec = {
            "comment_id": cid,
            "tenant_id": ctx.tenant_id,
            "timeline_id": timeline_id,
            "timeline_version": int(timeline_version),
            "at_tick": int(at_tick),
            "author_id": ctx.user_id,
            "comment": comment.strip(),
            "suggested_action": suggested_action,
            "status": "open",
            "created_at": iso_now(),
            "resolved_at": None,
        }
        with self._lock:
            self._comments[cid] = rec
        return self._public_view(rec)

    def list_comments(self, ctx: TenantContext, timeline_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                self._public_view(r)
                for r in self._comments.values()
                if r["tenant_id"] == ctx.tenant_id and r["timeline_id"] == timeline_id
            ]
        return sorted(rows, key=lambda r: (r["at_tick"], r["created_at"]))

    def resolve_comment(self, ctx: TenantContext, comment_id: str) -> dict[str, Any]:
        with self._lock:
            rec = self._comments.get(comment_id)
            if not rec or rec["tenant_id"] != ctx.tenant_id:
                raise AppError(404, "NOT_FOUND", f"Review comment {comment_id} not found")
            rec["status"] = "resolved"
            rec["resolved_at"] = iso_now()
            return self._public_view(rec)

    def compile_comment_to_agent_intent(
        self, ctx: TenantContext, comment_id: str, ticks_per_second: int = 30000
    ) -> dict[str, Any]:
        with self._lock:
            rec = self._comments.get(comment_id)
            if not rec or rec["tenant_id"] != ctx.tenant_id:
                raise AppError(404, "NOT_FOUND", f"Review comment {comment_id} not found")
            sec = round(rec["at_tick"] / float(ticks_per_second), 3)
            intent = f"[At {sec}s / tick {rec['at_tick']}] {rec['comment']}"
            if rec.get("suggested_action"):
                intent += f" (action: {rec['suggested_action']})"
            return {
                "comment_id": rec["comment_id"],
                "timeline_id": rec["timeline_id"],
                "timeline_version": rec["timeline_version"],
                "at_tick": rec["at_tick"],
                "at_sec": sec,
                "agent_intent": intent,
            }

    @staticmethod
    def _public_view(rec: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in rec.items() if k != "tenant_id"}
