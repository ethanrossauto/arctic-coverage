"""The single ASGI entrypoint. One FastAPI app, all routes.

⚠️ THIS SHAPE WAS FORCED BY THE PLATFORM, AND IT IS AN IMPROVEMENT.

The first version of this backend used Vercel's older Python convention: one file
per route under `api/`, each exporting a `handler` subclass of
`BaseHTTPRequestHandler`. Vercel's current Python runtime rejects that and asks
for a single ASGI entrypoint declared in `pyproject.toml`. Discovering that cost
one failed deploy, which is precisely why the first thing this project did was
try to deploy something trivial.

The forced answer is better than the original:

  * One app object, so `uvicorn api.index:app` locally and the Vercel function in
    production run the identical router. No second code path to drift.
  * The whole API is testable in-process with FastAPI's TestClient, with no
    network and no server.
  * Pydantic validation comes with it, which the tool layer needed regardless.

Routes are declared with their `/api` prefix in the path rather than via a
router prefix, so what is written here matches what the browser requests, and
grep finds it.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from ._lib import db

# 🔒 OPTIONAL BY CONSTRUCTION. The detection layer decorates an answer rather than
# producing one, so its absence must cost a field and never a route. Imported through a
# try so this file loads even mid-edit on a shared tree.
try:
    from ._lib import detect
except Exception:  # noqa: BLE001
    detect = None  # type: ignore[assignment]

from ._lib import (
    domain,
    executor,
    fields,
    grammar,
    lifecycle,
    parser,
    plaintext,
    ratelimit,
    transcribe,
)
from ._lib import llm as llmlib
from ._lib import mesh as meshlib
from ._lib import tools as toollib

app = FastAPI(
    title="Arctic Coverage",
    description=(
        "Situational awareness for deployable Arctic sensor networks."
    ),
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)


@app.middleware("http")
async def one_connection_per_request(request: Request, call_next: Any) -> Any:
    """Hold a single database connection open for the whole request.

    🔴 THIS IS THE LATENCY FIX, AND THE MEASUREMENT IS WHY IT EXISTS. Neon scales to zero
    and every fresh connection costs about 700 ms warm. One command opened three of them in
    series before this, and a multi-step plan opens more: every tool call and every audit
    row paid its own handshake. The queries themselves run against a 76-row table and are
    not the cost.

    ⚠️ MIDDLEWARE RATHER THAN A DECORATOR ON EACH ROUTE. A route added later inherits it
    without knowing it exists, which is the only version of this that stays true after
    somebody adds an endpoint in a hurry. The scope opens nothing by itself, so a request
    that never touches the database still pays nothing.

    🔑 IT ALSO WARMS THE REASONING PATH, for the same reason it holds the connection: this
    is the one place every request passes through. The first request an instance serves is
    almost never the command, it is the page asking for entities, and that is exactly the
    moment worth spending on the handshake nobody is waiting for yet. See
    `llm.warm_in_background`, which returns immediately and costs nothing.
    """
    llmlib.warm_in_background()
    with db.request_scope():
        return await call_next(request)


@app.get("/api/healthz")
def healthz() -> dict:
    """Liveness, plus enough detail to tell WHICH build answered.

    During the first deploys the useful question was never "is it up" but "is it
    running the code I think it is", so this says enough to tell builds apart.
    """
    return {
        "ok": True,
        # Reported rather than assumed: this endpoint exists to tell "the app is up but the
        # database is not" apart from "the app is down", which was exactly the gap that made
        # a broken deploy on 2026-08-07 diagnosable only from provider logs.
        "database": db.status(),
        "spend": ratelimit.status(),
    }


@app.get("/api/entities")
def entities(
    kind: str | None = Query(
        None,
        description="Filter to one asset kind: node, patrol, uas, hydrophone, vessel, radar, marker.",
    ),
) -> JSONResponse:
    """Every asset in the world, or every asset of one kind.

    Read-only, and deliberately NOT logged to the audit trail. The log records
    actions taken, and hydrating a client is not an action: logging it would bury
    every real event under a wall of page loads and make the trail useless for the
    thing it exists for. Writes and tool invocations are logged without exception.
    """
    try:
        # 🔴 EVERY ROW IS FETCHED EVEN WHEN ONE KIND IS ASKED FOR, AND THE FILTER IS
        # APPLIED BELOW. Connectivity is a property of the whole graph: whether a node can
        # reach a gateway depends on assets of other kinds standing between it and one.
        # Asking the database for `kind = 'node'` and computing flags from what came back
        # would compute them against a world with most of its relays deleted, and the
        # answer would be confidently wrong rather than missing. Sixty-eight rows is
        # cheaper than the second query the alternative needs.
        rows = db.fetch_entities()
    except RuntimeError as exc:
        # A missing connection string is a configuration failure, not an empty
        # world, and it must not render as one.
        raise HTTPException(503, _DB_UNAVAILABLE) from exc

    _add_connectivity(rows)
    _add_tracking(rows)
    if kind:
        rows = [r for r in rows if r["kind"] == kind]

    # ℹ️ `overdue` and `flag` are already on every row: `db.fetch_entities` decorates at
    # the read, so this endpoint, the mesh and every tool see one answer computed against
    # one instant. This route used to add them itself, which was one caller doing it
    # correctly while the browser did it again from its own copy of the intervals.
    return JSONResponse(
        {"entities": rows, "count": len(rows)},
        headers={"cache-control": "no-store"},
    )


def _add_connectivity(rows: list[dict]) -> None:
    """Put both connectivity flags on each row, where they are facts about that asset.

    🔑 THE ABSENCE IS THE CONTRACT, NOT AN OVERSIGHT. A vessel and a radar site carry no
    radio, so neither flag is true or false about them, and this leaves the keys off those
    rows rather than writing `false`. `false` would read as "we checked and it cannot
    reach us", which is a different and wrong claim: the honest answer is that the question
    does not apply.

    🔒 GUARDED, because it reaches into a module another part of the build owns and the
    whole application is imported through this file. A missing helper degrades the map's
    colouring; it must never take down the endpoints that have nothing to do with it.
    """
    flags = getattr(meshlib, "asset_flags", None)
    if flags is None:
        return
    try:
        by_id = flags(rows)
    except Exception:  # noqa: BLE001 - connectivity is a decoration on an answer that exists
        return
    for row in rows:
        found = by_id.get(row["id"])
        if found:
            row.update(found)


def _add_tracking(rows: list[dict]) -> None:
    """Say, per contact, whether we actually have it on the picture.

    🔑 THE POINT IS THE GAP BETWEEN "IT IS THERE" AND "WE KNOW IT IS THERE". A contact can
    be present in the world and absent from the display in two different ways: nothing is
    holding it, or something is holding it and the route carrying that report home is down.
    Both come back as `tracked: false`, because from an operator's chair they are the same
    situation, and `held` says which of the two it is.

    That distinction is what makes an adversary worth adding to this world. Placing a
    hostile contact somewhere nothing can see it is not a decoration; it is the question
    the whole sensor network exists to answer, and the map can now be asked it directly.

    ⚠️ CONTACTS ONLY, and absent on everything else, for the same reason the connectivity
    flags are absent on a vessel: a mesh node is not something we track, so neither `true`
    nor `false` is a fact about it.

    🔒 GUARDED, like the connectivity pair. A missing detection layer costs the checkbox,
    never the endpoint.
    """
    grouped_by_contact = getattr(detect, "held_by", None) if detect else None
    if grouped_by_contact is None:
        return
    try:
        held = grouped_by_contact(rows)
        self_reporting = getattr(detect, "_self_reporting", None)
    except Exception:  # noqa: BLE001 - a decoration on an answer that already exists
        return

    for row in rows:
        found = held.get(row["id"])
        if found is None:
            continue
        # 🔴 AN ASSET THE OPERATOR PLACED IS KNOWN BECAUSE THE OPERATOR PLACED IT, and not
        # saying so made a placed vessel VANISH. Nothing was detecting it and it carried no
        # AIS, so it came back `tracked: false` with nothing holding it, which the display
        # correctly reads as an undetected unknown and correctly hides behind a checkbox
        # that is ticked by default. Every step of that was right and the outcome was
        # absurd: you put a ship on the map and the map denies it is there.
        #
        # The console cannot legitimately know about a contact nobody detected. It can
        # always legitimately know about one it was told to create. Leaving the fields OFF
        # rather than setting them true is the same move this function already makes for a
        # mesh node: tracking is not a fact about this row, so neither value is honest.
        #
        # ⚠️ A HOSTILE PLACEMENT IS EXEMPT, AND THAT IS THE WHOLE POINT OF IT. Dropping an
        # adversary somewhere nothing can see it is the question this sensor network exists
        # to answer, so those keep the honest `tracked: false` and stay behind the checkbox.
        # The difference is intent: one is scenery the operator added, the other is a test.
        if (row.get("props") or {}).get("placed_by") == "operator" and not (
            (row.get("props") or {}).get("hostile")
        ):
            continue
        reported = [d for d in found if d.get("reported")]
        announcing = bool(self_reporting(row)) if self_reporting else False
        row["held"] = len(found)
        row["tracked"] = announcing or bool(reported)
        # 🔑 DETECTED UNKNOWN: we hold it, and it will not say what it is. Computed HERE
        # rather than in the browser, even though `tracked` and `ais_reporting` are both on
        # the wire and the client could nearly derive it. Nearly is the problem. `tracked`
        # is `announcing or reported`, so it spans two buckets, and "announcing" is not the
        # same question as `ais_reporting is False`: a contact with no AIS field at all
        # falls through to a transponder and then to an emitting flag. A client rule of
        # `tracked && ais_reporting === false` gets the common case right and quietly
        # disagrees with the server about the rest, which is two answers to one question.
        row["detected_unknown"] = bool(reported) and not announcing


@app.get("/api/mesh")
def mesh() -> JSONResponse:
    """Who can currently talk to whom, computed from live positions.

    Derived per request and never stored. A stored graph is a second source of truth that
    goes stale the moment an entity moves, and everything here moves.

    Read-only, so it is not logged, for the same reason `/api/entities` is not: the audit
    trail records actions taken, and looking at the world is not one.
    """
    try:
        rows = db.fetch_entities()
    except RuntimeError as exc:
        raise HTTPException(503, _DB_UNAVAILABLE) from exc

    payload = meshlib.mesh_status(rows)
    payload["model"] = _mesh_model()
    # 🔑 THE SECOND GRAPH OVER THE SAME ASSETS: which sensor is holding which contact. It
    # rides here rather than on its own endpoint because the map already polls this one and
    # both are the same kind of thing, a set of pairs computed from live positions and never
    # stored. Adding a third request per tick to draw one more line layer is a worse trade.
    #
    # ⚠️ ADDED TO THE ENDPOINT, NOT TO `meshlib.mesh_status`. The `mesh_status` TOOL answers
    # a question about radio connectivity, and detections are not that; giving it a field it
    # never reads would put them in an answer nobody asked for.
    #
    # Every pair carries `reported`, and the client draws only the ones that reach us. A
    # sensor holding something whose uplink is down is exactly the case this project refuses
    # to draw: the console cannot legitimately know it.
    # ⚠️ GUARDED, BECAUSE `detect` IS OPTIONAL BY CONSTRUCTION. The import at the top of this
    # file falls back to None so a missing detection layer costs a field and never a route.
    # Calling it unconditionally would have turned that guarantee into a 500 on the endpoint
    # the map polls twice a tick.
    payload["detections"] = detect.detections(rows) if detect else []
    return JSONResponse(payload, headers={"cache-control": "no-store"})


def _mesh_model() -> dict:
    """How the links above were decided, asked of the thing that decided them.

    🔴 THIS BLOCK USED TO BE A HARDCODED DESCRIPTION OF THE LINK MODEL, SERVED TO ANYONE
    WHO CALLED THE ENDPOINT, and it was written in a different file from the code it
    described. That is the exact shape that goes false quietly: change how links are
    computed and the API keeps publishing the old formula, with the confidence of
    something that came off the wire rather than out of a comment.

    So it is asked for instead. The module that computes the links describes them, and
    the two cannot disagree because there is only one of them.

    🔒 FAILS SOFT, IN BOTH DIRECTIONS. A build whose mesh module publishes no description
    says so, rather than this file inventing one on its behalf. Saying nothing about the
    assumptions is a smaller problem than saying something untrue about them.
    """
    describe = getattr(meshlib, "model", None)
    if callable(describe):
        try:
            return describe()
        except Exception:  # noqa: BLE001 - a broken description must not break /api/mesh
            pass

    return {
        "note": (
            "this build does not publish its link model. The links above are computed "
            "from live positions and are a planning aid, not a link budget."
        )
    }


def _model_context(
    context: dict[str, Any] | None,
    *,
    heard: str | None = None,
    running: str | None = None,
) -> dict[str, Any] | None:
    """The context, bounded, before it is serialised into a prompt.

    🔑 A FOLLOW-UP NEEDS THE THREAD. "Now just the ones on foot" is meaningless without the
    turn before it, so the recent history goes to the model as well as to the placeholder
    resolver.

    🔒 BOUNDED HERE BECAUSE IT ARRIVES FROM A BROWSER. Everything in `context` is client
    input, and it is about to be pasted into a paid API call: an oversized `recent` costs
    tokens on every request and pushes the instructions further from the question. Three
    turns and fifty ids is deixis, which is all this is for. Anything longer would be
    memory, and memory is not what "them" means.
    """
    trimmed = dict(context or {})
    # 🔑 ATTACHED HERE RATHER THAN AT EACH CALL SITE, so both ways into tier 2 carry it. The
    # direct path and the escalation path have drifted apart before, and a capability that
    # exists on one of them is a capability that works depending on how you phrased the
    # question.
    trimmed["world"] = _world_digest()
    # 🔑 THE WORDS BEFORE THE GUESS, WHEN A GUESS WAS MADE. The transcriber matches spoken
    # names against the live map, which is what makes "day mark oh three" resolve at all
    # and is also how "Resolute Bay Patrol" can arrive as "FLS Resolute Bay". That
    # substitution is the transcriber's opinion, formed with no knowledge of what the
    # sentence was asking for. This tier has the whole world and the request in front of it
    # and can tell that a patrol was meant, so it gets to see both and decide.
    #
    # ⚠️ ONLY WHEN THEY DIFFER. Attaching an identical string to every spoken command would
    # spend tokens to say nothing and teach the model to expect a correction that is not
    # there.
    if heard and running and heard.strip() != running.strip():
        trimmed["heard_before_correction"] = heard.strip()
    recent = trimmed.get("recent")
    if isinstance(recent, list):
        turns = []
        for turn in recent[-3:]:
            if not isinstance(turn, dict):
                continue
            ids = turn.get("ids")
            turns.append(
                {
                    "utterance": str(turn.get("utterance", ""))[:200],
                    "summary": str(turn.get("summary", ""))[:200],
                    "tier": turn.get("tier"),
                    "ids": [i for i in ids if isinstance(i, str)][:50] if isinstance(ids, list) else [],
                }
            )
        trimmed["recent"] = turns
    return trimmed


def _world_digest() -> list[dict[str, Any]]:
    """Every asset, compactly, for the model to reason over directly.

    🔑 THE ANSWER TO "SHOW ME THE NORTHERNMOST ASSET", AND TO A WHOLE CLASS LIKE IT. That
    question is not a filter, it is a comparison across the set, and no enumerated tool
    parameter will ever express every comparison somebody might ask for. Handing the model
    the actual rows lets it do the reasoning and answer with `ids`, which is a tool that
    already exists. The alternative is a new parameter per question, forever.

    ⚠️ IT IS THE MODEL'S WORKING SET, NEVER THE ANSWER. The model returns ids and the tools
    re-read those rows from the database, so what reaches the operator has been through the
    same path as every other answer. Nothing here is trusted: an id that does not exist
    resolves to nothing, exactly as a hallucinated name already did.

    🔒 SEVEN FIELDS, NOT THE ROW. Positions, kind, name, status and freshness are what
    comparisons are asked about. `props` is where the bulky per-kind detail lives and it is
    left out deliberately: it would multiply the token cost of every call for questions
    nobody asks, and `describe_entity` already exists for the one asset somebody cares about.
    """
    try:
        rows = db.fetch_entities()
    except Exception:  # noqa: BLE001 - the model can still route without the world
        return []
    digest = []
    for r in rows:
        digest.append(
            {
                "id": r["id"],
                "name": r["name"],
                "kind": r["kind"],
                "lat": round(r["lat"], 3) if r.get("lat") is not None else None,
                "lon": round(r["lon"], 3) if r.get("lon") is not None else None,
                "status": r.get("status"),
                "flag": r.get("flag"),
            }
        )
    return digest




# ---------------------------------------------------------------------------
# What the operator is told when the system itself could not act
# ---------------------------------------------------------------------------

#: What this console is, in one line, for a reply that has to explain itself.
_WHAT_THIS_IS = (
    "This console tracks Arctic sensor assets, the radio mesh between them, satellite "
    "backhaul, measured sea ice and unidentified contacts."
)
#: Three commands that always work, including when the metered layer does not.
#:
#: 🔴 TWO OF THE THREE DID NOT WORK, AND THIS IS THE ONE LINE WHERE THAT IS UNFORGIVABLE
#: (found 2026-08-14). It read `"mesh status", "which assets are overdue", "show me the
#: drones"`, and `parser.parse` returns None for the last two: they escalate to tier 2. So the
#: sentence shown to an operator BECAUSE tier 2 is unavailable recommended two commands that
#: need tier 2.
#:
#: They were written against the keyword parser, which matched "overdue" anywhere and dropped
#: "me" as filler. The anchored grammar does neither, and nothing failed when it landed because
#: no test read this constant back through the parser. There is one now.
#:
#: ⚠️ SO IT IS DERIVED, NOT TYPED. Taking the sentences from the grammar means this cannot go
#: stale again the next time the language changes, which it just did.
_TRY_THESE = ", ".join(f'"{s}"' for s in grammar.suggestions())

#: What the operator is told when tier 2 was asked and did not answer.
#:
#: 🔴 TIER 2 EITHER ANSWERS OR SAYS THIS. IT NEVER HANDS BACK TO THE PARSER. Both failure
#: paths used to end in a tier-1 sentence: the direct one returned `tier: "parser"` with a
#: line about the reasoning layer, and the escalation returned None so the operator was shown
#: the parser's own refusal about a name that does not exist. Neither says the thing that
#: actually happened, and the second is worse than saying nothing: the parser's refusal is
#: what the escalation exists to keep off the screen, so it arrives looking like an answer.
#:
#: ⚠️ VERBATIM, NOT DRESSED. `_dressed` exists to replace the machine's vocabulary with
#: something a visitor can read, and this sentence is already that. Appending the console
#: blurb and a list of examples after it would bury the one instruction it gives.
_TIER_TWO_UNREACHABLE = "Unable to reach Claude servers, please use deterministic commands only"

#: What a caller is told when the database cannot be reached.
#:
#: ⚠️ THE UNDERLYING MESSAGE IS A CONFIGURATION NOTE, not an answer. It names environment
#: variables and how to populate them locally, which is the right thing to say to whoever
#: is running this and the wrong thing to hand to anyone else. It stays in the server log,
#: where the person who needs it is looking, and the response says what happened instead.
_DB_UNAVAILABLE = "the world is not reachable right now, so this console cannot answer. Try again shortly."


#: How much model prose may occupy the answer line. Two plain sentences is what the prompt
#: asks for and roughly what the good answers measure; past this it is not an answer to a
#: question about the Arctic, it is something else wearing the console's voice.
_ANSWER_LIMIT = 500

# 🔴 THE VALIDATOR TALKING TO A PROGRAMMER. These are the shapes `executor.validate`
# produces, and every one of them is a sentence about this program's internals rather than
# about the world: step numbers, parameter names, the word "tool" used as a type. They are
# perfect in the audit log and wrong on the screen, which is the same distinction `_dressed`
# already draws between the system's failures and the world's refusals.
_MACHINE_TALK = re.compile(
    r"^step \d|is not a tool|unknown parameters|params must be|missing required parameters"
    r"|names no tool|is not an object|the plan is empty|the plan has \d+ steps",
    re.IGNORECASE,
)


def _showable(said: str) -> str | None:
    """Model prose, if it is the shape of an answer. None means show a refusal instead.

    🔴 THE SCHEMA CONSTRAINS `steps` AND HAS NEVER CONSTRAINED `reasoning`. Everything the
    model can be talked into saying arrives through this one field and is printed as the
    console's own voice, so the prompt is the only thing standing between a visitor and a
    poem on the answer line. A prompt is a request, not a boundary, which this codebase
    says out loud about the model everywhere except here.

    🔑 THE TEST IS SHAPE, NOT SUBJECT, because shape is what this layer can actually judge.
    A real answer here is one or two plain sentences. A poem, a list, a character sketch or
    a recitation of the instructions all arrive long or with line breaks in them, and none
    of the genuine answers ever has either. Judging the subject would need a classifier;
    judging the form needs two conditions and cannot be argued with.

    ⚠️ NOTHING IS LOST WHEN THIS SAYS NO. The full text is already in the audit row, written
    before this runs, so the record still holds exactly what the model said.
    """
    said = plaintext.plain(said)
    if not said:
        return None
    if "\n" in said or len(said) > _ANSWER_LIMIT:
        return None
    return said


def _lead_in(outcome: dict[str, Any], reasoning: str, *, even_when_refused: bool = False) -> None:
    """Put tier 2's own sentence in front of the result it went and fetched.

    🔴 A TIER-2 ANSWER IS NOT AN ORDINARY TOOL CALL AND SHOULD NOT READ LIKE ONE. Reaching
    this tier means the deterministic parser could not place the sentence, so something had
    to interpret the request before anything could run. Showing only the tool's line threw
    that interpretation away: asked what a survey was, the console answered "2 assets
    match: Survey 03 and Survey Team Alpha", which is a correct answer to a question nobody
    could see being asked.

    🔑 IT COSTS NOTHING, WHICH IS WHY IT IS THIS AND NOT A SECOND MODEL CALL. The obvious
    way to have the model narrate a result is to send the result back to it, which doubles
    the price and the latency of every escalated command. This text already exists: the
    model writes it while planning, before the data is fetched, and it was already being
    logged and thrown away. So the lead says what is about to happen rather than what came
    back, and the tool still reports the facts.

    ⚠️ SUCCESSES ONLY, WITH ONE EXCEPTION. A refusal from a tool already carries its own
    full explanation, and introducing one with "Searching for assets matching survey" would
    put a promise in front of an apology. The exception is a plan tier 2 proposed and the
    validator refused: there the lead is the only thing on screen saying what was attempted,
    and "Setting that drone's altitude. 99000 m is outside the altitude an asset here can be
    given" is a complete account of what happened, in order.
    """
    if not outcome.get("ok") and not even_when_refused:
        return
    lead = _showable(reasoning)
    summary = str(outcome.get("summary", "")).strip()
    if not lead or not summary or summary.startswith(lead):
        return
    if not lead.endswith((".", "!", "?", ":")):
        lead += "."
    # ⚠️ THE SECOND SENTENCE HAS TO START LIKE ONE. These messages are written to stand
    # alone, so they begin in lower case, which is right on their own and wrong directly
    # after a full stop: "Tasking Daymark 03. to task uas I still need a latitude" reads as
    # a string join rather than as a person talking.
    summary = summary[0].upper() + summary[1:]
    outcome["summary"] = f"{lead} {summary}"


def _dressed(cause: str = "") -> str:
    """A refusal an operator can read, for the failures that are the SYSTEM's rather than
    the world's.

    🔑 THE DISTINCTION IS WHERE THE REFUSAL CAME FROM, NOT HOW IT IS WORDED. A tool that
    declines because a hydrophone cannot go 2 km inland, or because a name matched five
    assets, is the product working: those sentences are specific, actionable and worth
    reading, and they are left exactly as they are. What gets dressed is the other kind:
    a parse that failed, a plan that would not validate, a provider that was unavailable, a
    referent that resolved to nothing. Those tell the operator about the machinery rather
    than about the Arctic, and "I could not turn that into a command" is a system talking
    to itself in front of a visitor.

    ⚠️ IT STILL SAYS SOMETHING TRUE. Dressing a refusal is not hiding it: the underlying
    reason is written to the audit log verbatim on the way past, so nothing is lost, it is
    just not the sentence a stranger reads first.

    🔴 A VALIDATOR REASON IS NOT A LEAD SENTENCE. Dressing used to pass the cause through
    whatever it said, so `step 1 (task_uas): altitude 99000.0 is out of range` reached the
    operator with a friendly paragraph stapled to it, which reads worse than either half
    alone: internal vocabulary given a polite voice. Those causes are replaced rather than
    prefixed, and they stay verbatim in the log.

    ⚠️ ONE SUGGESTION PER REFUSAL. Several causes already end with a specific "try ...",
    which is the better sentence because it was written for that exact request. Appending
    the generic list after it produced two suggestions in one breath and buried the good
    one, so a cause that already suggests something keeps its own.
    """
    lead = plaintext.plain(cause)
    if _MACHINE_TALK.search(lead):
        lead = "I could not turn that into a command I can run"
    lead = lead or "I could not act on that one"
    if not lead.endswith((".", "!", "?")):
        lead += "."
    if "try " in lead.lower():
        return f"{lead} {_WHAT_THIS_IS}"
    return f"{lead} {_WHAT_THIS_IS} Try {_TRY_THESE}."

def _log_tier2_failed(
    req: CommandRequest,
    exc: llmlib.LLMUnavailable,
    *,
    why: str,
    command_id: str | None = None,
    parent_command_id: str | None = None,
) -> None:
    """Write down a tier-2 failure in enough detail to diagnose it from the log alone.

    🔴 WHAT THIS REPLACED, AND THE DAY IT COST SOMETHING. Both failure paths wrote a row saying
    `unparsed / rejected` with the exception squashed into one sentence, and no tier. So the
    failures were invisible to every question anybody asks of this log: `where tier = 'llm'`
    missed them entirely, because a call that failed was recorded as if no tier had run.

    A live timeout was diagnosed by reading that one string and then going to the source for the
    ceiling, the retry count and the measured latency, none of which were in the row. Every one
    of them is here now, next to the utterance that provoked it.

    ⚠️ tier IS 'llm' AND result IS 'error', BOTH DELIBERATE. The call was made, so the tier ran
    and the log should say so; and by this codebase's own convention `rejected` means the request
    was refused while `error` means the machinery did not work, which is what an unreachable
    provider is. Both values are in the schema's check constraints, which is checked here rather
    than assumed: a value the database refuses turns every failure into a 500.
    """
    db.log_event(
        tool="tier2_failed",
        source=req.source,
        tier="llm",
        result="error",
        command_id=command_id,
        parent_command_id=parent_command_id,
        latency_ms=exc.elapsed_ms,
        params={"utterance": req.utterance, "why": why, **exc.as_params()},
        detail=(
            f"{why}; tier 2 unavailable"
            + (f" after {exc.elapsed_ms} ms" if exc.elapsed_ms is not None else "")
            + (f" over {exc.attempts} attempt(s)" if exc.attempts else "")
            + f": {exc}"
        ),
    )


def _tier_two_unreachable(
    exc: llmlib.LLMUnavailable,
    *,
    handed_over: dict[str, Any] | None = None,
    command_id: str | None = None,
) -> JSONResponse:
    """The one answer a failed tier-2 call gives, on both of the paths that reach it.

    🔑 ONE FUNCTION BECAUSE THERE ARE TWO WAYS IN, and they have drifted apart before. The
    direct path and the escalation both call the model, both can fail, and each used to
    answer that failure in its own way, so what an operator saw depended on whether the
    parser had guessed wrong first. `_teach` is here for the same reason.

    ⚠️ tier IS "llm", AND THAT IS THE CORRECTION. The direct path reported `tier: "parser"`
    on a failed model call, which puts the parser's chip on a line the parser had nothing to
    do with: the sentence was one it declined, and the tier chip is how this console makes
    "the model runs only when it earns its latency" checkable on screen. A chip that names
    the wrong tier makes the claim unreadable. The audit row already says `tier="llm",
    result="error"`, so the response now agrees with the log.

    🔑 THE WAIT IS PART OF THE ANSWER. A timeout at the client ceiling is the failure an
    operator actually notices, because they sat through it. `latency_ms` is already measured
    on the exception and renders in the trace as the seconds they waited, which is the
    difference between "this is broken" and "this took 30 seconds and then broke".
    """
    thinking: dict[str, Any] = {"tier": "llm"}
    if exc.model:
        thinking["model"] = exc.model
    if exc.elapsed_ms is not None:
        thinking["latency_ms"] = exc.elapsed_ms
    # 🔑 WHY THE MODEL WAS CALLED AT ALL, kept for the same reason a successful escalation
    # keeps it: without it the trace reads as a model that failed for no stated reason, when
    # what happened is that tier 1 declined first. Nested rather than merged, because these
    # are the parser's findings on a response whose tier is the model's.
    if handed_over and (handed_over.get("matched") or handed_over.get("declined")):
        thinking["parser"] = handed_over

    body: dict[str, Any] = {
        "ok": False,
        "summary": _TIER_TWO_UNREACHABLE,
        "tier": "llm",
        "results": [],
        "thinking": thinking,
    }
    if command_id:
        body["command_id"] = command_id
    return JSONResponse(body, status_code=200, headers={"cache-control": "no-store"})


def _log_tier1(
    req: CommandRequest,
    plan: list[dict] | None,
    trace: dict[str, Any],
    *,
    command_id: str | None = None,
) -> None:
    """Narrate tier 1's decision into the audit log, in the terms it actually decided in.

    ⚠️ PROSE, NOT A DUMP. The row an evaluator reads should say what happened, not require
    them to reconstruct it from a parameter bag. Every fact in the sentence is also in the
    params beside it, so the narrative is readable and the underlying values are checkable.
    """
    matched = trace.get("matched")
    sentence = trace.get("grammar")
    tools_n = len(toollib.REGISTRY)

    if matched:
        # 🔑 THE SENTENCE, NOT ONLY THE TOOL. Which declared phrasing answered is the one thing
        # in this row an operator could act on, and it is exactly what the reference card
        # prints. The row that used to sit here reported which words the parser had thrown
        # away, because a branch could answer half a question; an anchored grammar accounts for
        # every word by construction, so there is nothing left to report as dropped.
        story = f'matched the declared command "{sentence}", so {matched} ran with no model call'
        result = "ok"
    else:
        story = (
            f"searched {tools_n} available tools, no declared command matches this phrasing; "
            "escalating to the reasoning layer"
        )
        # 🔴 THIS WAS `"escalated"` AND THE DATABASE HAS BEEN REFUSING IT ALL ALONG. The check
        # constraint allows ok, rejected, clarify and error, so every insert raised, and
        # `log_event` is wrapped below in a try that swallows anything rather than taking a
        # command down. The result: tier 1 has never once logged an escalation, silently, while
        # the row for a successful match wrote fine. The log looked healthy and was missing the
        # half that says when the model gets used and why.
        #
        # ⚠️ `rejected` IS THE HONEST FIT AMONG THE FOUR, and the tool name is what keeps it
        # unambiguous: `tier1_parse` + `rejected` is this tier declining, where `unsupported` +
        # `rejected` is the console declining to the operator. Adding `escalated` to the
        # constraint would be better and is a migration on two live databases, which is a
        # decision rather than a fix.
        result = "rejected"

    try:
        db.log_event(
            tool="tier1_parse",
            source=req.source,
            tier="parser",
            result=result,
            command_id=command_id,
            parent_command_id=req.parent_command_id,
            params={
                "utterance": req.utterance,
                "matched": matched,
                "grammar": sentence,
                "declined": trace.get("declined"),
                "extracted": trace.get("extracted") or {},
            },
            detail=story,
        )
    except Exception:  # noqa: BLE001 - a log row must never take a command down
        return

def _tier_two_blocked(
    *,
    client_ip: str | None,
    origin: str | None,
    utterance: str | None,
    source: str,
    command_id: str | None = None,
    parent_command_id: str | None = None,
) -> str | None:
    """Why the model must not be called right now, or None if it may be.

    🔑 ONE GUARD, TWO ENTRY POINTS. Tier 2 is reached either because the parser did not
    recognise an utterance at all, or because a parser plan turned out to name something
    that does not exist. Both cost the same money and both have to be metered the same
    way, and the surest way to end up with one of them unmetered is to write the check
    out twice.
    """
    if not ratelimit.origin_allowed(origin):
        # ⚠️ WORDED FOR A PERSON, because it reaches one. The check is a filter against a
        # page on somebody else's domain driving up a bill, and "not available from this
        # origin" is the machine describing its own configuration to a visitor who has no
        # idea what an origin is.
        return "The reasoning layer is not available from this page"
    verdict = ratelimit.check(client_ip)
    if not verdict.allowed:
        db.log_event(
            tool="rate_limited", source=source, result="rejected",
            command_id=command_id, parent_command_id=parent_command_id,
            params={"utterance": utterance, "scope": verdict.scope,
                    "used": verdict.used, "limit": verdict.limit},
            detail=verdict.reason,
        )
        return verdict.reason
    return None


class CommandRequest(BaseModel):
    """What the client sends for every command, typed or from a button.

    `context` is the deixis carrier: what the operator is currently looking at and has
    selected. Without it, "show me what is in the current window" and "focus this asset"
    are unanswerable, and both are things people say constantly.
    """

    # 🔒 BOUNDED BECAUSE IT ARRIVES FROM ANYONE AND IS SPENT ON A PAID CALL. Without a cap
    # a single request can carry a novel: it is pasted whole into the model call, stored
    # whole in the audit row, and rendered whole in the panel that is meant to be read.
    # A thousand characters is far past anything a person says to a console and far short
    # of anything worth sending. Pydantic refuses the rest with a 422 before any of it is
    # logged or paid for.
    utterance: str | None = Field(default=None, max_length=1000)
    # 🔑 WHAT THE MICROPHONE ACTUALLY CAUGHT, when it differs from what is being run. The
    # transcriber matches spoken names against the live map, so "Resolute Bay Patrol" can
    # arrive as "FLS Resolute Bay", and that substitution is a guess. Carrying the original
    # words means tier 2 can see the guess and disagree with it, which is exactly the sort
    # of judgement the expensive tier is there for. Absent on typed commands, where there
    # is nothing to disagree with.
    heard: str | None = Field(default=None, max_length=1000)

    @field_validator("utterance", "heard")
    @classmethod
    def _strip_invisibles(cls, value: str | None) -> str | None:
        """Remove characters that would misrepresent this text once it is rendered.

        🔴 THE AUDIT PANEL IS THE REASON. A right-to-left override inside a command makes
        the row display in a different order from the one it was stored in, so the record
        of what somebody asked would show something else. A log that can be made to lie
        about its own contents is worse than no log.

        ⚠️ ONLY THE INVISIBLE ONES. Every character an operator can actually see survives,
        including their punctuation and spelling, because the history is supposed to hold
        what they said rather than a cleaned-up version of it.
        """
        return None if value is None else plaintext.visible(value)
    # A button sends its plan directly rather than a sentence describing itself. Same
    # endpoint, same validator, same log, different `source`.
    plan: list[dict] | None = None
    source: str = "typed"
    context: dict | None = None
    parent_command_id: str | None = None


@app.post("/api/command")
def command(req: CommandRequest, request: Request) -> JSONResponse:
    """The one way anything acts on the world.

    Buttons and language arrive here identically. The tier that produced the plan is
    recorded rather than inferred, so "the model is only called when it earns its
    latency" is a query against the log rather than a claim in a README.
    """
    if req.source not in ("ui_button", "typed", "voice", "system"):
        raise HTTPException(400, "unknown command source")

    # ⚠️ x-forwarded-for FIRST, because on Vercel every request arrives from the platform's
    # own proxy and `request.client` is that proxy for everyone. Taking the leftmost entry
    # of the chain is the caller. It is spoofable by a determined client, which is fine:
    # the global daily cap is the guard that does not depend on trusting this value.
    forwarded = request.headers.get("x-forwarded-for", "")
    client_ip = forwarded.split(",")[0].strip() or (request.client.host if request.client else None)
    origin = request.headers.get("origin")

    # 🔑 ONE ID FOR THE WHOLE COMMAND, MINTED BEFORE ANYTHING IS LOGGED. Everything this
    # handler writes about this request, what tier 1 matched, what tier 2 was asked and what
    # it cost, the plan, every step, now carries it, so the audit log can show one command as
    # one story instead of as a scatter of rows that only a clock relates. The executor used
    # to mint its own, which meant the rows written before it ran could never join them.
    command_id = str(uuid.uuid4())

    tier: str | None = None
    plan = req.plan
    # A button posts a plan directly and no tier reasoned about it, so there is nothing
    # to show. Initialised here rather than inside the utterance branch, because a name
    # that exists on only one path is a NameError waiting for the other one.
    thinking: dict[str, Any] = {"tier": None}

    if plan is None:
        if not req.utterance:
            raise HTTPException(400, "a command needs either an utterance or a plan")

        refusal = parser.unsupported(req.utterance)
        if refusal:
            # Recognised in order to decline. Logged, because "what did someone ask for
            # that this cannot do" is the most useful thing a demo's audit trail holds.
            db.log_event(
                tool="unsupported",
                source=req.source,
                result="rejected",
                command_id=command_id,
                parent_command_id=req.parent_command_id,
                params={"utterance": req.utterance},
                detail=refusal,
            )
            return JSONResponse(
                {"ok": False, "summary": _dressed(refusal), "tier": "parser", "results": []},
                status_code=200,
                headers={"cache-control": "no-store"},
            )

        plan = parser.parse(req.utterance)
        tier = "parser"
        thinking = parser.trace(req.utterance, plan)

        # 🔑 TIER 1 SAYS WHAT IT DID, IN THE LOG, ON EVERY COMMAND. What each tier decided
        # used to exist only as a field on the response, so it was visible for one turn in
        # one browser and then gone. The log is the record, and "which tier answered and
        # why" is the single most interesting thing about a two-tier design: without it,
        # the claim that the model runs only when it earns its latency is a sentence in a
        # README rather than something anyone can check afterwards.
        _log_tier1(req, plan, thinking, command_id=command_id)

        # 🔴 THE PARTIAL-MATCH ESCALATION USED TO LIVE HERE, AND IT IS GONE BECAUSE THE
        # CONDITION IT TESTED CAN NO LONGER ARISE. Tier 1 was thirty branches, each matching
        # its keywords anywhere in the sentence, so a branch could answer part of an utterance
        # and drop the rest in silence:
        #
        #   "show me all unkown parties"          -> the ground parties
        #   "show me all unkown parties on foot"  -> the SAME ground parties
        #
        # This block caught that after the fact, by asking `trace` which words appeared nowhere
        # in the plan and escalating when any did, keeping the partial plan as a fallback.
        #
        # 🔑 THE GRAMMAR REMOVED THE FAILURE RATHER THAN THE SYMPTOM. Every sentence tier 1
        # accepts is now anchored to the WHOLE utterance, so a match accounts for every word by
        # construction and a non-match produces no plan at all. There is no half-understood
        # plan left to keep, and no dropped-word list left to test. `fallback` went with it:
        # what it protected was the case where tier 1 had *something* and the model was
        # unavailable, and tier 1 now either has the answer or has nothing.
        #
        # ⚠️ IT SURVIVED AS A VARIABLE THAT WAS ALWAYS None, AND THAT WAS NOT HARMLESS: three
        # branches below still read it, so the code went on describing a fallback this design
        # no longer has, and two of those branches could never run. Tier 2 now either answers
        # or says it could not be reached, which is the rule the dead variable was contradicting.

        if plan is None:
            # ---- TIER 2 ----------------------------------------------------
            # The parser did not recognise it, so it goes to the model. This is the only
            # path that costs money, which is the entire design: the common shapes are
            # answered deterministically and the model handles the language that actually
            # needs it.
            # ---- SPEND GUARD ----------------------------------------------
            # Metered here and nowhere else: everything above this line is free, so
            # someone trying the example commands can never be throttled part-way
            # through a first look.
            blocked = _tier_two_blocked(
                client_ip=client_ip, origin=origin, utterance=req.utterance,
                source=req.source, command_id=command_id,
                parent_command_id=req.parent_command_id,
            )
            # ⚠️ THE GUARD IS NOT A TIER-2 FAILURE AND KEEPS ITS OWN SENTENCE. A rate limit
            # or a page on somebody else's domain has a specific, true thing to say, and it
            # is already worded for a person. Replacing it with "unable to reach Claude
            # servers" would be a lie about a system that is working exactly as configured.
            if blocked:
                return JSONResponse(
                    {"ok": False, "summary": _dressed(blocked), "tier": "parser", "results": []},
                    status_code=200, headers={"cache-control": "no-store"},
                )

            selection = None
            try:
                selection = llmlib.default_provider().select(
                    req.utterance,
                    _model_context(req.context, heard=req.heard, running=req.utterance),
                )
            except llmlib.LLMUnavailable as exc:
                _log_tier2_failed(
                    req,
                    exc,
                    why="no deterministic parse",
                    command_id=command_id,
                    parent_command_id=req.parent_command_id,
                )
                # 🔴 THIS USED TO ANSWER AS TIER 1. The line said the reasoning layer was not
                # answering and then wore the parser's chip, which is the one tier that had
                # already declined this sentence. Tier 2 was asked, tier 2 failed, and the
                # response says so.
                return _tier_two_unreachable(
                    exc, handed_over=thinking, command_id=command_id
                )

            if selection is not None:
                plan = selection.to_plan()
                tier = "llm"
                # 🔴 THE PARSER'S TRACE SURVIVES THE ESCALATION, and it did not until now.
                # Overwriting `thinking` wholesale threw away the one thing that explains
                # WHY the model was called, and it did so in exactly the case the trace was
                # built for: a partial match. A mis-transcription heard as "Hydrophone
                # Lancaster Sound 01 overdue" escalated because tier 1 could not place
                # "lancaster", "sound" and "01" — and the response then showed the model's
                # reasoning with no hint that three of the operator's words had gone
                # unused. The reason for the call is more useful than the call.
                handed_over = thinking if thinking.get("tier") == "parser" else None
                thinking = {
                    "tier": "llm",
                    # 🔑 ALREADY COMPUTED AND PREVIOUSLY THROWN AWAY. The schema requires
                    # the model to say why, it was logged as `detail` for an audit reader,
                    # and the operator watching the screen never saw a word of it.
                    "reasoning": selection.reasoning,
                    "steps": len(plan),
                    "model": selection.usage.get("model"),
                    "latency_ms": selection.usage.get("latency_ms"),
                    "cost_usd": selection.usage.get("cost_usd"),
                }
                # 🔑 ATTACHED ON EVERY ESCALATION NOW, AND THAT CHANGED WITH THE GRAMMAR. It
                # used to be attached only when tier 1 had matched something or had words it
                # could not place, on the reasoning that a phrasing it never recognised at all
                # has no opinion worth showing and "ignored: nothing" would imply one.
                #
                # Tier 1 has an opinion on every utterance now: `declined` says whether the
                # sentence was near a declared command or nowhere close, which is the difference
                # between "say it the printed way and it is instant and free" and "this needed
                # the model". That is the most useful thing on the screen during a wait.
                if handed_over and (
                    handed_over.get("matched") or handed_over.get("declined")
                ):
                    thinking["parser"] = handed_over
                # 🔑 The model call is logged BEFORE the plan runs, with its own cost. That
                # ordering matters: a plan the validator then rejects still cost money, and an
                # audit log that only records successful calls cannot tell you what tier 2
                # spent.
                db.log_event(
                    tool="tier2_reason",
                    source=req.source,
                    tier="llm",
                    result="ok",
                    command_id=command_id,
                    parent_command_id=req.parent_command_id,
                    params={"utterance": req.utterance, "selection": plan, **selection.usage},
                    # 🔑 THE SAME VOICE AS TIER 1'S ROW. What it checked, what it found, and
                    # what it decided to do about it, so the two rows read as one story
                    # rather than as two different systems keeping separate books.
                    detail=(
                        f"checked {len(toollib.REGISTRY)} available tools, "
                        + (
                            f"selected {', '.join(str(st.get('tool')) for st in plan)}"
                            if plan
                            else "no tool fits this request, answering directly"
                        )
                        + f". {selection.reasoning}"
                    ),
                    latency_ms=selection.usage.get("latency_ms"),
                )

            if not plan:
                # 🔑 NO STEPS IS NOT THE SAME AS NO ANSWER, and conflating them made the
                # console reply to "what does overdue mean" with "I understood that as no
                # action" followed by the answer, marked as a failure. A question about what
                # something on the display MEANS has nothing to run and is still answered;
                # only a genuinely empty reply is a failure.
                #
                # ⚠️ SHOWABLE, NOT MERELY PRESENT. An answer has to be the shape of an
                # answer before it is printed as one; see `_showable`. What fails that
                # test is still in the audit row above, logged before this line runs.
                said = _showable(selection.reasoning) if selection is not None else None
                return JSONResponse(
                    {
                        "ok": bool(said),
                        "summary": said or _dressed("that one is outside what this display covers"),
                        "tier": tier,
                        "results": [],
                        "thinking": thinking,
                    },
                    status_code=200,
                    headers={"cache-control": "no-store"},
                )

    try:
        outcome = executor.execute(
            plan,
            source=req.source,
            tier=tier,
            command_id=command_id,
            utterance=req.utterance,
            parent_command_id=req.parent_command_id,
            # The deixis carrier finally reaches the executor. It was collected by the
            # client and forwarded to the model from the start, but nothing downstream
            # could act on it, so "this asset" and "the current window" were words the
            # system understood and could not answer.
            context=req.context,
        )
    except executor.PlanRejected as exc:
        # 🔴 A REJECTED TIER-1 PLAN IS STILL TIER 1 BEING UNSURE, so it goes to the model
        # rather than to the operator. Nothing ran, so there is nothing to undo, and the
        # component allowed to be uncertain has not seen the utterance yet.
        if tier == "parser" and req.utterance:
            # 🔑 THE REJECTION'S OWN ID IS THE PARENT. This used to pass None, so the
            # model's retry was logged as an orphan: the audit showed a refused plan and,
            # separately, an answer with nothing connecting the two. The executor logs the
            # rejection under this id and re-raises carrying it.
            escalated = _escalate_to_tier_two(
                req,
                {"command_id": exc.command_id or command_id},
                client_ip=client_ip,
                origin=origin,
            )
            if escalated is not None:
                return escalated
        # The direct tier-2 path rejects here rather than in the escalation helper, and it
        # owes the operator the same account: what the model was about to do, then why it
        # was refused. Tier 1 gets no lead, as everywhere else.
        rejected: dict[str, Any] = {
            "ok": False,
            "summary": _dressed("; ".join(exc.reasons)),
            "tier": tier,
            "results": [],
        }
        if tier == "llm" and selection is not None:
            _lead_in(rejected, selection.reasoning, even_when_refused=True)
        return JSONResponse(
            rejected,
            status_code=200,
            headers={"cache-control": "no-store"},
        )
    except RuntimeError as exc:
        raise HTTPException(503, _DB_UNAVAILABLE) from exc

    # 🔴 A TIER-1 GUESS THAT RESOLVED TO NOTHING GOES TO THE MODEL, NOT TO THE OPERATOR.
    #
    # The parser's own contract is that it returns None when it does not know and never
    # guesses, "because a parser that half-matches steals the utterances the model should
    # have had". A plan whose referent matches nothing is that theft, discovered one step
    # too late: the utterance was not understood, and the component allowed to be
    # uncertain never saw it. So the original words are re-run through tier 2 instead of
    # showing a dead end.
    #
    # ⚠️ IT IS NOT ABOUT TYPOS, though a typo is what exposed it. The same escalation
    # covers nicknames, partial names, an operator's shorthand and any phrasing nobody
    # anticipated. And it makes the architecture's central claim stronger rather than
    # weaker: "the parser was unsure" becomes a recorded, costed reason for a model call
    # instead of an invisible wrong answer.
    #
    # 🔒 ONCE, AND ONLY FROM THE PARSER. `tier == "parser"` is the loop guard: a plan the
    # model produced can never come back here to be sent to the model again.
    #
    # 🔴 IT USED TO ALSO REQUIRE AN ABSENT `parent_command_id`, AND THAT SILENTLY DISABLED
    # TIER 2 FOR EVERY SPOKEN COMMAND. The intent was to stop an escalation escalating, but
    # nothing that reaches this line has ever been one: an escalation runs inside this
    # process and never re-enters the endpoint, and a clarification chip posts a PLAN, which
    # skips the whole parser branch. The only request that arrives carrying both an
    # utterance and a parent is a transcription's command, because voice deliberately hangs
    # the spoken command off the row that turned audio into words.
    #
    # So the condition matched voice and nothing else. Spoken "show detected" was refused by
    # the parser and never offered to the tier that could answer it, while the identical
    # typed sentence worked. Two behaviours for one utterance, decided by how it was said.
    # 🔑 ANY TIER-1 REFUSAL, NOT JUST AN UNRESOLVED NAME. This used to fire only when a
    # referent matched nothing, so "what is this map about" parsed as a describe, failed, and
    # showed the operator "nothing here matches this map about" from a system that could have
    # answered the question. Every way tier 1 can be unsure is the same situation: it
    # half-matched, and the tier allowed to be uncertain has not been asked yet.
    #
    # 🔒 UNLESS SOMETHING ALREADY HAPPENED. The executor is fail-fast and not transactional,
    # so a plan that placed an asset and then failed has already committed that write.
    # Re-running the utterance through the model would place a second one. Escalation is for
    # a refusal that changed nothing, which is what every read-only failure is.
    nothing_ran = not any(r.get("ok") for r in outcome.get("results", []))
    def _is_write(step: dict) -> bool:
        spec = toollib.REGISTRY.get(str(step.get("tool")))
        return bool(spec and spec.writes)

    wrote = any(_is_write(r) for r in outcome.get("results", []))
    # 🔴 A CLARIFICATION IS NOT UNCERTAINTY, IT IS A PRECISE QUESTION, and treating it as
    # the former quietly deleted the feature. A clarify comes back with every step marked not
    # ok and nothing run, which is exactly the shape of "tier 1 was unsure", so it escalated:
    # the model then answered the ambiguous phrase with a LIST, the ready-to-run chips were
    # thrown away, and a model call was spent replacing a better answer with a vaguer one.
    #
    # Asked to tell it about "daymark", the console should ask which of the five, with a
    # button per candidate. Measured across the whole probe set, it never once did: every
    # ambiguous phrasing came back as a list.
    #
    # 🔑 THE TEST IS WHETHER A QUESTION WAS ASKED, not whether the steps succeeded. Tier 1
    # resolved the phrase to several real assets and is asking which one was meant, which is
    # the opposite of not understanding.
    asked_a_question = "clarify" in (outcome.get("ui_effects") or {})
    tier_one_unsure = not asked_a_question and (
        outcome.get("unresolved")
        or (
            not all(r.get("ok") for r in outcome.get("results", []))
            and nothing_ran
            and not wrote
        )
    )
    if tier_one_unsure and tier == "parser" and req.utterance:
        escalated = _escalate_to_tier_two(req, outcome, client_ip=client_ip, origin=origin)
        if escalated is not None:
            return escalated

    outcome["ok"] = all(r["ok"] for r in outcome["results"])
    outcome["tier"] = tier

    # 🔴 REACHING HERE UNRESOLVED MEANS THE ESCALATION ABOVE COULD NOT RUN, so the operator
    # is about to be shown tier 1's "nothing here matches that name" after all. That is the
    # sentence this whole escalation path exists to keep off the screen: it describes the
    # parser's disappointment rather than anything about the Arctic. The reason is already
    # in the audit log, written verbatim by the tool that raised it.
    #
    # ⚠️ ONLY THE UNRESOLVED CASE. A tool that declined because a hydrophone cannot go
    # inland, or because a name matched five assets, said something specific and useful, and
    # dressing those up would replace the best messages this system produces with a
    # generic one.
    if outcome.get("unresolved") and not outcome["ok"]:
        outcome["summary"] = _dressed("I could not find what that referred to")

    # The direct tier-2 path: the parser had nothing, the model planned, the plan ran. Its
    # own sentence introduces the result. Tier 1 gets no lead, deliberately: it answers
    # instantly and confidently, and narrating a command that needed no interpretation
    # would be ceremony.
    if tier == "llm" and selection is not None:
        _lead_in(outcome, selection.reasoning)

    # 🔑 WHAT THE SYSTEM WAS THINKING, FOR WHICHEVER TIER DID IT. Tier 2 hands back the
    # reasoning it was already required to produce; tier 1 hands back what it matched and,
    # more usefully, which of the operator's words it did not use. Giving a person
    # something to read during a wait is the small reason. The real one is that a dropped
    # word is invisible in an answer and obvious in a trace.
    outcome["thinking"] = thinking

    # 🔑 TEACH THE FAST PATH AT THE MOMENT IT WOULD HAVE HELPED. The model just reached a
    # tool the deterministic tier can reach for nothing, so the operator is shown the
    # sentence that gets there directly. It is the same list the reference card is built
    # from, which is the point: one canonical phrasing per tool, learned either by reading
    # the card before or by being shown it after.
    #
    # ⚠️ ONLY AFTER TIER 2, because after tier 1 there is nothing to teach: the operator
    # already said something the parser answered.
    #
    # ⚠️ AND IT IS NOT A TRANSLATION OF WHAT THEY ASKED. "Which hydrophones have gone
    # quiet" is not the same question as "show the hydrophones"; the tool is the same. So
    # the wording says the tool is reachable without the model rather than claiming the two
    # sentences are equivalent, because they are not and an operator would find that out.
    if tier == "llm":
        outcome["teach"] = _teach(outcome.get("plan"))

    # `plan` is set by the executor and is the RESOLVED one; do not overwrite it with the
    # pre-resolution copy this function still holds.
    return JSONResponse(outcome, headers={"cache-control": "no-store"})


def _teach(plan: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """The deterministic phrasing for every tool a model plan reached.

    🔑 TEACHES THE FAST PATH AT THE MOMENT IT WOULD HAVE HELPED. The model just used a tool
    the parser can reach for nothing, so the operator is shown the sentence that gets there
    directly. It is the same one-per-tool list the reference card is built from, so reading
    it beforehand and being shown it afterwards teach the same words.

    ⚠️ NOT A TRANSLATION OF THE QUESTION. "Which hydrophones have gone quiet" is not the
    same question as "show the hydrophones"; only the tool is the same. The caller words it
    as "reachable without the model" rather than "next time say this", because an operator
    who tried the substitution and got a different answer would stop believing the line.

    ⚠️ AND IT LIVES HERE BECAUSE THERE ARE TWO TIER-2 PATHS. The direct one and the
    escalation have drifted apart before, and a capability that exists on one of them is a
    capability that works depending on how the question was phrased.
    """
    # 🔑 THE VALUES FROM THIS ANSWER, NOT THE CARD'S EXAMPLE. `phrase_for` fills the declared
    # sentence from the plan the model actually produced, so asking about FLS Resolute Bay is
    # answered with "tell me about FLS Resolute Bay" rather than the card's Daymark. The card
    # teaches the shape before; this teaches the sentence they could have said.
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for step in plan or []:
        name = step.get("tool")
        if not name or name in seen or name not in toollib.REGISTRY:
            continue
        seen.add(name)
        say = grammar.phrase_for(name, step.get("params") or {})
        if say:
            out.append({"tool": name, "say": say})
    return out


def _escalate_to_tier_two(
    req: CommandRequest, first: dict, *, client_ip: str | None, origin: str | None
) -> JSONResponse | None:
    """Re-run the utterance through the model. None means "keep the original refusal".

    🔴 A FAILED CALL NO LONGER RETURNS None, AND THAT IS THE CORRECTION. This used to treat
    every failure as "keep tier 1's sentence", on the reasoning that the operator already had
    a true, readable answer and an optimisation that can only improve things is one nobody
    has to reason about at 3am. The reasoning was wrong about which sentence they had. Tier 1
    got here by naming something that does not exist, so its refusal is not a true answer to
    the question, it is the dead end this whole path exists to keep off the screen. Handing it
    back dressed as the reply meant a model outage looked like the parser confidently saying
    no. Tier 2 either answers or says it could not be reached.

    ⚠️ None STILL MEANS "KEEP THE ORIGINAL REFUSAL", for the cases where the model was never
    the problem: no utterance to re-run, an answer with nothing sayable in it, or a database
    failure the caller reports its own way.

    🔗 THE WHOLE CHAIN IS LOGGED UNDER THE FIRST COMMAND'S ID, so the audit trail reads
    guess, escalation, answer. That is the same `parent_command_id` the clarification round
    trip uses, and it is what the column exists for.
    """
    # ⚠️ CHECKED HERE AND NOT ONLY IN THE CALLER. There is nothing to re-run without the
    # original words, and a guard that lives only at one call site is one refactor away
    # from not existing.
    if not req.utterance:
        return None

    parent = first.get("command_id")

    # ⚠️ A BLOCKED CALL ANSWERS TOO, AND FOR THE SAME REASON A FAILED ONE DOES. From the
    # operator's seat there is no difference between a model that could not be reached and one
    # that was not allowed to run: either way tier 2 did not answer, and falling through here
    # put the parser's dead end on screen. It keeps the guard's own wording rather than the
    # sentence above, because a rate limit is a true and specific thing to say.
    blocked = _tier_two_blocked(
        client_ip=client_ip, origin=origin, utterance=req.utterance,
        source=req.source, command_id=parent,
    )
    if blocked:
        return JSONResponse(
            {
                "ok": False,
                "command_id": parent,
                "summary": _dressed(blocked),
                "tier": "parser",
                "results": [],
            },
            status_code=200,
            headers={"cache-control": "no-store"},
        )

    # 🔑 THE ESCALATION TELLS THE MODEL WHAT THE PARSER LEARNED THE HARD WAY, and without
    # this it is simply the same call twice. The system prompt instructs the model to pass
    # a named asset through verbatim and let the system resolve it, which is right on a
    # first attempt and exactly wrong here: passing "daymark 3" through again reproduces
    # the failure that caused the escalation. Measured, that is precisely what happened.
    #
    # So the retry carries the name that did not match and every name that does. The model
    # is not being asked to spell-check; it is being given the world it is choosing from.
    ref = first.get("unresolved_ref") or {}
    context = dict(req.context or {})
    if ref.get("query"):
        context["unresolved_reference"] = ref["query"]
        context["known_asset_names"] = ref.get("available", [])

    try:
        selection = llmlib.default_provider().select(
            req.utterance, _model_context(context, heard=req.heard, running=req.utterance)
        )
    except llmlib.LLMUnavailable as exc:
        _log_tier2_failed(
            req,
            exc,
            why="tier 1 named something that does not exist",
            command_id=parent,
            parent_command_id=parent,
        )
        # 🔴 THE PARSER'S REFUSAL IS NOT THE ANSWER TO FALL BACK TO. It is the reason this
        # call was made: tier 1 resolved the operator's words to something that does not
        # exist. Showing it after a failed model call tells them their words matched nothing,
        # when what happened is that the tier which might have understood them was down.
        return _tier_two_unreachable(exc, command_id=parent)

    plan = selection.to_plan()
    db.log_event(
        tool="tier2_reason", source=req.source, tier="llm", result="ok",
        command_id=parent,
        parent_command_id=parent,
        # `escalated_from` is what makes this call answerable in the log: it separates
        # "the parser had no idea" from "the parser guessed and was wrong", which are
        # different stories about the same money.
        params={"utterance": req.utterance, "selection": plan,
                "escalated_from": "parser", **selection.usage},
        detail=(
            f"tier 1 escalated. Checked {len(toollib.REGISTRY)} available tools, "
            + (
                f"selected {', '.join(str(st.get('tool')) for st in plan)}"
                if plan
                else "no tool fits this request, answering directly"
            )
            + f". {selection.reasoning}"
        ),
        latency_ms=selection.usage.get("latency_ms"),
    )
    if not plan:
        # 🔴 A MODEL ANSWER WITH NO STEPS USED TO BE THROWN AWAY, and the operator was shown
        # the parser's refusal instead. Asked "what is a backhaul", the console replied
        # "nothing here matches a backhaul", which reads as though it had misheard the
        # question rather than understood it and had nothing to run. The model had in fact
        # answered; this path discarded it because it was looking for a plan.
        #
        # ⚠️ ONLY WHEN THERE IS SOMETHING TO SAY. An empty reasoning is not an answer, and
        # falling through to the original refusal is still right for that case: "the thing
        # you named does not exist" beats a blank reply.
        #
        # ⚠️ AND ONLY WHEN IT IS THE SHAPE OF AN ANSWER. Returning None here falls through
        # to tier 1's refusal, which is the wrong sentence for a coaxed reply: the operator
        # asked something this display does not cover and should be told that, not told
        # their words matched nothing. So the unshowable case answers with the boundary.
        said = _showable(selection.reasoning)
        if not selection.reasoning.strip():
            return None
        answered: dict[str, Any] = {
            "ok": True,
            "command_id": parent,
            "summary": said or _dressed("that one is outside what this display covers"),
            "results": [],
            "ui_effects": {},
            "tier": "llm",
            "escalated_from": "parser",
            "thinking": {
                "tier": "llm",
                "reasoning": selection.reasoning,
                "steps": 0,
                **{k: v for k, v in selection.usage.items() if k in ("model", "latency_ms", "cost_usd")},
            },
        }
        return JSONResponse(answered, headers={"cache-control": "no-store"})

    try:
        outcome = executor.execute(
            plan, source=req.source, tier="llm", utterance=req.utterance,
            parent_command_id=parent, context=req.context,
        )
    except executor.PlanRejected as exc:
        # 🔴 TIER 2's OWN REJECTION USED TO FALL BACK TO TIER 1's REFUSAL, and that is the
        # wrong sentence from the wrong component. Returning None hands the operator the
        # parser's message about words it could not place, when what actually happened is
        # that the model understood the request, proposed an action, and the action was
        # refused for a stated reason. Showing the parser instead hides both the
        # interpretation and the real reason.
        #
        # 🔑 SO IT ANSWERS AS TIER 2 DID: its own line saying what it was about to do,
        # then the reason the attempt was refused. The reasons are written for an operator
        # now, so this is worth showing rather than hiding.
        refused: dict[str, Any] = {
            "ok": False,
            "command_id": parent,
            "summary": _dressed("; ".join(exc.reasons)),
            "results": [],
            "tier": "llm",
            "escalated_from": "parser",
            "thinking": {"tier": "llm", "reasoning": selection.reasoning, "steps": len(plan)},
        }
        _lead_in(refused, selection.reasoning, even_when_refused=True)
        return JSONResponse(refused, headers={"cache-control": "no-store"})
    except RuntimeError:
        return None

    outcome["ok"] = all(r["ok"] for r in outcome["results"])
    outcome["tier"] = "llm"
    outcome["escalated_from"] = "parser"
    # The escalated path needs the lead more than the direct one does: this is exactly the
    # case where tier 1 had already given up, so without it the answer arrives with no sign
    # that anything understood the question.
    _lead_in(outcome, selection.reasoning)
    outcome["teach"] = _teach(outcome.get("plan"))
    return JSONResponse(outcome, headers={"cache-control": "no-store"})


@app.post("/api/transcribe")
async def transcribe_audio(request: Request) -> JSONResponse:
    """Audio in, one line of text out. The text then goes to /api/command like any other.

    🔑 METERED BY THE SAME GUARD AS TIER 2, and for the same reason: this is the other
    path that costs money per call. The deterministic command path stays free, so nothing
    a visitor does with the keyboard can be throttled.

    ⚠️ Takes a raw body rather than multipart. The browser has the blob already and
    multipart would mean a parser dependency for one field.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    client_ip = forwarded.split(",")[0].strip() or (request.client.host if request.client else None)

    if not ratelimit.origin_allowed(request.headers.get("origin")):
        return JSONResponse(
            {"ok": False, "detail": "voice is not available from this origin"},
            status_code=200, headers={"cache-control": "no-store"},
        )
    verdict = ratelimit.check(client_ip)
    if not verdict.allowed:
        db.log_event(
            tool="rate_limited", source="voice", result="rejected",
            params={"scope": verdict.scope, "used": verdict.used, "limit": verdict.limit},
            detail=verdict.reason,
        )
        return JSONResponse(
            {"ok": False, "detail": verdict.reason},
            status_code=200, headers={"cache-control": "no-store"},
        )

    audio = await request.body()
    try:
        out = transcribe.transcribe(audio, request.headers.get("content-type"))
    except transcribe.TranscriptionError as exc:
        # An honest "voice is unavailable" rather than a 500. Typing still works, and the
        # attempt is logged because "the microphone did not work" is worth being able to
        # see afterwards.
        db.log_event(
            tool="transcribe", source="voice", result="rejected",
            params={"audio_bytes": len(audio)}, detail=str(exc)[:400],
        )
        # `silent` separates "you said nothing" from "voice is broken". The display shows
        # the second and says nothing at all about the first; see `transcribe.NoSpeech`.
        return JSONResponse(
            {"ok": False, "detail": str(exc), "silent": isinstance(exc, transcribe.NoSpeech)},
            status_code=200, headers={"cache-control": "no-store"},
        )

    # 🔑 THE TRANSCRIPTION GETS A COMMAND ID AND HANDS IT BACK, which is what makes a
    # spoken command one thread in the log instead of two unrelated rows. Without it the
    # audit trail holds "some audio became these words" and, separately, "this command
    # ran", with nothing joining them: you could not ask what a person actually SAID to
    # cause a given action, which is the first question anyone asks of a voice interface
    # when it does something surprising.
    #
    # The client returns it as `parent_command_id` on the command that follows, so the
    # chain reads transcription, then plan, then steps, under one parent. It is the same
    # mechanism a clarification uses, and deliberately so: both are cases where one
    # interaction spans more than one request.
    command_id = str(uuid.uuid4())
    # 🔑 NO TIER, AND THE COLUMN'S OWN DEFINITION IS THE ARGUMENT. `db/schema.sql` calls it
    # "which tier produced the plan"; transcription produces no plan. It turns audio into a
    # sentence and stops, so the honest value is null.
    #
    # 🔴 IT WAS `llm` AND THAT BROKE THE ONE QUESTION THE COLUMN EXISTS FOR. Every spoken
    # command looked as though it had reached the reasoning tier, so "the model is only
    # called when it earns its latency" stopped being answerable here, and the badge chain
    # read `llm → parser` for a command tier 1 had answered by itself.
    #
    # ⚠️ AND IT WAS BRIEFLY `transcribe`, WHICH THE DATABASE REFUSED. `events_tier_check`
    # allows null, 'parser' or 'llm' and nothing else, so every spoken command 500'd. The
    # suite could not see it: those tests run on fixtures with no database, so a constraint
    # is not in the room. That the fix is null rather than a wider constraint is lucky
    # rather than clever, but it is also the right answer on the merits.
    #
    # The model that ran is not lost: `tool` says `transcribe`, and `params` carries the
    # model name, the token counts and the latency.
    db.log_event(
        tool="transcribe", source="voice", result="ok",
        command_id=command_id,
        params={k: v for k, v in out.items() if k != "text"},
        detail=out["text"],
        latency_ms=out["latency_ms"],
    )
    # 🔑 BOTH STRINGS TRAVEL, AND THE DISPLAY DECIDES WHICH IS WHICH. `heard` is what the
    # microphone actually caught and is what the operator reads back; `text` is the version
    # with names matched to the map and is what gets run. They are usually identical. When
    # they are not, the client says so out loud rather than quietly showing the corrected
    # sentence as though it were what was said.
    return JSONResponse(
        {
            "ok": True,
            "text": out["text"],
            "heard": out.get("heard") or out["text"],
            "command_id": command_id,
        },
        headers={"cache-control": "no-store"},
    )


@app.get("/api/tools")
def tools_list() -> JSONResponse:
    """The registry as data.

    One source for the model's prompt, the UI's button list and the command reference the
    console shows an operator, so a tool that exists is reachable from all three and a tool
    that is removed disappears from all three at once.

    `reference` is the same registry grouped by operator intent rather than by tool name,
    because `set_visible_kinds` is a schema word and "hide the radars" is what a person
    says. Every phrasing in it is exercised against the parser by the suite.
    """
    return JSONResponse({"tools": toollib.schemas(), "reference": toollib.reference()})


@app.get("/api/schema")
def asset_schema() -> JSONResponse:
    """Every field an asset can carry, declared once and served to whoever draws it.

    🔑 THE PANEL'S SHAPE IS NOW SERVER-OWNED. It used to be a hand-written row list inside a
    React component, so the operator-facing shape of an asset lived somewhere the server
    could not see and nothing could check. Same argument as the command reference: a second
    list maintained by hand is a list that drifts.

    Static for the life of the process, so it is the one response here that may be cached.
    """
    return JSONResponse(
        {"fields": fields.schema(), "kinds": sorted(domain.KINDS)},
        headers={"cache-control": "public, max-age=300"},
    )


@app.get("/api/events")
def events(
    since_id: int = Query(0, ge=0, description="Return only events newer than this id."),
    limit: int = Query(200, ge=1, le=1000),
    entity_id: str | None = None,
    command_id: str | None = None,
) -> JSONResponse:
    """The audit log, oldest first, optionally filtered.

    `since_id` is a cursor rather than a timestamp because a client polling for new
    rows is asking "what have I not seen", which is an id question. ⚠️ Ids are
    assigned in request order but can COMMIT out of order, so a client that must not
    miss a row should overlap its cursor rather than trust strict monotonicity.
    """
    try:
        rows = db.fetch_events(
            since_id=since_id, limit=limit, entity_id=entity_id, command_id=command_id
        )
    except RuntimeError as exc:
        raise HTTPException(503, _DB_UNAVAILABLE) from exc
    max_id = rows[-1]["id"] if rows else since_id
    return JSONResponse(
        {"events": rows, "count": len(rows), "max_id": max_id},
        headers={"cache-control": "no-store"},
    )


@app.get("/api/world")
def world() -> JSONResponse:
    """The idle clock, phrased for the thing on screen that has to explain it.

    🔑 IT RUNS THE IDLE CHECK, and that is deliberate rather than a leak. There is no
    scheduler here, so the reset can only ever be NOTICED by a request that was happening
    anyway. If the countdown polled something inert it would reach zero and sit there while
    the world stayed stale, waiting for somebody else to arrive, and a timer that visibly
    lies at zero is worse than no timer. Polling this is what makes the number true.

    ⚠️ THE POLL ITSELF IS NOT ACTIVITY. It refreshes nothing, which is the whole reason an
    abandoned tab cannot hold the world open. See `lifecycle.mark_activity` for the other
    half.
    """
    with db.connect() as conn, conn.cursor() as cur:
        lifecycle.reset_if_idle(cur=cur)
        conn.commit()
    return JSONResponse(lifecycle.status(), headers={"cache-control": "no-store"})


@app.post("/api/world/touch")
def world_touch() -> JSONResponse:
    """Somebody is deliberately using the display. Hold the reset off.

    🔑 A SEPARATE VERB FROM THE POLL ABOVE BECAUSE THEY MEAN OPPOSITE THINGS. A GET says
    "what is the state", and answering it must not change the state or one open tab keeps
    the world alive forever. A POST here says "a person did something", which is exactly the
    signal the idle window is supposed to measure and the one thing traffic alone cannot
    tell you.

    The client sends this only for deliberate acts: panning, zooming, selecting, toggling a
    layer. Not for its own five-second refresh.
    """
    lifecycle.mark_activity()
    return JSONResponse(lifecycle.status(), headers={"cache-control": "no-store"})


@app.post("/api/reset")
def reset_world() -> JSONResponse:
    """Put the seeded world back, because a viewer asked for it.

    🔒 OPEN TO EVERYONE, WHICH IS A DECISION AND NOT AN OVERSIGHT. Gating this to the
    owner's address would hide it from precisely the person it exists for: a stranger who
    has arrived to a world somebody else left in a mess. The protections are that it says
    what it will do before it does it, and that `lifecycle.reset_now` will not run it more
    than once a minute.

    ⚠️ 429 RATHER THAN A SILENT NO. The caller is a person watching a button, so the answer
    has to be something the interface can say out loud, which means the seconds remaining
    rather than a bare failure.
    """
    result = lifecycle.reset_now()
    if not result.get("ok"):
        retry = int(result.get("retry_after_s", 0))
        return JSONResponse(
            {"ok": False, "retry_after_s": retry,
             "detail": f"the world was reset moments ago; {retry}s before it can be reset again"},
            status_code=429,
            headers={"cache-control": "no-store", "retry-after": str(retry)},
        )
    return JSONResponse(result, headers={"cache-control": "no-store"})
