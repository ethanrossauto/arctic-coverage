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

from datetime import date, datetime, timezone

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ._lib import db
from ._lib import executor
from ._lib import llm as llmlib
from ._lib import ratelimit
from ._lib import parser
from ._lib import tools as toollib
from ._lib import satellites as satlib
from ._lib import mesh as meshlib
from ._lib import seed
from ._lib import window_builder

app = FastAPI(
    title="Arctic Coverage",
    description=(
        "Satellite coverage planning for deployable Arctic sensor nodes."
    ),
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)


@app.get("/api/healthz")
def healthz() -> dict:
    """Liveness, plus enough detail to tell WHICH build answered.

    The seed counts are here deliberately: during the first deploys the useful
    question was never "is it up" but "is it running the code I think it is".
    """
    return {
        "ok": True,
        # Reported rather than assumed: this endpoint exists to tell "the app is up but the
        # database is not" apart from "the app is down", which was exactly the gap that made
        # a broken deploy on 2026-08-07 diagnosable only from provider logs.
        "database": db.status(),
        "sites": len(seed.SEED_SITES),
        "seed_satellites": len(seed.SEED_TLES),
        "spend": ratelimit.status(),
        "mask_deg": seed.DEFAULT_MASK_DEG,
        "tle_epoch": seed.TLE_EPOCH.date().isoformat(),
    }


@app.get("/api/window")
def window(
    from_: str | None = Query(
        None,
        alias="from",
        description="ISO 8601 instant to start the window at. Defaults to now.",
    ),
    minutes: float = Query(
        window_builder.DEFAULT_WINDOW_MINUTES,
        ge=1,
        le=window_builder.MAX_WINDOW_MINUTES,
        description="Window length. Clamped at the endpoint, not in the tool layer, "
        "because this is a raw GET that anything can call.",
    ),
) -> JSONResponse:
    """Sampled satellite positions plus exact pass intervals for a time window."""
    if from_:
        try:
            start = datetime.fromisoformat(from_.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, "from must be an ISO 8601 instant")
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
    else:
        start = datetime.now(timezone.utc)

    try:
        payload = window_builder.build_window(start, minutes)
    except satlib.PropagationError as exc:
        # A propagation failure is a real answer about the orbit, not a crash.
        raise HTTPException(500, str(exc)) from exc

    return JSONResponse(payload, headers={"cache-control": "no-store"})


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
        rows = db.fetch_entities(kind)
    except RuntimeError as exc:
        # A missing connection string is a configuration failure, not an empty
        # world, and it must not render as one.
        raise HTTPException(503, str(exc)) from exc
    return JSONResponse(
        {"entities": rows, "count": len(rows)},
        headers={"cache-control": "no-store"},
    )


@app.get("/api/mesh")
def mesh() -> JSONResponse:
    """Who can currently talk to whom, computed from live positions.

    Derived per request and never stored, the same treatment satellite passes get. A
    stored graph is a second source of truth that goes stale the moment an entity moves,
    and everything here moves.

    Read-only, so it is not logged, for the same reason `/api/entities` is not: the audit
    trail records actions taken, and looking at the world is not one.
    """
    try:
        rows = db.fetch_entities()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc

    payload = meshlib.mesh_status(rows)
    payload["model"] = {
        # Stated on the wire, not just in a docstring, because every number above depends
        # on it and a reader of the API should not have to open the source to find the
        # assumptions.
        "horizon_formula": "4.12 * (sqrt(h1_m) + sqrt(h2_m))",
        "note": (
            "4/3-earth radio horizon, capped by each radio's rated range. Terrain "
            "masking, Fresnel clearance and fade margin are not modelled, so these "
            "links are an optimistic upper bound."
        ),
        "profiles": {
            kind: {"height_m": p.height_m, "max_range_km": p.max_range_km, "role": p.role}
            for kind, p in meshlib.RADIO.items()
        },
    }
    return JSONResponse(payload, headers={"cache-control": "no-store"})


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
            if not ratelimit.origin_allowed(origin):
                return JSONResponse(
                    {"ok": False, "summary": "the reasoning layer is not available from this origin",
                     "tier": "parser", "results": []},
                    status_code=200, headers={"cache-control": "no-store"},
                )
            verdict = ratelimit.check(client_ip)
            if not verdict.allowed:
                db.log_event(
                    tool="rate_limited", source=req.source, result="rejected",
                    params={"utterance": req.utterance, "scope": verdict.scope,
                            "used": verdict.used, "limit": verdict.limit},
                    detail=verdict.reason,
                )
                return JSONResponse(
                    {"ok": False, "summary": verdict.reason, "tier": "parser", "results": []},
                    status_code=200, headers={"cache-control": "no-store"},
                )

            try:
                selection = llmlib.default_provider().select(req.utterance, req.context)
            except llmlib.LLMUnavailable as exc:
                db.log_event(
                    tool="unparsed",
                    source=req.source,
                    result="rejected",
                    params={"utterance": req.utterance},
                    detail=f"no deterministic parse; tier 2 unavailable: {exc}",
                )
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
                    },
                    status_code=200,
                    headers={"cache-control": "no-store"},
                )

            plan = selection.to_plan()
            tier = "llm"
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
                        "summary": f"I understood that as no action. {selection.reasoning}",
                        "tier": "llm",
                        "results": [],
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
        )
    except executor.PlanRejected as exc:
        return JSONResponse(
            {"ok": False, "summary": "; ".join(exc.reasons), "tier": tier, "results": []},
            status_code=200,
            headers={"cache-control": "no-store"},
        )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc

    outcome["ok"] = all(r["ok"] for r in outcome["results"])
    outcome["tier"] = tier
    outcome["plan"] = plan
    return JSONResponse(outcome, headers={"cache-control": "no-store"})


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
