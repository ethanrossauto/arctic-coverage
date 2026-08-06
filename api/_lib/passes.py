"""Pass finding: when can a given site actually see a given satellite.

A "pass" here is a contiguous interval during which the satellite sits above the
site's elevation mask. The brief sets that mask at 15 degrees.

Method, and why it is two-part:

  1. Sample elevation on a coarse grid, and bisect any interval where
     (elevation - mask) changes sign. Bisection converges to the crossing to
     within a second in about five steps, and each step is one SGP4 call.

  2. ⚠️ Step 1 alone silently loses short passes. If a satellite's entire
     above-mask arc fits between two coarse samples, both samples read below
     the mask, there is no sign change, and the pass does not exist as far as
     the search is concerned. At a 15 degree mask, grazing passes that last
     under a minute are common rather than exotic. So any coarse sample that is
     a local maximum and lands anywhere near the mask gets a fine re-scan of
     its neighbourhood, and a pass found that way is then bisected normally.

The alternative to (2) is a coarse step small enough that no real pass can hide
in it, which means paying for many more SGP4 calls on every window request. The
local-maximum check costs almost nothing and is exact where it matters.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .geometry import Geodetic, LookAngles, geodetic_to_ecef, look_angles
from .satellites import Satellite, ecef_at

# Coarse sampling step. 30 s keeps SGP4 calls modest; the local-maximum rescan
# below is what makes that safe rather than lossy.
COARSE_STEP_S = 30.0

# A coarse local maximum within this many degrees of the mask gets a fine
# rescan. Generous on purpose: the cost is a handful of extra SGP4 calls per
# near miss, and the failure it prevents is a missing pass.
GRAZE_MARGIN_DEG = 6.0

FINE_STEP_S = 2.0
BISECT_ITERATIONS = 6  # 30 s / 2^6 -> better than 0.5 s


@dataclass
class Pass:
    """One above-mask interval for a (satellite, site) pair."""

    satellite_id: str
    site_id: str
    aos: datetime  # acquisition of signal: elevation crosses up through mask
    los: datetime  # loss of signal: elevation crosses back down
    tca: datetime  # time of closest approach, i.e. peak elevation
    max_elevation_deg: float
    duration_s: float

    def as_dict(self) -> dict:
        return {
            "satellite_id": self.satellite_id,
            "site_id": self.site_id,
            "aos": self.aos.isoformat(),
            "los": self.los.isoformat(),
            "tca": self.tca.isoformat(),
            "max_elevation_deg": round(self.max_elevation_deg, 3),
            "duration_s": round(self.duration_s, 1),
        }


def elevation_at(sat: Satellite, site: Geodetic, site_ecef, when: datetime) -> float:
    return look_angles(site, site_ecef, ecef_at(sat, when)).elevation_deg


def look_at(sat: Satellite, site: Geodetic, site_ecef, when: datetime) -> LookAngles:
    return look_angles(site, site_ecef, ecef_at(sat, when))


def _bisect_crossing(
    sat: Satellite,
    site: Geodetic,
    site_ecef,
    t_low: datetime,
    t_high: datetime,
    mask_deg: float,
) -> datetime:
    """Find the instant elevation crosses the mask, given a bracketing pair.

    Assumes (elevation - mask) has opposite signs at the two ends, which the
    caller has already established.
    """
    low_above = elevation_at(sat, site, site_ecef, t_low) >= mask_deg
    for _ in range(BISECT_ITERATIONS):
        mid = t_low + (t_high - t_low) / 2
        if (elevation_at(sat, site, site_ecef, mid) >= mask_deg) == low_above:
            t_low = mid
        else:
            t_high = mid
    return t_low + (t_high - t_low) / 2


def _refine_peak(
    sat: Satellite,
    site: Geodetic,
    site_ecef,
    t_start: datetime,
    t_end: datetime,
) -> tuple[datetime, float]:
    """Locate the elevation maximum in an interval by fine scan then bisection.

    Fine scan first because elevation is not guaranteed unimodal over a long
    interval; once the scan has bracketed the peak within two fine steps, a
    ternary narrowing gets the time to well under a second.
    """
    best_t, best_el = t_start, elevation_at(sat, site, site_ecef, t_start)
    steps = max(1, int((t_end - t_start).total_seconds() / FINE_STEP_S))
    for i in range(1, steps + 1):
        t = t_start + timedelta(seconds=i * FINE_STEP_S)
        el = elevation_at(sat, site, site_ecef, t)
        if el > best_el:
            best_t, best_el = t, el

    lo = max(t_start, best_t - timedelta(seconds=FINE_STEP_S))
    hi = min(t_end, best_t + timedelta(seconds=FINE_STEP_S))
    for _ in range(8):
        third = (hi - lo) / 3
        a, b = lo + third, hi - third
        if elevation_at(sat, site, site_ecef, a) < elevation_at(sat, site, site_ecef, b):
            lo = a
        else:
            hi = b
    t_peak = lo + (hi - lo) / 2
    return t_peak, elevation_at(sat, site, site_ecef, t_peak)


def find_passes(
    sat: Satellite,
    site: Geodetic,
    site_id: str,
    start: datetime,
    end: datetime,
    mask_deg: float = 15.0,
    coarse_step_s: float = COARSE_STEP_S,
) -> list[Pass]:
    """All above-mask intervals for one satellite over one site in [start, end].

    Returns passes clipped to the window: a pass already in progress at `start`
    reports `aos = start`, and one still running at `end` reports `los = end`.
    Clipping rather than discarding matters because the client uses these
    intervals to decide whether a link is up right now, and "the pass began
    before the window opened" must not read as "no link".
    """
    site_ecef = geodetic_to_ecef(site.lat_deg, site.lon_deg, site.alt_km)

    # One coarse sweep, reused by both the crossing search and the peak search.
    times: list[datetime] = []
    elevations: list[float] = []
    t = start
    while t <= end:
        times.append(t)
        elevations.append(elevation_at(sat, site, site_ecef, t))
        t += timedelta(seconds=coarse_step_s)
    if times[-1] < end:
        times.append(end)
        elevations.append(elevation_at(sat, site, site_ecef, end))

    # Candidate intervals: anywhere the mask is crossed, plus any near-mask
    # local maximum that a coarse grid could have stepped straight over.
    interval_starts: set[int] = set()
    for i in range(len(times) - 1):
        if (elevations[i] >= mask_deg) != (elevations[i + 1] >= mask_deg):
            interval_starts.add(i)
    for i in range(1, len(times) - 1):
        is_local_max = elevations[i] >= elevations[i - 1] and elevations[i] >= elevations[i + 1]
        near_mask = abs(elevations[i] - mask_deg) <= GRAZE_MARGIN_DEG
        if is_local_max and near_mask:
            interval_starts.add(i - 1)
            interval_starts.add(i)

    # Walk the timeline, opening a pass at an upward crossing and closing it at
    # the next downward one. Grazing passes discovered by the peak check are
    # handled by treating their refined peak as proof the interval is above the
    # mask, then bisecting outward from it.
    passes: list[Pass] = []
    open_aos: datetime | None = None
    inside = elevations[0] >= mask_deg
    if inside:
        open_aos = start

    for i in sorted(interval_starts):
        t0, t1 = times[i], times[i + 1]
        e0, e1 = elevations[i], elevations[i + 1]

        if (e0 >= mask_deg) != (e1 >= mask_deg):
            crossing = _bisect_crossing(sat, site, site_ecef, t0, t1, mask_deg)
            if e1 >= mask_deg:  # rising through the mask
                open_aos = crossing
                inside = True
            else:  # falling back below
                if open_aos is not None:
                    passes.append(
                        _build_pass(sat, site, site_ecef, sat.id, site_id, open_aos, crossing)
                    )
                open_aos = None
                inside = False
            continue

        if inside or open_aos is not None:
            continue  # already accounted for by the crossing logic

        # No crossing on the coarse grid: check whether a peak hides above the
        # mask inside this interval.
        t_peak, el_peak = _refine_peak(sat, site, site_ecef, t0, t1)
        if el_peak < mask_deg:
            continue
        aos = _bisect_crossing(sat, site, site_ecef, t0, t_peak, mask_deg)
        los = _bisect_crossing(sat, site, site_ecef, t_peak, t1, mask_deg)
        passes.append(_build_pass(sat, site, site_ecef, sat.id, site_id, aos, los))

    if open_aos is not None:  # still above the mask when the window closed
        passes.append(_build_pass(sat, site, site_ecef, sat.id, site_id, open_aos, end))

    passes.sort(key=lambda p: p.aos)
    return _dedupe(passes)


def _build_pass(sat, site, site_ecef, sat_id, site_id, aos, los) -> Pass:
    tca, max_el = _refine_peak(sat, site, site_ecef, aos, los)
    return Pass(
        satellite_id=sat_id,
        site_id=site_id,
        aos=aos,
        los=los,
        tca=tca,
        max_elevation_deg=max_el,
        duration_s=(los - aos).total_seconds(),
    )


def _dedupe(passes: list[Pass]) -> list[Pass]:
    """Drop passes that overlap an earlier one.

    The graze rescan can rediscover a pass the crossing search already found,
    because a near-mask local maximum sits inside an interval whose endpoints
    also straddle the mask. Cheaper to filter here than to make the candidate
    logic clever.
    """
    out: list[Pass] = []
    for p in passes:
        if out and p.aos < out[-1].los:
            continue
        out.append(p)
    return out


def cumulative_link_seconds(passes: list[Pass]) -> float:
    """Total time above the mask. The 'cumulative link-up time' the brief asks for."""
    return sum(p.duration_s for p in passes)
