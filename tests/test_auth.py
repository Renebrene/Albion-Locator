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
