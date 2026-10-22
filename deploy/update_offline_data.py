#!/usr/bin/env python3
"""Download/build/verify/swap offline geodata with rollback.

The script never edits the live index or vector tiles in place. It builds both
artifacts in a staging directory, checks SQLite integrity and tile counts, copies
the current live files to a timestamped rollback directory, and only then
atomically replaces them. Any failure restores the previous files.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SRC = ROOT / "packages" / "offline_geo" / "src"
DEFAULT_GEO_DIR = Path(os.getenv(
    "ALBION_GEO_DATA_DIR", str(Path.home() / ".local/share/albion-locator")
))
DEFAULT_URL = "https://download.geofabrik.de/russia/south-fed-district-latest.osm.pbf"
DEFAULT_BOUNDARY = ROOT / "static" / "rostov_boundary.geojson"
DEFAULT_TILEMAKER_CONFIG = ROOT / "deploy" / "vector" / "config.json"
DEFAULT_TILEMAKER_PROCESS = ROOT / "deploy" / "vector" / "process.lua"
LIVE_FILES = ("offline_geo.sqlite3", "vector.mbtiles")


def log(message: str) -> None:
    print(f"[offline-update] {message}", file=sys.stderr, flush=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], *, dry_run: bool = False,
         env: dict[str, str] | None = None) -> None:
    log("$ " + " ".join(command))
    if dry_run:
        return
    subprocess.run(command, check=True, env=env)


def download(url: str, destination: Path, *, dry_run: bool = False) -> None:
    log(f"download {url}")
    if dry_run:
        return
    request = urllib.request.Request(url, headers={"User-Agent": "albion-locator/update"})
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as out:
            expected = response.headers.get("content-length")
            received = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                received += len(chunk)
                if received % (32 * 1024 * 1024) < 1024 * 1024:
                    log(f"  received {received / 1024 / 1024:.0f} MiB")
        if expected and received != int(expected):
            raise RuntimeError(f"incomplete download: {received} of {expected} bytes")
        if received < 1024 * 1024:
            raise RuntimeError("downloaded PBF is unexpectedly small")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def write_poly(boundary: Path, destination: Path) -> None:
    sys.path.insert(0, str(PACKAGE_SRC))
    from offline_geo.osm_import import _poly_text

    destination.write_text(_poly_text(boundary), encoding="utf-8")


def sqlite_check(path: Path, table: str) -> None:
    if not path.is_file() or path.stat().st_size < 1024:
        raise RuntimeError(f"staged file is missing or empty: {path}")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"SQLite integrity check failed for {path}: {result}")
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if count <= 0:
        raise RuntimeError(f"table {table!r} is empty in {path}")


def copy_rollback(geo_dir: Path, stamp: str) -> Path | None:
    existing = [geo_dir / name for name in LIVE_FILES if (geo_dir / name).is_file()]
    if not existing:
        return None
    target = geo_dir / "update-backups" / stamp
    target.mkdir(parents=True, exist_ok=False)
    for source in existing:
        shutil.copy2(source, target / source.name)
    return target


def _prune_backups(geo_dir: Path, keep: int) -> None:
    if keep <= 0:
        return
    root = geo_dir / "update-backups"
    if not root.is_dir():
        return
    backups = sorted((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.name)
    for old in backups[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


def install_staged(staging: Path, geo_dir: Path, stamp: str,
                   built: set[str]) -> dict[str, Any]:
    rollback = copy_rollback(geo_dir, stamp)
    replaced: list[str] = []
    try:
        for name in sorted(built):
            staged = staging / name
            live = geo_dir / name
            os.replace(staged, live)
            replaced.append(name)
    except BaseException:
        if rollback:
            for name in replaced:
                backup = rollback / name
                if backup.is_file():
                    shutil.copy2(backup, geo_dir / name)
        raise
    return {
        "rollback": str(rollback) if rollback else None,
        "replaced": replaced,
    }


def build_index(staging: Path, pbf: Path, boundary: Path,
                source_date: str, *, dry_run: bool) -> dict[str, Any]:
    database = staging / "offline_geo.sqlite3"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PACKAGE_SRC) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    _run([
        sys.executable, "-m", "offline_geo", "import-osm-pbf",
        "--pbf", str(pbf), "--boundary", str(boundary),
        "--database", str(database), "--source-date", source_date,
    ], dry_run=dry_run, env=env)
    if not dry_run:
        sqlite_check(database, "metadata")
    return {"database": str(database)}


def build_vector(staging: Path, pbf: Path, *, dry_run: bool) -> dict[str, Any]:
    output = staging / "vector.mbtiles"
    _run([
        "tilemaker", "--input", str(pbf), "--output", str(output),
        "--config", str(DEFAULT_TILEMAKER_CONFIG),
        "--process", str(DEFAULT_TILEMAKER_PROCESS),
    ], dry_run=dry_run)
    if not dry_run:
        sqlite_check(output, "tiles")
    return {"vector": str(output)}


def update(args: argparse.Namespace) -> dict[str, Any]:
    geo_dir: Path = args.geo_dir.expanduser()
    geo_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    source_date = args.source_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lock_path = geo_dir / ".update.lock"
    lock_handle = lock_path.open("w")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_handle.close()
        raise RuntimeError("another offline data update is already running")

    staging = Path(tempfile.mkdtemp(prefix=".update-", dir=geo_dir))
    built: set[str] = set()
    try:
        source = args.pbf.expanduser().resolve() if args.pbf else staging / "source.osm.pbf"
        if args.pbf:
            if not args.pbf.is_file():
                raise RuntimeError(f"PBF does not exist: {args.pbf}")
        else:
            download(args.url, source, dry_run=args.dry_run)

        clipped = staging / "rostov.osm.pbf"
        poly = staging / "rostov.poly"
        write_poly(args.boundary, poly)
        _run([
            "osmium", "extract", "--polygon", str(poly),
            "--strategy", "complete_ways", "--overwrite",
            "--output", str(clipped), str(source),
        ], dry_run=args.dry_run)

        if not args.skip_index:
            build_index(staging, clipped, args.boundary, source_date, dry_run=args.dry_run)
            built.add("offline_geo.sqlite3")
        if not args.skip_vector:
            build_vector(staging, clipped, dry_run=args.dry_run)
            built.add("vector.mbtiles")
        if not built:
            raise RuntimeError("nothing to build: both --skip-index and --skip-vector were set")
        if args.dry_run:
            return {"dry_run": True, "built": sorted(built)}

        manifest = {
            "schema": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "version": f"osm-{source_date}",
            "source": args.url if not args.pbf else str(args.pbf),
            "source_date": source_date,
            "boundary": str(args.boundary),
            "files": {
                name: {
                    "sha256": sha256_file(staging / name),
                    "size": (staging / name).stat().st_size,
                }
                for name in built
            },
        }
        installed = install_staged(staging, geo_dir, stamp, built)
        manifest["rollback"] = installed["rollback"]
        manifest["replaced"] = installed["replaced"]
        manifest_path = geo_dir / "update-manifest.json"
        temporary_manifest = manifest_path.with_suffix(".json.tmp")
        temporary_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_manifest, manifest_path)
        _prune_backups(geo_dir, args.keep_backups)
        return manifest
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        lock_handle.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--geo-dir", type=Path, default=DEFAULT_GEO_DIR)
    parser.add_argument("--pbf", type=Path,
                        help="use an existing regional PBF instead of downloading")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--boundary", type=Path, default=DEFAULT_BOUNDARY)
    parser.add_argument("--source-date", help="YYYY-MM-DD recorded in the index manifest")
    parser.add_argument("--keep-backups", type=int, default=3)
    parser.add_argument("--skip-index", action="store_true")
    parser.add_argument("--skip-vector", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = update(args)
    except (OSError, RuntimeError, subprocess.CalledProcessError, urllib.error.URLError) as exc:
        log(f"FAILED: {exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
