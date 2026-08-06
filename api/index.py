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

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from ._lib import satellites as satlib
from ._lib import seed
from ._lib import window_builder

app = FastAPI(
    title="Arctic Coverage",
    description=(
        "Satellite coverage planning for deployable Arctic sensor nodes. "
        "Declared assessment scenario: Problem 3, LLM-based user interface."
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
        "sites": len(seed.SEED_SITES),
        "seed_satellites": len(seed.SEED_TLES),
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
