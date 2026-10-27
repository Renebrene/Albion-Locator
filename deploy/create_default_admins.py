#!/usr/bin/env python3
"""Create the default administrator accounts, or reset their passwords.

Idempotent: existing logins get the requested password and stay active, missing
logins are created with the admin role. `must_change_password` is cleared so the
password works on the first login.

ВНИМАНИЕ: пароль по умолчанию — короткое общеизвестное слово, а сервис выходит
наружу через туннель. Запускайте только на закрытом контуре и меняйте пароли
перед публикацией (окно «Пользователь» или `--password`).

Usage:
  python deploy/create_default_admins.py
  python deploy/create_default_admins.py --password 'новый-пароль'
  python deploy/create_default_admins.py --logins техотдел,админ
  ALBION_USERS_DB=/var/lib/albion/users.db python deploy/create_default_admins.py
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_USERS_DB = Path(os.getenv("ALBION_USERS_DB", str(ROOT / "src" / "users.db")))
DEFAULT_LOGINS = ("техотдел", "админ", "администратор", "альбион")
DEFAULT_PASSWORD = "альбион"
DEFAULT_NAMES = {
    "техотдел": "Техотдел",
    "админ": "Администратор",
    "администратор": "Администратор",
    "альбион": "АЛЬБИОН",
}


def _load_auth(db_path: Path):
    sys.path.insert(0, str(ROOT / "src"))
    import auth

    auth.DB_PATH = Path(db_path)
    return auth


def ensure_admins(
    db_path: Path | None = None,
    logins: tuple[str, ...] = DEFAULT_LOGINS,
    password: str = DEFAULT_PASSWORD,
    force_password_change: bool = False,
) -> list[dict[str, Any]]:
    """Create or update the given administrator accounts; returns a report.

    The caller owns the environment flag: `force_password_change=False` is passed
    through ALBION_REQUIRE_PASSWORD_CHANGE because auth reads it on every write.
    """
    db_path = Path(db_path or DEFAULT_USERS_DB)
    previous = os.environ.get("ALBION_REQUIRE_PASSWORD_CHANGE")
    os.environ["ALBION_REQUIRE_PASSWORD_CHANGE"] = "1" if force_password_change else "0"
    try:
        auth = _load_auth(db_path)
        auth.init_db()
        existing = {user["login"].casefold(): user for user in auth.list_users()}
        report: list[dict[str, Any]] = []
        for login in logins:
            user = existing.get(login.casefold())
            if user is None:
                created = auth.create_user(
                    login, password, DEFAULT_NAMES.get(login, login), role="admin",
                )
                auth.record_audit(
                    "user_created", actor={"login": "deploy-cli"},
                    target=created["login"],
                    details={"role": "admin", "source": "deploy/create_default_admins.py"},
                )
                report.append({"login": created["login"], "action": "created", "role": "admin"})
                continue
            auth.set_password(user["id"], password)
            extra: list[str] = []
            if user["role"] != "admin":
                auth.update_user(user["id"], role="admin")
                extra.append("role=admin")
            if not user["is_active"]:
                auth.update_user(user["id"], is_active=True)
                extra.append("activated")
            auth.record_audit(
                "user_password_reset", actor={"login": "deploy-cli"},
                target=user["login"],
                details={"source": "deploy/create_default_admins.py", "extra": extra},
            )
            report.append({
                "login": user["login"], "action": "password_reset",
                "role": "admin", "extra": extra,
            })
        return report
    finally:
        if previous is None:
            os.environ.pop("ALBION_REQUIRE_PASSWORD_CHANGE", None)
        else:
            os.environ["ALBION_REQUIRE_PASSWORD_CHANGE"] = previous


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(DEFAULT_USERS_DB),
                        help="путь к SQLite с пользователями (по умолчанию ALBION_USERS_DB)")
    parser.add_argument("--password", default=DEFAULT_PASSWORD,
                        help="пароль для всех перечисленных логинов")
    parser.add_argument("--logins", default=",".join(DEFAULT_LOGINS),
                        help="логины через запятую")
    parser.add_argument("--require-password-change", action="store_true",
                        help="потребовать смену пароля при первом входе")
    args = parser.parse_args()

    logins = tuple(item.strip() for item in args.logins.split(",") if item.strip())
    if not logins:
        print("! не передан ни один логин", file=sys.stderr)
        return 2
    if args.password == DEFAULT_PASSWORD:
        print("! пароль по умолчанию: смените его перед публикацией сервиса", file=sys.stderr)

    report = ensure_admins(
        Path(args.db), logins, args.password, args.require_password_change,
    )
    for item in report:
        suffix = f" ({', '.join(item['extra'])})" if item.get("extra") else ""
        print(f"{item['action']:<15} {item['login']:<16} роль={item['role']}{suffix}")
    print(f"всего: {len(report)}; база: {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
