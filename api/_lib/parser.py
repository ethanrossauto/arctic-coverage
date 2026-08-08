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

from .executor import VIEWPORT

# Asset kinds as an operator would say them, mapped to what the database calls them.
# Plurals and the obvious synonyms, because "drones" and "uas" are the same request.
KIND_WORDS: dict[str, str] = {
    "node": "node", "nodes": "node", "sensor": "node", "sensors": "node",
    "mesh node": "node", "mesh nodes": "node",
    "patrol": "patrol", "patrols": "patrol", "ranger": "patrol", "rangers": "patrol",
    # 🔴 "aircraft" USED TO MEAN "uas" HERE, and it stopped being true when aircraft
    # became a kind of their own. "Show me the aircraft" returned the drones: a confident
    # answer to a different question, from a synonym that was correct when the world had
    # only one thing that flew.
    "drone": "uas", "drones": "uas", "uas": "uas",
    "aircraft": "aircraft", "plane": "aircraft", "planes": "aircraft",
    "marker": "marker", "markers": "marker", "pin": "marker", "pins": "marker",
    "ground party": "ground_party", "ground parties": "ground_party",
    "party": "ground_party", "parties": "ground_party", "team": "ground_party",
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


_UNIT_MINUTES: dict[str, int] = {
    "minute": 1, "minutes": 1, "min": 1, "mins": 1,
    "hour": 60, "hours": 60, "hr": 60, "hrs": 60,
    "day": 1440, "days": 1440,
    "week": 10080, "weeks": 10080,
}

_UNITS = r"minutes?|mins?|hours?|hrs?|days?|weeks?"

# The phrases that carry a duration without carrying a number, which is how people
# actually ask. "The last day" is far commoner than "1 day".
_NAMED_WINDOWS: list[tuple[str, int]] = [
    (r"\b(?:this|the last|last|past) week\b", 10080),
    (r"\b(?:the last|last|past|today|the) day\b", 1440),
    # A 48-hour window is what actually contains yesterday, since "yesterday" from this
    # afternoon means a day that ended this morning.
    (r"\byesterday\b", 2880),
    (r"\b(?:this|the last|last|past) month\b", 43200),
]


def _window_minutes(text: str) -> int:
    """How far back an utterance is asking, defaulting to a day."""
    m = re.search(rf"\b(\d+(?:\.\d+)?)\s*({_UNITS})\b", text)
    if m:
        return max(1, int(float(m.group(1)) * _UNIT_MINUTES[m.group(2)]))
    for pattern, minutes in _NAMED_WINDOWS:
        if re.search(pattern, text):
            return minutes
    return 1440


def _history_target(text: str) -> str | None:
    """Which asset a history request is about, with the time words taken back out.

    ⚠️ THE DURATION AND THE TARGET ARE INTERLEAVED IN REAL SENTENCES, which is why this
    strips rather than splits: "history for Daymark 03 over the last 3 days" puts the
    window on the far side of the name from "4 days of history for Daymark 03". Pulling
    the time words out of whatever was captured handles both without a phrasing table.
    """
    for pattern in (
        r"\bhistor(?:y|ic|ical)\b(?:\s+(?:for|of|on))?\s+(.+)",
        r"\bwhere (?:has|have)\s+(.+?)\s+(?:been|gone)",
        r"^(?:show me |show |give me |get )?(?:the )?(.+?)(?:'s)?\s+histor(?:y|ical)\b",
    ):
        m = re.search(pattern, text)
        if not m:
            continue
        target = re.sub(rf"\b(?:over|in|during|for)?\s*(?:the\s+)?(?:last|past|previous)\s+\d*\s*(?:{_UNITS})\b", " ", m.group(1))
        target = re.sub(rf"\b\d+(?:\.\d+)?\s*(?:{_UNITS})\b", " ", target)
        for named, _ in _NAMED_WINDOWS:
            target = re.sub(named, " ", target)
        # ⚠️ "location" and its friends are noise words in a history request, and leaving
        # them in produced targets like "location this asset" that resolve to nothing.
        # Stripping them lets a deictic phrase survive intact for the context resolver.
        target = re.sub(
            r"\b(?:of|for|the|over|please|position|positions|location|locations|track|history)\b",
            " ",
            target,
        )
        target = " ".join(target.split()).strip(" ?,.")
        if target:
            return target
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
    # ⚠️ "dark" IS MATCHED AS AN IDIOM, NEVER AS A BARE WORD. It used to be bare, and in
    # an application about the Arctic that is a collision with the ordinary meaning:
    # "which of these could reach the contact before it gets dark" came back as the list
    # of vessels holding no AIS broadcast. A confident answer to a question nobody asked
    # is worse than declining, because declining sends the utterance to the tier that
    # could have handled it.
    if re.search(
        r"\bnot broadcasting\b|\bno ais\b|\bnot reporting ais\b"
        r"|\b(?:gone|going|went|goes) dark\b|\bdark (?:vessels?|contacts?|ships?)\b",
        text,
    ):
        return plan("list_entities", not_broadcasting=True)

    # --- overdue ----------------------------------------------------------
    # 🔴 ITS OWN BRANCH, BECAUSE IT USED TO BE AN ALIAS FOR THE CONDITION FILTER AND THEY
    # ARE NOT THE SAME QUESTION. Overdue is about silence: nothing has been heard from
    # this asset inside the interval its kind reports on. Condition is about the asset
    # itself, and an unserviceable asset can be beaconing happily every thirty seconds.
    # Assets sit in one set and not the other, so the alias made the app's own suggested
    # command return an answer that disagreed with the count in the strip next to it.
    if re.search(
        r"\boverdue\b|\bgone quiet\b|\bstopped reporting\b|\bnot reported\b|\bhaven'?t reported\b",
        text,
    ):
        return plan("list_entities", overdue=True, kind=_kind(text))

    # --- coverage ---------------------------------------------------------
    if re.search(
        r"\bnot seeing\b|\bwhat are we missing\b|\bundetected\b|\buntracked\b"
        r"|\bcoverage\b|\bblind spots?\b|\bwho is not being tracked\b",
        text,
    ):
        return plan("coverage")

    # --- break and repair ---------------------------------------------------
    # 🔑 THE VOCABULARY IS WHAT AN OPERATOR SAYS, NOT WHAT THE TOOL IS CALLED. Nobody types
    # "inject a fault"; they say kill it, break it, take it down. Recognising the words
    # people actually reach for is most of what tier 1 is for, and every one of these that
    # lands here is a command answered in milliseconds instead of seconds.
    m = re.search(
        r"\b(?:kill|break|fail|disable|take down|knock out|silence|black out)\b\s+(?:the\s+)?(.+)",
        text,
    )
    if m:
        return plan("inject_fault", target=m.group(1).strip(" ?"), fault="silent")

    m = re.search(
        r"\b(?:unserviceable|out of service|into maintenance|in for maintenance)\b"
        r"(?:\s+(?:on|for))?\s+(?:the\s+)?(.+)|"
        r"\b(?:take|put|mark)\b\s+(?:the\s+)?(.+?)\s+"
        r"\b(?:unserviceable|out of service|into maintenance)\b",
        text,
    )
    if m:
        target = (m.group(1) or m.group(2) or "").strip(" ?")
        if target:
            return plan("inject_fault", target=target, fault="maintenance")

    m = re.search(
        r"\b(?:fix|repair|restore|bring back|revive|clear the fault on|clear fault on)\b\s+(?:the\s+)?(.+)",
        text,
    )
    if m:
        return plan("clear_fault", target=m.group(1).strip(" ?"))

    # --- remove ---------------------------------------------------------------
    m = re.search(
        r"\b(?:remove|delete|get rid of|take off the map|scrub)\b\s+(?:the\s+)?(.+)", text
    )
    if m:
        return plan("remove_asset", target=m.group(1).strip(" ?"))

    # --- condition queries ------------------------------------------------
    # 🔑 THREE FLAGS, AND ONLY ONE OF THEM IS STORED HERE. `maintenance` and `nominal`
    # are conditions the world is in; `overdue` is a fact about the clock and is computed
    # above. "Down", "unserviceable" and "u/s" are what people say for the same thing.
    if re.search(r"\bmaintenance\b|\bunserviceable\b|\bu/s\b|\bdown\b|\bin the shop\b", text):
        return plan("list_entities", status="maintenance", kind=_kind(text))

    if re.search(r"\bnominal\b|\bhealthy\b|\bfine\b|\bok\b", text):
        return plan("list_entities", status="nominal", kind=_kind(text))

    # --- history ----------------------------------------------------------
    # Before the view and listing branches, because "show me 4 days of history for
    # Daymark 03" is a `show me ...` sentence and the loose listing branch would take it.
    if re.search(r"\bhistor(?:y|ic|ical)\b|\bwhere (?:has|have)\b|\bbeen over the\b", text):
        target = _history_target(text)
        if target:
            return plan(
                "entity_history",
                target=target,
                days=round(_window_minutes(text) / 1440.0, 4),
            )

    # --- view -------------------------------------------------------------
    # 🔴 A CAMERA COMMAND MUST NEVER REACH THE MODEL, and this branch was far too narrow
    # to guarantee it. "Zoom the map out completely" fell through to tier 2 and took 7.8
    # seconds, against 1.9 for anything the parser answers. That is the worst latency in
    # the product and it buys nothing: a camera request carries no domain question at all.
    # It resolves from a verb and at most a place name, so there is nothing for a
    # reasoning model to reason about.
    #
    # It is also the first thing anyone tries on a map, which makes it the first
    # impression of how fast the whole thing is.
    #
    # ⚠️ "show the whole arctic" USED TO BE WORSE THAN SLOW. It fell past here into the
    # loose listing branch and came back as `focus_entity(target="whole arctic")`, which
    # then failed to resolve. A wrong answer, not a decline, which is the exact failure
    # the module docstring warns about.
    if re.search(
        r"\b(reset|default|restore)\s+(the\s+)?(view|camera|map)\b"
        r"|\bzoom\s+(the\s+\w+\s+)?(all\s+the\s+way\s+)?out\b"
        r"|\bunzoom\b|\bback\s+out\b|\bpull\s+(back|out)\b"
        r"|\bshow\s+(me\s+)?(the\s+)?(whole|entire|full)\s+(arctic|map|picture|world|thing)\b"
        r"|\bshow\s+(me\s+)?everything\b|\bwide\s+view\b|\boverview\b"
        r"|\bfit\s+everything\b|\bwhole\s+arctic\b|\bdefault\s+view\b",
        text,
    ):
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

    # --- the current zoom window -------------------------------------------
    # 🔴 THE FIRST CAPABILITY THIS APPLICATION IS SUPPOSED TO HAVE, and it was answered
    # for a while by a wrong guess: "show me assets in the current zoom window" fell
    # through to the loose listing branch and came back as a search for an asset named
    # "assets in the current zoom window". The word the operator leans on hardest, "here",
    # was the one the system understood least.
    #
    # The bbox itself is not known to this layer and must not be: the parser stays a pure
    # function of the words, and `executor.resolve_context` swaps the placeholder for what
    # is actually on screen.
    if re.search(
        r"\b(?:current\s+)?(?:zoom\s+window|viewport|current\s+view|this\s+view"
        r"|on\s+screen|in\s+view|visible|current\s+window|here)\b",
        text,
    ):
        # "active flights" is what an operator says for what this world stores as
        # aircraft. Refusing it was correct when none existed; four do now.
        if re.search(r"\bflights?\b|\baircraft\b|\bin the air\b|\bairborne\b", text):
            return plan("list_entities", kind="aircraft", bbox=VIEWPORT)
        if re.search(r"\bweather\b|\bice\b|\boverlays?\b|\bconditions?\b", text):
            return plan("show_overlay", layer="ice")
        return plan("list_entities", kind=_kind(text), bbox=VIEWPORT)

    # --- environmental overlays ---------------------------------------------
    # Asked for without a viewport phrase too: "show me the ice" is the same request as
    # "show me the ice over the current window", because an overlay covers what is drawn.
    if re.search(r"\b(?:overlays?|ice|sea ice)\b", text) and re.search(
        r"\bshow\b|\bdisplay\b|\bturn on\b|\bwhere is\b", text
    ):
        return plan("show_overlay", layer="ice")

    # --- serialized actions -------------------------------------------------
    # 🔑 ONE REQUEST, SEVERAL ACTIONS, which is a named requirement and was supported by
    # the executor while nothing ever produced it: every branch in this file returned
    # exactly one step. "Isolate" is the word an operator uses for the whole sequence at
    # once, and it expands into the four the requirement names: filter the picture to that
    # kind, frame it, select it, and open its detail.
    m = re.search(r"\b(?:isolate|just show me|only show me|focus in on)\b\s+(?:the\s+)?(.+)", text)
    if m:
        target = m.group(1).strip(" ?")
        kind = _kind(target)
        steps: list[dict[str, Any]] = []
        if kind:
            steps.append({"tool": "list_entities", "params": {"kind": kind}})
            steps.append({"tool": "frame_entities", "params": {"kind": kind}})
        else:
            steps.append({"tool": "focus_entity", "params": {"target": target}})
            steps.append({"tool": "describe_entity", "params": {"target": target}})
        return steps

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
    # 🔴 THIS LIST SHRANK, AND KEEPING IT HONEST IS THE WHOLE POINT OF HAVING IT. It used
    # to refuse weather and air traffic outright. Both refusals were correct when they
    # were written and both went stale underneath us: the world now carries aircraft as
    # real assets, and measured sea ice is exactly the environmental overlay someone
    # asking for "weather" wants to see.
    #
    # ⚠️ A REFUSAL THAT OUTLIVES THE GAP IT DESCRIBED IS WORSE THAN NO REFUSAL, because it
    # turns a capability the system HAS into one it insists it does not, and it does so in
    # a confident sentence. These fire before the parser runs, so a stale entry here
    # silently outranks a working command.
    #
    # The test for anything in this list is not "did we build it" but "can the system
    # answer it today", and that answer changes without this file being touched.
    (
        r"\bads-?b\b|\bair traffic control\b|\bflight (?:plan|number)\b|\bcallsign lookup\b",
        "there is no live air traffic feed in this build, and nothing here fetches "
        "anything at runtime, deliberately. The aircraft on this map are the ones in the "
        'world itself; try "show active flights in the zoom window"',
    ),
    (
        r"\bforecast\b|\bwind speed\b|\btemperature\b|\bprecipitation\b|\bstorm\b",
        "there is no weather forecast in this build. What is available is measured sea "
        'ice concentration; try "show me the ice overlay"',
    ),
]


def unsupported(utterance: str) -> str | None:
    text = utterance.lower()
    for pattern, reply in UNSUPPORTED:
        if re.search(pattern, text):
            return reply
    return None
