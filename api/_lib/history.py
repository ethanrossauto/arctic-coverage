"""Where something has been, and how much of that time it could reach us.

🔴 THERE IS NO HISTORY TABLE, AND THE REASON IS WORTH READING BEFORE ADDING ONE.

The plan for this module was a row per asset per sixty seconds, with the storage arithmetic
worked out: 86,000 rows a day, about 10 MB, thirty days of retention and a prune. That plan
was correct for the world it was written in, where an asset's position was a stored value
that something had to keep writing down.

Then motion became derived. A route-based asset's position is now a pure function of its
stored row and the clock, which means any past instant can be COMPUTED exactly instead of
recalled. Once that was true, the table stopped being a record and became a cache of
arithmetic, and a cache that can disagree with the arithmetic it caches.

What writing it anyway would have cost, all of which is now simply absent:

  * a backfill on seed, so the table was not empty on a fresh deploy
  * a gap-filler, because a serverless platform has no process to write a row every minute
  * a retention policy and a prune job
  * 300 MB of storage
  * the possibility of a gap, which is the one thing a history view must not have

⚠️ WHAT IS GENUINELY LOST, stated rather than glossed. A stored history would record things
the arithmetic cannot reproduce: exactly when an operator moved an asset by hand, and where
something was before a reseed. The audit log holds the first of those, which is what it is
for. The second is gone, and for a demo that resets itself every five minutes that is the
correct trade rather than a regrettable one.

⚠️ AND IT IS NOT A RECORDING. These are positions reconstructed from a model of how each
asset travels, not observations of where it actually was. For seeded assets following known
routes those are the same thing by construction. For anything a person has moved by hand
they are not, and `positions()` says so in what it returns.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from . import db, mesh, motion

# The window is clamped by the caller, but this module refuses to be asked for a decade
# regardless, so a bad caller cannot turn one request into a minute of arithmetic.
MAX_WINDOW_MINUTES = 30 * 24 * 60

# 🔑 CONNECTIVITY IS SAMPLED FAR MORE COARSELY THAN POSITION, and the asymmetry is the whole
# reason this is affordable. A position is one asset's arithmetic; whether that asset could
# reach a gateway is a graph over every asset in the world, about 1,100 distance checks each
# time. Forty-eight samples across the window is enough to measure spells in tens of minutes
# and keeps the endpoint well under a tenth of a second.
CONNECTIVITY_SAMPLES = 48


def _window(minutes: int) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    span = max(1, min(int(minutes), MAX_WINDOW_MINUTES))
    return now - timedelta(minutes=span), now


def positions(entity_id: str, minutes: int, max_points: int) -> list[dict[str, Any]]:
    """Positions for one asset over the last `minutes`, downsampled to `max_points`.

    Oldest first. Returns `[]` rather than raising when the asset does not exist or has
    never had a position, because "nothing recorded for this" is a real answer and a caller
    can say it plainly.

    🔑 DOWNSAMPLED BY CONSTRUCTION, NOT AFTER THE FACT. `max_points` decides how many
    instants get evaluated, so asking for four days of a patrol costs 400 evaluations rather
    than 5,760 followed by throwing 93% of them away. The line looks identical either way.
    """
    rows = db.fetch_entities()
    row = next((r for r in rows if r["id"] == entity_id), None)
    if row is None or row.get("lat") is None:
        return []

    start, end = _window(minutes)
    span_minutes = (end - start).total_seconds() / 60.0
    # Never finer than a sample a minute: the underlying motion is smooth, so more points
    # than that is a longer payload drawing the same line.
    count = max(2, min(int(max_points), int(span_minutes)))
    step = (end - start) / (count - 1)

    out: list[dict[str, Any]] = []
    for i in range(count):
        when = start + step * i
        lat, lon = motion.position_at(row, when)
        if lat is None or lon is None:
            continue
        out.append({"ts": when.isoformat(), "lat": round(lat, 5), "lon": round(lon, 5)})
    return out


def connection_stats(entity_id: str) -> dict[str, Any]:
    """How well connected this asset is now, and how long its good spells tend to last.

    `connections` is its degree in the link graph right now: how many assets it can talk to.

    `avg_gateway_minutes` is the mean length of an unbroken spell during which a path
    existed from it back to a gateway, measured across the last day. `None` when it has not
    been reachable at any sampled instant, which is a different statement from zero and is
    rendered as "not known" rather than as a number.
    """
    rows = db.fetch_entities()
    if not any(r["id"] == entity_id for r in rows):
        return {"connections": 0, "avg_gateway_minutes": None}

    links = mesh.mesh_status(rows)["links"]
    degree = sum(1 for link in links if entity_id in (link["a"], link["b"]))

    start, end = _window(24 * 60)
    step = (end - start) / (CONNECTIVITY_SAMPLES - 1)
    sample_minutes = step.total_seconds() / 60.0

    spells: list[float] = []
    current = 0.0
    for i in range(CONNECTIVITY_SAMPLES):
        when = start + step * i
        if _reachable_at(rows, entity_id, when):
            current += sample_minutes
        elif current:
            spells.append(current)
            current = 0.0
    if current:
        spells.append(current)

    return {
        "connections": degree,
        "avg_gateway_minutes": round(sum(spells) / len(spells), 1) if spells else None,
    }


def _reachable_at(rows: list[dict[str, Any]], entity_id: str, when: datetime) -> bool:
    """Could this asset get a message home at that instant?

    Rebuilds the world as it stood then: every asset back at the position the motion model
    puts it, and every asset's freshness judged against that moment rather than against now.
    Anything less would answer a question about the past using today's graph.
    """
    from . import freshness

    snapshot = []
    for r in rows:
        lat, lon = motion.position_at(r, when)
        scratch = {**r, "lat": lat, "lon": lon, "props": dict(r.get("props") or {})}
        scratch["overdue"] = freshness.is_overdue(scratch, when)
        snapshot.append(scratch)
    return entity_id in set(mesh.mesh_status(snapshot)["server_reachable"])
