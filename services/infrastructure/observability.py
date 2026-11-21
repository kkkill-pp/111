"""Production Observability, Structured JSON Logging, Prometheus Metrics & Rate Limiting.

Zero external dependencies (pure Python stdlib) so it runs identically in sandbox,
Docker containers, and CI environments.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "password",
        "postgres_password",
        "api_key",
        "x-api-key",
        "secret",
        "approval_token",
        "token",
        "cookie",
        "set-cookie",
    }
)

_UUID_SEGMENT_RE = re.compile(
    r"/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_NUMERIC_SEGMENT_RE = re.compile(r"/\d+(?=/|$)")


def normalize_path_label(path: str) -> str:
    """Collapse UUID and numeric path segments to prevent Prometheus label cardinality explosion."""
    collapsed = _UUID_SEGMENT_RE.sub("/:id", path)
    collapsed = _NUMERIC_SEGMENT_RE.sub("/:id", collapsed)
    return collapsed


def redact_sensitive(obj: Any) -> Any:
    """Recursively redact sensitive keys before emitting structured logs."""
    if isinstance(obj, dict):
        return {
            k: ("[REDACTED]" if str(k).lower() in SENSITIVE_KEYS else redact_sensitive(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact_sensitive(v) for v in obj]
    return obj


class StructuredLogger:
    """Thread-safe JSON structured logger with bounded in-memory ring buffer."""

    def __init__(self, name: str = "aivs.api", max_buffer: int = 500) -> None:
        self._logger = logging.getLogger(name)
        self._lock = threading.Lock()
        self._buffer: deque[dict[str, Any]] = deque(maxlen=max_buffer)

    def log(self, level: str, event: str, **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": level.upper(),
            "event": event,
        }
        record.update(redact_sensitive(fields))
        serialized = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._buffer.append(record)
        py_level = getattr(logging, level.upper(), logging.INFO)
        self._logger.log(py_level, serialized)
        return record

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._buffer)[-limit:]


class MetricsRegistry:
    """Thread-safe Prometheus text exposition metrics registry (no external deps)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._start_time = time.monotonic()
        self._http_requests: dict[tuple[str, str, str], int] = defaultdict(int)
        self._http_duration_ms_sum: dict[tuple[str, str], float] = defaultdict(float)
        self._http_duration_ms_count: dict[tuple[str, str], int] = defaultdict(int)
        self._rate_limit_rejected: dict[str, int] = defaultdict(int)
        self._upload_size_rejected: int = 0

    def observe_http_request(
        self, method: str, path: str, status_code: int, duration_ms: float
    ) -> None:
        norm_path = normalize_path_label(path)
        status_class = f"{int(status_code) // 100}xx"
        with self._lock:
            self._http_requests[(method.upper(), norm_path, status_class)] += 1
            self._http_duration_ms_sum[(method.upper(), norm_path)] += float(duration_ms)
            self._http_duration_ms_count[(method.upper(), norm_path)] += 1

    def inc_rate_limit_rejected(self, tenant_key: str) -> None:
        safe_tenant = (tenant_key or "anonymous")[:64]
        with self._lock:
            self._rate_limit_rejected[safe_tenant] += 1

    def inc_upload_size_rejected(self) -> None:
        with self._lock:
            self._upload_size_rejected += 1

    @staticmethod
    def _escape_label(value: str) -> str:
        return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

    def render_prometheus(self) -> str:
        uptime = max(0.0, time.monotonic() - self._start_time)
        lines: list[str] = [
            "# HELP aivs_uptime_seconds Process uptime in seconds.",
            "# TYPE aivs_uptime_seconds gauge",
            f"aivs_uptime_seconds {uptime:.3f}",
            "# HELP aivs_http_requests_total Total HTTP requests handled by AI Video Studio API.",
            "# TYPE aivs_http_requests_total counter",
        ]
        with self._lock:
            for (method, path, status_class), count in sorted(self._http_requests.items()):
                lines.append(
                    f'aivs_http_requests_total{{method="{self._escape_label(method)}",'
                    f'path="{self._escape_label(path)}",'
                    f'status_class="{self._escape_label(status_class)}"}} {count}'
                )
            lines.extend(
                [
                    "# HELP aivs_http_request_duration_ms_sum Cumulative HTTP request duration in milliseconds.",
                    "# TYPE aivs_http_request_duration_ms_sum counter",
                ]
            )
            for (method, path), total_ms in sorted(self._http_duration_ms_sum.items()):
                lines.append(
                    f'aivs_http_request_duration_ms_sum{{method="{self._escape_label(method)}",'
                    f'path="{self._escape_label(path)}"}} {total_ms:.3f}'
                )
            lines.extend(
                [
                    "# HELP aivs_http_request_duration_ms_count Count of timed HTTP requests.",
                    "# TYPE aivs_http_request_duration_ms_count counter",
                ]
            )
            for (method, path), count in sorted(self._http_duration_ms_count.items()):
                lines.append(
                    f'aivs_http_request_duration_ms_count{{method="{self._escape_label(method)}",'
                    f'path="{self._escape_label(path)}"}} {count}'
                )
            lines.extend(
                [
                    "# HELP aivs_rate_limit_rejected_total Total requests rejected by per-tenant rate limiting.",
                    "# TYPE aivs_rate_limit_rejected_total counter",
                ]
            )
            for tenant_key, count in sorted(self._rate_limit_rejected.items()):
                lines.append(
                    f'aivs_rate_limit_rejected_total{{tenant="{self._escape_label(tenant_key)}"}} {count}'
                )
            lines.extend(
                [
                    "# HELP aivs_upload_size_rejected_total Total requests rejected due to payload size limit.",
                    "# TYPE aivs_upload_size_rejected_total counter",
                    f"aivs_upload_size_rejected_total {self._upload_size_rejected}",
                ]
            )
        return "\n".join(lines) + "\n"


class SlidingWindowRateLimiter:
    """Thread-safe per-tenant sliding window rate limiter."""

    def __init__(self, max_requests: int = 300, window_seconds: float = 60.0) -> None:
        self.max_requests = max(1, int(max_requests))
        self.window_seconds = max(0.1, float(window_seconds))
        self._lock = threading.Lock()
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, now: Optional[float] = None) -> tuple[bool, int, int]:
        """Return (allowed, remaining, retry_after_seconds)."""
        ts = time.monotonic() if now is None else float(now)
        cutoff = ts - self.window_seconds
        with self._lock:
            q = self._buckets[key]
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= self.max_requests:
                oldest = q[0]
                retry_after = max(1, int((oldest + self.window_seconds) - ts + 0.999))
                return False, 0, retry_after
            q.append(ts)
            remaining = max(0, self.max_requests - len(q))
            return True, remaining, 0


def resolve_trace_id(header_value: Optional[str]) -> str:
    val = (header_value or "").strip()
    return val if val else str(uuid4())
