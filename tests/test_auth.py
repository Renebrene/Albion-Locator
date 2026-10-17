import sys
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main


def fresh_client() -> TestClient:
    return TestClient(main.app)


def admin_client() -> TestClient:
    client = fresh_client()
    response = client.post(
        "/api/auth/login",
        json={"login": "админ", "password": "альбион"},
    )
    assert response.status_code == 200, response.text
    return client


def unique_login(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def test_health_is_public():
    assert fresh_client().get("/api/health").status_code == 200


def test_protected_api_requires_auth():
    client = fresh_client()
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/bookmarks").status_code == 401
    assert client.get("/api/users").status_code == 401


def test_default_admin_login():
    client = fresh_client()
    response = client.post(
        "/api/auth/login",
        json={"login": "админ", "password": "альбион"},
    )
    assert response.status_code == 200
    assert response.json()["user"]["login"] == "админ"

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["role"] == "admin"

    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


def test_wrong_password_is_rejected():
    response = fresh_client().post(
        "/api/auth/login",
        json={"login": "админ", "password": "неверный"},
    )
    assert response.status_code == 401


def test_create_user_and_editor_cannot_manage_users():
    admin = admin_client()
    login = unique_login("editor")
    password = "editor-pass"

    created = admin.post(
        "/api/users",
        json={"login": login, "password": password, "name": "Редактор", "role": "editor"},
    )
    assert created.status_code == 200, created.text

    editor = fresh_client()
    assert editor.post(
        "/api/auth/login",
        json={"login": login, "password": password},
    ).status_code == 200
    assert editor.get("/api/bookmarks").status_code == 200
    assert editor.get("/api/users").status_code == 403


def test_change_own_credentials():
    admin = admin_client()
    login = unique_login("change")
    password = "old-password"
    created = admin.post(
        "/api/users",
        json={"login": login, "password": password, "name": "Смена", "role": "editor"},
    )
    assert created.status_code == 200

    client = fresh_client()
    assert client.post("/api/auth/login", json={"login": login, "password": password}).status_code == 200

    new_login = unique_login("changed")
    changed = client.post(
        "/api/auth/change-credentials",
        json={
            "current_password": password,
            "login": new_login,
            "password": "new-password",
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["user"]["login"] == new_login

    assert fresh_client().post(
        "/api/auth/login", json={"login": login, "password": password}
    ).status_code == 401
    assert fresh_client().post(
        "/api/auth/login", json={"login": new_login, "password": "new-password"}
    ).status_code == 200


def test_duplicate_login_is_rejected():
    admin = admin_client()
    login = unique_login("duplicate")
    payload = {"login": login, "password": "password", "name": "Дубль", "role": "editor"}
    assert admin.post("/api/users", json=payload).status_code == 200
    assert admin.post("/api/users", json=payload).status_code == 400


def test_last_admin_cannot_be_disabled():
    admin = admin_client()
    me = admin.get("/api/auth/me").json()["user"]
    response = admin.patch(f"/api/users/{me['id']}", json={"is_active": False})
    assert response.status_code == 400
