"""Tier 2: the reasoning layer.

WHAT IT DOES, AND WHAT IT DELIBERATELY DOES NOT. The model does not write code, does not
emit free-form JSON, and does not invent parameters. It answers ONE multiple-choice
question about an utterance the deterministic parser could not handle:

    which DATA command is this person asking for, and with what parameters?

Everything else is decided by the executor and the validator. That framing is the whole
security and cost story: the output space is enumerated in a schema, so a hallucinated
tool name is not something to catch downstream, it is something the API will not produce.

🔴 IT USED TO ANSWER TWO QUESTIONS, AND THE SECOND ONE WAS A MISTAKE. The model was also
asked to pick a camera move, with "none" offered for answers scattered across the map.
The camera behaviour is simple and nearly always the same, so it should not be a decision
at all. Run the data command, then orient the camera to show what came back, which is a
pan to the centre of the answer and a zoom wide enough to hold it.

That is now derived in `executor._frame_results` from the entities the plan actually
touched. It removed an entire axis the model could get wrong, shrank the schema, and cut
output tokens per call. The old design's own example was the tell: it argued that "what is
not broadcasting" should leave the camera alone, which in practice means handing the
operator a list of two contacts and no idea where they are.

⚠️ THE MODEL NEVER TOUCHES STATE. It returns a selection; `executor.execute` runs it
through the same validator every button press goes through. If the model picks a real
tool with impossible parameters, the validator refuses it exactly as it would refuse a
malformed button.

COST. Every call logs its own token counts and computed cost, and every call is metered
before it is made (see `ratelimit.py`). Not telemetry for its own sake: the claim this
architecture makes is that the model is only called when it earns its latency, and that
claim is checkable only if each call's price is recorded next to the tier that produced it.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from .tools import REGISTRY

if TYPE_CHECKING:
    # ⚠️ TYPE-ONLY, because `anthropic` is imported lazily inside the call for the same
    # reason `psycopg` is in db.py: a missing optional dependency must degrade one layer to
    # "unavailable" rather than take the whole API down at import time. `from __future__
    # import annotations` is what lets these names be referenced without importing them.
    from anthropic.types import MessageParam, OutputConfigParam, TextBlockParam

# --------------------------------------------------------------------------
# Model and pricing
# --------------------------------------------------------------------------

MODEL = "claude-opus-5"

# 🔑 THE LARGEST MODEL, KEPT DELIBERATELY, AND THE MEASUREMENT IS WHY THE CHOICE IS
# INFORMED RATHER THAN INHERITED. All three were run against this exact schema on the same
# utterances, warm:
#
#   model        median latency   cost per call   prompt cache
#   haiku-4-5          2.0 s        $0.00128        NEVER ENGAGES
#   sonnet-5           3.8 s        $0.00145        1272 tokens read
#   opus-5             3.5 s        $0.00239        1272 tokens read
#
# ⚠️ WHAT THAT TEST COULD AND COULD NOT SHOW. All three answered every test utterance
# identically, which sounds like a case for the cheaper one and is not: the utterances were
# ones a person would think of first, and a test that nothing fails cannot rank anything.
# The calls that decide this are the awkward ones, where the phrasing is oblique or the
# request half fits two tools, and those are exactly the calls a small sample has no power
# over. Paying about a cent per twenty commands to be better on the hard tail is the right
# side to err on for an interface whose whole claim is that it understands you.
#
# 🔴 THE SMALLEST MODEL IS NOT A MODEL SWAP HERE, and this is worth keeping whichever way
# the choice goes. It refuses the effort parameter outright, so it needs a different request
# shape. And its minimum cacheable prefix is larger than this prompt, so the tool schemas
# fall under the threshold and nothing caches: every request then pays full price for the
# whole system prompt instead of a tenth of it. Nothing reports that. No error, no warning,
# the cache counters simply read zero, which is why it turned up by printing them rather
# than by reasoning. Cheapest tokens, and at this prompt size not the cheapest call.

# ⚠️ EFFORT LOW IS DELIBERATE AND IS THE MAIN COST LEVER. This is a constrained
# multiple-choice task over an enumerated schema, not open-ended reasoning: there is very
# little for depth to buy. Opus at low effort is unusually strong on exactly this shape
# of problem, and it keeps the latency inside what a person will tolerate between typing
# and seeing the map move.
# 🔑 THE ANNOTATION IS THE POINT, not decoration. `effort` is one of exactly five values the
# API accepts, and a typo here would be rejected at the first live call with a 400 that
# nothing in the test suite could catch, because the tests use the replay provider. Naming
# the literal type moves that failure to the type checker.
EFFORT: Literal["low", "medium", "high", "xhigh", "max"] = "low"

# ⚠️ max_tokens BOUNDS THINKING **PLUS** THE RESPONSE, and on this model thinking is ON by
# default. A budget sized for the JSON alone truncates mid-answer, which surfaces as a
# malformed selection rather than as an obvious error. Sized with room for both.
MAX_TOKENS = 2048

# 🔴 THE NUMBER CAME FROM A MEASUREMENT, NOT FROM A HUNCH. Timed over live calls, a
# selection takes 2.9 to 4.1 seconds. One call took 53, and that is the case this exists
# for: an operator typing a command should get an answer or an honest failure in a bounded
# time, and a request with no ceiling turns a slow call into an interface that has simply
# stopped. Twelve seconds is three times the observed median, so it cannot fire on a
# normal call.
#
# Giving up is CHEAP HERE, and that is what makes the short ceiling affordable. A timeout
# raises `LLMUnavailable` like any other transport failure, and the caller answers with
# what the deterministic tier can still do. The operator loses the reasoning layer for one
# utterance; they do not lose the application.
TIMEOUT_S = 12.0

# One retry, not the SDK's default of two. Two would put the worst case past half a
# minute, which is the thing the ceiling above exists to prevent.
MAX_RETRIES = 1

# USD per token, per model. Published list prices, written as a rate rather than a
# per-million figure so the arithmetic below has no hidden factor of a million in it.
#
# 🔴 KEYED BY MODEL, WHICH IT WAS NOT. One pair of constants was hardcoded here and used
# for whatever model happened to run, so the logged cost was right only while exactly one
# model was ever called. Changing the model would have kept the log filling up with
# confident, wrong numbers, and the wrongness would have been invisible: the figures stay
# plausible, they are just computed at another model's rates. That matters more here than
# in most places, because "the model is only called when it earns its latency" is supposed
# to be a query over this column rather than a claim.
#
# ⚠️ LIST PRICE, NOT PROMOTIONAL PRICE. One of these is discounted until the end of August
# 2026. Recording the discount would mean the log quietly under-reports the day it lapses,
# and a cost log that has to be read alongside a calendar is worse than one that is
# slightly pessimistic.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00 / 1_000_000, 25.00 / 1_000_000),
    "claude-sonnet-5": (3.00 / 1_000_000, 15.00 / 1_000_000),
    "claude-haiku-4-5": (1.00 / 1_000_000, 5.00 / 1_000_000),
}

# Cache reads are about a tenth of input; writes carry a 1.25x premium for the 5 minute
# TTL. Both are tracked separately because the whole point of caching the tool schemas is
# that the saving should be visible.
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


@dataclass
class Selection:
    """What tier 2 returns: the chosen data command, its parameters, and the reasoning."""

    data_tool: str
    view_tool: str
    params: dict[str, Any]
    reasoning: str
    usage: dict[str, Any]

    def to_plan(self) -> list[dict[str, Any]]:
        """The selection as a plan, in the identical schema the parser emits.

        🔑 This is what makes the two tiers interchangeable. Downstream, nothing can tell
        which tier produced a plan: same validator, same executor, same audit rows, and
        the `tier` column is the only record of which one ran.
        """
        plan: list[dict[str, Any]] = []
        if self.data_tool and self.data_tool != "none":
            plan.append({"tool": self.data_tool, "params": _params_for(self.data_tool, self.params)})
        if self.view_tool and self.view_tool != "none":
            plan.append({"tool": self.view_tool, "params": _params_for(self.view_tool, self.params)})
        return plan


def _params_for(tool: str, supplied: dict[str, Any]) -> dict[str, Any]:
    """Keep only the parameters this tool actually declares.

    The schema below offers one flat parameter bag rather than a per-tool shape, because
    JSON Schema unions across a dozen tools are unreadable and the model fills them badly.
    The cost of that simplification is that the bag can carry keys the chosen tool does
    not take, so they are dropped here rather than being sent on to fail validation.
    """
    spec = REGISTRY.get(tool)
    if spec is None:
        return {}
    return {k: v for k, v in supplied.items() if k in spec.params and v is not None}


# --------------------------------------------------------------------------
# The schema: an enumerated answer space
# --------------------------------------------------------------------------

# Tools split by axis. A query and a camera move are different decisions, so the model is
# asked for one of each rather than for a free-form list.
DATA_TOOLS = [
    "list_entities",
    "describe_entity",
    "entity_history",
    "mesh_status",
    "coverage",
    "show_overlay",
    "place_asset",
    "task_uas",
    "remove_asset",
    "inject_fault",
    "clear_fault",
    "none",
]
# ⚠️ THE MODEL NO LONGER CHOOSES A CAMERA MOVE, and removing that axis was a correction.
# The camera is derived from the result by the executor (`_frame_results`): run the data
# command, then orient to show what came back. That is one fewer thing the model can get
# wrong, one fewer enum in the schema, and fewer output tokens per call.
#
# These remain only for the rare request that is PURELY about the view ("reset the view"),
# which the deterministic parser already handles, so tier 2 sees them almost never.
VIEW_TOOLS = ["reset_view", "none"]

# 🔑 `focus_entity` AND `frame_entities` ARE DELIBERATELY ABSENT FROM BOTH LISTS, and the
# reason is the same correction that emptied VIEW_TOOLS.
#
# Once the executor started framing the camera on whatever a plan returned, both of them
# became a second way to say something the model can already say. "Focus Daymark 03" is
# `describe_entity`, which selects the asset, frames it, and answers the question the
# operator was about to ask next. "Frame all the drones" is `list_entities(kind='uas')`,
# which highlights them and frames them. Offering the pair as well would put two correct
# answers in the enum for one request and make the choice between them arbitrary, which
# is exactly the axis that was removed for being a thing the model could get wrong for
# no gain.
#
# They stay in the registry because the parser reaches them directly and a button may
# call either. Reachable from the deterministic tier, absent from the enumerated one, on
# purpose rather than by omission.


def response_schema() -> dict[str, Any]:
    """The JSON schema the API is required to produce.

    🔒 THE ENUMS ARE THE SECURITY BOUNDARY. A tool name outside these lists is not
    something to detect and reject downstream; it is something the API will not emit.
    That converts the most likely failure of a free-form JSON prompt into an
    impossibility, and leaves the validator to police parameters rather than identity.
    """
    return {
        "type": "object",
        "properties": {
            "reasoning": {
                "type": "string",
                "description": "One sentence on why this command answers the request.",
            },
            "data_tool": {
                "type": "string",
                "enum": DATA_TOOLS,
                "description": "The command that answers the question, or 'none' for a pure camera move.",
            },
            "view_tool": {
                "type": "string",
                "enum": VIEW_TOOLS,
                "description": (
                    "Almost always 'none'. The camera is oriented automatically to show whatever "
                    "the data command returns. Use 'reset_view' only if the request is purely "
                    "about returning to the default view."
                ),
            },
            "kind": {
                "type": "string",
                "enum": ["node", "patrol", "uas", "launch_site", "hydrophone", "vessel", "radar", "marker", ""],
            },
            # ⚠️ TWO VALUES, NOT THREE. An asset carries exactly one of three flags, but
            # `overdue` is not one this parameter can take: it is computed from the clock,
            # so it has its own boolean below. Offering it here as a status would let the
            # model ask for a stored value that does not exist.
            "status": {"type": "string", "enum": ["nominal", "maintenance", ""]},
            "target": {"type": "string", "description": "A single asset by name or id, or empty."},
            "targets": {"type": "array", "items": {"type": "string"}},
            "lat": {"type": "number"},
            "lon": {"type": "number"},
            "altitude_m": {"type": "number"},
            "name": {"type": "string"},
            "not_broadcasting": {"type": "boolean"},
            "isolated": {"type": "boolean"},
            "overdue": {"type": "boolean"},
            "hostile": {"type": "boolean"},
            "fault": {"type": "string", "enum": ["silent", "maintenance", ""]},
            "days": {
                "type": "number",
                "description": "For entity_history: how far back to look, in days. 0.5 is twelve hours.",
            },
        },
        "required": ["reasoning", "data_tool", "view_tool"],
        "additionalProperties": False,
    }


def system_prompt() -> str:
    """The instructions, built from the live registry so it cannot drift from the code.

    ⚠️ Generated rather than written out, because a hand-maintained list of tools in a
    prompt is a second source of truth that goes stale the first time a tool is renamed.
    """
    lines = [
        "You route operator requests on an Arctic sensor-network display to preset commands.",
        "",
        "Pick the one DATA command that answers the request.",
        "",
        "You do NOT choose how the map moves. The camera is oriented automatically to show",
        "whatever the data command returns. Leave view_tool as 'none' unless the request is",
        "purely about resetting the view.",
        "",
        "DATA commands:",
    ]
    for name in DATA_TOOLS:
        if name == "none":
            lines.append("  none - the request is only about the camera")
            continue
        spec = REGISTRY.get(name)
        if spec:
            lines.append(f"  {name} - {spec.summary}")
    lines += ["", "VIEW commands:"]
    for name in VIEW_TOOLS:
        if name == "none":
            lines.append("  none - leave the camera where it is")
            continue
        spec = REGISTRY.get(name)
        if spec:
            lines.append(f"  {name} - {spec.summary}")

    lines += [
        "",
        "Fill only the parameters the chosen commands need. Leave the rest out.",
        "Never invent an asset name: if the request names something, pass it through verbatim",
        "in 'target' and let the system resolve it.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


class Provider(Protocol):
    def select(self, utterance: str, context: dict[str, Any] | None) -> Selection: ...


class ReplayProvider:
    """Fixture-backed provider. Zero credentials, so CI can exercise the whole path.

    🔑 This is what lets the tier-2 tests run in CI with no API key and no network. It is
    not a mock of the SDK: it returns the same `Selection` the real provider returns, so
    everything downstream of the model call is genuinely under test.
    """

    def __init__(self, fixtures: dict[str, dict[str, Any]] | None = None) -> None:
        self.fixtures = fixtures or {}

    def select(self, utterance: str, context: dict[str, Any] | None = None) -> Selection:
        key = utterance.strip().lower()
        data = self.fixtures.get(key)
        if data is None:
            raise LLMUnavailable(f"no replay fixture for {utterance!r}")
        return Selection(
            data_tool=data.get("data_tool", "none"),
            view_tool=data.get("view_tool", "none"),
            params={k: v for k, v in data.items() if k not in ("data_tool", "view_tool", "reasoning")},
            reasoning=data.get("reasoning", "replayed fixture"),
            usage={"replay": True, "cost_usd": 0.0},
        )


class LLMUnavailable(RuntimeError):
    """No provider could answer. Distinct from a bad answer, and reported differently."""


class AnthropicProvider:
    """The real thing.

    ⚠️ The SDK is imported lazily. A missing `anthropic` package or a missing key must
    degrade tier 2 only; it must not take down `/api/entities`, which is what a
    module-scope import would do the first time this file is touched.
    """

    def __init__(self, model: str = MODEL) -> None:
        self.model = model

    def select(self, utterance: str, context: dict[str, Any] | None = None) -> Selection:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMUnavailable("ANTHROPIC_API_KEY is not set")
        try:
            import anthropic
        except ImportError as exc:
            raise LLMUnavailable("the anthropic package is not installed") from exc

        client = anthropic.Anthropic(timeout=TIMEOUT_S, max_retries=MAX_RETRIES)
        user = utterance if not context else f"{utterance}\n\nCurrent view: {json.dumps(context)}"

        # ⚠️ THESE THREE ARE ANNOTATED WITH THE SDK'S OWN PARAM TYPES rather than left as
        # bare dict literals. Inline, the type checker infers `dict[str, Collection[str]]`
        # from the literal and no overload of `create()` matches, so the call reports as an
        # error while being perfectly correct at runtime. Naming the types makes the
        # checker agree AND means a future SDK change to any of these shapes is caught here
        # instead of at the first live call.
        system: list[TextBlockParam] = [
            # 🔑 The system prompt carries every tool schema and is byte-identical on every
            # request, so it is worth caching. This model's minimum cacheable prefix is 512
            # tokens, which the tool list clears comfortably.
            {"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}
        ]
        output_config: OutputConfigParam = {
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": response_schema()},
        }
        messages: list[MessageParam] = [{"role": "user", "content": user}]

        started = time.perf_counter()
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                output_config=output_config,
                messages=messages,
            )
        except Exception as exc:  # noqa: BLE001 - any transport failure is "tier 2 unavailable"
            raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc

        elapsed_ms = int((time.perf_counter() - started) * 1000)

        # ⚠️ CHECK stop_reason BEFORE READING content. This model's safety classifiers can
        # decline a request, which arrives as a normal 200 with an empty content list.
        # Indexing content[0] first turns a refusal into an IndexError.
        if response.stop_reason == "refusal":
            raise LLMUnavailable("the model declined this request")

        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise LLMUnavailable(f"no text in response (stop_reason={response.stop_reason})")

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMUnavailable(f"response was not valid JSON: {text[:200]}") from exc

        return Selection(
            data_tool=parsed.get("data_tool", "none"),
            view_tool=parsed.get("view_tool", "none"),
            params={
                k: v
                for k, v in parsed.items()
                if k not in ("data_tool", "view_tool", "reasoning") and v not in ("", None)
            },
            reasoning=parsed.get("reasoning", ""),
            usage=usage_and_cost(response.usage, elapsed_ms, self.model),
        )


def usage_and_cost(usage: Any, elapsed_ms: int, model: str) -> dict[str, Any]:
    """Token counts and what they cost, in one place.

    🔑 REPORTED PER CALL, NOT AGGREGATED. The architectural claim is that the model runs
    only when the parser cannot answer; with per-call cost logged beside the `tier`
    column, "the model is only called when it earns its latency" becomes a query over the
    audit log rather than a sentence in a README.

    ⚠️ `getattr` with defaults throughout: cache fields are absent on responses that did
    not touch the cache, and a missing attribute must read as zero rather than raise
    inside a cost calculation.
    """
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0

    # 🔒 AN UNKNOWN MODEL COSTS None, NOT ZERO. A model missing from the table is a model
    # nobody priced, and reporting that as $0.00 would put a free call in the log that
    # never happened. The counts are still recorded, so the row stays useful.
    rates = PRICES.get(model)
    if rates is None:
        cost = None
    else:
        price_in, price_out = rates
        cost = (
            inp * price_in
            + out * price_out
            + cache_read * price_in * CACHE_READ_MULTIPLIER
            + cache_write * price_in * CACHE_WRITE_MULTIPLIER
        )

    return {
        "model": model,
        "effort": EFFORT,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        # Six decimal places because a single call costs a fraction of a cent, and
        # rounding to four would report most of them as zero.
        "cost_usd": None if cost is None else round(cost, 6),
        "latency_ms": elapsed_ms,
    }


def default_provider() -> Provider:
    """The real provider when a key exists, the replay provider otherwise.

    🔒 Falls back rather than failing, so a developer with no key still gets a working
    tier 1 and an honest "tier 2 unavailable" on anything the parser cannot handle.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicProvider()
    return ReplayProvider()
