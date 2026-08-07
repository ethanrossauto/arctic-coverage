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

import base64
import json
from pathlib import Path

import pytest

ICE = Path(__file__).resolve().parents[1] / "public" / "data" / "ice.json"


@pytest.fixture(scope="module")
def ice() -> dict:
    if not ICE.exists():
        pytest.skip(f"{ICE} is not built; run scripts/build_ice_history.py")
    return json.loads(ICE.read_text())


def cells(ice: dict, date: str) -> bytes:
    return base64.b64decode(ice["concentration"][date])


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


def test_the_pole_hole_is_present_and_is_not_ice_free(ice):
    """The satellite cannot see the pole, and unmeasured is not the same as open water.

    Rendering the hole as ocean would be inventing a measurement, in the one place the
    instrument is guaranteed to have none.
    """
    hole = ice["poleHoleValue"]
    assert hole > 100, "the pole hole must be distinguishable from any concentration"
    for d in ice["dates"]:
        assert hole in set(cells(ice, d)), f"{d} has no pole hole at all"
