"""Structured metrics and request tracing."""

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main
import observability


def test_normalize_path_replaces_identifiers():
    assert observability.normalize_path("/api/users/42/password") == "/api/users/{id}/password"
    assert observability.normalize_path("/api/bookmarks/deadbeefdeadbeef") == "/api/bookmarks/{id}"


def test_render_metrics_contains_http_and_upstream_counters():
    observability.reset_metrics()
    observability.record_http("GET", "/api/users/7", 200, 0.125, user_id=1)
    observability.record_upstream("wfs", "success", duration=0.25, retries=1)
    observability.record_cache("wfs", True)
    observability.record_audit_event("login")

    text = observability.render_metrics()
    assert 'albion_http_requests_total{method="GET",path="/api/users/{id}",status="200"} 1' in text
    assert 'albion_upstream_requests_total{service="wfs",outcome="success"} 1' in text
    assert 'albion_upstream_retries_total{service="wfs"} 1' in text
    assert 'albion_cache_events_total{service="wfs",event="hit"} 1' in text
    assert 'albion_audit_events_total{action="login"} 1' in text


def test_metrics_endpoint_is_loopback_only_and_can_require_token(monkeypatch):
    client = TestClient(main.app)
    observability.reset_metrics()
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "albion_info" in response.text
    assert response.headers["cache-control"] == "no-store"

    monkeypatch.setenv("ALBION_METRICS_TOKEN", "secret-token")
    denied = client.get("/metrics")
    assert denied.status_code == 403
    allowed = client.get("/metrics", headers={"X-Metrics-Token": "secret-token"})
    assert allowed.status_code == 200


def test_request_id_is_returned_and_logged():
    client = TestClient(main.app)
    response = client.get("/api/health", headers={"X-Request-ID": "abc123"})
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "abc123"

    generated = client.get("/api/health")
    assert generated.headers["x-request-id"]
