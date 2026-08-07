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
from dataclasses import dataclass, field
from typing import Any, Callable

from . import db, mesh as meshlib, terrain

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


def _require_one(text: str, entities: list[dict[str, Any]]) -> dict[str, Any]:
    matches = _resolve(text, entities)
    if not matches:
        near = ", ".join(e["name"] for e in entities[:4])
        raise ToolError(f'nothing here matches "{text}". Some of what exists: {near}')
    if len(matches) > 1:
        names = ", ".join(f'{e["name"]} ({e["id"]})' for e in matches[:6])
        raise ToolError(f'"{text}" matches {len(matches)} assets: {names}. Which one?')
    return matches[0]


def _bbox_contains(bbox: dict[str, Any], lat: float, lon: float) -> bool:
    """Point-in-box with wraparound, the server twin of `src/map/bounds.ts`.

    ⚠️ THE TWO MUST AGREE OR A FILTER HIGHLIGHTS A DIFFERENT SET THAN IT REPORTS. Kept as
    one short function in each language rather than a shared module for eight lines of
    arithmetic, with this note on both sides.

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
    for limit, zoom in ((0.0, 7.5), (15, 6.5), (60, 5.2), (200, 4.2), (600, 3.2), (1500, 2.4)):
        if spread_km <= limit:
            break
    else:
        zoom = 1.8

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
        "status": "nominal, degraded, warning or silent",
        "not_broadcasting": "true to return only contacts holding no AIS broadcast",
        "isolated": "true to return only assets on no mesh at all",
    },
)
def list_entities(
    kind: str | None = None,
    status: str | None = None,
    not_broadcasting: bool | None = None,
    isolated: bool | None = None,
) -> ToolResult:
    rows = _entities()
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    if status:
        rows = [r for r in rows if r["status"] == status]
    if not_broadcasting:
        rows = [r for r in rows if r.get("ais_reporting") is False]
    if isolated:
        alone = set(meshlib.mesh_status(_entities())["isolated"])
        rows = [r for r in rows if r["id"] in alone]

    return ToolResult(
        ok=True,
        message=f"{len(rows)} matching",
        data={"ids": [r["id"] for r in rows], "names": [r["name"] for r in rows]},
        # A query highlights its answer and does NOT move the camera. Answering "what is
        # overdue" by flying somewhere is disorienting when the answer is four things in
        # three places.
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


@tool(
    "describe_entity",
    "Everything known about one asset, including its mesh neighbours.",
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
    return ToolResult(
        ok=True,
        message=f'{asset["name"]}: {asset["kind"]}, {asset["status"]}, {len(peers)} mesh neighbours',
        data={"asset": asset, "peers": peers},
        ui_effects={"select": asset["id"], "camera": frame_for([(asset["lat"], asset["lon"])])}
        if asset["lat"] is not None
        else {"select": asset["id"]},
        entity_id=asset["id"],
    )


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
    },
    writes=True,
)
def place_asset(kind: str, lat: float, lon: float, name: str | None = None) -> ToolResult:
    placeable = {"node", "hydrophone", "marker", "launch_site"}
    if kind not in placeable:
        raise ToolError(f'cannot place a {kind}; placeable kinds are {", ".join(sorted(placeable))}')

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
            "geometry": None,
            "props": {"placed_by": "operator"},
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
    if asset["status"] == "degraded":
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
                "state": "on_station",
                "station": [lat, lon],
                "eta_min": round(eta_min, 1),
                "endurance_min_remaining": round(endurance - eta_min, 1),
            },
            "last_heard": None,
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


def schemas() -> list[dict[str, Any]]:
    """The registry as data, for building the model's prompt and the UI's button list.

    One source for both, so a tool that exists is reachable from both, and a tool that is
    removed disappears from both at once.
    """
    return [
        {"name": t.name, "summary": t.summary, "params": t.params, "writes": t.writes}
        for t in REGISTRY.values()
    ]
