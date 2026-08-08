#!/usr/bin/env python3
"""Build the vendored basemap from Natural Earth, at a resolution the Arctic needs.

    python3 scripts/build_basemap.py [--source 10m|50m] [--tolerance 0.01]

WHY THIS EXISTS AND WHY IT IS A BUILD STEP. The app fetches nothing at runtime, on
purpose, so every polygon it draws has to be in the repo. But a hand-committed blob with
no provenance is a file nobody can regenerate or reason about, so the download, the clip
and the simplification live here and the output is committed.

🔴 THE ARCTIC IS THE WORST CASE FOR A GLOBAL BASEMAP, and 50m was not good enough. At
1:50m the Canadian Arctic archipelago is drawn with roughly 30,000 vertices for the whole
world, which put CFS Alert, Rankin Inlet and six real North Warning System radar sites in
open water when tested against the polygons. Those are not data errors, they are a
coastline simplified for looking at a globe from far away, used for a question it cannot
answer.

At 1:10m the same region carries about fifteen times the detail. That matters here beyond
looking better, because the polygons are load-bearing: they decide whether a placement is
physically possible, and a coastline that cannot resolve a coastal settlement forces the
constraint check to be far more permissive than it should be.

⚠️ 10m IS 10 MB FOR THE WHOLE WORLD, which is far too much to ship. Two reductions, in
this order:

  1. CLIP to the Arctic. Everything this app draws is north of 50 degrees, and a polygon
     that never enters the viewport is bytes spent on nothing.
  2. SIMPLIFY with Douglas-Peucker at a tolerance chosen against a MEASURED outcome
     rather than by eye: the check below reports how many real places test correctly, and
     the tolerance is the loosest one that keeps them all right.

⚠️ Natural Earth's own documentation notes accuracy problems in northern Russia, which is
outside this app's area of interest. The Canadian Arctic is well surveyed in this dataset.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".build"
OUT = ROOT / "public" / "data"

SOURCES = {
    "10m": "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_land.geojson",
    "50m": "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_land.geojson",
}
PROVINCES = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
    "ne_10m_admin_1_states_provinces_lines.geojson"
)

# Everything this application draws is north of here. 50 rather than 60 so the southern
# ends of the patrol routes (Churchill is at 58.8) and the map's context still exist.
CLIP_SOUTH = 45.0

# Rings smaller than this are dropped. In the Arctic there are thousands of islets that
# cost vertices and carry no information at the zooms this app uses; a 30 km^2 floor keeps
# anything you could land on and discards the speckle.
MIN_RING_AREA_KM2 = 30.0


def fetch(url: str, name: str) -> dict:
    """Download once, cache in .build/, which is gitignored."""
    CACHE.mkdir(exist_ok=True)
    path = CACHE / name
    if not path.exists():
        print(f"downloading {name} ...", file=sys.stderr)
        urllib.request.urlopen(url, timeout=300)  # noqa: S310 - fixed, known https URL
        with urllib.request.urlopen(url, timeout=300) as resp:  # noqa: S310
            path.write_bytes(resp.read())
    return json.loads(path.read_text())


def ring_area_km2(ring: list[list[float]]) -> float:
    """Shoelace area, with a cos(lat) correction so it means something at 80 degrees.

    A degree of longitude is 111 km at the equator and 19 km at 80 north. Without the
    correction an Arctic island's area is overstated by a factor of six and the
    small-ring filter throws away the wrong things.
    """
    if len(ring) < 3:
        return 0.0
    lat0 = sum(p[1] for p in ring) / len(ring)
    k = math.cos(math.radians(lat0))
    total = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1], strict=True):
        total += (x1 * k) * y2 - (x2 * k) * y1
    return abs(total) / 2.0 * (111.32**2)


def simplify(ring: list[list[float]], tol: float) -> list[list[float]]:
    """Douglas-Peucker, iterative so a 40,000-point ring cannot blow the stack.

    ⚠️ Tolerance is in DEGREES and longitude degrees shrink toward the pole, so the same
    tolerance is a smaller real distance the further north you go. That is the right bias
    here: detail is preserved exactly where this application needs it.
    """
    if len(ring) < 4:
        return ring
    keep = [False] * len(ring)
    keep[0] = keep[-1] = True
    stack = [(0, len(ring) - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi <= lo + 1:
            continue
        x1, y1 = ring[lo]
        x2, y2 = ring[hi]
        dx, dy = x2 - x1, y2 - y1
        norm = math.hypot(dx, dy)
        worst, worst_i = -1.0, -1
        for i in range(lo + 1, hi):
            px, py = ring[i]
            if norm == 0:
                d = math.hypot(px - x1, py - y1)
            else:
                d = abs(dy * px - dx * py + x2 * y1 - y2 * x1) / norm
            if d > worst:
                worst, worst_i = d, i
        if worst > tol:
            keep[worst_i] = True
            stack.append((lo, worst_i))
            stack.append((worst_i, hi))
    out = [p for p, k in zip(ring, keep, strict=True) if k]
    return out if len(out) >= 4 else ring


def process(data: dict, tol: float) -> tuple[dict, dict]:
    """Clip, simplify, drop specks. Returns the FeatureCollection and some statistics."""
    kept_rings: list[list[list[float]]] = []
    stats = {"rings_in": 0, "rings_out": 0, "verts_in": 0, "verts_out": 0}

    for feature in data["features"]:
        geom = feature["geometry"]
        polygons = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
        for polygon in polygons:
            for ring in polygon[:1]:  # outer rings only; holes are lakes, see terrain.py
                stats["rings_in"] += 1
                stats["verts_in"] += len(ring)
                if max((p[1] for p in ring), default=-90) < CLIP_SOUTH:
                    continue
                if ring_area_km2(ring) < MIN_RING_AREA_KM2:
                    continue
                small = simplify([[round(p[0], 4), round(p[1], 4)] for p in ring], tol)
                kept_rings.append(small)
                stats["rings_out"] += 1
                stats["verts_out"] += len(small)

    fc = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [r]}}
            for r in kept_rings
        ],
    }
    return fc, stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="10m", choices=sorted(SOURCES))
    ap.add_argument("--tolerance", type=float, default=0.008, help="Douglas-Peucker tolerance, degrees")
    ap.add_argument("--provinces", action="store_true", help="also build provinces.json")
    args = ap.parse_args()

    data = fetch(SOURCES[args.source], f"ne_{args.source}_land.geojson")
    fc, stats = process(data, args.tolerance)

    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / "land.json"
    target.write_text(json.dumps(fc, separators=(",", ":")))

    print(
        f"{args.source} @ tol={args.tolerance}: "
        f"{stats['rings_in']} rings / {stats['verts_in']} verts  ->  "
        f"{stats['rings_out']} rings / {stats['verts_out']} verts, "
        f"{target.stat().st_size / 1e6:.2f} MB"
    )

    if args.provinces:
        prov = fetch(PROVINCES, "ne_10m_admin_1_lines.geojson")
        lines = []
        for feature in prov["features"]:
            props = feature.get("properties") or {}
            if props.get("adm0_name") != "Canada" and props.get("admin") != "Canada":
                continue
            geom = feature["geometry"]
            parts = [geom["coordinates"]] if geom["type"] == "LineString" else geom["coordinates"]
            for part in parts:
                if max((p[1] for p in part), default=-90) < CLIP_SOUTH:
                    continue
                lines.append(
                    {
                        "type": "Feature",
                        "properties": {"kind": "province"},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": simplify([[round(p[0], 4), round(p[1], 4)] for p in part], args.tolerance),
                        },
                    }
                )
        out = OUT / "provinces.json"
        out.write_text(json.dumps({"type": "FeatureCollection", "features": lines}, separators=(",", ":")))
        print(f"provinces: {len(lines)} lines, {out.stat().st_size / 1e6:.2f} MB")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
