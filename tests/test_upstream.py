"""Retry and TTL cache behavior for external providers."""

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import upstream


class FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class SequenceClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_retries_transient_status_then_returns_success(monkeypatch):
    monkeypatch.setattr(upstream, "random", type("R", (), {"uniform": staticmethod(lambda a, b: 0.0)}))
    client = SequenceClient([FakeResponse(503), FakeResponse(200)])

    response = asyncio.run(upstream.request_with_retry(
        client, "GET", "https://example.test", service="test",
        attempts=3, base_delay=0,
    ))

    assert response.status_code == 200
    assert len(client.calls) == 2


def test_does_not_retry_non_transient_status():
    client = SequenceClient([FakeResponse(404), FakeResponse(200)])

    response = asyncio.run(upstream.request_with_retry(
        client, "GET", "https://example.test", service="test",
        attempts=3, base_delay=0,
    ))

    assert response.status_code == 404
    assert len(client.calls) == 1


def test_retries_timeout_then_succeeds():
    client = SequenceClient([httpx.TimeoutException("slow"), FakeResponse(200)])

    response = asyncio.run(upstream.request_with_retry(
        client, "GET", "https://example.test", service="test",
        attempts=3, base_delay=0,
    ))

    assert response.status_code == 200
    assert len(client.calls) == 2


def test_raises_last_timeout_after_attempts():
    client = SequenceClient([
        httpx.TimeoutException("slow"),
        httpx.TimeoutException("slow"),
    ])

    with pytest.raises(httpx.TimeoutException):
        asyncio.run(upstream.request_with_retry(
            client, "GET", "https://example.test", service="test",
            attempts=2, base_delay=0,
        ))
    assert len(client.calls) == 2


def test_ttl_cache_expires(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr(upstream.time, "monotonic", lambda: clock["now"])
    cache = upstream.TTLCache(ttl=5, max_entries=2)

    cache.set("a", 1)
    assert cache.get("a") == (True, 1)
    clock["now"] = 106.0
    assert cache.get("a") == (False, None)


def test_ttl_cache_evicts_oldest_when_full():
    cache = upstream.TTLCache(ttl=60, max_entries=2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)

    assert cache.get("a") == (False, None)
    assert cache.get("b") == (True, 2)
    assert cache.get("c") == (True, 3)
