"""PostgreSQL 15+ Fencing-Token Worker Runtime (Phase 2).

Calls the database-enforced six-dimensional fencing functions from migrations/004 & 005:
  - claim_next_task()
  - heartbeat_task_with_fencing()
  - complete_task_with_fencing()
  - fail_task_with_fencing()
  - publish_artifact_with_fencing()

Prevents split-brain writes from stale or expired workers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class FencingRejectionError(RuntimeError):
    """Raised when PostgreSQL rejects a stale or expired worker lease_token."""


@dataclass(frozen=True)
class ClaimedTask:
    task_id: str
    tenant_id: str
    task_type: str
    payload_json: dict[str, Any]
    lease_token: int


class FencingWorker:
    def __init__(self, database_url: str, worker_id: str, lease_seconds: int = 120) -> None:
        if not (5 <= lease_seconds <= 3600):
            raise ValueError("lease_seconds must be between 5 and 3600 (enforced by 004_tasks_outbox_workers.sql)")
        self.database_url = database_url
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def claim_next(self) -> ClaimedTask | None:
        with self._connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT * FROM public.claim_next_task(%s, %s)",
                        (self.worker_id, self.lease_seconds),
                    )
                    row = cur.fetchone()
        if not row or not row.get("task_id"):
            return None
        return ClaimedTask(
            task_id=str(row["task_id"]),
            tenant_id=str(row["tenant_id"]),
            task_type=str(row["task_type"]),
            payload_json=dict(row.get("payload_json") or {}),
            lease_token=int(row["lease_token"]),
        )

    def heartbeat(self, task: ClaimedTask) -> bool:
        with self._connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL ROLE app_runtime")
                    cur.execute("SELECT set_config('app.tenant_id', %s, true)", (task.tenant_id,))
                    cur.execute(
                        "SELECT public.heartbeat_task_with_fencing(%s, %s, %s, %s, %s) AS ok",
                        (task.tenant_id, task.task_id, self.worker_id, task.lease_token, self.lease_seconds),
                    )
                    row = cur.fetchone()
        return bool(row and row["ok"])

    def complete(self, task: ClaimedTask, result_json: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb
        with self._connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL ROLE app_runtime")
                    cur.execute("SELECT set_config('app.tenant_id', %s, true)", (task.tenant_id,))
                    cur.execute(
                        "SELECT public.complete_task_with_fencing(%s, %s, %s, %s, %s) AS ok",
                        (task.tenant_id, task.task_id, self.worker_id, task.lease_token, Jsonb(result_json)),
                    )
                    row = cur.fetchone()
        if not (row and row["ok"]):
            raise FencingRejectionError(f"Stale/expired lease_token={task.lease_token} rejected on complete for task {task.task_id}")

    def fail(self, task: ClaimedTask, error_message: str, retryable: bool = True) -> bool:
        with self._connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL ROLE app_runtime")
                    cur.execute("SELECT set_config('app.tenant_id', %s, true)", (task.tenant_id,))
                    cur.execute(
                        "SELECT public.fail_task_with_fencing(%s, %s, %s, %s, %s, %s) AS ok",
                        (task.tenant_id, task.task_id, self.worker_id, task.lease_token, error_message, retryable),
                    )
                    row = cur.fetchone()
        return bool(row and row["ok"])

    def execute_once(self, handler: Callable[[ClaimedTask], dict[str, Any]]) -> bool:
        """Claims one task, runs handler, and commits result with fencing guard."""
        task = self.claim_next()
        if task is None:
            return False
        try:
            res = handler(task)
            self.complete(task, res)
            return True
        except FencingRejectionError:
            raise
        except Exception as exc:
            self.fail(task, str(exc), retryable=True)
            return True
