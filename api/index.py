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

import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ._lib import db

# 🔒 OPTIONAL BY CONSTRUCTION. The detection layer decorates an answer rather than
# producing one, so its absence must cost a field and never a route. Imported through a
# try so this file loads even mid-edit on a shared tree.
try:
    from ._lib import detect
except Exception:  # noqa: BLE001
    detect = None  # type: ignore[assignment]

from ._lib import executor, parser, ratelimit, transcribe
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
    """
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
        raise HTTPException(503, str(exc)) from exc

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
        reported = [d for d in found if d.get("reported")]
        announcing = bool(self_reporting(row)) if self_reporting else False
        row["held"] = len(found)
        row["tracked"] = announcing or bool(reported)


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
        raise HTTPException(503, str(exc)) from exc

    payload = meshlib.mesh_status(rows)
    payload["model"] = _mesh_model()
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


def _model_context(context: dict[str, Any] | None) -> dict[str, Any] | None:
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
    if not context:
        return context
    trimmed = dict(context)
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


def _tier_two_blocked(
    *, client_ip: str | None, origin: str | None, utterance: str | None, source: str
) -> str | None:
    """Why the model must not be called right now, or None if it may be.

    🔑 ONE GUARD, TWO ENTRY POINTS. Tier 2 is reached either because the parser did not
    recognise an utterance at all, or because a parser plan turned out to name something
    that does not exist. Both cost the same money and both have to be metered the same
    way, and the surest way to end up with one of them unmetered is to write the check
    out twice.
    """
    if not ratelimit.origin_allowed(origin):
        return "the reasoning layer is not available from this origin"
    verdict = ratelimit.check(client_ip)
    if not verdict.allowed:
        db.log_event(
            tool="rate_limited", source=source, result="rejected",
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

    utterance: str | None = None
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
                params={"utterance": req.utterance},
                detail=refusal,
            )
            return JSONResponse(
                {"ok": False, "summary": refusal, "tier": "parser", "results": []},
                status_code=200,
                headers={"cache-control": "no-store"},
            )

        plan = parser.parse(req.utterance)
        tier = "parser"
        thinking = parser.trace(req.utterance, plan)

        # 🔴 A PARTIAL MATCH IS NOT AN ANSWER, and this is where it stops being presented as
        # one. Every tier-1 branch matches on part of an utterance and returns on the first
        # hit, so words outside that part are dropped in silence:
        #
        #   "show me all unkown parties"          -> the ground parties
        #   "show me all unkown parties on foot"  -> the SAME ground parties
        #
        # Two different questions, one byte-identical answer, nothing anywhere admitting
        # that "unkown" and "on foot" were thrown away. The parser's own contract says it
        # declines rather than half-matching, and `trace` is what finally makes a half
        # match visible enough to act on.
        #
        # ⚠️ THE PARSER PLAN IS KEPT AS A FALLBACK, NOT DISCARDED. If the model is rate
        # limited or unavailable, a partial answer WITH its dropped words named is better
        # than no answer at all, and the trace travels with it either way.
        fallback: list[dict] | None = None
        if plan is not None and thinking["ignored"]:
            fallback = plan
            plan = None

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
                client_ip=client_ip, origin=origin, utterance=req.utterance, source=req.source
            )
            if blocked and fallback is None:
                return JSONResponse(
                    {"ok": False, "summary": blocked, "tier": "parser", "results": []},
                    status_code=200, headers={"cache-control": "no-store"},
                )

            selection = None
            try:
                # A spend guard that fired is the same situation as a model that will not
                # answer: tier 2 is not available for this utterance. Funnelling both into
                # one path is what stops the fallback below existing in two versions.
                if blocked:
                    raise llmlib.LLMUnavailable(blocked)
                selection = llmlib.default_provider().select(req.utterance, _model_context(req.context))
            except llmlib.LLMUnavailable as exc:
                db.log_event(
                    tool="unparsed",
                    source=req.source,
                    result="rejected",
                    params={"utterance": req.utterance},
                    detail=f"no deterministic parse; tier 2 unavailable: {exc}",
                )
                if fallback is None:
                    return JSONResponse(
                        {
                            "ok": False,
                            "summary": (
                                "I could not parse that, and the reasoning layer is unavailable "
                                f"({exc}). Try: \"mesh status\", \"show me the drones\", "
                                "\"what is not broadcasting\", or \"send Daymark 05 to 73.0 -95.9\""
                            ),
                            "tier": "parser",
                            "results": [],
                            "thinking": thinking,
                        },
                        status_code=200,
                        headers={"cache-control": "no-store"},
                    )
                # ⚠️ THE PARTIAL MATCH IS BETTER THAN NOTHING, BUT ONLY BECAUSE IT ARRIVES
                # LABELLED. `thinking.ignored` names the words that did not land and
                # `degraded` says why the better answer was not available, so the operator
                # is looking at an answer that admits what it is.
                plan, tier = fallback, "parser"
                thinking["degraded"] = f"tier 2 unavailable: {exc}"

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
                # Only when tier 1 actually looked and came up short. A phrasing it never
                # recognised at all has an empty trace, and showing "ignored: nothing" would
                # imply it had an opinion it does not have.
                if handed_over and (handed_over.get("ignored") or handed_over.get("matched")):
                    thinking["parser"] = handed_over
                # 🔑 The model call is logged BEFORE the plan runs, with its own cost. That
                # ordering matters: a plan the validator then rejects still cost money, and an
                # audit log that only records successful calls cannot tell you what tier 2
                # spent.
                db.log_event(
                    tool="llm_select",
                    source=req.source,
                    tier="llm",
                    result="ok",
                    params={"utterance": req.utterance, "selection": plan, **selection.usage},
                    detail=selection.reasoning,
                    latency_ms=selection.usage.get("latency_ms"),
                )

            if not plan:
                return JSONResponse(
                    {
                        "ok": False,
                        "summary": (
                            f"I understood that as no action. {selection.reasoning}"
                            if selection is not None
                            else "I could not turn that into a command."
                        ),
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
            utterance=req.utterance,
            parent_command_id=req.parent_command_id,
            # The deixis carrier finally reaches the executor. It was collected by the
            # client and forwarded to the model from the start, but nothing downstream
            # could act on it, so "this asset" and "the current window" were words the
            # system understood and could not answer.
            context=req.context,
        )
    except executor.PlanRejected as exc:
        return JSONResponse(
            {"ok": False, "summary": "; ".join(exc.reasons), "tier": tier, "results": []},
            status_code=200,
            headers={"cache-control": "no-store"},
        )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc

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
    # 🔒 ONCE, AND ONLY FROM THE PARSER. `tier == "parser"` stops a model plan bouncing
    # back to the model, and the absent `parent_command_id` stops an escalation escalating.
    # Those two conditions are the loop guard.
    if outcome.get("unresolved") and tier == "parser" and req.utterance and not req.parent_command_id:
        escalated = _escalate_to_tier_two(req, outcome, client_ip=client_ip, origin=origin)
        if escalated is not None:
            return escalated

    outcome["ok"] = all(r["ok"] for r in outcome["results"])
    outcome["tier"] = tier
    # 🔑 WHAT THE SYSTEM WAS THINKING, FOR WHICHEVER TIER DID IT. Tier 2 hands back the
    # reasoning it was already required to produce; tier 1 hands back what it matched and,
    # more usefully, which of the operator's words it did not use. Giving a person
    # something to read during a wait is the small reason. The real one is that a dropped
    # word is invisible in an answer and obvious in a trace.
    outcome["thinking"] = thinking
    # `plan` is set by the executor and is the RESOLVED one; do not overwrite it with the
    # pre-resolution copy this function still holds.
    return JSONResponse(outcome, headers={"cache-control": "no-store"})


def _escalate_to_tier_two(
    req: CommandRequest, first: dict, *, client_ip: str | None, origin: str | None
) -> JSONResponse | None:
    """Re-run the utterance through the model. None means "keep the original refusal".

    🔑 EVERY FAILURE PATH RETURNS None RATHER THAN AN ERROR. The operator already has a
    true, readable answer: the thing they named does not exist. If the model is rate
    limited, unavailable, or produces nothing runnable, the right outcome is that original
    sentence, not a worse one about the escalation. An optimisation that can only improve
    the answer or leave it alone is one nobody has to reason about at 3am.

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

    if _tier_two_blocked(
        client_ip=client_ip, origin=origin, utterance=req.utterance, source=req.source
    ):
        return None

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
        selection = llmlib.default_provider().select(req.utterance, _model_context(context))
    except llmlib.LLMUnavailable as exc:
        db.log_event(
            tool="unparsed", source=req.source, result="rejected",
            command_id=parent, parent_command_id=parent,
            params={"utterance": req.utterance, "escalated_from": "parser"},
            detail=f"tier 1 named something that does not exist; tier 2 unavailable: {exc}",
        )
        return None

    plan = selection.to_plan()
    db.log_event(
        tool="llm_select", source=req.source, tier="llm", result="ok",
        parent_command_id=parent,
        # `escalated_from` is what makes this call answerable in the log: it separates
        # "the parser had no idea" from "the parser guessed and was wrong", which are
        # different stories about the same money.
        params={"utterance": req.utterance, "selection": plan,
                "escalated_from": "parser", **selection.usage},
        detail=selection.reasoning,
        latency_ms=selection.usage.get("latency_ms"),
    )
    if not plan:
        return None

    try:
        outcome = executor.execute(
            plan, source=req.source, tier="llm", utterance=req.utterance,
            parent_command_id=parent, context=req.context,
        )
    except (executor.PlanRejected, RuntimeError):
        return None

    outcome["ok"] = all(r["ok"] for r in outcome["results"])
    outcome["tier"] = "llm"
    outcome["escalated_from"] = "parser"
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
        return JSONResponse(
            {"ok": False, "detail": str(exc)},
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
    db.log_event(
        tool="transcribe", source="voice", tier="llm", result="ok",
        command_id=command_id,
        params={k: v for k, v in out.items() if k != "text"},
        detail=out["text"],
        latency_ms=out["latency_ms"],
    )
    return JSONResponse(
        {"ok": True, "text": out["text"], "command_id": command_id},
        headers={"cache-control": "no-store"},
    )


@app.get("/api/tools")
def tools_list() -> JSONResponse:
    """The registry as data.

    One source for the model's prompt and the UI's button list, so a tool that exists is
    reachable from both and a tool that is removed disappears from both at once.
    """
    return JSONResponse({"tools": toollib.schemas()})


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
        raise HTTPException(503, str(exc)) from exc
    max_id = rows[-1]["id"] if rows else since_id
    return JSONResponse(
        {"events": rows, "count": len(rows), "max_id": max_id},
        headers={"cache-control": "no-store"},
    )
