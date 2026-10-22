"""CSRF, secure cookies, headers, docs exposure and login throttling."""

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main
import security


def _client() -> TestClient:
    return TestClient(main.app)


def _login(client: TestClient, headers: dict[str, str] | None = None):
    return client.post(
        "/api/auth/login",
        json={"login": "админ", "password": "альбион"},
        headers=headers,
    )


def test_docs_and_openapi_are_disabled():
    client = _client()
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/redoc").status_code == 404


def test_security_headers_are_added_and_hsts_only_for_https():
    client = _client()
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "geolocation=(self)" in response.headers["permissions-policy"]
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "strict-transport-security" not in response.headers

    https = client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert "max-age=" in https.headers["strict-transport-security"]


def test_https_proxy_sets_secure_session_and_csrf_cookies():
    client = _client()
    response = _login(client, headers={"X-Forwarded-Proto": "https"})
    assert response.status_code == 200, response.text
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(cookie for cookie in cookies if cookie.startswith("albion_session="))
    csrf_cookie = next(cookie for cookie in cookies if cookie.startswith("albion_csrf="))
    assert "Secure" in session_cookie
    assert "HttpOnly" in session_cookie
    assert "Secure" in csrf_cookie
    assert "HttpOnly" not in csrf_cookie


def test_csrf_is_required_for_unsafe_api(monkeypatch):
    monkeypatch.setenv("ALBION_CSRF", "1")
    client = _client()
    login = _login(client)
    assert login.status_code == 200
    csrf = client.cookies.get(security.CSRF_COOKIE)
    assert csrf

    denied = client.post("/api/auth/logout")
    assert denied.status_code == 403
    assert "CSRF" in denied.json()["detail"]

    wrong = client.post("/api/auth/logout", headers={"X-CSRF-Token": "wrong"})
    assert wrong.status_code == 403

    allowed = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert allowed.status_code == 200


def test_me_repairs_missing_csrf_cookie(monkeypatch):
    monkeypatch.setenv("ALBION_CSRF", "1")
    client = _client()
    assert _login(client).status_code == 200
    client.cookies.delete(security.CSRF_COOKIE)

    response = client.get("/api/auth/me")
    assert response.status_code == 200
    assert any(
        cookie.startswith("albion_csrf=")
        for cookie in response.headers.get_list("set-cookie")
    )


def test_login_locks_after_repeated_failures(monkeypatch):
    monkeypatch.setattr(security, "LOGIN_GUARD", security.LoginGuard(2, 60, 60))
    monkeypatch.setattr(security, "LOGIN_RATE", security.SlidingWindowLimiter(0, 60))
    client = _client()
    payload = {"login": "nobody-lockout", "password": "wrong"}

    assert client.post("/api/auth/login", json=payload).status_code == 401
    assert client.post("/api/auth/login", json=payload).status_code == 401
    locked = client.post("/api/auth/login", json=payload)
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) >= 1


def test_login_rate_limit(monkeypatch):
    monkeypatch.setattr(security, "LOGIN_GUARD", security.LoginGuard(100, 60, 60))
    monkeypatch.setattr(security, "LOGIN_RATE", security.SlidingWindowLimiter(2, 60))
    client = _client()

    assert _login(client).status_code == 200
    assert _login(client).status_code == 200
    limited = _login(client)
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers


def test_cross_origin_post_is_rejected():
    client = _client()
    response = client.post(
        "/api/auth/login",
        json={"login": "админ", "password": "альбион"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_sliding_window_limiter_expires():
    clock = {"now": 100.0}
    original = security.time.monotonic
    security.time.monotonic = lambda: clock["now"]
    try:
        limiter = security.SlidingWindowLimiter(2, 10)
        assert limiter.check("key") is None
        assert limiter.check("key") is None
        assert limiter.check("key") is not None
        clock["now"] = 111.0
        assert limiter.check("key") is None
    finally:
        security.time.monotonic = original
