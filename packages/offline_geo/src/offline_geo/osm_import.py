"""Optional OSM PBF import helpers for the offline geo index.

The PBF parser is imported lazily; core search remains stdlib-only. This module
never downloads data. It can clip a user-provided regional extract with the
installed ``osmium`` CLI and then parse the clipped PBF using pyosmium.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from .index import build_index, normalize_text

EARTH_RADIUS_M = 6_371_008.8
PLACE_TYPES = {"city", "town", "village", "hamlet", "isolated_dwelling", "suburb", "neighbourhood"}
ROAD_TYPES = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
    "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
    "residential", "living_street", "service", "road", "track",
}
STREET_TYPES = {
    "primary", "primary_link", "secondary", "secondary_link", "tertiary",
    "tertiary_link", "unclassified", "residential", "living_street", "service",
}
MAX_LOCALITY_DISTANCE_M = 20_000.0


def _display_name(tags: Mapping[str, str]) -> str | None:
    return (tags.get("name:ru") or tags.get("name") or tags.get("official_name") or "").strip() or None


def _stable_osm_id(kind: str, osm_id: int) -> str:
    return f"osm-{kind}:{osm_id}"


def _safe_coordinates(points: Iterable[Any]) -> list[list[float]]:
    result = []
    for point in points:
        try:
            lon, lat = float(point.lon), float(point.lat)
        except (AttributeError, TypeError, ValueError):
            continue
        if math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90:
            result.append([lon, lat])
    return result


def _centroid(coords: list[list[float]]) -> tuple[float, float] | None:
    if not coords:
        return None
    # For small clipped regional polygons/ways, the vertex mean is a bounded
    # address/building representative point, not a surveyed entrance coordinate.
    points = coords[:-1] if len(coords) > 1 and coords[0] == coords[-1] else coords
    if not points:
        return None
    return (sum(point[0] for point in points) / len(points),
            sum(point[1] for point in points) / len(points))


def _line_length_m(coords: list[list[float]]) -> float:
    total = 0.0
    for first, second in zip(coords, coords[1:]):
        lon1, lat1 = map(math.radians, first)
        lon2, lat2 = map(math.radians, second)
        dlat, dlon = lat2 - lat1, lon2 - lon1
        hav = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        total += 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(hav)))
    return total


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, a)
    lon2, lat2 = map(math.radians, b)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    hav = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(hav)))


def _settlement_for(name: str | None, location: tuple[float, float] | None,
                    place_by_name: dict[str, list[dict[str, Any]]], places: list[dict[str, Any]]) -> dict[str, Any] | None:
    if name:
        candidates = place_by_name.get(normalize_text(name), [])
        if candidates:
            return min(candidates, key=lambda item: _distance_m(location, item["point"]) if location else 0)
    if location and places:
        nearest = min(places, key=lambda item: _distance_m(location, item["point"]))
        if _distance_m(location, nearest["point"]) <= MAX_LOCALITY_DISTANCE_M:
            return nearest
    return None


def _poly_text(boundary_geojson: str | os.PathLike[str]) -> str:
    path = Path(boundary_geojson)
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("type") == "Feature":
        geometry = data.get("geometry") or {}
    elif data.get("type") == "FeatureCollection":
        features = data.get("features") or []
        if len(features) != 1:
            raise ValueError("boundary GeoJSON FeatureCollection must contain exactly one feature")
        geometry = features[0].get("geometry") or {}
    else:
        geometry = data
    if geometry.get("type") != "Polygon":
        raise ValueError("boundary must be a single Polygon GeoJSON; MultiPolygon/hole handling is not implicit")
    rings = geometry.get("coordinates") or []
    if len(rings) != 1:
        raise ValueError("boundary polygon holes are not supported by the simple .poly writer")
    ring = rings[0]
    if len(ring) < 4:
        raise ValueError("boundary ring has too few coordinates")
    lines = ["rostov_oblast", "1"]
    for point in ring:
        if len(point) < 2:
            raise ValueError("invalid boundary coordinate")
        lon, lat = float(point[0]), float(point[1])
        if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("boundary coordinates must be WGS84 longitude/latitude")
        lines.append(f"  {lon:.8f}  {lat:.8f}")
    if ring[0] != ring[-1]:
        lines.append(f"  {float(ring[0][0]):.8f}  {float(ring[0][1]):.8f}")
    lines.extend(["END", "END", ""])
    return "\n".join(lines)


class _OSMCollect:
    """Collect a compact, normalized first-pass extract from a clipped PBF."""

    def __init__(self):
        self.places: list[dict[str, Any]] = []
        self.address_objects: list[dict[str, Any]] = []
        self.roads: list[dict[str, Any]] = []
        self.skipped = {"unnamed_place": 0, "unlocated_place": 0,
                        "address_without_locality_or_street": 0,
                        "invalid_or_short_road": 0}

    def _handle(self, obj: Any, kind: str, tags: Mapping[str, str], coords: list[list[float]]) -> None:
        point = _centroid(coords) if kind == "way" else (tuple(coords[0]) if coords else None)
        place_type = tags.get("place")
        name = _display_name(tags)
        if place_type in PLACE_TYPES:
            if not name:
                self.skipped["unnamed_place"] += 1
            elif not point:
                self.skipped["unlocated_place"] += 1
            else:
                self.places.append({"id": _stable_osm_id(kind, obj.id), "name": name, "point": point})

        house_number = tags.get("addr:housenumber")
        street = tags.get("addr:street") or tags.get("addr:place")
        if house_number and street and point:
            self.address_objects.append({
                "id": _stable_osm_id(kind, obj.id), "house_number": house_number,
                "street": street, "settlement": tags.get("addr:city") or tags.get("addr:town"),
                "lon": point[0], "lat": point[1],
                "location_precision": "osm address node" if kind == "node" else "building centroid",
            })
        elif house_number:
            self.skipped["address_without_locality_or_street"] += 1

        if kind == "way" and tags.get("highway") in ROAD_TYPES and len(coords) >= 2:
            road_name = name
            ref = (tags.get("ref") or "").strip() or None
            if road_name or ref:
                length_m = _line_length_m(coords)
                if length_m <= 0:
                    self.skipped["invalid_or_short_road"] += 1
                else:
                    self.roads.append({
                        "id": _stable_osm_id("way", obj.id),
                        "name": road_name,
                        "ref": ref,
                        # Do not mistake way length for cumulative official chainage.
                        # The measured length is retained only as an attribute; kilometer
                        # search stays unavailable until a road reference route is assembled
                        # or an authoritative start/end measure is supplied.
                        "km_start": None,
                        "km_end": None,
                        "geometry": {"type": "LineString", "coordinates": coords},
                        "source": "OpenStreetMap",
                        "km_provenance": "no authoritative chainage; geometry length only",
                        "attributes": {"highway": tags.get("highway"), "osm_way_id": obj.id,
                                       "geometry_length_m": length_m},
                    })

    def node(self, node: Any) -> None:
        if not node.location.valid():
            return
        self._handle(node, "node", node.tags, [[node.location.lon, node.location.lat]])

    def way(self, way: Any) -> None:
        coords = _safe_coordinates(way.nodes)
        self._handle(way, "way", way.tags, coords)


def _normalized_records(collector: _OSMCollect) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    place_by_name: dict[str, list[dict[str, Any]]] = {}
    settlements: dict[str, dict[str, Any]] = {}
    for place in collector.places:
        settlements.setdefault(place["id"], {"id": place["id"], "name": place["name"]})
        place_by_name.setdefault(normalize_text(place["name"]), []).append(place)

    streets_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    houses: list[dict[str, Any]] = []
    for address in collector.address_objects:
        point = (address["lon"], address["lat"])
        settlement_hint = address.get("settlement")
        exact_places = place_by_name.get(normalize_text(settlement_hint), []) if settlement_hint else []
        settlement = _settlement_for(settlement_hint, point, place_by_name, collector.places)
        if settlement is None:
            # Do not guess a city from a region-wide nearest point without local context.
            collector.skipped["address_without_locality_or_street"] += 1
            continue
        assignment = (
            "matched OSM addr:city/addr:town"
            if exact_places
            else "approximate nearest OSM place within 20 km"
        )
        if not exact_places:
            collector.skipped["approximate_settlement_assignments"] = collector.skipped.get("approximate_settlement_assignments", 0) + 1
        settlement_id = settlement["id"]
        street_name = address["street"].strip()
        street_key = (settlement_id, normalize_text(street_name))
        street_id = f"osm-street:{hashlib.sha256((settlement_id + chr(31) + street_key[1]).encode()).hexdigest()[:24]}"
        streets_by_key.setdefault(street_key, {
            "id": street_id, "settlement_id": settlement_id, "name": street_name,
        })
        houses.append({
            "id": address["id"], "street_id": street_id,
            "house_number": address["house_number"], "lon": address["lon"], "lat": address["lat"],
            "location_precision": address["location_precision"],
            "settlement_assignment": assignment,
        })

    # Add named street records from roads only when they can be associated with a locality.
    for road in collector.roads:
        tags = road.get("attributes") or {}
        if tags.get("highway") not in STREET_TYPES or not road.get("name"):
            continue
        geometry = road.get("geometry") or {}
        coords = geometry.get("coordinates") or []
        point = _centroid(coords)
        settlement = _settlement_for(None, point, place_by_name, collector.places)
        if settlement is None:
            continue
        street_name = road["name"]
        street_key = (settlement["id"], normalize_text(street_name))
        street_id = f"osm-street:{hashlib.sha256((settlement['id'] + chr(31) + street_key[1]).encode()).hexdigest()[:24]}"
        streets_by_key.setdefault(street_key, {
            "id": street_id, "settlement_id": settlement["id"], "name": street_name,
        })

    counts = {**collector.skipped, "address_objects": len(collector.address_objects),
              "places": len(collector.places), "roads": len(collector.roads),
              "streets": len(streets_by_key), "houses": len(houses), "settlements": len(settlements)}
    return list(settlements.values()), list(streets_by_key.values()), houses, collector.roads, counts


def update_osm_pbf(
    pbf_path: str | os.PathLike[str],
    *,
    updater_executable: str = "pyosmium-up-to-date",
) -> dict[str, Any]:
    """Apply available upstream diffs to a local Geofabrik PBF safely.

    The updater is run on a staging copy in the same directory. The original is
    replaced only after a successful exit and a nonempty output. The external
    updater uses replication metadata embedded in the PBF; if the source snapshot
    is older than the provider's retained diff history, fetch a fresh PBF manually.
    This function does not download inputs itself.
    """
    source = Path(pbf_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"OSM PBF input does not exist: {source}")
    fd, stage_name = tempfile.mkstemp(prefix=f".{source.name}.", suffix=".update.pbf", dir=source.parent)
    os.close(fd)
    stage = Path(stage_name)
    try:
        shutil.copy2(source, stage)
        try:
            subprocess.run([updater_executable, str(stage)], check=True, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise RuntimeError("PBF update needs the optional `pyosmium-up-to-date` command") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "unknown updater error")[-2000:]
            raise RuntimeError(f"OSM PBF update failed; original snapshot was preserved: {detail}") from exc
        if not stage.is_file() or stage.stat().st_size == 0:
            raise RuntimeError("OSM PBF updater produced an empty output; original snapshot was preserved")
        os.replace(stage, source)
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"path": str(source), "size_bytes": source.stat().st_size, "sha256": digest.hexdigest()}
    finally:
        try:
            stage.unlink()
        except FileNotFoundError:
            pass


def import_osm_pbf(
    pbf_path: str | os.PathLike[str],
    boundary_geojson: str | os.PathLike[str],
    database_path: str | os.PathLike[str],
    *,
    source_date: str | None = None,
    osmium_executable: str = "osmium",
) -> dict[str, Any]:
    """Clip a downloaded PBF to a Polygon boundary, then atomically build SQLite.

    Requires the optional `osmium` CLI and `osmium` Python package. The caller is
    responsible for downloading source data and clearing its use/license. No HTTP
    access occurs in this function.
    """
    source = Path(pbf_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"OSM PBF input does not exist: {source}")
    try:
        import osmium  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("OSM import needs the optional `osmium` Python package") from exc

    database = Path(database_path).expanduser().resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="offline-geo-osm-") as temporary:
        temp = Path(temporary)
        poly = temp / "rostov.poly"
        clipped = temp / "rostov-clipped.osm.pbf"
        poly.write_text(_poly_text(boundary_geojson), encoding="utf-8")
        command = [
            osmium_executable, "extract", "--polygon", str(poly),
            "--strategy", "complete_ways", "--overwrite",
            "--output", str(clipped), str(source),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise RuntimeError("OSM import needs the optional `osmium` command-line utility") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "unknown osmium error")[-2000:]
            raise RuntimeError(f"OSM polygon extract failed: {detail}") from exc

        collector = _OSMCollect()

        class _Handler(osmium.SimpleHandler):
            def node(self, node: Any) -> None:
                collector.node(node)

            def way(self, way: Any) -> None:
                collector.way(way)

        handler = _Handler()
        try:
            handler.apply_file(str(clipped), locations=True, idx="flex_mem")
        except Exception as exc:
            raise RuntimeError(f"Could not parse clipped OSM PBF: {exc}") from exc

    settlements, streets, houses, roads, counts = _normalized_records(collector)
    manifest = {
        "source": "OpenStreetMap / user-provided Geofabrik regional PBF",
        "source_date": source_date,
        "license": "Open Database License (ODbL) 1.0",
        "attribution": "© OpenStreetMap contributors — https://www.openstreetmap.org/copyright",
        "boundary": str(Path(boundary_geojson).resolve()),
        "importer": "offline_geo.osm_import",
        "import_counts": counts,
        "km_note": "OSM road km ranges are approximate lengths from each individual way, not official chainage.",
    }
    return build_index(database, settlements=settlements, streets=streets,
                       houses=houses, roads=roads, manifest=manifest)


__all__ = ["import_osm_pbf", "update_osm_pbf"]
