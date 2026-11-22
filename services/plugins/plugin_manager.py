"""AI Video Studio v3.0 — Pluggable Platform Kernel (Phase 5 Stage 1).

Provides a clean, license-guarded, hot-toggleable plugin architecture so
domain capabilities (Motion.md templates, anchored review comments, dynamic
overlay renderers, model gateways, etc.) can be installed, enabled, disabled,
or swapped without modifying the core TimelineService or HITL governance layer.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from services.infrastructure.errors import AppError

ALLOWED_PLUGIN_LICENSES = frozenset(
    {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "MPL-2.0", "Proprietary-Internal"}
)
ALLOWED_PLUGIN_CATEGORIES = frozenset(
    {"template", "review", "renderer", "agent_tool", "media_gen", "export"}
)


@dataclass
class PluginManifest:
    plugin_id: str
    name: str
    version: str
    category: str
    license: str
    description: str
    capabilities: list[str] = field(default_factory=list)
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "name": self.name,
            "version": self.version,
            "category": self.category,
            "license": self.license,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "enabled": self.enabled,
        }


class AIVSPlugin:
    """Base class for all pluggable AI Video Studio platform modules."""

    manifest: PluginManifest

    def on_enable(self, registry: "PluginRegistry") -> None:
        """Optional hook called when the plugin is enabled."""

    def on_disable(self, registry: "PluginRegistry") -> None:
        """Optional hook called when the plugin is disabled."""


class PluginRegistry:
    """Thread-safe platform plugin registry with LIC-001 license enforcement and hot-toggle."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._plugins: dict[str, AIVSPlugin] = {}
        # tenant_id -> plugin_id -> {"enabled": bool, "config": dict}
        self._tenant_policies: dict[str, dict[str, dict[str, Any]]] = {}
        # Optional persistent source of truth for third-party global approval/enablement.
        self.platform_state_provider: Any = None

    def register(self, plugin: AIVSPlugin) -> dict[str, Any]:
        m = plugin.manifest
        if m.license not in ALLOWED_PLUGIN_LICENSES:
            raise AppError(
                422,
                "LICENSE_VIOLATION",
                f"Plugin '{m.plugin_id}' license '{m.license}' violates LIC-001 allowlist ({sorted(ALLOWED_PLUGIN_LICENSES)})",
            )
        if m.category not in ALLOWED_PLUGIN_CATEGORIES:
            raise AppError(
                422,
                "VALIDATION_ERROR",
                f"Plugin '{m.plugin_id}' has unsupported category '{m.category}'",
            )
        with self._lock:
            self._plugins[m.plugin_id] = plugin
        if m.enabled:
            plugin.on_enable(self)
        return m.to_dict()

    def unregister(self, plugin_id: str) -> None:
        with self._lock:
            plugin = self._plugins.pop(plugin_id, None)
        if not plugin:
            raise AppError(404, "PLUGIN_NOT_FOUND", f"Plugin '{plugin_id}' is not registered")
        if plugin.manifest.enabled:
            plugin.manifest.enabled = False
            plugin.on_disable(self)

    def enable(self, plugin_id: str) -> dict[str, Any]:
        plugin = self.get_plugin(plugin_id)
        if not plugin.manifest.enabled:
            plugin.manifest.enabled = True
            plugin.on_enable(self)
        return plugin.manifest.to_dict()

    def disable(self, plugin_id: str) -> dict[str, Any]:
        plugin = self.get_plugin(plugin_id)
        if plugin.manifest.enabled:
            plugin.manifest.enabled = False
            plugin.on_disable(self)
        return plugin.manifest.to_dict()

    def get_plugin(self, plugin_id: str) -> AIVSPlugin:
        with self._lock:
            plugin = self._plugins.get(plugin_id)
        if not plugin:
            raise AppError(404, "PLUGIN_NOT_FOUND", f"Plugin '{plugin_id}' is not registered")
        return plugin

    def _is_globally_enabled(self, plugin: AIVSPlugin) -> bool:
        """Resolve catalog state on every use so all API instances observe revocation promptly."""
        state = None
        if self.platform_state_provider is not None:
            state = self.platform_state_provider.get_runtime_state(plugin.manifest.plugin_id)
        if state is None:
            return bool(plugin.manifest.enabled)
        with self._lock:
            changed = plugin.manifest.enabled != bool(state)
            plugin.manifest.enabled = bool(state)
        if changed:
            (plugin.on_enable if state else plugin.on_disable)(self)
        return bool(state)

    def require_enabled(self, plugin_id: str) -> AIVSPlugin:
        """Fail loudly (503) if plugin is globally disabled — zero silent fallback (ADR-004)."""
        plugin = self.get_plugin(plugin_id)
        if not self._is_globally_enabled(plugin):
            raise AppError(
                503,
                "PLUGIN_DISABLED",
                f"Plugin '{plugin_id}' is currently disabled by platform policy",
            )
        return plugin

    def list_plugins(self, category: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._plugins.values())
        if category:
            items = [p for p in items if p.manifest.category == category]
        result = []
        for plugin in sorted(items, key=lambda p: p.manifest.plugin_id):
            item = plugin.manifest.to_dict()
            item["enabled"] = self._is_globally_enabled(plugin)
            result.append(item)
        return result

    def set_tenant_policy(
        self,
        tenant_id: str,
        plugin_id: str,
        enabled: Optional[bool] = None,
        config: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        plugin = self.get_plugin(plugin_id)
        global_enabled = self._is_globally_enabled(plugin)
        with self._lock:
            tmap = self._tenant_policies.setdefault(tenant_id, {})
            cur = dict(tmap.get(plugin_id) or {"enabled": True, "config": {}})
            if enabled is not None:
                cur["enabled"] = bool(enabled)
            if config is not None:
                cur["config"] = dict(config)
            tmap[plugin_id] = cur
        effective = bool(global_enabled and cur["enabled"])
        return {
            **plugin.manifest.to_dict(),
            "global_enabled": global_enabled,
            "tenant_enabled": cur["enabled"],
            "effective_enabled": effective,
            "tenant_config": dict(cur["config"]),
        }

    def get_tenant_policy(self, tenant_id: str, plugin_id: str) -> dict[str, Any]:
        plugin = self.get_plugin(plugin_id)
        with self._lock:
            cur = dict(self._tenant_policies.get(tenant_id, {}).get(plugin_id) or {"enabled": True, "config": {}})
        global_enabled = self._is_globally_enabled(plugin)
        effective = bool(global_enabled and cur["enabled"])
        return {
            **plugin.manifest.to_dict(),
            "global_enabled": global_enabled,
            "tenant_enabled": cur["enabled"],
            "effective_enabled": effective,
            "tenant_config": dict(cur["config"]),
        }

    def require_enabled_for_tenant(self, plugin_id: str, tenant_id: str) -> tuple[AIVSPlugin, dict[str, Any]]:
        plugin = self.require_enabled(plugin_id)
        with self._lock:
            cur = dict(self._tenant_policies.get(tenant_id, {}).get(plugin_id) or {"enabled": True, "config": {}})
        if not cur["enabled"]:
            raise AppError(
                403,
                "PLUGIN_DISABLED_FOR_TENANT",
                f"Plugin '{plugin_id}' is disabled by tenant policy",
            )
        return plugin, dict(cur["config"])

    def list_plugins_for_tenant(self, tenant_id: str, category: Optional[str] = None) -> list[dict[str, Any]]:
        base = self.list_plugins(category=category)
        return [self.get_tenant_policy(tenant_id, item["plugin_id"]) for item in base]
