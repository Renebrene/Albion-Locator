"""Normalize locally obtained SKDF geo_road_axis_lr GeoJSON features.

This adapter makes no network requests. The layer's XSD is known, but live
feature access currently returns permission denied. Do not treat this adapter as
permission to bulk-fetch or redistribute SKDF data.
"""
from __future__ import annotations

import math
from typing import Any, Mapping


def _metres_to_km(value: Any, field: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        metres = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a numeric metre value") from exc
    if not math.isfinite(metres) or metres < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return metres / 1000.0


def normalize_road_line_feature(feature: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one locally obtained ``lyr_road_by_value_line`` GeoJSON feature.

    The layer XSD defines `road_id`, `part_id`, `start_km`, `finish_km`,
    `category_gid`, `coating_gid`, and `geom` (GML MultiCurve). GeoJSON responses
    commonly encode a single curve as a one-part MultiLineString; that safe case
    is reduced to a LineString. Multiple parts or measured/3D positions are
    rejected until their component order and measure semantics are verified.
    This function performs no network requests.
    """
    if not isinstance(feature, Mapping):
        raise ValueError("SKDF feature must be a mapping")
    properties = feature.get("properties") or {}
    if not isinstance(properties, Mapping):
        raise ValueError("SKDF feature properties must be a mapping")
    geometry = feature.get("geometry")
    if geometry is None:
        geometry = properties.get("geom")
    if not isinstance(geometry, Mapping):
        raise ValueError("SKDF feature does not contain decoded GeoJSON geometry")
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if kind == "MultiLineString":
        if not isinstance(coordinates, (list, tuple)) or len(coordinates) != 1:
            raise ValueError("multi-part SKDF geometry requires verified component ordering before import")
        coordinates = coordinates[0]
    elif kind != "LineString":
        raise ValueError("SKDF road geometry must be LineString or one-part MultiLineString")
    if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
        raise ValueError("SKDF road geometry must have at least two coordinates")
    normalized_coordinates = []
    for point in coordinates:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError("SKDF measured/3D coordinates are not supported until geom measure semantics are verified")
        lon, lat = float(point[0]), float(point[1])
        if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("SKDF road coordinates must be finite WGS84 longitude/latitude")
        normalized_coordinates.append([lon, lat])

    start = properties.get("start_km")
    finish = properties.get("finish_km")
    try:
        km_start = float(start) if start not in (None, "") else None
        km_end = float(finish) if finish not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise ValueError("SKDF start_km and finish_km must be decimal kilometre values") from exc
    if (km_start is None) != (km_end is None):
        raise ValueError("SKDF start_km and finish_km must both be present or both absent")
    if km_start is not None and (not math.isfinite(km_start) or not math.isfinite(km_end) or km_start < 0 or km_end < 0):
        raise ValueError("SKDF kilometre endpoints must be finite and non-negative")

    feature_id = properties.get("id", feature.get("id"))
    if feature_id is None:
        raise ValueError("SKDF line feature requires its id")
    name = str(properties.get("road_name") or "").strip() or None
    return {
        "id": str(feature_id),
        "name": name,
        "ref": None,
        "km_start": km_start,
        "km_end": km_end,
        "geometry": {"type": "LineString", "coordinates": normalized_coordinates},
        "source": "SKDF lyr_road_by_value_line",
        "km_provenance": "official start/finish" if km_start is not None else "estimated from geometry",
        "source_ids": {"road_id": properties.get("road_id"), "part_id": properties.get("part_id")},
        "attributes": {
            key: properties.get(key) for key in (
                "settlment_name", "value_of_the_road", "value_of_the_road_gid",
                "region_gid", "agglomeration_gid", "category_gid", "class_of_road_gid",
                "coating_gid", "lanes", "roadway_width", "roadbed_width", "is_checked",
            ) if properties.get(key) is not None
        },
    }


def normalize_axis_feature(feature: Mapping[str, Any]) -> dict[str, Any]:
    """Convert one already-obtained SKDF axis GeoJSON feature to package format.

    XSD fields include ``id``, ``road_id``, ``road_part_id``, ``start_m``,
    ``finish_m``, ``road_length_m``, ``geom_length_m``, and ``geom``. Official
    endpoint values are retained as km start/end. Any interpolation between
    those endpoints remains an estimate along the supplied geometry; it is not
    asserted to be measured chainage.

    Only a single GeoJSON LineString is accepted. MultiLineString parts and
    GML measured-curve encodings require an authorized real feature sample to
    establish component order/M semantics; flattening them here could corrupt
    kilometre interpolation.
    """
    if not isinstance(feature, Mapping):
        raise ValueError("SKDF feature must be a mapping")
    properties = feature.get("properties") or {}
    if not isinstance(properties, Mapping):
        raise ValueError("SKDF feature properties must be a mapping")
    geometry = feature.get("geometry")
    if geometry is None:
        geometry = properties.get("geom")
    if isinstance(geometry, str):
        raise ValueError("SKDF geometry must be decoded GeoJSON, not a string")
    if not isinstance(geometry, Mapping):
        raise ValueError("SKDF feature does not contain GeoJSON geometry")
    if geometry.get("type") != "LineString":
        raise ValueError(
            "only LineString is supported; verify MultiLineString/GML measure semantics before normalization"
        )

    raw_start = properties.get("start_m")
    raw_finish = properties.get("finish_m")
    km_start = _metres_to_km(raw_start, "start_m")
    km_end = _metres_to_km(raw_finish, "finish_m")
    if (km_start is None) != (km_end is None):
        raise ValueError("SKDF start_m and finish_m must either both be present or both be absent")

    axis_id = properties.get("id", feature.get("id"))
    road_id = properties.get("road_id")
    part_id = properties.get("road_part_id")
    # Keep the geometry feature ID unique. Parent IDs remain available in source metadata
    # for future schema evolution, but are not misrepresented as a road code/ref.
    record_id = str(axis_id) if axis_id is not None else None
    if record_id is None:
        raise ValueError("SKDF axis feature requires its geometry id")

    name = str(properties.get("road_name") or "").strip() or None
    return {
        "id": record_id,
        "name": name,
        "ref": None,
        "km_start": km_start,
        "km_end": km_end,
        "geometry": geometry,
        "source": "SKDF geo_road_axis_lr",
        "km_provenance": "official start/finish" if km_start is not None else "estimated from geometry",
        "source_ids": {"road_id": road_id, "road_part_id": part_id},
        "lengths_m": {
            "road_length_m": properties.get("road_length_m"),
            "geom_length_m": properties.get("geom_length_m"),
        },
    }


__all__ = ["normalize_axis_feature", "normalize_road_line_feature"]
