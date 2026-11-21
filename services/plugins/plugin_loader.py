"""External Third-Party Plugin Discovery & Dynamic Loader SDK (Phase 5 Stage 3).

Loads drop-in plugin directories containing:
  - plugin.json  (manifest: plugin_id, name, version, category, license, entrypoint)
  - <entrypoint>.py exposing `create_plugin() -> AIVSPlugin`

Security & Governance Guarantees:
  1. Pre-import LIC-001 license allowlist check (forbidden GPL/AGPL code is NEVER imported)
  2. Strict path-traversal guard (entrypoint must stay inside the plugin directory)
  3. Schema validation of plugin.json before loading
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from services.infrastructure.errors import AppError
from services.plugins.plugin_manager import (
    ALLOWED_PLUGIN_CATEGORIES,
    ALLOWED_PLUGIN_LICENSES,
    AIVSPlugin,
    PluginRegistry,
)

REQUIRED_MANIFEST_KEYS = ("plugin_id", "name", "version", "category", "license", "entrypoint")


def load_plugin_package(plugin_dir: str | Path, registry: PluginRegistry) -> dict[str, Any]:
    """Validate plugin.json, enforce LIC-001 pre-import, load entrypoint, and register."""
    root = Path(plugin_dir).resolve()
    manifest_path = root / "plugin.json"
    if not manifest_path.is_file():
        raise AppError(422, "VALIDATION_ERROR", f"Missing plugin.json in {root}")

    try:
        meta = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AppError(422, "VALIDATION_ERROR", f"Invalid JSON in {manifest_path}: {exc}")

    missing = [k for k in REQUIRED_MANIFEST_KEYS if not meta.get(k)]
    if missing:
        raise AppError(422, "VALIDATION_ERROR", f"plugin.json missing required fields: {missing}")

    # 1) Pre-import LIC-001 license enforcement (never execute forbidden-license code)
    lic = str(meta["license"])
    if lic not in ALLOWED_PLUGIN_LICENSES:
        raise AppError(
            422,
            "LICENSE_VIOLATION",
            f"External plugin '{meta['plugin_id']}' license '{lic}' violates LIC-001 allowlist (blocked before import)",
        )

    cat = str(meta["category"])
    if cat not in ALLOWED_PLUGIN_CATEGORIES:
        raise AppError(422, "VALIDATION_ERROR", f"Unsupported plugin category '{cat}'")

    # 2) Strict path-traversal guard for entrypoint ("module.py:factory_fn")
    entry = str(meta["entrypoint"])
    if ":" not in entry:
        raise AppError(422, "VALIDATION_ERROR", "entrypoint must use 'file.py:factory_fn' format")
    rel_file, factory_name = entry.split(":", 1)
    if Path(rel_file).is_absolute() or ".." in Path(rel_file).parts:
        raise AppError(422, "VALIDATION_ERROR", f"Path traversal forbidden in plugin entrypoint: {rel_file}")
    entry_path = (root / rel_file).resolve()
    if root not in entry_path.parents or not entry_path.is_file():
        raise AppError(422, "VALIDATION_ERROR", f"Plugin entrypoint not found inside package: {rel_file}")

    # 3) Isolated module load
    mod_name = f"aivs_ext_plugin_{meta['plugin_id']}"
    spec = importlib.util.spec_from_file_location(mod_name, str(entry_path))
    if spec is None or spec.loader is None:
        raise AppError(500, "PLUGIN_LOAD_ERROR", f"Failed to load spec for {entry_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    factory = getattr(module, factory_name, None)
    if not callable(factory):
        raise AppError(422, "VALIDATION_ERROR", f"Factory '{factory_name}' not callable in {rel_file}")

    plugin_instance = factory()
    if not isinstance(plugin_instance, AIVSPlugin):
        raise AppError(422, "VALIDATION_ERROR", f"Factory '{factory_name}' must return an AIVSPlugin instance")

    return registry.register(plugin_instance)


def load_plugins_from_directory(plugins_root: str | Path, registry: PluginRegistry) -> list[dict[str, Any]]:
    """Scan immediate subdirectories containing plugin.json and load each into registry."""
    root = Path(plugins_root)
    if not root.is_dir():
        return []
    loaded: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "plugin.json").is_file():
            loaded.append(load_plugin_package(child, registry))
    return loaded
