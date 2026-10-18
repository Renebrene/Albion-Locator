"""Preliminary, stdlib-only MSK-61 coordinate conversion.

This module implements the supplied local Transverse Mercator parameters on the
Krassovsky ellipsoid.  ``CALCULATED`` is projection-only; it does not imply or
perform an officially verified datum transformation. ``GOST_51794_2008`` applies
the supplied Pulkovo 1942-to-WGS 84 seven-parameter coordinate-frame Helmert
transformation (nominal 3 m parameter-set accuracy) at zero ellipsoidal height.
The MSK-61 datum/zone parameters have not been officially verified here.

The projected axis convention is X = northing, Y = easting.  A zone must always
be supplied explicitly; this avoids a silent, plausible result in the wrong zone.
Only Python's standard library is used.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Final


class Msk61Error(ValueError):
    """Raised when an input is invalid or outside the preliminary local domain."""


class Profile(str, Enum):
    """Available preliminary transform profiles."""

    CALCULATED = "calculated"
    GOST_51794_2008 = "gost_51794_2008"


@dataclass(frozen=True, slots=True)
class GeographicResult:
    """Geographic output in decimal degrees (latitude, longitude)."""

    latitude: float
    longitude: float
    zone: int
    profile: Profile
    status: str
    warning: str


@dataclass(frozen=True, slots=True)
class Msk61Result:
    """Projected output using the MSK axis convention X=northing, Y=easting."""

    x: float
    y: float
    zone: int
    profile: Profile
    status: str
    warning: str


# Synonym useful to callers that prefer the coordinate-system name.
Msk61Coordinate = Msk61Result


@dataclass(frozen=True, slots=True)
class _Zone:
    central_meridian: float
    false_easting: float


_ZONES: Final[dict[int, _Zone]] = {
    1: _Zone(37.98333333333, 1_300_000.0),
    2: _Zone(40.98333333333, 2_300_000.0),
    3: _Zone(43.98333333333, 3_300_000.0),
}
_FALSE_NORTHING: Final[float] = -4_811_057.628
_K0: Final[float] = 1.0
_KRASSOVSKY_A: Final[float] = 6_378_245.0
_KRASSOVSKY_F: Final[float] = 1.0 / 298.3
_WGS84_A: Final[float] = 6_378_137.0
_WGS84_F: Final[float] = 1.0 / 298.257223563
_ARCSEC_TO_RAD: Final[float] = math.pi / (180.0 * 3600.0)
_PRELIMINARY_STATUS: Final[str] = "preliminary_unverified"
_MAX_LON_DELTA: Final[float] = 3.0
_MIN_LATITUDE: Final[float] = 40.0
_MAX_LATITUDE: Final[float] = 55.0

_COMMON_WARNING: Final[str] = (
    "Preliminary calculator only: the MSK-61 datum and supplied zone parameters "
    "have not been officially verified. Use the result as a candidate, not as "
    "survey-grade or authoritative coordinates."
)
_CALCULATED_WARNING: Final[str] = (
    _COMMON_WARNING
    + " CALCULATED applies the supplied Krassovsky Transverse Mercator projection "
    "only; it performs no datum shift and its geographic output is not asserted to be WGS 84."
)
_GOST_WARNING: Final[str] = (
    _COMMON_WARNING
    + " GOST_51794_2008 applies the supplied Pulkovo 1942-to-WGS 84 coordinate-frame "
    "Helmert parameters at zero ellipsoidal height. The cited nominal 3 m accuracy "
    "is a parameter-set caveat, not a guaranteed local accuracy."
)


def _profile(value: Profile | str) -> Profile:
    if isinstance(value, Profile):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        for profile in Profile:
            if normalized in (profile.value, profile.name.lower()):
                return profile
    raise Msk61Error("profile must be CALCULATED or GOST_51794_2008")


def _zone(number: int) -> _Zone:
    if isinstance(number, bool) or not isinstance(number, int) or number not in _ZONES:
        raise Msk61Error("zone must be the explicit integer 1, 2, or 3")
    return _ZONES[number]


def _finite(*values: float) -> bool:
    try:
        return all(math.isfinite(value) for value in values)
    except TypeError:
        return False


def _validate_latlon(latitude: float, longitude: float, zone_number: int) -> _Zone:
    zone = _zone(zone_number)
    if not _finite(latitude, longitude):
        raise Msk61Error("latitude and longitude must be finite numbers")
    if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
        raise Msk61Error("latitude or longitude is outside its geographic range")
    if not _MIN_LATITUDE <= latitude <= _MAX_LATITUDE:
        raise Msk61Error("latitude is outside the preliminary local domain (40 to 55 degrees)")
    if abs(longitude - zone.central_meridian) > _MAX_LON_DELTA:
        raise Msk61Error("longitude is outside the selected zone's preliminary 3-degree domain")
    return zone


def _ellipsoid_parameters(a: float, f: float) -> tuple[float, float, float, float]:
    eccentricity_squared = f * (2.0 - f)
    eccentricity = math.sqrt(eccentricity_squared)
    n = f / (2.0 - f)
    # Sixth-order Krueger series; higher terms are far below local-coordinate needs.
    a_bar = a / (1.0 + n) * (
        1.0 + n**2 / 4.0 + n**4 / 64.0 + n**6 / 256.0
    )
    return eccentricity_squared, eccentricity, n, a_bar


def _krueger_coefficients(n: float) -> tuple[tuple[float, ...], tuple[float, ...]]:
    n2, n3, n4, n5, n6 = n**2, n**3, n**4, n**5, n**6
    alpha = (
        0.5 * n - 2.0 / 3.0 * n2 + 5.0 / 16.0 * n3 + 41.0 / 180.0 * n4
        - 127.0 / 288.0 * n5 + 7891.0 / 37800.0 * n6,
        13.0 / 48.0 * n2 - 3.0 / 5.0 * n3 + 557.0 / 1440.0 * n4
        + 281.0 / 630.0 * n5 - 1983433.0 / 1935360.0 * n6,
        61.0 / 240.0 * n3 - 103.0 / 140.0 * n4 + 15061.0 / 26880.0 * n5
        + 167603.0 / 181440.0 * n6,
        49561.0 / 161280.0 * n4 - 179.0 / 168.0 * n5
        + 6601661.0 / 7257600.0 * n6,
        34729.0 / 80640.0 * n5 - 3418889.0 / 1995840.0 * n6,
        212378941.0 / 319334400.0 * n6,
    )
    beta = (
        0.5 * n - 2.0 / 3.0 * n2 + 37.0 / 96.0 * n3 - 1.0 / 360.0 * n4
        - 81.0 / 512.0 * n5 + 96199.0 / 604800.0 * n6,
        1.0 / 48.0 * n2 + 1.0 / 15.0 * n3 - 437.0 / 1440.0 * n4
        + 46.0 / 105.0 * n5 - 1118711.0 / 3870720.0 * n6,
        17.0 / 480.0 * n3 - 37.0 / 840.0 * n4 - 209.0 / 4480.0 * n5
        + 5569.0 / 90720.0 * n6,
        4397.0 / 161280.0 * n4 - 11.0 / 504.0 * n5
        - 830251.0 / 7257600.0 * n6,
        4583.0 / 161280.0 * n5 - 108847.0 / 3991680.0 * n6,
        20648693.0 / 638668800.0 * n6,
    )
    return alpha, beta


def _forward_tm(latitude: float, longitude: float, zone: _Zone) -> tuple[float, float]:
    e2, eccentricity, n, a_bar = _ellipsoid_parameters(_KRASSOVSKY_A, _KRASSOVSKY_F)
    del e2  # The conformal-latitude expression uses eccentricity directly.
    alpha, _ = _krueger_coefficients(n)
    phi = math.radians(latitude)
    delta_lambda = math.radians(longitude - zone.central_meridian)
    sin_phi = math.sin(phi)
    tau = math.tan(phi)
    sigma = math.sinh(eccentricity * math.atanh(eccentricity * sin_phi))
    tau_prime = tau * math.sqrt(1.0 + sigma * sigma) - sigma * math.sqrt(1.0 + tau * tau)

    xi_prime = math.atan2(tau_prime, math.cos(delta_lambda))
    eta_prime = math.asinh(
        math.sin(delta_lambda) / math.sqrt(tau_prime * tau_prime + math.cos(delta_lambda) ** 2)
    )
    xi = xi_prime
    eta = eta_prime
    for j, coefficient in enumerate(alpha, 1):
        two_j_xi = 2.0 * j * xi_prime
        two_j_eta = 2.0 * j * eta_prime
        xi += coefficient * math.sin(two_j_xi) * math.cosh(two_j_eta)
        eta += coefficient * math.cos(two_j_xi) * math.sinh(two_j_eta)

    _, _, _, a_bar = _ellipsoid_parameters(_KRASSOVSKY_A, _KRASSOVSKY_F)
    northing = _FALSE_NORTHING + _K0 * a_bar * xi
    easting = zone.false_easting + _K0 * a_bar * eta
    return northing, easting


def _inverse_tm(northing: float, easting: float, zone: _Zone) -> tuple[float, float]:
    if not -600_000.0 <= northing <= 1_500_000.0:
        raise Msk61Error("northing is outside the preliminary local domain")
    if abs(easting - zone.false_easting) > 500_000.0:
        raise Msk61Error("easting is inconsistent with the selected zone")

    _, eccentricity, n, a_bar = _ellipsoid_parameters(_KRASSOVSKY_A, _KRASSOVSKY_F)
    _, beta = _krueger_coefficients(n)
    xi = (northing - _FALSE_NORTHING) / (_K0 * a_bar)
    eta = (easting - zone.false_easting) / (_K0 * a_bar)
    xi_prime = xi
    eta_prime = eta
    for j, coefficient in enumerate(beta, 1):
        two_j_xi = 2.0 * j * xi
        two_j_eta = 2.0 * j * eta
        xi_prime -= coefficient * math.sin(two_j_xi) * math.cosh(two_j_eta)
        eta_prime -= coefficient * math.cos(two_j_xi) * math.sinh(two_j_eta)

    sinh_eta = math.sinh(eta_prime)
    tau_prime = math.sin(xi_prime) / math.hypot(sinh_eta, math.cos(xi_prime))
    # Invert tau' = sinh(psi - e*atanh(e*tanh(psi))) by Newton iteration.
    target = math.asinh(tau_prime)
    psi = target
    for _ in range(12):
        tanh_psi = math.tanh(psi)
        denominator = 1.0 - eccentricity * eccentricity * tanh_psi * tanh_psi
        value = psi - eccentricity * math.atanh(eccentricity * tanh_psi) - target
        derivative = 1.0 - eccentricity * eccentricity * (1.0 - tanh_psi * tanh_psi) / denominator
        step = value / derivative
        psi -= step
        if abs(step) < 1e-14:
            break
    latitude = math.degrees(math.atan(math.sinh(psi)))
    delta_lambda = math.atan2(sinh_eta, math.cos(xi_prime))
    longitude = zone.central_meridian + math.degrees(delta_lambda)
    if not _MIN_LATITUDE <= latitude <= _MAX_LATITUDE:
        raise Msk61Error("coordinate transforms outside the preliminary local latitude domain")
    if abs(longitude - zone.central_meridian) > _MAX_LON_DELTA:
        raise Msk61Error("coordinate transforms outside the selected zone's preliminary domain")
    return latitude, longitude


def _geodetic_to_ecef(
    latitude: float, longitude: float, a: float, f: float, height: float = 0.0
) -> tuple[float, float, float]:
    phi = math.radians(latitude)
    lam = math.radians(longitude)
    e2 = f * (2.0 - f)
    sin_phi, cos_phi = math.sin(phi), math.cos(phi)
    prime_vertical = a / math.sqrt(1.0 - e2 * sin_phi * sin_phi)
    return (
        (prime_vertical + height) * cos_phi * math.cos(lam),
        (prime_vertical + height) * cos_phi * math.sin(lam),
        (prime_vertical * (1.0 - e2) + height) * sin_phi,
    )


def _ecef_to_geodetic(
    x: float, y: float, z: float, a: float, f: float
) -> tuple[float, float, float]:
    e2 = f * (2.0 - f)
    longitude = math.atan2(y, x)
    p = math.hypot(x, y)
    if p < 1e-9:
        latitude = math.copysign(math.pi / 2.0, z)
        b = a * (1.0 - f)
        height = abs(z) - b
        return math.degrees(latitude), math.degrees(longitude), height

    latitude = math.atan2(z, p * (1.0 - e2))
    height = 0.0
    for _ in range(12):
        sin_phi = math.sin(latitude)
        prime_vertical = a / math.sqrt(1.0 - e2 * sin_phi * sin_phi)
        cos_phi = math.cos(latitude)
        height = p / cos_phi - prime_vertical
        updated = math.atan2(z, p * (1.0 - e2 * prime_vertical / (prime_vertical + height)))
        if abs(updated - latitude) < 1e-14:
            latitude = updated
            break
        latitude = updated
    sin_phi = math.sin(latitude)
    prime_vertical = a / math.sqrt(1.0 - e2 * sin_phi * sin_phi)
    height = p / math.cos(latitude) - prime_vertical
    return math.degrees(latitude), math.degrees(longitude), height


def _helmert_matrix() -> tuple[tuple[float, float, float], ...]:
    """Coordinate-frame Helmert matrix for the supplied Pulkovo42 -> WGS84 params."""
    tx, ty, tz = 23.57, -140.95, -79.8
    del tx, ty, tz  # translations are applied separately by the caller
    rx = 0.0 * _ARCSEC_TO_RAD
    ry = -0.35 * _ARCSEC_TO_RAD
    rz = -0.79 * _ARCSEC_TO_RAD
    scale = 1.0 - 0.22e-6
    # EPSG coordinate-frame convention (rotation signs opposite position-vector).
    return (
        (scale, scale * rz, -scale * ry),
        (-scale * rz, scale, scale * rx),
        (scale * ry, -scale * rx, scale),
    )


def _mat_vec(matrix: tuple[tuple[float, float, float], ...], vector: tuple[float, float, float]) -> tuple[float, float, float]:
    return tuple(sum(row[index] * vector[index] for index in range(3)) for row in matrix)  # type: ignore[return-value]


def _inverse_3x3(matrix: tuple[tuple[float, float, float], ...]) -> tuple[tuple[float, float, float], ...]:
    a, b, c = matrix
    determinant = (
        a[0] * (b[1] * c[2] - b[2] * c[1])
        - a[1] * (b[0] * c[2] - b[2] * c[0])
        + a[2] * (b[0] * c[1] - b[1] * c[0])
    )
    if abs(determinant) < 1e-30:
        raise Msk61Error("Helmert transform matrix is singular")
    inverse_determinant = 1.0 / determinant
    return (
        ((b[1] * c[2] - b[2] * c[1]) * inverse_determinant,
         (a[2] * c[1] - a[1] * c[2]) * inverse_determinant,
         (a[1] * b[2] - a[2] * b[1]) * inverse_determinant),
        ((b[2] * c[0] - b[0] * c[2]) * inverse_determinant,
         (a[0] * c[2] - a[2] * c[0]) * inverse_determinant,
         (a[2] * b[0] - a[0] * b[2]) * inverse_determinant),
        ((b[0] * c[1] - b[1] * c[0]) * inverse_determinant,
         (a[1] * c[0] - a[0] * c[1]) * inverse_determinant,
         (a[0] * b[1] - a[1] * b[0]) * inverse_determinant),
    )


def _apply_helmert(
    vector: tuple[float, float, float], inverse: bool = False
) -> tuple[float, float, float]:
    tx, ty, tz = 23.57, -140.95, -79.8
    matrix = _helmert_matrix()
    if inverse:
        shifted = (vector[0] - tx, vector[1] - ty, vector[2] - tz)
        return _mat_vec(_inverse_3x3(matrix), shifted)
    transformed = _mat_vec(matrix, vector)
    return transformed[0] + tx, transformed[1] + ty, transformed[2] + tz


def _source_to_wgs84(latitude: float, longitude: float) -> tuple[float, float]:
    source_xyz = _geodetic_to_ecef(latitude, longitude, _KRASSOVSKY_A, _KRASSOVSKY_F)
    target_xyz = _apply_helmert(source_xyz)
    target_latitude, target_longitude, _ = _ecef_to_geodetic(
        *target_xyz, _WGS84_A, _WGS84_F
    )
    return target_latitude, target_longitude


def _wgs84_to_source(latitude: float, longitude: float) -> tuple[float, float]:
    target_xyz = _geodetic_to_ecef(latitude, longitude, _WGS84_A, _WGS84_F)
    source_xyz = _apply_helmert(target_xyz, inverse=True)
    source_latitude, source_longitude, _ = _ecef_to_geodetic(
        *source_xyz, _KRASSOVSKY_A, _KRASSOVSKY_F
    )
    return source_latitude, source_longitude


def msk61_to_latlon(
    x: float,
    y: float,
    zone: int,
    profile: Profile | str = Profile.CALCULATED,
) -> GeographicResult:
    """Convert local MSK-61 X (northing), Y (easting) to geographic degrees.

    ``zone`` is required and must be 1, 2, or 3. The CALCULATED profile returns
    the inverse projection on Krassovsky; the GOST profile subsequently applies
    the supplied Pulkovo 1942 -> WGS 84 Helmert parameters.
    """
    selected_profile = _profile(profile)
    selected_zone = _zone(zone)
    if not _finite(x, y):
        raise Msk61Error("X and Y must be finite numbers")
    latitude, longitude = _inverse_tm(float(x), float(y), selected_zone)
    if selected_profile is Profile.GOST_51794_2008:
        latitude, longitude = _source_to_wgs84(latitude, longitude)
    return GeographicResult(
        latitude=latitude,
        longitude=longitude,
        zone=zone,
        profile=selected_profile,
        status=_PRELIMINARY_STATUS,
        warning=_GOST_WARNING if selected_profile is Profile.GOST_51794_2008 else _CALCULATED_WARNING,
    )


def latlon_to_msk61(
    latitude: float,
    longitude: float,
    zone: int,
    profile: Profile | str = Profile.CALCULATED,
) -> Msk61Result:
    """Convert geographic decimal degrees to local MSK-61 X/Y.

    Geographic input for GOST_51794_2008 is treated as WGS 84 and inverse-shifted
    to Pulkovo 1942 before projection. Ellipsoidal height is assumed to be zero.
    """
    selected_profile = _profile(profile)
    selected_zone = _validate_latlon(latitude, longitude, zone)
    source_latitude, source_longitude = float(latitude), float(longitude)
    if selected_profile is Profile.GOST_51794_2008:
        source_latitude, source_longitude = _wgs84_to_source(source_latitude, source_longitude)
        if abs(source_longitude - selected_zone.central_meridian) > _MAX_LON_DELTA:
            raise Msk61Error("datum-shifted position is outside the selected zone's preliminary domain")
    northing, easting = _forward_tm(source_latitude, source_longitude, selected_zone)
    if not _finite(northing, easting):
        raise Msk61Error("coordinate transform produced a non-finite result")
    return Msk61Result(
        x=northing,
        y=easting,
        zone=zone,
        profile=selected_profile,
        status=_PRELIMINARY_STATUS,
        warning=_GOST_WARNING if selected_profile is Profile.GOST_51794_2008 else _CALCULATED_WARNING,
    )


__all__ = [
    "GeographicResult",
    "Msk61Coordinate",
    "Msk61Error",
    "Msk61Result",
    "Profile",
    "latlon_to_msk61",
    "msk61_to_latlon",
]
