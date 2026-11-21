"""Single-source-of-truth contract loader (reads directly from packages/contracts/)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, ValidationError

from services.infrastructure.errors import AppError

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "packages" / "contracts"
SCHEMAS_DIR = CONTRACTS_DIR / "02_schemas"
AGENT_DIR = CONTRACTS_DIR / "04_agent"

_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}
_VALIDATOR_CACHE: dict[str, Draft202012Validator] = {}


def canonical_sha256(obj: Any) -> str:
    encoded = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_schema(filename: str) -> dict[str, Any]:
    if filename not in _SCHEMA_CACHE:
        path = SCHEMAS_DIR / filename
        _SCHEMA_CACHE[filename] = json.loads(path.read_text(encoding="utf-8"))
    return _SCHEMA_CACHE[filename]


def validate_against_schema(filename: str, payload: dict[str, Any], sub_ref: str | None = None) -> None:
    cache_key = f"{filename}::{sub_ref or ''}"
    if cache_key not in _VALIDATOR_CACHE:
        base = load_schema(filename)
        if sub_ref:
            schema = {
                "$schema": base["$schema"],
                "$defs": base.get("$defs", {}),
                "$ref": sub_ref,
            }
        else:
            schema = base
        _VALIDATOR_CACHE[cache_key] = Draft202012Validator(schema)
    try:
        _VALIDATOR_CACHE[cache_key].validate(payload)
    except ValidationError as exc:
        raise AppError(
            status_code=422,
            code="VALIDATION_ERROR",
            message=f"Schema validation failed ({filename}): {exc.message}",
            details={"path": list(exc.path), "schema": filename},
        ) from exc


def load_permission_matrix() -> dict[str, Any]:
    return yaml.safe_load((AGENT_DIR / "permission-matrix.yaml").read_text(encoding="utf-8"))


def load_tool_contracts() -> dict[str, Any]:
    return yaml.safe_load((AGENT_DIR / "tool-contracts.yaml").read_text(encoding="utf-8"))
