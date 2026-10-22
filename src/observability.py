"""Structured logging, request metrics and Prometheus-style exposition.

The module is intentionally dependency-free so it can be used both by the
FastAPI application and by standalone maintenance scripts.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from collections import defaultdict
from contextvars import ContextVar
from typing import Any

APP_VERSION = "4.2.1"
_STARTED_AT = time.time()
_REQUEST_ID: ContextVar[str | None] = ContextVar("albion_request_id", default=None)
_CONFIGURED = False
_LOCK = threading.Lock()

# {method: {normalized_path: {status: count}}}
_HTTP_REQUESTS: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
_HTTP_DURATION: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
_UPSTREAM_REQUESTS: dict[tuple[str, str], int] = defaultdict(int)
_UPSTREAM_DURATION: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
_UPSTREAM_RETRIES: dict[str, int] = defaultdict(int)
_CACHE_EVENTS: dict[tuple[str, str], int] = defaultdict(int)
_AUDIT_EVENTS: dict[str, int] = defaultdict(int)

_NUMERIC_PATH = re.compile(r"/\d+(?=/|$)")
_UUID_PATH = re.compile(r"/[0-9a-fA-F]{16,}(?=/|$)")
_SAFE_LABEL = re.compile(r"[^a-zA-Z0-9_.:/-{}]")


def configure_logging() -> None:
    """Configure root logging once, using JSON by default for journald."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    level_name = os.getenv("ALBION_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = os.getenv("ALBION_LOG_FORMAT", "json").strip().lower()
    handler = logging.StreamHandler(sys.stderr)
    if fmt in {"json", "structured"}:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    _CONFIGURED = True


class _JsonFormatter(logging.Formatter):
    _EXTRA_FIELDS = (
        "event", "request_id", "method", "path", "status", "duration_ms",
        "user_id", "service", "outcome", "retries", "cache_hit", "action",
        "actor", "target", "detail",
    )

    def format(self, record: logging.LogRecord) -> str:
        created = time.gmtime(record.created)
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", created)
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in self._EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def get_logger(name: str = "albion") -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)


def log_event(level: int, event: str, message: str = "", **fields: Any) -> None:
    """Write one structured event. Never raises for serialization problems."""
    logger = get_logger("albion.event")
    extra = {"event": event, **fields}
    if extra.get("request_id") is None:
        extra["request_id"] = current_request_id()
    try:
        logger.log(level, message or event, extra=extra)
    except Exception:
        pass


def normalize_path(path: str) -> str:
    path = path or "/"
    path = _UUID_PATH.sub("/{id}", path)
    path = _NUMERIC_PATH.sub("/{id}", path)
    return path[:200]


def record_http(method: str, path: str, status: int, duration: float,
                user_id: Any | None = None) -> None:
    method = (method or "GET").upper()[:16]
    normalized = normalize_path(path)
    status_label = str(int(status))
    with _LOCK:
        _HTTP_REQUESTS[method][normalized][status_label] += 1
        bucket = _HTTP_DURATION[(method, normalized)]
        bucket[0] += max(0.0, duration)
        bucket[1] += 1.0


def record_upstream(service: str, outcome: str, duration: float | None = None,
                    retries: int = 0, cache_hit: bool = False,
                    status: int | None = None, detail: str | None = None) -> None:
    service = service[:40]
    outcome = outcome[:40]
    with _LOCK:
        _UPSTREAM_REQUESTS[(service, outcome)] += 1
        if duration is not None:
            bucket = _UPSTREAM_DURATION[service]
            bucket[0] += max(0.0, duration)
            bucket[1] += 1.0
        if retries:
            _UPSTREAM_RETRIES[service] += retries
        _CACHE_EVENTS[(service, "hit" if cache_hit else "miss")] += 1
    log_event(
        logging.WARNING if outcome in {"error", "timeout", "http_error"} else logging.INFO,
        "upstream_request",
        "Upstream request",
        service=service,
        outcome=outcome,
        duration_ms=round(duration * 1000.0, 2) if duration is not None else None,
        retries=retries,
        cache_hit=cache_hit,
        status=status,
        detail=detail,
    )


def record_cache(service: str, hit: bool) -> None:
    with _LOCK:
        _CACHE_EVENTS[(service, "hit" if hit else "miss")] += 1


def record_audit_event(action: str) -> None:
    with _LOCK:
        _AUDIT_EVENTS[action[:80]] += 1


def reset_metrics() -> None:
    """Used by tests; production metrics live for the process lifetime."""
    with _LOCK:
        _HTTP_REQUESTS.clear()
        _HTTP_DURATION.clear()
        _UPSTREAM_REQUESTS.clear()
        _UPSTREAM_DURATION.clear()
        _UPSTREAM_RETRIES.clear()
        _CACHE_EVENTS.clear()
        _AUDIT_EVENTS.clear()


def _label(value: str) -> str:
    return _SAFE_LABEL.sub("_", str(value))[:120]


def render_metrics() -> str:
    lines: list[str] = []
    with _LOCK:
        lines.append("# HELP albion_info Build information")
        lines.append("# TYPE albion_info gauge")
        lines.append(f'albion_info{{version="{_label(APP_VERSION)}"}} 1')
        lines.append("# HELP albion_start_time_seconds Process start time")
        lines.append("# TYPE albion_start_time_seconds gauge")
        lines.append(f"albion_start_time_seconds {_STARTED_AT:.3f}")

        lines.append("# HELP albion_http_requests_total HTTP requests by method, path and status")
        lines.append("# TYPE albion_http_requests_total counter")
        for method in sorted(_HTTP_REQUESTS):
            for path in sorted(_HTTP_REQUESTS[method]):
                for status in sorted(_HTTP_REQUESTS[method][path]):
                    count = _HTTP_REQUESTS[method][path][status]
                    lines.append(
                        'albion_http_requests_total{method="%s",path="%s",status="%s"} %d'
                        % (_label(method), _label(path), _label(status), count)
                    )

        lines.append("# HELP albion_http_request_duration_seconds_sum Total request time")
        lines.append("# TYPE albion_http_request_duration_seconds_sum counter")
        lines.append("# HELP albion_http_request_duration_seconds_count Request count")
        lines.append("# TYPE albion_http_request_duration_seconds_count counter")
        for (method, path), (total, count) in sorted(_HTTP_DURATION.items()):
            labels = f'method="{_label(method)}",path="{_label(path)}"'
            lines.append(f"albion_http_request_duration_seconds_sum{{{labels}}} {total:.6f}")
            lines.append(f"albion_http_request_duration_seconds_count{{{labels}}} {count:.0f}")

        lines.append("# HELP albion_upstream_requests_total External requests by service and outcome")
        lines.append("# TYPE albion_upstream_requests_total counter")
        for (service, outcome), count in sorted(_UPSTREAM_REQUESTS.items()):
            lines.append(
                'albion_upstream_requests_total{service="%s",outcome="%s"} %d'
                % (_label(service), _label(outcome), count)
            )

        lines.append("# HELP albion_upstream_retries_total Retried external requests")
        lines.append("# TYPE albion_upstream_retries_total counter")
        for service, count in sorted(_UPSTREAM_RETRIES.items()):
            lines.append(f'albion_upstream_retries_total{{service="{_label(service)}"}} {count}')

        lines.append("# HELP albion_upstream_duration_seconds_sum External request time")
        lines.append("# TYPE albion_upstream_duration_seconds_sum counter")
        for service, (total, _count) in sorted(_UPSTREAM_DURATION.items()):
            lines.append(f'albion_upstream_duration_seconds_sum{{service="{_label(service)}"}} {total:.6f}')

        lines.append("# HELP albion_cache_events_total Cache hits and misses")
        lines.append("# TYPE albion_cache_events_total counter")
        for (service, event), count in sorted(_CACHE_EVENTS.items()):
            lines.append(
                f'albion_cache_events_total{{service="{_label(service)}",event="{_label(event)}"}} {count}'
            )

        lines.append("# HELP albion_audit_events_total Audit events by action")
        lines.append("# TYPE albion_audit_events_total counter")
        for action, count in sorted(_AUDIT_EVENTS.items()):
            lines.append(f'albion_audit_events_total{{action="{_label(action)}"}} {count}')
    return "\n".join(lines) + "\n"


class ObservabilityMiddleware:
    """Add request id, structured access log and metrics to every HTTP call."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        request_id = (headers.get(b"x-request-id") or b"").decode("latin-1").strip()[:128]
        if not request_id:
            request_id = uuid.uuid4().hex
        token = _REQUEST_ID.set(request_id)
        started = time.perf_counter()
        status_code = 500

        async def send_with_request_id(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message.get("status", 500))
                raw_headers = list(message.get("headers") or [])
                if not any(key.lower() == b"x-request-id" for key, _ in raw_headers):
                    raw_headers.append((b"x-request-id", request_id.encode("latin-1")))
                    message["headers"] = raw_headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            duration = time.perf_counter() - started
            user = (scope.get("state") or {}).get("user") or {}
            try:
                record_http(
                    scope.get("method", "GET"),
                    scope.get("path", "/"),
                    status_code,
                    duration,
                    user.get("id") if isinstance(user, dict) else None,
                )
                log_event(
                    logging.INFO, "request",
                    "request",
                    request_id=request_id,
                    method=scope.get("method", "GET"),
                    path=normalize_path(scope.get("path", "/")),
                    status=status_code,
                    duration_ms=round(duration * 1000.0, 2),
                    user_id=user.get("id") if isinstance(user, dict) else None,
                )
            finally:
                _REQUEST_ID.reset(token)


def current_request_id() -> str | None:
    return _REQUEST_ID.get()
