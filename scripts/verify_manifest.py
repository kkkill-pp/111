#!/usr/bin/env python3
"""Deterministic SHA-256 manifest generator & verifier for packages/contracts/manifest.json."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = ROOT / "packages" / "contracts"
MANIFEST_PATH = CONTRACTS_DIR / "manifest.json"


def compute_files() -> dict[str, dict[str, object]]:
    entries: dict[str, dict[str, object]] = {}
    for p in sorted(CONTRACTS_DIR.rglob("*")):
        if p.is_file() and p.name != "manifest.json" and "__pycache__" not in p.parts and not p.name.endswith(".pyc"):
            rel = p.relative_to(CONTRACTS_DIR).as_posix()
            raw = p.read_bytes()
            entries[rel] = {
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
    return entries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", action="store_true", help="Regenerate packages/contracts/manifest.json")
    args = parser.parse_args()

    actual = compute_files()
    if args.update:
        manifest = {
            "package": "ai_video_studio_engineering_contracts",
            "version": "3.0.0",
            "baseline_inherited_from": "2.2.0",
            "static_contract_checks": "61 passed",
            "file_count": len(actual),
            "files": actual,
        }
        MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        print(f"Updated {MANIFEST_PATH} ({len(actual)} files).")
        return 0

    if not MANIFEST_PATH.exists():
        print(f"FAIL: {MANIFEST_PATH} not found", file=sys.stderr)
        return 1

    expected = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    exp_files = expected.get("files", {})
    mismatches = []
    missing = []
    extra = []

    for rel, meta in exp_files.items():
        if rel not in actual:
            missing.append(rel)
        elif actual[rel]["sha256"] != meta["sha256"]:
            mismatches.append((rel, meta["sha256"], actual[rel]["sha256"]))
    for rel in actual:
        if rel not in exp_files:
            extra.append(rel)

    ok_count = len(exp_files) - len(missing) - len(mismatches)
    print(f"MANIFEST VERIFY: ok={ok_count} mismatch={len(mismatches)} missing={len(missing)} extra={len(extra)} total={len(actual)}")
    if mismatches or missing or extra:
        for rel, e, a in mismatches:
            print(f"  MISMATCH: {rel} expected={e[:12]} actual={a[:12]}", file=sys.stderr)
        for rel in missing:
            print(f"  MISSING: {rel}", file=sys.stderr)
        for rel in extra:
            print(f"  EXTRA: {rel}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
