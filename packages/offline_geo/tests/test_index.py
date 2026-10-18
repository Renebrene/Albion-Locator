from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from offline_geo import (
    GeoIndex,
    build_from_files,
    build_index,
    default_database_path,
    normalize_axis_feature,
    normalize_road_line_feature,
    parse_kilometer,
)


class OfflineGeoIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Path(self.tempdir.name) / "offline_geo.sqlite3"
        self.metadata = build_index(
            self.db,
            settlements=[{"id": "s1", "name": "Ростов-на-Дону"}],
            streets=[{"id": "st1", "settlement_id": "s1", "name": "Большая Садовая"}],
            houses=[{"id": "h1", "street_id": "st1", "house_number": "12А", "lon": 39.71, "lat": 47.22}],
            roads=[{
                "id": "r1", "name": "Северный обход", "ref": "М-4",
                "km_start": "10+000", "km_end": "12+000",
                "geometry": [[39.70, 47.20], [39.72, 47.22]],
                "source": "test fixture", "km_provenance": "official start/finish",
            }],
            manifest={"source": "synthetic test records", "source_date": "2026-10-18"},
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_manifest_and_version_counts(self) -> None:
        self.assertEqual(self.metadata["schema_version"], 4)
        with GeoIndex(self.db) as index:
            manifest = index.manifest
            self.assertEqual(manifest["source"], "synthetic test records")
            self.assertEqual(manifest["record_counts"], {"settlements": 1, "streets": 1, "houses": 1, "roads": 1})

    def test_bounded_context_suggestions_return_house_coordinates(self) -> None:
        with GeoIndex(self.db) as index:
            settlement = index.suggest_context("Рост", limit=4)
            self.assertEqual([item["type"] for item in settlement], ["settlement"])
            self.assertEqual(settlement[0]["id"], "s1")

            streets = index.suggest_context("Сад", settlement="s1")
            self.assertEqual(streets[0]["street_id"], "st1")

            houses = index.suggest_context("12", settlement="s1", street="st1")
            self.assertEqual(houses[0]["type"], "house")
            self.assertEqual((houses[0]["lon"], houses[0]["lat"]), (39.71, 47.22))
            self.assertEqual(houses[0]["house_number"], "12А")
            self.assertLessEqual(len(index.suggest_context("", limit=200)), 100)

    def test_road_reference_chainage_and_provenance(self) -> None:
        self.assertEqual(parse_kilometer("5+250"), 5.25)
        with GeoIndex(self.db) as index:
            roads = index.search_roads(ref="м-4", km="11+000")
            absent = index.search_roads(ref="absent")
        self.assertEqual(len(roads), 1)
        road = roads[0]
        self.assertEqual(road["id"], "r1")
        self.assertEqual(road["km_provenance"], "official start/finish")
        self.assertEqual(road["source"], "test fixture")
        self.assertEqual(road["selected_km"], 11.0)
        self.assertAlmostEqual(road["selected_lon"], 39.71, places=4)
        self.assertAlmostEqual(road["selected_lat"], 47.21, places=4)
        self.assertEqual(absent, [])

    def test_nearest_road_projection(self) -> None:
        with GeoIndex(self.db) as index:
            nearest = index.nearest_road(39.71, 47.21, max_distance_m=100)
            too_far = index.nearest_road(0, 0, max_distance_m=100)
        self.assertIsNotNone(nearest)
        assert nearest is not None
        self.assertEqual(nearest["id"], "r1")
        self.assertLess(nearest["distance_m"], 1)
        self.assertAlmostEqual(nearest["snapped_lon"], 39.71, places=4)
        self.assertAlmostEqual(nearest["km"], 11.0, places=2)
        self.assertIsNone(too_far)

    def test_failed_rebuild_preserves_existing_database(self) -> None:
        with self.assertRaises(ValueError):
            build_index(self.db, roads=[{"name": "invalid", "geometry": [[0, 0]]}])
        with GeoIndex(self.db) as index:
            self.assertEqual(index.manifest["source"], "synthetic test records")
            self.assertEqual(len(index.search_roads(ref="М-4")), 1)

    def test_build_from_prepared_csv_and_geojson(self) -> None:
        root = Path(self.tempdir.name)
        settlements = root / "settlements.csv"
        settlements.write_text("id,name\ns2,Fixture\n", encoding="utf-8")
        streets = root / "streets.csv"
        streets.write_text("id,settlement_id,name\nst2,s2,Local Road\n", encoding="utf-8")
        roads = root / "roads.geojson"
        roads.write_text(json.dumps({
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "id": "r2", "properties": {"name": "Test Road", "ref": "R2"},
                          "geometry": {"type": "LineString", "coordinates": [[1, 1], [1.1, 1.1]]}}],
        }), encoding="utf-8")
        db = root / "files.sqlite3"
        build_from_files(db, settlements=settlements, streets=streets, roads=roads)
        with GeoIndex(db) as index:
            self.assertEqual(index.suggest_context("fixture")[0]["id"], "s2")
            self.assertEqual(index.search_roads(ref="R2")[0]["id"], "r2")

    def test_default_path_uses_environment_override(self) -> None:
        old = os.environ.get("ALBION_GEO_DATA_DIR")
        os.environ["ALBION_GEO_DATA_DIR"] = "/tmp/offline-geo-test-data"
        try:
            self.assertEqual(default_database_path(), Path("/tmp/offline-geo-test-data/offline_geo.sqlite3"))
        finally:
            if old is None:
                os.environ.pop("ALBION_GEO_DATA_DIR", None)
            else:
                os.environ["ALBION_GEO_DATA_DIR"] = old

    def test_invalid_chainage_rejected(self) -> None:
        for value in ("abc", -1, float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_kilometer(value)

    def test_multipart_geometry_must_be_split_before_import(self) -> None:
        with self.assertRaisesRegex(ValueError, "LineString"):
            build_index(
                Path(self.tempdir.name) / "multipart.sqlite3",
                roads=[{"id": "multi", "ref": "R", "geometry": {
                    "type": "MultiLineString",
                    "coordinates": [[[1, 1], [2, 2]], [[3, 3], [4, 4]]],
                }}],
            )

    def test_skdf_axis_xsd_fields_map_to_segment_endpoints(self) -> None:
        record = normalize_axis_feature({
            "properties": {
                "id": 123,
                "road_id": 50,
                "road_part_id": 900,
                "road_name": "Тестовая дорога",
                "start_m": 12000,
                "finish_m": 15750,
                "road_length_m": 3750,
                "geom_length_m": 3712,
            },
            "geometry": {"type": "LineString", "coordinates": [[39.0, 47.0], [39.1, 47.1]]},
        })
        self.assertEqual(record["id"], "123")
        self.assertEqual(record["name"], "Тестовая дорога")
        self.assertIsNone(record["ref"])
        self.assertEqual(record["km_start"], 12.0)
        self.assertEqual(record["km_end"], 15.75)
        self.assertEqual(record["km_provenance"], "official start/finish")
        self.assertEqual(record["source_ids"], {"road_id": 50, "road_part_id": 900})
        self.assertEqual(record["lengths_m"], {"road_length_m": 3750, "geom_length_m": 3712})
        db = Path(self.tempdir.name) / "skdf-adapter.sqlite3"
        build_index(db, roads=[record])
        with GeoIndex(db) as index:
            indexed = index.search_roads(query="Тестовая дорога")[0]
        self.assertEqual(indexed["source_ids"], {"road_id": 50, "road_part_id": 900})
        self.assertEqual(indexed["attributes"]["geom_length_m"], 3712)

    def test_skdf_road_line_maps_kilometres_and_onepart_multiline(self) -> None:
        record = normalize_road_line_feature({
            "id": "feature-1",
            "properties": {
                "id": 765, "road_id": 12, "part_id": 34,
                "road_name": "М-4 участок", "start_km": "120.5", "finish_km": "121.25",
                "category_gid": 6, "coating_gid": 2,
            },
            "geometry": {"type": "MultiLineString", "coordinates": [[[39.0, 47.0], [39.1, 47.1]]]},
        })
        self.assertEqual(record["id"], "765")
        self.assertEqual(record["km_start"], 120.5)
        self.assertEqual(record["km_end"], 121.25)
        self.assertEqual(record["source_ids"], {"road_id": 12, "part_id": 34})
        self.assertEqual(record["attributes"]["category_gid"], 6)
        self.assertEqual(record["geometry"]["type"], "LineString")
        db = Path(self.tempdir.name) / "skdf-line.sqlite3"
        build_index(db, roads=[record])
        with GeoIndex(db) as index:
            road = index.search_roads(query="М-4 участок", km="120+750")[0]
        self.assertEqual(road["source_ids"], {"road_id": 12, "part_id": 34})
        self.assertEqual(road["attributes"]["category_gid"], 6)
        self.assertEqual(road["selected_km"], 120.75)

    def test_skdf_road_line_rejects_multiple_geometry_parts(self) -> None:
        with self.assertRaisesRegex(ValueError, "component ordering"):
            normalize_road_line_feature({
                "properties": {"id": 765, "start_km": 0, "finish_km": 1},
                "geometry": {"type": "MultiLineString", "coordinates": [
                    [[39.0, 47.0], [39.1, 47.1]], [[39.2, 47.2], [39.3, 47.3]],
                ]},
            })

    def test_skdf_axis_adapter_rejects_unverified_multipart_measure_encoding(self) -> None:
        with self.assertRaisesRegex(ValueError, "MultiLineString"):
            normalize_axis_feature({
                "properties": {"id": 1, "start_m": 0, "finish_m": 1000},
                "geometry": {"type": "MultiLineString", "coordinates": []},
            })


if __name__ == "__main__":
    unittest.main()
