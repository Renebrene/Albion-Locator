# offline_geo

A standalone Python package for building and querying a local geographic lookup index. It is deliberately independent of FastAPI and the web UI. Runtime implementation uses only the Python standard library (`sqlite3`, FTS5, and R*Tree); no network calls are made and no geographic data is included.

## Import and CLI

Run from the repository root without installing dependencies:

```sh
PYTHONPATH=packages/offline_geo/src python -m offline_geo --help
```

Python callers can use:

```python
from offline_geo import GeoIndex, build_from_files, build_index

metadata = build_index(
    "/path/to/offline_geo.sqlite3",
    settlements=[{"id": "s1", "name": "Example"}],
    streets=[{"id": "st1", "settlement_id": "s1", "name": "Main Street"}],
    houses=[{"street_id": "st1", "house_number": "12", "lon": 39.7, "lat": 47.2}],
    roads=[{"id": "r1", "name": "A-123", "ref": "A-123", "km_start": 0,
            "km_end": 10, "geometry": [[39.6, 47.1], [39.8, 47.3]],
            "source": "locally-prepared", "km_provenance": "estimated from geometry"}],
)
with GeoIndex("/path/to/offline_geo.sqlite3") as index:
    suggestions = index.suggest_context("ma", settlement="s1")
    roads = index.search_roads(ref="A-123", km="5+250")
    nearest = index.nearest_road(39.7, 47.2, max_distance_m=5000)
```

`GeoIndex()` reads `$ALBION_GEO_DATA_DIR/offline_geo.sqlite3`, or defaults to `~/.local/share/albion-locator/offline_geo.sqlite3`. It opens the index read-only. `build_index()` accepts normalized record iterables; `build_from_files()` accepts CSV or GeoJSON prepared inputs (`FeatureCollection`/`Feature` or a list of records). Roads accept GeoJSON LineString geometry or a coordinate list in WGS84 `[longitude, latitude]` order. MultiLineString inputs must be normalized upstream into one road record per LineString part, with stable distinct IDs and preserved `ref`, source, and chainage metadata; a multipart geometry is rejected rather than silently flattened or joined across gaps. The nearest-road lookup currently scans all indexed road segments (O(N)); an R*Tree-backed candidate accelerator is a future optimization. `osm_import.py` provides an optional PBF→SQLite importer (see below); source PBFs are never downloaded automatically or embedded by this package.

### Optional OSM regional importer and monthly update

The core package has no runtime dependencies. To import PBF, install the optional Python parser extra and separately install the `osmium` CLI (for example from the OS package manager):

```sh
python -m pip install -e 'packages/offline_geo[osm]'
```

This does not download any data. Obtain the South Federal District PBF manually from Geofabrik; it covers more than Rostov Oblast and must be clipped to a verified boundary. The repository's `static/rostov_boundary.geojson` is used by the example; verify its provenance/validity before a production import. Example commands:

```sh
# First import of a manually downloaded regional snapshot
offline-geo import-osm-pbf \
  --pbf /data/source/south-fed-district-latest.osm.pbf \
  --boundary /path/to/rostov_boundary.geojson \
  --database "$ALBION_GEO_DATA_DIR/offline_geo.sqlite3" \
  --source-date 2026-10-18

# Monthly maintenance, if the existing PBF embeds valid Geofabrik replication metadata
offline-geo update-osm-pbf --pbf /data/source/south-fed-district-latest.osm.pbf
# Rebuild the SQLite index after updating the PBF
offline-geo import-osm-pbf \
  --pbf /data/source/south-fed-district-latest.osm.pbf \
  --boundary /path/to/rostov_boundary.geojson \
  --database "$ALBION_GEO_DATA_DIR/offline_geo.sqlite3" \
  --source-date 2026-11-18
```

`update-osm-pbf` runs the updater on a staging copy and only replaces the source file after success. If the PBF is too old for the provider's retained diffs, download a fresh regional snapshot manually. The database rebuild is staged and atomically replaces the prior index only after integrity checks. The update workflow is not scheduled automatically; archive source files and generated indexes stay outside Git.

OSM address and locality coverage varies. House way coordinates are representative building centroids, not entrances. Locality assignment based on nearby place objects is explicitly marked approximate. OSM roads do not have official linear chainage; this importer does **not** fabricate `start_km`/`finish_km` from a way's length, so road+kilometer searches need an authoritative chainage source or a separately validated route-stationing model. Report the import manifest and inspect `import_counts`/skipped records before treating the index as complete.

The `build` command accepts separate prepared input files:

```sh
PYTHONPATH=packages/offline_geo/src python -m offline_geo build \
  --database /data/offline_geo.sqlite3 \
  --settlements settlements.csv --streets streets.csv \
  --houses houses.csv --roads roads.geojson \
  --source local-osm-extract --source-date 2026-10-18
PYTHONPATH=packages/offline_geo/src python -m offline_geo manifest --database /data/offline_geo.sqlite3
```

The importer's expected columns/keys are `settlements: id,name`; `streets: id,settlement_id,name` (or a `settlement` name); `houses: id,street_id,house_number,lon,lat` (or `street` and optional `settlement`); `roads: id,name,ref,km_start,km_end,geometry,source,km_provenance`. Missing identifiers are deterministic from their parent/name fields. Input data is assumed already normalized and legally cleared for the intended use.

### SKDF `lyr_road_by_value_line` XSD mapping

The supplied line-layer XSD uses `id`, `road_id`, `part_id`, `road_name`, `start_km`, `finish_km`, `category_gid`, `coating_gid`, `value_of_the_road`, and `geom` (`gml:MultiCurvePropertyType`). `normalize_road_line_feature()` maps numeric start/end km, road/part IDs and category/coating metadata. It accepts a LineString or a one-component GeoJSON MultiLineString; it rejects multiple components and 3D/measured coordinates because order and linear-measure encoding are not known. Current WFS samples demonstrate geometries can contain malformed or geographically distant components, so imported geometries require explicit QA/clipping. Access to a feature via public WFS is not proof of permission for bulk cache/redistribution.

### SKDF `geo_road_axis_lr` XSD mapping (schema inspected, feature access not available)

The supplied XSD declares: `road_id` (road ID), `road_part_id` (road-section ID), `start_m` (section begin in m), `finish_m` (section end in m), `road_length_m` (section length in m), `geom_length_m` (drawn-geometry length in m), `road_name`, `geom`, simplified `geom_1000` / `geom_100` / `geom_10`, `value_of_road_gid`, `is_skeleton`, `zoom`, and `passport_id`. Older `start`/`finish` attributes are explicitly marked legacy/not used. A future adapter should map `road_part_id` (not the current layer's `part_id` spelling) to the offline road-section ID and convert m to km only at the package boundary. `geom_length_m` is not interchangeable with authoritative chainage length. The XSD labels `geom` as a geometry with linear referencing, but the actual serialized measure encoding was not present in the XSD; verify it against an authorized feature before using it to interpolate within a part. The WFS advertises the layer but actual unauthenticated GetFeature is denied by the database, so the fields cannot currently be imported from live feature responses.

## API summary

- `GeoIndex.suggest_context(query='', *, settlement=None, street=None, limit=10)` returns bounded settlement suggestions, street suggestions scoped to a settlement, or house suggestions scoped to a street. House suggestions include `lon` and `lat` when provided in the input.
- `GeoIndex.search_roads(query=None, *, ref=None, km=None, limit=20)` searches local road name/reference data. A chainage such as `5+250` is interpreted as 5.25 km. Matching road records include geometry, source, km provenance, and an interpolated selected location when possible.
- `GeoIndex.nearest_road(lon, lat, *, max_distance_m=None)` projects a WGS84 point onto local road geometry and returns the closest segment, snapped point, distance, and interpolated km when both endpoint km values exist.
- `GeoIndex.manifest` exposes package/schema versions, build timestamp, counts, and caller-provided source/version/license metadata.

Builds are staged beside the destination, checked for SQLite integrity and foreign-key consistency, then atomically replaced. A failed staging build leaves a prior database in place. Applications should close index connections during rebuilds.

## Data provenance and legal notes

This package contains code only; there is no bundled SQLite database, map, or geographic dataset. Users are responsible for recording the source, snapshot date, and applicable rights in `manifest` metadata and for clearing data before importing or redistribution.

When OpenStreetMap-derived material is sourced, the OpenStreetMap data is licensed under the Open Database License (ODbL) and applicable attribution/share-alike obligations may apply to a database or derivative database. Follow current OSM attribution and ODbL guidance for the exact use and distribution model; this package does not provide legal advice or determine whether a particular output is a derivative database.

The legal rights and reuse terms for SKDF sources/data have **not been established** by this package. Do not assume that SKDF data may be copied, bulk downloaded, indexed, or redistributed. No SKDF WFS client or bulk-fetch behavior is implemented; only local, prepared input files are accepted. Obtain and verify permission and terms before importing or distributing any such material.

No package-specific code license is asserted here. Apply the parent repository's license, if one is established, to code reuse and distribution.
