"""Orbital geometry: TLE -> TEME -> ECEF -> geodetic, and station-relative look angles.

Written by hand on top of bare `sgp4` rather than using a batteries-included
library, deliberately. `sgp4` gives you a TEME state vector and nothing else;
everything from there to "what azimuth is the dish pointing and can it see the
bird" is in this file. That is the part of this project worth being able to
explain, so it is the part that is not delegated.

The chain, in order:

    TLE or elements -> Satrec           (sgp4)
    Satrec.sgp4(jd, fr) -> r_TEME       (km, True Equator Mean Equinox of date)
    TEME -> ECEF                        rotate about Z by GMST
    ECEF -> geodetic                    WGS84 lat/lon/alt
    station geodetic -> ECEF            forward transform
    (target_ECEF - station_ECEF) -> ENU -> azimuth / elevation / slant range

WHAT IS DELIBERATELY OMITTED, and what it costs. Written down because "I didn't
know" and "I decided it didn't matter" are very different answers, and only one of
them is still true a year later:

  * Polar motion (the x_p, y_p wobble of the rotation axis in the crust). This
    is the ONLY real omission in TEME->ECEF. Worth about 15 m of ground
    position. Ignored.
  * UT1 vs UTC. GMST is properly a function of UT1; we feed it UTC. |DUT1| is
    bounded by 0.9 s by definition, which is up to 13.5 arcsec of Earth
    rotation, or roughly 400 m of ground displacement at the equator. In 2026
    DUT1 happens to sit near zero so the real error is far smaller, but the
    BOUND is what matters for an honest claim. At LEO slant ranges this is
    ~0.02 deg of azimuth, which is two orders of magnitude below the 15 deg
    mask we are testing against.
  * Atmospheric refraction. Real receivers see a satellite slightly before
    geometric rise: 0.1-0.5 deg of apparent elevation lift near the horizon,
    which moves AOS and LOS by a few seconds against a real tracker. Not
    modelled. This is the largest single reason our pass times will differ from
    heavens-above by seconds rather than by nothing.

  NOT omitted, because it does not belong here: nutation and precession. Those
  live in the TEME->GCRF/J2000 direction. TEME->ECEF via GMST does not involve
  them at all, and claiming to have "skipped nutation" here would be a category
  error rather than a simplification.

ELLIPSOID NOTE. SGP4 is defined on WGS72 and we pass `WGS72` to it, because
that is the convention TLEs are published in. The geodetic conversion below uses
WGS84, because that is what "latitude and longitude" means to every consumer of
this API. The two ellipsoids differ by a few metres. Mixing them here is
correct, not sloppy: each is used where its own convention applies.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# WGS84, for geodetic coordinates.
WGS84_A = 6378.137  # semi-major axis, km
WGS84_F = 1.0 / 298.257223563  # flattening
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)  # first eccentricity squared

# WGS72, matching what we hand to sgp4. Used only for mean-motion derivation
# when generating synthetic constellations (see satellites.py).
WGS72_MU = 398600.8  # km^3 / s^2

TAU = 2.0 * math.pi


@dataclass(frozen=True)
class Geodetic:
    """A point on or above the WGS84 ellipsoid."""

    lat_deg: float
    lon_deg: float
    alt_km: float


@dataclass(frozen=True)
class LookAngles:
    """Where a target is, as seen from a ground site."""

    azimuth_deg: float  # 0 = true north, increasing clockwise (east = 90)
    elevation_deg: float  # 0 = local horizontal, 90 = overhead
    slant_range_km: float


def gmst_rad(jd_ut1: float) -> float:
    """Greenwich Mean Sidereal Time as an angle, radians, from a Julian date.

    Vallado's polynomial (Fundamentals of Astrodynamics and Applications, the
    `gstime` form). Expressed in seconds of sidereal time first, then folded
    into a day and converted, which is how the reference states it: 240 sec of
    sidereal time is one degree.

    We pass UTC where UT1 is wanted. See the module docstring for the bound.
    """
    t = (jd_ut1 - 2451545.0) / 36525.0
    seconds = (
        67310.54841
        + (876600.0 * 3600.0 + 8640184.812866) * t
        + 0.093104 * t * t
        - 6.2e-6 * t * t * t
    )
    degrees = (seconds % 86400.0) / 240.0
    return math.radians(degrees) % TAU


def teme_to_ecef(r_teme_km: tuple[float, float, float], gmst: float) -> tuple[float, float, float]:
    """Rotate a TEME position into an Earth-fixed frame.

    TEME and ECEF share the Z axis; they differ by the Earth's rotation about
    it. So this is a single rotation by -GMST, and nothing else. Polar motion
    would be a further sub-arcsecond tilt (see module docstring).
    """
    x, y, z = r_teme_km
    c, s = math.cos(gmst), math.sin(gmst)
    return (x * c + y * s, -x * s + y * c, z)


def ecef_to_geodetic(x: float, y: float, z: float) -> Geodetic:
    """ECEF (km) -> WGS84 latitude, longitude, altitude.

    Iterative, because the closed forms are harder to read for no benefit at
    this scale. Longitude is exact and direct; latitude and altitude are
    coupled through the local radius of curvature, so they are solved together.
    Four iterations is far past convergence for anything from the sea floor to
    geostationary; the loop exits early once latitude stops moving.
    """
    lon = math.atan2(y, x)
    p = math.hypot(x, y)

    if p < 1e-9:  # on the spin axis: longitude is undefined, pick zero
        sign = 1.0 if z >= 0 else -1.0
        b = WGS84_A * (1.0 - WGS84_F)
        return Geodetic(sign * 90.0, 0.0, abs(z) - b)

    lat = math.atan2(z, p * (1.0 - WGS84_E2))  # spherical first guess
    alt = 0.0
    for _ in range(8):
        sin_lat = math.sin(lat)
        n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
        alt = p / math.cos(lat) - n
        next_lat = math.atan2(z, p * (1.0 - WGS84_E2 * n / (n + alt)))
        if abs(next_lat - lat) < 1e-13:
            lat = next_lat
            break
        lat = next_lat

    sin_lat = math.sin(lat)
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    alt = p / math.cos(lat) - n
    return Geodetic(math.degrees(lat), math.degrees(lon), alt)


def geodetic_to_ecef(lat_deg: float, lon_deg: float, alt_km: float) -> tuple[float, float, float]:
    """WGS84 latitude, longitude, altitude -> ECEF (km). The forward transform."""
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    return (
        (n + alt_km) * cos_lat * math.cos(lon),
        (n + alt_km) * cos_lat * math.sin(lon),
        (n * (1.0 - WGS84_E2) + alt_km) * sin_lat,
    )


def look_angles(
    site: Geodetic,
    site_ecef: tuple[float, float, float],
    target_ecef: tuple[float, float, float],
) -> LookAngles:
    """Azimuth, elevation and slant range of a target as seen from a site.

    The site's ECEF position is passed in rather than recomputed, because in a
    pass search this is called thousands of times for a site that never moves.

    Method: take the ECEF difference vector, then rotate it into the site's
    local East-North-Up frame. Azimuth and elevation are then just the two
    angles of that vector in ENU. Elevation uses atan2 against the horizontal
    magnitude rather than asin(U/range): same answer, but it stays well
    conditioned when the target is near the zenith.
    """
    dx = target_ecef[0] - site_ecef[0]
    dy = target_ecef[1] - site_ecef[1]
    dz = target_ecef[2] - site_ecef[2]

    lat, lon = math.radians(site.lat_deg), math.radians(site.lon_deg)
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    sin_lon, cos_lon = math.sin(lon), math.cos(lon)

    east = -sin_lon * dx + cos_lon * dy
    north = -sin_lat * cos_lon * dx - sin_lat * sin_lon * dy + cos_lat * dz
    up = cos_lat * cos_lon * dx + cos_lat * sin_lon * dy + sin_lat * dz

    slant = math.sqrt(dx * dx + dy * dy + dz * dz)
    azimuth = math.degrees(math.atan2(east, north)) % 360.0
    elevation = math.degrees(math.atan2(up, math.hypot(east, north)))
    return LookAngles(azimuth, elevation, slant)


def ground_range_km(alt_km: float, mask_deg: float, earth_radius_km: float = 6371.0) -> float:
    """How far from a site a satellite can be and still clear the mask.

    Not used by the pass search: this exists because it is the number that
    decides whether a constellation can see the Arctic at all, and getting it
    wrong silently produces an empty sky that every geometry test passes.

    A 780 km satellite clears a 15 degree mask only within ~1737 km, while a
    53-degree-inclination ground track never comes closer than 3280 km to
    Alert. Hence the seeded constellation is polar. See seed.py.
    """
    mask = math.radians(mask_deg)
    central = math.acos(earth_radius_km * math.cos(mask) / (earth_radius_km + alt_km)) - mask
    return earth_radius_km * central
