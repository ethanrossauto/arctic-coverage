"""What counts as recently heard from. The rule lives here and nowhere else.

🔑 WHY THIS IS ITS OWN MODULE, AND IT IS NOT TIDINESS. "Overdue" is one rule that four
different places need an answer from: the map draws a ring from it, the status strip
counts it, a typed query filters on it, and the mesh decides reachability with it. Every
time it has been answered locally instead of asked for, the copies drifted and the screen
contradicted itself. That has now happened twice in one afternoon, in two directions:

  * the browser held its own table of per-kind intervals while the server held another,
    so the footer count and the typed answer could disagree while both looked right;
  * the seed asserted a condition value next to a staleness that disagreed with it, which
    is how "late" ended up meaning the same thing as "broken".

It sits BELOW everything that needs it and imports nothing but the standard library, so
any module can use it without a cycle. That is the property that makes it usable: the
domain cannot import the tool layer, and the database layer cannot import either, so a
rule they all need has to live under all of them.

⚠️ THERE ARE THREE FLAGS AND ONLY TWO OF THEM ARE STORED. `nominal` and `maintenance` are
conditions of the asset and live in the `status` column. `overdue` is a fact about the
clock: true at 14:31 and false at 14:29 with nothing having changed in the world. Storing
it would mean something had to keep rewriting it, and the cost of getting that wrong is
already measured, since a world seeded to have a handful of overdue assets had fifty by
the afternoon.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# How long each kind may go unheard before it counts as overdue, because the kinds report
# on different rhythms. One global threshold would either call every patrol overdue or
# never notice a dead node: a mesh node beacons continuously, a Ranger patrol checks in
# when it stops moving.
#
# ⚠️ `radar` IS ABSENT ON PURPOSE, NOT BY OMISSION. Those sites are not on the mesh at
# all, which is the interoperability problem stated as data rather than as prose. An asset
# cannot be overdue to a network it was never on, and giving them a threshold would make
# twelve sites permanently overdue and bury the four that really are.
OVERDUE_MINUTES: dict[str, int] = {
    "node": 120,
    "hydrophone": 180,
    "uas": 60,
    "patrol": 240,
    "vessel": 60,
}

FLAGS = ("nominal", "maintenance", "overdue")


def minutes_since_heard(row: dict[str, Any], now: datetime | None = None) -> float | None:
    """How long since this asset last reported, or None if it never has.

    `now` is injectable because a test that computes its own "now" is a test that passes
    at three in the afternoon and fails at midnight.
    """
    last = row.get("last_heard")
    if not last:
        return None
    if isinstance(last, str):
        try:
            last = datetime.fromisoformat(last)
        except ValueError:
            return None
    if last.tzinfo is None:
        # The column is timestamptz, so a naive value means something upstream dropped the
        # offset. Reading it as UTC is the only interpretation that is not a guess.
        last = last.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - last).total_seconds() / 60.0


def is_overdue(row: dict[str, Any], now: datetime | None = None) -> bool:
    """Has this asset missed the reporting interval for its kind?"""
    threshold = OVERDUE_MINUTES.get(row.get("kind", ""))
    if threshold is None:
        return False
    mins = minutes_since_heard(row, now)
    return mins is not None and mins > threshold


def flag_for(row: dict[str, Any], now: datetime | None = None) -> str:
    """The one flag an asset carries: nominal, maintenance or overdue.

    ⚠️ MAINTENANCE OUTRANKS OVERDUE. An asset in the shop is quiet BECAUSE it is in the
    shop, so calling it overdue is true and useless; the fact worth showing is the one an
    operator can act on. Exactly one flag comes back, always, which is what lets a legend
    have three entries and a filter have three buttons.
    """
    if row.get("status") == "maintenance":
        return "maintenance"
    return "overdue" if is_overdue(row, now) else "nominal"


def decorate(row: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Add `overdue` and `flag` to a row, in place, and return it.

    🔑 APPLIED ONCE, AT THE READ, so every consumer sees the same answer for the same
    request. Computing it per caller is what produced the drift this module exists to
    stop, and computing it per caller with the SAME function would still let two of them
    disagree by a second across a threshold inside one response.
    """
    now = now or datetime.now(UTC)
    row["overdue"] = is_overdue(row, now)
    row["flag"] = flag_for(row, now)
    return row
