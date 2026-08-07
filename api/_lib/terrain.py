"""Land and water, from the same polygons the map draws.

WHAT THIS IS FOR. Several asset kinds are only physically possible in one medium: a
sensor node and a Ranger patrol belong on land, a hydrophone and a vessel belong on
water. Those are not style rules, they are the difference between a plan that could be
executed and one that could not, and the validator refusing an impossible placement for
a stated physical reason is the most convincing thing this system does.

🔑 IT REUSES `public/data/land.json`, WHICH IS ALREADY ON DISK for the basemap. One file
answering both "what does the coastline look like" and "is this point on land" means the
picture and the rule can never disagree, which they would immediately if the rule were
checked against a second, better dataset.

⚠️ ITS LIMITS ARE STATED LOUDLY RATHER THAN DISCOVERED. This is Natural Earth 1:10m,
clipped to the Arctic and simplified by scripts/build_basemap.py, which is about 108,000
vertices north of 45 degrees. That is roughly fifteen times the detail of the 1:50m set
this project started with, and the upgrade was made because the polygons are load-bearing
rather than decorative: at 1:50m, CFS Alert and six real radar sites tested as open water.

The Canadian Arctic coastline is genuinely fractal, so even at this resolution a point can
sit inside a fjord and test as water, or a narrow isthmus can vanish. So:

  * A refusal is trustworthy: if this says a point is 40 km out in Viscount Melville
    Sound, it is.
  * An approval near a coast is NOT proof of anything, and nothing in this project
    should claim it is.
  * `margin_km` exists precisely so a caller can ask for a decision that is robust to
    the data, rather than one that depends on which side of a simplified line a point
    fell.

⚠️ NO POSTGIS, NO SHAPELY. Point-in-polygon over a hundred thousand vertices is a ray
cast behind a bounding-box reject, which is about forty lines. An extension or a dependency bought for
that would be a dependency to install, pin, and explain in a README.
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

from .mesh import haversine_km

# The renderer serves this from public/; the API reads the same file off disk. Resolved
# from this module's location so it works under uvicorn, pytest and the Vercel bundle
# alike, none of which agree about the working directory.
LAND_PATH = Path(__file__).resolve().parents[2] / "public" / "data" / "land.json"

# A ring, plus its bounding box, so the common case (a point nowhere near this island)
# costs four comparisons instead of a full ray cast.
_Ring = tuple[list[tuple[float, float]], float, float, float, float]


@lru_cache(maxsize=1)
def _rings() -> list[_Ring]:
    """Every outer ring of every land polygon, with bounding boxes.

    ⚠️ INTERIOR RINGS (lakes) ARE DELIBERATELY IGNORED. Great Bear Lake would otherwise
    test as water and be a legal hydrophone site, which is technically what the data
    says and operationally absurd. Treating all land as solid is the less wrong of the
    two available errors, and it is the one a reader can predict.

    🔒 FAILS CLOSED. A missing or unparseable land file raises rather than returning an
    empty list, because an empty list makes every point on earth read as water and every
    constraint check silently pass.
    """
    if not LAND_PATH.exists():
        raise RuntimeError(f"land polygons not found at {LAND_PATH}; terrain checks cannot run")

    data = json.loads(LAND_PATH.read_text())
    features = data.get("features")
    if not features:
        raise RuntimeError(f"{LAND_PATH} contains no features; refusing to treat that as all-water")

    out: list[_Ring] = []
    for feature in features:
        geom = feature.get("geometry") or {}
        kind = geom.get("type")
        if kind == "Polygon":
            polygons = [geom["coordinates"]]
        elif kind == "MultiPolygon":
            polygons = geom["coordinates"]
        else:
            continue
        for polygon in polygons:
            if not polygon:
                continue
            # [0] is the outer ring; [1:] are holes, ignored per the note above.
            ring = [(float(x), float(y)) for x, y in polygon[0]]
            if len(ring) < 3:
                continue
            lons = [p[0] for p in ring]
            lats = [p[1] for p in ring]
            out.append((ring, min(lons), min(lats), max(lons), max(lats)))
    return out


def _in_ring(ring: list[tuple[float, float]], lon: float, lat: float) -> bool:
    """Standard crossing-number ray cast, eastward.

    ⚠️ Rings that cross the antimeridian are not special-cased. Natural Earth splits
    those at 180 degrees, and every position in this project is in the western Arctic,
    so the case does not arise. If it ever does, it will read as a point being on the
    wrong side of a landmass rather than as a crash, which is worth knowing.
    """
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat):
            # x of the edge at this latitude
            x_cross = xi + (lat - yi) * (xj - xi) / (yj - yi)
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def is_land(lat: float, lon: float) -> bool:
    """Is this point on land, according to the polygons the map draws?"""
    for ring, min_lon, min_lat, max_lon, max_lat in _rings():
        if lon < min_lon or lon > max_lon or lat < min_lat or lat > max_lat:
            continue
        if _in_ring(ring, lon, lat):
            return True
    return False


def is_water(lat: float, lon: float) -> bool:
    return not is_land(lat, lon)


def distance_to_coast_km(lat: float, lon: float, search_deg: float = 3.0) -> float:
    """Roughly how far this point is from the nearest coastline vertex.

    Vertex distance, not true distance to the coastline segment, so it OVERSTATES the
    distance for a point beside a long straight edge. That bias is in the safe direction
    for the way it is used: it makes `margin_km` look larger than it is only where the
    coastline is sparsely sampled, and the caller is told the figure is approximate.

    `search_deg` bounds the scan. A point in the middle of an ocean legitimately has no
    coast nearby, and the returned `inf` says so rather than pretending.
    """
    best = math.inf
    for ring, min_lon, min_lat, max_lon, max_lat in _rings():
        if (
            lon < min_lon - search_deg
            or lon > max_lon + search_deg
            or lat < min_lat - search_deg
            or lat > max_lat + search_deg
        ):
            continue
        for rlon, rlat in ring:
            if abs(rlat - lat) > search_deg:
                continue
            d = haversine_km(lat, lon, rlat, rlon)
            if d < best:
                best = d
    return best


def nearest_land(lat: float, lon: float, max_km: float = 120.0) -> tuple[float, float] | None:
    """The closest coastline vertex to this point, or None if none is within `max_km`.

    🔑 THIS IS WHAT PLACES THE SENSOR NODES. A chokepoint axis is drawn down the middle
    of a strait, because that is where the water a sensor watches actually is, but a
    node has to physically sit on a shore. Snapping each planned position to the nearest
    coast puts it where one could really be installed, and does it from the same data
    the map draws so the result cannot look wrong on screen.

    ⚠️ Returns a VERTEX of the simplified coastline, so it is a shoreline within the
    resolution of the basemap, not a surveyed site. Good enough to be defensible, and
    described that way in the seed rather than as a real position.
    """
    best: tuple[float, float] | None = None
    best_d = max_km
    search_deg = max_km / 111.0 + 1.0
    for ring, min_lon, min_lat, max_lon, max_lat in _rings():
        if (
            lon < min_lon - search_deg
            or lon > max_lon + search_deg
            or lat < min_lat - search_deg
            or lat > max_lat + search_deg
        ):
            continue
        for rlon, rlat in ring:
            if abs(rlat - lat) > search_deg:
                continue
            d = haversine_km(lat, lon, rlat, rlon)
            if d < best_d:
                best_d = d
                best = (rlat, rlon)
    return best


# --------------------------------------------------------------------------
# The constraint table: which medium each kind can exist in
# --------------------------------------------------------------------------
#
# ⚠️ THE SEASON IS PART OF THIS AND IS NOT HIDDEN. The scenario is set in AUGUST, when
# the Northwest Passage is navigable, which is why eight vessels are transiting it. In
# August a snowmobile patrol cannot cross open water. In March it drives over the same
# water on sea ice, and the identical validator with `sea_ice` set would permit the
# route it refuses here.
#
# That is stated in the README as a parameter of the scenario rather than left as an
# assumption someone has to reverse-engineer from a refusal.

LAND_KINDS = {"node", "patrol", "launch_site", "radar"}
WATER_KINDS = {"hydrophone", "vessel"}
# A drone flies, a marker is an annotation, and neither has an opinion about the surface.
ANY_KINDS = {"uas", "marker"}

# 🔴 THE MOST IMPORTANT NUMBER IN THIS FILE, AND IT WAS MEASURED, NOT CHOSEN.
#
# A first version of this check refused any point whose medium disagreed with its kind.
# Run against the seeded world it rejected 30 of 62 assets, including CFS Alert, Rankin
# Inlet, and six real North Warning System radar sites. Those are unarguably on land;
# what is wrong is the resolution of a coastline simplified for drawing a globe.
#
# Measuring the distance from each disputed point to the nearest coastline vertex
# separated the two populations cleanly enough to act on:
#
#   real coastal places (settlements, radar sites)   3.5 - 20 km, one outlier at 32
#   genuinely mid-strait positions                    20 - 54 km
#
# ⚠️ Nearly every Arctic settlement is coastal BY DEFINITION, because that is where
# people live and where things get landed. So a check with no tolerance does not
# enforce a domain rule, it just rejects the Arctic.
#
# 🔴 THE TOLERANCE IS SET BY MEASUREMENT, AND IT MOVED ONCE THE BASEMAP IMPROVED.
#
# At Natural Earth 1:50m, the worst real place (Brevoort Island, a radar site on dry
# land) sat 20.5 km from the simplified coastline, while the middle of Victoria Strait
# sat 5.8 km from it. The two populations OVERLAPPED, so no threshold separated them, and
# the tolerance had to be 22 km. That permitted a node in the middle of Barrow Strait,
# which is exactly the placement the check exists to refuse.
#
# Rebuilding the basemap from 1:10m (scripts/build_basemap.py) changed the numbers, and
# that is the whole reason it was worth doing:
#
#   worst real place still testing as water    20.5 km  ->  4.8 km  (Eureka)
#   middle of Barrow Strait                    18.9 km  -> 17.4 km  (still clearly water)
#
# The populations now separate. 6 km sits above every real place and below every genuinely
# offshore point, so:
#
#   ✅ REFUSES: the middle of Barrow Strait (17 km), Viscount Melville Sound (63 km),
#      Amundsen Gulf (65 km), the Beaufort Sea. The placements that are actually wrong.
#   ⚪ PERMITS: the middle of Victoria Strait (3.7 km) and Peel Sound (5.9 km), which are
#      genuinely narrow enough that a few kilometres of water is not evidence of anything.
#
# 🔑 The check still refuses only what it can prove, and the point of the better basemap
# is that it can now prove much more. When it refuses, it is right, and it never
# contradicts what someone who knows the Arctic already knows about where Alert is.
COASTAL_TOLERANCE_KM = 6.0


def check_placement(kind: str, lat: float, lon: float, sea_ice: bool = False) -> str | None:
    """None if this placement is physically possible, or a plain-English reason if not.

    The message is written to be read by an operator rather than parsed by a machine,
    because it ends up in the audit log and in the reply to a command. It names the
    medium and the distance, not the rule: "43 km into Viscount Melville Sound" tells you
    what is wrong with the world, "constraint LAND_KINDS failed" tells you what is wrong
    with the code.

    🔒 IT ONLY REFUSES WHAT IT CAN PROVE. Within `COASTAL_TOLERANCE_KM` of a coastline
    the basemap cannot resolve which side of the shore a point is on, so the answer is
    "allowed", and the check says nothing rather than guessing. See the note above that
    constant: this is deliberate, and it is what keeps every refusal defensible.
    """
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return f"{lat:.4f}, {lon:.4f} is not a valid position"

    if kind in ANY_KINDS:
        return None

    on_land = is_land(lat, lon)
    wants_land = kind in LAND_KINDS
    if on_land == wants_land:
        return None

    # The medium is wrong. Is it wrong by enough to be sure?
    offset = distance_to_coast_km(lat, lon)
    if offset <= COASTAL_TOLERANCE_KM:
        return None

    label = kind.replace("_", " ")
    if wants_land:
        if sea_ice:
            return None
        return (
            f"that position is roughly {offset:.0f} km out in open water, and a {label} "
            "has to be on land. The scenario is set in August, so there is no sea ice to cross"
        )
    return (
        f"that position is roughly {offset:.0f} km inland, and a {label} has to be in water"
    )
