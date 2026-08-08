"""Keeping a public demo in a state worth looking at, without a scheduler.

THE PROBLEM THIS SOLVES, and it is a real one rather than a tidiness exercise. `last_heard`
is stored as an absolute timestamp, so the seeded world ages: the handful of assets that are
deliberately overdue becomes most of them, then all of them. Measured on the live database,
it reached 50 of 68 within a few days of seeding. A display whose whole job is answering
"what has gone quiet" is useless when the answer is "everything", and it is worse than
useless once the grey treatment lands, because a correct display of stale data is
indistinguishable from a broken one.

It also drifts in the other direction. Anyone can move, create or delete assets, which is
the point of having a command layer, and none of that should still be there for the next
person who opens the page.

🔴 WHY IT IS LAZY AND NOT A CRON JOB. This runs on serverless functions, so there is no
long-lived process to run a timer in, and a scheduled job at the platform's smallest
interval would still leave the world stale between firings. Instead the check rides on
requests that are happening anyway: any read asks "has nothing happened for a while", and
resets before answering if so. The cost is one cheap row read per read request, and the
benefit is that the world is never served stale rather than never being stale, which is the
property that actually matters.

⚠️ WHAT COUNTS AS ACTIVITY IS DELIBERATELY NARROW. A command counts. A browser polling the
map does not, because a page left open on a second monitor would otherwise hold the world
open forever and this would never fire at all.

🔑 AND A NEW VISITOR GETS THE RESET FOR FREE. If the world has been idle past the window,
the first request from anyone triggers the reset before their first frame is drawn, so
someone arriving at a quiet moment sees the intended scenario rather than the previous
visitor's leftovers. Someone arriving while another person is working does not, and must
not: resetting the world under an active user would be the worst behaviour in this file.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from . import db

# How long the world may go without a command before the next request resets it.
#
# 🔴 SET IT TO 0 TO DISABLE, AND THE BROWSER SUITE MUST. A full e2e run takes minutes and
# issues no COMMANDS, only reads, so from this module's point of view the world is idle and
# a reset fires partway through. Every `last_heard` is rewritten and every `created_at`
# moves, so motion re-anchors and positions jump under whatever test is mid-assertion.
#
# That is not a hypothetical: it flaked a radar test in a 3.5 minute suite run, which then
# passed on its own. A test that fails only in company is the most expensive kind, because
# the obvious reading is that the test is wrong.
IDLE_RESET_MINUTES = float(os.environ.get("IDLE_RESET_MINUTES", "5"))

# 🔒 A SAFETY FLOOR ON HOW OFTEN A RESET CAN HAPPEN AT ALL. Without it, a bug in the
# activity bookkeeping turns every single read into a full delete-and-reseed, which is both
# expensive and destroys anything a user is in the middle of doing. Sixty seconds is far
# below the idle window, so it never interferes in normal operation and only ever catches
# the pathological case.
MIN_SECONDS_BETWEEN_RESETS = 60.0


def _now() -> datetime:
    return datetime.now(UTC)


def _read_state(cur: Any) -> dict[str, Any] | None:
    cur.execute(
        "select last_activity, last_reset from world_state where id = 1"
    )
    return cur.fetchone()


def reset_if_idle(
    *, cur: Any = None, seed_rows: list[dict[str, Any]] | None = None
) -> bool:
    """Reset the world to seed if nothing has happened for the idle window.

    🔑 PASS A CURSOR IF YOU ALREADY HAVE ONE. The idle check is a single cheap row read, but
    opening a connection for it is not: profiling the read path showed 12.9 seconds, ALL of
    it network wait, because every request paid for two round trips to ask two questions.
    Sharing the caller's cursor makes the common answer, "no reset owed", free.

    Returns True when it actually reset, so a caller can say so rather than guess.

    ⚠️ FAILS OPEN, DELIBERATELY, and this is the opposite of the rule the secret scanners
    follow. Those refuse when they cannot tell; this one carries on. The asymmetry is on
    purpose: a scanner that cannot check might let a secret out, whereas this failing means
    the map is briefly stale. Taking the whole site down to avoid a stale map would be the
    larger harm, and the next request tries again anyway.
    """
    try:
        if cur is not None:
            due = _is_due(cur, _now())
        else:
            with db.connect() as conn, conn.cursor() as own:
                due = _is_due(own, _now())
                conn.commit()
        if not due:
            return False

        _reset_world(seed_rows=seed_rows)
        return True
    except Exception:  # noqa: BLE001 - see the docstring
        return False


def _is_due(cur: Any, now: datetime) -> bool:
    """Is a reset owed? Cheap, and safe to run on a caller's cursor."""
    state = _read_state(cur)
    if state is None:
        # First ever request. Start the clock rather than resetting a world that was
        # seeded seconds ago by hand.
        cur.execute("insert into world_state (id) values (1) on conflict do nothing")
        return False
    if IDLE_RESET_MINUTES <= 0:
        return False  # disabled: see the constant
    if now - state["last_activity"] < timedelta(minutes=IDLE_RESET_MINUTES):
        return False
    return now - state["last_reset"] >= timedelta(seconds=MIN_SECONDS_BETWEEN_RESETS)


def _reset_world(*, seed_rows: list[dict[str, Any]] | None = None) -> None:
    """Delete everything and lay the seeded world back down.

    🔑 DELETE RATHER THAN UPSERT. Re-inserting over the top would refresh the seeded assets
    and leave behind everything a visitor created, so the world would accumulate other
    people's markers forever while looking like it had been reset. The seed script upserts
    because it is run by hand against a world you want to keep; this is the other case.

    ⚠️ THE AUDIT LOG GOES TOO. It is a record of commands against entities that no longer
    exist, and the first thing the next visitor would see is a panel full of someone else's
    work. The log's no-foreign-key design means it would survive perfectly well; clearing it
    is a product decision about a shared demo, not a technical necessity, and it is the one
    part of this file worth arguing with.
    """
    from . import assets  # local import: assets is heavy and only needed on a real reset

    rows = seed_rows if seed_rows is not None else assets.seed_rows()
    # ⚠️ ONE CONNECTION FOR ALL OF IT. This was three, and each one cost a second or more of
    # network wait on a database that is not in this datacentre. It also makes the reset a
    # single transaction, so a failure halfway cannot leave an empty world behind.
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("delete from events")
        cur.execute("delete from entities")
        cur.executemany(db.UPSERT_ENTITY, [db.entity_params(r) for r in rows])
        cur.execute(
            """
            insert into world_state (id, last_activity, last_reset)
                 values (1, now(), now())
            on conflict (id) do update set last_activity = now(), last_reset = now()
            """
        )
        conn.commit()


def status() -> dict[str, Any]:
    """Where the idle clock currently stands, for the health endpoint and the README."""
    try:
        with db.connect() as conn, conn.cursor() as cur:
            state = _read_state(cur)
        if state is None:
            return {"idle_reset_minutes": IDLE_RESET_MINUTES, "state": "not initialised"}
        now = _now()
        return {
            "idle_reset_minutes": IDLE_RESET_MINUTES,
            "idle_seconds": round((now - state["last_activity"]).total_seconds()),
            "seconds_since_reset": round((now - state["last_reset"]).total_seconds()),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:120]}
