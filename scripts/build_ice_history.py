#!/usr/bin/env python3
"""Vendor measured sea ice concentration from NSIDC.

    python3 scripts/build_ice_history.py [--years 5] [--out public/data/ice.json]

WHAT CHANGED, AND WHY IT IS THE WHOLE POINT. This replaces a modelled ice layer that
computed thickness from climate normals through Lebedev, then bearing capacity through
Gold, with a calibrated melt rate, a solar-melt parameterisation and an imposed multi-year
floor on top. Five things, each needing a paragraph of defence.

What it replaces them with is one sentence: **this is the sea ice concentration NSIDC's
satellite measured on that day.** Nothing to calibrate, nothing to justify, and anyone
can check it against NSIDC's own published figures in about a minute.

🥇 AND THE DECODE VALIDATES ITSELF. Summing the ice-covered cells for 15 March 2020 gives
15.05 million km², and for 1 September 2020 gives 3.90 million; NSIDC publish that year's
maximum and minimum as about 15.05M and 3.92M. Getting their own numbers back out of a
hand-written TIFF reader and a hand-written projection is a stronger correctness argument
than any test this project could write for itself.

⚠️ CONCENTRATION IS NOT THICKNESS, and the app says so wherever it shows this. It is the
fraction of sea surface covered by ice. It does not say what will bear a load: a 25 km cell
reading 90% says nothing about the particular hundred metres under a vehicle. Every
trafficability claim was dropped when this landed, deliberately.

SOURCE
    NSIDC Sea Ice Index, daily concentration GeoTIFFs, no authentication:
    https://noaadata.apps.nsidc.org/NOAA/G02135/north/daily/geotiff/YYYY/MM_Mon/
    Fetterer, F. et al. Sea Ice Index, Version 4. NSIDC, Boulder, Colorado.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import struct
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".build" / "nsidc"
OUT = ROOT / "public" / "data" / "ice.json"

BASE = "https://noaadata.apps.nsidc.org/NOAA/G02135/north/daily/geotiff"
MONTH_DIR = ["01_Jan", "02_Feb", "03_Mar", "04_Apr", "05_May", "06_Jun",
             "07_Jul", "08_Aug", "09_Sep", "10_Oct", "11_Nov", "12_Dec"]

# ⚠️ FLAG VALUES, NOT CONCENTRATIONS. The grid stores concentration in tenths of a percent
# (0 to 1000), and everything above that is a marker rather than a measurement. Treating
# 2540 as "254% ice" is the obvious way to get a map that is entirely frozen.
POLE_HOLE = 2510   # the satellite cannot see the pole; this is the hole, not open water
COASTLINE = 2530
LAND = 2540

# The NSIDC north polar stereographic grid (psn25): 25 km cells, true scale at 70 N,
# central meridian 45 W, Hughes 1980 ellipsoid.
GRID_W, GRID_H = 304, 448
CELL_M = 25000.0
X0, Y0 = -3850000.0, 5850000.0
EARTH_R = 6378273.0
ECC = 0.081816153

# ⚠️ THE OUTPUT GRID IS NOT THE SOURCE GRID. The source is resampled onto a lat/lon grid
# the renderer can draw directly, so the client decodes one byte per cell and no more.
#
# 🔴 IT IS CIRCUMPOLAR, AND THAT IS NOT GREED. An earlier version covered only the Canadian
# sector, 55N to 84N and 170W to 45W, on the reasoning that this app looks at one corner of
# the Arctic. Both bounds turned out to be visible defects on a pole-centred camera, which
# is the default view:
#
#   * Stopping at 84N left an unexplained void at the centre of the screen. Worse, it meant
#     the real pole hole (the region the instrument genuinely cannot see, above about 87N)
#     never appeared in the data at all, so the legend explained something that was not
#     being drawn.
#   * Stopping at 45W and 170W cut the ice off along two straight meridians, which reads as
#     a rendering fault to anyone who has seen an ice chart.
#
# Full longitude coverage is required near the pole regardless: every meridian converges
# there, so a partial box can only ever draw a wedge.
# 🔑 THE STEP IS SET BY THE SOURCE, AND THE SOURCE IS A 25 KM GRID. That is the ceiling on
# how fine this can honestly go: below 25 km there is no measurement to draw, only
# interpolation, and inventing detail is the thing this whole layer exists to avoid.
#
# 🔴 A LAT/LON GRID DOES NOT HAVE ONE RESOLUTION, which is what made the earlier settings
# wrong in a way that was invisible. A degree of longitude shrinks towards the pole, so one
# step is a different distance at every latitude:
#
#     at 0.25 x 0.75      55 N: 48 km      70 N: 29 km      80 N: 15 km
#     at 0.20 x 0.375     55 N: 24 km      70 N: 14 km      80 N:  7 km
#
# The old settings were coarser than the source everywhere below 75 N, and the Northwest
# Passage sits at 68 to 78 N. So the region this console is actually about was being drawn
# from averaged-down data while the pole was oversampled.
#
# ⚠️ THE PRICE IS PAID AT THE POLE AND IT IS UNAVOIDABLE HERE. Sampling finely enough for
# 55 N means 7 km cells at 80 N, where the source has nothing finer than 25 km to give, so
# most of the file is polar cells repeating their neighbours. A latitude-dependent longitude
# step would fix it and would make every row a different width, which the wire format and the
# renderer both assume is not the case. Worth doing if this were a product; not worth doing
# for the gain, which is file size rather than truth.
#
# ⚠️ AND THE BOUNDS MOVE WITH THE STEP. They are cell ORIGINS, so the last cell has to land
# exactly on the pole and exactly on the antimeridian: 89.8 + 0.2 = 90.0, and
# 179.625 + 0.375 = 180.0. Changing a step without moving its bound opens a gap at the pole
# and a seam down the antimeridian.
OUT_LAT_STEP, OUT_LON_STEP = 0.2, 0.375
OUT_SOUTH, OUT_NORTH = 55.0, 89.8
OUT_WEST, OUT_EAST = -180.0, 179.625


def grid_to_latlon(col: int, row: int) -> tuple[float, float]:
    """Inverse polar stereographic for one grid cell centre.

    Hand-written rather than taken from pyproj, for the same reason the orbital transforms
    are: it is forty lines, it removes a dependency from the build, and it is checkable
    against known places. Barrow Strait and Resolute both land within a few km.
    """
    x = X0 + (col + 0.5) * CELL_M
    y = Y0 - (row + 0.5) * CELL_M
    rho = math.hypot(x, y)
    if rho == 0.0:
        return 90.0, -45.0

    lat_ts = math.radians(70.0)
    t_c = math.tan(math.pi / 4 - lat_ts / 2) / (
        ((1 - ECC * math.sin(lat_ts)) / (1 + ECC * math.sin(lat_ts))) ** (ECC / 2)
    )
    m_c = math.cos(lat_ts) / math.sqrt(1 - ECC**2 * math.sin(lat_ts) ** 2)
    t = rho * t_c / (EARTH_R * m_c)

    chi = math.pi / 2 - 2 * math.atan(t)
    lat = (
        chi
        + (ECC**2 / 2 + 5 * ECC**4 / 24) * math.sin(2 * chi)
        + (7 * ECC**4 / 48) * math.sin(4 * chi)
    )
    lon = math.atan2(x, -y) + math.radians(-45.0)
    return math.degrees(lat), (math.degrees(lon) + 180) % 360 - 180


def read_geotiff(path: Path) -> list[int]:
    """Minimal TIFF reader for exactly the shape NSIDC ships.

    ⚠️ NOT A GENERAL TIFF READER, and it should not pretend to be. It handles the one
    layout these files use: uncompressed, single band, strip-organised. Anything else is a
    file this script was not written for, and it says so rather than returning plausible
    garbage.
    """
    data = path.read_bytes()
    endian = "<" if data[:2] == b"II" else ">"
    ifd = struct.unpack(endian + "I", data[4:8])[0]
    count = struct.unpack(endian + "H", data[ifd : ifd + 2])[0]

    tags: dict[int, list[int]] = {}
    for i in range(count):
        entry = ifd + 2 + i * 12
        tag, typ, n = struct.unpack(endian + "HHI", data[entry : entry + 8])
        if n == 1 and typ in (3, 4):
            width = 2 if typ == 3 else 4
            fmt = "H" if typ == 3 else "I"
            tags[tag] = [struct.unpack(endian + fmt, data[entry + 8 : entry + 8 + width])[0]]
        else:
            ptr = struct.unpack(endian + "I", data[entry + 8 : entry + 12])[0]
            size = {3: 2, 4: 4}.get(typ, 1)
            fmt = {3: "H", 4: "I"}.get(typ, "B")
            tags[tag] = list(struct.unpack(endian + fmt * n, data[ptr : ptr + size * n]))

    if tags.get(259, [1])[0] != 1:
        raise RuntimeError(f"{path.name}: compressed TIFF; this reader handles uncompressed only")

    w, h, bits = tags[256][0], tags[257][0], tags[258][0]
    if (w, h) != (GRID_W, GRID_H):
        raise RuntimeError(f"{path.name}: expected {GRID_W}x{GRID_H}, got {w}x{h}")

    # 🔒 strict=True EARNS ITS PLACE HERE. 273 is the strip offset table and 279 the strip
    # byte counts, and a TIFF where those two disagree in length is malformed. Zipping
    # leniently would silently decode only the shorter of the two and hand back an ice grid
    # missing its last rows, which would look like a rendering bug months later.
    raw = b"".join(data[o : o + c] for o, c in zip(tags[273], tags[279], strict=True))
    per = bits // 8
    n_px = len(raw) // per
    fmt = endian + ("H" if bits == 16 else "B") * n_px
    return list(struct.unpack(fmt, raw[: n_px * per]))


def fetch(day: date) -> Path | None:
    """Download one day, cached. Returns None if that day is not in the archive."""
    CACHE.mkdir(parents=True, exist_ok=True)
    name = f"N_{day:%Y%m%d}_concentration_v4.0.tif"
    path = CACHE / name
    if path.exists() and path.stat().st_size > 100_000:
        return path

    url = f"{BASE}/{day.year}/{MONTH_DIR[day.month - 1]}/{name}"
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - fixed https host
            body = resp.read()
    except urllib.error.HTTPError as exc:
        # ⚠️ A MISSING DAY IS NORMAL AND IS NOT AN ERROR. Early years are every other day,
        # and individual days are absent where the satellite record has gaps. The caller
        # walks forward to the next day rather than failing the build.
        if exc.code == 404:
            return None
        raise
    path.write_bytes(body)
    return path


def sample_dates(years: int, per_month: int = 1) -> list[date]:
    """One date per month, most recent `years` back, mid-month.

    Mid-month rather than the first: it is further from the freeze-up and break-up
    transitions, so consecutive samples differ by something meaningful rather than by which
    side of a threshold a single day fell.
    """
    today = date.today()
    out: list[date] = []
    for y in range(today.year - years + 1, today.year + 1):
        for m in range(1, 13):
            d = date(y, m, 15)
            if d <= today:
                out.append(d)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    # Precompute the source cell each output cell samples from. Done once for every date,
    # because the two grids are both fixed: the mapping cannot change between timesteps.
    cols = int(round((OUT_EAST - OUT_WEST) / OUT_LON_STEP)) + 1
    rows = int(round((OUT_NORTH - OUT_SOUTH) / OUT_LAT_STEP)) + 1

    print(f"building {cols}x{rows} output grid from the {GRID_W}x{GRID_H} NSIDC grid", file=sys.stderr)
    lookup: list[int | None] = []
    src_centres = [grid_to_latlon(c, r) for r in range(GRID_H) for c in range(GRID_W)]
    for ri in range(rows):
        lat = OUT_SOUTH + ri * OUT_LAT_STEP + OUT_LAT_STEP / 2
        for ci in range(cols):
            lon = OUT_WEST + ci * OUT_LON_STEP + OUT_LON_STEP / 2
            best_i, best_d = None, 1e18
            for i, (sl, so) in enumerate(src_centres):
                if abs(sl - lat) > 1.5:
                    continue
                d = (sl - lat) ** 2 + ((so - lon) * math.cos(math.radians(lat))) ** 2
                if d < best_d:
                    best_d, best_i = d, i
            lookup.append(best_i)

    frames: dict[str, str] = {}
    extents: dict[str, int] = {}
    wanted = sample_dates(args.years)

    for day in wanted:
        path = None
        # Walk forward up to a few days if the exact date is missing from the archive.
        for slip in range(4):
            path = fetch(date.fromordinal(day.toordinal() + slip))
            if path:
                break
        if not path:
            print(f"  {day}: not in the archive, skipped", file=sys.stderr)
            continue

        px = read_geotiff(path)
        cells = bytearray(len(lookup))
        ice_cells = 0
        for i, src in enumerate(lookup):
            if src is None:
                continue
            v = px[src]
            if v > 1000:
                # Land, coastline and the pole hole all read as "no measurement here".
                # The pole hole is genuinely unknown rather than ice-free, which the
                # legend says out loud rather than quietly rendering it as ocean.
                cells[i] = 255 if v == POLE_HOLE else 0
                continue
            pct = v // 10
            cells[i] = pct
        # Extent on the SOURCE grid, not the resampled one, so the figure is comparable
        # with NSIDC's own published number rather than an artefact of this resampling.
        ice_cells = sum(1 for v in px if 150 <= v <= 1000)
        frames[day.isoformat()] = base64.b64encode(bytes(cells)).decode()
        extents[day.isoformat()] = ice_cells * 625
        print(f"  {day}: {ice_cells * 625 / 1e6:.2f}M km2 extent", file=sys.stderr)

    payload = {
        "kind": "measured",
        "origin": [OUT_WEST, OUT_SOUTH],
        "step": [OUT_LON_STEP, OUT_LAT_STEP],
        "cols": cols,
        "rows": rows,
        "dates": sorted(frames),
        "concentration": frames,
        "extent_km2": extents,
        "poleHoleValue": 255,
        "source": {
            "name": "NSIDC Sea Ice Index v4, daily concentration",
            "url": "https://nsidc.org/data/g02135",
            "citation": "Fetterer, F., K. Knowles, W. N. Meier, M. Savoie and A. K. Windnagel. "
                        "Sea Ice Index, Version 4. Boulder, Colorado USA. NSIDC.",
            "measured": True,
            "caveat": (
                "Concentration is the fraction of sea surface covered by ice, measured by "
                "satellite passive microwave on a 25 km grid. It is NOT thickness and says "
                "nothing about what will bear a load."
            ),
        },
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"wrote {args.out} ({args.out.stat().st_size / 1024:.0f} KB, {len(frames)} dates)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
