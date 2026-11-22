"""Persistent global plugin catalog. Tenant policy remains in PluginRegistry/008."""
from __future__ import annotations

import hmac
import json
import re
from typing import Any

from services.infrastructure.errors import AppError
from services.plugins.plugin_manager import ALLOWED_PLUGIN_LICENSES
from urllib.parse import urlsplit

_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9_]{2,119}$")
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CATEGORIES = {"template", "review", "renderer", "agent_tool", "media_gen", "export"}


def verify_platform_token(expected: str | None, supplied: str | None) -> bool:
    return bool(expected and supplied and hmac.compare_digest(expected.encode(), supplied.encode()))


class PlatformPluginRegistry:
    """Cross-tenant registry using a dedicated PostgreSQL role; no tenant RLS context."""
    def __init__(self, database_url: str):
        self.database_url = database_url

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise AppError(503, "REGISTRY_DRIVER_MISSING", "Install psycopg[binary] for persistent platform registry") from exc
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def _tx(self):
        conn = self._connect()
        conn.execute("SET LOCAL ROLE plugin_registry_runtime")
        return conn

    def list_plugins(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.transaction():
                conn.execute("SET LOCAL ROLE plugin_registry_runtime")
                rows = conn.execute("""
                    SELECT p.*, COALESCE(jsonb_agg(jsonb_build_object(
                      'version', v.version, 'sha256', v.sha256, 'artifact_uri', v.artifact_uri,
                      'created_at', v.created_at) ORDER BY v.created_at DESC)
                      FILTER (WHERE v.version IS NOT NULL), '[]'::jsonb) AS versions
                    FROM public.platform_plugins p LEFT JOIN public.platform_plugin_versions v USING(plugin_id)
                    GROUP BY p.plugin_id ORDER BY p.plugin_id
                """).fetchall()
                return [self._normalize(r) for r in rows]

    @staticmethod
    def _normalize(row: dict[str, Any]) -> dict[str, Any]:
        return {k: (v.isoformat() if hasattr(v, "isoformat") else str(v) if k == "sha256" else v) for k, v in dict(row).items()}

    def register_release(self, *, plugin_id: str, publisher: str, display_name: str,
                         category: str, license_id: str, version: str,
                         manifest: dict[str, Any], artifact_uri: str,
                         artifact_sha256: str, actor: str) -> dict[str, Any]:
        if not _PLUGIN_ID.fullmatch(plugin_id):
            raise AppError(422, "VALIDATION_ERROR", "plugin_id must be lowercase snake_case (3-120 chars)")
        if category not in _CATEGORIES or not _SEMVER.fullmatch(version):
            raise AppError(422, "VALIDATION_ERROR", "Unsupported category or invalid semantic version")
        if not _SHA256.fullmatch(artifact_sha256):
            raise AppError(422, "VALIDATION_ERROR", "artifact_sha256 must be lowercase SHA-256 hex")
        if (not isinstance(manifest, dict) or manifest.get("plugin_id") != plugin_id
                or manifest.get("version") != version or manifest.get("category") != category
                or manifest.get("license") != license_id):
            raise AppError(422, "VALIDATION_ERROR", "Manifest plugin_id/version/category/license must match release identity")
        parsed_uri = urlsplit(artifact_uri)
        if parsed_uri.scheme not in {"s3", "https"} or not parsed_uri.netloc:
            raise AppError(422, "VALIDATION_ERROR", "artifact_uri must be an s3:// or https:// URI")
        with self._connect() as conn:
            with conn.transaction():
                conn.execute("SET LOCAL ROLE plugin_registry_runtime")
                conn.execute("""INSERT INTO public.platform_plugins
                  (plugin_id,publisher,display_name,category,license_id,created_by)
                  VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (plugin_id) DO NOTHING""",
                  (plugin_id,publisher,display_name,category,license_id,actor))
                identity = conn.execute("SELECT publisher,category,license_id FROM public.platform_plugins WHERE plugin_id=%s", (plugin_id,)).fetchone()
                if not identity or (identity["publisher"], identity["category"], identity["license_id"]) != (publisher, category, license_id):
                    raise AppError(409, "PLUGIN_IDENTITY_CONFLICT", "Publisher, category and license identity cannot change after registration")
                existing_release = conn.execute("SELECT sha256 FROM public.platform_plugin_versions WHERE plugin_id=%s AND version=%s", (plugin_id, version)).fetchone()
                if existing_release:
                    raise AppError(409, "PLUGIN_VERSION_IMMUTABLE", "This plugin version already exists; publish a new semantic version")
                conn.execute("""INSERT INTO public.platform_plugin_versions
                  (plugin_id,version,sha256,manifest_json,artifact_uri,created_by)
                  VALUES (%s,%s,%s,%s::jsonb,%s,%s)""",
                  (plugin_id,version,artifact_sha256,json.dumps(manifest,sort_keys=True),artifact_uri,actor))
                conn.execute("""INSERT INTO public.platform_plugin_events
                  (plugin_id,version,event_type,actor,details_json)
                  VALUES (%s,%s,'registered',%s,%s::jsonb)""",
                  (plugin_id,version,actor,json.dumps({"sha256":artifact_sha256})))
        return {"plugin_id": plugin_id, "version": version, "sha256": artifact_sha256,
                "license_status": "license_review_pending", "globally_enabled": False}

    def review_license(self, plugin_id: str, *, approved: bool, actor: str, note: str = "") -> dict[str, Any]:
        if approved:
            with self._connect() as conn:
                with conn.transaction():
                    conn.execute("SET LOCAL ROLE plugin_registry_runtime")
                    license_row = conn.execute("SELECT license_id FROM public.platform_plugins WHERE plugin_id=%s", (plugin_id,)).fetchone()
            if not license_row:
                raise AppError(404, "PLUGIN_NOT_FOUND", f"Unknown platform plugin {plugin_id}")
            if license_row["license_id"] not in ALLOWED_PLUGIN_LICENSES:
                raise AppError(409, "LICENSE_NOT_ALLOWED", "License is outside LIC-001 allowlist and cannot be approved")
        status = "approved" if approved else "rejected"
        event = "license_approved" if approved else "license_rejected"
        with self._connect() as conn:
            with conn.transaction():
                conn.execute("SET LOCAL ROLE plugin_registry_runtime")
                row = conn.execute("""UPDATE public.platform_plugins SET license_status=%s,
                    globally_enabled=CASE WHEN %s THEN globally_enabled ELSE FALSE END,
                    updated_at=clock_timestamp() WHERE plugin_id=%s RETURNING plugin_id,license_status,globally_enabled""",
                    (status, approved, plugin_id)).fetchone()
                if not row:
                    raise AppError(404, "PLUGIN_NOT_FOUND", f"Unknown platform plugin {plugin_id}")
                conn.execute("INSERT INTO public.platform_plugin_events(plugin_id,event_type,actor,details_json) VALUES (%s,%s,%s,%s::jsonb)",
                             (plugin_id,event,actor,json.dumps({"note":note})))
                return dict(row)

    def get_runtime_state(self, plugin_id: str) -> bool | None:
        """Return None for built-ins not governed by the catalog; otherwise fail closed."""
        with self._connect() as conn:
            with conn.transaction():
                conn.execute("SET LOCAL ROLE plugin_registry_runtime")
                row = conn.execute("SELECT license_status,globally_enabled FROM public.platform_plugins WHERE plugin_id=%s", (plugin_id,)).fetchone()
                if not row:
                    return None
                return bool(row["license_status"] == "approved" and row["globally_enabled"])

    def set_enabled(self, plugin_id: str, *, enabled: bool, actor: str) -> dict[str, Any]:
        with self._connect() as conn:
            with conn.transaction():
                conn.execute("SET LOCAL ROLE plugin_registry_runtime")
                row = conn.execute("""UPDATE public.platform_plugins SET globally_enabled=%s,updated_at=clock_timestamp()
                    WHERE plugin_id=%s AND (%s=FALSE OR license_status='approved')
                    RETURNING plugin_id,license_status,globally_enabled""", (enabled,plugin_id,enabled)).fetchone()
                if not row:
                    exists = conn.execute("SELECT 1 FROM public.platform_plugins WHERE plugin_id=%s", (plugin_id,)).fetchone()
                    if not exists:
                        raise AppError(404, "PLUGIN_NOT_FOUND", f"Unknown platform plugin {plugin_id}")
                    raise AppError(409, "LICENSE_NOT_APPROVED", "Plugin must pass license review before global enablement")
                conn.execute("INSERT INTO public.platform_plugin_events(plugin_id,event_type,actor) VALUES (%s,%s,%s)",
                             (plugin_id,"enabled" if enabled else "disabled",actor))
                return dict(row)
