"""Backup manifest, checksum and retention tests."""

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import backup_data


def _make_sqlite(path: Path, value: str = "ok") -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        conn.execute("INSERT INTO sample VALUES (?)", (value,))


def _fixture(tmp_path: Path):
    users = tmp_path / "users.db"
    geo = tmp_path / "geo"
    geo.mkdir()
    _make_sqlite(users)
    _make_sqlite(geo / "offline_geo.sqlite3")
    _make_sqlite(geo / "vector.mbtiles")
    return users, geo


def test_backup_creates_manifest_and_verifies(tmp_path):
    users, geo = _fixture(tmp_path)
    dest = tmp_path / "backups"

    snapshot = backup_data.create_snapshot(dest, users, geo, keep=2, stamp="20260101T000000Z")

    assert snapshot.name == "backup-20260101T000000Z"
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    paths = {entry["path"] for entry in manifest["files"]}
    assert paths == {"users.db", "geo/offline_geo.sqlite3", "geo/vector.mbtiles"}
    for entry in manifest["files"]:
        assert len(entry["sha256"]) == 64
        assert entry["size"] == (snapshot / entry["path"]).stat().st_size
    assert backup_data.verify_snapshot(snapshot) == []


def test_backup_verify_detects_tampering(tmp_path):
    users, geo = _fixture(tmp_path)
    dest = tmp_path / "backups"
    snapshot = backup_data.create_snapshot(dest, users, geo, stamp="20260101T000000Z")

    target = snapshot / "geo" / "vector.mbtiles"
    with target.open("ab") as handle:
        handle.write(b"tampered")

    errors = backup_data.verify_snapshot(snapshot)
    assert any("size mismatch" in error or "sha256 mismatch" in error for error in errors)


def test_backup_retention_keeps_newest_snapshots(tmp_path):
    users, geo = _fixture(tmp_path)
    dest = tmp_path / "backups"
    for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
        backup_data.create_snapshot(dest, users, geo, keep=2, stamp=stamp)

    assert sorted(path.name for path in dest.glob("backup-*")) == [
        "backup-20260102T000000Z",
        "backup-20260103T000000Z",
    ]
    assert backup_data.latest_snapshot(dest).name == "backup-20260103T000000Z"


def test_backup_records_missing_optional_files(tmp_path):
    users = tmp_path / "users.db"
    _make_sqlite(users)
    geo = tmp_path / "empty-geo"
    geo.mkdir()

    snapshot = backup_data.create_snapshot(tmp_path / "backups", users, geo, stamp="20260101T000000Z")
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))

    assert set(manifest["missing"]) == {
        "geo/offline_geo.sqlite3", "geo/basemap.mbtiles", "geo/vector.mbtiles",
    }
    assert backup_data.verify_snapshot(snapshot) == []
