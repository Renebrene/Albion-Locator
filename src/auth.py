"""Минимальная авторизация локатора: SQLite, PBKDF2 и cookie-сессии.

Модуль намеренно использует только стандартную библиотеку Python.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("ALBION_USERS_DB", str(PROJECT_DIR / "users.db")))

SESSION_COOKIE = "albion_session"
SESSION_TTL = 30 * 24 * 60 * 60
PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 6
DEFAULT_LOGIN = "админ"
DEFAULT_PASSWORD = "альбион"
DEFAULT_NAME = "Администратор"

ALLOWED_ROLES = {"admin", "editor"}
ROLE_LABELS = {"admin": "Администратор", "editor": "Редактор"}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                login TEXT NOT NULL,
                login_norm TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'admin'
                    CHECK (role IN ('admin', 'editor')),
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_user_id
                ON sessions(user_id);

            -- Персональное состояние интерфейса. Хранится одним JSON-документом
            -- на пользователя: состав полей меняется вместе с UI и не должен
            -- требовать миграции схемы на каждое новое поле.
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                payload TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_workspace (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                payload TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL
            );

            -- Избранное персонально: раньше это был общий JSON-файл на всех.
            CREATE TABLE IF NOT EXISTS user_bookmarks (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_user_bookmarks_user
                ON user_bookmarks(user_id, created_at DESC);
            """
        )
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count == 0:
            conn.execute(
                """
                INSERT INTO users
                    (login, login_norm, password_hash, name, role, is_active, created_at)
                VALUES
                    (?, ?, ?, ?, 'admin', 1, ?)
                """,
                (
                    DEFAULT_LOGIN,
                    _normalize_login(DEFAULT_LOGIN),
                    hash_password(DEFAULT_PASSWORD),
                    DEFAULT_NAME,
                    time.time(),
                ),
            )
        conn.commit()


def _normalize_login(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").strip().casefold()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _public_user(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "login": row["login"],
        "name": row["name"],
        "role": row["role"],
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
    }


def _row_by_id(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, login, name, role, is_active, created_at
        FROM users
        WHERE id = ?
        """,
        (user_id,),
    ).fetchone()


def authenticate(login: str, password: str) -> dict[str, Any] | None:
    login_norm = _normalize_login(login)
    if not login_norm or not password:
        return None

    with closing(_connect()) as conn:
        row = conn.execute(
            """
            SELECT id, login, password_hash, name, role, is_active, created_at
            FROM users
            WHERE login_norm = ? AND is_active = 1
            LIMIT 1
            """,
            (login_norm,),
        ).fetchone()

    if row is None or not verify_password(password, row["password_hash"]):
        return None

    return _public_user(row)


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.time()

    with closing(_connect()) as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        conn.execute(
            """
            INSERT INTO sessions (token_hash, user_id, expires_at, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (token_hash, user_id, now + SESSION_TTL, now),
        )
        conn.commit()

    return token


def get_user_by_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with closing(_connect()) as conn:
        row = conn.execute(
            """
            SELECT u.id, u.login, u.name, u.role, u.is_active, u.created_at
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?
              AND s.expires_at > ?
              AND u.is_active = 1
            LIMIT 1
            """,
            (token_hash, time.time()),
        ).fetchone()

    return _public_user(row) if row else None


def delete_session(token: str | None) -> None:
    if not token:
        return

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        conn.commit()


def delete_user_sessions(user_id: int, keep_token: str | None = None) -> None:
    keep_hash = (
        hashlib.sha256(keep_token.encode("utf-8")).hexdigest()
        if keep_token
        else None
    )
    with closing(_connect()) as conn:
        if keep_hash:
            conn.execute(
                "DELETE FROM sessions WHERE user_id = ? AND token_hash <> ?",
                (user_id, keep_hash),
            )
        else:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.commit()


def _validate_login(login: str) -> str:
    login = (login or "").strip()
    if not login:
        raise ValueError("Укажите логин.")
    if len(login) > 120:
        raise ValueError("Логин слишком длинный.")
    return login


def _validate_password(password: str) -> str:
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Пароль должен содержать минимум {MIN_PASSWORD_LENGTH} символов."
        )
    return password


def _validate_role(role: str) -> str:
    role = (role or "").strip()
    if role not in ALLOWED_ROLES:
        raise ValueError("Недопустимая роль. Допустимо: admin, editor.")
    return role


def _active_admin_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1"
    ).fetchone()[0]


def change_credentials(
    user_id: int,
    current_password: str,
    new_login: str | None = None,
    new_password: str | None = None,
) -> dict[str, Any]:
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, login, password_hash FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

        if row is None:
            raise ValueError("Пользователь не найден.")
        if not verify_password(current_password, row["password_hash"]):
            raise ValueError("Текущий пароль указан неверно.")

        login = row["login"]
        password_hash = row["password_hash"]

        if new_login is not None and new_login.strip():
            login = _validate_login(new_login)
            login_norm = _normalize_login(login)
            duplicate = conn.execute(
                "SELECT id FROM users WHERE login_norm = ? AND id <> ?",
                (login_norm, user_id),
            ).fetchone()
            if duplicate:
                raise ValueError("Пользователь с таким логином уже существует.")
            conn.execute(
                "UPDATE users SET login = ?, login_norm = ? WHERE id = ?",
                (login, login_norm, user_id),
            )

        if new_password:
            password_hash = hash_password(_validate_password(new_password))
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (password_hash, user_id),
            )

        updated = _row_by_id(conn, user_id)
        conn.commit()

    return _public_user(updated)


def list_users() -> list[dict[str, Any]]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT id, login, name, role, is_active, created_at
            FROM users
            ORDER BY id
            """
        ).fetchall()
    return [_public_user(row) for row in rows]


def create_user(
    login: str,
    password: str,
    name: str,
    role: str = "editor",
) -> dict[str, Any]:
    login = _validate_login(login)
    password = _validate_password(password)
    name = (name or "").strip() or login
    role = _validate_role(role)
    login_norm = _normalize_login(login)

    with closing(_connect()) as conn:
        duplicate = conn.execute(
            "SELECT id FROM users WHERE login_norm = ?",
            (login_norm,),
        ).fetchone()
        if duplicate:
            raise ValueError("Пользователь с таким логином уже существует.")

        conn.execute(
            """
            INSERT INTO users
                (login, login_norm, password_hash, name, role, is_active, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (login, login_norm, hash_password(password), name, role, time.time()),
        )
        row = conn.execute(
            """
            SELECT id, login, name, role, is_active, created_at
            FROM users
            WHERE login_norm = ?
            """,
            (login_norm,),
        ).fetchone()
        conn.commit()

    return _public_user(row)


def update_user(
    user_id: int,
    login: str | None = None,
    name: str | None = None,
    role: str | None = None,
    is_active: bool | None = None,
) -> dict[str, Any]:
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _row_by_id(conn, user_id)
        if row is None:
            raise ValueError("Пользователь не найден.")

        fields: list[str] = []
        values: list[Any] = []

        if login is not None:
            login_value = _validate_login(login)
            login_norm = _normalize_login(login_value)
            duplicate = conn.execute(
                "SELECT id FROM users WHERE login_norm = ? AND id <> ?",
                (login_norm, user_id),
            ).fetchone()
            if duplicate:
                raise ValueError("Пользователь с таким логином уже существует.")
            fields.extend(["login = ?", "login_norm = ?"])
            values.extend([login_value, login_norm])

        if name is not None:
            name_value = name.strip()
            if not name_value:
                raise ValueError("Имя не может быть пустым.")
            fields.append("name = ?")
            values.append(name_value)

        if role is not None:
            role_value = _validate_role(role)
            if (
                row["role"] == "admin"
                and row["is_active"]
                and role_value != "admin"
                and _active_admin_count(conn) <= 1
            ):
                raise ValueError("Нельзя лишить роли последнего администратора.")
            fields.append("role = ?")
            values.append(role_value)

        if is_active is not None:
            active_value = 1 if is_active else 0
            if (
                row["role"] == "admin"
                and row["is_active"]
                and not active_value
                and _active_admin_count(conn) <= 1
            ):
                raise ValueError("Нельзя отключить последнего администратора.")
            fields.append("is_active = ?")
            values.append(active_value)

        if fields:
            values.append(user_id)
            conn.execute(
                f"UPDATE users SET {', '.join(fields)} WHERE id = ?",
                tuple(values),
            )

        updated = _row_by_id(conn, user_id)
        conn.commit()

    return _public_user(updated)


def set_password(user_id: int, password: str) -> None:
    password = _validate_password(password)
    with closing(_connect()) as conn:
        result = conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(password), user_id),
        )
        if result.rowcount == 0:
            raise ValueError("Пользователь не найден.")
        conn.commit()


# ========== ПЕРСОНАЛЬНОЕ СОСТОЯНИЕ ==========
# Документы preferences/workspace читаются и пишутся целиком. Версия растёт на
# каждой записи; клиент передаёт ожидаемую версию, чтобы две открытые вкладки
# не затирали друг друга молча.

def _read_document(table: str, user_id: int) -> dict[str, Any] | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            f"SELECT payload, version, updated_at FROM {table} WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "payload": payload,
        "version": row["version"],
        "updated_at": row["updated_at"],
    }


def _write_document(
    table: str,
    user_id: int,
    payload: dict[str, Any],
    expected_version: int | None = None,
) -> dict[str, Any]:
    encoded = json.dumps(payload, ensure_ascii=False)
    now = time.time()
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT version FROM {table} WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        current_version = row["version"] if row else 0
        if expected_version is not None and expected_version != current_version:
            conn.rollback()
            raise ValueError("Состояние изменено в другой вкладке; обновите страницу.")
        next_version = current_version + 1
        conn.execute(
            f"""
            INSERT INTO {table} (user_id, payload, version, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                payload = excluded.payload,
                version = excluded.version,
                updated_at = excluded.updated_at
            """,
            (user_id, encoded, next_version, now),
        )
        conn.commit()
    return {"payload": payload, "version": next_version, "updated_at": now}


def get_preferences(user_id: int) -> dict[str, Any] | None:
    return _read_document("user_preferences", user_id)


def save_preferences(user_id: int, payload: dict[str, Any],
                     expected_version: int | None = None) -> dict[str, Any]:
    return _write_document("user_preferences", user_id, payload, expected_version)


def clear_preferences(user_id: int) -> None:
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM user_preferences WHERE user_id = ?", (user_id,))
        conn.commit()


def get_workspace(user_id: int) -> dict[str, Any] | None:
    return _read_document("user_workspace", user_id)


def save_workspace(user_id: int, payload: dict[str, Any],
                   expected_version: int | None = None) -> dict[str, Any]:
    return _write_document("user_workspace", user_id, payload, expected_version)


def clear_workspace(user_id: int) -> None:
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM user_workspace WHERE user_id = ?", (user_id,))
        conn.commit()


def list_bookmarks(user_id: int) -> list[dict[str, Any]]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT id, name, lat, lon, metadata, created_at
            FROM user_bookmarks
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 200
            """,
            (user_id,),
        ).fetchall()
    bookmarks = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata"])
        except (TypeError, ValueError):
            metadata = {}
        bookmarks.append({
            "id": row["id"],
            "name": row["name"],
            "lat": row["lat"],
            "lon": row["lon"],
            "metadata": metadata if isinstance(metadata, dict) else {},
            "created_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(row["created_at"])),
        })
    return bookmarks


def add_bookmark(user_id: int, name: str, lat: float, lon: float,
                 metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    bookmark_id = secrets.token_hex(16)
    now = time.time()
    encoded = json.dumps(metadata or {}, ensure_ascii=False)
    with closing(_connect()) as conn:
        conn.execute(
            """
            INSERT INTO user_bookmarks
                (id, user_id, name, lat, lon, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (bookmark_id, user_id, name, lat, lon, encoded, now, now),
        )
        conn.commit()
    return {
        "id": bookmark_id,
        "name": name,
        "lat": lat,
        "lon": lon,
        "metadata": metadata or {},
        "created_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(now)),
    }


def update_bookmark(user_id: int, bookmark_id: str, name: str) -> dict[str, Any]:
    with closing(_connect()) as conn:
        result = conn.execute(
            "UPDATE user_bookmarks SET name = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (name, time.time(), bookmark_id, user_id),
        )
        if result.rowcount == 0:
            raise ValueError("Закладка не найдена.")
        conn.commit()
    return {"id": bookmark_id, "name": name}


def delete_bookmark(user_id: int, bookmark_id: str) -> None:
    with closing(_connect()) as conn:
        conn.execute(
            "DELETE FROM user_bookmarks WHERE id = ? AND user_id = ?",
            (bookmark_id, user_id),
        )
        conn.commit()


def count_bookmarks(user_id: int) -> int:
    with closing(_connect()) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM user_bookmarks WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]


init_db()
