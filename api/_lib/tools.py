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
from typing import Any

from . import db, freshness, terrain
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
        names = ", ".join(f'{c["name"]} ({c["id"]})' for c in candidates)
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


REGISTRY: dict[str, Tool] = {}


def tool(name: str, summary: str, params: dict[str, str], writes: bool = False):
    def wrap(fn: Callable[..., ToolResult]) -> Callable[..., ToolResult]:
        REGISTRY[name] = Tool(name=name, summary=summary, params=params, fn=fn, writes=writes)
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
    "List assets, optionally filtered by kind, status, overdue or AIS state.",
    {
        "kind": "one asset kind, e.g. node, patrol, uas, hydrophone, vessel, radar, launch_site",
        "status": "nominal or maintenance. For assets that have gone quiet use overdue, not status",
        "not_broadcasting": "true to return only contacts holding no AIS broadcast",
        "isolated": "true to return only assets on no mesh at all",
        "overdue": "true to return only assets that have not reported inside their kind's interval",
        "bbox": "restrict to what is on screen right now, for 'in the current zoom window'",
        "ids": "restrict to these exact assets, for 'list them' after a previous answer",
    },
)
def list_entities(
    kind: str | None = None,
    status: str | None = None,
    not_broadcasting: bool | None = None,
    isolated: bool | None = None,
    overdue: bool | None = None,
    bbox: dict[str, Any] | None = None,
    ids: list[str] | None = None,
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
        message=f"{len(rows)} matching",
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
        ui_effects={"highlight": [r["id"] for r in rows]},
    )


@tool(
    "mesh_status",
    "Report radio connectivity: how many links are up, which groups can reach each other, what is isolated.",
    {},
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
        return {"available": False, "reason": "connection history is not recorded in this build"}
    try:
        stats = fn(entity_id) or {}
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"[:160]}
    return {"available": True, **stats}


@tool(
    "describe_entity",
    "Everything known about one asset, including its mesh neighbours and how reliably it has been connected.",
    {"target": "asset name or id"},
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

    return ToolResult(
        ok=True,
        message=(
            f'{asset["name"]}: {asset["kind"]}, {asset["status"]}, '
            f"{len(peers)} mesh neighbours{tail}"
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
            f'no position history is recorded for {asset["name"]} in this build. What is '
            "available: its current position, status, and who it can reach on the mesh"
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


@tool("focus_entity", "Select one asset and move the camera to it.", {"target": "asset name or id"})
def focus_entity(target: str) -> ToolResult:
    rows = _entities()
    asset = _require_one(target, rows)
    if asset["lat"] is None:
        raise ToolError(f'{asset["name"]} has no fixed position to focus on')
    return ToolResult(
        ok=True,
        message=f'focused {asset["name"]}',
        ui_effects={"select": asset["id"], "camera": frame_for([(asset["lat"], asset["lon"])])},
        entity_id=asset["id"],
    )


@tool(
    "frame_entities",
    "Move the camera so that all the named assets are visible at once.",
    {"targets": "list of asset names or ids", "kind": "or frame every asset of one kind"},
)
def frame_entities(targets: list[str] | None = None, kind: str | None = None) -> ToolResult:
    rows = _entities()
    if kind:
        chosen = [r for r in rows if r["kind"] == kind]
    else:
        chosen = [_require_one(t, rows) for t in (targets or [])]
    points = [(r["lat"], r["lon"]) for r in chosen if r["lat"] is not None]
    if not points:
        raise ToolError("none of those assets have a position to frame")
    camera = frame_for(points)
    return ToolResult(
        ok=True,
        message=f'framing {len(points)} assets across {camera.get("spread_km", 0)} km',
        ui_effects={"camera": camera, "highlight": [r["id"] for r in chosen]},
    )


# The environmental layers this build actually has. Sea ice is measured satellite data,
# vendored per date; there is no weather feed, and saying so is better than implying one.
OVERLAYS: dict[str, str] = {
    "ice": "measured sea ice concentration",
}


@tool(
    "show_overlay",
    "Turn on an environmental overlay, such as measured sea ice, over the current view.",
    {"layer": "ice"},
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
            f'there is no "{key}" overlay in this build. What exists: '
            + ", ".join(f"{n} ({d})" for n, d in OVERLAYS.items())
            + ". Nothing here fetches live weather at runtime, deliberately"
        )
    return ToolResult(
        ok=True,
        message=f"showing {OVERLAYS[key]}",
        data={"layer": key},
        ui_effects={"overlay": {"layer": key, "visible": True}},
    )


@tool("reset_view", "Return the camera to the default view of the whole Arctic.", {})
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
        "kind": "node, hydrophone, marker or launch_site",
        "lat": "latitude in degrees",
        "lon": "longitude in degrees",
        "name": "optional display name",
        "hostile": "true to place it as an adversary rather than one of ours",
    },
    writes=True,
)
def place_asset(
    kind: str, lat: float, lon: float, name: str | None = None, hostile: bool | None = None
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
            "alt_m": 0.0,
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
            "props": {"placed_by": "operator", **({"hostile": True} if hostile else {})},
            "last_heard": None,
            "ais_reporting": None,
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

    props = asset.get("props") or {}
    endurance = float(props.get("endurance_min_remaining") or 0)
    cruise = float(props.get("cruise_kmh") or 140)
    reserve_min = 30.0

    distance = meshlib.haversine_km(asset["lat"], asset["lon"], lat, lon)
    radius_km = max(0.0, (endurance - reserve_min) / 2.0 / 60.0 * cruise)
    if distance > radius_km:
        raise ToolError(
            f'{asset["name"]} cannot reach that station: {distance:.0f} km out against a '
            f'{radius_km:.0f} km radius on {endurance:.0f} minutes of fuel, and it has to '
            "come back"
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
                "state": "on_station",
                "station": [lat, lon],
                "eta_min": round(eta_min, 1),
                "endurance_min_remaining": round(endurance - eta_min, 1),
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
            "this build does not compute sensor coverage. What is available: asset status, "
            "mesh connectivity, and which contacts are not broadcasting"
        )

    out = summary(_entities())
    counts = out.get("counts", {})

    # 🔑 THE ID LISTS ARE ALREADY IN THE ANSWER, so this names the contacts rather than
    # recomputing them. A count nobody can act on is a worse answer than a short list
    # somebody can go and look at, and the two cannot disagree if only one of them exists.
    missing = list(out.get("detected_not_reported", [])) + list(out.get("untracked", []))

    parts = [f'{counts.get("self_reporting", 0)} reporting their own position']
    if counts.get("tracked"):
        parts.append(f'{counts["tracked"]} held by a sensor')
    if counts.get("detected_not_reported"):
        parts.append(
            f'{counts["detected_not_reported"]} seen by a sensor that cannot report it'
        )
    if counts.get("untracked"):
        parts.append(f'{counts["untracked"]} held by nothing at all')
    if not missing:
        parts.append("nothing is unaccounted for")

    return ToolResult(
        ok=True,
        message="; ".join(parts),
        data={**out, "not_on_the_picture": missing},
        ui_effects={"highlight": missing},
    )


@tool(
    "remove_asset",
    "Remove an asset from the world. Works on anything, seeded or placed by an operator.",
    {"target": "asset name or id"},
    writes=True,
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
        {"name": t.name, "summary": t.summary, "params": t.params, "writes": t.writes}
        for t in REGISTRY.values()
    ]


@tool(
    "show_unknown",
    "Contacts not announcing their own identity that the sensor network is actually holding.",
    {},
)
def show_unknown() -> ToolResult:
    """The unidentified contacts the console can legitimately claim to have.

    🔑 AN UNKNOWN IS A CONTACT THAT IS NOT SELF-REPORTING, and that question is already
    answered for all three contact kinds: AIS for a vessel, a transponder for an aircraft,
    an emitter for a ground party. `detect.coverage_summary` splits exactly that set, so
    nothing new is computed here and there is no second definition of "unknown" to keep in
    step with the first.

    🔴 ONLY `tracked` IS RETURNED AS THE ANSWER, and the two buckets left out are the whole
    point of the tool:

        tracked                 not talking, a sensor holds it, the report gets home  -> YES
        detected_not_reported   a sensor holds it and CANNOT deliver the report       -> no
        untracked               nothing holds it and it is not talking                -> no

    ⚠️ `detected_not_reported` IS THE COUNTER-INTUITIVE EXCLUSION, and it is the strict
    reading on purpose: if the report is not reaching you, you do not have the contact. A
    sensor holding something it cannot deliver is a link fault, not coverage. So the
    default display claims only what actually arrived.

    🔒 `untracked` IS AN HONESTY PROBLEM, NOT JUST A FILTER. Nothing holds it and it is not
    talking, so the console **cannot legitimately know it exists**: that set is read out of
    the seeded world, not derived from the sensor network. Putting it behind a deliberate
    act means the default view asserts nothing it cannot support, which is the same rule
    the ice layer and the relay figures already follow. It must never re-enter a default
    count, summary or status line.

    Both excluded lists still travel in `data`, because the client's reveal toggle needs
    them and a second round trip to fetch what was already computed would be waste.
    """
    detect = _detect_module()
    summary = getattr(detect, "coverage_summary", None) if detect else None
    if summary is None:
        raise ToolError(
            "this build does not compute which contacts are identified. What is available: "
            "asset status, mesh connectivity, and which contacts are not broadcasting"
        )

    out = summary(_entities())
    by_id = {r["id"]: r for r in _entities()}

    covered = list(out.get("tracked", []))
    stranded = list(out.get("detected_not_reported", []))
    unheld = list(out.get("untracked", []))

    rows = [by_id[i] for i in covered if i in by_id]
    message = f"{len(covered)} unknown contact{'' if len(covered) == 1 else 's'} held by the network"
    if stranded or unheld:
        # ⚠️ NAMED, NOT SILENTLY DROPPED. The operator is being shown a smaller set than
        # exists, and a display that quietly narrows what it claims is the failure this
        # whole distinction is meant to prevent. Saying how many were withheld, and why,
        # costs one clause.
        message += f"; {len(stranded) + len(unheld)} more cannot be confirmed from the network alone"

    return ToolResult(
        ok=True,
        message=message,
        data={
            "ids": covered,
            "names": [r["name"] for r in rows],
            "points": [
                [r["lat"], r["lon"]]
                for r in rows
                if r.get("lat") is not None and r.get("lon") is not None
            ],
            # The reveal buckets. Present so a toggle costs no round trip; never counted
            # in `ids`, which is what the default view asserts.
            "detected_not_reported": stranded,
            "untracked": unheld,
            "counts": {
                "covered": len(covered),
                "detected_not_reported": len(stranded),
                "untracked": len(unheld),
            },
        },
        ui_effects={"highlight": covered},
    )
