"""Tests for the preliminary, stdlib-only MSK-61 calculator."""

import math
import sys
from pathlib import Path

import pytest

# This is also how the standalone source package is imported before installation.
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from offline_geo.msk61 import (  # noqa: E402
    Msk61Error,
    Profile,
    latlon_to_msk61,
    msk61_to_latlon,
)


ZONES = {
    1: (37.98333333333, 1_300_000.0),
    2: (40.98333333333, 2_300_000.0),
    3: (43.98333333333, 3_300_000.0),
}


@pytest.mark.parametrize("zone,central_meridian,false_easting", [
    (zone, *values) for zone, values in ZONES.items()
])
def test_central_meridian_has_supplied_false_easting(zone, central_meridian, false_easting):
    projected = latlon_to_msk61(47.2, central_meridian, zone)

    assert projected.y == pytest.approx(false_easting, abs=1e-8)
    assert projected.x == pytest.approx(418_516.0453, abs=0.01)
    assert projected.zone == zone
    assert projected.profile is Profile.CALCULATED
    assert projected.status == "preliminary_unverified"
    assert "not been officially verified" in projected.warning
    assert "no datum shift" in projected.warning


@pytest.mark.parametrize("zone,central_meridian,_false_easting", [
    (zone, *values) for zone, values in ZONES.items()
])
def test_calculated_round_trip_inside_each_zone(zone, central_meridian, _false_easting):
    expected = (47.234567, central_meridian + 0.81)

    projected = latlon_to_msk61(*expected, zone, Profile.CALCULATED)
    geographic = msk61_to_latlon(projected.x, projected.y, zone, Profile.CALCULATED)

    assert geographic.latitude == pytest.approx(expected[0], abs=1e-9)
    assert geographic.longitude == pytest.approx(expected[1], abs=1e-9)
    assert geographic.profile is Profile.CALCULATED


@pytest.mark.parametrize("zone,central_meridian,_false_easting", [
    (zone, *values) for zone, values in ZONES.items()
])
def test_gost_profile_applies_reversible_datum_transform(zone, central_meridian, _false_easting):
    expected = (47.234567, central_meridian + 0.81)

    projected = latlon_to_msk61(*expected, zone, Profile.GOST_51794_2008)
    geographic = msk61_to_latlon(
        projected.x, projected.y, zone, Profile.GOST_51794_2008
    )

    assert geographic.latitude == pytest.approx(expected[0], abs=2e-8)
    assert geographic.longitude == pytest.approx(expected[1], abs=2e-8)
    assert geographic.profile is Profile.GOST_51794_2008
    assert geographic.status == "preliminary_unverified"
    assert "nominal 3 m" in geographic.warning
    # GOST is a distinct datum operation, not just a profile label.
    calculated = latlon_to_msk61(*expected, zone, Profile.CALCULATED)
    assert math.hypot(projected.x - calculated.x, projected.y - calculated.y) > 50.0


@pytest.mark.parametrize("zone,central_meridian,_false_easting", [
    (zone, *values) for zone, values in ZONES.items()
])
def test_inverse_round_trip_from_central_meridian(zone, central_meridian, _false_easting):
    projected = latlon_to_msk61(47.2, central_meridian, zone)
    geographic = msk61_to_latlon(projected.x, projected.y, zone)

    assert geographic.latitude == pytest.approx(47.2, abs=1e-9)
    assert geographic.longitude == pytest.approx(central_meridian, abs=1e-9)


def test_gost_profile_matches_published_rostov_example():
    # Secondary-source example only; this is not an official cadastral control point.
    projected = latlon_to_msk61(47.236947, 39.673323, 2, Profile.GOST_51794_2008)
    assert projected.x == pytest.approx(423_461.477, abs=0.01)
    assert projected.y == pytest.approx(2_200_918.281, abs=0.01)
    assert projected.status == "preliminary_unverified"


def test_profile_names_are_case_insensitive():
    result = latlon_to_msk61(47.2, 40.0, 2, "GOST_51794_2008")

    assert result.profile is Profile.GOST_51794_2008


@pytest.mark.parametrize("bad_zone", [0, 4, -1, True, 2.0, "2", None])
def test_rejects_missing_or_invalid_zone(bad_zone):
    with pytest.raises(Msk61Error, match="zone must be"):
        latlon_to_msk61(47.2, 40.0, bad_zone)


@pytest.mark.parametrize("latitude,longitude,zone", [
    (float("nan"), 40.0, 2),
    (47.2, float("inf"), 2),
    (39.9, 40.0, 2),
    (47.2, 44.0, 2),
    (91.0, 40.0, 2),
])
def test_rejects_invalid_geographic_coordinates(latitude, longitude, zone):
    with pytest.raises(Msk61Error):
        latlon_to_msk61(latitude, longitude, zone)


@pytest.mark.parametrize("x,y,zone", [
    (float("nan"), 2_300_000.0, 2),
    (400_000.0, float("inf"), 2),
    (400_000.0, 3_300_000.0, 2),
    (2_000_000.0, 2_300_000.0, 2),
])
def test_rejects_invalid_projected_coordinates(x, y, zone):
    with pytest.raises(Msk61Error):
        msk61_to_latlon(x, y, zone)


def test_calculated_and_gost_are_explicit_distinct_profiles():
    calculated = msk61_to_latlon(400_000.0, 2_300_000.0, 2, Profile.CALCULATED)
    gost = msk61_to_latlon(400_000.0, 2_300_000.0, 2, Profile.GOST_51794_2008)

    assert abs(calculated.latitude - gost.latitude) > 0.00004
    assert abs(calculated.longitude - gost.longitude) > 0.001
    assert "not asserted to be WGS 84" in calculated.warning
    assert "zero ellipsoidal height" in gost.warning


def test_independent_proj_control_points_cover_all_zones():
    """48 independent PROJ vectors, not official cadastral control points."""
    import json
    from pathlib import Path

    fixture = Path(__file__).parent / "data" / "msk61_proj_control_points.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    points = data["points"]
    assert len(points) >= 20
    assert {point["zone"] for point in points} == {1, 2, 3}

    tolerance = float(data["tolerance_m"])
    for point in points:
        projected = latlon_to_msk61(
            point["lat"], point["lon"], point["zone"], Profile.CALCULATED
        )
        offset = math.hypot(projected.x - point["x"], projected.y - point["y"])
        assert offset <= tolerance, (point["id"], offset)

        inverse = msk61_to_latlon(
            point["x"], point["y"], point["zone"], Profile.CALCULATED
        )
        assert inverse.latitude == pytest.approx(point["lat"], abs=2e-8)
        assert inverse.longitude == pytest.approx(point["lon"], abs=2e-8)
