"""Tenant-scoped storage layer mirroring HITL v2.2 PostgreSQL schema.

Production uses PostgreSQL 15+ with `SET LOCAL app.tenant_id` and `FORCE RLS`
(see `migrations/000`–`006`). For sandbox unit/E2E verification without a local
PostgreSQL daemon, `InMemoryTenantStore` enforces the exact same tenant scope,
idempotency keys, optimistic version locking, and single-use ApprovalToken rules.
"""
from __future__ import annotations

import copy
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from services.infrastructure.errors import AppError


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    user_id: str
    role: str
    trace_id: str


ROLE_ORDER = {"viewer": 10, "reviewer": 20, "editor": 30, "admin": 40, "owner": 50}


def require_role(ctx: TenantContext, allowed_roles: list[str]) -> None:
    if ctx.role not in allowed_roles:
        raise AppError(
            status_code=403,
            code="FORBIDDEN",
            message=f"Role '{ctx.role}' is not permitted; requires one of {allowed_roles}",
        )


class InMemoryTenantStore:
    """Fail-closed tenant-partitioned repository for sandbox & unit testing."""

    def __init__(self, media_root: str) -> None:
        self.media_root = media_root
        self._tables: dict[str, dict[str, dict[str, Any]]] = {
            name: {}
            for name in [
                "workspaces", "projects", "upload_sessions", "assets", "media_probes",
                "analysis_runs", "creative_brief_versions", "storyboards", "storyboard_versions",
                "timelines", "timeline_versions", "edit_proposals", "edit_operations",
                "human_reviews", "approval_tokens", "render_plans", "render_jobs",
                "artifacts", "qa_reports", "deliveries", "tasks",
            ]
        }
        self.audit_events: list[dict[str, Any]] = []
        self._idempotency: dict[tuple[str, str, str], dict[str, Any]] = {}

    def get_idempotent(self, tenant_id: str, scope: str, key: str) -> dict[str, Any] | None:
        rec = self._idempotency.get((tenant_id, scope, key))
        return copy.deepcopy(rec) if rec is not None else None

    def save_idempotent(self, tenant_id: str, scope: str, key: str, response: dict[str, Any]) -> None:
        self._idempotency[(tenant_id, scope, key)] = copy.deepcopy(response)

    def put(self, table: str, record: dict[str, Any]) -> dict[str, Any]:
        if "tenant_id" not in record or not record["tenant_id"]:
            raise AppError(500, "INTERNAL_ERROR", f"Missing tenant_id on {table}")
        self._tables[table][record["id"]] = copy.deepcopy(record)
        return copy.deepcopy(record)

    def get(self, ctx: TenantContext, table: str, record_id: str) -> dict[str, Any]:
        rec = self._tables[table].get(record_id)
        if rec is None or rec.get("tenant_id") != ctx.tenant_id:
            # Never leak cross-tenant resource existence
            raise AppError(404, "NOT_FOUND", f"{table} resource not found")
        return copy.deepcopy(rec)

    def query(self, ctx: TenantContext, table: str, **filters: Any) -> list[dict[str, Any]]:
        results = []
        for rec in self._tables[table].values():
            if rec.get("tenant_id") != ctx.tenant_id:
                continue
            if all(rec.get(k) == v for k, v in filters.items()):
                results.append(copy.deepcopy(rec))
        return results

    def add_audit(
        self,
        ctx: TenantContext,
        actor_type: str,
        actor_id: str,
        action: str,
        resource_type: str,
        resource_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.audit_events.append({
            "id": str(uuid4()),
            "tenant_id": ctx.tenant_id,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "originating_user_id": ctx.user_id,
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "metadata": metadata or {},
            "trace_id": ctx.trace_id,
            "created_at": iso_now(),
        })

    def issue_approval_token(
        self,
        ctx: TenantContext,
        human_review_id: str,
        subject_type: str,
        subject_id: str,
        bound_version: str,
        bound_checksum: str,
        ttl_minutes: int = 15,
    ) -> str:
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        rec = {
            "id": str(uuid4()),
            "tenant_id": ctx.tenant_id,
            "human_review_id": human_review_id,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "bound_version": bound_version,
            "bound_checksum": bound_checksum,
            "token_hash": token_hash,
            "issued_to_user_id": ctx.user_id,
            "expires_at": (utcnow() + timedelta(minutes=ttl_minutes)).isoformat().replace("+00:00", "Z"),
            "consumed_at": None,
            "created_at": iso_now(),
        }
        self.put("approval_tokens", rec)
        return raw_token

    def consume_approval_token(
        self,
        ctx: TenantContext,
        timeline_id: str,
        proposal_id: str,
        expected_base_version: int,
        expected_checksum: str,
        raw_token: str,
    ) -> str:
        """Mirrors PostgreSQL consume_approval_token_for_timeline() atomically."""
        tl = self.get(ctx, "timelines", timeline_id)
        if tl["current_version"] != expected_base_version:
            raise AppError(
                409,
                "VERSION_CONFLICT",
                f"Timeline advanced to v{tl['current_version']}; proposal targets v{expected_base_version}",
            )

        prop = self.get(ctx, "edit_proposals", proposal_id)
        if prop["status"] != "approved" or prop["proposal_checksum"] != expected_checksum:
            raise AppError(409, "APPROVAL_EXPIRED", "Proposal is not approved or checksum mismatched")

        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        matched = None
        for rec in self._tables["approval_tokens"].values():
            if (
                rec["tenant_id"] == ctx.tenant_id
                and rec["token_hash"] == token_hash
                and rec["subject_type"] == "edit_proposal"
                and rec["subject_id"] == proposal_id
                and rec["bound_version"] == str(expected_base_version)
                and rec["bound_checksum"] == expected_checksum
                and rec["issued_to_user_id"] == ctx.user_id
                and rec["consumed_at"] is None
            ):
                exp = datetime.fromisoformat(rec["expires_at"].replace("Z", "+00:00"))
                if exp > utcnow():
                    matched = rec
                    break

        if matched is None:
            raise AppError(409, "APPROVAL_EXPIRED", "Invalid, expired, wrong-user, or already consumed approval token")

        review = self.get(ctx, "human_reviews", matched["human_review_id"])
        if review["status"] not in ("approved", "approved_with_warnings"):
            raise AppError(409, "APPROVAL_REQUIRED", "Linked HumanReview is not approved")

        matched["consumed_at"] = iso_now()
        return matched["human_review_id"]
