"""Asynchronous Render Job Manager with Lease-Token Fencing Guard (Phase 4 Stage 4).

Executes real RenderPlan compilation, FFmpeg rendering, and QA inspection in a
bounded background thread pool while enforcing:
  - Strict tenant isolation (cross-tenant access returns 404)
  - Monotonic lease_token fencing (canceled/superseded jobs reject stale writes)
  - Full lifecycle tracking: queued -> running -> succeeded | failed | canceled
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

from services.infrastructure.errors import AppError
from services.infrastructure.store import TenantContext, iso_now


class RenderJobManager:
    def __init__(self, max_workers: int = 2) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="aivs-render")
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def submit_job(
        self,
        ctx: TenantContext,
        timeline_id: str,
        timeline_version: int,
        kind: str,
        runner: Callable[[], dict[str, Any]],
        auto_start: bool = True,
    ) -> dict[str, Any]:
        job_id = str(uuid4())
        job: dict[str, Any] = {
            "job_id": job_id,
            "tenant_id": ctx.tenant_id,
            "timeline_id": timeline_id,
            "timeline_version": timeline_version,
            "kind": kind,
            "status": "queued",
            "lease_token": 1,
            "progress_pct": 0,
            "artifact_id": None,
            "render_plan_id": None,
            "qa_report_id": None,
            "error": None,
            "created_at": iso_now(),
            "updated_at": iso_now(),
        }
        with self._lock:
            self._jobs[job_id] = job
            snapshot = self._public_view(job)

        if auto_start:
            self._executor.submit(self._run_worker, job_id, 1, runner)
        return snapshot

    def _run_worker(self, job_id: str, expected_lease_token: int, runner: Callable[[], dict[str, Any]]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job["status"] != "queued" or job["lease_token"] != expected_lease_token:
                return
            job["status"] = "running"
            job["progress_pct"] = 25
            job["updated_at"] = iso_now()

        try:
            result = runner()
        except Exception as exc:
            with self._lock:
                job = self._jobs.get(job_id)
                if job and job["lease_token"] == expected_lease_token and job["status"] == "running":
                    job["status"] = "failed"
                    job["error"] = str(exc)
                    job["updated_at"] = iso_now()
            return

        # Fencing check before committing result: if canceled while running, reject stale completion
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job["lease_token"] != expected_lease_token or job["status"] != "running":
                return
            job["status"] = "succeeded"
            job["progress_pct"] = 100
            job["artifact_id"] = result.get("artifact_id")
            job["render_plan_id"] = result.get("render_plan_id")
            job["qa_report_id"] = result.get("qa_report_id")
            job["updated_at"] = iso_now()

    def get_job(self, ctx: TenantContext, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job["tenant_id"] != ctx.tenant_id:
                raise AppError(404, "NOT_FOUND", f"Render job {job_id} not found")
            return self._public_view(job)

    def cancel_job(self, ctx: TenantContext, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job["tenant_id"] != ctx.tenant_id:
                raise AppError(404, "NOT_FOUND", f"Render job {job_id} not found")
            if job["status"] in ("succeeded", "failed"):
                raise AppError(409, "INVALID_STATE", f"Cannot cancel render job in terminal state '{job['status']}'")
            job["status"] = "canceled"
            job["lease_token"] += 1  # Fencing bump invalidates any in-flight worker commit
            job["updated_at"] = iso_now()
            return self._public_view(job)

    def try_commit_with_fencing(self, job_id: str, lease_token: int, result: dict[str, Any]) -> bool:
        """Explicit fencing commit helper for deterministic unit testing of stale workers."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job["lease_token"] != lease_token or job["status"] not in ("queued", "running"):
                return False
            job["status"] = "succeeded"
            job["progress_pct"] = 100
            job["artifact_id"] = result.get("artifact_id")
            job["updated_at"] = iso_now()
            return True

    @staticmethod
    def _public_view(job: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in job.items() if k != "tenant_id"}
