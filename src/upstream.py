"""Shared retry/cache helpers for external HTTP providers."""
from __future__ import annotations

import asyncio
import random
import time
from email.utils import parsedate_to_datetime
from typing import Any, Hashable

import httpx

import observability

RETRY_STATUSES = {429, 500, 502, 503, 504}


class TTLCache:
    """Small bounded TTL cache suitable for single-process async use."""

    def __init__(self, ttl: float, max_entries: int = 256):
        self.ttl = max(0.0, float(ttl))
        self.max_entries = max(1, int(max_entries))
        self._items: dict[Hashable, tuple[float, Any]] = {}

    def get(self, key: Hashable) -> tuple[bool, Any]:
        item = self._items.get(key)
        if item is None:
            return False, None
        expires_at, value = item
        if expires_at <= time.monotonic():
            self._items.pop(key, None)
            return False, None
        return True, value

    def set(self, key: Hashable, value: Any) -> None:
        if len(self._items) >= self.max_entries and key not in self._items:
            oldest = min(self._items, key=lambda item: self._items[item][0])
            self._items.pop(oldest, None)
        self._items[key] = (time.monotonic() + self.ttl, value)

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        return max(0.0, when.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


def _delay(attempt: int, base_delay: float, max_delay: float,
           retry_after: float | None = None) -> float:
    if retry_after is not None:
        return min(max_delay, retry_after)
    # Full jitter keeps several workers from retrying in lockstep.
    ceiling = min(max_delay, base_delay * (2 ** (attempt - 1)))
    return random.uniform(0.0, max(0.0, ceiling))


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    service: str,
    attempts: int = 3,
    base_delay: float = 0.35,
    max_delay: float = 3.0,
    **kwargs: Any,
) -> httpx.Response:
    """Perform an HTTP request with bounded retries for transient failures.

    The response is returned even when it is a retryable status on the final
    attempt; callers keep their existing status handling. Network/timeouts are
    raised after the last attempt.
    """
    attempts = max(1, int(attempts))
    started = time.monotonic()
    retries = 0
    last_error: Exception | None = None
    last_response: httpx.Response | None = None

    for attempt in range(1, attempts + 1):
        try:
            request = getattr(client, "request", None)
            if request is not None:
                response = await request(method, url, **kwargs)
            else:  # compatibility with small test doubles and legacy clients
                response = await getattr(client, method.lower())(url, **kwargs)
            last_response = response
            status_code = int(getattr(response, "status_code", 200) or 200)
            if status_code not in RETRY_STATUSES or attempt >= attempts:
                outcome = "success" if status_code < 400 else "http_error"
                observability.record_upstream(
                    service, outcome,
                    duration=time.monotonic() - started,
                    retries=retries, status=status_code,
                )
                return response
            headers = getattr(response, "headers", {}) or {}
            retry_after = _retry_after_seconds(headers.get("retry-after"))
            retries += 1
            observability.record_upstream(
                service, "retry",
                duration=time.monotonic() - started,
                retries=retries, status=status_code,
            )
            await asyncio.sleep(_delay(attempt, base_delay, max_delay, retry_after))
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            if attempt >= attempts:
                outcome = "timeout" if isinstance(exc, httpx.TimeoutException) else "error"
                observability.record_upstream(
                    service, outcome,
                    duration=time.monotonic() - started,
                    retries=retries, detail=type(exc).__name__,
                )
                raise
            retries += 1
            observability.record_upstream(
                service, "retry",
                duration=time.monotonic() - started,
                retries=retries, detail=type(exc).__name__,
            )
            await asyncio.sleep(_delay(attempt, base_delay, max_delay))

    if last_response is not None:  # pragma: no cover - loop always returns/raises
        return last_response
    raise last_error or RuntimeError("upstream request failed")
