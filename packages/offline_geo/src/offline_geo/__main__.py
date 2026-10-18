"""Command line interface: ``python -m offline_geo``."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .index import GeoIndex, build_from_files, default_database_path, parse_kilometer
from .osm_import import import_osm_pbf, update_osm_pbf
from .schema import PACKAGE_VERSION, SCHEMA_VERSION


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="offline-geo", description="Build and query a local SQLite/FTS5 geographic index.")
    parser.add_argument("--version", action="version", version=f"offline-geo {PACKAGE_VERSION} (schema {SCHEMA_VERSION})")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="atomically build from prepared CSV or GeoJSON inputs")
    build.add_argument("--database", type=Path, default=default_database_path())
    build.add_argument("--settlements", type=Path)
    build.add_argument("--streets", type=Path)
    build.add_argument("--houses", type=Path)
    build.add_argument("--roads", type=Path)
    build.add_argument("--source", help="dataset/source identifier recorded in manifest")
    build.add_argument("--source-date", help="source snapshot date recorded in manifest")

    update = sub.add_parser("update-osm-pbf", help="apply available upstream diffs to an existing local Geofabrik PBF")
    update.add_argument("--pbf", type=Path, required=True)
    update.add_argument("--updater", default="pyosmium-up-to-date")

    import_osm = sub.add_parser("import-osm-pbf", help="clip a local PBF and build a staged SQLite index")
    import_osm.add_argument("--pbf", type=Path, required=True)
    import_osm.add_argument("--boundary", type=Path, required=True)
    import_osm.add_argument("--database", type=Path, default=default_database_path())
    import_osm.add_argument("--source-date", required=True, help="snapshot date of the PBF input")
    import_osm.add_argument("--osmium", default="osmium")

    inspect = sub.add_parser("manifest", help="print database version and source manifest")
    inspect.add_argument("--database", type=Path, default=default_database_path())

    suggest = sub.add_parser("suggest", help="suggest the next settlement, street, or house")
    suggest.add_argument("query", nargs="?", default="")
    suggest.add_argument("--settlement")
    suggest.add_argument("--street")
    suggest.add_argument("--database", type=Path, default=default_database_path())
    suggest.add_argument("--limit", type=int, default=10)

    roads = sub.add_parser("roads", help="search roads by name/reference and optional chainage")
    roads.add_argument("query", nargs="?", default=None)
    roads.add_argument("--ref")
    roads.add_argument("--km", type=parse_kilometer)
    roads.add_argument("--database", type=Path, default=default_database_path())
    roads.add_argument("--limit", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "update-osm-pbf":
            output = update_osm_pbf(args.pbf, updater_executable=args.updater)
            print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.command == "import-osm-pbf":
            output = import_osm_pbf(
                args.pbf, args.boundary, args.database,
                source_date=args.source_date,
                osmium_executable=args.osmium,
            )
            print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.command == "build":
            manifest = {key: value for key, value in {
                "source": args.source, "source_date": args.source_date,
            }.items() if value is not None}
            metadata = build_from_files(args.database, settlements=args.settlements,
                                        streets=args.streets, houses=args.houses, roads=args.roads,
                                        manifest=manifest)
            print(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        with GeoIndex(args.database) as index:
            if args.command == "manifest":
                output = index.manifest
            elif args.command == "suggest":
                output = index.suggest_context(args.query, settlement=args.settlement,
                                               street=args.street, limit=args.limit)
            else:
                output = index.search_roads(args.query, ref=args.ref, km=args.km, limit=args.limit)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"offline-geo: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
