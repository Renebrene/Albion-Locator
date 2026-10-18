"""Standalone, offline geographic index backed by SQLite and FTS5."""

from .index import (
    GeoIndex,
    build_from_files,
    build_index,
    default_database_path,
    initialize_database,
    normalize_text,
    parse_kilometer,
)
from .schema import PACKAGE_VERSION, SCHEMA_VERSION
from .skdf import normalize_axis_feature, normalize_road_line_feature
from .osm_import import import_osm_pbf, update_osm_pbf

__all__ = [
    "GeoIndex",
    "PACKAGE_VERSION",
    "SCHEMA_VERSION",
    "build_from_files",
    "build_index",
    "default_database_path",
    "initialize_database",
    "normalize_text",
    "normalize_axis_feature",
    "normalize_road_line_feature",
    "import_osm_pbf",
    "update_osm_pbf",
    "parse_kilometer",
]
