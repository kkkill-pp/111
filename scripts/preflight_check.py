#!/usr/bin/env python3
"""Production Preflight & Readiness Check CLI for AI Video Studio v3.0.

Verifies runtime binaries, CJK fontconfig, license redlines (LIC-001),
frozen manifest SHA-256 integrity, and v3.0 cross-contract compliance.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_cmd(cmd: list[str], cwd: Path = REPO_ROOT) -> tuple[bool, str]:
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=60)
        out = (proc.stdout + "\n" + proc.stderr).strip()
        return proc.returncode == 0, out[-400:]
    except Exception as exc:
        return False, str(exc)


def run_preflight() -> dict:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, severity: str = "blocker") -> None:
        checks.append({"name": name, "status": "pass" if ok else "fail", "severity": severity, "detail": detail})

    # 1. Python version >= 3.10
    py_ok = sys.version_info >= (3, 10)
    add("python_version", py_ok, f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")

    # 2. FFmpeg & FFprobe
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    add("ffmpeg_binary", bool(ffmpeg_path), ffmpeg_path or "missing")
    add("ffprobe_binary", bool(ffprobe_path), ffprobe_path or "missing")

    # 3. Bundled CJK fontconfig
    fc_path = REPO_ROOT / "assets/fonts/openshorts-fontmap.conf"
    add("cjk_fontconfig_present", fc_path.is_file(), str(fc_path.relative_to(REPO_ROOT)))

    # 4. License redline (LIC-001)
    cloud_absent = not (REPO_ROOT / "cloud").exists()
    req_txt = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    no_ultralytics = ("ultralytics" not in req_txt) and ("yolov8" not in req_txt)
    add("license_redline_lic001", cloud_absent and no_ultralytics, "cloud/ absent and ultralytics excluded")

    # 5. Production deployment artifacts
    deploy_files = [
        "infra/docker/Dockerfile.api",
        "infra/docker/Dockerfile.web",
        "infra/nginx/nginx.conf",
        "infra/compose/docker-compose.prod.yml",
        ".env.example",
    ]
    missing_deploy = [f for f in deploy_files if not (REPO_ROOT / f).is_file()]
    add("deployment_artifacts_present", len(missing_deploy) == 0, "all 5 present" if not missing_deploy else f"missing: {missing_deploy}")

    # 6. Frozen Manifest SHA-256 (70/70)
    m_ok, m_out = _run_cmd([sys.executable, "scripts/verify_manifest.py"])
    add("manifest_sha256_integrity", m_ok, m_out.splitlines()[-1] if m_out else "")

    # 7. Cross-Contract Verification (61/61)
    c_ok, c_out = _run_cmd([sys.executable, "packages/contracts/09_tests_and_adr/verify_contracts.py"])
    add("v3_contract_verification", c_ok, c_out.splitlines()[-1] if c_out else "")

    # 8. Plugin platform & external contrib loader check
    try:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from services.plugins import PluginRegistry
        from services.plugins.builtin import (
            AnchoredReviewPlugin,
            MediaGatewayPlugin,
            MotionOverlayRendererPlugin,
            MotionTemplatePlugin,
        )
        from services.plugins.plugin_loader import load_plugins_from_directory

        reg = PluginRegistry()
        for p in (MotionTemplatePlugin(), AnchoredReviewPlugin(), MotionOverlayRendererPlugin(), MediaGatewayPlugin()):
            reg.register(p)
        contrib_dir = REPO_ROOT / "plugins_contrib"
        if contrib_dir.is_dir():
            load_plugins_from_directory(contrib_dir, reg)
        loaded_cnt = len(reg.list_plugins())
        add("plugin_platform_loaded", loaded_cnt >= 4, f"{loaded_cnt} LIC-001-verified plugins loaded")
    except Exception as exc:
        add("plugin_platform_loaded", False, str(exc))

    # 9. Optional env check (advisory only)
    db_set = bool(os.environ.get("DATABASE_URL"))
    add(
        "database_url_configured",
        True,
        "DATABASE_URL set (PostgreSQL mode)" if db_set else "DATABASE_URL unset (in-memory local dev mode)",
        severity="info",
    )

    blockers_failed = [c for c in checks if c["status"] == "fail" and c["severity"] == "blocker"]
    return {
        "ready": len(blockers_failed) == 0,
        "passed": sum(1 for c in checks if c["status"] == "pass"),
        "failed": len(blockers_failed),
        "total": len(checks),
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Video Studio v3.0 Production Preflight Check")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")
    args = parser.parse_args()

    report = run_preflight()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for c in report["checks"]:
            tag = "PASS" if c["status"] == "pass" else "FAIL"
            print(f"[{tag}] {c['name']}: {c['detail']}")
        print(f"PREFLIGHT READY={report['ready']} ({report['passed']}/{report['total']} passed)")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
