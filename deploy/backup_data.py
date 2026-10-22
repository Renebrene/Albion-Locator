#!/usr/bin/env python3
"""Create a consistent, checksummed backup of users and offline geodata.

SQLite databases are copied through the SQLite backup API so a live WAL writer
does not produce a torn file. Other files are copied normally. Each snapshot is
staged in a temporary directory and renamed into place only after the manifest
is complete.

Usage:
  python deploy/backup_data.py --dest /var/backups/albion-locator --keep 14
  python deploy/backup_data.py --verify-latest --dest /var/backups/albion-locator
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_USERS_DB = Path(os.getenv("ALBION_USERS_DB", str(ROOT / "src" / "users.db")))
DEFAULT_GEO_DIR = Path(os.getenv(
    "ALBION_GEO_DATA_DIR", str(Path.home() / ".local/share/albion-locator")
))
DEFAULT_BACKUP_DIR = Path(os.getenv(
    "ALBION_BACKUP_DIR", str(Path.home() / "albion-backups")
))
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".mbtiles"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sqlite(path: Path) -> bool:
    if path.suffix.lower() in SQLITE_SUFFIXES:
        return True
    try:
        with path.open("rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def copy_consistent(source: Path, destination: Path) -> None:
    """Copy a file, using SQLite's online backup API for live databases."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if _is_sqlite(source):
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
            with sqlite3.connect(destination) as dst:
                src.backup(dst)
        return
    shutil.copy2(source, destination)


def _add_file(snapshot: Path, source: Path, relative: str,
              manifest: dict[str, Any]) -> None:
    if not source.is_file():
        manifest["missing"].append(relative)
        return
    target = snapshot / relative
    copy_consistent(source, target)
    manifest["files"].append({
        "path": relative,
        "source": str(source),
        "size": target.stat().st_size,
        "sha256": sha256_file(target),
        "source_mtime": source.stat().st_mtime,
    })


def create_snapshot(dest: Path, users_db: Path, geo_dir: Path,
                    keep: int = 14, stamp: str | None = None) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = dest / f"backup-{stamp}"
    if final.exists():
        raise FileExistsError(f"backup already exists: {final}")
    staging = Path(tempfile.mkdtemp(prefix=".backup-", dir=dest))
    manifest: dict[str, Any] = {
        "schema": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": [],
        "missing": [],
    }
    try:
        _add_file(staging, users_db, "users.db", manifest)
        manifest["offline_geo"] = {}
        for name in ("offline_geo.sqlite3", "basemap.mbtiles", "vector.mbtiles"):
            source = geo_dir / name
            _add_file(staging, source, f"geo/{name}", manifest)
            if source.is_file():
                manifest["offline_geo"][name] = {
                    "size": source.stat().st_size,
                    "mtime": source.stat().st_mtime,
                }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _prune(dest, keep)
    return final


def _prune(dest: Path, keep: int) -> None:
    if keep <= 0:
        return
    snapshots = sorted(
        (path for path in dest.glob("backup-*") if path.is_dir()),
        key=lambda path: path.name,
    )
    for old in snapshots[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


def verify_snapshot(snapshot: Path) -> list[str]:
    """Return a list of verification errors; an empty list means success."""
    errors: list[str] = []
    manifest_path = snapshot / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"manifest is unreadable: {exc}"]
    for entry in manifest.get("files", []):
        target = snapshot / entry["path"]
        if not target.is_file():
            errors.append(f"missing file: {entry['path']}")
            continue
        if target.stat().st_size != entry["size"]:
            errors.append(f"size mismatch: {entry['path']}")
            continue
        if sha256_file(target) != entry["sha256"]:
            errors.append(f"sha256 mismatch: {entry['path']}")
            continue
        if _is_sqlite(target):
            try:
                with sqlite3.connect(f"file:{target}?mode=ro", uri=True) as conn:
                    result = conn.execute("PRAGMA integrity_check").fetchone()
                    if not result or result[0] != "ok":
                        errors.append(f"sqlite integrity: {entry['path']}: {result}")
            except sqlite3.Error as exc:
                errors.append(f"sqlite error: {entry['path']}: {exc}")
    for missing in manifest.get("missing", []):
        if (snapshot / missing).exists():
            errors.append(f"unexpected file: {missing}")
    return errors


def latest_snapshot(dest: Path) -> Path | None:
    snapshots = sorted(
        (path for path in dest.glob("backup-*") if path.is_dir()),
        key=lambda path: path.name,
    )
    return snapshots[-1] if snapshots else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, default=DEFAULT_BACKUP_DIR,
                        help="directory for snapshots (default: %(default)s)")
    parser.add_argument("--users-db", type=Path, default=DEFAULT_USERS_DB)
    parser.add_argument("--geo-dir", type=Path, default=DEFAULT_GEO_DIR)
    parser.add_argument("--keep", type=int, default=14,
                        help="number of snapshots to retain; 0 keeps all")
    parser.add_argument("--verify-latest", action="store_true",
                        help="only verify the newest snapshot")
    parser.add_argument("--verify", type=Path,
                        help="verify a specific snapshot directory")
    args = parser.parse_args(argv)

    if args.verify or args.verify_latest:
        snapshot = args.verify or latest_snapshot(args.dest)
        if snapshot is None:
            print(f"no snapshots in {args.dest}", file=sys.stderr)
            return 1
        errors = verify_snapshot(snapshot)
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 1
        print(f"verified {snapshot}")
        return 0

    try:
        snapshot = create_snapshot(
            args.dest, args.users_db, args.geo_dir, keep=args.keep
        )
    except (OSError, sqlite3.Error) as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    print(snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
