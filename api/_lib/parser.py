"""Tier 1: the deterministic tier, and the language it answers.

WHY A PARSER SITS IN FRONT OF THE MODEL. Most of what an operator types is not ambiguous.
"show me the drones" has one meaning, and sending it to a language model costs a network
round trip, a few cents, a variable amount of latency and a small chance of a wrong answer, in
exchange for nothing.

So the declared sentences are matched here: instant, free, offline, and deterministic. The
model is for the language this cannot handle, which is the language that actually needs it:
compound requests, vague references, and phrasings nobody anticipated.

🔑 IT EMITS THE IDENTICAL PLAN SCHEMA THE MODEL EMITS. That is the property that makes the
two-tier idea one system rather than two wearing one name: the validator, the executor and the
audit log cannot tell which tier produced a plan, and the `tier` column is what records which
one did. That makes "the model is only called when it earns its latency" a query rather than a
claim.

🔑 THE LANGUAGE ITSELF LIVES IN `grammar.py`, AND THIS FILE IS ONLY THE DOOR. That split is
the whole change: this module used to be thirty ordered `re.search` branches, each matching its
keywords anywhere in the sentence, and a rule that matches part of an utterance answers part of
a question while looking like it answered all of it. Every sentence tier 1 accepts is now
declared and anchored, so a partial match cannot exist to be guarded against.

⚠️ RETURNS None WHEN THE SENTENCE IS NOT IN THE LANGUAGE, and never guesses. A parser that
half-matches is worse than one that declines, because it steals the utterances the model should
have had.
"""
from __future__ import annotations

import re
from typing import Any

from . import grammar

# Re-exported because the transcription hints are built from the words an operator can say,
# and the kind lexicon is half of that. One declaration, in `grammar`.
KIND_WORDS = grammar.KIND_WORDS


def parse(utterance: str) -> list[dict[str, Any]] | None:
    """An utterance to a plan, or None if this tier does not recognise it."""
    found = grammar.match(utterance)
    return found.steps if found else None


# ⚠️ THE MOST NATURAL THINGS TO ASK AN ARCTIC MAP INCLUDE THINGS THIS SYSTEM DOES NOT DO,
# and they are what someone types first. A blank stare is the worst possible answer, so they
# are recognised explicitly and refused with what IS available. Recognising a request in order
# to decline it is a different act from failing to understand it, and the log records it as
# such.
UNSUPPORTED: list[tuple[str, str]] = [
    # 🔴 THIS LIST SHRANK, AND KEEPING IT HONEST IS THE WHOLE POINT OF HAVING IT. It used to
    # refuse weather and air traffic outright. Both refusals were correct when they were
    # written and both went stale underneath us: the world now carries aircraft as real
    # assets, and measured sea ice is exactly the environmental overlay someone asking for
    # "weather" wants to see.
    #
    # ⚠️ A REFUSAL THAT OUTLIVES THE GAP IT DESCRIBED IS WORSE THAN NO REFUSAL, because it
    # turns a capability the system HAS into one it insists it does not, and it does so in a
    # confident sentence. These fire before the grammar is consulted, so a stale entry here
    # silently outranks a working command.
    #
    # The test for anything in this list is not "did we build it" but "can the system answer
    # it today", and that answer changes without this file being touched.
    (
        r"\bads-?b\b|\bair traffic control\b|\bflight (?:plan|number)\b|\bcallsign lookup\b",
        # ⚠️ THE SUGGESTED COMMAND IS PART OF THE CLAIM, so it has to be one that actually
        # answers. The suite runs every suggestion below through the grammar for that reason.
        "there is no live air traffic feed here, and nothing on this display fetches "
        "anything at runtime, deliberately. The aircraft on this map are the ones in the "
        'world itself; try "list the aircraft"',
    ),
    (
        r"\bforecast\b|\bwind speed\b|\btemperature\b|\bprecipitation\b|\bstorm\b",
        "there is no weather forecast here. What this display has is measured sea "
        'ice concentration; try "overlay the ice"',
    ),
]


def unsupported(utterance: str) -> str | None:
    text = utterance.lower()
    for pattern, reply in UNSUPPORTED:
        if re.search(pattern, text):
            return reply
    return None


# --------------------------------------------------------------------------
# What tier 1 did, and why it handed over when it did
# --------------------------------------------------------------------------

#: The first word of every declared sentence, mapped to the sentences that open with it. Used
#: only to say something useful when tier 1 declines.
_OPENINGS: dict[str, list[str]] = grammar.openings()


def _declined(text: str) -> str:
    """Why this tier passed, in the terms an operator can act on.

    🔑 "NOT RECOGNISED" IS TRUE AND USELESS. The one thing worth knowing is whether the
    sentence was close to a command that exists, because that is the difference between "say
    it the declared way and it costs nothing" and "this genuinely needs the model". Naming the
    nearest declared sentence is also the reference card arriving at the moment it is wanted,
    which is the same teaching the escalated answer already does with `teach`.
    """
    first = text.split()[0] if text.split() else ""
    near = _OPENINGS.get(first)
    if near:
        return f'not the declared phrasing; the deterministic tier answers "{near[0]}"'
    return "no declared command reads like this, so the reasoning layer has it"


def trace(utterance: str, plan: list[dict[str, Any]] | None) -> dict[str, Any]:
    """What tier 1 decided, and which declared sentence it decided it from.

    🔴 WHAT THIS USED TO BE, AND WHY IT IS SMALLER. It used to compute an `ignored` list: the
    words of the utterance that appeared nowhere in the plan, which was the only way to see
    that a branch had answered half a question.

        "show me all unkown parties"          -> the ground parties
        "show me all unkown parties on foot"  -> the SAME ground parties

    Both dropped "unkown", the second also dropped "on foot", and neither answer admitted it.
    Deriving the list needed a filler vocabulary of about two hundred words plus a table of
    verbs that only counted as used when the plan honoured them, and it went wrong in both
    directions: a word missing from the filler list escalated a command tier 1 had answered
    perfectly, and a word wrongly in it hid a real omission.

    🔑 AN EXACT GRAMMAR REMOVES THE QUESTION RATHER THAN ANSWERING IT BETTER. A declared
    sentence is anchored to the whole utterance, so a match accounts for every word by
    construction and a non-match produces no plan at all. There is nothing left to report as
    dropped, and the two hundred word list is deleted rather than maintained.

    ⚠️ SO THE HONEST FIELDS CHANGED, AND `ignored` IS GONE FROM NEW ROWS. Historical audit rows
    still carry it and the panel still renders it; nothing is rewritten, because what those
    rows say about what happened then is still true.
    """
    text = " ".join(utterance.lower().split())
    if not plan:
        return {
            "tier": "parser",
            "matched": None,
            "grammar": None,
            "extracted": {},
            "declined": _declined(text),
        }

    found = grammar.match(utterance)
    tools_matched = [str(step.get("tool", "")) for step in plan]

    # 🔴 THE PARAMETERS IT INFERRED, WHICH ARE THE HALF NOBODY CAN SEE. The transcript already
    # shows what was heard, so a misread word is visible on screen. What is not visible is what
    # this tier then made of it: a voice test heard "which assets are overdue" as "Hydrophone
    # Lancaster Sound 01 overdue", and the difference between the right answer and the wrong
    # one was an unannounced `kind="hydrophone"`.
    #
    # Merged across steps, because the operator is being shown what the sentence was understood
    # to mean rather than a per-step breakdown they did not ask for. Placeholders are shown
    # UNRESOLVED on purpose: `__viewport__` is what this tier actually decided, and the
    # resolving happens later and elsewhere.
    extracted: dict[str, Any] = {}
    for step in plan:
        for key, value in (step.get("params") or {}).items():
            if key in extracted and _placeholder(value) and not _placeholder(extracted[key]):
                continue
            extracted[key] = value

    return {
        "tier": "parser",
        "matched": tools_matched[0] if len(tools_matched) == 1 else tools_matched,
        # Which declared sentence answered. The one field an operator can do something with:
        # it is exactly what the reference card prints, so a trace teaches the card.
        "grammar": found.sentence if found else None,
        "extracted": extracted,
        "declined": None,
    }


def _placeholder(value: Any) -> bool:
    """⚠️ A PLACEHOLDER NEVER OVERWRITES A REAL VALUE in the merged view. "Isolate Daymark 01"
    resolves its own subject, so the later steps carry `__subject__` on the same key the first
    step carried "daymark 01". Letting the last write win hid the only part the operator would
    recognise behind an internal token."""
    if isinstance(value, str):
        return value.startswith("__") and value.endswith("__")
    if isinstance(value, list):
        return bool(value) and all(_placeholder(v) for v in value)
    return False
