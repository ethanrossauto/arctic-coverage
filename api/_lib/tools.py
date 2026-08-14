"""The tool registry: every action the system can take, defined once.

🔒 THE ARCHITECTURAL RULE THIS FILE EXISTS TO ENFORCE. Buttons and language drive the
SAME tools through the SAME executor. A button press and a typed command are the same
call with a different `source`, so:

  * Nothing is implemented twice, and the two paths cannot drift apart.
  * "The model can do it" and "the button can do it" are the same claim, checkable by
    reading one table rather than by testing two code paths.
  * The audit log is complete BY CONSTRUCTION, because the executor is the only way to
    reach a tool and it writes an event for every step it runs.

🔒 AND THE RULE ABOUT THE MODEL. The model never touches state. It proposes JSON, and
the executor treats that JSON as untrusted input from an unauthenticated source, because
that is exactly what it is. Validation happens here, not in the prompt: a prompt is a
request, and a validator is a guarantee.

⚠️ EVERY TOOL IS PURE-ISH AND RETURNS A ToolResult. Tools do not talk to the map, do not
format prose for the user, and do not decide what the camera does. They return facts and
`ui_effects`; the client decides how to draw them. That separation is what lets the same
tool answer a button, a typed command and a test.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from . import db, freshness, grammar, terrain
from . import mesh as meshlib

# --------------------------------------------------------------------------
# Result and error types
# --------------------------------------------------------------------------


@dataclass
class ToolResult:
    """What every tool returns.

    `message` is written for a human and ends up in the audit log and on screen, so it
    says what happened in the world rather than what the code did.

    ⚠️ `ui_effects` GO TO THE ISSUING CLIENT ONLY. Camera moves, selection and panel
    state are per-operator. Entity changes are shared and reach everyone through the
    event poll. A viewport move must never yank another operator's screen, which is the
    kind of thing that is obvious once said and invisible until someone says it.
    """

    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    ui_effects: dict[str, Any] = field(default_factory=dict)
    entity_id: str | None = None


class ToolError(Exception):
    """A tool refused to act, for a reason a person should read.

    Distinct from an unexpected exception on purpose: this is the system working
    correctly and declining, and it is logged as `rejected` rather than `error`.
    """


class Unresolved(ToolError):
    """A referent matched nothing at all.

    🔑 THE MIRROR OF `Ambiguous`, AND THE STRONGER SIGNAL OF THE TWO. Many matches means
    "I understood you and need you to narrow it", which is a question worth asking. Zero
    matches means "I did not understand you", and when the plan came from the
    deterministic tier that usually means the parser guessed rather than declining.

    So this one is not the end of the conversation: the API re-runs the original utterance
    through tier 2 rather than showing the operator a dead end. A misspelling, a nickname,
    a piece of an operator's own shorthand and a phrasing nobody anticipated all arrive
    here, and all of them are things a model is better at than a regular expression.

    ⛔ THE FIX IS ESCALATION, NEVER FUZZY MATCHING. Edit distance in `_resolve` would
    answer the misspelling and buy a whole class of confidently wrong answers, which is
    the failure this architecture exists to prevent. The model is the component that is
    allowed to be uncertain.

    A subclass for the same reason `Ambiguous` is one: every existing handler keeps
    treating it as an ordinary refusal, and only the code that cares looks closer.

    ⚠️ IT CARRIES THE QUERY AND WHAT DOES EXIST, for the same reason `Ambiguous` carries
    its candidates. The first version threw both away into a formatted sentence, and the
    escalation it exists to feed then re-sent the model the identical utterance with no
    new information, so the model passed the same dead name straight back. An escalation
    that tells the model nothing it did not already know is not a retry, it is the same
    call twice.
    """

    def __init__(self, message: str, *, query: str = "", available: list[str] | None = None) -> None:
        super().__init__(message)
        self.query = query
        self.available = available or []


class Ambiguous(ToolError):
    """The request was understood; it just named more than one thing.

    🔑 A SUBCLASS RATHER THAN A FLAG, because every existing handler already does the
    right thing with it. Anything that catches `ToolError` keeps treating this as a
    refusal and logs it as one; only the executor, which catches `Ambiguous` first,
    knows to turn it into a question. Adding a branch to a base class would have meant
    auditing every catch site instead.

    ⚠️ IT CARRIES THE CANDIDATES, WHICH IS THE ENTIRE POINT. The old code built the same
    list, formatted it into a sentence, and threw the structure away, so the one thing a
    caller needed in order to offer a choice was the one thing that did not survive.
    `message` is still a complete English sentence, so a client that ignores all of this
    is no worse off than before.
    """

    def __init__(self, query: str, candidates: list[dict[str, Any]], total: int) -> None:
        # ⚠️ NAMES ONLY. The sentence used to read "Daymark 03 (uas-daymark-03)", which put
        # an internal identifier in a question addressed to a person, and did it directly
        # above a row of buttons offering the same choice properly. The ids still travel on
        # `candidates`, which is where the client needs them and where nobody reads them.
        names = ", ".join(str(c["name"]) for c in candidates)
        more = "" if total <= len(candidates) else f" and {total - len(candidates)} more"
        super().__init__(f'"{query}" matches {total} assets: {names}{more}. Which one?')
        self.query = query
        self.candidates = candidates
        self.total = total


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------


@dataclass
class Tool:
    name: str
    summary: str
    params: dict[str, str]  # name -> short description, used to build the model's schema
    fn: Callable[..., ToolResult]
    # Whether this tool changes the world. Read-only tools are still logged, because
    # "who looked at what" is part of an audit trail, but only writers touch entities.
    writes: bool = False
    # 🔑 WHAT AN OPERATOR SAYS TO REACH THIS TOOL IS NOT DECLARED HERE. It used to be, as a
    # `says` tuple beside each tool, and it was the second copy of a sentence the parser also
    # had to know: the card said one thing, the parser accepted another, and the suite could
    # only check that they overlapped. The sentence now lives once, in `grammar.RULES`, and
    # `says()` below reads it from there. See `grammar.card_sentences`.
    # Which heading this sits under on the card. Operator intent, not implementation.
    group: str = "ask"


REGISTRY: dict[str, Tool] = {}

#: The card's headings, in the order they are shown. Ordered by how often an operator
#: reaches for them rather than alphabetically.
GROUPS: tuple[tuple[str, str], ...] = (
    ("see", "SEE"),
    ("look", "LOOK AT"),
    ("ask", "ASK"),
    ("do", "DO"),
)


def tool(
    name: str,
    summary: str,
    params: dict[str, str],
    writes: bool = False,
    group: str = "ask",
):
    def wrap(fn: Callable[..., ToolResult]) -> Callable[..., ToolResult]:
        REGISTRY[name] = Tool(
            name=name,
            summary=summary,
            params=params,
            fn=fn,
            writes=writes,
            group=group,
        )
        return fn

    return wrap


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _entities() -> list[dict[str, Any]]:
    return db.fetch_entities()


def _resolve(text: str, entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find the entities a phrase might mean.

    🔑 RETURNS A LIST, ALWAYS, AND NEVER GUESSES. Zero matches is a visible failure with
    suggestions; several matches is a clarification, not a coin toss. Both tiers share
    this one function, so a button, a typed command and the model all resolve names the
    same way; a second resolver would mean two behaviours for the same words.
    """
    needle = text.strip().lower()
    if not needle:
        return []
    exact = [e for e in entities if e["id"].lower() == needle or e["name"].lower() == needle]
    if exact:
        return exact
    return [e for e in entities if needle in e["name"].lower() or needle in e["id"].lower()]


# How many candidates a clarification offers before it stops listing them. Six fits on
# screen and reads aloud; past that the honest answer is "narrow it down", not a longer
# list nobody scans.
MAX_CANDIDATES = 6

# 🔑 EVERY KIND THE WORLD CAN HOLD IS PLACEABLE. This used to be four, which made the map
# read-only for most of what is on it: an operator could add a sensor but not the ship they
# were watching or the patrol they were sending. Someone opening this up to put assets
# where the real ones are is the thing the application most wants to encourage, and a short
# list quietly decided that half of that was not allowed.
#
# ⚠️ WHAT STOPS A NONSENSE PLACEMENT IS THE TERRAIN CHECK, NOT THIS LIST. A vessel on a
# glacier and a radar site in open water are both still refused, with a reason naming the
# medium and the distance. That is a physical answer rather than a policy one, which is why
# widening the policy costs nothing.
#
# ⚠️ THE DATABASE HOLDS THE SAME LIST IN A CHECK CONSTRAINT, and a test pins the two
# together. Two copies of one list is the shape that drifts, and the failure would be ugly:
# a kind allowed here and refused there dies inside the insert as an unhandled error rather
# than as a clean refusal, so the audit log would call it a broken tool instead of a bad
# request.
#: A cruising height for the two kinds that fly, in metres.
#:
#: 🔑 PLACED ASSETS USED TO ARRIVE AT ZERO, WHICH IS A CLAIM RATHER THAN A BLANK. An
#: aircraft at 0 m is not an aircraft with unknown altitude, it is an aircraft on the
#: ground, and the display drew it that way. These match the band the seeded fleet already
#: flies in (9100 and 3200) so a placed aircraft looks like the ones beside it.
#: ⚠️ A HYDROPHONE'S "ALTITUDE" IS NEGATIVE, which is the same error as the aircraft in the
#: other direction: a listening array sitting at 0 m is one floating on the surface. The
#: seeded arrays sit around 200 m down.
_PLACED_ALTITUDE_M: dict[str, float] = {"aircraft": 9000.0, "uas": 3000.0, "hydrophone": -200.0}


def _placed_props(kind: str) -> dict[str, Any]:
    """The movement facts a placed asset needs in order to describe itself.

    🔴 THE MOTION MODEL ALREADY KNEW THE SPEED AND THE ENTITY DID NOT SAY SO. A routeless
    asset drifts at `motion._DRIFT_SPEED_KMH[kind]`, so a placed aircraft crosses the map at
    400 km/h while its own record carried no speed field at all and the panel showed none.
    The map and the data disagreed about the same asset, which is the shape of bug this
    codebase keeps deleting elsewhere.

    🔑 SO THE NUMBER IS READ FROM THE MOTION TABLE RATHER THAN RETYPED HERE. One source, so
    changing how fast a placed vessel drifts changes what it reports about itself in the
    same edit. Retyping it would be two numbers that agree until somebody tunes one.

    🔴 AND THE TWO THAT ARE NOT COSMETIC AT ALL. A placed asset was arriving unable to do
    the job its kind exists for, which only shows up when the operator tries to use it:

      * A NODE WITH NO `payload` CARRIES NO SENSOR. `detect._sensor_for` requires one and
        correctly refuses to guess, so a node placed to cover a gap detected nothing, and
        the coverage answer for it was an honest zero about a mast that should have been
        working. It gets `eo_ir`, the shortest-ranged of the three, because a placement is
        a request for a sensor rather than a request for the best one.
      * A DRONE WITH NO `endurance_min_remaining` CANNOT BE TASKED ANYWHERE. `task_uas`
        reads it as 0 and computes a fuel radius of 0 km, so every task was refused with
        "cannot reach that station ... on 0 minutes of fuel" for a drone that had just been
        placed. It arrives fuelled, at the endurance the seeded fleet carries.

    ⚠️ NOTHING BEYOND THAT IS INVENTED. A stationary kind gets no speed because it has
    none, and nothing gets a classification, a flag of registry or a hull number: those are
    facts about a real asset that nobody supplied, and filling them with plausible values
    would be the display making things up.
    """
    from . import motion  # local: motion imports terrain, and this is called per placement

    props: dict[str, Any] = {}
    if kind == "node":
        # The payload is the only one of these that decides anything: `detect.sensor_for`
        # reads it to work out what this mast can see. A power source and a battery
        # percentage were written here too and consulted by nothing.
        props["payload"] = "eo_ir"
    if kind == "uas":
        props["flight_radius_km"] = motion.UAS_FLIGHT_RADIUS_KM
    drift_kmh = motion._DRIFT_SPEED_KMH.get(kind)
    if drift_kmh:
        # One unit stored, whatever the kind. The display decides whether to say knots.
        props["speed_kmh"] = round(drift_kmh)
    return props


PLACEABLE_KINDS: frozenset[str] = frozenset(
    {
        "node",
        "patrol",
        "uas",
        "launch_site",
        "hydrophone",
        "vessel",
        "radar",
        "marker",
        "aircraft",
        "ground_party",
    }
)


def bbox_contains(bbox: dict[str, Any], lat: float, lon: float) -> bool:
    """Point-in-box with wraparound, the server twin of `src/map/bounds.ts`.

    🔴 THIS WAS DELETED AS DEAD CODE AND HAD TO COME BACK, which is worth recording. It
    was genuinely unreachable: nothing took a bbox, so removing it was correct about the
    code and wrong about the product, because "assets in the current zoom window" is the
    first capability the application is supposed to have. Unused is not the same as
    unwanted, and the honest fix was to finish the feature rather than to delete its
    remains.

    The cases that matter, and that a naive version gets wrong: a pole-centred globe view
    legitimately spans EVERY longitude, and an oblique view can produce west > east.
    """
    if lat < bbox["south"] or lat > bbox["north"]:
        return False
    if bbox.get("global"):
        return True
    lon = ((lon + 180.0) % 360.0 + 360.0) % 360.0 - 180.0
    if bbox.get("wraps"):
        return lon >= bbox["west"] or lon <= bbox["east"]
    return bbox["west"] <= lon <= bbox["east"]


def _require_one(text: str, entities: list[dict[str, Any]]) -> dict[str, Any]:
    """One asset, or a question. Never a guess.

    Raising `Ambiguous` rather than picking the first match is the difference between a
    system that asks and one that acts on an assumption the operator never made. The
    executor turns it into a choice; nothing here knows or cares how it is drawn.
    """
    matches = _resolve(text, entities)
    if not matches:
        # 🔴 `Unresolved`, NOT A PLAIN `ToolError`, AND THE DISTINCTION IS THE WHOLE POINT.
        # Zero matches is the strongest signal there is that the utterance was not
        # understood, and when the plan came from the deterministic tier it usually means
        # the parser guessed. So the caller escalates to tier 2 instead of showing this
        # message, and it can only do that if this failure is separable from every other
        # thing a tool declines for. It is still a ToolError, so anything that does not
        # care about the difference is unchanged.
        near = ", ".join(e["name"] for e in entities[:4])
        raise Unresolved(
            f'nothing here matches "{text}". Some of what exists: {near}',
            query=text,
            # Every name, not the four in the sentence. The sentence is for a person and
            # four is plenty; the list is for the escalated model call, which needs the
            # whole set to pick the one the operator meant.
            available=[e["name"] for e in entities],
        )
    if len(matches) > 1:
        raise Ambiguous(
            query=text,
            candidates=[
                {"id": e["id"], "name": e["name"], "kind": e["kind"], "status": e["status"]}
                for e in matches[:MAX_CANDIDATES]
            ],
            total=len(matches),
        )
    return matches[0]


#: How many assets a result sentence names before it starts counting instead.
_NAMED_IN_ANSWER = 4

#: Kinds whose plural is not the word plus an s. Kept as a table rather than a rule,
#: because the list of kinds is short, closed, and known: guessing with a general
#: pluralisation rule would be more code and more ways to be wrong.
_PLURALS: dict[str, str] = {"aircraft": "aircraft", "uas": "uas", "ground party": "ground parties"}


def _plural(word: str) -> str:
    return _PLURALS.get(word, f"{word}s")


def named_list(names: list[str], limit: int = _NAMED_IN_ANSWER) -> str:
    """Names, joined the way a person would say them out loud.

    ⚠️ A LIST THAT NAMES EVERYTHING IS NOT MORE HELPFUL. Seventy-six names is a wall of
    text where a count and four examples is an answer, and the map is already showing the
    full set. Four is what fits in one spoken breath.
    """
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) <= limit:
        return f"{', '.join(names[:-1])} and {names[-1]}"
    return f"{', '.join(names[:limit])} and {len(names) - limit} more"


def _matched(count: int, names: list[str]) -> str:
    """What a listing says when it is read aloud rather than parsed.

    🔴 IT USED TO SAY "2 matching", AND THAT IS A COUNTER, NOT AN ANSWER. Asked what a
    survey was, the console replied "2 matching", which is the shape of a log line: it
    reports the size of a result set and says nothing about the Arctic. Every other message
    in this module is a sentence, and this was the one an operator sees most often.

    ⚠️ NAMING THEM IS THE POINT, not the grammar. The count alone forces the operator to go
    hunting on the map for what was counted, which is work the answer should have done.
    """
    if count == 0:
        return "nothing here matches that"
    if count == 1:
        return f"one asset matches: {names[0]}"
    return f"{count} assets match: {named_list(names)}"


# --------------------------------------------------------------------------
# Overdue
# --------------------------------------------------------------------------

# 🔑 THE RULE LIVES IN `freshness`, NOT HERE, and these names are re-exported so callers
# do not have to care which module answers. It moved out because four layers need the
# same answer and this one imports the domain, so nothing below it could ask this file
# without a cycle. Re-exported rather than aliased at each call site: `tools.is_overdue`
# is what the tests and the API already say, and a move that renames every caller is a
# move that gets half done.
OVERDUE_MINUTES = freshness.OVERDUE_MINUTES
minutes_since_heard = freshness.minutes_since_heard
is_overdue = freshness.is_overdue
flag_for = freshness.flag_for

# ⚠️ A SECOND `def is_overdue` USED TO SIT BELOW THIS AND SHADOW IT. It was a byte-for-byte
# copy of the one in `freshness`, so the behaviour never differed and nothing failed. What
# it meant was that this re-export was dead: `tools.is_overdue` resolved to the local copy,
# and the de-duplication these four lines exist to perform had not actually happened.
#
# 🔑 Worth remembering as a shape rather than an incident. Moving a function and re-exporting
# it is only finished when the original is DELETED; leaving it means two definitions that
# agree today, silently diverge on the first edit, and cannot be told apart by reading.
# Found by ruff (F811) and mypy (no-redef) independently, which is why both are in CI.


def _as_datetime(value: Any) -> datetime | None:
    """An instant back into a real datetime for a write.

    `fetch_entities` serialises timestamps to ISO strings so they can be sent to a
    browser, and a tool that reads a row and writes it back would otherwise hand a text
    value to a `timestamptz` column, which Postgres refuses rather than coerces.
    """
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return value if isinstance(value, datetime) else None


def frame_for(points: list[tuple[float, float]]) -> dict[str, Any]:
    """The camera that best shows these positions.

    🔴 THE CENTROID IS COMPUTED ON THE SPHERE, NOT BY AVERAGING LAT/LON, and this is not
    a refinement. Two assets at -179 and +179 average to longitude 0, which is the far
    side of the planet, and near the pole the error is worse still. Every camera target
    in this application is polar, so the naive version is wrong in the normal case rather
    than in an edge case.

    Convert each point to a 3D unit vector, average, renormalise, convert back. Six
    lines, correct everywhere.
    """
    if not points:
        raise ToolError("nothing to frame")

    x = y = z = 0.0
    for lat, lon in points:
        rlat, rlon = math.radians(lat), math.radians(lon)
        x += math.cos(rlat) * math.cos(rlon)
        y += math.cos(rlat) * math.sin(rlon)
        z += math.sin(rlat)
    n = len(points)
    x, y, z = x / n, y / n, z / n
    norm = math.sqrt(x * x + y * y + z * z)
    if norm < 1e-9:
        # Points spread evenly over the globe have no meaningful centre. Falling back to
        # the default polar view is honest; picking one of them would not be.
        return {"center": [-95.0, 74.0], "zoom": 2.1, "reason": "positions have no common centre"}
    lat_c = math.degrees(math.asin(z / norm))
    lon_c = math.degrees(math.atan2(y, x))

    # Angular radius: the furthest member from that centre, as a great-circle distance.
    spread_km = max(meshlib.haversine_km(lat_c, lon_c, lat, lon) for lat, lon in points)

    # Zoom from spread. Empirical rather than derived: MapLibre's globe projection does
    # not expose a clean degrees-per-pixel, so this is a ladder tuned against
    # screenshots, clamped at both ends. A single point has no scale of its own and gets
    # a fixed close zoom rather than infinite.
    zoom = 1.8  # the widest rung, used when nothing on the ladder is wide enough
    for limit, rung in ((0.0, 7.5), (15, 6.5), (60, 5.2), (200, 4.2), (600, 3.2), (1500, 2.4)):
        if spread_km <= limit:
            zoom = rung
            break

    return {
        "center": [round(lon_c, 4), round(lat_c, 4)],
        "zoom": zoom,
        "spread_km": round(spread_km, 1),
    }


# --------------------------------------------------------------------------
# Query tools
# --------------------------------------------------------------------------


@tool(
    "list_entities",
    "List or count assets, optionally filtered by name, kind, status, overdue or AIS state.",
    {
        "kind": "one asset kind, e.g. node, patrol, uas, hydrophone, vessel, radar, launch_site",
        "status": "nominal or maintenance. For assets that have gone quiet use overdue, not status",
        "not_broadcasting": "true to return only contacts holding no AIS broadcast",
        "isolated": "true to return only assets on no mesh at all",
        "overdue": "true to return only assets that have not reported inside their kind's interval",
        "bbox": "restrict to what is on screen right now, for 'in the current zoom window'",
        "ids": "restrict to these exact assets, for 'list them' after a previous answer",
        "name": "match assets whose name contains this text, for 'how many Daymark are there'",
    },
    group="see",
)
def list_entities(
    kind: str | None = None,
    status: str | None = None,
    not_broadcasting: bool | None = None,
    isolated: bool | None = None,
    overdue: bool | None = None,
    bbox: dict[str, Any] | None = None,
    ids: list[str] | None = None,
    name: str | None = None,
) -> ToolResult:
    """Assets, filtered.

    🔴 `overdue` IS A REAL FILTER NOW, AND IT WAS ADVERTISED BEFORE IT EXISTED. The
    summary line above has offered it since this registry was written, the command bar
    ships "which assets are overdue" as a suggested example, and the parser answered that
    sentence with a condition filter, which is a different question with a different
    answer.
    Overdue is about silence, status is about condition, and two assets were in one set
    and not the other. So the app's own suggestion returned a set that disagreed with the
    count in the footer beside it.
    """
    all_rows = _entities()
    rows = all_rows
    if ids is not None:
        # 🔑 THE CARRIER FOR "LIST THEM". The executor turns the `__result__` placeholder
        # into the ids the previous command answered with, and this is what receives
        # them. Applied FIRST so anything else the operator said narrows that set rather
        # than competing with it: "list them, just the drones" is the intersection.
        #
        # ⚠️ An empty list means an empty answer, and must never be read as "no filter".
        # `is not None` rather than a truth test is the whole difference: "list them"
        # after an answer that found nothing should find nothing, not everything.
        wanted = set(ids)
        rows = [r for r in rows if r["id"] in wanted]
    if name:
        # 🔴 THE FILTER THAT DID NOT EXIST, AND ITS ABSENCE PRODUCED A CONFIDENT LIE.
        # "How many Daymark are there" went to tier 2, which had no way to express a filter
        # by name, so it asked for something that matched nothing and the console answered
        # "0 matching" about a fleet of five. An unanswerable question reported as a
        # confident zero is the worst outcome available: a refusal names a limit, a zero
        # names a fact about the world, and only one of those was true.
        #
        # ⚠️ SUBSTRING AND CASE-INSENSITIVE, because a family of assets is a shared prefix
        # here: "daymark" is Daymark 01 through 05, and exact matching would answer the
        # question about the family with the answer about a unit that does not exist.
        needle = name.strip().lower()
        rows = [r for r in rows if needle in r["name"].lower()]
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    if status:
        rows = [r for r in rows if r["status"] == status]
    if not_broadcasting:
        rows = [r for r in rows if r.get("ais_reporting") is False]
    if overdue:
        now = datetime.now(UTC)
        rows = [r for r in rows if is_overdue(r, now)]
    if isolated:
        # ⚠️ THE WORLD IS ALREADY IN HAND. This re-read it, so asking for isolated assets
        # cost two full round trips to the database for one question. Every connection to
        # the pooled endpoint is hundreds of milliseconds of pure network, so a second
        # read is not a tidiness issue: it is the single most expensive thing a filter
        # here can do.
        alone = set(meshlib.mesh_status(all_rows)["isolated"])
        rows = [r for r in rows if r["id"] in alone]
    if bbox:
        # ⚠️ AN ASSET WITH NO POSITION CANNOT BE ON SCREEN. Route-only entities are
        # excluded rather than silently kept, because "what is in view" that includes
        # something with no place is not an answer anyone can check.
        rows = [
            r
            for r in rows
            if r.get("lat") is not None
            and r.get("lon") is not None
            and bbox_contains(bbox, r["lat"], r["lon"])
        ]

    return ToolResult(
        ok=True,
        message=_matched(len(rows), [r["name"] for r in rows]),
        # 🔑 POSITIONS TRAVEL WITH THE ANSWER so that framing it needs no second read of
        # the world. The executor used to re-fetch every entity purely to look up where
        # the results were, which doubled the database cost of the commonest command in
        # the application to move a camera.
        data={
            "ids": [r["id"] for r in rows],
            "names": [r["name"] for r in rows],
            "points": [
                [r["lat"], r["lon"]]
                for r in rows
                if r.get("lat") is not None and r.get("lon") is not None
            ],
        },
        # Highlighting is this tool's own effect. The camera is NOT decided here: the
        # executor frames whatever the plan returned, once, after every plan. An earlier
        # version of this comment claimed a list query left the camera alone, which stayed
        # in the file after `executor._frame_results` made it false. Handing an operator a
        # list of four assets and no idea where they are is the behaviour that changed,
        # and the comment describing it should have gone with it.
        # 🔑 ASKING FOR A KIND UNHIDES IT. "Show me the drones" must produce drones, and if
        # the operator switched that kind off ten minutes ago the honest reading of the
        # request is that they want it back, not that they want an empty highlight over a
        # layer they cannot see. Harmless when nothing is hidden: the client removes a kind
        # from a set it is not in.
        #
        # ⚠️ ONLY WHEN A KIND WAS NAMED. An unfiltered list is not a request to undo the
        # operator's whole view, and treating it as one would make "what is overdue" quietly
        # reset a filter they set on purpose.
        ui_effects={
            "highlight": [r["id"] for r in rows],
            **({"kinds": {"mode": "show", "kinds": [kind]}} if kind else {}),
        },
    )


@tool(
    "recent_activity",
    "What has happened in this world recently: assets placed or removed, faults injected or "
    "cleared, drones tasked, and anything the console refused. Use for 'what changed', "
    "'what have I done', 'show me the event log'.",
    {
        "days": "how far back to look, in days. Fractions are fine: 0.04 is about an hour",
        "target": "one asset by name or id, to see only what happened to that one",
    },
    group="ask",
)
def recent_activity(days: float = 1.0, target: str | None = None) -> ToolResult:
    """The audit log, answered as a sentence instead of pointed at.

    🔑 THE PANEL EXISTED AND NOTHING COULD ASK IT ANYTHING. Seventeen tools and not one read
    the log, so "what did I just change" had no answer and the parser resolved "show me the
    event log" to `focus_entity`, hunting for an asset by that name. A capability missing
    from the tool list reads to the model as a capability that does not exist, which is the
    same way the log came to deny its own existence once before.

    🔑 IT READS EVERYONE'S HISTORY, AND THAT IS CORRECT HERE. One database means one world,
    so what happened to it is a shared fact. **Deixis is personal, audit is communal**: "them"
    must never resolve against a stranger's command, and "what has happened" would be a lie
    if it hid theirs. Same data, opposite scoping, which is why one lives in the client's
    `context.recent` and this one does not.

    ⚠️ CHANGES AND REFUSALS ONLY, NOT EVERY READ. Forty rows of somebody listing the
    hydrophones is not what anyone means by "what has happened", and burying two real edits
    under them answers the question worse than saying nothing.

    ⚠️ AND IT NAMES A RESET WHEN ONE FALLS INSIDE THE WINDOW. The world returns to the seed
    after an idle spell, so rows either side of that belong to different worlds and reading
    them as one history is how you conclude something was deleted that was never there.
    """
    rows = db.fetch_events(limit=1000)
    if not rows:
        return ToolResult(ok=True, message="nothing has happened here yet", data={"events": []})

    window = max(0.0, float(days)) * 86400.0
    now = datetime.now(UTC)
    recent = []
    for row in rows:
        stamp = row.get("ts")
        when = datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp
        if when is None:
            continue
        if (now - when).total_seconds() <= window:
            recent.append((when, row))

    reset_at = next(
        (when for when, row in reversed(recent) if row.get("tool") == "world_reset"), None
    )
    if reset_at is not None:
        recent = [(when, row) for when, row in recent if when >= reset_at]

    if target:
        asset = _require_one(target, _entities())
        recent = [
            (when, row)
            for when, row in recent
            if row.get("entity_id") == asset["id"] or asset["name"].lower() in str(row.get("detail", "")).lower()
        ]

    changed = [
        (when, row)
        for when, row in recent
        if (row.get("tool") in REGISTRY and REGISTRY[row["tool"]].writes)
        or row.get("result") == "rejected"
    ]

    if not changed:
        scope = f" to {target}" if target else ""
        return ToolResult(
            ok=True,
            message=f"nothing has changed{scope} in that window, and nothing was refused",
            data={"events": [], "considered": len(recent)},
        )

    lines = []
    for when, row in changed[-8:]:
        mins = max(0, int((now - when).total_seconds() // 60))
        ago = "just now" if mins == 0 else f"{mins} min ago"
        verb = row.get("tool")
        detail = str(row.get("detail") or "")[:80]
        outcome = "refused: " if row.get("result") == "rejected" else ""
        lines.append(f"{ago}, {verb}: {outcome}{detail}")

    reset_note = ""
    if reset_at is not None:
        mins = max(0, int((now - reset_at).total_seconds() // 60))
        reset_note = f" The world was reset {mins} min ago; anything before that was a different world."

    return ToolResult(
        ok=True,
        message=f"{len(changed)} change(s) and refusal(s): " + "; ".join(lines) + reset_note,
        data={"events": [row for _, row in changed], "count": len(changed)},
    )


@tool(
    "mesh_status",
    "Report radio connectivity: how many links are up, which groups can reach each other, what is isolated.",
    {},
    group="ask",
)
def mesh_status() -> ToolResult:
    st = meshlib.mesh_status(_entities())
    groups = ", ".join(f'{g["label"]} ({g["size"]})' for g in st["groups"] if g["size"] > 2)
    return ToolResult(
        ok=True,
        message=(
            f'{st["counts"]["links"]} links up across {st["counts"]["groups"]} groups; '
            f'{st["counts"]["isolated"]} assets on no mesh. Main groups: {groups}'
        ),
        data=st,
    )


@tool(
    "backhaul_status",
    "How many assets carry a satellite backhaul terminal, and how many can currently reach "
    "one through the mesh.",
    {},
    group="ask",
)
def backhaul_status() -> ToolResult:
    """Answer "how many assets have a backhaul" both ways, because it has two readings.

    🔑 THE QUESTION IS GENUINELY AMBIGUOUS AND BOTH READINGS ARE USEFUL. "Have a backhaul"
    can mean carrying the terminal, which is a fact about the kit, or having a way out
    through one, which is a fact about the network and the one an operator acts on. Picking
    either silently would answer a different question from the one asked about half the
    time, and asking which they meant would be pedantry when both numbers fit in a sentence.

    ⚠️ THE SECOND FIGURE IS REACHABILITY, NOT GROUP MEMBERSHIP, and they differ in a way
    worth stating. Sitting in a group that contains a gateway is topology; reaching one
    means every hop on the path is an asset we are currently hearing from. An asset one dead
    relay away from a gateway is in the group and cannot get a message home, and the whole
    point of this display is not to call that connected.
    """
    rows = _entities()
    st = meshlib.mesh_status(rows)

    carriers = [a for a in rows if meshlib.is_gateway(a)]
    reachable = st["server_reachable"]
    # Only assets with a position are in the graph at all, so that is the honest denominator
    # rather than every row in the table.
    placed = [a for a in rows if a.get("lat") is not None and a.get("lon") is not None]

    # ⚠️ NAMED, BUT NOT ALL OF THEM. Eleven names in a transcript line is a wall nobody
    # reads, and the answer to "how many" is the number. A few names make the count
    # checkable at a glance; the rest are on the map, highlighted, which is the better
    # place to look at eleven things.
    shown = [a["name"] for a in carriers[:4]]
    rest = len(carriers) - len(shown)
    named = ", ".join(shown) + (f", and {rest} more" if rest > 0 else "") if shown else "none"

    return ToolResult(
        ok=True,
        message=(
            f"{len(carriers)} assets carry a backhaul terminal ({named}); "
            f"{len(reachable)} of {len(placed)} can currently reach one through the mesh"
        ),
        data={
            "carrying": [a["id"] for a in carriers],
            "can_reach": reachable,
            "placed": len(placed),
        },
        # The terminals themselves are what "which ones have a backhaul" points at. The
        # reachable set is a count rather than a highlight: lighting up two thirds of the
        # map says nothing an operator can use.
        ui_effects={"highlight": [a["id"] for a in carriers]},
    )


def _history_module() -> Any:
    """The position-history module, or None if this build does not record one.

    ⚠️ IMPORTED INSIDE THE FUNCTION, NOT AT MODULE SCOPE, AND THAT IS THE WHOLE REASON
    THIS HELPER EXISTS. `api/index.py` imports this file, so an ImportError up top would
    take down every route in the application, including the ones with nothing to do with
    history. An optional capability that is not present must degrade to "not available"
    and cost nothing else.
    """
    try:
        from . import history  # noqa: PLC0415 - deliberately deferred; see the docstring

        return history
    except Exception:  # noqa: BLE001 - absent, broken or half-written all mean the same here
        return None


def _connection_stats(entity_id: str) -> dict[str, Any]:
    """How much of the time this asset has actually had a route out.

    A live neighbour count says what is true this second. These say what has been true,
    which is the difference between "it is connected" and "it is reliable", and the
    second one is the question worth asking about a node sitting on an island.

    🔒 NEVER RAISES. This decorates an answer that has already been computed; a missing
    or failing history layer must not turn a working description into an error.
    """
    hist = _history_module()
    fn = getattr(hist, "connection_stats", None) if hist else None
    if fn is None:
        return {"available": False, "reason": "connection history is not recorded here"}
    try:
        stats = fn(entity_id) or {}
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"[:160]}
    return {"available": True, **stats}


@tool(
    "describe_entity",
    "Everything known about one asset, including its mesh neighbours and how reliably it has been connected.",
    {"target": "asset name or id"},
    group="ask",
)
def describe_entity(target: str) -> ToolResult:
    rows = _entities()
    asset = _require_one(target, rows)
    st = meshlib.mesh_status(rows)
    peers = [
        (link["b"] if link["a"] == asset["id"] else link["a"])
        for link in st["links"]
        if asset["id"] in (link["a"], link["b"])
    ]
    # 🔑 A CONTACT SHOULD BE ABLE TO SAY WHY WE THINK IT IS THERE. Without this, a dark
    # vessel on the map is an assertion with no provenance; with it, the answer is "a
    # camera on Barrow Strait 04 is holding it at 11 km", which is checkable.
    detect = _detect_module()
    held_fn = getattr(detect, "held_by", None) if detect else None
    holders: list[dict[str, Any]] = []
    if held_fn:
        try:
            holders = held_fn(rows).get(asset["id"], []) or []
        except Exception:  # noqa: BLE001 - provenance decorates an answer that exists
            holders = []

    stats = _connection_stats(asset["id"])
    held = stats.get("avg_gateway_minutes")
    tail = f", gateway held {held:.0f} min on average" if isinstance(held, (int, float)) else ""

    # 🔴 IT KNEW WHICH SENSORS WERE HOLDING THE CONTACT AND DID NOT SAY SO. `held_by` has
    # been in this result's data all along, and the sentence never mentioned it, so asked
    # WHICH hydrophone was holding a contact the console answered "UNKNOWN VESSEL 01: vessel,
    # nominal, 0 mesh neighbours". The information was computed, carried, and withheld from
    # the one line anybody reads.
    #
    # ⚠️ ONLY WHEN SOMETHING HOLDS IT. An empty list here is meaningful for a contact and
    # meaningless for a mesh node, and "held by nothing" on a node nobody is watching would
    # be an answer to a question that was not asked.
    held_by = ""
    if holders:
        by = named_list([str(h.get("sensor_name") or h.get("name") or h.get("id")) for h in holders])
        held_by = f", held by {by}"

    return ToolResult(
        ok=True,
        message=(
            f'{asset["name"]}: {asset["kind"]}, {asset["status"]}, '
            f"{len(peers)} mesh {_plural('neighbour') if len(peers) != 1 else 'neighbour'}"
            f"{held_by}{tail}"
        ),
        data={
            "asset": asset,
            "peers": peers,
            # Named separately from `peers` because the panel wants a number and the map
            # wants the ids, and making the panel call `.length` on a list it does not
            # otherwise use is how a display ends up owning a piece of the domain.
            "connections": len(peers),
            "connectivity": stats,
            # Absent-versus-empty matters here too: [] means nothing holds this contact,
            # which is a finding. A non-contact simply never gets the key.
            **({"held_by": holders} if holders or asset["kind"] in ("vessel", "aircraft", "ground_party") else {}),
        },
        ui_effects={"select": asset["id"], "camera": frame_for([(asset["lat"], asset["lon"])])}
        if asset["lat"] is not None
        else {"select": asset["id"]},
        entity_id=asset["id"],
    )


# A track has to be drawable. Four days at one sample a minute is 5,760 points for one
# asset, which is a slow line nobody can read; the series is downsampled to this before it
# is returned, and the response says how many samples it stands for.
MAX_TRACK_POINTS = 400


@tool(
    "entity_history",
    "Where one asset has been over a window of time.",
    {
        "target": "asset name or id",
        "days": "how far back to look, in days. Fractions are fine: 0.5 is the last twelve hours",
    },
    group="look",
)
def entity_history(target: str, days: float = 1.0) -> ToolResult:
    """One asset's recent positions, as a series.

    ⚠️ WINDOW FIRST, THEN DOWNSAMPLE, AND NOT THE OTHER WAY AROUND. Returning the raw
    series would put the cost of a four-day question on the wire and then on the renderer,
    for a line whose shape is identical at four hundred points. The count of what it was
    drawn from is returned alongside, so the thinning is visible rather than silent.
    """
    rows = _entities()
    asset = _require_one(target, rows)

    hist = _history_module()
    fn = getattr(hist, "positions", None) if hist else None
    if fn is None:
        raise ToolError(
            f'no past positions are recorded for {asset["name"]}. What this display can show '
            "is its current position, status, and who it can reach on the mesh"
        )

    window_days = max(0.0, min(float(days), 30.0))
    minutes = max(1, int(window_days * 1440))
    series = fn(asset["id"], minutes=minutes, max_points=MAX_TRACK_POINTS) or []

    points = [
        (float(p["lat"]), float(p["lon"]))
        for p in series
        if p.get("lat") is not None and p.get("lon") is not None
    ]
    if not points:
        return ToolResult(
            ok=True,
            message=f'nothing recorded for {asset["name"]} in the last {_window_words(minutes)}',
            data={"asset_id": asset["id"], "points": [], "window_minutes": minutes},
            ui_effects={"select": asset["id"]},
            entity_id=asset["id"],
        )

    return ToolResult(
        ok=True,
        message=(
            f'{asset["name"]}: {len(points)} positions over the last {_window_words(minutes)}'
        ),
        data={"asset_id": asset["id"], "points": series, "window_minutes": minutes},
        ui_effects={
            "select": asset["id"],
            # lon-first, matching GeoJSON and everything else on the wire, so the client
            # never has to remember which way round this particular payload is.
            "track": {
                "id": asset["id"],
                "coordinates": [[lon, lat] for lat, lon in points],
            },
            "camera": frame_for(points),
        },
        entity_id=asset["id"],
    )


def _window_words(minutes: int) -> str:
    if minutes < 90:
        return f"{minutes} minutes"
    if minutes < 2880:
        return f"{minutes / 60:.0f} hours"
    return f"{minutes / 1440:.0f} days"


# --------------------------------------------------------------------------
# View tools
# --------------------------------------------------------------------------


@tool(
    "focus_entity",
    "Select one asset and move the camera to it.",
    {"target": "asset name or id"},
    group="look",
)
def focus_entity(target: str) -> ToolResult:
    rows = _entities()
    asset = _require_one(target, rows)
    if asset["lat"] is None:
        raise ToolError(f'{asset["name"]} has no fixed position to focus on')
    # 🔴 THE CAMERA WENT TO SOMETHING THE OPERATOR COULD NOT SEE. Kind visibility is a
    # sticky preference: hide the drones, ask for Daymark 01 five commands later, and the
    # view flew to a blank patch of map while the console reported "focused Daymark 01". It
    # had, and that was the whole problem.
    #
    # 🔑 ONLY THIS ASSET'S KIND, NEVER EVERYTHING. Unhiding the lot would undo a filter the
    # operator set on purpose, which is the opposite error. Mode "show" adds one kind back
    # and leaves every other choice they made alone.
    return ToolResult(
        ok=True,
        message=f'focused {asset["name"]}',
        ui_effects={
            "select": asset["id"],
            "camera": frame_for([(asset["lat"], asset["lon"])]),
            "kinds": {"mode": "show", "kinds": [asset["kind"]]},
        },
        entity_id=asset["id"],
    )


# 🔴 `frame_entities` WAS HERE AND IS GONE (2026-08-14). It moved the camera to hold a set,
# which `executor._frame_results` already does to whatever ANY plan returned: "list the patrols"
# framed the patrols before this tool was reached, so FRAME and LIST were one camera move behind
# two verbs. It had good lingo and no action of its own, which is the half of the
# unique-vocabulary test that is easy to miss.
#
# ⚠️ ITS ONE UNIQUE CAPABILITY WENT WITH IT AND IS WORTH NAMING: `targets` framed an arbitrary
# LIST of assets, where `list_entities` filters. "Frame Daymark 01 and Eureka 02" is now a tier-2
# sentence. It was never sayable on tier 1 anyway, and `targets` was never in the model's schema,
# so what actually changed is that the model reaches the same camera through `list_entities`.


# The environmental layers this build actually has. Sea ice is measured satellite data,
# vendored per date; there is no weather feed, and saying so is better than implying one.
OVERLAYS: dict[str, str] = {
    "ice": "measured sea ice concentration",
}


@tool(
    "show_overlay",
    "Turn on an environmental overlay, such as measured sea ice, over the current view.",
    {"layer": "ice"},
    group="see",
)
def show_overlay(layer: str = "ice") -> ToolResult:
    """Answer "show me the overlays" without inventing a feed this build does not have.

    🔒 NOTHING HERE READS THE ICE DATA, and that is deliberate rather than incidental. The
    ice layer is measured satellite concentration rendered by the client; the server has
    no business re-deriving it, and a second copy of that answer would be a second thing
    to keep true. This tool does one honest thing: it asks the display to show a layer it
    already has.

    ⚠️ IT DOES NOT PRETEND TO BE WEATHER. A request for weather gets sea ice plus a plain
    statement of what that is, because the alternative is a command that appears to
    succeed while showing something else entirely.
    """
    key = (layer or "ice").strip().lower()
    if key not in OVERLAYS:
        raise ToolError(
            f'there is no "{key}" overlay on this display. What exists: '
            + ", ".join(f"{n} ({d})" for n, d in OVERLAYS.items())
            + ". Nothing here fetches live weather at runtime, deliberately"
        )
    return ToolResult(
        ok=True,
        message=f"showing {OVERLAYS[key]}",
        data={"layer": key},
        ui_effects={"overlay": {"layer": key, "visible": True}},
    )


#: What `set_visible_kinds` will accept, and the whole vocabulary of that control.
VIEW_MODES = ("hide", "show", "only", "all")


@tool(
    "set_visible_kinds",
    "Hide or show whole kinds of asset on the map. Use mode 'only' to show one kind and "
    "hide the rest, or mode 'all' to bring everything back.",
    {
        "mode": "one of hide, show, only, all",
        "kinds": "list of kinds to act on, such as radar or vessel. Not needed for 'all'",
    },
    group="see",
)
def set_visible_kinds(mode: str = "show", kinds: list[str] | None = None) -> ToolResult:
    """Turn whole kinds off and on, for reading a crowded map.

    🔑 A DISPLAY PREFERENCE, AND IT SAYS SO. Nothing here touches the world, the counts or
    what any other question answers: twelve radar sites do not stop existing because
    somebody is trying to read a cluster underneath them. It is the same control as the VIEW
    menu in the header, reachable by saying it instead of clicking it.

    🔒 THE SERVER DOES NOT KNOW WHAT IS CURRENTLY HIDDEN, AND DELIBERATELY DOES NOT ASK.
    Which kinds are switched off is one browser's preference, held in that browser. So this
    returns an INTENT rather than a computed set, and the client resolves it against its own
    state. The alternative, shipping the current filter up with every command so the server
    can diff it, would make a display preference part of the request contract and give two
    places an opinion about one piece of state.

    ⚠️ `only` AND `all` ARE NOT SUGAR FOR REPEATED HIDES. "Show only the vessels" has to
    mean the same thing whatever was already hidden, and a plan built out of hides would
    depend on where the operator happened to start.
    """
    key = (mode or "show").strip().lower()
    if key not in VIEW_MODES:
        raise ToolError(
            f'"{key}" is not something I can do to the view. What I can do: '
            + ", ".join(VIEW_MODES)
        )

    if key == "all":
        return ToolResult(
            ok=True,
            message="showing every kind",
            data={"mode": key},
            ui_effects={"kinds": {"mode": key, "kinds": []}},
        )

    names = [k.strip().lower().replace(" ", "_") for k in (kinds or []) if k and k.strip()]
    if not names:
        raise ToolError(
            f'"{key}" needs at least one kind of asset named, such as radar or vessel'
        )

    unknown = [n for n in names if n not in terrain.CLASSIFIED_KINDS]
    if unknown:
        raise ToolError(
            f"there is no {', '.join(unknown)} on this display. What exists: "
            + ", ".join(sorted(terrain.CLASSIFIED_KINDS))
        )

    # ⚠️ SAID AS A GROUP, BECAUSE THAT IS WHAT IS BEING SHOWN. The kinds are stored in the
    # singular ("vessel", "ground_party") since each row is one of them, and reading that
    # back as "showing only vessel" describes one boat rather than a category. The plural
    # is formed here, at the point where the word stops being a value and becomes a
    # sentence.
    spoken = ", ".join(_plural(n.replace("_", " ")) for n in names)
    message = (
        f"showing only {spoken}" if key == "only"
        else f"hiding {spoken}" if key == "hide"
        else f"showing {spoken}"
    )
    return ToolResult(
        ok=True,
        message=message,
        data={"mode": key, "kinds": names},
        ui_effects={"kinds": {"mode": key, "kinds": names}},
    )


@tool(
    "reset_view",
    "Return the camera to the default view of the whole Arctic.",
    {},
    group="look",
)
def reset_view() -> ToolResult:
    return ToolResult(
        ok=True,
        message="view reset",
        ui_effects={"camera": {"center": [-95.0, 74.0], "zoom": 2.1}, "highlight": [], "select": None},
    )


# --------------------------------------------------------------------------
# Write tools
# --------------------------------------------------------------------------


@tool(
    "place_asset",
    "Place a new asset at a position. Refuses positions the asset could not physically occupy.",
    {
        # 🔴 BUILT FROM THE CONSTANT, BECAUSE THE HAND-TYPED VERSION WENT STALE AND LIED TO
        # THE MODEL. It read "node, hydrophone, marker or launch_site" long after the set
        # grew to ten, so tier 2 was told six placeable kinds did not exist and would refuse
        # to place a vessel it was perfectly able to place. A description of a constant
        # belongs to that constant.
        "kind": f"one of: {', '.join(sorted(PLACEABLE_KINDS))}",
        "lat": "latitude in degrees",
        "lon": "longitude in degrees",
        "name": "optional display name",
        "hostile": "true to place it as an adversary rather than one of ours",
        "unknown": (
            "true to place it as an unidentified contact: classified unknown, and not "
            "announcing itself, so it is only on the map if a sensor actually holds it"
        ),
        "backhaul": (
            "true to give it its own satellite terminal, so it reaches the outside world "
            "on its own. Without one it is only reachable through the mesh, by way of a "
            "neighbour that can already get home"
        ),
    },
    writes=True,
    group="do",
)
def place_asset(
    kind: str,
    lat: float,
    lon: float,
    name: str | None = None,
    hostile: bool | None = None,
    unknown: bool | None = None,
    backhaul: bool | None = None,
) -> ToolResult:
    if kind not in PLACEABLE_KINDS:
        raise ToolError(
            f'"{kind}" is not an asset kind. Placeable kinds are '
            f'{", ".join(sorted(PLACEABLE_KINDS))}'
        )

    # 🥇 THE CHECK THAT MAKES THIS MORE THAN A FORM. A well-formed, correctly typed
    # request naming a real kind at valid coordinates can still be physically impossible,
    # and this is where that gets caught, with a reason an operator can act on.
    why_not = terrain.check_placement(kind, lat, lon)
    if why_not:
        raise ToolError(why_not)

    existing = {r["id"] for r in _entities()}
    n = 1
    while f"{kind}-user-{n:02d}" in existing:
        n += 1
    new_id = f"{kind}-user-{n:02d}"

    db.insert_entity(
        {
            "id": new_id,
            "kind": kind,
            "name": name or f"{kind.replace('_', ' ').title()} {n:02d}",
            "lat": lat,
            "lon": lon,
            # An aircraft at 0 m is not "altitude unknown", it is an aircraft on the
            # ground. See _PLACED_ALTITUDE_M.
            "alt_m": _PLACED_ALTITUDE_M.get(kind, 0.0),
            "status": "nominal",
            # ⚠️ NO ROUTE, AND THAT IS WHAT KEEPS IT WHERE IT WAS PUT. Every placeable
            # kind is a fixed installation: a node on a guyed mast, a hydrophone moored to
            # the bottom, a launch site. They have nowhere to move along and no reason to,
            # so an operator dropping one where the real thing stands finds it there
            # afterwards without anything having to defend it.
            "geometry": None,
            # 🔴 DELIBERATELY NOT FROZEN, and this is the whole point of placing a vessel
            # or a patrol rather than only a mast. A placed asset of a moving kind should
            # start moving; freezing it would leave a ship sitting on the water forever,
            # which is a stranger sight than any drift. Stationary kinds need no flag to
            # stay put: a node has no route and nothing will carry it anywhere.
            #
            # ⚠️ `task_uas` STILL WRITES `motion_frozen`, and the difference is intent. A
            # placed asset was put somewhere to exist; a tasked drone was sent somewhere
            # specific, and wandering off station is not what was asked for.
            # 🔑 AN ADVERSARY IS A FLAG, NOT A KIND. A hostile vessel is still a vessel:
            # it floats, it drifts, it is held or missed by the same sensors on the same
            # ranges. Making it its own kind would have meant teaching terrain, motion,
            # detection and the mesh about a thing that behaves identically to one they
            # already know, and every one of those would have needed the same entry.
            # 🔑 UNKNOWN IS AN IDENTITY, NOT AN ALLEGIANCE, AND IT IS KEPT SEPARATE FROM
            # `hostile` FOR THAT REASON. The seeded world happens to mark its unknown
            # contacts hostile as well, but they are two different claims: one says we
            # cannot say what this is, the other says we can and it is not ours. An
            # operator dropping a contact they cannot identify is making the first claim
            # only, and folding them together would put an assertion in their mouth.
            #
            # ⚠️ AND IT HAS TO SILENCE THE THING TOO, or the word is decorative. "Unknown"
            # is only true of something that is not announcing itself: a vessel with AIS up
            # is telling us its name, so classifying it unknown and leaving it broadcasting
            # would produce a contact the console can identify and has labelled otherwise.
            # `detected_unknown` is derived from exactly that, so this is what makes a
            # placed unknown behave like a seeded one.
            "props": {
                "placed_by": "operator",
                **_placed_props(kind),
                **({"hostile": True} if hostile else {}),
                **(
                    {"classification": "unknown", "emitting": False, "transponder": False}
                    if unknown
                    else {}
                ),
                # 🔑 A ROLE, AND THE VALUE SAYS HOW RATHER THAN JUST YES. `is_gateway` reads
                # this field's truthiness and nothing else, so any kind may carry one, and
                # "satellite" is written rather than `True` because a fibre-fed site and a
                # satellite one behave completely differently in an outage and a bare
                # boolean could never grow into that. The seeded terminals are written the
                # same way, so a placed gateway is indistinguishable from a laid-down one.
                #
                # ⚠️ WITHOUT IT, REACHABILITY IS A QUESTION ABOUT NEIGHBOURS. An asset with
                # no terminal reaches this console only through the mesh, by a chain of
                # assets we are hearing from that ends at something which does have one. So
                # dropping a node in an empty stretch of coast and finding it unreachable is
                # the model working rather than a fault.
                **({"backhaul": "satellite"} if backhaul else {}),
            },
            # 🔴 IT REPORTS THE MOMENT IT IS PUT DOWN, AND `None` HERE WAS A REAL BUG. An
            # asset with no `last_heard` has never been heard from, so freshness could not
            # place it on the timeline at all: it was excluded from the mesh sweep, never
            # stamped, and sat outside the model permanently. A node dropped next to a live
            # gateway stayed cut off forever, which is not what the operator just did.
            #
            # 🔑 NOW IT ENTERS THE WORLD REPORTING, AND THE MODEL TAKES OVER FROM THERE. A
            # placed asset that can get a message home keeps a current heartbeat; one that
            # cannot ages out of contact on its own, exactly as a seeded asset does. That is
            # what makes the BACKHAUL flag mean something you can watch happen.
            "last_heard": datetime.now(UTC),
            "ais_reporting": False if unknown else None,
            "created_by": "user",
        }
    )
    return ToolResult(
        ok=True,
        message=f'placed {new_id} at {lat:.3f}, {lon:.3f}',
        data={"id": new_id},
        ui_effects={"select": new_id, "refetch": True},
        entity_id=new_id,
    )


@tool(
    "task_uas",
    "Send a drone to a station and set its altitude. Refuses tasks beyond its fuel radius.",
    {
        "target": "which drone",
        "lat": "station latitude",
        "lon": "station longitude",
        "altitude_m": "altitude to hold, in metres",
    },
    writes=True,
    group="do",
)
def task_uas(target: str, lat: float, lon: float, altitude_m: float = 3200.0) -> ToolResult:
    """Task a drone to a station.

    🔑 THIS IS THE TOOL THE FLAGSHIP DEMO NEEDS, and it was missing from the first plan:
    the registry had place, query, filter and camera, and "put a drone up to bridge those
    two clusters" is none of those. It is a state change to an existing entity.

    ⚠️ THE FUEL CHECK IS REAL ARITHMETIC, NOT A RANGE CONSTANT. A drone has to get there
    AND get back, so the reachable radius is half the remaining endurance at cruise, less
    a reserve. That makes "which drone can reach that contact" a genuine calculation with
    a different answer per airframe, which is the whole reason endurance differs in the
    seed.
    """
    rows = _entities()
    asset = _require_one(target, rows)
    if asset["kind"] != "uas":
        raise ToolError(f'{asset["name"]} is a {asset["kind"]}, not a drone')
    if asset["status"] == "maintenance":
        raise ToolError(f'{asset["name"]} is in maintenance and cannot be tasked')

    from . import motion  # local: motion imports terrain, same reason as the placement path

    props = asset.get("props") or {}
    cruise = float(props.get("speed_kmh") or 140)

    # 🔑 ONE DECLARED RADIUS, NOT A FUEL MODEL. This used to derive the reachable distance
    # from `endurance_min_remaining`, a reserve and a cruise speed, which meant three stored
    # numbers and an arithmetic chain standing behind one answer: can it get there and back.
    # The radius is that answer, stated once. The refusal below is unchanged in what it
    # tells the operator, and it no longer quotes a fuel figure nothing else consults.
    radius_km = float(props.get("flight_radius_km") or motion.UAS_FLIGHT_RADIUS_KM)

    distance = meshlib.haversine_km(asset["lat"], asset["lon"], lat, lon)
    if distance > radius_km:
        raise ToolError(
            f'{asset["name"]} cannot reach that station: {distance:.0f} km out against a '
            f"{radius_km:.0f} km radius, and it has to come back"
        )

    eta_min = distance / cruise * 60.0
    db.insert_entity(
        {
            **{k: asset[k] for k in ("id", "kind", "name", "geometry", "ais_reporting")},
            "lat": lat,
            "lon": lon,
            "alt_m": altitude_m,
            "status": "nominal",
            "props": {
                **props,
                # 🔑 `state` AND `station` SAY MORE THAN A STOP FLAG EVER COULD: they name
                # the point the drone was sent to, so whatever simulates motion can hold it
                # there, orbit it, or fly it home. `motion_frozen` is written alongside them
                # only because the motion layer reads it today, and it is redundant with
                # these two. Removing it while it has a reader would turn a working guard
                # into a dead one, so it goes when the reader has moved.
                "motion_frozen": True,
                "station": [lat, lon],
                "eta_min": round(eta_min, 1),
            },
            # 🔴 CARRIED THROUGH, NOT CLEARED. This wrote None, so tasking a drone erased
            # its heartbeat and quietly removed it from overdue accounting for good: the
            # one action an operator takes on a drone was the action that stopped the
            # system noticing when that drone went quiet.
            #
            # Nor is it refreshed to now. `last_heard` records when the ASSET last
            # reported its own state, and an operator sending it somewhere is not the
            # asset saying anything. Writing now here would mean a drone could be tasked
            # into looking healthy, which is the failure this column exists to catch.
            "last_heard": _as_datetime(asset.get("last_heard")),
            "created_by": asset["created_by"],
        }
    )
    return ToolResult(
        ok=True,
        message=(
            f'{asset["name"]} tasked to {lat:.2f}, {lon:.2f} at {altitude_m:.0f} m; '
            f'{distance:.0f} km, about {eta_min:.0f} minutes at cruise'
        ),
        data={"eta_min": round(eta_min, 1), "distance_km": round(distance, 1)},
        ui_effects={"select": asset["id"], "refetch": True, "camera": frame_for([(lat, lon)])},
        entity_id=asset["id"],
    )


def _detect_module() -> Any:
    """The detection layer, or None if this build does not carry one.

    Deferred for the same reason the history import is: this file is imported by the
    application entry point, so an absent optional capability must cost a command rather
    than every route.
    """
    try:
        from . import detect  # noqa: PLC0415 - deliberately deferred

        return detect
    except Exception:  # noqa: BLE001
        return None


@tool(
    "coverage",
    "What the sensor network can and cannot currently see, including contacts nothing is holding.",
    {},
    group="see",
)
def coverage() -> ToolResult:
    """Answer "what are we not seeing", which is the question this network exists for.

    🥇 THE INTERESTING BUCKET IS `detected_not_reported`: something IS holding the contact
    and the report cannot get home, because the route back to a gateway is down. The ocean
    is not empty, we simply cannot hear the thing that can see it. An operator told
    "nothing out there" in that situation has been misinformed by their own display, and
    the fix is a command away rather than a mystery.

    ⚠️ IT REPORTS A GAP RATHER THAN A REASSURANCE. "Four contacts tracked" is comfortable
    and useless on its own; the number that matters is the one nobody is holding.
    """
    detect = _detect_module()
    summary = getattr(detect, "coverage_summary", None) if detect else None
    if summary is None:
        raise ToolError(
            "this display does not compute sensor coverage. What it can show: asset status, "
            "mesh connectivity, and which contacts are not broadcasting"
        )

    # Read once and kept, because the answer now names the contacts as well as counting
    # them and a second read would be a second version of the world in one sentence.
    rows = _entities()
    out = summary(rows)

    # 🔑 THE ID LISTS ARE ALREADY IN THE ANSWER, so this names the contacts rather than
    # recomputing them. A count nobody can act on is a worse answer than a short list
    # somebody can go and look at, and the two cannot disagree if only one of them exists.
    missing = list(out.get("detected_not_reported", [])) + list(out.get("untracked", []))

    # 🔴 A COUNT NOBODY CAN ACT ON IS HALF AN ANSWER. This reported four numbers and no
    # names, so "1 held by nothing at all" could not be followed by "which one" without
    # running the whole thing again, and running it again returned the identical sentence.
    # Three model calls, one answer, and it read as though the question had been ignored
    # twice.
    #
    # ⚠️ TWO NAMES THEN A COUNT, because this is one line in a transcript rather than a
    # report. The full set travels in `data` and is one command away: the ids go out with
    # the answer, so "list them" resolves against them instead of starting over.
    named = {r["id"]: r["name"] for r in rows}

    def bucket(key: str, phrase: str) -> str | None:
        ids = list(out.get(key, []))
        if not ids:
            return None
        return f"{len(ids)} {phrase} ({named_list([named.get(i, i) for i in ids], limit=2)})"

    parts = [
        p
        for p in (
            bucket("self_reporting", "reporting their own position"),
            bucket("tracked", "held by a sensor"),
            bucket("detected_not_reported", "seen by a sensor that cannot report it"),
            bucket("untracked", "held by nothing at all"),
        )
        if p
    ]
    if not parts:
        parts = ["no contacts on the picture at all"]
    if not missing:
        parts.append("nothing is unaccounted for")

    return ToolResult(
        ok=True,
        message="; ".join(parts),
        # 🔑 `ids` IS WHAT "THEM" BINDS TO, and its absence is why the follow-up questions
        # could not be answered at all. The gap is what this tool is about, so the gap is
        # what the next command points at; the per-bucket lists are still here beside it for
        # anything narrower.
        data={**out, "not_on_the_picture": missing, "ids": missing},
        ui_effects={"highlight": missing},
    )


@tool(
    "edit_asset",
    "Change one field on one asset. Only fields the schema declares editable, and only on "
    "kinds they apply to.",
    {
        "target": "asset name or id",
        "field": "which field to change, by its declared name",
        "value": "the new value, as text; numbers are parsed",
    },
    writes=True,
    group="do",
)
def edit_asset(target: str, field: str, value: str) -> ToolResult:
    """One field, one asset, checked against the declaration rather than against a list here.

    🔑 THE SCHEMA DECIDES WHAT MAY BE EDITED, NOT THIS FUNCTION. `fields.py` says where each
    value came from, and origin decides the capability: `observed` and `derived` are never
    editable, because editing an observed value falsifies the record and a derived one is
    edited through its inputs or it simply disagrees with them on the next read. Hardcoding
    a list of editable names here would be a second copy of that decision, and it would go
    stale the first time a field changed origin, which `heading_deg` did within the hour.

    ⚠️ MOUSE ONLY, AND THE REASON IS THE EDITS THEMSELVES. Most of what is editable is a
    CHOICE from a short closed list: a payload is one of the sensors that exist, a backhaul is
    on or off. That is a dropdown, and a dropdown is precisely what voice and typing are worst
    at. Saying "electro optical infrared" reliably is harder than clicking it, and a near miss
    does not fail loudly, it sets a field to a string no sensor answers to.

    There is a second reason and it is worth knowing, but it is the smaller one: a generic
    set-a-field needs a field name and a value, two more entries in `llm.STEP_PARAMS`, which
    sits at 13 against a ceiling that returns `400 Schema is too complex` at 15. Exposing this
    would have taken tier 2 down for every utterance while the whole suite stayed green,
    because the replay provider never sends a schema.
    """
    from . import fields as fieldlib

    spec = fieldlib.BY_NAME.get(field)
    if spec is None:
        known = ", ".join(sorted(f.name for f in fieldlib.FIELDS if f.editable))
        raise ToolError(f"there is no field called {field}. Editable fields are: {known}")
    if not spec.editable:
        origins = " or ".join(spec.origins)
        raise ToolError(
            f"{spec.label} is {origins}, so it is not something to set: it is what the "
            "sensors reported or what the model worked out"
        )

    rows = _entities()
    asset = _require_one(target, rows)
    if not spec.applies_to(asset["kind"]):
        raise ToolError(f'{spec.label} does not apply to a {asset["kind"]}')

    parsed: Any = value
    if spec.choices and value not in spec.choices:
        raise ToolError(
            f'{spec.label} is one of {", ".join(spec.choices)}, and "{value}" is not'
        )
    if spec.type == "number":
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ToolError(f'{spec.label} is a number, and "{value}" is not one') from exc
        if parsed < 0:
            raise ToolError(f"{spec.label} cannot be negative")
    elif spec.type == "boolean":
        parsed = str(value).strip().lower() in ("1", "true", "yes", "on")

    was = (asset.get("props") or {}).get(field)
    # ⚠️ EVERY COLUMN THE ROW ALREADY HAD, NOT JUST THE ONES BEING CHANGED. `insert_entity`
    # is an upsert over the full row, so a key left out is not "unchanged", it is missing:
    # dropping `created_by` and `last_heard` failed the write outright. Copying the asset and
    # overlaying one prop is the only shape that cannot silently lose a column.
    db.insert_entity(
        {
            **{
                k: asset.get(k)
                for k in (
                    "id", "kind", "name", "geometry", "ais_reporting",
                    "lat", "lon", "alt_m", "status", "created_by", "last_heard",
                )
            },
            "props": {**(asset.get("props") or {}), field: parsed},
        }
    )
    unit = f" {spec.unit}" if spec.unit else ""
    before = "unset" if was is None else f"{was}{unit}"
    return ToolResult(
        ok=True,
        message=f'{asset["name"]}: {spec.label} {before} to {parsed}{unit}',
        data={"id": asset["id"], "field": field, "from": was, "to": parsed},
        entity_id=asset["id"],
    )


@tool(
    "remove_asset",
    "Remove an asset from the world. Works on anything, seeded or placed by an operator.",
    {"target": "asset name or id"},
    writes=True,
    group="do",
)
def remove_asset(target: str) -> ToolResult:
    """Take one asset off the map.

    🔑 THE COUNTERPART TO PLACING, AND THE MAP IS ONLY EDITABLE IF BOTH EXIST. An operator
    who can add but not remove has a world that only ever grows, so a mistake is permanent
    and the display drifts further from what they meant with every correction.

    ⚠️ IT DOES NOT REFUSE TO REMOVE SEEDED ASSETS. The seed is a starting position rather
    than a protected fixture, and a console that lets you delete your own additions while
    guarding the scenery would be enforcing an authorship rule nobody asked for. The idle
    reset restores the world, so nothing here is unrecoverable.
    """
    rows = _entities()
    asset = _require_one(target, rows)

    removed = db.delete_entity(asset["id"])
    if not removed:
        raise ToolError(f'{asset["name"]} was already gone by the time the removal ran')

    return ToolResult(
        ok=True,
        message=f'removed {asset["name"]} ({asset["id"]})',
        data={"id": asset["id"], "kind": asset["kind"]},
        # The selection has to be cleared: leaving a detail panel open on a row that no
        # longer exists is how a UI ends up showing a ghost.
        ui_effects={"refetch": True, "select": None, "highlight": []},
        entity_id=asset["id"],
    )


# What an operator can break, and every one of these drives a state the display already
# renders. That is deliberate: a fault nobody can see on the map teaches nothing, and a
# fault that needs new rendering is a fault that will not be ready in time.
FAULTS: dict[str, str] = {
    "silent": "stops reporting, so it ages into overdue and greys out",
    "maintenance": "goes unserviceable, so it cannot be tasked",
}

# How far back a silenced asset's last report is pushed. Beyond every kind's interval, so
# the fault takes hold immediately rather than at some later moment the operator has to
# wait out. A demo that needs a four-hour wait to show a failure is not a demo.
SILENCE_BACKDATE_MINUTES = 6 * 60


@tool(
    "inject_fault",
    "Break something on purpose: make an asset go silent, or take it out of service.",
    {
        "target": "asset name or id",
        "fault": "silent, or maintenance",
    },
    writes=True,
    group="do",
)
def inject_fault(target: str, fault: str = "silent") -> ToolResult:
    """Introduce a failure so the operator can watch the picture respond.

    🔑 FAULTS ARE EXPRESSED IN THE FIELDS THE DISPLAY ALREADY WATCHES, not in a parallel
    fault model. Going quiet is `last_heard` moving into the past; going unserviceable is
    `status`. Both already colour the map, both already drive a question the command layer
    answers, and neither needs a line of new rendering. A separate `faults` table would
    have been a second source of truth about the same two facts.

    ⚠️ THE INTERESTING PART IS WHAT FOLLOWS, WHICH IS WHY THIS IS WORTH HAVING. Silencing
    one node is not one grey icon: if that node carries the cluster's backhaul, everything
    behind it loses its route to the display, and a contact a working camera can still see
    stops arriving because nothing can carry it home. That is a failure propagating through
    a real dependency rather than a flag being toggled.
    """
    key = (fault or "silent").strip().lower()
    if key not in FAULTS:
        raise ToolError(
            f'"{fault}" is not a fault I can inject. Available: '
            + "; ".join(f"{n} ({d})" for n, d in FAULTS.items())
        )

    rows = _entities()
    asset = _require_one(target, rows)
    props = dict(asset.get("props") or {})
    updated = {**asset, "props": props}

    if key == "silent":
        updated["last_heard"] = datetime.now(UTC) - timedelta(
            minutes=SILENCE_BACKDATE_MINUTES
        )
    else:
        updated["status"] = "maintenance"

    props["fault"] = key
    _write_back(updated)

    return ToolResult(
        ok=True,
        message=f'{asset["name"]} is now {key}: it {FAULTS[key]}',
        data={"id": asset["id"], "fault": key},
        ui_effects={"refetch": True, "select": asset["id"]},
        entity_id=asset["id"],
    )


@tool(
    "clear_fault",
    "Repair an asset: bring it back into service and mark it as reporting again.",
    {"target": "asset name or id"},
    writes=True,
    group="do",
)
def clear_fault(target: str) -> ToolResult:
    """Undo a fault, so the operator can watch the picture heal.

    Restoring is as much of the demonstration as breaking. A fault that cannot be cleared
    turns every experiment into a reseed, and nobody explores a world they can only damage.
    """
    rows = _entities()
    asset = _require_one(target, rows)
    props = dict(asset.get("props") or {})
    props.pop("fault", None)

    _write_back(
        {
            **asset,
            "props": props,
            "status": "nominal",
            "last_heard": datetime.now(UTC),
        }
    )

    return ToolResult(
        ok=True,
        message=f'{asset["name"]} is back in service and reporting',
        data={"id": asset["id"]},
        ui_effects={"refetch": True, "select": asset["id"]},
        entity_id=asset["id"],
    )


def _write_back(row: dict[str, Any]) -> None:
    """Persist a row that came out of a read, without persisting anything derived.

    🔴 THE READ PATH DECORATES ROWS AND `insert_entity` REPLACES THEM WHOLE. Freshness adds
    `overdue` and `flag`, connectivity adds its own pair, and the motion layer moves
    positions, none of which are columns. Handing the whole row back would try to write
    keys the insert does not know, and would persist simulated values as though an operator
    had set them. So exactly the stored columns are sent, and nothing else.
    """
    db.insert_entity(
        {
            "id": row["id"],
            "kind": row["kind"],
            "name": row["name"],
            "lat": row["lat"],
            "lon": row["lon"],
            "alt_m": row.get("alt_m"),
            "status": row["status"],
            "geometry": row.get("geometry"),
            "props": row.get("props") or {},
            "last_heard": _as_datetime(row.get("last_heard")),
            "ais_reporting": row.get("ais_reporting"),
            "created_by": row.get("created_by", "user"),
        }
    )


def schemas() -> list[dict[str, Any]]:
    """The registry as data, for building the model's prompt and the UI's button list.

    One source for both, so a tool that exists is reachable from both, and a tool that is
    removed disappears from both at once.
    """
    return [
        {
            "name": t.name,
            "summary": t.summary,
            "params": t.params,
            "writes": t.writes,
            "says": says(t.name),
            "group": t.group,
        }
        for t in REGISTRY.values()
    ]


def says(name: str) -> list[str]:
    """The sentences the reference card prints for one tool.

    🔑 READ FROM THE GRAMMAR, NOT DECLARED BESIDE THE TOOL. This was a `says` tuple on each
    `@tool`, which made it the second copy of a sentence the parser also had to know: the card
    could teach one phrasing while tier 1 accepted a different one, and the suite could only
    check that the two overlapped. Now the sentence exists once and the card is a rendering of
    the language, so a phrasing the parser does not answer cannot be printed.
    """
    return list(grammar.card_sentences().get(name, ()))


class Card(NamedTuple):
    """How one tool is printed on the reference card."""

    #: 🔴 A NAME, NOT THE IDENTIFIER. `list_entities` is a Python symbol: it names the function
    #: to whoever maintains this file and nothing at all to the operator reading the card, who
    #: is deciding between two phrasings and has no reason to care what the callable is called.
    #: An identifier printed in a product is a seam showing.
    label: str
    #: What it does, in the words the choice between two tools is made in.
    #:
    #: ⚠️ NOT the `summary` beside each `@tool`. That one is written for the model choosing a
    #: tool from seventeen, so it enumerates parameters and edge cases and runs to three lines.
    #: A card is read at a glance.
    does: str


#: The card's own vocabulary: one name and one description per tool.
#:
#: 🔑 THE FUNCTION NAME IS NOT THE ANSWER TO "which of these do I want".
#: `list_entities`, `describe_entity` and `focus_entity` are schema words, and an operator
#: reading "show", "tell me about" and "focus" cannot tell that one lists a set, one opens a
#: record and one moves the camera. That is the actual confusion, so the card answers it in the
#: words the choice is made in.
#:
#: 🔑 ONE TABLE, NOT TWO. The name and the description are read together, printed together and
#: rewritten together, so keeping them in two dicts keyed by the same string would be two copies
#: of one decision and an invitation for a tool to have half an entry.
#:
#: ⚠️ SEVERAL NAMES ARE ALREADY THE COMMAND WORD, and where that happens the card reads
#: `"sitrep" (Sitrep - what has changed in this world)`. The repetition is the message: the word
#: you say IS the thing you are reaching.
CARD: dict[str, Card] = {
    "list_entities": Card("Asset List", "lists or counts a set of assets"),
    "coverage": Card("Coverage Gaps", "what the network is NOT seeing"),
    "show_overlay": Card("Ice Overlay", "draws the measured sea ice"),
    "set_visible_kinds": Card("Display Filter", "hides or shows a whole kind"),
    "reset_view": Card("Wide View", "camera back to the whole Arctic"),
    "focus_entity": Card("Slew To Asset", "camera onto one asset, and selects it"),
    "entity_history": Card("Track History", "where one asset has been"),
    "mesh_status": Card("Comms Check", "radio links, groups, and what is isolated"),
    # The description was shortened to one line on the card. Measured: the longer wording was
    # the only one that wrapped, and a wrapped description reads as a second command rather
    # than as a note on the first.
    "backhaul_status": Card("Satcom Check", "satellite terminals, and who can reach one"),
    "describe_entity": Card("Asset Readout", "the full record for one asset"),
    "recent_activity": Card("Sitrep", "what has changed in this world"),
    "place_asset": Card("Emplace", "puts a new asset on the map"),
    "task_uas": Card("Vector Drone", "sends a drone to a position"),
    "remove_asset": Card("Scrub Asset", "deletes one asset"),
    "inject_fault": Card("Deadline", "makes an asset go silent, or unserviceable"),
    "clear_fault": Card("Restore", "brings an asset back into service"),
    # Mouse only, so it prints on no card. Named here anyway: the table is the one place a
    # tool's operator-facing name lives, and a tool missing from it is what the suite checks.
    "edit_asset": Card("Field Edit", "changes one declared field on one asset"),
}


def reference() -> list[dict[str, Any]]:
    """The registry arranged the way a person reads it: by intent, not by tool name.

    Groups keep their declared order and drop out entirely when empty, so the card never
    renders a heading with nothing under it.
    """
    out: list[dict[str, Any]] = []
    for key, label in GROUPS:
        # 🔑 THE TOOL TRAVELS WITH THE SENTENCE. "Show", "tell" and "focus"
        # are three verbs an operator cannot tell apart from the sentences alone: one lists a
        # set, one opens a record, one moves the camera. Naming the function beside the phrase
        # is the shortest way to say that they are three different things.
        #
        # 🔴 THE NAME AND THE DESCRIPTION ARE TWO FIELDS NOW, AND THEY USED TO BE ONE. `tool`
        # carried the gloss and fell back to the identifier, so the card printed "not
        # announcing" where a reader had every reason to think they were seeing a name, and the
        # two tools with no gloss printed a raw Python symbol instead. A field called `tool`
        # that usually does not hold the tool reads fine until somebody needs the actual name.
        #
        # ⚠️ `tool` IS THE OPERATOR'S NAME FOR IT, NOT THE IDENTIFIER. `list_entities` names the
        # function to whoever maintains this file; the card is read by somebody choosing what to
        # say, and a Python symbol on a console is a seam showing.
        phrases = [
            {
                "say": phrase,
                "tool": CARD[t.name].label if t.name in CARD else t.name,
                "does": CARD[t.name].does if t.name in CARD else "",
            }
            for t in REGISTRY.values()
            if t.group == key
            for phrase in says(t.name)
        ]
        if phrases:
            out.append({"key": key, "label": label, "says": phrases})
    return out


# 🔴 `show_unknown` WAS HERE AND IS GONE (2026-08-14). It answered "which unidentified contacts
# do we actually hold", which is one bucket of `detect.coverage_summary` — the same call
# `coverage` makes, whose answer already names that bucket ("N held by a sensor"). Two tools, one
# computation, one of them a subset of the other's sentence.
#
# 🔑 IT IS THE CLEANEST CASE OF THE UNIQUE-VOCABULARY TEST. No verb of its own could be written
# for it: the only honest sentence is "list the unknowns", and that is `list_entities` with a
# filter. A tool that cannot earn a word is a filter wearing a tool's clothes.
#
# ⚠️ THE STRICT READING SURVIVES, WHICH IS THE PART WORTH KEEPING. `coverage` reports all four
# buckets separately, so `detected_not_reported` (a sensor holds it and cannot deliver) and
# `untracked` (nothing holds it) are still never folded into what the console claims to see. That
# distinction was this tool's real content and it lives in `detect.coverage_summary`, where it
# always did.
