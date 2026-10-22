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


def test_root_serves_login_gated_app_without_caching():
    response = fresh_client().get("/")
    assert response.status_code == 200
    assert 'id="authOverlay"' in response.text
    assert 'id="usersSettingsBlock"' in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert 'id="authOverlay"' in response.text
    for element_id in (
        # Новый каркас варианта A: вкладки, единые поля, карточка точки, статус-бар.
        "app-main", "status-bar", "tabPoint", "tabBatch", "tabSaved",
        "searchArmCheckbox", "placePointBtn",
        "searchDock", "searchDockTopHost", "searchDockBottomHost",
        "searchPositionSelect",
        "modeInput1", "modeInput2", "modeLabel1", "modeLabel2",
        "modeActionBtn", "modeStatus",
        "roadKmPanel", "roadKmNumber", "roadKmSlider", "roadKmApplyButton",
        "currentPointCard", "currentPointDetails", "cpWgs", "cpMsk", "cpAddress",
        "resultSourceValue", "settings-source", "mskDirection", "wgsLat", "wgsLon",
        "mskZoneHelpButton", "mskZoneTooltip",
        "sidebar-left", "sidebar-right", "resultsFrame",
    ):
        assert f'id="{element_id}"' in response.text
    assert "setupFrameTabs()" in response.text
    assert "setupSettingsModal()" in response.text
    assert "setupSearchPanel()" in response.text
    assert "initializeYandexAddressSuggest()" in response.text
    assert "ymaps.SuggestView" in response.text
    assert "/api/maps-config" in response.text
    assert "/static/vendor/leaflet.js" in response.text
    assert "/static/vendor/dom-to-image-more.min.js" in response.text
    assert "api-maps.yandex.ru/2.1/" in response.text
    assert "/api/roads/online/search" in response.text
    assert "/api/roads/online/sections" in response.text
    assert "roadKmSlider" in response.text
    assert "applyRoadKmNumber()" in response.text
    assert "fillRule: 'evenodd'" in response.text
    assert "color: '#5b6670'" in response.text
    assert "weight: 1.5" in response.text
    assert "/api/address/online/geocode" in response.text
    assert "/api/msk61/to-geographic" in response.text
    assert "/api/msk61/from-geographic" in response.text
    assert "Кадастровые районы Ростовской области по зонам МСК-61" in response.text
    assert "Азовский (61:01)" in response.text
    assert "https://terraingis.ru/msk-61.html" in response.text
    # Старые элементы поиска удалены, дублей нет.
    for removed_id in ("map-search-toolbar", "searchModeSelect", "coordSingle", "batch-panel"):
        assert f'id="{removed_id}"' not in response.text


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


def test_admin_password_reset_revokes_existing_sessions():
    admin = admin_client()
    login = unique_login("reset")
    created = admin.post("/api/users", json={"login": login, "password": "old-pass", "name": "Сброс", "role": "editor"})
    assert created.status_code == 200

    target = fresh_client()
    assert target.post("/api/auth/login", json={"login": login, "password": "old-pass"}).status_code == 200
    user_id = created.json()["user"]["id"]
    changed = admin.post(f"/api/users/{user_id}/password", json={"password": "new-pass"})
    assert changed.status_code == 200
    assert target.get("/api/auth/me").status_code == 401
    assert fresh_client().post("/api/auth/login", json={"login": login, "password": "old-pass"}).status_code == 401
    assert fresh_client().post("/api/auth/login", json={"login": login, "password": "new-pass"}).status_code == 200


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


def test_schema_migration_adds_password_change_flag(tmp_path, monkeypatch):
    import sqlite3
    import auth

    database = tmp_path / "old-users.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login TEXT NOT NULL,
            login_norm TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'admin',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO users (login, login_norm, password_hash, name, role, is_active, created_at) "
        "VALUES ('старый', 'старый', 'x', 'Старый', 'admin', 1, 0)"
    )
    connection.commit()
    connection.close()

    monkeypatch.setattr(auth, "DB_PATH", database)
    auth.init_db()
    with sqlite3.connect(database) as check:
        columns = {row[1] for row in check.execute("PRAGMA table_info(users)")}
    assert "must_change_password" in columns


def test_forced_password_change_flow(tmp_path, monkeypatch):
    import pytest
    import auth

    monkeypatch.setattr(auth, "DB_PATH", tmp_path / "flag-users.db")
    monkeypatch.setenv("ALBION_REQUIRE_PASSWORD_CHANGE", "1")
    auth.init_db()

    user = auth.authenticate("админ", "альбион")
    assert user is not None
    assert user["must_change_password"] is True

    with pytest.raises(ValueError):
        auth.change_credentials(user["id"], "альбион", None, None)

    updated = auth.change_credentials(user["id"], "альбион", None, "НовыйПароль123")
    assert updated["must_change_password"] is False
    assert auth.authenticate("админ", "НовыйПароль123")["must_change_password"] is False

    created = auth.create_user("editor_temp", "ВременныйПароль1", "Редактор", "editor")
    assert created["must_change_password"] is True

    auth.set_password(user["id"], "СброшенныйПароль1")
    assert auth.authenticate("админ", "СброшенныйПароль1")["must_change_password"] is True


def test_audit_log_records_security_events_without_secrets():
    client = admin_client()
    login_name = unique_login("audit")
    created = client.post(
        "/api/users",
        json={"login": login_name, "password": "secret-pass", "name": "Аудит", "role": "editor"},
    )
    assert created.status_code == 200, created.text
    user_id = created.json()["user"]["id"]

    # A failed login must be visible to the administrator, without the password.
    failed = fresh_client().post(
        "/api/auth/login",
        json={"login": login_name, "password": "wrong-password"},
    )
    assert failed.status_code == 401

    response = client.get("/api/audit", params={"limit": 50})
    assert response.status_code == 200, response.text
    events = response.json()["events"]
    actions = [event["action"] for event in events]
    assert "login" in actions
    assert "user_created" in actions
    assert "login_failed" in actions
    created_event = next(event for event in events if event["action"] == "user_created")
    assert created_event["target"] == f"user:{user_id}"
    assert "secret-pass" not in response.text
    assert "wrong-password" not in response.text


def test_audit_log_is_admin_only_and_supports_action_filter():
    editor = fresh_client()
    login_name = unique_login("editor_audit")
    password = "editor-pass"
    admin = admin_client()
    created = admin.post(
        "/api/users",
        json={"login": login_name, "password": password, "name": "Редактор", "role": "editor"},
    )
    assert created.status_code == 200, created.text
    assert editor.post(
        "/api/auth/login", json={"login": login_name, "password": password}
    ).status_code == 200
    assert editor.get("/api/audit").status_code == 403

    filtered = admin.get("/api/audit", params={"action": "user_created", "limit": 5})
    assert filtered.status_code == 200
    assert filtered.json()["events"]
    assert all(event["action"] == "user_created" for event in filtered.json()["events"])
