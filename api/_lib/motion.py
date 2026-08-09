"""How the world moves: assets follow their tracks in real time.

`db/schema.sql` has always said a route-based entity's position is "derived from its
geometry and the clock, not stored". That was false for months, because a position was
computed once at seed time and nothing ever recomputed it. This module is what makes the
comment true, and it is kept apart from `assets.py` because that file is the world and this
is what happens to it.

🔴 AN ASSET WE CANNOT HEAR DOES NOT MOVE. That is the whole design rather than an
optimisation. A console shows what it knows, and what it knows about a silent asset is
where it was when it last reported. Animating it onward would be the display inventing a
position, which is the one thing an operational picture must never do. So a grey icon is
also a stopped icon, and the two facts have one cause.

⚠️ A HUMAN ALWAYS WINS. Anything carrying `props.motion_frozen` is left exactly where it was
put. The command layer sets that flag when a person tasks something, because it is the only
layer that knows the difference between the world moving and someone asking for a move.
Dragging an asset back onto a track two seconds after an operator positioned it would be
the worst bug this file could have.

⚠️ AND IT NEVER WRITES BACK. Every position here is derived from the stored one plus
elapsed time, so it is a pure function of the row and the clock. Running it twice gives the
same answer, and no accumulated drift can build up in the database.
"""
from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime
from typing import Any

from . import terrain
from .mesh import EARTH_R_KM, haversine_km

# The kinds that are contacts rather than kit of ours. They are the world being observed,
# so their motion is ground truth and does not depend on our hearing from them. Kept beside
# the motion rules rather than imported from `detect`, which imports this module.
CONTACT_KINDS = frozenset({"vessel", "aircraft", "ground_party"})

_SPEED_FIELDS = (("speed_kmh", 1.0), ("cruise_kmh", 1.0), ("speed_kn", 1.852))


def _speed_kmh(row: dict[str, Any]) -> float | None:
    props = row.get("props") or {}
    for field_name, to_kmh in _SPEED_FIELDS:
        value = props.get(field_name)
        if value:
            return float(value) * to_kmh
    return None


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _track_points(row: dict[str, Any]) -> list[tuple[float, float]]:
    """The route as (lat, lon), or empty if this asset has no route.

    GeoJSON is lon-first and everything else in this file is lat-first, so the swap happens
    here rather than at each use.
    """
    geometry = row.get("geometry") or {}
    if geometry.get("type") != "LineString":
        return []
    coords = geometry.get("coordinates") or []
    return [(float(lat), float(lon)) for lon, lat in coords if lon is not None]


def _legs(points: list[tuple[float, float]]) -> list[float]:
    """The length of each segment of a track, in km."""

    return [
        haversine_km(points[i][0], points[i][1], points[i + 1][0], points[i + 1][1])
        for i in range(len(points) - 1)
    ]


def _point_at(points: list[tuple[float, float]], distance_km: float) -> tuple[float, float]:
    """Walk `distance_km` along the track. Never stops, and never jumps.

    🔴 A CLOSED ROUTE WRAPS; AN OPEN ONE TURNS ROUND. Both keep the asset moving, and the
    difference is what stops a drawn track from tearing.

    A patrol loop starts and finishes at the same settlement, so running off the end and
    back to the start is seamless: the two points are the same point. A vessel transit ends
    a thousand kilometres from where it began, and wrapping that teleports the ship the full
    length of the Northwest Passage in one sample. Live, it is a jump. On a four-day history
    line it draws a stripe straight across the map, which is how it was found: a route worth
    2,227 km of travel produced 5,198 km of drawn path.

    Turning round at each end is continuous, so the line stays a line. The honest limit,
    stated because someone will ask: a real ship does not sail back and forth. Its route
    here is one leg of a transit, and this world does not model where it goes afterwards.
    Stopping dead at the end would model that no better and would look like a fault.
    """
    if len(points) < 2:
        return points[0] if points else (0.0, 0.0)

    legs = _legs(points)
    total = sum(legs)
    if total <= 0:
        return points[0]

    closed = points[0] == points[-1]
    if closed:
        remaining = distance_km % total
    else:
        # Fold the distance into an out-and-back of length 2 x total, then mirror the
        # second half. The asset reaches the end and retraces its own route.
        folded = distance_km % (2 * total)
        remaining = folded if folded <= total else (2 * total) - folded
    for i, leg in enumerate(legs):
        if remaining <= leg or leg == 0:
            fraction = (remaining / leg) if leg else 0.0
            a, b = points[i], points[i + 1]
            return (
                a[0] + (b[0] - a[0]) * fraction,
                a[1] + (b[1] - a[1]) * fraction,
            )
        remaining -= leg
    return points[-1]


def _offset_along(points: list[tuple[float, float]], lat: float, lon: float) -> float:
    """How far along the track the asset's stored position sits.

    🔴 WITHOUT THIS, MOTION TELEPORTS. An asset is seeded partway down its route, not at the
    start of it, so walking `speed * elapsed` from `points[0]` moves it to a completely
    different place on the first tick. A cargo ship jumped 405 km in ninety minutes at
    12.5 knots, which is roughly the speed of a passenger jet.

    Nearest vertex rather than a true perpendicular projection, because every seeded asset
    sits exactly on a vertex, so the simple version is exact where it is used and the
    general one would be untested code carrying a rounding error nobody would ever look at.
    """

    best_i, best_d = 0, float("inf")
    for i, (plat, plon) in enumerate(points):
        d = haversine_km(lat, lon, plat, plon)
        if d < best_d:
            best_i, best_d = i, d
    return sum(_legs(points)[:best_i])


# A placed asset has no route, so it needs a speed of its own and a direction to hold.
_DRIFT_SPEED_KMH = {
    "vessel": 20.0,
    "patrol": 5.0,
    "uas": 120.0,
    "aircraft": 400.0,
    "ground_party": 4.0,
}


def _drift_heading_deg(asset_id: str) -> float:
    """A stable course for an asset that was placed rather than routed.

    🔑 DERIVED FROM THE ID, NOT DRAWN AT RANDOM AND NOT STORED. `random()` would give the
    asset a different bearing on every request, so it would jitter on the spot instead of
    going anywhere, and every client would see a different picture. Hashing the id is stable
    across every read and every process, needs no column, and nothing has to be kept in
    sync. Idea from Lane A, and it is better than the stored field I would have written.
    """
    digest = hashlib.sha256(asset_id.encode()).hexdigest()
    return float(int(digest[:8], 16) % 360)


def _project(lat: float, lon: float, heading_deg: float, distance_km: float) -> tuple[float, float]:
    """Where you end up going `distance_km` on `heading_deg` from here."""

    ang = distance_km / EARTH_R_KM
    brg = math.radians(heading_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(ang) + math.cos(p1) * math.sin(ang) * math.cos(brg))
    l2 = l1 + math.atan2(
        math.sin(brg) * math.sin(ang) * math.cos(p1),
        math.cos(ang) - math.sin(p1) * math.sin(p2),
    )
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def _drift(row: dict[str, Any], hours: float) -> tuple[float, float] | None:
    """Move a placed asset along its own heading, turning rather than running aground.

    ⚠️ THE TERRAIN CHECK IS THE POINT, not a nicety. A placed vessel drifting onto a glacier
    is a worse sight than one that never moved at all, and it is exactly the class of error
    this project has already had twice: a patrol route across open water, and node positions
    down the middle of a strait. So a heading that would put the asset in the wrong medium
    is rejected and the next one tried, and if nothing works it stays where it is.

    Honest about what it is: a straight course with obstacle avoidance by rotation, not
    navigation. It keeps a placed asset alive on the map without pretending to plan a route.
    """

    # 🔴 ONLY THINGS A PERSON PLACED. A seeded asset with no route is parked, not adrift:
    # a drone sits at its base until someone tasks it, and this drifting them away at
    # 120 km/h took two of the five off the mesh within minutes of the first reseed. The
    # instruction that created this behaviour was about PLACED assets, and the seed is not
    # one of them.
    if row.get("created_by") != "user":
        return None

    kind = row.get("kind")
    if not isinstance(kind, str):
        return None
    speed = _DRIFT_SPEED_KMH.get(kind)
    if speed is None or row.get("lat") is None:
        return None

    distance = speed * hours
    if distance <= 0:
        return None

    base = _drift_heading_deg(str(row.get("id", "")))
    for turn in range(0, 360, 30):
        lat, lon = _project(float(row["lat"]), float(row["lon"]), base + turn, distance)
        try:
            if terrain.check_placement(kind, lat, lon) is None:
                return lat, lon
        except ValueError:
            # An unclassified kind. terrain refuses to guess and so does this.
            return None
    return None


# A drone lands with this much endurance still in hand rather than flying the tank dry,
# which is what a real crew plans for and what makes the number on screen mean something.
UAS_RESERVE_MIN = 20.0

# Long enough to be a real interruption, short enough to see happen during a demo. It is a
# battery swap and a turnaround, not a full charge from flat.
UAS_RECHARGE_MIN = 45.0

UAS_CRUISE_ALT_M = 3200.0


def _uas_cycle(row: dict[str, Any], minutes_elapsed: float) -> dict[str, Any] | None:
    """Where this drone is in its fly-then-recharge cycle, or None if it never flies.

    🔑 ENDURANCE IS DERIVED, NOT DECREMENTED. The stored value is a full tank, and what is
    left right now is computed from the clock, exactly like position. Nothing has to keep
    rewriting a row as fuel burns, there is no sweeper job, and running this twice gives the
    same answer. It is the same argument the audit log makes for not storing `overdue`.

    🔑 AND THE FLEET IS DELIBERATELY OUT OF PHASE. Each drone's cycle is offset by a stable
    hash of its id, so they do not all launch and all land together. That is what makes
    "another one goes up when this one comes down" true across the fleet rather than a thing
    that has to be choreographed: at any moment some are flying and some are on the pad.

    ⚠️ A drone in maintenance never enters the cycle. It is unserviceable, and the tool that
    refuses to task it says so for the same reason.
    """
    props = row.get("props") or {}
    if props.get("state") == "maintenance":
        return None

    full = float(props.get("endurance_min_remaining") or 0.0)
    if full <= UAS_RESERVE_MIN:
        return None

    flight_min = full - UAS_RESERVE_MIN
    period = flight_min + UAS_RECHARGE_MIN
    offset = _drift_heading_deg(str(row.get("id", ""))) / 360.0 * period
    t = (minutes_elapsed + offset) % period

    if t < flight_min:
        remaining, state, airborne = full - t, "on_station", True
    else:
        charging_for = t - flight_min
        recovered = (full - UAS_RESERVE_MIN) * (charging_for / UAS_RECHARGE_MIN)
        remaining, state, airborne = UAS_RESERVE_MIN + recovered, "charging", False

    # 🔑 THE PERCENTAGE IS COMPUTED FROM THE MINUTES, not stored beside them. They are one
    # fact in two units, and the only way to guarantee an operator never sees "8% / 74 min"
    # is for the second number to be arithmetic on the first. `full` is the capacity, kept
    # so the ratio can be checked rather than taken on trust.
    return {
        "airborne": airborne,
        "state": state,
        "remaining": round(remaining, 1),
        "full": round(full, 1),
        "pct": round(remaining / full * 100),
    }


def position_at(row: dict[str, Any], when: datetime) -> tuple[float | None, float | None]:
    """Where this asset was, or will be, at `when`. Pure: it does not touch `row`.

    🔑 THIS IS WHY THERE IS NO POSITION HISTORY TABLE. Motion is a function of the stored
    row and the clock, so any past instant can be evaluated exactly rather than looked up.
    A table would be a cache of arithmetic, and one that could disagree with the arithmetic.

    ⚠️ FRESHNESS IS EVALUATED AT `when`, NOT NOW, and that is the subtle part. An asset that
    is overdue today was reporting normally last Tuesday, so asking whether it was being
    heard from has to be asked about the moment in question. Get this wrong and an asset
    that went quiet an hour ago has a flat history stretching back for days, which is
    exactly the sort of confident wrong answer this codebase keeps deleting.
    """
    from . import freshness

    scratch = {**row, "props": dict(row.get("props") or {})}
    scratch["overdue"] = freshness.is_overdue(scratch, when)
    advance([scratch], when)
    return scratch.get("lat"), scratch.get("lon")


def advance(rows: list[dict[str, Any]], now: datetime | None = None) -> None:
    """Move every asset that is travelling and that we can still hear. In place.

    ⚠️ MUST RUN AFTER `freshness.decorate`, which is what puts `overdue` on the row. Called
    before it, every asset looks fresh and every asset moves, including the ones whose
    entire story is that they stopped reporting two days ago.

    Silent on anything it cannot compute. A row with no route, no speed or no `created_at`
    is left exactly as it was rather than being moved to a guessed position.
    """
    now = now or datetime.now(UTC)
    for row in rows:
        props = row.get("props") or {}
        if props.get("motion_frozen"):
            continue
        # 🔴 FROZEN IS KEYED ON WHETHER WE ARE TRACKING IT, NOT ON WHETHER IT IS LATE.
        # An asset of ours that has gone quiet stays where it last reported, because
        # animating it onward would be the display inventing a position. That rule is right
        # and it was being applied to the wrong set: a CONTACT is not reporting to us at
        # all, so judging it by its own silence froze the very things the sensor network
        # exists to watch. Ships with routes and speeds sat motionless in Lancaster Sound.
        #
        # 🔑 A CONTACT IS THE WORLD, NOT A REPORT. It keeps moving whether or not anything
        # is holding it, which is precisely what makes the coverage view worth having: the
        # contact nobody can see is still going somewhere. The display already declares that
        # bucket as simulation ground truth rather than an observation, and hides it behind
        # a control by default, so moving it claims nothing that was not already disclosed.
        if row.get("overdue") and row.get("kind") not in CONTACT_KINDS:
            continue

        started = _as_datetime(row.get("created_at"))
        if started is None:
            continue
        # ⚠️ NOT CLAMPED AT ZERO. A negative value means "before this row existed", which
        # is exactly what a history query asks for: run the same route backwards. Clamping
        # it made every historical sample return the seed position, so a four-day track of
        # a patrol moving at 18 km/h came back 4 km long.
        hours = (now - started).total_seconds() / 3600.0

        # 🔋 A DRONE BURNS ITS BATTERY, LANDS, RECHARGES AND GOES UP AGAIN. Endurance and
        # altitude are both derived here, so the number in the info panel is the number the
        # link model is using when it decides whether that drone is a 50 km air relay or a
        # 25 km ground asset. Two displays of one fact cannot disagree if there is one fact.
        if row.get("kind") == "uas":
            phase = _uas_cycle(row, hours * 60.0)
            if phase is not None:
                props["endurance_min_remaining"] = phase["remaining"]
                props["endurance_min_full"] = phase["full"]
                props["battery_pct"] = phase["pct"]
                props["state"] = phase["state"]
                row["alt_m"] = UAS_CRUISE_ALT_M if phase["airborne"] else 0.0
                row["props"] = props
            # A drone on its pad is parked, not travelling, so it never walks a route.
            if not (row.get("alt_m") or 0) > 0:
                continue

        points = _track_points(row)
        speed = _speed_kmh(row)

        # No route: this is something a person placed rather than something the seed routed.
        # It still moves if it is the sort of thing that moves, on a heading of its own.
        if len(points) < 2:
            drifted = _drift(row, hours)
            if drifted is not None:
                row["lat"], row["lon"] = round(drifted[0], 5), round(drifted[1], 5)
            continue

        if not speed:
            continue
        # From where it actually is, not from the head of its route. The stored position is
        # always the seeded one, because `advance` edits the rows in memory and never writes
        # back, so this stays deterministic however many times it runs.
        start_km = _offset_along(points, float(row["lat"]), float(row["lon"]))
        lat, lon = _point_at(points, start_km + speed * hours)
        row["lat"] = round(lat, 5)
        row["lon"] = round(lon, 5)

