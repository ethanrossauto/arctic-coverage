"""The vendored sea ice measurements, checked against what NSIDC publishes.

🔑 WHY THIS TEST IS THE ONE WORTH HAVING. The ice layer makes exactly one claim: this
is the sea ice concentration a satellite measured on this date. That claim is checkable
against figures NSIDC publishes independently, so the test does not assert that the code
agrees with itself. It asserts that the decode agrees with the outside world.

The decode chain it guards is entirely hand-written: a GeoTIFF reader, an inverse polar
stereographic projection (true scale 70N, central meridian -45), and a resample onto the
render grid. Any one of those failing silently produces a map that still looks like ice.
The seasonal extent figures are what catch it, because a projection error moves ice onto
land and a scaling error moves the total.

Runs with no credentials, no network and no database: everything it reads is in the repo.
"""
from __future__ import annotations

import json
import struct
import zlib
from functools import cache
from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parents[1] / "public" / "data"
ICE_INDEX = DATA / "ice-index.json"
ICE_DIR = DATA / "ice"


@pytest.fixture(scope="module")
def ice() -> dict:
    if not ICE_INDEX.exists():
        pytest.skip(f"{ICE_INDEX} is not built; run scripts/build_ice_history.py")
    return json.loads(ICE_INDEX.read_text())


@cache
def _decode_png(path: str) -> tuple[int, int, bytes]:
    """Decode one 8-bit greyscale PNG back to raw pixels.

    🔑 THIS DOUBLES AS A CHECK ON THE ENCODER. It asserts the header really says 8-bit
    greyscale and that every row uses filter type 0, which is what the build claims to
    write. A test that decoded leniently would pass on a file no raster layer could read.

    Deliberately not a general PNG reader: it handles exactly the one shape this project
    produces, and anything else fails loudly rather than being quietly coerced.
    """
    raw = Path(path).read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"

    pos, width, height, idat = 8, 0, 0, bytearray()
    while pos < len(raw):
        (length,) = struct.unpack(">I", raw[pos : pos + 4])
        tag = raw[pos + 4 : pos + 8]
        body = raw[pos + 8 : pos + 8 + length]
        if tag == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", body[:10])
            assert depth == 8, f"{path} is {depth}-bit, expected 8"
            assert colour == 0, f"{path} colour type {colour}, expected 0 (greyscale)"
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
        pos += 12 + length

    flat = zlib.decompress(bytes(idat))
    stride = width + 1
    assert len(flat) == stride * height, f"{path} has the wrong number of scanlines"

    out = bytearray()
    for y in range(height):
        row = flat[y * stride : (y + 1) * stride]
        assert row[0] == 0, f"{path} row {y} uses filter {row[0]}, expected 0"
        out += row[1:]
    return width, height, bytes(out)


def cells(ice: dict, date: str) -> bytes:
    width, height, pixels = _decode_png(str(ICE_DIR / f"{date}.png"))
    assert (width, height) == (ice["cols"], ice["rows"]), (
        f"{date}.png is {width}x{height} but the index declares {ice['cols']}x{ice['rows']}"
    )
    return pixels


def value_at(ice: dict, date: str, lat: float, lon: float) -> int:
    """Concentration at a lat/lon, or -1 when the point is outside the render grid.

    ⚠️ Bounds-checked deliberately. Without the check an out-of-range longitude reads
    across a row boundary and returns a plausible number from somewhere else entirely,
    which is exactly the false alarm this function was written after.
    """
    lon0, lat0 = ice["origin"]
    dlon, dlat = ice["step"]
    i = round((lon - lon0) / dlon)
    j = round((lat - lat0) / dlat)
    if not (0 <= i < ice["cols"] and 0 <= j < ice["rows"]):
        return -1
    return cells(ice, date)[j * ice["cols"] + i]


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

def test_one_measurement_per_month_over_five_years(ice):
    dates = ice["dates"]
    assert len(dates) == len(set(dates)), "a date is vendored twice"
    assert dates == sorted(dates), "dates are not in chronological order"

    months = {d[:7] for d in dates}
    assert len(months) == len(dates), "two measurements fall in the same month"
    assert 55 <= len(dates) <= 61, f"expected about five years of months, got {len(dates)}"


def test_every_declared_date_has_a_file_and_nothing_is_orphaned(ice):
    """The index and the directory must agree in both directions.

    A date in the index with no file is a broken request at runtime. A file with no index
    entry is dead weight in a repo that publishes, and it is what happens when a rebuild
    changes the date set and nobody prunes.
    """
    declared = set(ice["dates"])
    on_disk = {p.stem for p in ICE_DIR.glob("*.png")}
    assert declared - on_disk == set(), f"declared but missing: {sorted(declared - on_disk)}"
    assert on_disk - declared == set(), f"orphaned files: {sorted(on_disk - declared)}"


def test_the_northern_edge_stops_short_of_the_pole(ice):
    """🔴 A MapLibre raster source cannot reach 90 N.

    Mercator is infinite at the pole, so a corner coordinate there resolves to an
    out-of-range tile and the layer does not render AT ALL. Measured: 89.5 fails, 89.0
    renders. This pins the bound so a future change to the step cannot quietly push the top
    edge back over it and blank the whole layer.
    """
    lat0 = ice["origin"][1]
    top = lat0 + ice["step"][1] * ice["rows"]
    assert top <= 89.0, f"the grid reaches {top} N; a raster source cannot render past 89.0"


def test_every_date_decodes_to_exactly_the_declared_grid(ice):
    expected = ice["cols"] * ice["rows"]
    for d in ice["dates"]:
        assert len(cells(ice, d)) == expected, f"{d} decodes to the wrong cell count"


def test_no_value_is_outside_the_declared_range(ice):
    hole = ice["poleHoleValue"]
    for d in ice["dates"]:
        bad = {v for v in cells(ice, d) if v > 100 and v != hole}
        assert not bad, f"{d} carries values that are neither concentration nor pole hole: {sorted(bad)}"


# ---------------------------------------------------------------------------
# Agreement with the published record
# ---------------------------------------------------------------------------

def test_march_and_september_bracket_the_published_seasonal_range(ice):
    """NSIDC's Arctic maximum runs about 14-15.5M km2 and the minimum about 3.5-5.5M.

    Bands rather than points on purpose: the vendored dates are mid-month samples, not
    the actual annual extremes, so demanding an exact figure would encode the sample
    rather than the physics. A projection or scaling error misses these by millions.
    """
    for d in ice["dates"]:
        extent = ice["extent_km2"][d] / 1e6
        if d[5:7] == "03":
            assert 13.0 <= extent <= 16.0, f"March {d} extent {extent:.2f}M km2 is outside the published range"
        if d[5:7] == "09":
            assert 3.0 <= extent <= 6.5, f"September {d} extent {extent:.2f}M km2 is outside the published range"


def test_every_winter_has_more_ice_than_the_following_summer(ice):
    """The one seasonal fact no Arctic dataset may get wrong."""
    by_month = {d[:7]: ice["extent_km2"][d] for d in ice["dates"]}
    checked = 0
    for key, march in by_month.items():
        year, month = key.split("-")
        if month != "03":
            continue
        sept = by_month.get(f"{year}-09")
        if sept is None:
            continue
        assert march > sept * 1.5, f"March {year} ({march}) is not clearly above September ({sept})"
        checked += 1
    assert checked >= 4, "not enough March/September pairs to be a real check"


# ---------------------------------------------------------------------------
# Geolocation. A projection error puts ice on dry land and still looks like a map.
# ---------------------------------------------------------------------------

# Interiors, far from any coast, so a cell or two of coastal bleed cannot reach them.
LAND = [
    ("Quebec interior", 55.0, -72.0),
    ("Ontario interior", 55.0, -88.0),
    ("Alaska interior", 65.0, -150.0),
    ("Yukon interior", 64.0, -135.0),
    ("Baffin Island interior", 68.5, -70.0),
    ("Ellesmere Island interior", 80.0, -75.0),
    ("Victoria Island interior", 70.5, -110.0),
    ("Banks Island interior", 73.0, -121.0),
]

# Open sea that carries ice every March without exception.
#
# ⚠️ Baffin Bay is deliberately NOT in this list. It looks like it belongs, and it does
# reach 100% in most years, but March 2025 measured 54% at 73N 65W. That is real: the
# ice edge and the North Water Polynya move a long way between years, and 2025 had a
# notably low maximum. An assertion that called that a failure would be encoding one
# year's weather as a law.
OCEAN_IN_MARCH = [
    ("Beaufort Sea", 73.0, -140.0),
    ("Hudson Bay centre", 59.0, -86.0),
    ("M'Clintock Channel", 71.5, -102.0),
]


@pytest.mark.parametrize("name,lat,lon", LAND, ids=[x[0] for x in LAND])
def test_land_is_never_reported_as_sea_ice(ice, name, lat, lon):
    for d in ice["dates"]:
        v = value_at(ice, d, lat, lon)
        if v == -1:
            pytest.skip(f"{name} is outside the render grid")
        assert v == 0, f"{name} reports concentration {v} on {d}"


@pytest.mark.parametrize("name,lat,lon", OCEAN_IN_MARCH, ids=[x[0] for x in OCEAN_IN_MARCH])
def test_reliably_frozen_water_is_frozen_in_march(ice, name, lat, lon):
    marches = [d for d in ice["dates"] if d[5:7] == "03"]
    assert marches, "no March measurements vendored"
    for d in marches:
        v = value_at(ice, d, lat, lon)
        if v == -1:
            pytest.skip(f"{name} is outside the render grid")
        assert v >= 80, f"{name} reports only {v}% ice on {d}"


def test_the_grid_stops_at_the_last_latitude_a_raster_source_will_render(ice):
    """🔴 A MapLibre raster source cannot reach the pole.

    Mercator is infinite at 90 N, so a corner coordinate there resolves to an out-of-range
    tile and the layer does not draw AT ALL. Measured by Lane B on the live page: 89.5 fails,
    89.0 renders. This pins the bound so a later change to the step cannot quietly push the
    top edge over it and blank the entire ice layer with no error anywhere.
    """
    top = ice["origin"][1] + ice["step"][1] * ice["rows"]
    assert top <= 89.0, f"the grid reaches {top} N; a raster source cannot render past 89.0"


def test_the_pole_hole_appears_only_where_the_source_has_one_and_only_at_the_top(ice):
    """The satellite cannot see the pole, and unmeasured is not the same as open water.

    🔴 THE HOLE IS NOT THE SAME SIZE THROUGHOUT THE RECORD, which this test found rather
    than assumed. Measured across all 55 vendored dates:

        2022, 2023, 2024   no hole cell falls inside this grid at all
        2025, 2026         every date has one, reaching down to 88.5 N

    The split is clean at 2025-01, which is the signature of a change in the source product
    rather than anything seasonal. Whatever the cause, the consequence for the display is
    concrete and worth stating: **older dates legitimately show no pole hole, newer ones do.**
    Anything drawing a legend entry for it has to tolerate its absence rather than treating an
    empty result as a decode failure.

    ⚠️ Asserting the hole is ALWAYS present is what this test used to do, and it was wrong for
    36 of the 55 dates. Asserting it is always absent would now be wrong for the other 19. The
    checkable property is that where it appears, it appears at the TOP, because a projection
    or resample error is exactly what would scatter it.
    """
    hole = ice["poleHoleValue"]
    assert hole > 100, "the sentinel must stay distinguishable from any concentration"

    cols, lat0, dlat = ice["cols"], ice["origin"][1], ice["step"][1]
    seen_any = False
    for d in ice["dates"]:
        px = cells(ice, d)
        rows_with = [j for j in range(ice["rows"]) if hole in set(px[j * cols : (j + 1) * cols])]
        if not rows_with:
            continue
        seen_any = True
        lowest = lat0 + dlat * min(rows_with)
        assert lowest >= 88.0, (
            f"{d} has an unmeasured cell at {lowest:.2f} N. The pole hole cannot reach that "
            f"far south, so this is a projection or resample error putting it in the wrong place."
        )

    assert seen_any, (
        "no date carries the pole hole at all. That was true while the grid stopped at 84 N "
        "and it made the legend describe something never drawn; if it is true again, either "
        "the northern bound moved down or the sentinel stopped being written."
    )
