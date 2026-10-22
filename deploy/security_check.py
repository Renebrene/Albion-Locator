#!/usr/bin/env python3
"""Preflight checks for a LAN/tunnel deployment.

Never prints secret values. Checks the .env file mode, confirms that it is
ignored by Git, and reports which provider keys are configured so the operator
can rotate/restrict them in the provider console.
"""
from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEY_NAMES = ("YANDEX_API_KEY", "YANDEX_SUGGEST_API_KEY", "YANDEX_JS_API_KEY")
RECOMMENDED = (
    "ALBION_COOKIE_SECURE", "ALBION_CSRF", "ALBION_ALLOWED_ORIGINS",
    "ALBION_METRICS_TOKEN", "ALBION_API_RATE_LIMIT",
)


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip("'\"")
    return values


def git_ignored(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            cwd=ROOT, capture_output=True, text=True,
        )
    except OSError:
        return False
    return result.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args(argv)
    env_file = args.env_file
    errors: list[str] = []
    warnings: list[str] = []

    if env_file.is_file():
        mode = stat.S_IMODE(env_file.stat().st_mode)
        if mode & 0o077:
            errors.append(f"{env_file} has mode {mode:o}; expected 600")
        if not git_ignored(env_file):
            errors.append(f"{env_file} is not ignored by Git")
    else:
        warnings.append(f"{env_file} does not exist; relying on process environment")

    values = parse_env(env_file)
    print(f"env file: {env_file} ({'present' if env_file.is_file() else 'missing'})")
    for name in KEY_NAMES:
        print(f"{name}: {'configured' if values.get(name) else 'not configured'}")
    for name in RECOMMENDED:
        if not values.get(name):
            warnings.append(f"{name} is not set explicitly in .env")

    for warning in warnings:
        print(f"WARN: {warning}", file=sys.stderr)
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if errors:
        return 1
    print("security preflight: OK")
    print("Reminder: rotate provider keys and restrict them by domain after any exposure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
