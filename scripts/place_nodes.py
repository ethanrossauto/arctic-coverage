#!/usr/bin/env python3
"""Place the mesh sensor nodes along the chokepoints, on land, at radio spacing.

    python3 scripts/place_nodes.py

WHY THIS IS A SCRIPT AND NOT RUNTIME CODE. The positions it produces are pasted into
`api/_lib/assets.py` as an explicit table. That is deliberate:

  * The seed stays deterministic and reviewable. A reader can look up any node and see
    where it is, rather than having to run a snapping algorithm in their head.
  * The API keeps no runtime dependency on the coastline for seeding.
  * The provenance is still in the repo, so "why is this node here" has an answer that
    is not "someone typed it".

THE PROBLEM IT SOLVES. A chokepoint axis runs down the middle of a strait, because that
is where the water a sensor watches is. A sensor node has to sit on a shore. And the
nodes have to be close enough together to actually form a mesh, which at a 12 m mast
means 28.5 km of radio horizon, so they are spaced at about 22 km for margin.

⚠️ ONE SHORE PER CLUSTER, NOT BOTH. Alternating banks looks more thorough and would
break the mesh: the straits here are 40-80 km wide, so a node on the far bank is out of
range of everything. A shore-following chain is both operationally sensible and the only
version that stays connected.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api._lib.mesh import haversine_km, radio_horizon_km  # noqa: E402
from api._lib.terrain import _rings, is_land  # noqa: E402

# Target spacing, and the horizon it is derived from. 22 km is about 77% of the pairwise
# horizon for two 12 m masts, which leaves margin for the terrain this model does not
# have and for a neighbour going down.
NODE_MAST_M = 12.0
TARGET_SPACING_KM = 22.0
MAX_SPACING_KM = radio_horizon_km(NODE_MAST_M, NODE_MAST_M)

# Each chokepoint as an axis down the middle of the waterway, plus how many nodes to put
# on it. The axes are the real navigable routes; the nodes end up on whichever shore is
# nearest each point along them.
AXES: list[tuple[str, str, int, list[tuple[float, float]]]] = [
    (
        "barrow",
        "Barrow Strait / Lancaster Sound",
        7,
        # West to east down Barrow Strait into Lancaster Sound: the eastern gate of the
        # Northwest Passage, which everything transiting from the Atlantic side must use.
        [(74.40, -96.50), (74.35, -94.00), (74.25, -91.00), (74.15, -88.00)],
    ),
    (
        "pow",
        "Prince of Wales Strait / Amundsen Gulf",
        6,
        # The western gate: down Prince of Wales Strait between Banks and Victoria
        # Islands, then out through Amundsen Gulf.
        [(73.30, -115.00), (72.40, -117.80), (71.60, -120.20), (70.60, -123.00)],
    ),
    (
        "victoria",
        "Victoria Strait / Franklin Strait",
        5,
        # The middle route: the shallow, ice-choked stretch where a transit has the least
        # room to manoeuvre.
        [(71.80, -96.00), (70.90, -97.80), (69.90, -100.20), (69.20, -101.60)],
    ),
    (
        "nares",
        "Ellesmere / Nares Strait",
        6,
        # The polar route: Smith Sound into Kane Basin and up Nares Strait, the channel
        # between Ellesmere Island and Greenland, past Eureka and Alert.
        #
        # ⚠️ The first version of this axis started at (76.5, -79.0), which is Jones
        # Sound, a different waterway two degrees south. The placer did exactly as it was
        # told and produced a tidy connected chain on the south coast of Ellesmere, named
        # after a strait it was nowhere near. Worth remembering: every automated check
        # here passed, because none of them knows what a place is called.
        [(78.20, -74.00), (79.50, -71.00), (80.60, -67.00), (81.70, -62.50)],
    ),
]


def interpolate(axis: list[tuple[float, float]], step_km: float = 4.0) -> list[tuple[float, float]]:
    """Dense sample points along a polyline axis, roughly `step_km` apart."""
    out: list[tuple[float, float]] = []
    for (lat1, lon1), (lat2, lon2) in zip(axis, axis[1:]):
        seg = haversine_km(lat1, lon1, lat2, lon2)
        n = max(1, int(seg / step_km))
        for i in range(n):
            f = i / n
            out.append((lat1 + (lat2 - lat1) * f, lon1 + (lon2 - lon1) * f))
    out.append(axis[-1])
    return out


def nearest_shore(lat: float, lon: float, max_km: float = 90.0) -> tuple[float, float] | None:
    """Closest coastline vertex, pulled very slightly inland so it tests as land.

    ⚠️ A coastline VERTEX sits exactly on the boundary, and a point-in-polygon test on a
    boundary point is a coin toss decided by floating-point rounding. Nudging 0.01
    degrees (about 1 km) toward the polygon's interior makes the result stable. The
    direction is taken from the neighbouring vertices' midpoint, which is inside the
    landmass for any locally convex stretch of coast, and is checked rather than assumed.
    """
    best = None
    best_d = max_km
    for ring, min_lon, min_lat, max_lon, max_lat in _rings():
        if lon < min_lon - 3 or lon > max_lon + 3 or lat < min_lat - 3 or lat > max_lat + 3:
            continue
        for idx, (rlon, rlat) in enumerate(ring):
            if abs(rlat - lat) > 3:
                continue
            d = haversine_km(lat, lon, rlat, rlon)
            if d < best_d:
                prev = ring[idx - 1]
                nxt = ring[(idx + 1) % len(ring)]
                mid_lon = (prev[0] + nxt[0]) / 2
                mid_lat = (prev[1] + nxt[1]) / 2
                for scale in (0.02, 0.05, 0.12, 0.3):
                    clat = rlat + (mid_lat - rlat) * scale
                    clon = rlon + (mid_lon - rlon) * scale
                    if is_land(clat, clon):
                        best_d, best = d, (round(clat, 4), round(clon, 4))
                        break
    return best


def _best_ring(axis: list[tuple[float, float]], corridor_km: float = 80.0) -> list[tuple[float, float]] | None:
    """The landmass that actually LINES this waterway, not merely the closest one.

    🔴 THE FIRST VERSION TOOK THE NEAREST RING AND IT WAS THE WRONG QUESTION. The ring
    closest to the western end of Barrow Strait turned out to have FOUR vertices: a small
    islet, sitting a little nearer the axis than the shore of Devon Island. Walking it
    produced one node and then wrapped around to where it started.

    The right question is which coastline FOLLOWS the strait, so rings are scored by how
    many of their vertices lie within the corridor. A shore that runs the length of the
    waterway scores dozens; an islet scores two or three.
    """
    best = None
    best_score = 2
    for ring, min_lon, min_lat, max_lon, max_lat in _rings():
        if len(ring) < 8:
            continue
        score = 0
        for rlon, rlat in ring:
            if min(haversine_km(rlat, rlon, a_lat, a_lon) for a_lat, a_lon in axis) <= corridor_km:
                score += 1
        if score > best_score:
            best_score, best = score, ring
    return best


def _walk_ring(ring: list[tuple[float, float]], start_idx: int, step: int, spacing_km: float):
    """Yield points every `spacing_km` ALONG the coastline, interpolating across edges.

    ⚠️ NODES CANNOT BE PLACED AT VERTICES ALONE. Natural Earth's edges here run 12-18 km
    on a well-sampled shore and far longer on a smoothed one, so "walk to the next
    vertex" gives spacing this project does not get to choose. Arc length is the
    quantity that matters, so the walk accumulates it and interpolates wherever the next
    node falls.
    """
    n = len(ring)
    carried = 0.0
    for hop in range(n):
        i = (start_idx + step * hop) % n
        j = (start_idx + step * (hop + 1)) % n
        (lon1, lat1), (lon2, lat2) = ring[i], ring[j]
        seg = haversine_km(lat1, lon1, lat2, lon2)
        if seg <= 0:
            continue
        travelled = spacing_km - carried
        while travelled <= seg:
            f = travelled / seg
            yield (lat1 + (lat2 - lat1) * f, lon1 + (lon2 - lon1) * f), (lat2 - lat1, lon2 - lon1)
            travelled += spacing_km
        carried = (carried + seg) % spacing_km


def _push_inland(lat: float, lon: float, dlat: float, dlon: float) -> tuple[float, float] | None:
    """Move a point on the coastline just inside the land, using the edge normal.

    Which side is inland depends on the polygon's winding and on whether this stretch is
    a headland or a bay, so both normals are tried and the answer is CHECKED. Returning
    None when neither is land is correct and better than guessing: it means the point sits
    on a sliver too thin for this coastline's resolution to represent.
    """
    norm = math.hypot(dlat, dlon)
    if norm == 0:
        return None
    # Perpendicular to the edge, scaled to roughly 2-6 km depending on the try.
    for eps in (0.03, 0.08, 0.18):
        for sign in (1, -1):
            clat = lat + sign * (-dlon / norm) * eps
            clon = lon + sign * (dlat / norm) * eps
            if is_land(clat, clon):
                return (round(clat, 4), round(clon, 4))
    return None


def _inland(ring: list[tuple[float, float]], idx: int) -> tuple[float, float] | None:
    """Nudge a coastline vertex just inside its own landmass so it tests as land.

    A vertex sits exactly on the boundary, where a point-in-polygon test is decided by
    floating-point rounding rather than by geography. The nudge direction comes from the
    midpoint of the two neighbouring vertices, which lies inside the landmass on any
    locally convex stretch of coast, and the result is CHECKED rather than assumed,
    because concave stretches (the inside of a bay) push the other way.
    """
    rlon, rlat = ring[idx]
    prev, nxt = ring[idx - 1], ring[(idx + 1) % len(ring)]
    mid_lon, mid_lat = (prev[0] + nxt[0]) / 2, (prev[1] + nxt[1]) / 2
    for scale in (0.02, 0.06, 0.15, 0.35, 0.7):
        clat = rlat + (mid_lat - rlat) * scale
        clon = rlon + (mid_lon - rlon) * scale
        if is_land(clat, clon):
            return (round(clat, 4), round(clon, 4))
    return None


def place(key: str, count: int, axis: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Walk the coastline lining the axis, placing a node every TARGET_SPACING_KM."""
    ring = _best_ring(axis)
    if ring is None:
        return []

    # Enter the ring at the vertex nearest the start of the waterway.
    start_idx = min(
        range(len(ring)),
        key=lambda i: haversine_km(axis[0][0], axis[0][1], ring[i][1], ring[i][0]),
    )
    end_lat, end_lon = axis[-1]
    probe = max(2, len(ring) // 100)
    fwd = ring[(start_idx + probe) % len(ring)]
    bwd = ring[(start_idx - probe) % len(ring)]
    step = 1 if haversine_km(end_lat, end_lon, fwd[1], fwd[0]) < haversine_km(end_lat, end_lon, bwd[1], bwd[0]) else -1

    chosen: list[tuple[float, float]] = []
    for (lat, lon), (dlat, dlon) in _walk_ring(ring, start_idx, step, TARGET_SPACING_KM):
        if min(haversine_km(lat, lon, a[0], a[1]) for a in axis) > 140.0:
            break
        pt = _push_inland(lat, lon, dlat, dlon)
        if pt is None:
            continue
        if chosen:
            gap = haversine_km(*chosen[-1], *pt)
            if gap < 8.0 or gap > MAX_SPACING_KM:
                continue
        chosen.append(pt)
        if len(chosen) == count:
            break
    return chosen


def main() -> int:
    print(f"# spacing target {TARGET_SPACING_KM} km, hard max {MAX_SPACING_KM:.1f} km "
          f"(radio horizon, {NODE_MAST_M:.0f} m masts)\n")
    ok = True
    for key, label, count, axis in AXES:
        pts = place(key, count, axis)
        gaps = [haversine_km(*a, *b) for a, b in zip(pts, pts[1:])]
        all_land = all(is_land(*p) for p in pts)
        linked = all(g <= MAX_SPACING_KM for g in gaps)
        status = "ok" if (all_land and linked and len(pts) == count) else "PROBLEM"
        print(f'    (\n        "{key}",\n        "{label}",\n        [')
        for lat, lon in pts:
            print(f"            ({lat:.4f}, {lon:.4f}),")
        print("        ],\n    ),")
        print(f"    # {status}: {len(pts)}/{count} placed, all on land={all_land}, "
              f"gaps {min(gaps):.1f}-{max(gaps):.1f} km\n" if gaps else "")
        ok = ok and status == "ok"
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
