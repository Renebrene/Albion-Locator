"""Atomic install/rollback behavior for the offline update helper."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import update_offline_data


def test_install_staged_replaces_files_and_keeps_rollback(tmp_path):
    geo = tmp_path / "geo"
    staging = tmp_path / "stage"
    geo.mkdir()
    staging.mkdir()
    (geo / "offline_geo.sqlite3").write_text("old-index", encoding="utf-8")
    (geo / "vector.mbtiles").write_text("old-tiles", encoding="utf-8")
    (staging / "offline_geo.sqlite3").write_text("new-index", encoding="utf-8")
    (staging / "vector.mbtiles").write_text("new-tiles", encoding="utf-8")

    result = update_offline_data.install_staged(
        staging, geo, "20260101T000000Z",
        {"offline_geo.sqlite3", "vector.mbtiles"},
    )

    assert (geo / "offline_geo.sqlite3").read_text() == "new-index"
    assert (geo / "vector.mbtiles").read_text() == "new-tiles"
    rollback = Path(result["rollback"])
    assert (rollback / "offline_geo.sqlite3").read_text() == "old-index"
    assert (rollback / "vector.mbtiles").read_text() == "old-tiles"
    assert result["replaced"] == ["offline_geo.sqlite3", "vector.mbtiles"]


def test_install_staged_restores_previous_files_when_replace_fails(tmp_path):
    geo = tmp_path / "geo"
    staging = tmp_path / "stage"
    geo.mkdir()
    staging.mkdir()
    (geo / "offline_geo.sqlite3").write_text("old-index", encoding="utf-8")
    (staging / "offline_geo.sqlite3").write_text("new-index", encoding="utf-8")
    # vector.mbtiles is listed as built but missing from staging, so the second
    # os.replace raises after the first file has already been swapped.
    with pytest.raises(FileNotFoundError):
        update_offline_data.install_staged(
            staging, geo, "20260101T000000Z",
            {"offline_geo.sqlite3", "vector.mbtiles"},
        )
    assert (geo / "offline_geo.sqlite3").read_text() == "old-index"
