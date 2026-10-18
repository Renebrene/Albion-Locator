"""Персональное состояние: preferences, workspace и избранное на пользователя."""
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main


def fresh_client() -> TestClient:
    return TestClient(main.app)


def admin_client() -> TestClient:
    client = fresh_client()
    response = client.post("/api/auth/login", json={"login": "админ", "password": "альбион"})
    assert response.status_code == 200, response.text
    return client


def editor_client() -> TestClient:
    admin = admin_client()
    login = f"state_{uuid.uuid4().hex[:8]}"
    created = admin.post(
        "/api/users",
        json={"login": login, "password": "state-pass", "name": "Состояние", "role": "editor"},
    )
    assert created.status_code == 200, created.text
    client = fresh_client()
    assert client.post(
        "/api/auth/login", json={"login": login, "password": "state-pass"}
    ).status_code == 200
    return client


def test_state_endpoints_require_auth():
    client = fresh_client()
    assert client.get("/api/user/preferences").status_code == 401
    assert client.put("/api/user/preferences", json={"payload": {}}).status_code == 401
    assert client.get("/api/user/workspace").status_code == 401
    assert client.put("/api/user/workspace", json={"payload": {}}).status_code == 401


def test_preferences_round_trip_and_versioning():
    client = admin_client()
    empty = client.get("/api/user/preferences")
    assert empty.status_code == 200
    assert empty.json() == {"payload": {}, "version": 0}

    saved = client.put(
        "/api/user/preferences",
        json={"payload": {"radius": 1500, "theme": "dark"}, "version": 0},
    )
    assert saved.status_code == 200
    assert saved.json()["version"] == 1

    loaded = client.get("/api/user/preferences").json()
    assert loaded["payload"]["radius"] == 1500
    assert loaded["version"] == 1


def test_workspace_round_trip():
    client = admin_client()
    saved = client.put(
        "/api/user/workspace",
        json={"payload": {"map": {"lat": 47.23, "lon": 39.72, "zoom": 9}}},
    )
    assert saved.status_code == 200
    loaded = client.get("/api/user/workspace").json()
    assert loaded["payload"]["map"]["zoom"] == 9


def test_stale_version_is_rejected():
    client = admin_client()
    current = client.get("/api/user/workspace").json()["version"]
    first = client.put(
        "/api/user/workspace", json={"payload": {"step": 1}, "version": current}
    )
    assert first.status_code == 200
    # Повторная отправка с устаревшей версией отбивается конфликтом.
    stale = client.put(
        "/api/user/workspace", json={"payload": {"step": 2}, "version": current}
    )
    assert stale.status_code == 409


def test_state_documents_are_per_user():
    admin = admin_client()
    editor = editor_client()
    assert admin.put(
        "/api/user/preferences", json={"payload": {"theme": "dark"}}
    ).status_code == 200
    # Другой пользователь не видит настройки администратора.
    assert editor.get("/api/user/preferences").json()["payload"] == {}


def test_state_payload_size_is_limited():
    client = admin_client()
    huge = {"blob": "x" * (main.MAX_STATE_PAYLOAD_BYTES + 100)}
    response = client.put("/api/user/preferences", json={"payload": huge})
    assert response.status_code == 413


def test_bookmarks_are_per_user():
    admin = admin_client()
    editor = editor_client()
    created = admin.post("/api/bookmarks", json={"name": "admin point", "lat": 47.2, "lon": 39.7})
    assert created.status_code == 200
    bookmark_id = created.json()["bookmark"]["id"]

    admin_list = admin.get("/api/bookmarks").json()["bookmarks"]
    assert any(item["id"] == bookmark_id for item in admin_list)
    editor_list = editor.get("/api/bookmarks").json()["bookmarks"]
    assert all(item["id"] != bookmark_id for item in editor_list)

    # Чужой пользователь не может удалить или переименовать закладку.
    assert editor.patch(f"/api/bookmarks/{bookmark_id}", json={"name": "hijack"}).status_code == 404
    editor.delete(f"/api/bookmarks/{bookmark_id}")
    assert any(
        item["id"] == bookmark_id for item in admin.get("/api/bookmarks").json()["bookmarks"]
    )


def test_bookmark_rename_and_delete():
    client = admin_client()
    created = client.post("/api/bookmarks", json={"name": "old", "lat": 47.2, "lon": 39.7})
    bookmark_id = created.json()["bookmark"]["id"]
    renamed = client.patch(f"/api/bookmarks/{bookmark_id}", json={"name": "new"})
    assert renamed.status_code == 200
    assert renamed.json()["bookmark"]["name"] == "new"
    assert client.delete(f"/api/bookmarks/{bookmark_id}").status_code == 200
    assert all(
        item["id"] != bookmark_id for item in client.get("/api/bookmarks").json()["bookmarks"]
    )


def test_workspace_delete_resets_only_workspace():
    client = admin_client()
    client.put("/api/user/workspace", json={"payload": {"tab": "point"}})
    client.put("/api/user/preferences", json={"payload": {"theme": "dark"}})
    assert client.delete("/api/user/workspace").status_code == 200
    assert client.get("/api/user/workspace").json()["payload"] == {}
    # Настройки сброс рабочей сессии не затрагивает.
    assert client.get("/api/user/preferences").json()["payload"]["theme"] == "dark"


@pytest.mark.parametrize("path", ["/api/user/preferences", "/api/user/workspace"])
def test_state_mutations_reject_cross_origin(path):
    client = admin_client()
    response = client.put(
        path,
        json={"payload": {"x": 1}},
        headers={"origin": "https://attacker.example"},
    )
    assert response.status_code == 403
