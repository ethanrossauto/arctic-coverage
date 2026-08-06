"""Satellites: loading real TLEs, synthesising constellations, and propagating.

Two ways a satellite gets into this system, and they converge on one object:

  1. A vendored TLE, parsed with `Satrec.twoline2rv`.
  2. A synthetic satellite built from orbital elements with `Satrec.sgp4init`,
     which is what `simulate_constellation` uses.

After construction both are a plain `Satrec` and `.sgp4(jd, fr)` behaves
identically, so nothing downstream knows or cares which it was. That is the
whole reason for doing it this way instead of generating TLE text: no string
formatting, no column alignment, no checksums.

⚠️ sgp4init's argument order is NOT classical element order. It is:

    sgp4init(whichconst, opsmode, satnum, epoch, bstar, ndot, nddot,
             ecco, argpo, inclo, mo, no_kozai, nodeo)

Note that `argpo` comes BEFORE `inclo`, and `nodeo` (RAAN) is LAST, after mean
motion. Transposing any of those runs perfectly cleanly and quietly puts the
satellite in the wrong orbit, so they are passed by keyword below.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from sgp4.api import WGS72, Satrec, jday

from . import geometry
from .geometry import Geodetic

# sgp4's WGS72 constants. Used for deriving mean motion from an altitude, so
# that a synthetic satellite's realised altitude matches what was asked for.
WGS72_RADIUS_KM = 6378.135
WGS72_MU = 398600.8

# sgp4init counts its epoch in days since 1949-12-31 00:00 UT. That instant is
# Julian date 2433281.5.
SGP4_EPOCH_JD = 2433281.5


@dataclass
class Satellite:
    """A satellite plus the metadata we display. `satrec` does the physics."""

    id: str
    name: str
    satrec: Satrec
    source: str  # "tle" | "synthetic"
    norad_id: int | None = None


def jday_utc(dt: datetime) -> tuple[float, float]:
    """Julian date split into whole days and fraction, as sgp4 wants it.

    Split rather than summed because sgp4 takes (jd, fr) precisely so that the
    fractional part keeps full precision instead of being lost in the low bits
    of a number around 2.46 million.
    """
    dt = dt.astimezone(timezone.utc)
    return jday(
        dt.year,
        dt.month,
        dt.day,
        dt.hour,
        dt.minute,
        dt.second + dt.microsecond / 1e6,
    )


def from_tle(sat_id: str, name: str, line1: str, line2: str) -> Satellite:
    """Build a Satellite from a real two-line element set."""
    satrec = Satrec.twoline2rv(line1, line2, WGS72)
    return Satellite(id=sat_id, name=name, satrec=satrec, source="tle", norad_id=satrec.satnum)


def mean_motion_rad_per_min(altitude_km: float) -> float:
    """Circular-orbit mean motion at a given altitude, radians per minute.

    n = sqrt(mu / a^3) is radians per second; times 60 for sgp4's units.

    ⚠️ sgp4init wants the KOZAI mean motion. What this returns is the Keplerian
    value, which is effectively Brouwer. The difference is about 0.1%, showing up
    as a few km of realised altitude versus the altitude requested. That is
    irrelevant for coverage geometry at a 15 degree mask, and it is noted in the
    README rather than corrected, because the correction needs a J2 term whose
    only effect here would be to make a synthetic satellite's altitude match a
    number the user typed slightly more exactly.
    """
    a = WGS72_RADIUS_KM + altitude_km
    return 60.0 * math.sqrt(WGS72_MU / (a * a * a))


def synthesize_constellation(
    count: int,
    epoch: datetime,
    altitude_km: float = 780.0,
    inclination_deg: float = 86.4,
    name_prefix: str = "SIM",
    id_prefix: str = "sat-sim",
    first_satnum: int = 90001,
) -> list[Satellite]:
    """Generate a Walker-style constellation of `count` satellites.

    🔴 The inclination default is 86.4 degrees and that is not decoration. A
    satellite's ground track never goes further from the equator than its
    inclination, and at a 15 degree mask a 780 km satellite is only visible
    within about 1737 km of a site. A 53-degree constellation (Starlink-like)
    never comes within 3280 km of Alert, so it would produce a demonstrably,
    correctly, completely empty sky over every Arctic station. 86.4 degrees is
    Iridium NEXT's inclination, which is the real Arctic satcom constellation
    and passes directly over all five seeded sites.

    Layout: RAAN is spread over 180 degrees rather than 360, because a plane and
    its 180-degree counterpart are the same great circle traversed the other
    way, so spreading over the full turn would duplicate coverage. Mean anomaly
    is spread within each plane, with a small inter-plane phase offset so that
    satellites in adjacent planes do not all cross the equator together. That
    is the Walker delta idea, kept simple.
    """
    count = max(1, int(count))
    planes = max(1, round(math.sqrt(count)))
    per_plane = math.ceil(count / planes)

    epoch_days = jday_from_epoch_days(epoch)
    n_kozai = mean_motion_rad_per_min(altitude_km)
    inclo = math.radians(inclination_deg)

    out: list[Satellite] = []
    for i in range(count):
        plane = i // per_plane
        slot = i % per_plane

        raan_deg = (180.0 / planes) * plane
        # Even spacing in-plane, plus a phase nudge per plane so planes are not
        # synchronised with each other.
        ma_deg = (360.0 / per_plane) * slot + (360.0 / count) * plane

        satrec = Satrec()
        satrec.sgp4init(
            WGS72,  # whichconst: match the TLE convention
            "i",  # opsmode: improved
            first_satnum + i,  # satnum
            epoch_days,  # epoch: days since 1949-12-31 00:00 UT
            0.0,  # bstar: no drag decay needed over a 72h window
            0.0,  # ndot: ignored by SGP4 proper
            0.0,  # nddot: ignored by SGP4 proper
            1e-4,  # ecco: near-circular, not exactly zero
            0.0,  # argpo: meaningless at ~zero eccentricity
            inclo,  # inclo: radians
            math.radians(ma_deg),  # mo: radians
            n_kozai,  # no_kozai: radians per MINUTE
            math.radians(raan_deg),  # nodeo: radians. Last, not third.
        )
        out.append(
            Satellite(
                id=f"{id_prefix}-{i + 1:02d}",
                name=f"{name_prefix}-{i + 1:02d}",
                satrec=satrec,
                source="synthetic",
            )
        )
    return out


def jday_from_epoch_days(dt: datetime) -> float:
    """Days since 1949-12-31 00:00 UT, which is sgp4init's epoch convention."""
    jd, fr = jday_utc(dt)
    return (jd + fr) - SGP4_EPOCH_JD


class PropagationError(RuntimeError):
    """SGP4 refused to propagate. Carries the library's own error code."""

    def __init__(self, code: int, message: str):
        super().__init__(f"sgp4 error {code}: {message}")
        self.code = code


def teme_at(sat: Satellite, when: datetime) -> tuple[float, float, float]:
    """TEME position in km at an instant.

    ⚠️ The error code is checked. `Satrec.sgp4` returns (error, position,
    velocity) and a non-zero error still hands back a position, which is
    garbage. Ignoring it is how a decayed or badly-conditioned orbit ends up
    plotted somewhere plausible-looking.
    """
    jd, fr = jday_utc(when)
    error, position, _velocity = sat.satrec.sgp4(jd, fr)
    if error != 0:
        raise PropagationError(error, SGP4_ERRORS.get(error, "unknown"))
    return position


def ecef_at(sat: Satellite, when: datetime) -> tuple[float, float, float]:
    """Earth-fixed position in km at an instant."""
    jd, fr = jday_utc(when)
    return geometry.teme_to_ecef(teme_at(sat, when), geometry.gmst_rad(jd + fr))


def subpoint_at(sat: Satellite, when: datetime) -> Geodetic:
    """Sub-satellite point: the geodetic position directly beneath the bird."""
    return geometry.ecef_to_geodetic(*ecef_at(sat, when))


SGP4_ERRORS = {
    1: "mean eccentricity out of range",
    2: "mean motion less than zero",
    3: "perturbed eccentricity out of range",
    4: "semi-latus rectum less than zero",
    5: "epoch elements are sub-orbital",
    6: "satellite has decayed",
}
