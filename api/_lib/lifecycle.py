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

⚠️ WHAT COUNTS AS ACTIVITY: DELIBERATE ACTS, NOT TRAFFIC. A command counts, and so does a
person visibly working the display, panning, zooming, selecting an asset or toggling a
layer. The browser's own five-second poll does not, and that exclusion is the entire point:
keying activity off traffic would let one forgotten tab on a second monitor hold the world
open forever and this would never fire at all. An abandoned tab keeps polling. It does not
pan the map.

🔑 A NEW VISITOR GETS THE RESET FOR FREE. If the world has been idle past the window, the
first request from anyone triggers the reset before their first frame is drawn, so someone
arriving at a quiet moment sees the intended scenario rather than the previous visitor's
leftovers.

⚠️ AND THE HONEST LIMIT OF THAT, because the previous version of this paragraph overclaimed.
It said someone arriving while another person is working never triggers a reset, and called
doing so the worst behaviour in this file. That is only true under a definition of "working"
this module gets to choose, and it was choosing a narrow one. There is ONE world and one
clock, shared by every viewer, so a reset is always global: it lands on everyone looking, not
only on whoever's request happened to trigger it. Counting deliberate interaction is what
makes the claim nearly true instead of aspirational. The rest is handled by saying it out
loud rather than by hiding it, which is why the display names the world as shared and counts
down before it goes.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from . import db

# How long the world may go without deliberate use before the next request resets it.
#
# 🔑 THIRTY MINUTES, AND THE NUMBER IS LOAD-BEARING RATHER THAN ROUND. It was five, chosen
# so a visitor would always meet a fresh world. Five turned out to be far too short for the
# thing the window actually protects, which is not freshness at all: a visitor arriving at
# any quiet moment triggers the reset on their own first request, so they meet a fresh world
# at five minutes or at sixty. What the window really decides is how long someone can be
# reading, thinking or talking before their work is destroyed under them. Five minutes of
# that is an ordinary pause. Thirty is not.
#
# It also decides whether the audit log survives long enough to be looked at, which is the
# one requirement here that lives entirely in a table this file deletes.
#
# 🔴 SET IT TO 0 TO DISABLE, AND THE BROWSER SUITE STILL SHOULD. A full run takes minutes
# and issues no commands, so from this module's point of view the world is idle and a reset
# can fire partway through. Every `last_heard` is rewritten and every `created_at` moves, so
# motion re-anchors and positions jump under whatever test is mid-assertion. At thirty
# minutes a suite run finishes comfortably inside the window, so this is now a belt rather
# than a necessity, and it stays because a deterministic run is worth one environment
# variable.
#
# That is not a hypothetical: it flaked a radar test in a 3.5 minute suite run, which then
# passed on its own. A test that fails only in company is the most expensive kind, because
# the obvious reading is that the test is wrong.
IDLE_RESET_MINUTES = float(os.environ.get("IDLE_RESET_MINUTES", "30"))

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
        "select last_activity, last_reset, last_reset_cause from world_state where id = 1"
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

        _reset_world(seed_rows=seed_rows, cause="idle")
        return True
    except Exception:  # noqa: BLE001 - see the docstring
        return False


def mark_activity() -> None:
    """Record that a person deliberately used the display.

    🔑 SEPARATE FROM `db.log_event` ON PURPOSE, and the difference is what gets written. A
    command produces an audit row and refreshes the clock as a side effect of that row. A
    pan, a zoom or a layer toggle refreshes the clock and must produce NOTHING, because the
    audit log is a record of what happened to the world and moving a camera did not happen
    to the world. Routing these through `log_event` to save a function would fill the log
    with mouse movements and make the one artifact an evaluator inspects useless.

    ⚠️ FAILS OPEN, like everything else here. Not recording an interaction means a reset may
    arrive earlier than someone deserved; raising would take the page down to protect a
    countdown.
    """
    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into world_state (id, last_activity) values (1, now())
                on conflict (id) do update set last_activity = now()
                """
            )
            conn.commit()
    except Exception:  # noqa: BLE001 - see the docstring
        return


def reset_now(*, seed_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Reset because somebody asked, not because the clock ran out.

    Returns `{"ok": True, ...}` when it reset, or `{"ok": False, "retry_after_s": N}` when
    the floor below has not elapsed.

    🔒 IT REUSES `MIN_SECONDS_BETWEEN_RESETS` RATHER THAN INVENTING A LIMIT. This is a
    public endpoint on a public demo, so it needs a floor, and the floor that already exists
    is the right one: it was put there to stop a bug turning every read into a full reseed,
    and it answers the same question here, which is how often this world may legitimately be
    torn down. A second, different number would mean two answers to one question.

    ⚠️ IT DOES NOT FAIL OPEN, and that is the opposite of `reset_if_idle` directly above.
    That one carries on when it cannot tell, because the cost of being wrong is a briefly
    stale map. This one was asked for by a person who is watching, so a failure they never
    hear about is worse than an error they do.
    """
    with db.connect() as conn, conn.cursor() as cur:
        state = _read_state(cur)
        if state is not None:
            waited = (_now() - state["last_reset"]).total_seconds()
            if waited < MIN_SECONDS_BETWEEN_RESETS:
                return {
                    "ok": False,
                    "retry_after_s": int(round(MIN_SECONDS_BETWEEN_RESETS - waited)),
                }

    _reset_world(seed_rows=seed_rows, cause="manual")
    return {"ok": True, **status()}


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


def _reset_world(*, seed_rows: list[dict[str, Any]] | None = None, cause: str = "idle") -> None:
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

    🔑 SO THE ARGUMENT IS ANSWERED RATHER THAN WON: the reset writes one row into the empty
    log saying that it happened and why. The objection to clearing the log was never really
    about the rows, it was that a blank table cannot be told from a broken one. A log whose
    first line explains why it is short is a record; a log that is merely empty is not.
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
            insert into world_state (id, last_activity, last_reset, last_reset_cause)
                 values (1, now(), now(), %s)
            on conflict (id) do update
                    set last_activity = now(), last_reset = now(), last_reset_cause = %s
            """,
            (cause, cause),
        )
        conn.commit()

    # 🔑 THE LOG EXPLAINS ITS OWN EMPTINESS, and this row is the whole reason the deletion
    # above is defensible. An audit log that is simply blank is indistinguishable from one
    # that was never written, which is the worst thing a record can be. One row saying the
    # world was reset and why turns "there is nothing here" into "nothing has happened
    # since", and those are different claims.
    #
    # ⚠️ AFTER THE COMMIT, NOT INSIDE IT. `db.log_event` is the single writer for this table
    # and the no-foreign-key design leans on that, so this goes through it rather than
    # inlining an insert to save a round trip. The cost is that a failure here leaves a
    # freshly reset world with an empty log, which is the state it would have been in
    # anyway.
    db.log_event(
        tool="world_reset",
        source="system",
        result="ok",
        actor="system",
        params={"cause": cause, "entities": len(rows)},
        detail=(
            "the shared world was reset after the idle window elapsed"
            if cause == "idle"
            else "the shared world was reset on request"
        ),
    )


def status() -> dict[str, Any]:
    """Where the idle clock stands, in the terms the display needs to speak to a viewer.

    🔑 `generation` IS THE FIELD THAT MATTERS AND IT IS NOT A CLOCK. It changes exactly when
    the world is laid back down, so a client that remembers the last value it saw can tell
    that the world changed underneath it without diffing anything. It covers both causes
    with one mechanism: the idle window elapsing, and another viewer pressing the button
    while you are watching. The second is the case that has no timer to warn you, which is
    precisely why the notice reads off `cause` rather than assuming.

    ⚠️ `enabled` IS REPORTED RATHER THAN INFERRED. With the reset switched off for a test
    run or a recording, a display that still promised "resets after 0 min idle" would be
    telling the viewer something false, and the client cannot work that out from a countdown
    of zero.
    """
    try:
        with db.connect() as conn, conn.cursor() as cur:
            state = _read_state(cur)
        if state is None:
            return {
                "enabled": IDLE_RESET_MINUTES > 0,
                "idle_reset_minutes": IDLE_RESET_MINUTES,
                "state": "not initialised",
            }
        now = _now()
        idle_s = round((now - state["last_activity"]).total_seconds())
        remaining = None
        if IDLE_RESET_MINUTES > 0:
            remaining = max(0, round(IDLE_RESET_MINUTES * 60) - idle_s)
        return {
            "enabled": IDLE_RESET_MINUTES > 0,
            "idle_reset_minutes": IDLE_RESET_MINUTES,
            "idle_seconds": idle_s,
            "seconds_until_reset": remaining,
            "seconds_since_reset": round((now - state["last_reset"]).total_seconds()),
            "generation": state["last_reset"].isoformat(),
            "cause": state["last_reset_cause"],
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:120]}
