"""Position history, which is computed rather than stored.

There is no history table. A route-based asset's position is a pure function of its stored
row and the clock, so any past instant is evaluated rather than recalled. That makes these
tests cheap and total: no fixture data to seed, no gaps to tolerate, and every assertion is
about arithmetic that runs the same way every time.

⚠️ THE DATABASE IS STUBBED, NOT USED. `history` reads the world through `db.fetch_entities`,
so the whole module is testable by replacing that one function. That keeps this suite
runnable with no credentials, which is what lets CI run it on a pull request from a fork.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from api._lib import db, history, motion
from api._lib.assets import seed_rows
from api._lib.mesh import haversine_km


@pytest.fixture
def world(monkeypatch) -> list[dict]:
    """The seeded world, aged so that history has somewhere to look back into.

    `created_at` is set an hour in the past because that is the instant motion measures
    from. Left at "now", every historical sample would be extrapolating backwards, which is
    supported but is not the case worth testing first.
    """
    started = datetime.now(UTC) - timedelta(hours=1)
    rows = seed_rows()
    for row in rows:
        row["created_at"] = started
        row["flag"] = "nominal"
        row["overdue"] = False
    monkeypatch.setattr(db, "fetch_entities", lambda kind=None: rows)
    return rows


# ---------------------------------------------------------------------------
# positions()
# ---------------------------------------------------------------------------

def test_a_track_comes_back_oldest_first_and_within_the_point_budget(world):
    points = history.positions("patrol-resolute", minutes=240, max_points=100)
    assert points, "a patrol on a route should have a history"
    assert len(points) <= 100
    stamps = [p["ts"] for p in points]
    assert stamps == sorted(stamps), "positions must come back oldest first"


def test_downsampling_happens_before_the_work_not_after(world):
    """`max_points` decides how many instants are EVALUATED.

    Asking for four days at 400 points must cost 400 evaluations, not 5,760 followed by
    discarding 93% of them. The observable half of that is simply that the budget is
    honoured exactly rather than approximately.
    """
    for budget in (10, 50, 400):
        points = history.positions("patrol-resolute", minutes=4 * 24 * 60, max_points=budget)
        assert len(points) == budget, f"asked for {budget} points, got {len(points)}"


def test_the_track_is_continuous_with_no_jump_across_the_end_of_a_route(world):
    """🔴 THE REGRESSION THIS FILE EXISTS FOR.

    A route that is not a closed loop used to WRAP: on reaching the last waypoint the asset
    was teleported back to the first. Live that is a jump; on a four-day history line it
    draws a stripe straight across the map. It was found by measuring, not by looking: a
    route worth 2,227 km of travel produced 5,198 km of drawn path.

    Open routes now turn round at each end instead. The checkable property is that no single
    step between consecutive samples is wildly larger than the rest.
    """
    for entity in ("patrol-resolute", "vsl-nordic-star", "vsl-unk-01"):
        points = history.positions(entity, minutes=4 * 24 * 60, max_points=200)
        hops = [
            haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
            for a, b in zip(points, points[1:], strict=False)
        ]
        assert hops, f"{entity} produced no track"
        biggest, typical = max(hops), sorted(hops)[len(hops) // 2]
        assert biggest <= max(typical * 6, 25), (
            f"{entity} jumps {biggest:.0f} km between samples against a typical {typical:.1f} km. "
            f"That is a route wrapping rather than turning round."
        )


def test_a_track_never_travels_faster_than_the_asset_can(world):
    """Speed is the one thing a reconstructed position must not get wrong.

    A patrol at 18 km/h cannot cover 400 km in ninety minutes, and it did once: motion walked
    from the head of the route rather than from where the asset actually was, so a 12.5 knot
    cargo ship moved at roughly the speed of a passenger jet.
    """
    for entity, limit_kmh in (("patrol-resolute", 18.0), ("vsl-nordic-star", 23.2)):
        points = history.positions(entity, minutes=6 * 60, max_points=100)
        for a, b in zip(points, points[1:], strict=False):
            hours = (
                datetime.fromisoformat(b["ts"]) - datetime.fromisoformat(a["ts"])
            ).total_seconds() / 3600.0
            km = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
            assert km <= limit_kmh * hours * 1.05 + 0.01, (
                f"{entity} covered {km:.1f} km in {hours * 60:.1f} min, "
                f"which is faster than {limit_kmh} km/h"
            )


def test_a_static_asset_has_a_history_that_does_not_move(world):
    """A node is bolted to a mast. Its history is a real answer, and it is a single point."""
    points = history.positions("node-barrow-01", minutes=240, max_points=50)
    assert points
    first = points[0]
    assert all(p["lat"] == first["lat"] and p["lon"] == first["lon"] for p in points)


def test_an_unknown_asset_returns_nothing_rather_than_raising(world):
    """"Nothing recorded for this" is a real answer a caller can say out loud."""
    assert history.positions("no-such-asset", minutes=240, max_points=50) == []
    assert history.connection_stats("no-such-asset") == {
        "connections": 0,
        "avg_gateway_minutes": None,
    }


def test_the_window_is_clamped_so_one_request_cannot_ask_for_a_decade(world):
    """A bad caller must not be able to turn one request into a minute of arithmetic.

    ⚠️ A second of tolerance, not an exact bound. The samples are laid down as
    `start + step * i` with `step` a float division across the whole window, so the last one
    lands about sixteen microseconds past the end over thirty days. Tightening the assertion
    to exact would be testing IEEE 754 rather than the clamp.
    """
    points = history.positions("patrol-resolute", minutes=999_999_999, max_points=50)
    span = datetime.fromisoformat(points[-1]["ts"]) - datetime.fromisoformat(points[0]["ts"])
    assert span <= timedelta(minutes=history.MAX_WINDOW_MINUTES, seconds=1)


# ---------------------------------------------------------------------------
# The freezing rule, which is where history and the mesh meet
# ---------------------------------------------------------------------------

def test_freshness_is_judged_at_the_instant_asked_about_not_at_now(world):
    """🔑 THE SUBTLE ONE, AND GETTING IT WRONG IS INVISIBLE.

    An asset that is overdue today was reporting normally last week. Asking whether it was
    being heard from has to be asked about the moment in question, or an asset that went
    quiet an hour ago comes back with a flat history stretching back for days, confidently.
    """
    row = next(r for r in world if r["id"] == "patrol-resolute")
    now = datetime.now(UTC)

    # A patrol reports every few hours; this one last reported 19 minutes ago, so it was
    # being heard from throughout the window and its history should move.
    early = motion.position_at(row, now - timedelta(hours=3))
    late = motion.position_at(row, now)
    assert haversine_km(*early, *late) > 1.0, "a fresh asset's history should not be flat"


def test_an_asset_that_has_gone_quiet_stops_where_it_last_reported(world):
    """The other half: once it is overdue, the reconstruction stops rather than guessing."""
    row = dict(next(r for r in world if r["id"] == "patrol-resolute"))
    now = datetime.now(UTC)
    # Last heard from four days ago, well past any patrol's reporting interval.
    row["last_heard"] = (now - timedelta(days=4)).isoformat()

    a = motion.position_at(row, now - timedelta(hours=2))
    b = motion.position_at(row, now)
    assert a == b, "an asset nobody can hear must not be animated onward"


# ---------------------------------------------------------------------------
# connection_stats()
# ---------------------------------------------------------------------------

def test_connections_counts_links_and_an_off_mesh_asset_has_none(world):
    on_mesh = history.connection_stats("node-barrow-01")
    assert on_mesh["connections"] > 0

    # 59.2 km from its base, well beyond the 25 km ground range.
    assert history.connection_stats("patrol-resolute")["connections"] == 0


def test_never_reachable_reports_none_rather_than_zero(world):
    """None means "not known"; zero would be a measurement nobody took.

    A ground party carries no radio, so it is not on the mesh at any instant and has never
    had a path to a gateway. Reporting "0 minutes" would imply the question was asked and
    answered.
    """
    assert history.connection_stats("gnd-unk-02")["avg_gateway_minutes"] is None


def test_a_patrol_that_returns_to_base_has_real_connected_spells(world):
    """🔑 THIS TEST WAS WRONG FIRST, AND THE WORLD WAS RIGHT.

    It asserted `patrol-resolute` is never reachable, on the grounds that it sits 59.2 km
    from its base and the ground range is 25 km. But its route is a LOOP whose first and last
    waypoint are the base itself, so it drops off the mesh on the way out and rejoins on the
    way back. Measured: spells averaging around five and a half hours.

    That is the behaviour worth pinning rather than the one I assumed. A patrol whose
    connectivity never changed across a full circuit would mean motion was not being applied
    to it at all.
    """
    stats = history.connection_stats("patrol-resolute")
    assert stats["avg_gateway_minutes"] is not None, (
        "the loop returns to base, so there must be at least one connected spell"
    )
    assert 30 <= stats["avg_gateway_minutes"] <= 20 * 60
