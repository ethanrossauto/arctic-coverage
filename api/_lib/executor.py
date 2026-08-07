"""Validate a plan, then run it, logging every step.

🔒 THE EXECUTOR IS THE ONLY WAY TO REACH A TOOL. Not a convention, a structural fact:
nothing else imports `tools.REGISTRY` to call anything. That is what makes "every action
is in the audit log" a property of the code rather than a promise, because the one code
path that can act is the one that writes the event.

🔒 VALIDATION IS ATOMIC. Any invalid step rejects the WHOLE plan and nothing executes.

That is a deliberate divergence from the obvious alternative (drop the bad steps, run the
rest), and the reason is that a plan's steps SHARE REFERENTS. "Focus SAT-02, filter to
it, then frame it against Alert" is three steps about one thing; running steps one and
three after step two was rejected leaves a state nobody asked for and nobody can explain
from the log. Partial execution of a plan is worse than no execution, because the
operator now has to work out what happened rather than reading one refusal.

The cost is real and is stated in the README's tradeoffs: one bad parameter loses the
whole utterance, and the user retypes.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from . import db
from .tools import REGISTRY, ToolError, ToolResult

# A plan longer than this is not a plan, it is a loop, and the model does not get one.
MAX_STEPS = 8


class PlanRejected(Exception):
    """The plan did not survive validation. Carries every reason, not just the first.

    Reporting only the first failure means a user fixes it, resubmits, and meets the
    second. Cheap to collect them all, and it is the difference between one round trip
    and four.
    """

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def validate(plan: list[dict[str, Any]]) -> list[str]:
    """Every reason this plan must not run. Empty means it may."""
    reasons: list[str] = []

    if not isinstance(plan, list) or not plan:
        return ["the plan is empty"]
    if len(plan) > MAX_STEPS:
        return [f"the plan has {len(plan)} steps; the limit is {MAX_STEPS}"]

    for i, step in enumerate(plan, start=1):
        if not isinstance(step, dict):
            reasons.append(f"step {i} is not an object")
            continue
        name = step.get("tool")
        if not name:
            reasons.append(f"step {i} names no tool")
            continue
        spec = REGISTRY.get(name)
        if spec is None:
            # ⚠️ THE ALLOWLIST IS THE REGISTRY ITSELF. A model that invents a plausible
            # tool name gets refused here rather than reaching a getattr somewhere.
            reasons.append(f'step {i}: "{name}" is not a tool. Available: {", ".join(sorted(REGISTRY))}')
            continue

        params = step.get("params", {})
        if not isinstance(params, dict):
            reasons.append(f"step {i}: params must be an object")
            continue

        unknown = set(params) - set(spec.params)
        if unknown:
            reasons.append(f'step {i} ({name}): unknown parameters {", ".join(sorted(unknown))}')

        # Numeric sanity, clamped here rather than trusted. A hallucinated latitude is
        # the single most likely bad value in a geographic tool call.
        for key in ("lat", "latitude"):
            if key in params and not _in_range(params[key], -90, 90):
                reasons.append(f"step {i} ({name}): {key} {params[key]!r} is not a latitude")
        for key in ("lon", "longitude"):
            if key in params and not _in_range(params[key], -180, 180):
                reasons.append(f"step {i} ({name}): {key} {params[key]!r} is not a longitude")
        if "altitude_m" in params and not _in_range(params["altitude_m"], 0, 30000):
            reasons.append(f"step {i} ({name}): altitude {params['altitude_m']!r} is out of range")

    return reasons


def _in_range(value: Any, lo: float, hi: float) -> bool:
    try:
        return lo <= float(value) <= hi
    except (TypeError, ValueError):
        return False


def execute(
    plan: list[dict[str, Any]],
    *,
    source: str,
    tier: str | None,
    utterance: str | None = None,
    parent_command_id: str | None = None,
) -> dict[str, Any]:
    """Validate, then run every step under one command id.

    ⚠️ A REJECTED PLAN IS STILL LOGGED. An audit log that records only what succeeded
    cannot answer "what did someone try to do", which is most of what an audit log is
    for. It lands as `result='rejected'` with the reasons in `detail`.
    """
    command_id = str(uuid.uuid4())

    reasons = validate(plan)
    if reasons:
        db.log_event(
            tool="plan",
            source=source,
            tier=tier,
            result="rejected",
            command_id=command_id,
            parent_command_id=parent_command_id,
            params={"plan": plan, "utterance": utterance},
            detail="; ".join(reasons),
        )
        raise PlanRejected(reasons)

    results: list[dict[str, Any]] = []
    merged_effects: dict[str, Any] = {}

    for step in plan:
        name = step["tool"]
        params = step.get("params", {})
        spec = REGISTRY[name]
        started = time.perf_counter()

        try:
            result: ToolResult = spec.fn(**params)
            elapsed = int((time.perf_counter() - started) * 1000)
            db.log_event(
                tool=name,
                source=source,
                tier=tier,
                result="ok",
                command_id=command_id,
                parent_command_id=parent_command_id,
                params=params,
                detail=result.message,
                entity_id=result.entity_id,
                latency_ms=elapsed,
            )
            results.append({"tool": name, "ok": True, "message": result.message, "data": result.data})
            merged_effects.update(result.ui_effects)

        except ToolError as exc:
            # The system working correctly and declining. Logged as a refusal, not an
            # error, because the distinction is the whole point of having a validator.
            elapsed = int((time.perf_counter() - started) * 1000)
            db.log_event(
                tool=name,
                source=source,
                tier=tier,
                result="rejected",
                command_id=command_id,
                parent_command_id=parent_command_id,
                params=params,
                detail=str(exc),
                latency_ms=elapsed,
            )
            results.append({"tool": name, "ok": False, "message": str(exc)})
            # ⚠️ STOP HERE. Later steps share referents with this one, so continuing
            # would act on assumptions this refusal just invalidated.
            break

        except Exception as exc:  # noqa: BLE001 - logged, then re-raised as a step failure
            elapsed = int((time.perf_counter() - started) * 1000)
            db.log_event(
                tool=name,
                source=source,
                tier=tier,
                result="error",
                command_id=command_id,
                parent_command_id=parent_command_id,
                params=params,
                detail=f"{type(exc).__name__}: {exc}"[:400],
                latency_ms=elapsed,
            )
            results.append({"tool": name, "ok": False, "message": f"{name} failed: {exc}"})
            break

    # 🔑 THE CAMERA IS A FUNCTION OF THE RESULT, NOT A SEPARATE DECISION.
    #
    # An earlier design asked the model to choose a view command alongside the data
    # command, and gave it "none" as an option for answers scattered across the map.
    # That was wrong twice over. It added a whole axis the model could get wrong, and it
    # left the operator looking at the old view holding a list of things they now have to
    # find. The right framing: run the data command, then orient the camera to best show
    # what came back.
    #
    # So framing happens HERE, once, after every plan, derived from the entities the plan
    # actually touched. A tool that has its own opinion about the camera (focus_entity,
    # reset_view) still wins, because it was asked for explicitly.
    if "camera" not in merged_effects:
        auto = _frame_results(results)
        if auto:
            merged_effects["camera"] = auto

    return {
        "command_id": command_id,
        "results": results,
        "ui_effects": merged_effects,
        # 🔒 THE SUMMARY IS WRITTEN BY THE EXECUTOR FROM WHAT ACTUALLY RAN, never by the
        # model. A model-written summary is a claim about the world; this is a report of
        # it, and the two diverge exactly when it matters most.
        "summary": _summarise(results),
    }


def _summarise(results: list[dict[str, Any]]) -> str:
    if not results:
        return "nothing ran"
    failed = [r for r in results if not r["ok"]]
    if failed:
        return failed[-1]["message"]
    if len(results) == 1:
        return results[0]["message"]
    return " · ".join(r["message"] for r in results)


def _frame_results(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The camera that best shows whatever the plan just returned.

    Collects every entity id any step reported, looks up their live positions, and frames
    them. That is a pan to the centre of the answer plus a zoom wide enough to hold all of
    it, which is what "show me the result" means when the result is a set of places.

    ⚠️ ONE ENTITY IS NOT A SPECIAL CASE HERE, it just frames to a fixed close zoom, because
    a single point has no extent to derive a zoom from. See `frame_for`.

    🔒 NEVER RAISES. This runs after the real work has already succeeded and been logged;
    a framing failure must not turn a completed command into an error. Worst case it
    returns None and the camera stays where the operator left it.
    """
    ids: list[str] = []
    for r in results:
        if not r.get("ok"):
            continue
        data = r.get("data") or {}
        found = data.get("ids")
        if isinstance(found, list):
            ids.extend(str(i) for i in found)
        elif data.get("id"):
            ids.append(str(data["id"]))
        elif isinstance(data.get("asset"), dict) and data["asset"].get("id"):
            ids.append(str(data["asset"]["id"]))

    if not ids:
        return None

    try:
        from . import db as _db
        from .tools import frame_for

        by_id = {row["id"]: row for row in _db.fetch_entities()}
        points = [
            (by_id[i]["lat"], by_id[i]["lon"])
            for i in dict.fromkeys(ids)  # dedupe, order preserved
            if i in by_id and by_id[i].get("lat") is not None
        ]
        if not points:
            return None
        camera = frame_for(points)
        # Said on the wire so the client can show WHY the camera moved, and so anyone
        # reading the response can see the framing was derived rather than guessed.
        camera["framed"] = len(points)
        return camera
    except Exception:  # noqa: BLE001 - framing is a convenience; never fail a done command
        return None
