"""Real PostgreSQL 15+ psycopg (v3) Tenant-Scoped Persistence Layer.

Enforces database-level multi-tenant isolation on EVERY transaction via:
  1. `SET LOCAL ROLE app_runtime;` (NOBYPASSRLS non-superuser role)
  2. `SELECT set_config('app.tenant_id', %s, true);` (transaction-local RLS context)
  3. Atomic DB-side `consume_approval_token_for_timeline()` for single-use HITL tokens.
"""
from __future__ import annotations

import copy
import hashlib
import os
import secrets
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Iterator
from uuid import uuid4

from services.infrastructure.errors import AppError
from services.infrastructure.store import TenantContext, iso_now, utcnow

# Allowed table whitelist (prevents any dynamic SQL injection on table names)
ALLOWED_TABLES = frozenset({
    "workspaces",
    "projects",
    "timelines",
    "timeline_versions",
    "edit_proposals",
    "human_reviews",
    "approval_tokens",
    "render_plans",
    "artifacts",
    "qa_reports",
    "audit_events",
})


class PostgresTenantStore:
    """Production PostgreSQL 15+ repository using psycopg v3 + FORCE RLS + app_runtime."""

    def __init__(
        self,
        database_url: str,
        media_root: str,
        use_app_runtime_role: bool = True,
    ) -> None:
        self.database_url = database_url
        self.media_root = media_root
        self.use_app_runtime_role = use_app_runtime_role
        self._idempotency: dict[tuple[str, str, str], dict[str, Any]] = {}

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise AppError(
                500,
                "INTERNAL_ERROR",
                "psycopg (v3) is required for PostgresTenantStore; install psycopg[binary]",
            ) from exc
        return psycopg.connect(self.database_url, row_factory=dict_row)

    @staticmethod
    def _jsonb(val: Any):
        from psycopg.types.json import Jsonb
        return Jsonb(val)

    @contextmanager
    def _tenant_tx(self, tenant_id: str) -> Iterator[Any]:
        if not tenant_id:
            raise AppError(500, "INTERNAL_ERROR", "Missing tenant_id for PostgreSQL RLS context")
        with self._connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    if self.use_app_runtime_role:
                        cur.execute("SET LOCAL ROLE app_runtime")
                    cur.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
                    yield cur

    def seed_tenant_and_user(self, tenant_id: str, user_id: str, tenant_name: str = "Tenant", email: str = "user@example.com") -> None:
        """Bootstrap global parent rows (tenants, users) using the migration/owner connection."""
        with self._connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO public.tenants (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                        (tenant_id, tenant_name),
                    )
                    cur.execute(
                        "INSERT INTO public.users (id, auth_subject, email, display_name) VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                        (user_id, f"auth|{user_id}", email, email.split("@")[0]),
                    )

    def get_idempotent(self, tenant_id: str, scope: str, key: str) -> dict[str, Any] | None:
        rec = self._idempotency.get((tenant_id, scope, key))
        return copy.deepcopy(rec) if rec is not None else None

    def save_idempotent(self, tenant_id: str, scope: str, key: str, response: dict[str, Any]) -> None:
        self._idempotency[(tenant_id, scope, key)] = copy.deepcopy(response)

    def put(self, table: str, record: dict[str, Any]) -> dict[str, Any]:
        if table not in ALLOWED_TABLES:
            raise AppError(500, "INTERNAL_ERROR", f"Unsupported PostgreSQL table: {table}")
        tenant_id = record.get("tenant_id")
        if not tenant_id:
            raise AppError(500, "INTERNAL_ERROR", f"Missing tenant_id on {table}")

        with self._tenant_tx(str(tenant_id)) as cur:
            if table == "workspaces":
                cur.execute(
                    """
                    INSERT INTO public.workspaces (id, tenant_id, name, status, hitl_policy_json, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, status = EXCLUDED.status, updated_at = now()
                    """,
                    (
                        record["id"], tenant_id, record["name"], record.get("status", "active"),
                        self._jsonb(record.get("hitl_policy", {})), record.get("created_by"),
                    ),
                )
            elif table == "projects":
                cur.execute(
                    """
                    INSERT INTO public.projects (id, tenant_id, workspace_id, name, description, status, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, description = EXCLUDED.description, status = EXCLUDED.status, updated_at = now()
                    """,
                    (
                        record["id"], tenant_id, record["workspace_id"], record["name"],
                        record.get("description"), record.get("status", "active"), record.get("created_by"),
                    ),
                )
            elif table == "timelines":
                cur.execute(
                    """
                    INSERT INTO public.timelines (id, tenant_id, project_id, name, current_version, profile_json, status, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET current_version = EXCLUDED.current_version, status = EXCLUDED.status, updated_at = now()
                    """,
                    (
                        record["id"], tenant_id, record["project_id"], record.get("name", "Main Timeline"),
                        int(record.get("current_version", 1)), self._jsonb(record.get("profile_json", record.get("profile", {}))),
                        record.get("status", "active"), record.get("created_by"),
                    ),
                )
            elif table == "timeline_versions":
                cur.execute(
                    """
                    INSERT INTO public.timeline_versions (
                      id, tenant_id, timeline_id, version, parent_version, origin_command,
                      restored_from_version, snapshot_json, snapshot_checksum,
                      created_by_type, created_by_id, undo_scope
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        record["id"], tenant_id, record["timeline_id"], int(record["version"]),
                        record.get("parent_version"), record.get("origin_command", "edit"),
                        record.get("restored_from_version"), self._jsonb(record["snapshot_json"]),
                        record["snapshot_checksum"], record.get("created_by_type", "user"),
                        str(record.get("created_by_id", "system")), record.get("undo_scope", "user_edit"),
                    ),
                )
            elif table == "edit_proposals":
                cur.execute(
                    """
                    INSERT INTO public.edit_proposals (
                      id, tenant_id, timeline_id, base_version, result_version, status,
                      actor_type, actor_id, originating_user_id, originating_request_id,
                      user_intent, rationale, risk_level, evidence_refs_json, diff_json,
                      impact_summary_json, proposal_checksum, idempotency_key, agent_permission_level
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, result_version = EXCLUDED.result_version, updated_at = now()
                    """,
                    (
                        record["id"], tenant_id, record["timeline_id"], int(record["base_version"]),
                        record.get("result_version"), record.get("status", "draft"),
                        record["actor"]["type"], record["actor"]["id"],
                        record.get("originating_user_id"), record.get("originating_request_id", str(uuid4())),
                        record.get("user_intent"), record.get("rationale", "proposal"),
                        record.get("risk_level", "medium"), self._jsonb(record.get("evidence_refs", [])),
                        self._jsonb(record.get("diff", {})), self._jsonb(record.get("impact_summary", {})),
                        record["proposal_checksum"], record.get("idempotency_key", f"prop-{record['id']}"),
                        record.get("agent_permission_level", "L1"),
                    ),
                )
            elif table == "human_reviews":
                cur.execute(
                    """
                    INSERT INTO public.human_reviews (
                      id, tenant_id, project_id, subject_type, subject_id, subject_version,
                      subject_checksum, status, decision, reviewer_id, requested_by_type,
                      requested_by_id, comment, selected_items_json, rejected_items_json,
                      idempotency_key, decided_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, decision = EXCLUDED.decision, updated_at = now()
                    """,
                    (
                        record["id"], tenant_id, record["project_id"], record["subject_type"],
                        record["subject_id"], str(record["subject_version"]), record["subject_checksum"],
                        record["status"], record.get("decision"), record.get("reviewer_id"),
                        record.get("requested_by_type", "user"), str(record.get("requested_by_id", "user")),
                        record.get("comment"), self._jsonb(record.get("selected_items", [])),
                        self._jsonb(record.get("rejected_items", [])),
                        record.get("idempotency_key", f"rev-{record['id']}"), record.get("decided_at"),
                    ),
                )
            elif table == "approval_tokens":
                cur.execute(
                    """
                    INSERT INTO public.approval_tokens (
                      id, tenant_id, project_id, human_review_id, subject_type, subject_id,
                      bound_version, bound_checksum, token_hash, issued_to_user_id, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        record["id"], tenant_id, record.get("project_id"), record["human_review_id"],
                        record["subject_type"], record["subject_id"], str(record["bound_version"]),
                        record["bound_checksum"], record["token_hash"], record["issued_to_user_id"],
                        record["expires_at"],
                    ),
                )
            elif table == "render_plans":
                cur.execute(
                    """
                    INSERT INTO public.render_plans (
                      id, tenant_id, project_id, timeline_id, timeline_version,
                      render_mode, renderer, plan_version, plan_json, plan_checksum, created_by
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        record["id"], tenant_id, record["project_id"], record["timeline_id"],
                        int(record["timeline_version"]), record.get("render_mode", "preview"),
                        "ffmpeg", record.get("plan_version", "3.0"), self._jsonb(record["plan_json"]),
                        record["plan_checksum"], record.get("created_by"),
                    ),
                )
            elif table == "artifacts":
                cur.execute(
                    """
                    INSERT INTO public.artifacts (
                      id, tenant_id, project_id, timeline_id, timeline_version, artifact_type,
                      storage_key, checksum_sha256, size_bytes, metadata_json, status, created_by_lease_token
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status
                    """,
                    (
                        record["id"], tenant_id, record["project_id"], record.get("timeline_id"),
                        record.get("timeline_version"), record["artifact_type"], record["storage_key"],
                        record["checksum_sha256"], int(record["size_bytes"]),
                        self._jsonb(record.get("metadata", {})), record.get("status", "ready"),
                        record.get("created_by_lease_token", 1),
                    ),
                )
        return copy.deepcopy(record)

    def get(self, ctx: TenantContext, table: str, record_id: str) -> dict[str, Any]:
        if table not in ALLOWED_TABLES:
            raise AppError(500, "INTERNAL_ERROR", f"Unsupported PostgreSQL table: {table}")
        with self._tenant_tx(ctx.tenant_id) as cur:
            cur.execute(f"SELECT * FROM public.{table} WHERE id = %s", (record_id,))
            row = cur.fetchone()
        if row is None:
            raise AppError(404, "NOT_FOUND", f"{table} resource not found")
        return {k: (str(v) if hasattr(v, "hex") else v) for k, v in dict(row).items()}

    def query(self, ctx: TenantContext, table: str, **filters: Any) -> list[dict[str, Any]]:
        if table not in ALLOWED_TABLES:
            raise AppError(500, "INTERNAL_ERROR", f"Unsupported PostgreSQL table: {table}")
        clauses = []
        params: list[Any] = []
        for col, val in filters.items():
            if not col.isidentifier():
                raise AppError(400, "VALIDATION_ERROR", f"Invalid filter column: {col}")
            clauses.append(f"{col} = %s")
            params.append(val)
        where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._tenant_tx(ctx.tenant_id) as cur:
            cur.execute(f"SELECT * FROM public.{table}{where_sql}", tuple(params))
            rows = cur.fetchall()
        return [{k: (str(v) if hasattr(v, "hex") else v) for k, v in dict(r).items()} for r in rows]

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
        with self._tenant_tx(ctx.tenant_id) as cur:
            cur.execute(
                """
                INSERT INTO public.audit_events (
                  id, tenant_id, actor_type, actor_id, originating_user_id,
                  action, resource_type, resource_id, metadata_json, trace_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(uuid4()), ctx.tenant_id, actor_type, actor_id, ctx.user_id,
                    action, resource_type, str(resource_id), self._jsonb(metadata or {}), ctx.trace_id,
                ),
            )

    def issue_approval_token(
        self,
        ctx: TenantContext,
        human_review_id: str,
        subject_type: str,
        subject_id: str,
        bound_version: str,
        bound_checksum: str,
        ttl_minutes: int = 15,
        project_id: str | None = None,
    ) -> str:
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        rec = {
            "id": str(uuid4()),
            "tenant_id": ctx.tenant_id,
            "project_id": project_id,
            "human_review_id": human_review_id,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "bound_version": bound_version,
            "bound_checksum": bound_checksum,
            "token_hash": token_hash,
            "issued_to_user_id": ctx.user_id,
            "expires_at": (utcnow() + timedelta(minutes=ttl_minutes)).isoformat().replace("+00:00", "Z"),
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
        """Executes PostgreSQL atomic single-use consume_approval_token_for_timeline() inside RLS tx."""
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        with self._tenant_tx(ctx.tenant_id) as cur:
            cur.execute(
                """
                SELECT public.consume_approval_token_for_timeline(%s, %s, %s, %s, %s, %s, %s) AS review_id
                """,
                (
                    ctx.tenant_id, timeline_id, proposal_id, int(expected_base_version),
                    expected_checksum, token_hash, ctx.user_id,
                ),
            )
            row = cur.fetchone()
        review_id = row["review_id"] if row else None
        if not review_id:
            raise AppError(409, "APPROVAL_EXPIRED", "Invalid, expired, wrong-user, stale-version, or already consumed approval token")
        return str(review_id)


def create_tenant_store(media_root: str, database_url: str | None = None):
    """Factory: returns PostgresTenantStore when DATABASE_URL is configured, else InMemoryTenantStore."""
    from services.infrastructure.store import InMemoryTenantStore

    dsn = database_url if database_url is not None else os.environ.get("DATABASE_URL")
    if dsn and dsn.startswith(("postgres://", "postgresql://")):
        return PostgresTenantStore(database_url=dsn, media_root=media_root), "postgresql"
    return InMemoryTenantStore(media_root=media_root), "in_memory"
