#!/usr/bin/env python3
"""Synthetic benchmark for GeoIndex.nearest_road.

Builds an isolated SQLite index with a configurable number of road lines and
measures query latency. This is a planning benchmark, not a dataset-quality
test: it answers whether a linear scan needs an RTree prefilter at regional
scale.

Example:
    uv run --python 3.11 python packages/offline_geo/benchmarks/bench_nearest_road.py \
        --roads 20000 --points 12 --queries 200
"""
from __future__ import annotations

import argparse
import random
import statistics
import tempfile
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from offline_geo.index import GeoIndex, build_index  # noqa: E402

# Approximate Rostov region bounding box.
LON_MIN, LON_MAX = 38.3, 44.2
LAT_MIN, LAT_MAX = 46.0, 50.1


def make_roads(count: int, points: int, seed: int = 42):
    rng = random.Random(seed)
    for index in range(count):
        lon = rng.uniform(LON_MIN, LON_MAX)
        lat = rng.uniform(LAT_MIN, LAT_MAX)
        step_lon = rng.uniform(0.002, 0.02)
        step_lat = rng.uniform(-0.004, 0.004)
        geometry = [
            [lon + step_lon * point, lat + step_lat * point]
            for point in range(points)
        ]
        yield {
            "id": f"road-{index}",
            "name": f"Дорога {index}",
            "ref": f"T-{index}",
            "km_start": 0,
            "km_end": float(points - 1),
            "km_provenance": "official start/finish",
            "source": "synthetic-benchmark",
            "geometry": geometry,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--roads", type=int, default=5000)
    parser.add_argument("--points", type=int, default=12)
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    queries = [
        (rng.uniform(LON_MIN, LON_MAX), rng.uniform(LAT_MIN, LAT_MAX))
        for _ in range(args.queries)
    ]
    with tempfile.TemporaryDirectory(prefix="albion-bench-") as directory:
        path = Path(directory) / "offline_geo.sqlite3"
        started = time.perf_counter()
        build_index(
            path,
            roads=make_roads(args.roads, args.points, args.seed),
            manifest={"purpose": "nearest_road synthetic benchmark"},
        )
        build_seconds = time.perf_counter() - started
        index = GeoIndex(path)
        latencies = []
        for lon, lat in queries:
            started = time.perf_counter()
            index.nearest_road(lon, lat)
            latencies.append((time.perf_counter() - started) * 1000)
        index.close()

    latencies.sort()
    p50 = statistics.median(latencies)
    p95 = latencies[int(len(latencies) * 0.95) - 1]
    print(f"roads={args.roads} points_per_road={args.points} segments={args.roads * (args.points - 1)}")
    print(f"build={build_seconds:.2f}s queries={args.queries}")
    print(f"nearest_road p50={p50:.2f}ms p95={p95:.2f}ms max={max(latencies):.2f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
