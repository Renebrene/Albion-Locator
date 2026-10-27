"""Cookie, CSRF and rate-limit helpers for the single-process deployment."""
from __future__ import annotations

import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from http.cookies import SimpleCookie
from typing import Any

CSRF_COOKIE = "albion_csrf"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def docs_enabled() -> bool:
    value = os.getenv("ALBION_ENABLE_DOCS", "0").strip().lower()
    return value in {"1", "true", "yes", "on"}


def csrf_enabled() -> bool:
    value = os.getenv("ALBION_CSRF", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def request_is_https(scope: dict[str, Any]) -> bool:
    headers = {key.lower(): value for key, value in scope.get("headers", [])}
    forwarded = headers.get(b"x-forwarded-proto", b"").decode("latin-1")
    proto = forwarded.split(",")[0].strip().lower()
    if proto:
        return proto == "https"
    standard = headers.get(b"forwarded", b"").decode("latin-1").lower()
    if "proto=https" in standard:
        return True
    for name in (b"x-forwarded-ssl", b"x-url-scheme", b"front-end-https"):
        value = headers.get(name, b"").decode("latin-1").strip().lower()
        if value in {"on", "https"}:
            return True
    return scope.get("scheme") == "https"


def cookie_secure(scope: dict[str, Any]) -> bool:
    mode = os.getenv("ALBION_COOKIE_SECURE", "auto").strip().lower()
    if mode in {"1", "true", "yes", "on", "always"}:
        return True
    if mode in {"0", "false", "no", "off", "never"}:
        return False
    return request_is_https(scope)


def cookie_value(scope: dict[str, Any], name: str) -> str | None:
    headers = {key.lower(): value for key, value in scope.get("headers", [])}
    raw = headers.get(b"cookie")
    if not raw:
        return None
    cookies = SimpleCookie()
    try:
        cookies.load(raw.decode("latin-1"))
    except Exception:
        return None
    morsel = cookies.get(name)
    return morsel.value if morsel else None


def header_value(scope: dict[str, Any], name: str) -> str | None:
    wanted = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key.lower() == wanted:
            return value.decode("latin-1").strip()
    return None


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_valid(cookie_token: str | None, header_token: str | None) -> bool:
    if not cookie_token or not header_token:
        return False
    return hmac.compare_digest(cookie_token, header_token)


def client_ip(scope: dict[str, Any]) -> str:
    forwarded = header_value(scope, "x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64] or "unknown"
    client = scope.get("client")
    return str(client[0])[:64] if client else "unknown"


class SlidingWindowLimiter:
    """Count events per key in a fixed wall-clock window."""

    def __init__(self, limit: int, window_seconds: float, max_keys: int = 10_000):
        self.limit = max(0, int(limit))
        self.window = max(1.0, float(window_seconds))
        self.max_keys = max(1, int(max_keys))
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> float | None:
        """Record a request; return seconds to wait when the limit is hit."""
        if self.limit <= 0:
            return None
        now = time.monotonic()
        queue = self._events[key]
        while queue and queue[0] <= now - self.window:
            queue.popleft()
        if len(queue) >= self.limit:
            return max(0.25, queue[0] + self.window - now)
        if len(self._events) > self.max_keys:
            self._events.clear()
        queue.append(now)
        return None

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._events.clear()
        else:
            self._events.pop(key, None)


class LoginGuard:
    """Track failed logins per IP+login and lock the key temporarily."""

    def __init__(self, max_failures: int, window_seconds: float,
                 lockout_seconds: float, max_keys: int = 10_000):
        self.max_failures = max(1, int(max_failures))
        self.window = max(1.0, float(window_seconds))
        self.lockout = max(1.0, float(lockout_seconds))
        self.max_keys = max(1, int(max_keys))
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._locked_until: dict[str, float] = {}

    def retry_after(self, key: str) -> float | None:
        now = time.monotonic()
        until = self._locked_until.get(key, 0.0)
        if until > now:
            return until - now
        if until:
            self._locked_until.pop(key, None)
        queue = self._failures.get(key)
        if queue:
            while queue and queue[0] <= now - self.window:
                queue.popleft()
            if not queue:
                self._failures.pop(key, None)
        return None

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        self.retry_after(key)  # prune expired failures
        queue = self._failures[key]
        queue.append(now)
        if len(queue) >= self.max_failures:
            self._locked_until[key] = now + self.lockout
            queue.clear()
        if len(self._failures) > self.max_keys:
            self._failures.clear()
            self._locked_until.clear()

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)
        self._locked_until.pop(key, None)

    def reset(self) -> None:
        self._failures.clear()
        self._locked_until.clear()


LOGIN_GUARD = LoginGuard(
    _env_int("ALBION_LOGIN_MAX_FAILURES", 5, minimum=1),
    _env_int("ALBION_LOGIN_WINDOW_SECONDS", 900, minimum=1),
    _env_int("ALBION_LOGIN_LOCKOUT_SECONDS", 900, minimum=1),
)
LOGIN_RATE = SlidingWindowLimiter(
    _env_int("ALBION_LOGIN_RATE_LIMIT", 20),
    _env_int("ALBION_LOGIN_RATE_WINDOW", 60, minimum=1),
)
# Общий лимит на /api/* выключен по умолчанию: сервис работает локально или
# через VPN, а один сеанс работы с ползунком километража даёт десятки запросов
# (история, МСК, обратный геокодер). Лимит включается осознанно для публичного
# контура: ALBION_API_RATE_LIMIT=300 (0 = выключен).
# Отдельный лимит входов LOGIN_RATE и lockout ниже остаются включёнными: они
# защищают от подбора пароля, а не от всплесков нагрузки.
API_RATE = SlidingWindowLimiter(
    _env_int("ALBION_API_RATE_LIMIT", 0),
    _env_int("ALBION_API_RATE_WINDOW", 60, minimum=1),
)

DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://api-maps.yandex.ru "
    "https://yandex.ru https://*.yandex.ru https://*.yandex.net "
    "https://yastatic.net https://*.yastatic.net; "
    "style-src 'self' 'unsafe-inline' https://yandex.ru https://*.yandex.ru "
    "https://yastatic.net https://*.yastatic.net; "
    "img-src 'self' data: blob: https://tile.openstreetmap.org "
    "https://*.tile.openstreetmap.org https://*.yandex.ru https://*.yandex.net "
    "https://yastatic.net https://*.yastatic.net; "
    "connect-src 'self' https://overpass-api.de "
    "https://tile.openstreetmap.org https://*.tile.openstreetmap.org "
    "https://*.yandex.ru https://*.yandex.net "
    "https://yastatic.net https://*.yastatic.net; "
    "font-src 'self' data:; worker-src 'self' blob:; "
    "child-src 'self' blob:; frame-src 'self' https://yandex.ru "
    "https://*.yandex.ru; frame-ancestors 'none'; object-src 'none'; "
    "base-uri 'self'; form-action 'self'"
)


def _append_header(message: dict[str, Any], name: str, value: str) -> None:
    headers = list(message.get("headers") or [])
    lowered = {key.lower() for key, _ in headers}
    if name.lower().encode("latin-1") not in lowered:
        headers.append((name.lower().encode("latin-1"), value.encode("latin-1")))
    message["headers"] = headers


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                _append_header(message, "X-Content-Type-Options", "nosniff")
                _append_header(message, "X-Frame-Options", "DENY")
                _append_header(message, "Referrer-Policy", "strict-origin-when-cross-origin")
                _append_header(
                    message, "Permissions-Policy",
                    "geolocation=(self), camera=(), microphone=()",
                )
                _append_header(message, "X-Robots-Tag", "noindex, nofollow")
                csp = os.getenv("ALBION_CSP_POLICY", DEFAULT_CSP)
                if csp.strip().lower() not in {"", "off", "0", "false"}:
                    _append_header(message, "Content-Security-Policy", csp)
                if request_is_https(scope):
                    _append_header(
                        message, "Strict-Transport-Security",
                        "max-age=31536000; includeSubDomains",
                    )
                if scope.get("path", "").startswith("/api/"):
                    _append_header(message, "Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def _send_json(send, status_code: int, payload: dict[str, Any],
                     extra_headers: list[tuple[bytes, bytes]] | None = None) -> None:
    import json

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    headers.extend(extra_headers or [])
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class ApiRateLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith("/api/") and path != "/api/health":
            retry_after = API_RATE.check(client_ip(scope))
            if retry_after is not None:
                await _send_json(
                    send, 429,
                    {"detail": "Слишком много запросов; повторите позже"},
                    [(b"retry-after", str(max(1, int(retry_after))).encode("ascii"))],
                )
                return
        await self.app(scope, receive, send)
