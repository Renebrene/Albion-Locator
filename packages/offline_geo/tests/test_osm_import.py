from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from offline_geo import GeoIndex
from offline_geo.osm_import import (
    _OSMCollect,
    _normalized_records,
    _poly_text,
    import_osm_pbf,
    update_osm_pbf,
)


class OSMImporterTests(unittest.TestCase):
    def test_boundary_geojson_converts_to_osmium_poly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            boundary = Path(directory) / "boundary.geojson"
            boundary.write_text(json.dumps({
                "type": "Feature",
                "properties": {},
                "geometry": {"type": "Polygon", "coordinates": [[
                    [39.0, 47.0], [40.0, 47.0], [40.0, 48.0], [39.0, 47.0],
                ]]},
            }), encoding="utf-8")
            poly = _poly_text(boundary)
        self.assertTrue(poly.startswith("rostov_oblast\n1\n"))
        self.assertTrue(poly.endswith("END\nEND\n"))
        self.assertIn("39.00000000  47.00000000", poly)

    def test_normalizer_tracks_exact_and_approximate_settlement_assignment(self) -> None:
        collector = _OSMCollect()
        collector.places = [
            {"id": "place:1", "name": "Ростов-на-Дону", "point": (39.7, 47.2)},
        ]
        collector.address_objects = [
            {"id": "osm-node:1", "house_number": "12", "street": "Садовая",
             "settlement": "Ростов-на-Дону", "lon": 39.701, "lat": 47.201,
             "location_precision": "osm address node"},
            {"id": "osm-way:2", "house_number": "14", "street": "Садовая",
             "settlement": None, "lon": 39.702, "lat": 47.202,
             "location_precision": "building centroid"},
        ]
        settlements, streets, houses, roads, counts = _normalized_records(collector)
        self.assertEqual(len(settlements), 1)
        self.assertEqual(len(streets), 1)
        self.assertEqual(len(houses), 2)
        self.assertEqual(houses[0]["settlement_assignment"], "matched OSM addr:city/addr:town")
        self.assertEqual(houses[1]["settlement_assignment"], "approximate nearest OSM place within 20 km")
        self.assertEqual(counts["approximate_settlement_assignments"], 1)
        self.assertEqual(roads, [])

    def test_osm_road_length_is_not_mislabelled_as_chainage(self) -> None:
        collector = _OSMCollect()
        collector.places = [{"id": "place:1", "name": "Ростов-на-Дону", "point": (39.7, 47.2)}]
        collector.roads = [{
            "id": "osm-way:8", "name": "Мира", "ref": None,
            "geometry": {"type": "LineString", "coordinates": [[39.7, 47.2], [39.71, 47.2]]},
            "attributes": {"highway": "residential", "geometry_length_m": 759.0},
            "km_start": None, "km_end": None,
            "km_provenance": "no authoritative chainage; geometry length only",
        }]
        _, _, _, roads, _ = _normalized_records(collector)
        self.assertEqual(roads[0]["km_start"], None)
        self.assertEqual(roads[0]["km_end"], None)
        self.assertEqual(roads[0]["km_provenance"], "no authoritative chainage; geometry length only")
        self.assertIn("geometry_length_m", roads[0]["attributes"])

    def test_update_failure_preserves_original_pbf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "region.osm.pbf"
            source.write_bytes(b"original-snapshot")
            with patch("offline_geo.osm_import.subprocess.run", side_effect=RuntimeError("failure")):
                with self.assertRaises(RuntimeError):
                    update_osm_pbf(source)
            self.assertEqual(source.read_bytes(), b"original-snapshot")

    def test_update_success_replaces_source_only_after_staged_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "region.osm.pbf"
            source.write_bytes(b"old")

            def fake_update(command, check, capture_output, text):
                Path(command[-1]).write_bytes(b"new snapshot")
                return types.SimpleNamespace(stdout="updated", stderr="")

            with patch("offline_geo.osm_import.subprocess.run", side_effect=fake_update):
                result = update_osm_pbf(source)
            self.assertEqual(source.read_bytes(), b"new snapshot")
            self.assertEqual(result["size_bytes"], len(b"new snapshot"))
            self.assertEqual(len(result["sha256"]), 64)

    def test_import_pbf_with_stub_parser_builds_local_snapshot(self) -> None:
        class Location:
            def __init__(self, lon, lat):
                self.lon, self.lat = lon, lat
            def valid(self):
                return True

        class Node:
            def __init__(self, ident, lon, lat, tags):
                self.id, self.location, self.tags = ident, Location(lon, lat), tags

        class WayNode:
            def __init__(self, lon, lat):
                self.lon, self.lat = lon, lat

        class Way:
            def __init__(self, ident, tags, points):
                self.id, self.tags, self.nodes = ident, tags, [WayNode(*point) for point in points]

        class Handler:
            def apply_file(self, path, locations, idx):
                self.node(Node(1, 39.7, 47.2, {"place": "city", "name": "Ростов"}))
                self.node(Node(2, 39.701, 47.201, {
                    "addr:housenumber": "10", "addr:street": "Садовая", "addr:city": "Ростов",
                }))
                self.way(Way(3, {"highway": "residential", "name": "Мира", "ref": "R-1"},
                             [(39.7, 47.2), (39.71, 47.2)]))

        fake_osmium = types.SimpleNamespace(SimpleHandler=Handler)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.osm.pbf"
            source.write_bytes(b"synthetic input")
            boundary = root / "boundary.geojson"
            boundary.write_text(json.dumps({
                "type": "Feature", "properties": {},
                "geometry": {"type": "Polygon", "coordinates": [[
                    [39.0, 47.0], [40.0, 47.0], [40.0, 48.0], [39.0, 47.0],
                ]]},
            }), encoding="utf-8")
            database = root / "offline_geo.sqlite3"

            def fake_extract(command, check, capture_output, text):
                Path(command[8]).write_bytes(b"synthetic clipped PBF")
                return types.SimpleNamespace(stdout="", stderr="")

            with patch.dict(sys.modules, {"osmium": fake_osmium}), patch(
                "offline_geo.osm_import.subprocess.run", side_effect=fake_extract
            ):
                manifest = import_osm_pbf(source, boundary, database, source_date="test-date")
            self.assertEqual(manifest["record_counts"]["settlements"], 1)
            self.assertEqual(manifest["record_counts"]["houses"], 1)
            with GeoIndex(database) as index:
                house = index.suggest_context("10", settlement="osm-node:1", street="osm-street:"
                                              + __import__("hashlib").sha256(
                                                  ("osm-node:1" + chr(31) + "садовая").encode()
                                              ).hexdigest()[:24])
                roads = index.search_roads(ref="R-1")
            self.assertEqual(house[0]["house_number"], "10")
            self.assertEqual(roads[0]["km_start"], None)
            self.assertEqual(roads[0]["km_provenance"], "no authoritative chainage; geometry length only")

    def test_missing_input_pbf_is_reported_before_optional_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                import_osm_pbf(Path(directory) / "missing.osm.pbf", "boundary.geojson", "output.sqlite3")


if __name__ == "__main__":
    unittest.main()
