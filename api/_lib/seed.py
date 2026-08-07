"""Seed world: five real Canadian Arctic sites and three real Iridium NEXT birds.

🔴 WHY THESE SATELLITES, AND WHY IT IS NOT A COSMETIC CHOICE.

A satellite's ground track never reaches further from the equator than its
inclination, and at a 15 degree elevation mask a satellite at 780 km
is only visible within about 1737 km of a site. Put those together against the
seeded sites:

    inclination 53 deg (Starlink-like)  ->  nearest track to Alert: 3280 km
    inclination 51.6 deg (ISS)          ->  further still
    inclination 86.4 deg (Iridium NEXT) ->  passes directly overhead

So a constellation at 53 degrees produces zero passes over Alert, Eureka,
Resolute Bay and Cambridge Bay. Not "fewer passes": zero, forever. And every
geometry test in this repo would still pass, because the physics would be
correctly reporting no coverage. The scenario, not the maths, is what makes or
breaks an Arctic coverage tool.

Iridium NEXT is the right answer twice over: 86.4 degrees clears all five sites,
and it is the constellation that actually carries Arctic satcom traffic today.

The three are drawn from three different orbital planes (RAAN 4, 67 and 301
degrees, which are 0, 60 and 120 degrees apart as great circles once the
180-degree ambiguity is folded out), so the seeded sky has genuinely
independent passes rather than three satellites in a line.

⚠️ TLEs ARE VENDORED, NOT FETCHED. Epoch is 2026-08-06 (day 218.5 of 2026).
There is no runtime network call anywhere in this application, deliberately: a
demo that dies because a third party rate-limited it is a demo that dies in
front of the person you were demoing to. The cost is drift. SGP4 accuracy decays
from roughly a kilometre at epoch to a few kilometres after a week, which moves
pass times by seconds and changes nothing about coverage geometry at a 15 degree
mask.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .geometry import Geodetic
from .satellites import Satellite, from_tle

# The project-wide elevation mask. One definition, referenced everywhere.
DEFAULT_MASK_DEG = 15.0

TLE_EPOCH = datetime(2026, 8, 6, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Site:
    """A ground site. `kind` distinguishes seeded infrastructure from user drops."""

    id: str
    name: str
    lat_deg: float
    lon_deg: float
    alt_m: float
    kind: str = "ground_station"

    @property
    def geodetic(self) -> Geodetic:
        return Geodetic(self.lat_deg, self.lon_deg, self.alt_m / 1000.0)


# Real places, with real coordinates. Invented sites invite questions that have
# no good answers, and these five are the actual spine of Canadian High Arctic
# presence: Alert is the northernmost permanently inhabited place on Earth.
SEED_SITES: list[Site] = [
    Site("gs-alert", "Alert", 82.5018, -62.3481, 30.0),
    Site("gs-eureka", "Eureka", 79.9833, -85.9333, 10.0),
    Site("gs-resolute", "Resolute Bay", 74.6973, -94.8297, 40.0),
    Site("gs-cambridge", "Cambridge Bay", 69.1169, -105.0597, 20.0),
    Site("gs-iqaluit", "Iqaluit", 63.7467, -68.5170, 34.0),
]

# Iridium NEXT, three planes. Fetched from Celestrak 2026-08-06 and frozen here.
SEED_TLES: list[tuple[str, str, str, str]] = [
    (
        "sat-iridium-128",
        "IRIDIUM 128",
        "1 42811U 17039J   26218.51225378  .00000018  00000+0 -55977-6 0  9996",
        "2 42811  86.3989   4.1078 0002227  92.5542 267.5909 14.34217630477198",
    ),
    (
        "sat-iridium-106",
        "IRIDIUM 106",
        "1 41917U 17003A   26218.60789712  .00000007  00000+0 -47112-5 0  9999",
        "2 41917  86.3916  67.4653 0002371  88.0439 272.1028 14.34217459500437",
    ),
    (
        "sat-iridium-113",
        "IRIDIUM 113",
        "1 42803U 17039A   26218.75276852  .00000000  00000+0 -72067-5 0  9995",
        "2 42803  86.3983 301.0487 0002062  96.6018 263.5413 14.34217472478434",
    ),
]


def seed_satellites() -> list[Satellite]:
    return [from_tle(sat_id, name, l1, l2) for sat_id, name, l1, l2 in SEED_TLES]


def seed_sites() -> list[Site]:
    return list(SEED_SITES)
