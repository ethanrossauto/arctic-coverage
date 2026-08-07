"""Tier 1: the deterministic parser.

WHY A PARSER SITS IN FRONT OF THE MODEL. Most of what an operator types is not ambiguous.
"show me the drones" has one meaning, and sending it to a language model costs a network
round trip, a few cents, a variable amount of latency and a small chance of a wrong
answer, in exchange for nothing.

So the common shapes are matched here: instant, free, offline, and deterministic. The
model is for the language this cannot handle, which is the language that actually needs
it: compound requests, vague references, and phrasings nobody anticipated.

🔑 IT EMITS THE IDENTICAL PLAN SCHEMA THE MODEL EMITS. That is the property that makes
the whole two-tier idea work rather than being two systems wearing one name: the
validator, the executor and the audit log cannot tell which tier produced a plan, and
the `tier` column is what records which one did. That makes "the model is only called
when it earns its latency" a query rather than a claim.

⚠️ RETURNS None WHEN IT DOES NOT KNOW, and never guesses. A parser that half-matches is
worse than one that declines, because it steals the utterances the model should have
had.
"""
from __future__ import annotations

import re
from typing import Any

# Asset kinds as an operator would say them, mapped to what the database calls them.
# Plurals and the obvious synonyms, because "drones" and "uas" are the same request.
KIND_WORDS: dict[str, str] = {
    "node": "node", "nodes": "node", "sensor": "node", "sensors": "node",
    "mesh node": "node", "mesh nodes": "node",
    "patrol": "patrol", "patrols": "patrol", "ranger": "patrol", "rangers": "patrol",
    "drone": "uas", "drones": "uas", "uas": "uas", "aircraft": "uas",
    "hydrophone": "hydrophone", "hydrophones": "hydrophone",
    "vessel": "vessel", "vessels": "vessel", "ship": "vessel", "ships": "vessel",
    "contact": "vessel", "contacts": "vessel",
    "radar": "radar", "radars": "radar",
    "launch site": "launch_site", "launch sites": "launch_site",
    "base": "launch_site", "bases": "launch_site",
}

_COORD = r"(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)"


def _kind(text: str) -> str | None:
    """Longest match wins, so "mesh nodes" is not read as "nodes" inside a longer phrase."""
    for word in sorted(KIND_WORDS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(word)}\b", text):
            return KIND_WORDS[word]
    return None


def parse(utterance: str) -> list[dict[str, Any]] | None:
    """An utterance to a plan, or None if this tier does not recognise it."""
    text = " ".join(utterance.lower().split())
    if not text:
        return None

    def plan(tool: str, **params: Any) -> list[dict[str, Any]]:
        return [{"tool": tool, "params": {k: v for k, v in params.items() if v is not None}}]

    # --- connectivity -----------------------------------------------------
    if re.search(r"\b(mesh|connectivity|network) (status|state)\b", text) or re.search(
        r"\bwhich (clusters?|groups?) are (cut off|isolated|disconnected)\b", text
    ):
        return plan("mesh_status")

    if re.search(r"\b(isolated|cut off|no mesh|off the mesh|unreachable)\b", text):
        return plan("list_entities", isolated=True, kind=_kind(text))

    # --- the flagship query ----------------------------------------------
    if re.search(r"\bnot broadcasting\b|\bno ais\b|\bdark\b|\bnot reporting ais\b", text):
        return plan("list_entities", not_broadcasting=True)

    # --- status queries ---------------------------------------------------
    for word, status in (("silent", "silent"), ("degraded", "degraded"), ("overdue", "degraded")):
        if re.search(rf"\b{word}\b", text):
            return plan("list_entities", status=status, kind=_kind(text))

    # --- view -------------------------------------------------------------
    if re.search(r"\b(reset|default) (the )?view\b|\bzoom out\b|\bshow everything\b", text):
        return plan("reset_view")

    m = re.search(r"\b(?:frame|fit|show all|show me all)\b\s+(?:the\s+)?(.+)", text)
    if m and _kind(m.group(1)):
        return plan("frame_entities", kind=_kind(m.group(1)))

    # --- place ------------------------------------------------------------
    m = re.search(rf"\b(?:place|drop|put)\b.*?\b(marker|node|hydrophone|launch site)\b.*?{_COORD}", text)
    if m:
        return plan(
            "place_asset",
            kind=KIND_WORDS.get(m.group(1), m.group(1).replace(" ", "_")),
            lat=float(m.group(2)),
            lon=float(m.group(3)),
        )

    # --- task a drone -----------------------------------------------------
    m = re.search(rf"\b(?:send|task|move|fly)\b\s+(.+?)\s+(?:to|toward)\s+{_COORD}", text)
    if m:
        alt = re.search(r"\bat\s+(\d+)\s*(?:m|metres|meters)\b", text)
        return plan(
            "task_uas",
            target=m.group(1).strip(),
            lat=float(m.group(2)),
            lon=float(m.group(3)),
            altitude_m=float(alt.group(1)) if alt else None,
        )

    # --- describe / focus -------------------------------------------------
    m = re.search(r"\b(?:tell me about|describe|what is|details? (?:on|for))\s+(?:the\s+)?(.+)", text)
    if m:
        return plan("describe_entity", target=m.group(1).strip(" ?"))

    m = re.search(r"\b(?:focus|select|go to|centre on|center on|zoom to)\s+(?:on\s+)?(?:the\s+)?(.+)", text)
    if m:
        return plan("focus_entity", target=m.group(1).strip(" ?"))

    # --- plain listing, last because it is the loosest -------------------
    m = re.search(r"\b(?:show|list|where are|find)\b\s+(?:me\s+)?(?:the\s+|all\s+)*(.+)", text)
    if m:
        kind = _kind(m.group(1))
        if kind:
            return plan("list_entities", kind=kind)
        # A show/list of something that is not a kind is probably one asset by name.
        return plan("focus_entity", target=m.group(1).strip(" ?"))

    # Bare kind, e.g. someone types just "drones".
    if _kind(text) and len(text.split()) <= 3:
        return plan("list_entities", kind=_kind(text))

    return None


# ⚠️ THE MOST NATURAL THINGS TO ASK AN ARCTIC MAP INCLUDE THINGS THIS SYSTEM DOES NOT DO,
# and they are what someone types first. A blank stare is the worst possible answer, so
# they are recognised explicitly and refused with what IS available. Recognising a request
# in order to decline it is a different act from failing to understand it, and the log
# records it as such.
UNSUPPORTED: list[tuple[str, str]] = [
    (
        r"\bweather\b|\bforecast\b|\bwind\b|\bice chart\b",
        "there is no weather layer in this build. It would need external raster tiles, and "
        "nothing here fetches anything at runtime, deliberately. What is available: mesh "
        "connectivity, asset status, and the contacts picture",
    ),
    (
        r"\bflights?\b|\bair traffic\b|\bads-?b\b",
        "live air traffic is not in this build. The aircraft here are the tasked drones; "
        "try \"show me the drones\" or \"send Daymark 05 to 73.0 -95.9\"",
    ),
]


def unsupported(utterance: str) -> str | None:
    text = utterance.lower()
    for pattern, reply in UNSUPPORTED:
        if re.search(pattern, text):
            return reply
    return None
