"""Verification of the hand-written transform chain.

The transforms in `api/_lib/geometry.py` are the highest-risk code in this
project. A wrong axis, a degrees-for-radians slip or a sign error in the ENU
rotation produces output that looks entirely plausible on a map and is wrong by
thousands of kilometres. So they are verified two ways, because the two catch
different classes of error:

  LAYER 1 (this file): cross-check against skyfield, which implements the same
  transforms independently and properly. skyfield is a TEST-ONLY dependency and
  is deliberately absent from the deployed function. Both sides use SGP4 to
  propagate, which is the point: it isolates the comparison to the transform
  chain, which is the part that was hand-written.

  LAYER 2 (manual, recorded in the README): one pass time checked by hand
  against a public tracker. That catches a systematic error the two
  implementations could share, such as a mishandled TLE epoch or a time-system
  mixup, which no amount of cross-checking against another library will reveal.

⚠️ TEST DESIGN NOTES, each of which is load-bearing:

  * Both sides are fed a `datetime`, never a shared precomputed Julian date. If
    they shared a jd, a broken jd conversion would cancel out of the comparison
    and the test would pass while the application was wrong.

  * Tolerances are deliberately LOOSE: ~2 km on position, ~0.1 deg on angles.
    The known omissions (polar motion, UT1-vs-UTC, no refraction) cost metres to
    hundreds of metres. Real bug classes cost thousands of kilometres. A loose
    tolerance still catches every error that matters, and a tight one would
    just fail on the approximations we chose on purpose.

  * There is a NEGATIVE test. Asserting that passes are found proves less than
    it appears; asserting that a satellite is *not* visible when it should not
    be is what catches a mask comparison with the wrong sign.
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from _lib import geometry, passes, satellites, seed  # noqa: E402

# Tolerances. See the docstring: loose on purpose.
POSITION_TOL_KM = 2.0
ANGLE_TOL_DEG = 0.1
ALTITUDE_TOL_KM = 2.0

# A fixed instant, so failures are reproducible rather than time-dependent.
T0 = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)
SAMPLE_TIMES = [T0 + timedelta(minutes=17 * i) for i in range(8)]


@pytest.fixture(scope="module")
def sky():
    """skyfield's timescale and satellite objects, built independently of ours."""
    from skyfield.api import EarthSatellite, load, wgs84

    ts = load.timescale(builtin=True)  # builtin: no ephemeris download, CI stays offline
    sats = {
        sat_id: EarthSatellite(l1, l2, name, ts)
        for sat_id, name, l1, l2 in seed.SEED_TLES
    }
    return {"ts": ts, "sats": sats, "wgs84": wgs84}


@pytest.fixture(scope="module")
def ours():
    return {s.id: s for s in seed.seed_satellites()}


# --------------------------------------------------------------------------
# Layer 1: the transform chain against skyfield
# --------------------------------------------------------------------------


def test_subpoint_matches_skyfield(ours, sky):
    """TEME -> ECEF -> geodetic, checked against skyfield's own subpoint.

    This is the single most important test in the repo. It exercises the GMST
    rotation and the geodetic conversion together, which is where an axis or
    unit error would live.
    """
    for sat_id, sat in ours.items():
        sky_sat = sky["sats"][sat_id]
        for when in SAMPLE_TIMES:
            mine = satellites.subpoint_at(sat, when)

            t = sky["ts"].from_datetime(when)  # a datetime, not a shared jd
            theirs = sky["wgs84"].subpoint_of(sky_sat.at(t))
            their_height = sky["wgs84"].height_of(sky_sat.at(t)).km

            dlat = mine.lat_deg - theirs.latitude.degrees
            dlon = (mine.lon_deg - theirs.longitude.degrees + 180.0) % 360.0 - 180.0
            # Convert the angular difference to ground distance so one tolerance
            # covers all latitudes; a degree of longitude is tiny near the pole.
            mean_lat = math.radians((mine.lat_deg + theirs.latitude.degrees) / 2.0)
            ground_km = math.hypot(
                dlat * 111.19, dlon * 111.19 * math.cos(mean_lat)
            )

            assert ground_km < POSITION_TOL_KM, (
                f"{sat_id} at {when}: subpoint off by {ground_km:.3f} km "
                f"(mine {mine.lat_deg:.4f},{mine.lon_deg:.4f} vs "
                f"skyfield {theirs.latitude.degrees:.4f},{theirs.longitude.degrees:.4f})"
            )
            assert abs(mine.alt_km - their_height) < ALTITUDE_TOL_KM, (
                f"{sat_id} at {when}: altitude off by "
                f"{abs(mine.alt_km - their_height):.3f} km"
            )


def test_look_angles_match_skyfield(ours, sky):
    """Azimuth, elevation and slant range against skyfield's topocentric altaz.

    Also pins the AZIMUTH CONVENTION: skyfield measures azimuth from true north
    increasing eastward, and so do we. A test that only checked elevation would
    happily pass with azimuth measured from south, which is a real convention in
    some references.
    """
    from skyfield.api import wgs84

    for site in seed.seed_sites():
        geo = site.geodetic
        site_ecef = geometry.geodetic_to_ecef(geo.lat_deg, geo.lon_deg, geo.alt_km)
        sky_site = wgs84.latlon(site.lat_deg, site.lon_deg, elevation_m=site.alt_m)

        for sat_id, sat in ours.items():
            sky_sat = sky["sats"][sat_id]
            for when in SAMPLE_TIMES:
                mine = geometry.look_angles(geo, site_ecef, satellites.ecef_at(sat, when))

                t = sky["ts"].from_datetime(when)
                alt, az, dist = (sky_sat - sky_site).at(t).altaz()

                assert abs(mine.elevation_deg - alt.degrees) < ANGLE_TOL_DEG, (
                    f"{sat_id}/{site.id} at {when}: elevation "
                    f"{mine.elevation_deg:.4f} vs {alt.degrees:.4f}"
                )
                assert abs(mine.slant_range_km - dist.km) < POSITION_TOL_KM, (
                    f"{sat_id}/{site.id} at {when}: range "
                    f"{mine.slant_range_km:.3f} vs {dist.km:.3f}"
                )
                # Azimuth is meaningless when the target is near the zenith, and
                # wraps at 360, so compare the shortest angular distance and skip
                # the degenerate case.
                if mine.elevation_deg < 89.0:
                    daz = abs((mine.azimuth_deg - az.degrees + 180.0) % 360.0 - 180.0)
                    assert daz < ANGLE_TOL_DEG, (
                        f"{sat_id}/{site.id} at {when}: azimuth "
                        f"{mine.azimuth_deg:.4f} vs {az.degrees:.4f}"
                    )


def test_geodetic_roundtrip():
    """geodetic -> ECEF -> geodetic returns the same point.

    Independent of skyfield: catches a flattening or eccentricity error in
    either direction, since a matched pair of mistakes is far less likely than a
    single one.
    """
    for lat in (-89.9, -45.0, 0.0, 12.34, 63.7467, 82.5018, 89.9):
        for lon in (-179.9, -62.3481, 0.0, 105.0597, 179.9):
            for alt in (0.0, 0.03, 780.0, 35786.0):
                x, y, z = geometry.geodetic_to_ecef(lat, lon, alt)
                back = geometry.ecef_to_geodetic(x, y, z)
                assert abs(back.lat_deg - lat) < 1e-7, (lat, lon, alt, back)
                assert abs((back.lon_deg - lon + 180) % 360 - 180) < 1e-7
                assert abs(back.alt_km - alt) < 1e-6


def test_gmst_against_skyfield(sky):
    """The GMST rotation itself, isolated from everything else."""
    for when in SAMPLE_TIMES:
        t = sky["ts"].from_datetime(when)
        jd, fr = satellites.jday_utc(when)
        mine = math.degrees(geometry.gmst_rad(jd + fr))
        theirs = t.gmst * 15.0  # skyfield reports GMST in hours
        diff = abs((mine - theirs + 180.0) % 360.0 - 180.0)
        # skyfield uses UT1; we use UTC. The gap is bounded by |DUT1| <= 0.9 s,
        # which is at most 13.5 arcsec = 0.00375 deg of Earth rotation.
        assert diff < 0.005, f"{when}: GMST {mine:.6f} vs {theirs:.6f} (diff {diff:.6f} deg)"


# --------------------------------------------------------------------------
# Pass finding
# --------------------------------------------------------------------------


def test_passes_agree_with_skyfield_events(ours, sky):
    """Our pass list against skyfield's own find_events over the same window.

    Compared on COUNT and on AOS times to within 5 seconds rather than exactly:
    both sides bisect to a different tolerance and skyfield's culmination search
    differs in detail. A systematic error would show up as minutes, not seconds.
    """
    from skyfield.api import wgs84

    site = seed.SEED_SITES[0]  # Alert
    geo = site.geodetic
    sky_site = wgs84.latlon(site.lat_deg, site.lon_deg, elevation_m=site.alt_m)
    start, end = T0, T0 + timedelta(hours=6)

    for sat_id, sat in ours.items():
        mine = passes.find_passes(
            sat, geo, site.id, start, end, mask_deg=seed.DEFAULT_MASK_DEG
        )
        t0 = sky["ts"].from_datetime(start)
        t1 = sky["ts"].from_datetime(end)
        times, kinds = sky["sats"][sat_id].find_events(
            sky_site, t0, t1, altitude_degrees=seed.DEFAULT_MASK_DEG
        )
        their_aos = [t.utc_datetime() for t, k in zip(times, kinds) if k == 0]

        # Ours clips passes to the window, so a pass already in progress at
        # `start` has no rise event on skyfield's side. Compare only passes whose
        # AOS is strictly inside the window.
        my_aos = [p.aos for p in mine if p.aos > start + timedelta(seconds=1)]

        assert len(my_aos) == len(their_aos), (
            f"{sat_id}/{site.id}: found {len(my_aos)} rises, skyfield found "
            f"{len(their_aos)}"
        )
        for a, b in zip(my_aos, their_aos):
            assert abs((a - b).total_seconds()) < 5.0, f"{sat_id}: AOS {a} vs {b}"


def test_negative_no_pass_when_below_mask(ours):
    """A satellite below the mask must not be reported as a pass.

    The positive tests would all still pass if the mask comparison had the wrong
    sign and every below-mask instant were treated as visible. This is the test
    that catches it: raise the mask to 89 degrees, where a near-overhead pass is
    still almost impossible, and assert the pass list collapses.
    """
    site = seed.SEED_SITES[0]
    sat = next(iter(ours.values()))
    found = passes.find_passes(
        sat, site.geodetic, site.id, T0, T0 + timedelta(hours=6), mask_deg=89.0
    )
    assert found == [], f"expected no passes above an 89 degree mask, got {len(found)}"


def test_graze_rescan_finds_short_passes(ours):
    """A coarse grid alone loses short passes; the rescan must recover them.

    Runs the same window at a deliberately punishing 120 s coarse step and at a
    5 s step, and asserts the coarse run does not lose passes the fine run
    finds. This is the guard on the local-maximum rescan in passes.py.
    """
    site = seed.SEED_SITES[4]  # Iqaluit: lowest latitude, so the most grazing passes
    window = (T0, T0 + timedelta(hours=12))
    for sat in ours.values():
        fine = passes.find_passes(
            sat, site.geodetic, site.id, *window, mask_deg=seed.DEFAULT_MASK_DEG,
            coarse_step_s=5.0,
        )
        coarse = passes.find_passes(
            sat, site.geodetic, site.id, *window, mask_deg=seed.DEFAULT_MASK_DEG,
            coarse_step_s=120.0,
        )
        assert len(coarse) >= len(fine), (
            f"{sat.id}: coarse step lost passes ({len(coarse)} vs {len(fine)} fine)"
        )


# --------------------------------------------------------------------------
# 🔴 The SCENARIO test. Not a geometry test.
# --------------------------------------------------------------------------


def test_every_seeded_site_sees_the_constellation():
    """Every seeded site must get at least one pass per day from the seed birds.

    🔴 THIS IS THE TEST THAT CATCHES THE MISTAKE NO OTHER TEST CAN SEE.

    Every geometry test above would pass perfectly with a 53-degree-inclination
    constellation, because the maths would be right: those satellites genuinely
    never rise at Alert. The application would be correct and the demo would be
    an empty sky. Physics verification cannot catch a scenario error, so the
    scenario gets its own assertion.
    """
    sats = seed.seed_satellites()
    start, end = TLE_START, TLE_START + timedelta(hours=24)
    for site in seed.seed_sites():
        total = 0
        for sat in sats:
            total += len(
                passes.find_passes(
                    sat, site.geodetic, site.id, start, end, mask_deg=seed.DEFAULT_MASK_DEG
                )
            )
        assert total >= 1, (
            f"{site.name} ({site.lat_deg} N) sees NO passes in 24 h from the seeded "
            f"constellation. Check the inclination: a track never goes poleward of it."
        )


def test_ground_range_sanity():
    """The number that decides whether a constellation can see the Arctic at all."""
    assert 1300 < geometry.ground_range_km(550, 15.0) < 1420
    assert 1690 < geometry.ground_range_km(780, 15.0) < 1790
    # And the consequence, stated as an assertion so it cannot rot:
    # a 53-degree track's closest approach to Alert exceeds any of those ranges.
    nearest_km = (82.5018 - 53.0) * 111.19
    assert nearest_km > geometry.ground_range_km(780, 15.0) * 1.5


TLE_START = datetime(2026, 8, 7, 0, 0, 0, tzinfo=timezone.utc)
