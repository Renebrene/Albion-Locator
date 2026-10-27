"""Default administrator bootstrap script tests."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
sys.path.insert(0, str(ROOT / "src"))

import auth  # noqa: E402
import create_default_admins  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Isolated users DB; auth.DB_PATH is restored automatically."""
    path = tmp_path / "users.db"
    monkeypatch.setattr(auth, "DB_PATH", path)
    auth.init_db()
    return path


def _user(login: str) -> dict:
    return next(user for user in auth.list_users() if user["login"] == login)


def test_creates_missing_admins(db):
    report = create_default_admins.ensure_admins(db, ("техотдел", "альбион"), "альбион")
    assert [item["action"] for item in report] == ["created", "created"]
    assert _user("техотдел")["role"] == "admin"
    assert _user("техотдел")["is_active"] is True
    assert auth.authenticate("техотдел", "альбион") is not None
    assert auth.authenticate("техотдел", "другой-пароль") is None


def test_existing_login_resets_password_and_restores_admin_rights(db):
    # init_db() уже создаёт bootstrap-администратора «админ»; приводим его
    # к состоянию «редактор, отключён», чтобы проверить восстановление прав.
    seeded = _user("админ")
    # auth не даёт разжаловать последнего администратора — заводим второго.
    auth.create_user("запасной", "пароль-запасного", "Запасной", role="admin")
    auth.set_password(seeded["id"], "старый-пароль")
    auth.update_user(seeded["id"], role="editor")
    auth.update_user(seeded["id"], is_active=False)

    report = create_default_admins.ensure_admins(db, ("админ",), "альбион")

    assert report[0]["action"] == "password_reset"
    assert report[0]["extra"] == ["role=admin", "activated"]
    user = _user("админ")
    assert user["role"] == "admin" and user["is_active"] is True
    assert auth.authenticate("админ", "альбион") is not None
    assert auth.authenticate("админ", "старый-пароль") is None


def test_password_works_on_first_login_by_default(db):
    create_default_admins.ensure_admins(db, ("альбион",), "альбион")
    assert _user("альбион")["must_change_password"] is False


def test_forced_change_flag_can_be_requested(db):
    create_default_admins.ensure_admins(
        db, ("администратор",), "альбион", force_password_change=True,
    )
    assert _user("администратор")["must_change_password"] is True


def test_repeated_run_is_idempotent(db):
    create_default_admins.ensure_admins(db, ("админ",), "альбион")
    first = _user("админ")["id"]
    create_default_admins.ensure_admins(db, ("админ",), "новый-пароль")
    assert _user("админ")["id"] == first
    assert len(auth.list_users()) == 1
    assert auth.authenticate("админ", "новый-пароль") is not None


def test_environment_flag_is_restored(db, monkeypatch):
    monkeypatch.setenv("ALBION_REQUIRE_PASSWORD_CHANGE", "0")
    create_default_admins.ensure_admins(db, ("админ",), "альбион")
    import os

    assert os.environ["ALBION_REQUIRE_PASSWORD_CHANGE"] == "0"
