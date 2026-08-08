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

The cost is real: one bad parameter loses the whole utterance, and the user retypes.

⚠️ EXECUTION IS FAIL-FAST, NOT TRANSACTIONAL, AND THE DIFFERENCE IS WORTH SAYING OUT
LOUD. Once a plan validates, its steps run in order and the first refusal stops the rest.
Steps that already ran have already committed (`db.insert_entity` commits per call), so
there is no rollback and this layer does not pretend to offer one. Atomicity is a
property of the CHECK, not of the WRITE.

🔴 THE REASON THIS PARAGRAPH USED TO GIVE HAS EXPIRED, AND THAT IS WORTH RECORDING RATHER
THAN QUIETLY REWRITING. It said making execution transactional "would mean one connection
held open across every step of a plan", and treated that as the prohibitive cost. As of the
per-request connection in `db.py`, **that is exactly what already happens**: one connection
is held for the whole request, so every step of a plan shares it. The obstacle named here
was removed by a change made for an unrelated reason, latency, and nothing pointed that out.

What actually stands in the way now is smaller and different: every writer calls
`conn.commit()` for itself, so a plan-level transaction would mean taking that decision away
from the tools. And the shape argument is unchanged and still the real one: the writing
tools are `place_asset` and `task_uas`, and no plan either tier produces contains two of
them, so there is no partial write to roll back. That is a reason to leave it, not a reason
to claim it was never a gap.
"""
from __future__ import annotations

import copy
import inspect
import time
import uuid
from typing import Any

from . import db
from .tools import REGISTRY, Ambiguous, ToolError, ToolResult, Unresolved

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

        # ⚠️ MISSING IS AS INVALID AS WRONG, and this used to check only the second one.
        # `place_asset` with no `lat` passed validation cleanly and then died inside the
        # call as a TypeError, which the executor logged as `error`, the category
        # reserved for "this code has a bug", not for "you did not say where". A
        # malformed request has to be refused by the validator, or the audit log stops
        # being able to tell a bad request from a broken tool.
        missing = sorted(set(_required(spec.fn)) - set(params))
        if missing:
            reasons.append(f'step {i} ({name}): missing required parameters {", ".join(missing)}')

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


def _required(fn: Any) -> list[str]:
    """The parameters a tool cannot run without, read off the function itself.

    🔑 DERIVED, NOT DECLARED. The registry's `params` dict is a description of each
    parameter for the model's prompt, and adding a "required" flag to it would create a
    second statement of something the signature already says perfectly. A default means
    optional; no default means required. The two can then never disagree, because there
    is only one of them.
    """
    return [
        name
        for name, p in inspect.signature(fn).parameters.items()
        if p.default is inspect.Parameter.empty
        and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    ]


# What the parser writes when the operator said "this" or "the current zoom window"
# instead of naming something. The plan stays a plain data structure that means nothing
# until it meets the context, which is what lets the parser stay free of live state.
VIEWPORT = "__viewport__"
SELECTION = "__selected__"

# 🔴 WHAT THE LAST COMMAND ANSWERED WITH. "List them" means the three things you just
# showed me, and until this existed it meant nothing at all: the placeholder was absent,
# "them" bound to no state, and the request widened silently to the whole world.
#
#   > how many unknown parties on foot
#   · 3 matching
#   > list them
#   · 76 matching
#
# 🔑 BOUND TO THE PREVIOUS RESULT SET, NOT TO THE PREVIOUS SENTENCE. Re-parsing the earlier
# utterance would be a second guess at a question that has already been answered exactly;
# the ids that came back are the only true record of what "them" refers to.
RESULT = "__result__"

# 🔴 THE ASSET THIS PLAN IS ABOUT, once one of its own steps has resolved it. The other
# three placeholders are answered by the CLIENT: the viewport and the selection come in on
# the context, and the previous result set comes in on the history. This one is answered by
# the plan itself, mid-flight.
#
# It is what lets one request do all four of the things a serialized action is supposed to
# do to a named asset. "Isolate Daymark 01" means: put the camera on it, filter the picture
# to it, select it, and open its detail. The filter step needs the asset's id, the parser
# only ever had the words "daymark 01", and the id does not exist until a step has run. So
# the first step to name an asset fixes the subject and the rest of the plan refers to it.
SUBJECT = "__subject__"

# The words people actually use for the thing they are looking at. Deixis is not a corner
# case in a map application: "this", "it", "that one" are how anyone refers to the asset
# they just clicked, and a system that cannot resolve them forces the operator to type a
# name they can already see on screen.
DEICTIC = {
    "this",
    "this one",
    "this asset",
    "this entity",
    "it",
    "that",
    "that one",
    "the selected asset",
    "the selection",
    "selected",
}


# The plural half of deixis. Kept apart from `DEICTIC` because they resolve against
# different state and fail with different advice: "this" means the selection and is fixed
# by clicking something, "them" means the last answer and is fixed by asking for one.
PLURAL_DEICTIC = {
    "them",
    "those",
    "these",
    "they",
    "the last lot",
    "the last ones",
    "that list",
    "the list",
    "the results",
    "those ones",
    "the same ones",
}


def _last_result_ids(context: dict[str, Any]) -> list[str]:
    """The entity ids the previous command answered with.

    🔒 READ FROM THE CLIENT'S `recent` HISTORY, NEWEST LAST, AND ONLY THE LAST ONE. "Them"
    never reaches two turns back: an operator who says it means the thing on the screen in
    front of them. Anything longer would be memory, and this is deixis.

    ⚠️ SHAPE-CHECKED RATHER THAN TRUSTED. This comes from the browser, so a malformed entry
    must read as "no previous answer" and produce the honest refusal, never an exception
    from inside a parameter substitution.
    """
    recent = context.get("recent")
    if not isinstance(recent, list) or not recent:
        return []
    last = recent[-1]
    if not isinstance(last, dict):
        return []
    ids = last.get("ids")
    if not isinstance(ids, list):
        return []
    return [i for i in ids if isinstance(i, str) and i]


def _bind_subject(params: dict[str, Any], subject: str | None) -> dict[str, Any]:
    """Replace the SUBJECT placeholder with the asset this plan has already resolved.

    🔑 IT BINDS AT RUN TIME, WHICH IS THE ONLY TIME IT CAN. The parser knows the operator
    typed "daymark 01"; it does not know which row that is, and it must not, because
    working from words alone is what lets it be tested with no database. The id only exists
    once a step has run, so the substitution happens here, between steps.

    ⚠️ SCALAR OR INSIDE A LIST, because the tools differ: `describe_entity` takes one
    `target`, `list_entities` takes a list of `ids`. Matching on the placeholder VALUE
    rather than on the parameter name means neither this function nor the parser needs a
    table of which parameters are plural.

    An unbound placeholder is left exactly as it is. It then reaches the tool as the literal
    string, resolves to nothing, and is refused by name, which is a visible failure rather
    than a step that quietly widened to everything.
    """
    if subject is None:
        return dict(params)
    bound: dict[str, Any] = {}
    for key, value in params.items():
        if value == SUBJECT:
            bound[key] = subject
        elif isinstance(value, list):
            bound[key] = [subject if v == SUBJECT else v for v in value]
        else:
            bound[key] = value
    return bound


def _with_ids(result: ToolResult) -> dict[str, Any]:
    """Guarantee `data.ids` on any result that names an entity.

    🔴 ONE PLACE, NOT EIGHT. Only `list_entities` and `show_unknown` filled this in, and
    every other entity-bearing tool left it out: `describe_entity`, `focus_entity`,
    `entity_history`, `place_asset` and the four writers all know exactly which asset they
    acted on and none of them said so in a field anybody could read generically.

    🔑 WHY IT MATTERS BEYOND TIDINESS. `data.ids` is what the client accumulates into the
    conversation history, and it is what "list them" binds to. Without this, "tell me about
    Daymark 03" followed by "frame them" has nothing to point at, so a perfectly reasonable
    two-turn exchange dies on a tool that simply forgot to mention its own subject.

    ⚠️ IT NEVER OVERWRITES. A tool that already reports its ids is authoritative, including
    when it reports an EMPTY list: "those zero things" is an answer, and replacing it with
    the entity_id would turn an empty answer into a one-item one.
    """
    data = dict(result.data)
    if "ids" not in data and result.entity_id:
        data["ids"] = [result.entity_id]
    return data


def resolve_context(
    plan: list[dict[str, Any]], context: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Turn "this" and "the current window" into an id and a box, or say why it cannot.

    🔑 THIS IS WHERE A COMMAND MEETS THE SCREEN. The parser and the model both work from
    words alone and have no idea what is selected or visible, which is what keeps them
    testable without a browser. The plans they emit carry a placeholder, and this is the
    one place that placeholder becomes live state.

    ⚠️ AN UNRESOLVABLE PLACEHOLDER IS A REFUSAL WITH A REASON, NEVER A SILENT DROP. Asking
    for "this asset" with nothing selected is a real mistake an operator makes, and the
    useful answer names it: select something first. Dropping the parameter instead would
    quietly widen the request to every asset in the world, which is the most expensive
    possible way to be wrong about what someone asked for.
    """
    context = context or {}
    resolved: list[dict[str, Any]] = []

    for step in plan:
        params = dict(step.get("params", {}))
        for key, value in list(params.items()):
            if value == VIEWPORT:
                bbox = context.get("bbox")
                if not bbox:
                    raise PlanRejected(
                        ["I cannot tell what is on screen right now, so 'the current "
                         "window' has nothing to mean. Try naming a kind instead"]
                    )
                params[key] = bbox
            elif value == RESULT or (
                isinstance(value, str) and value.strip().lower() in PLURAL_DEICTIC
            ):
                ids = _last_result_ids(context)
                if not ids:
                    raise PlanRejected(
                        ["I do not have a previous answer to point at, so 'them' has "
                         "nothing to mean yet. Ask for something first"]
                    )
                params[key] = ids
            elif value == SELECTION or (
                isinstance(value, str) and value.strip().lower() in DEICTIC
            ):
                selected = context.get("selected_id")
                if not selected:
                    raise PlanRejected(
                        ["nothing is selected, so I do not know which asset 'this' means. "
                         "Select one on the map, or name it"]
                    )
                params[key] = selected
        resolved.append({**step, "params": params})

    return resolved


def execute(
    plan: list[dict[str, Any]],
    *,
    source: str,
    tier: str | None,
    utterance: str | None = None,
    parent_command_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate, then run every step under one command id.

    ⚠️ A REJECTED PLAN IS STILL LOGGED. An audit log that records only what succeeded
    cannot answer "what did someone try to do", which is most of what an audit log is
    for. It lands as `result='rejected'` with the reasons in `detail`.
    """
    command_id = str(uuid.uuid4())

    # Deixis is resolved BEFORE validation, so what gets validated, logged and run is what
    # the words actually meant. Validating the placeholder would check a string nobody
    # intended to pass, and the audit row would record it.
    try:
        plan = resolve_context(plan, context)
    except PlanRejected as exc:
        db.log_event(
            tool="plan", source=source, tier=tier, result="rejected",
            command_id=command_id, parent_command_id=parent_command_id,
            params={"plan": plan, "utterance": utterance}, detail="; ".join(exc.reasons),
        )
        raise

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

    # 🔴 THE DECISION IS A ROW, NOT JUST ITS CONSEQUENCES, and this was the hole in the
    # chain. A plan that FAILED validation logged the utterance; a plan that passed logged
    # only its steps, and a step row carries the tool's parameters rather than the
    # sentence. So the audit log could tell you that `list_entities(kind='uas')` ran and
    # could not tell you that someone said "where are my drones" to cause it. The one
    # question an interface like this has to be able to answer afterwards is what a person
    # asked for and what the system decided that meant, and half of it was unrecoverable
    # on exactly the commands that worked.
    #
    # It lands BEFORE the steps so the row order is the reasoning order: the model's
    # selection if there was one, then the plan, then what the plan did. Reading a
    # command_id top to bottom now reads as a chain.
    #
    # ✅ THIS USED TO WARN THAT THE ROW COST ONE MORE CONNECTION, because `db.log_event`
    # opened its own. It no longer does: `db.request_scope` holds one connection for the
    # whole request and every audit write shares it. Kept as a correction rather than
    # deleted, because the warning read as a standing cost of logging and it was really a
    # property of how connections were managed two layers down.
    db.log_event(
        tool="plan",
        source=source,
        tier=tier,
        result="ok",
        command_id=command_id,
        parent_command_id=parent_command_id,
        params={"plan": plan, "utterance": utterance},
        detail=f'accepted {len(plan)} step(s): {", ".join(s["tool"] for s in plan)}',
    )

    results: list[dict[str, Any]] = []
    merged_effects: dict[str, Any] = {}
    # Set only by an `Unresolved` refusal below. Reported in the outcome so the API can
    # decide whether to escalate the utterance to tier 2 instead of showing a dead end.
    unresolved = False
    unresolved_ref: dict[str, Any] | None = None
    # The asset this plan has resolved, once any step has named one. See SUBJECT.
    subject: str | None = None

    for index, step in enumerate(plan):
        name = step["tool"]
        params = _bind_subject(step.get("params", {}), subject)
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
            results.append(
                {"tool": name, "ok": True, "message": result.message, "data": _with_ids(result)}
            )
            merged_effects.update(result.ui_effects)
            # 🔑 THE FIRST STEP TO NAME AN ASSET FIXES THE SUBJECT FOR THE REST OF THE PLAN.
            # First rather than latest, deliberately: the steps after it are meant to be
            # about the same thing, and letting a later step move the target would make a
            # chain mean different things depending on how far through it you looked.
            if subject is None and result.entity_id:
                subject = result.entity_id

        except Ambiguous as exc:
            # 🔑 THE ONE REFUSAL THAT IS A QUESTION. Everything else the executor
            # declines is final: the operator has to type something different. This one
            # is not, because the system already knows every answer that would work, and
            # a refusal that withholds the list it just computed is a worse interface
            # than one that had never resolved the name at all.
            #
            # Caught above ToolError because it IS one. The ordering is what lets every
            # other catch site keep treating it as an ordinary refusal.
            elapsed = int((time.perf_counter() - started) * 1000)
            db.log_event(
                tool=name,
                source=source,
                tier=tier,
                # 🔑 ITS OWN OUTCOME. "I understood you and need one more word" is not a
                # refusal, and recording it as one would make the two most useful
                # questions about this log unanswerable: how often does the system have
                # to ask, and how often does it have to say no. The candidates go in
                # `params`, so the row records what was offered as well as that it asked.
                result="clarify",
                command_id=command_id,
                parent_command_id=parent_command_id,
                params={**params, "clarify_candidates": [c["id"] for c in exc.candidates]},
                detail=str(exc),
                latency_ms=elapsed,
            )
            results.append({"tool": name, "ok": False, "message": str(exc)})
            merged_effects["clarify"] = _clarify(exc, command_id, plan, index)
            break

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
            # 🔑 REPORTED, NOT DECIDED HERE. The executor does not know which tier wrote
            # this plan or whether re-running the utterance is affordable, so it says what
            # happened and lets the API decide. A referent that matched nothing is the one
            # refusal worth escalating rather than showing.
            if isinstance(exc, Unresolved):
                unresolved = True
                # The query that failed and everything that does exist, carried
                # through so the escalated model call knows more than the first one.
                unresolved_ref = {"query": exc.query, "available": exc.available}
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
        # 🔑 THE RESOLVED PLAN, NOT THE ONE THAT ARRIVED. "this asset" became an id and
        # "the current window" became a box before anything ran, so reporting the
        # placeholder would show the caller a plan that never executed while the audit log
        # holds the one that did. Two accounts of the same command is the thing this
        # layer exists to prevent.
        "plan": plan,
        # 🔑 "a referent matched nothing", which is a different thing from "this was
        # refused". The API escalates on it when the plan came from the parser.
        "unresolved": unresolved,
        "unresolved_ref": unresolved_ref,
    }


def _clarify(
    exc: Ambiguous, command_id: str, plan: list[dict[str, Any]], index: int
) -> dict[str, Any]:
    """Turn "which one?" into something a client can actually offer.

    🔑 EVERY OPTION CARRIES A READY-TO-RUN PLAN, which is what keeps this from needing a
    second endpoint, a second validator or a server-side memory of half-finished
    commands. The client posts the option's `plan` straight back to /api/command with
    `parent_command_id` set to the `command_id` here, and it arrives as an ordinary
    button-shaped request: same validator, same executor, same audit rows. A clarify
    session held on the server would be state to expire, and on a serverless platform it
    would be state that does not survive the next invocation.

    ⚠️ THE PLAN STARTS AT THE AMBIGUOUS STEP, NOT AT THE BEGINNING. Anything before it
    already ran and already committed, so replaying the whole plan would place a second
    asset or fly a drone twice. The prior steps are logged under the parent command; the
    answer only owes the part that did not happen.
    """
    return {
        "command_id": command_id,
        "query": exc.query,
        "question": f'Which "{exc.query}" did you mean?',
        "total": exc.total,
        "options": [
            {
                "id": c["id"],
                "label": c["name"],
                "detail": f'{c["kind"]}, {c["status"]}',
                "plan": _resubmit(plan, index, exc.query, c["id"]),
            }
            for c in exc.candidates
        ],
    }


def _resubmit(
    plan: list[dict[str, Any]], index: int, query: str, chosen_id: str
) -> list[dict[str, Any]] | None:
    """The same request with the vague word replaced by one id.

    Substitution is by value rather than by parameter name: the ambiguous phrase is
    whatever the operator typed, and it arrives in `target` for most tools and inside
    `targets` for `frame_entities`. Matching on the value finds it in both without this
    function needing a table of which parameter each tool resolves.

    🔒 RETURNS None RATHER THAN A PLAN THAT WOULD ASK THE SAME QUESTION AGAIN. If nothing
    could be substituted, offering the option anyway would give the operator a button
    that loops. The client should render those as plain text.
    """
    steps = copy.deepcopy(plan[index:])
    params = steps[0].setdefault("params", {})
    needle = query.strip().lower()
    swapped = False

    for key, value in list(params.items()):
        if isinstance(value, str) and value.strip().lower() == needle:
            params[key] = chosen_id
            swapped = True
        elif isinstance(value, list):
            replaced = [
                chosen_id if isinstance(v, str) and v.strip().lower() == needle else v
                for v in value
            ]
            if replaced != value:
                params[key] = replaced
                swapped = True

    if not swapped:
        # The phrase was rewritten somewhere between the plan and the resolver. Falling
        # back to the tool's own target parameter is still an unambiguous request.
        spec = REGISTRY.get(steps[0].get("tool", ""))
        if spec is None or "target" not in spec.params:
            return None
        params["target"] = chosen_id

    return steps


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
    # 🔴 POSITIONS COME FROM THE RESULTS, NOT FROM A SECOND READ OF THE WORLD. This used
    # to re-fetch every entity purely to look up where the answer was, which meant the
    # commonest command in the application paid for two full trips to the database: one to
    # find the assets and one to find out where they are. Measured, a connection to the
    # pooled endpoint is hundreds of milliseconds of pure network, and a simple listing
    # command was opening four of them.
    #
    # Tools that return a set of entities now carry their positions, so framing is
    # arithmetic on data already in hand.
    points: list[tuple[float, float]] = []
    ids: list[str] = []
    for r in results:
        if not r.get("ok"):
            continue
        data = r.get("data") or {}
        for point in data.get("points") or []:
            if isinstance(point, (list, tuple)) and len(point) == 2:
                points.append((float(point[0]), float(point[1])))
        found = data.get("ids")
        if isinstance(found, list):
            ids.extend(str(i) for i in found)
        elif data.get("id"):
            ids.append(str(data["id"]))
        elif isinstance(data.get("asset"), dict) and data["asset"].get("id"):
            ids.append(str(data["asset"]["id"]))

    if not points and not ids:
        return None

    try:
        from .tools import frame_for

        # ⚠️ THE FALLBACK STILL EXISTS, and it still costs a read. A tool that reports ids
        # without positions (a single placed asset, a described entity) is framed the old
        # way rather than not at all. The point is that the common path no longer pays it.
        if not points:
            from . import db as _db

            by_id = {row["id"]: row for row in _db.fetch_entities()}
            points = [
                (by_id[i]["lat"], by_id[i]["lon"])
                for i in dict.fromkeys(ids)
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
