"""Browser E2E fixtures: isolated uvicorn server and Playwright browser."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_health(url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.15)
    raise RuntimeError(f"server did not become healthy: {url}")


@pytest.fixture(scope="session")
def app_server(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    data_dir = tmp_path_factory.mktemp("e2e-auth")
    port = _free_port()
    env = dict(os.environ)
    env.update({
        "ALBION_USERS_DB": str(data_dir / "users.db"),
        "ALBION_REQUIRE_PASSWORD_CHANGE": "0",
        "ALBION_UPSTREAM_CACHE_TTL": "0",
        "ALBION_UPSTREAM_RETRY_ATTEMPTS": "1",
        "ALBION_API_RATE_LIMIT": "0",
        "ALBION_LOGIN_RATE_LIMIT": "0",
        "ALBION_CSRF": "1",
        "ALBION_SETTINGS_FILE": str(data_dir / "user_settings.json"),
        "ALBION_GEO_DATA_DIR": str(data_dir / "geo"),
        "ALBION_BASEMAP_MBTILES": str(data_dir / "basemap.mbtiles"),
        "ALBION_VECTOR_MBTILES": str(data_dir / "vector.mbtiles"),
        "YANDEX_API_KEY": "",
        "YANDEX_SUGGEST_API_KEY": "",
        "YANDEX_JS_API_KEY": "",
        "PYTHONUNBUFFERED": "1",
    })
    process = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "main:app",
            "--app-dir", str(ROOT / "src"),
            "--host", "127.0.0.1", "--port", str(port),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_health(f"{base_url}/api/health")
        yield {"base_url": base_url}
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture(scope="session")
def browser():
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as playwright:
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        browser = (
            playwright.chromium.launch(headless=True, executable_path=executable)
            if executable else playwright.chromium.launch(headless=True)
        )
        yield browser
        browser.close()


@pytest.fixture()
def page(browser, app_server):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    # Each test must start from a clean server-side session: otherwise
    # loadUserState() restores the previous test's point/map and the real
    # locate request it fires races the stubbed assertions of the next test.
    base_url = app_server["base_url"]
    page.request.post(f"{base_url}/api/auth/login",
                      data={"login": "админ", "password": "альбион"})
    csrf = next((item["value"] for item in context.cookies()
                 if item["name"] == "albion_csrf"), "")
    csrf_headers = {"X-CSRF-Token": csrf}
    page.request.delete(f"{base_url}/api/user/workspace", headers=csrf_headers)
    page.request.delete(f"{base_url}/api/user/preferences", headers=csrf_headers)
    context.clear_cookies()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def collect_console(message) -> None:
        if message.type != "error":
            return
        text = message.text
        # Startup intentionally probes /api/auth/me (401 before login) and
        # browsers request a missing favicon (404); neither is a JS failure.
        if "401 (Unauthorized)" in text or "404 (Not Found)" in text:
            return
        errors.append(text)

    page.on("console", collect_console)
    page._e2e_errors = errors  # type: ignore[attr-defined]
    try:
        yield page
    finally:
        context.close()


def login(page, base_url: str) -> None:
    page.goto(base_url)
    page.fill("#authLogin", "админ")
    page.fill("#authPassword", "альбион")
    page.click("#authSubmit")
    page.wait_for_selector("body.authenticated")
