"""Offline SQLite index construction and local search APIs.

The module only consumes prepared local records. It never fetches data or parses PBF.
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import sqlite3
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping

from .schema import PACKAGE_VERSION, SCHEMA_SQL, SCHEMA_VERSION

EARTH_RADIUS_M = 6_371_008.8
MAX_LIMIT = 100


def default_database_path() -> Path:
    """Return the conventional database path without creating it."""
    data_dir = os.environ.get("ALBION_GEO_DATA_DIR")
    if data_dir:
        return Path(data_dir).expanduser() / "offline_geo.sqlite3"
    return Path.home() / ".local" / "share" / "albion-locator" / "offline_geo.sqlite3"


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(text.split())


def parse_kilometer(value: Any) -> float | None:
    """Parse numeric kilometres or chainage strings such as ``12+345``."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        text = str(value).strip().replace(",", ".")
        match = re.fullmatch(r"([+-]?\d+)\s*\+\s*(\d{1,3}(?:[.,]\d+)?)", text)
        if match:
            result = float(match.group(1)) + float(match.group(2).replace(",", ".")) / 1000.0
        else:
            try:
                result = float(text)
            except ValueError as exc:
                raise ValueError(f"invalid kilometer value: {value!r}") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError("kilometer value must be finite and non-negative")
    return result


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("limit must be an integer")
    return min(MAX_LIMIT, max(1, value))


def _stable_id(kind: str, explicit: Any, *parts: Any) -> str:
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    # Unit-separator avoids ambiguous concatenation while keeping stable readable IDs.
    raw = "\x1f".join([kind, *(str(part or "") for part in parts)])
    import hashlib
    return f"{kind}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def _coordinates(value: Any) -> list[list[float]] | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, Mapping):
        if value.get("type") != "LineString":
            raise ValueError("road geometry must be a GeoJSON LineString")
        value = value.get("coordinates")
    if not isinstance(value, (list, tuple)):
        raise ValueError("road geometry must be a coordinate sequence")
    # Allow a GeoJSON feature geometry directly.
    coords: list[list[float]] = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise ValueError("each road coordinate must contain longitude and latitude")
        lon, lat = float(point[0]), float(point[1])
        if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("road coordinates must be finite WGS84 longitude/latitude")
        coords.append([lon, lat])
    if len(coords) < 2:
        raise ValueError("road LineString must contain at least two coordinates")
    return coords


def _record_geometry(record: Mapping[str, Any]) -> list[list[float]] | None:
    geometry = record.get("geometry", record.get("geometry_json", record.get("coordinates")))
    if isinstance(geometry, Mapping) and geometry.get("type") == "Feature":
        geometry = geometry.get("geometry")
    return _coordinates(geometry)


def _read_records(path: str | os.PathLike[str] | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    if suffix == ".csv":
        with file_path.open("r", encoding="utf-8-sig", newline="") as stream:
            return [dict(row) for row in csv.DictReader(stream)]
    if suffix in {".json", ".geojson"}:
        with file_path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if isinstance(data, list):
            features = data
        elif isinstance(data, dict) and data.get("type") == "FeatureCollection":
            features = data.get("features", [])
        elif isinstance(data, dict) and data.get("type") == "Feature":
            features = [data]
        elif isinstance(data, dict):
            return [data]
        else:
            raise ValueError(f"unsupported normalized input in {file_path}")
        records = []
        for item in features:
            if not isinstance(item, dict):
                raise ValueError("GeoJSON records must be objects")
            props = dict(item.get("properties") or {})
            if item.get("id") is not None:
                props.setdefault("id", item["id"])
            if item.get("geometry") is not None:
                props.setdefault("geometry", item["geometry"])
            records.append(props)
        return records
    raise ValueError(f"unsupported input format {suffix!r}; use CSV or GeoJSON")


def _resolve_identifier(conn: sqlite3.Connection, table: str, explicit: Any, name: str,
                        parent_col: str | None = None, parent_id: str | None = None,
                        parent_name: str | None = None) -> str:
    if explicit is not None and str(explicit).strip():
        value = str(explicit).strip()
        found = conn.execute(f"SELECT id FROM {table} WHERE id=?", (value,)).fetchone()
        if found:
            return str(found[0])
        # An explicit ID can denote a parent not yet imported; preserve it as FK target
        # only if the expected parent table already contains it.
        raise ValueError(f"unknown {table[:-1]} id {value!r}")
    if parent_col and parent_id:
        row = conn.execute(
            f"SELECT id FROM {table} WHERE {parent_col}=? AND normalized_name=? ORDER BY name LIMIT 1",
            (parent_id, normalize_text(name)),
        ).fetchone()
    elif parent_col and parent_name:
        row = conn.execute(
            f"SELECT t.id FROM {table} t JOIN settlements s ON s.id=t.settlement_id "
            "WHERE s.normalized_name=? AND t.normalized_name=? ORDER BY t.name LIMIT 1",
            (normalize_text(parent_name), normalize_text(name)),
        ).fetchone()
    else:
        row = conn.execute(f"SELECT id FROM {table} WHERE normalized_name=? ORDER BY name LIMIT 1",
                           (normalize_text(name),)).fetchone()
    if row:
        return str(row[0])
    raise ValueError(f"cannot resolve {table[:-1]} {name!r}")


def _load_inputs(conn: sqlite3.Connection, settlements: Iterable[Mapping[str, Any]],
                 streets: Iterable[Mapping[str, Any]], houses: Iterable[Mapping[str, Any]],
                 roads: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = {"settlements": 0, "streets": 0, "houses": 0, "roads": 0}
    for record in settlements:
        name = str(record.get("name", "")).strip()
        if not name:
            raise ValueError("settlement name is required")
        ident = _stable_id("settlement", record.get("id"), normalize_text(name))
        conn.execute("INSERT INTO settlements(id,name,normalized_name) VALUES(?,?,?)",
                     (ident, name, normalize_text(name)))
        counts["settlements"] += 1
    for record in streets:
        name = str(record.get("name", "")).strip()
        if not name:
            raise ValueError("street name is required")
        settlement_value = record.get("settlement_id")
        if settlement_value:
            settlement_id = _resolve_identifier(conn, "settlements", settlement_value, "")
        else:
            settlement_name = record.get("settlement", record.get("settlement_name"))
            if not settlement_name:
                raise ValueError("street requires settlement_id or settlement name")
            settlement_id = _resolve_identifier(conn, "settlements", None, str(settlement_name))
        ident = _stable_id("street", record.get("id"), settlement_id, normalize_text(name))
        conn.execute("INSERT INTO streets(id,settlement_id,name,normalized_name) VALUES(?,?,?,?)",
                     (ident, settlement_id, name, normalize_text(name)))
        counts["streets"] += 1
    for record in houses:
        number = str(record.get("house_number", record.get("number", record.get("name", "")))).strip()
        if not number:
            raise ValueError("house_number is required")
        street_value = record.get("street_id")
        if street_value:
            street_id = _resolve_identifier(conn, "streets", street_value, "")
        else:
            street_name = record.get("street", record.get("street_name"))
            if not street_name:
                raise ValueError("house requires street_id or street name")
            settlement = record.get("settlement", record.get("settlement_name"))
            street_id = _resolve_identifier(conn, "streets", None, str(street_name),
                                             parent_col="settlement_id", parent_name=str(settlement) if settlement else None)
        lon, lat = record.get("lon", record.get("longitude")), record.get("lat", record.get("latitude"))
        if lon in (None, "") or lat in (None, ""):
            geometry = record.get("geometry")
            if isinstance(geometry, str):
                geometry = json.loads(geometry)
            if isinstance(geometry, Mapping):
                geometry = geometry.get("coordinates")
            if isinstance(geometry, (list, tuple)) and len(geometry) >= 2:
                lon, lat = geometry[0], geometry[1]
        if (lon in (None, "")) != (lat in (None, "")):
            raise ValueError("house coordinates require both lon and lat")
        if lon not in (None, ""):
            lon, lat = float(lon), float(lat)
            if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError("house coordinates must be finite WGS84 longitude/latitude")
        ident = _stable_id("house", record.get("id"), street_id, normalize_text(number))
        precision = str(record.get("location_precision") or "unknown")
        assignment = str(record.get("settlement_assignment") or "unknown")
        conn.execute("""INSERT INTO houses(id,street_id,house_number,normalized_house_number,lon,lat,
                       location_precision,settlement_assignment) VALUES(?,?,?,?,?,?,?,?)""",
                     (ident, street_id, number, normalize_text(number), lon, lat, precision, assignment))
        counts["houses"] += 1
    for record in roads:
        name = str(record.get("name", "")).strip() or None
        ref = str(record.get("ref", record.get("road_ref", ""))).strip() or None
        if not name and not ref:
            raise ValueError("road requires a name or ref")
        start = parse_kilometer(record.get("km_start", record.get("start_km")))
        end = parse_kilometer(record.get("km_end", record.get("end_km")))
        geometry = _record_geometry(record)
        provenance = str(record.get("km_provenance") or record.get("provenance") or "estimated from geometry")
        if provenance not in {
            "official start/finish",
            "estimated from geometry",
            "no authoritative chainage; geometry length only",
        }:
            raise ValueError("unsupported km_provenance value")
        source = str(record.get("source") or "unknown")
        ident = _stable_id("road", record.get("id"), normalize_text(ref), normalize_text(name),
                           start, end, json.dumps(geometry, separators=(",", ":"), ensure_ascii=False))
        source_ids = record.get("source_ids")
        attributes = record.get("attributes", record.get("lengths_m"))
        conn.execute("""INSERT INTO roads(id,name,normalized_name,ref,normalized_ref,km_start,km_end,
                       geometry_json,source,km_provenance,source_ids_json,attributes_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (ident, name, normalize_text(name), ref, normalize_text(ref), start, end,
                      json.dumps(geometry, separators=(",", ":")) if geometry else None, source, provenance,
                      json.dumps(source_ids, ensure_ascii=False, sort_keys=True) if source_ids is not None else None,
                      json.dumps(attributes, ensure_ascii=False, sort_keys=True) if attributes is not None else None))
        if geometry:
            for segment_index, (first, second) in enumerate(zip(geometry, geometry[1:])):
                cursor = conn.execute(
                    "INSERT INTO road_segments"
                    "(road_id,segment_index,lon1,lat1,lon2,lat2) VALUES(?,?,?,?,?,?)",
                    (ident, segment_index, first[0], first[1], second[0], second[1]),
                )
                conn.execute(
                    "INSERT INTO road_segments_rtree"
                    "(id,min_lon,max_lon,min_lat,max_lat) VALUES(?,?,?,?,?)",
                    (
                        cursor.lastrowid,
                        min(first[0], second[0]),
                        max(first[0], second[0]),
                        min(first[1], second[1]),
                        max(first[1], second[1]),
                    ),
                )
        counts["roads"] += 1
    return counts


def initialize_database(path: str | os.PathLike[str], *, manifest: Mapping[str, Any] | None = None) -> Path:
    """Create an empty schema at *path*, refusing to overwrite an existing database."""
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    conn = sqlite3.connect(destination)
    try:
        conn.executescript(SCHEMA_SQL)
        metadata = _metadata(manifest, {"record_counts": {"settlements": 0, "streets": 0, "houses": 0, "roads": 0}})
        conn.executemany("INSERT INTO metadata(key,value) VALUES(?,?)",
                         [(key, json.dumps(value, ensure_ascii=False, sort_keys=True)) for key, value in metadata.items()])
        conn.commit()
    finally:
        conn.close()
    return destination


def _metadata(manifest: Mapping[str, Any] | None, extra: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "package_version": PACKAGE_VERSION,
        "created_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "data_license_notice": "Verify source-specific rights before distributing any populated index.",
    }
    if manifest:
        result.update(dict(manifest))
    result.update(extra)
    return result


def build_index(path: str | os.PathLike[str], *, settlements: Iterable[Mapping[str, Any]] = (),
                streets: Iterable[Mapping[str, Any]] = (), houses: Iterable[Mapping[str, Any]] = (),
                roads: Iterable[Mapping[str, Any]] = (), manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Atomically rebuild an index from normalized in-memory records.

    The new SQLite database is built beside the destination, validated, then swapped in
    with ``os.replace``. Existing readers keep their old inode until they close it.
    """
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, stage_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".staging", dir=destination.parent)
    os.close(fd)
    stage = Path(stage_name)
    try:
        conn = sqlite3.connect(stage)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA_SQL)
            conn.execute("BEGIN")
            counts = _load_inputs(conn, settlements, streets, houses, roads)
            metadata = _metadata(manifest, {"record_counts": counts})
            conn.executemany("INSERT INTO metadata(key,value) VALUES(?,?)",
                             [(key, json.dumps(value, ensure_ascii=False, sort_keys=True)) for key, value in metadata.items()])
            conn.commit()
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
            if integrity != "ok" or foreign_key_errors:
                raise sqlite3.DatabaseError(f"staged database failed validation: {integrity}; {foreign_key_errors[:3]}")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        os.replace(stage, destination)
        return metadata
    finally:
        try:
            stage.unlink()
        except FileNotFoundError:
            pass


def build_from_files(path: str | os.PathLike[str], *, settlements: str | os.PathLike[str] | None = None,
                     streets: str | os.PathLike[str] | None = None, houses: str | os.PathLike[str] | None = None,
                     roads: str | os.PathLike[str] | None = None,
                     manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Atomically build an index from prepared CSV or GeoJSON files."""
    return build_index(path, settlements=_read_records(settlements), streets=_read_records(streets),
                       houses=_read_records(houses), roads=_read_records(roads), manifest=manifest)


def _fts_expression(query: str) -> str:
    tokens = re.findall(r"[^\W_]+", normalize_text(query), flags=re.UNICODE)
    return " AND ".join('"' + token.replace('"', '""') + '"*' for token in tokens)


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, a)
    lon2, lat2 = map(math.radians, b)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def _interpolate(coords: list[list[float]], fraction: float) -> tuple[float, float] | None:
    lengths = [_haversine(tuple(a), tuple(b)) for a, b in zip(coords, coords[1:])]
    total = sum(lengths)
    if not total:
        return tuple(coords[0])
    target = min(1.0, max(0.0, fraction)) * total
    traversed = 0.0
    for index, (a, b, length) in enumerate(zip(coords, coords[1:], lengths)):
        if target <= traversed + length or index == len(lengths) - 1:
            ratio = 0.0 if not length else (target - traversed) / length
            return (a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio)
        traversed += length
    return tuple(coords[-1])


class GeoIndex:
    """Read-only local search API backed by the standalone SQLite database."""

    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path).expanduser() if path is not None else default_database_path()
        self.path = self.path.resolve()
        if not self.path.is_file():
            raise FileNotFoundError(f"offline geodata index not found: {self.path}")
        # URI read-only mode prevents accidental creation or modification by API clients.
        self._conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row
        version = self._conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        # Schema 4 has no road_segments_rtree; it remains readable via the
        # linear fallback. Rebuild to get the accelerated nearest-road path.
        schema_version = json.loads(version[0]) if version else None
        if schema_version not in {4, SCHEMA_VERSION}:
            self._conn.close()
            raise ValueError("unsupported or missing offline geodata schema version")
        self._has_segment_rtree = bool(self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='road_segments_rtree'"
        ).fetchone())

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "GeoIndex":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @property
    def manifest(self) -> dict[str, Any]:
        return {row["key"]: json.loads(row["value"]) for row in self._conn.execute("SELECT key,value FROM metadata")}

    def _find_context_id(self, table: str, explicit: str | None, name: str | None,
                         parent_id: str | None = None) -> str | None:
        if explicit:
            row = self._conn.execute(f"SELECT id FROM {table} WHERE id=?", (str(explicit),)).fetchone()
            if row:
                return str(row[0])
            # permit callers to use a unique exact visible name instead of an ID
            name = str(explicit)
        if name:
            query = f"SELECT id FROM {table} WHERE normalized_name=?"
            args: list[Any] = [normalize_text(name)]
            if table == "streets" and parent_id:
                query += " AND settlement_id=?"
                args.append(parent_id)
            query += " ORDER BY name,id LIMIT 1"
            row = self._conn.execute(query, args).fetchone()
            return str(row[0]) if row else None
        return None

    def suggest_context(self, query: str = "", *, settlement: str | None = None,
                        street: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """Suggest settlements, then streets within a settlement, then houses."""
        limit = _limit(limit)
        q = normalize_text(query)
        if street is not None:
            settlement_id = self._find_context_id("settlements", settlement, None)
            if not settlement_id:
                return []
            street_id = self._find_context_id("streets", street, None, settlement_id)
            if not street_id:
                return []
            rows = self._conn.execute("""SELECT h.id,h.house_number,h.street_id,s.settlement_id,
                    s.name AS street_name,sett.name AS settlement_name,h.lon,h.lat,h.location_precision,h.settlement_assignment
                    FROM houses h JOIN streets s ON s.id=h.street_id
                    JOIN settlements sett ON sett.id=s.settlement_id
                    WHERE h.street_id=? AND h.normalized_house_number LIKE ?
                    ORDER BY h.normalized_house_number,h.house_number LIMIT ?""",
                    (street_id, f"%{q}%", limit)).fetchall()
            return [dict(type="house", id=r["id"], name=r["house_number"], settlement_id=r["settlement_id"],
                         street_id=r["street_id"], house_number=r["house_number"], lon=r["lon"], lat=r["lat"],
                         location_precision=r["location_precision"],
                         settlement_assignment=r["settlement_assignment"],
                         settlement=r["settlement_name"], street=r["street_name"]) for r in rows]
        if settlement is not None:
            settlement_id = self._find_context_id("settlements", settlement, None)
            if not settlement_id:
                return []
            if q and _fts_expression(q):
                rows = self._conn.execute("""SELECT id,name,settlement_id FROM streets
                        WHERE settlement_id=? AND rowid IN (
                            SELECT rowid FROM streets_fts WHERE streets_fts MATCH ?
                        ) ORDER BY normalized_name,name LIMIT ?""",
                        (settlement_id, _fts_expression(q), limit)).fetchall()
            else:
                rows = self._conn.execute("""SELECT id,name,settlement_id FROM streets
                        WHERE settlement_id=? ORDER BY normalized_name,name LIMIT ?""",
                        (settlement_id, limit)).fetchall()
            return [dict(type="street", id=r["id"], name=r["name"], settlement_id=r["settlement_id"],
                         street_id=r["id"], house_number=None, lon=None, lat=None) for r in rows]
        if q and _fts_expression(q):
            rows = self._conn.execute("""SELECT id,name FROM settlements
                    WHERE rowid IN (SELECT rowid FROM settlements_fts WHERE settlements_fts MATCH ?)
                    ORDER BY normalized_name,name LIMIT ?""",
                    (_fts_expression(q), limit)).fetchall()
        else:
            rows = self._conn.execute("SELECT id,name FROM settlements ORDER BY normalized_name,name LIMIT ?",
                                      (limit,)).fetchall()
        return [dict(type="settlement", id=r["id"], name=r["name"], settlement_id=r["id"],
                     street_id=None, house_number=None, lon=None, lat=None) for r in rows]

    def search_roads(self, query: str | None = None, *, ref: str | None = None,
                     km: Any = None, limit: int = 20) -> list[dict[str, Any]]:
        """Search road names/references and optionally select a chainage location."""
        limit = _limit(limit)
        kilometer = parse_kilometer(km)
        clauses: list[str] = []
        args: list[Any] = []
        if query and query.strip():
            expression = _fts_expression(query)
            if expression:
                clauses.append("rowid IN (SELECT rowid FROM roads_fts WHERE roads_fts MATCH ?)")
                args.append(expression)
        if ref and ref.strip():
            expression = _fts_expression(ref)
            if expression:
                clauses.append("rowid IN (SELECT rowid FROM roads_fts WHERE roads_fts MATCH ?)")
                args.append(expression)
        if kilometer is not None:
            clauses.append("km_start IS NOT NULL AND km_end IS NOT NULL AND "
                           "? BETWEEN min(km_start,km_end) AND max(km_start,km_end)")
            args.append(kilometer)
        sql = "SELECT * FROM roads" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        sql += " ORDER BY normalized_ref,normalized_name,id LIMIT ?"
        args.append(limit)
        results = []
        for row in self._conn.execute(sql, args):
            coords = json.loads(row["geometry_json"]) if row["geometry_json"] else None
            selected = None
            if kilometer is not None and row["km_start"] is not None and row["km_end"] is not None:
                denom = row["km_end"] - row["km_start"]
                if coords and denom:
                    selected = _interpolate(coords, (kilometer - row["km_start"]) / denom)
                elif coords:
                    selected = tuple(coords[0])
            elif kilometer is None and coords:
                selected = _interpolate(coords, 0.5)
            results.append(self._road_result(row, coords, selected_km=kilometer if kilometer is not None else None,
                                             selected=selected))
        return results

    def _road_result(self, row: sqlite3.Row, coords: list[list[float]] | None, *,
                     selected_km: float | None = None, selected: tuple[float, float] | None = None) -> dict[str, Any]:
        return {"id": row["id"], "name": row["name"], "ref": row["ref"],
                "km_start": row["km_start"], "km_end": row["km_end"], "geometry": coords,
                "source": row["source"], "km_provenance": row["km_provenance"],
                "source_ids": json.loads(row["source_ids_json"]) if row["source_ids_json"] else None,
                "attributes": json.loads(row["attributes_json"]) if row["attributes_json"] else None,
                "selected_km": selected_km,
                "selected_lon": selected[0] if selected else None,
                "selected_lat": selected[1] if selected else None}

    @staticmethod
    def _segment_metrics(lon: float, lat: float, cos_lat: float,
                         first: list[float], second: list[float]) -> tuple[float, float, float, float]:
        """Return exact-ish distance and snap point for one segment."""
        ax = math.radians(first[0] - lon) * EARTH_RADIUS_M * cos_lat
        ay = math.radians(first[1] - lat) * EARTH_RADIUS_M
        bx = math.radians(second[0] - lon) * EARTH_RADIUS_M * cos_lat
        by = math.radians(second[1] - lat) * EARTH_RADIUS_M
        dx, dy = bx - ax, by - ay
        denom = dx * dx + dy * dy
        fraction = 0.0 if denom == 0 else min(1.0, max(0.0, -(ax * dx + ay * dy) / denom))
        snapped_lon = first[0] + (second[0] - first[0]) * fraction
        snapped_lat = first[1] + (second[1] - first[1]) * fraction
        return _haversine((lon, lat), (snapped_lon, snapped_lat)), fraction, snapped_lon, snapped_lat

    def _nearest_road_linear(self, lon: float, lat: float):
        cos_lat = max(1e-8, math.cos(math.radians(lat)))
        best = None
        for row in self._conn.execute("SELECT * FROM roads WHERE geometry_json IS NOT NULL"):
            coords = json.loads(row["geometry_json"])
            for index, (first, second) in enumerate(zip(coords, coords[1:])):
                distance, fraction, snapped_lon, snapped_lat = self._segment_metrics(
                    lon, lat, cos_lat, first, second
                )
                if best is None or distance < best[0]:
                    best = (distance, row, coords, index, fraction, snapped_lon, snapped_lat)
        return best

    def _nearest_road_rtree(self, lon: float, lat: float, max_search_m: float | None = None):
        """BBox prefilter, then exact projection on candidate segments."""
        cos_lat = max(1e-8, math.cos(math.radians(lat)))
        limit_m = 100_000.0 if max_search_m is None else max(1.0, float(max_search_m))
        radius_m = min(2_000.0, limit_m)
        while radius_m <= limit_m:
            delta_lat = radius_m / 111_320.0
            delta_lon = radius_m / (111_320.0 * cos_lat)
            rows = self._conn.execute(
                "SELECT s.id, s.road_id, s.segment_index, s.lon1, s.lat1, s.lon2, s.lat2 "
                "FROM road_segments_rtree r JOIN road_segments s ON s.id = r.id "
                "WHERE r.max_lon >= ? AND r.min_lon <= ? "
                "AND r.max_lat >= ? AND r.min_lat <= ?",
                (lon - delta_lon, lon + delta_lon, lat - delta_lat, lat + delta_lat),
            )
            best = None
            for segment in rows:
                first = [segment["lon1"], segment["lat1"]]
                second = [segment["lon2"], segment["lat2"]]
                distance, fraction, snapped_lon, snapped_lat = self._segment_metrics(
                    lon, lat, cos_lat, first, second
                )
                if best is None or distance < best[0]:
                    best = (distance, segment["road_id"], segment["segment_index"],
                            fraction, snapped_lon, snapped_lat)
            if best is not None:
                row = self._conn.execute(
                    "SELECT * FROM roads WHERE id = ?", (best[1],)
                ).fetchone()
                if row is not None:
                    coords = json.loads(row["geometry_json"])
                    return (best[0], row, coords, best[2], best[3], best[4], best[5])
            radius_m *= 2
        return None

    def nearest_road(self, lon: float, lat: float, *, max_distance_m: float | None = None) -> dict[str, Any] | None:
        """Project a WGS84 point onto the nearest indexed road segment."""
        lon, lat = float(lon), float(lat)
        if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("query point must be finite WGS84 longitude/latitude")
        if max_distance_m is not None and (not math.isfinite(max_distance_m) or max_distance_m < 0):
            raise ValueError("max_distance_m must be finite and non-negative")
        if self._has_segment_rtree:
            best = self._nearest_road_rtree(lon, lat, max_distance_m)
            # При явном лимите отсутствие кандидата в радиусе уже означает
            # корректный результат None; линейный fallback нужен только без лимита.
            if best is None and max_distance_m is not None:
                return None
        else:
            best = None
        if best is None:
            best = self._nearest_road_linear(lon, lat)
        if best is None:
            return None
        distance, row, coords, segment_index, fraction, snapped_lon, snapped_lat = best
        if max_distance_m is not None and distance > max_distance_m:
            return None
        km = None
        if row["km_start"] is not None and row["km_end"] is not None:
            lengths = [_haversine(tuple(a), tuple(b)) for a, b in zip(coords, coords[1:])]
            total = sum(lengths)
            progress = (sum(lengths[:segment_index]) + lengths[segment_index] * fraction) / total if total else 0.0
            km = row["km_start"] + (row["km_end"] - row["km_start"]) * progress
        result = self._road_result(row, coords, selected_km=km, selected=(snapped_lon, snapped_lat))
        result.update(distance_m=distance, snapped_lon=snapped_lon, snapped_lat=snapped_lat,
                      segment_index=segment_index, fraction=fraction, km=km)
        return result
