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

from datetime import UTC, datetime, timedelta
from typing import Any

from . import domain

# How long each kind may go unheard before it counts as overdue, because the kinds report
# on different rhythms. One global threshold would either call every patrol overdue or
# never notice a dead node: a mesh node beacons continuously, a Ranger patrol checks in
# when it stops moving.
#
# 🔑 EVERY ONE OF THESE IS A NUMBER OF MISSED BEACONS, NOT A GUESS. Everything reports
# every five seconds, so a threshold is really "how many reports may go missing before an
# operator should be told", and each is defensible in one sentence:
#
#   launch_site   5 min   a fixed installation on mains or a genset with its own satellite
#                         terminal. It has no reason to be quiet, and six of the eleven
#                         gateways sit on one, so silence here strands a whole cluster.
#   uas           3 min   airborne, endurance measured in tens of minutes, and the most
#                         valuable thing in the air. Losing it is worth interrupting someone.
#   aircraft      5 min   a contact moving at 400 km/h is 33 km from its last fix after five
#                         minutes, so the position stops being useful before the asset does.
#   node         10 min   a mast-mounted mesh radio, fixed and mains-independent. Long enough
#                         to ride out a squall, a reboot or a brief fade; short enough to
#                         notice a mast that has actually gone down.
#   vessel       10 min   AIS transmits every few seconds under way, so ten minutes of
#                         silence from a ship that was broadcasting is a real event.
#   hydrophone   20 min   a seabed unit relaying through a surface buoy. Sea state and ice
#                         interrupt that hop routinely and it recovers on its own.
#   patrol       30 min   dismounted troops behind terrain. A valley or a ridge takes them
#                         off the mesh for a while and that is normal, not a fault.
#   ground_party 30 min   a contact on foot, masked by the same terrain as a patrol.
#
# ⚠️ THESE USED TO BE ONE TO FOUR HOURS, which is a scale nothing here justified: it meant
# tolerating between 720 and 2,880 missed reports before saying a word. Four kinds had no
# entry at all, so `launch_site`, `aircraft` and `ground_party` could never be reported
# late however long they were silent, and a ground party untracked for 4.7 hours sat on the
# map reading nominal beside a vessel in the identical state reading overdue.
#
# ⚠️ `radar` IS ABSENT ON PURPOSE, NOT BY OMISSION. Those sites are not on the mesh at
# all, which is the interoperability problem stated as data rather than as prose. An asset
# cannot be overdue to a network it was never on, and giving them a threshold would make
# twelve sites permanently overdue and bury the four that really are.
#
# 🔒 NOTHING ON THE MAP MOVES BECAUSE OF A NUMBER IN THIS TABLE. `motion.advance` draws to
# the last report that arrived, so these decide a LABEL and a ring colour, never whether the
# display is telling the truth about where something is. That separation is deliberate: when
# freezing was keyed on this table, a generous threshold meant animating an asset nobody had
# heard from in nearly four hours.
OVERDUE_MINUTES: dict[str, int] = {
    "uas": 3,
    "launch_site": 5,
    "aircraft": 5,
    "node": 10,
    "vessel": 10,
    "hydrophone": 20,
    "patrol": 30,
    "ground_party": 30,
}

# 🔑 HOW OFTEN A WORKING ASSET SPEAKS, WHICH IS NOT THE SAME QUANTITY AS THE ONE ABOVE AND
# MUST NOT BE DERIVED FROM IT. A mesh node beacons every few seconds; it is called overdue
# after two hours because an operator tolerates a great many missed beacons before treating
# silence as a fault. One is a heartbeat, the other is a patience threshold, and they differ
# by three orders of magnitude.
#
# 🔴 CONFLATING THEM IS A BUG THIS FILE SHIPPED. The jitter used to be `threshold / 4`, in
# MINUTES, so a healthy node was stamped as last heard anywhere up to thirty minutes ago and
# a healthy hydrophone up to forty-five. Every one of them was reporting perfectly. The
# screen said otherwise, because the only number available to jitter against was the wrong
# one and nothing named the right one.
#
# ⚠️ Kinds absent here fall back to the default, deliberately: the value is a property of
# the radio rather than of the kind, and five seconds is what these carry.
REPORT_INTERVAL_SECONDS: dict[str, float] = {
    "node": 5.0,
    "hydrophone": 5.0,
    "uas": 5.0,
    "patrol": 5.0,
    "launch_site": 5.0,
    "vessel": 5.0,
    "aircraft": 5.0,
    "ground_party": 5.0,
}

DEFAULT_REPORT_INTERVAL_SECONDS = 5.0

# 🔑 HOW FAR BACK A SEEDED `last_heard` HAS TO SIT BEFORE THE ASSET COUNTS AS DELIBERATELY
# SILENT. Whether an asset was laid down working or broken is a fact about the SCENARIO, and
# this is the one number that decides it.
#
# 🔴 IT USED TO BE THE KIND'S OVERDUE THRESHOLD, AND THAT COUPLING WAS A REAL BUG. Retuning
# a threshold then silently reclassified assets as broken: dropping the hydrophone number to
# an operational twenty minutes turned every unit in the Lancaster Sound array that had been
# seeded with thirty minutes of harmless scatter into a permanently dead one, the barrier
# went dark, and every contact it was holding stopped being reported. The browser suite
# caught it as `detected unknown` reaching zero, which is three steps downstream of the edit.
#
# ⚠️ SO IT IS DELIBERATELY NOT PER KIND AND NOT TUNABLE ALONGSIDE THE DISPLAY. A threshold
# says how long an operator waits before worrying; this says what the world was set up to
# be. Tying the second to the first meant a display preference could rewrite the scenario.
# Sixty minutes sits in the wide gap between the two populations the seed actually creates:
# working assets are laid down reporting, and the handful that are meant to be silent are
# seeded hours or days back.
SEEDED_SILENT_MINUTES = 60.0

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

    # 🔑 RELATIONSHIP AND THREAT ARE DERIVED HERE, NOT STORED, for the same reason `overdue`
    # is: they are a function of what a thing IS plus what the data says about it, so a
    # stored copy is one more place for the answer to drift. `relationship` comes straight
    # off the kind declaration; `threat` is the separate axis.
    #
    # 🔑 WHY TWO FIELDS RATHER THAN ONE `owned` FLAG. Under a single boolean a NORAD radar
    # site and an unidentified vessel are both "not ours", and telling those apart is most
    # of what this display is for. One is a known fixed installation somebody else operates;
    # the other is a contact that will not say what it is.
    spec = domain.spec(row.get("kind"))
    if spec is not None:
        row["relationship"] = spec.relationship
        row["threat"] = _threat_of(row, spec)

    # Derived for the same reason as the two above: the answer exists in the sensor model
    # and reading `props` alone missed every node.
    #
    # ⚠️ LOCAL IMPORT, like `refresh` below and for the same reason: `detect` imports this
    # module, so naming it at the top would be a cycle. The line below is what the module
    # docstring means by sitting under everything that needs it.
    from . import detect  # noqa: PLC0415

    reach = detect.sensor_range_km(row)
    if reach is not None:
        row["detection_radius_km"] = reach
    return row


def _threat_of(row: dict[str, Any], spec: domain.KindSpec) -> str:
    """How this thing should be regarded, which is not the same as who operates it.

    ⚠️ `unknown` IS THE DEFAULT FOR A CONTACT AND THAT IS THE HONEST ANSWER. A contact
    nobody has judged has not been judged; calling it friendly because nothing said
    otherwise would put a judgement on the screen that no person made.
    """
    if spec.relationship != "contact":
        return "friendly"
    hostile = (row.get("props") or {}).get("hostile")
    if hostile is True:
        return "hostile"
    if hostile is False:
        return "friendly"
    return "unknown"


# 🔴 A WORLD WHOSE ASSETS NEVER REPORT AGAIN FREEZES SOLID, AND THAT WAS THE STATE OF IT.
# `last_heard` was written once at seed time and never moved, so every healthy asset aged
# past its own interval and turned overdue: a drone at one hour, a node at two, the whole
# picture by four. `motion.advance` deliberately refuses to move an overdue asset, on the
# sound principle that a display must not animate something it cannot hear. The two rules
# are individually right and together they turned the map into a photograph, with ships
# that had routes and speeds sitting motionless in the middle of Lancaster Sound.
#
# 🔑 SO A WORKING ASSET KEEPS REPORTING, DERIVED THE SAME WAY MOTION IS. Nothing is written
# back and no scheduler exists: the heartbeat is computed from the clock at the one read
# everything goes through, exactly as position already is. An asset that was healthy when
# the world was laid down stays healthy; time passing is not a fault.
#
# ⚠️ AND THE SILENT ONES MUST STAY SILENT, which is the whole difficulty. Going quiet is
# expressed in this codebase as `last_heard` moving into the past: that is what the seeded
# scenario does to make four assets overdue, and it is exactly what `inject_fault` does at
# runtime. A heartbeat that refreshed everything would erase both, and the demo would lose
# the failures it was built to show.
#
# The test that separates them: WAS THIS ASSET ALREADY BEYOND ITS INTERVAL WHEN THE VALUE
# WAS WRITTEN? A healthy asset is seeded a few minutes back, well inside its interval. A
# deliberately silent one is seeded or backdated hours before the world existed. Comparing
# `last_heard` against `created_at` rather than against now is what makes that readable
# after the fact, however long the world has been up.
def _was_healthy_at_birth(row: dict[str, Any], now: datetime | None = None) -> bool:
    """Was this asset reporting normally when the world was laid down?

    The test that separates a working asset from a deliberately silent one. Going quiet is
    expressed here as `last_heard` moving into the past: that is what the seeded scenario
    does to make four assets overdue, and exactly what `inject_fault` does at runtime. So
    the question is not how stale it is now, which time answers on its own, but whether it
    was ALREADY beyond its interval at the moment the value was written.
    """
    last = minutes_since_heard(row, now)
    if last is None:
        return False  # never reported at all: a radar site, not ours to speak for

    born = row.get("created_at")
    if isinstance(born, str):
        try:
            born = datetime.fromisoformat(born)
        except ValueError:
            return False
    if born is None:
        return False
    if born.tzinfo is None:
        born = born.replace(tzinfo=UTC)

    now = now or datetime.now(UTC)
    stale_at_birth = (born - (now - timedelta(minutes=last))).total_seconds() / 60.0
    return stale_at_birth <= SEEDED_SILENT_MINUTES


def _stamp(row: dict[str, Any], now: datetime, age_s: float = 0.0) -> None:
    """Set `last_heard` to `age_s` seconds ago, jittered by up to one reporting interval.

    ⚠️ JITTERED, NOT SET TO NOW. Seventy-six assets reporting on the same instant is a tell
    that nothing is being modelled, and every freshness readout on screen would be
    identical. The offset comes from the id, so it is stable across every read rather than
    shimmering on each poll.

    🔑 `age_s` IS HOW STALE THIS CONSOLE'S INFORMATION IS, AND IT IS USUALLY ZERO. An asset
    whose route home is intact was heard within a beacon of now. One sitting behind a relay
    that died two days ago was last heard two days ago, whatever its own radio is doing, and
    that number arrives here already computed from the link graph. The jitter is added on
    top rather than replacing it, so a stale asset does not quietly become a fresh one.

    ⚠️ THE JITTER IS SUB-INTERVAL, so it can never move an asset across its own overdue
    threshold. That was not true of the version this replaced, where the offset was drawn
    from a quarter of the threshold itself and therefore decided the answer it was supposed
    to be decorating.
    """
    interval = REPORT_INTERVAL_SECONDS.get(
        row.get("kind", ""), DEFAULT_REPORT_INTERVAL_SECONDS
    )
    offset = (hash(str(row.get("id", ""))) % 1000) / 1000.0
    stamped = now - timedelta(seconds=age_s + interval * offset)
    # ⚠️ WRITTEN IN THE SHAPE THE ROW ALREADY USES. `refresh` runs on rows that have been
    # through `_serialise`, where timestamps are ISO strings on their way to JSON, and
    # putting a datetime back in broke the entities endpoint outright. Everything that reads
    # this field parses either form, so matching what is already there is the safe direction.
    #
    # 🔴 THE SHAPE IS READ FROM `created_at`, NOT FROM `last_heard`, AND THAT IS THE WHOLE
    # POINT OF THIS LINE. Asking `isinstance(row["last_heard"], str)` cannot tell a
    # serialised row from an unserialised one when the field is None, and it is None on
    # every asset an operator places. So a placed contact that any sensor picked up got a
    # raw datetime written into a row on its way to JSON, and `/api/entities` returned 500
    # for the entire world until somebody deleted it. One placement took down the console.
    #
    # `created_at` is always present and always a timestamp, so it can answer the question
    # the field being written cannot answer about itself.
    serialised = isinstance(row.get("last_heard"), str) or isinstance(
        row.get("created_at"), str
    )
    row["last_heard"] = stamped.isoformat() if serialised else stamped


def refresh(rows: list[dict[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
    """Bring the world up to date the way a dashboard receiving live data would be.

    🔴 WITHOUT THIS THE WORLD FREEZES SOLID. `last_heard` was written once and never moved,
    so every healthy asset aged past its own interval and turned overdue: a drone at one
    hour, a node at two, the whole picture by four. `motion.advance` then refuses to move an
    overdue asset, correctly, and the map became a photograph with ships sitting motionless
    mid-passage. Time passing is not a fault, and a working asset keeps sending.

    🔑 TRACKED IS THE WORD, AND IT MEANS TWO THINGS. An asset is tracked when it is
    reporting its own position, or when a sensor that is itself still reporting is holding
    it. That is the only definition under which this display is what it claims to be: a
    dashboard showing what actually arrived over the network.

    ⛔ SO A CONTACT NOBODY IS HOLDING IS NOT REFRESHED, and that is the point rather than an
    omission. Refreshing every contact would put a current position on the screen for things
    the console cannot see, which is precisely the claim the coverage view exists to refuse.
    Those keep their last known fix and go overdue, exactly as they should.

    ⚠️ TWO PASSES, BECAUSE DETECTION DEPENDS ON SENSORS AND SENSORS DO NOT DEPEND ON
    CONTACTS. Our own kit is settled first, which says which sensors are alive; the contacts
    are then judged against those. Detection is computed on the stored positions, which is
    the honest input: it is where everything was when this tick began.

    🔑 AND OUR OWN KIT IS SETTLED THROUGH THE LINK GRAPH RATHER THAN ASSET BY ASSET. A
    working radio is not the same thing as a report arriving. A healthy unit sitting behind
    a relay that died two days ago has not been heard from for two days, and stamping it
    fresh is how this console came to report an asset as cut off from the network and heard
    from eight minutes ago in the same frame. `mesh.heard_through_mesh` is what knows the
    difference between transmitting and being heard; the three stages below only apply it.
    """
    now = now or datetime.now(UTC)
    from . import detect, mesh  # local: both import this module

    contacts = [r for r in rows if r.get("kind") in detect.CONTACT_KINDS]
    ours = [r for r in rows if r.get("kind") not in detect.CONTACT_KINDS]

    # Stage one: how long ago each of our assets last TRANSMITTED, read from the stored row
    # and from nothing else. A working radio sends continuously, so its age is zero; one
    # that has gone quiet is as old as the value already sitting on the row. Deriving this
    # from anything computed below would make the link graph an input to itself.
    transmitted: dict[str, float] = {}
    for row in ours:
        if row.get("kind") not in mesh.MESH_KINDS:
            continue
        asset_id = str(row.get("id", ""))
        if _was_healthy_at_birth(row, now):
            transmitted[asset_id] = 0.0
            continue
        quiet_for = minutes_since_heard(row, now)
        if quiet_for is not None:
            transmitted[asset_id] = quiet_for * 60.0

    # Stage two: what actually reached this console, which is the stalest hop along each
    # asset's best route to a gateway.
    heard = mesh.heard_through_mesh(rows, transmitted)

    # Stage three: apply it. An asset with no route home at all is absent from `heard`, so
    # it keeps the value it had and ages out on its own, which is the truthful reading of
    # having received nothing from it.
    for row in ours:
        if row.get("kind") in mesh.MESH_KINDS:
            age_s = heard.get(str(row.get("id", "")))
            if age_s is not None:
                _stamp(row, now, age_s)
        elif _was_healthy_at_birth(row, now):
            # Not on the mesh, so there is no path to reason about. In practice nothing
            # reaches here: a radar site answers to its own operator and a marker is a note
            # about a place, and both come back carrying no `last_heard` at all. The branch
            # exists so that anything which later acquires one keeps the old behaviour
            # rather than silently freezing.
            _stamp(row, now)
        decorate(row, now)

    # Pass two: contacts, judged by whether anything alive is holding them.
    try:
        held = detect.held_by(rows)
    except Exception:  # noqa: BLE001 - without detection nothing is refreshed, never too much
        held = {}

    for row in contacts:
        self_reporting = bool(row.get("ais_reporting")) or bool(
            (row.get("props") or {}).get("emitting")
        )
        # 🔑 `reported`, NOT MERELY DETECTED, AND THE DIFFERENCE IS THE WHOLE COVERAGE
        # STORY. A sensor can be holding a contact perfectly well and have no route home,
        # which this module already computes and the display already names as its own
        # bucket. Nothing arrived, so this dashboard has no current fix on that contact and
        # must not draw one: it keeps its last known position and ages, which is exactly
        # what "detected, not reported" is supposed to look like.
        watched = any(d.get("reported") for d in held.get(row["id"], []))
        if self_reporting or watched:
            _stamp(row, now)
        decorate(row, now)

    return rows
