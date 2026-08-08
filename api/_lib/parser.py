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

from .executor import RESULT, SUBJECT, VIEWPORT

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
    # ⚠️ "flight" AND "flights" LIVE HERE NOW, AND THEY DID NOT. One branch below already
    # knew that "active flights" means the aircraft, so the command worked; this table did
    # not, so `trace` could not account for the word and reported it as one the parser had
    # thrown away. That sent a command tier 1 answers perfectly correctly off to the model,
    # on the app's own suggested example. A synonym known to one branch and not to the
    # shared table is the same drift that made "aircraft" mean "uas" for a week.
    "aircraft": "aircraft", "plane": "aircraft", "planes": "aircraft",
    "flight": "aircraft", "flights": "aircraft",
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


def _looks_plural(text: str) -> bool:
    """Is this tail asking about several things rather than naming one?

    ⚠️ CRUDE ON PURPOSE, AND IT ONLY EVER CAUSES A DEFERRAL. A wrong answer here sends an
    utterance to tier 2 that tier 1 could have handled, which costs a few seconds and a
    fraction of a cent. The failure it prevents is a confident wrong answer, so the
    asymmetry is worth it and a cleverer rule would not be.

    ⛔ NOT a stemmer and never a spell-checker. `_resolve` stays exact for the reason the
    whole design rests on: the model is the component allowed to be uncertain.
    """
    last = text.split()[-1] if text.split() else ""
    if not last.endswith("s") or last.endswith(("ss", "us", "is")):
        return False
    # An asset genuinely named "... 05s" is not a thing, but a trailing digit means the
    # operator is naming a unit, so leave those alone.
    return not any(ch.isdigit() for ch in last)


def parse(utterance: str) -> list[dict[str, Any]] | None:
    """An utterance to a plan, or None if this tier does not recognise it."""
    text = " ".join(utterance.lower().split())
    if not text:
        return None

    def plan(tool: str, **params: Any) -> list[dict[str, Any]]:
        return [{"tool": tool, "params": {k: v for k, v in params.items() if v is not None}}]

    # --- more than one action in one sentence -----------------------------
    # 🔴 EVERY BRANCH BELOW ANSWERS A SINGLE INTENT AND RETURNS ON THE FIRST MATCH, so an
    # utterance naming two actions gets whichever one happens to be checked first and the
    # rest is dropped without a word. "Show me everything that has gone quiet, then put the
    # camera back" came back as the overdue list, with the second half silently gone.
    #
    # A partial answer is the worst of the three outcomes available here. It is not a
    # refusal the operator can react to and it is not the answer, it just looks like the
    # answer. Declining sends the utterance to the tier that can serialize it, which is
    # the same argument the "dark" idiom below is written on.
    #
    # ⚠️ SEQUENCING WORDS ONLY, NEVER A BARE "and". "Show me the nodes and the drones" is
    # one request with two filters, not two requests, and deferring it would push a
    # perfectly good tier-1 answer onto a model for nothing. What is matched here is a
    # word that orders one action after another.
    if re.search(
        r"\band then\b|\bafter that\b|\bafterwards?\b|\bfollowed by\b|,\s*then\b"
        r"|\bthen\b\s+(?:open|show|list|find|focus|frame|select|reset|describe"
        r"|task|send|place|drop|remove|delete|inject|clear|zoom|centre|center)\b",
        text,
    ):
        return None

    # --- connectivity -----------------------------------------------------
    if re.search(r"\b(mesh|connectivity|network) (status|state)\b", text) or re.search(
        r"\bwhich (clusters?|groups?) are (cut off|isolated|disconnected)\b", text
    ):
        return plan("mesh_status")

    if re.search(r"\b(isolated|cut off|no mesh|off the mesh|unreachable)\b", text):
        return plan("list_entities", isolated=True, kind=_kind(text))

    # --- unidentified contacts --------------------------------------------
    # 🔑 AN UNKNOWN IS A CONTACT THAT IS NOT ANNOUNCING WHO IT IS, which the detection
    # layer already answers for vessels, aircraft and ground parties alike. This must sit
    # ABOVE the listing branches: "show me the unknowns" otherwise falls through to the
    # loose rule, matches no kind, and widens to the whole world.
    #
    # ⚠️ "unknown" IS NOT MATCHED AS A BARE WORD INSIDE A LONGER QUESTION, the same care
    # "dark" needed below. "I do not know" and "unknown position" are ordinary English in
    # this domain and are not requests for the contact list.
    if re.search(
        r"\bunknowns?\b|\bunidentified\b|\bunknown (?:contacts?|vessels?|aircraft|parties)\b"
        r"|\bwho is out there\b|\bnot (?:identified|squawking)\b",
        text,
    ) and not re.search(r"\b(?:do not|don't|dont|cannot|can't) know\b", text):
        return plan("show_unknown")

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
    #
    # ⚠️ "down" IS MATCHED PREDICATIVELY, NEVER AS A BARE WORD, for the same reason "dark"
    # is above. Bare, it read "narrow it down to the drones" as a maintenance filter and
    # answered with one asset, confidently and wrongly. "Down" is a direction and half of
    # a dozen phrasal verbs before it is ever a condition, so it counts only where it is
    # saying something IS down: after a form of "be", after an interrogative or a
    # quantifier, or at the end of the sentence. "down to" is excluded outright, which is
    # the shape that actually bit.
    if re.search(
        r"\bmaintenance\b|\bunserviceable\b|\bu/s\b|\bin the shop\b"
        r"|\b(?:is|are|was|were|any|anything|everything|what|whats|what's|which|show me what)\b"
        r"(?:\s+\S+){0,3}\s+down\b(?!\s+to\b)"
        r"|\bdown\b(?!\s+to\b)\s*[?.!]?$",
        text,
    ):
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
        r"\bshow\b|\bdisplay\b|\bturn on\b|\bwhere is\b|\bopen\b|\bpull up\b|\bbring up\b", text
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
            # 🔴 FOUR ACTIONS FROM ONE REQUEST, and the middle one is why `SUBJECT` exists.
            # "Isolate Daymark 01" means all of: put the camera on it, filter the picture to
            # it, select it, and open its detail. `focus_entity` does the camera and the
            # selection; `describe_entity` opens the detail; the FILTER needs the asset's
            # id, and this file only ever had the words "daymark 01".
            #
            # So the first step resolves the name, and the two after it refer to whatever it
            # resolved. Three existing tools, no new capability, and the plan reads in the
            # order an operator would describe it.
            steps.append({"tool": "focus_entity", "params": {"target": target}})
            steps.append({"tool": "list_entities", "params": {"ids": [SUBJECT]}})
            steps.append({"tool": "describe_entity", "params": {"target": SUBJECT}})
        return steps

    # --- the previous answer ----------------------------------------------
    # 🔴 "LIST THEM" USED TO RETURN THE WHOLE WORLD. "Them" reached no branch, fell through
    # to the loose listing rule below, matched no kind, and widened to everything:
    #
    #   > how many unknown parties on foot
    #   · 3 matching
    #   > list them
    #   · 76 matching
    #
    # Answering a narrower question with a wider answer is the worst shape of wrong
    # available here, because 76 looks like a working command.
    #
    # 🔑 THE PLACEHOLDER CARRIES IT, NOT THIS FILE. The parser has no idea what the last
    # command returned and must not: it works from words alone, which is what keeps it
    # testable with no browser and no database. `executor.resolve_context` is the one place
    # live state is bound, exactly as it already is for "this" and the current window.
    m = re.search(
        r"\b(?:list|show|frame|focus on|describe|zoom to|go to|centre on|center on)\b"
        r"\s+(?:me\s+)?(?:all\s+)?(?:of\s+)?"
        r"(them|those|these|the last lot|the last ones|that list|the list|the results)\b",
        text,
    )
    if m:
        if re.search(r"\b(?:frame|zoom to|go to|centre on|center on)\b", text):
            return plan("frame_entities", targets=RESULT)
        return plan("list_entities", ids=RESULT)

    # --- plain listing, last because it is the loosest -------------------
    # ⚠️ "open" AND "pull up" ARE HERE BECAUSE THEY ARE HOW PEOPLE ASK, and their absence
    # was costing a model call for the most ordinary phrasing in the app. "Open daymark" is
    # a single asset by name; it fell through every branch to tier 2, which spent several
    # seconds arriving at the same `describe_entity` the deterministic tier could have
    # produced instantly. Tier 2 exists for language this cannot handle, and a common verb
    # is not that.
    m = re.search(
        r"\b(?:show|list|where are|find|open|pull up|bring up)\b\s+(?:me\s+)?(?:the\s+|all\s+)*(.+)",
        text,
    )
    if m:
        kind = _kind(m.group(1))
        if kind:
            return plan("list_entities", kind=kind)

        tail = m.group(1).strip(" ?")
        # 🔴 "PROBABLY ONE ASSET BY NAME" WAS A GUESS, AND THIS FILE IS NOT ALLOWED ONE.
        # The comment that used to sit here said "probably", which is the tell. Two ways
        # it went wrong, and only the second is about spelling:
        #
        #   "show me all unkowns"   -> focus_entity(target="all unkowns") -> matches nothing
        #   "show me all unknowns"  -> focus_entity(target="all unknowns") -> STILL WRONG
        #
        # ⚠️ THE SECOND ONE IS THE REAL BUG. Spelled correctly, that request is a FILTER
        # OVER MANY THINGS, and `focus_entity` is singular: the tool was wrong before the
        # typo was ever a factor. So a plural tail, or an utterance carrying "all", never
        # takes the singular fallback.
        #
        # Returning None is a fine answer. It is what sends the utterance to the tier that
        # is allowed to be uncertain, which is the whole contract this file opens with.
        if re.search(r"\ball\b", text) or _looks_plural(tail):
            return None

        return plan("focus_entity", target=tail)

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
        # ⚠️ THE SUGGESTED COMMAND IS PART OF THE CLAIM, so it has to be one that actually
        # answers. This used to offer "show active flights in the zoom window", and
        # "active" is a filter this world does not model: the utterance is a partial match,
        # which now correctly goes to the model, so the refusal was quietly recommending
        # the slowest path in the app. A refusal that names what IS available should name
        # something the deterministic tier answers instantly.
        "there is no live air traffic feed in this build, and nothing here fetches "
        "anything at runtime, deliberately. The aircraft on this map are the ones in the "
        'world itself; try "show me the aircraft"',
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


# --------------------------------------------------------------------------
# What tier 1 did with each word
# --------------------------------------------------------------------------

# Words that carry no request on their own. A word here is never reported as ignored,
# because leaving it out changes nothing about what was asked.
#
# ⚠️ GENEROUS ON PURPOSE, AND THE ASYMMETRY IS WHY. A word missing from this list shows up
# as ignored, and an ignored word sends the utterance to tier 2: the cost of being too
# strict is latency and a fraction of a cent, paid on a request tier 1 could have answered.
# The cost of being too loose is a word silently dropped from a question, answered
# confidently, which is the failure this whole trace exists to expose. Err toward listing.
FILLER = {
    # asking
    "show", "shows", "showing", "display", "list", "find", "get", "give", "tell", "see",
    "where", "what", "whats", "which", "who", "how", "many", "much", "is", "are", "was",
    "were", "do", "does", "did", "can", "could", "would", "will", "let", "want", "need",
    "look", "looking", "check", "know", "there", "here", "any", "some", "all", "every",
    "please", "just", "now", "currently", "right", "still", "again", "also", "too",
    # glue
    "me", "my", "i", "we", "us", "you", "it", "its", "the", "a", "an", "of", "for", "to",
    "in", "on", "at", "by", "with", "from", "and", "or", "but", "that", "this", "these",
    "those", "them", "their", "they", "he", "she", "his", "her", "be", "been", "being",
    "have", "has", "had", "up", "out", "over", "about", "into", "onto", "as", "if", "then",
    "than", "so", "not", "no", "yes", "ok", "okay",
    # the domain's own generic nouns: they name the subject, never narrow it
    "asset", "assets", "entity", "entities", "thing", "things", "unit", "units", "one",
    "ones", "item", "items", "everything", "anything", "something", "map", "screen",
    "view", "picture", "world", "system", "status", "state", "info", "information",
    "detail", "details", "data", "lists",
    # command verbs: they choose the tool, and the tool name is already counted, but
    # not every verb appears in the name it selects ("send" picks `task_uas`)
    "send", "task", "move", "go", "fly", "put", "place", "drop", "add", "set", "make",
    "open", "focus", "centre", "center", "zoom", "frame", "select", "reset", "return",
    "remove", "delete", "take", "clear", "fix", "isolate", "narrow", "only", "down",
    "back", "start", "started", "rid", "kill", "pull", "bring", "break", "disable", "silence", "knock",
    "inject", "fault", "faults",
    # the viewport, which becomes a placeholder rather than a word in the plan
    "current", "window", "visible", "onscreen", "viewport",
    # time, which becomes a number of days rather than the words that expressed it
    "last", "past", "previous", "recent", "recently", "hour", "hours", "day", "days",
    "week", "weeks", "month", "months", "yesterday", "today", "since", "ago", "during",
    "history", "historic", "historical", "location", "locations", "position",
    "positions", "track", "tracks", "gone",
    # overlays: "weather" is deliberately answered with sea ice and says so, so it is
    # handled rather than dropped
    "overlay", "overlays", "weather", "layer", "layers",
    # the vocabulary `show_unknown` answers to. The tool's NAME covers "unknown" and
    # "unknowns"; these are the other ways people ask the same question, and without them
    # a command tier 1 answers correctly reads as a partial match and goes to the model.
    # "contact" and "contacts" sit here as generic domain nouns, beside "assets" and
    # "entities": where they really do mean vessels, `KIND_WORDS` puts `kind` on the plan
    # and the kind expansion consumes them there.
    "unidentified", "identified", "squawking", "contact", "contacts",
    # 🔴 "active" IS FILLER HERE BECAUSE THIS WORLD HAS NO INACTIVE SUBSET TO NARROW TO,
    # and the measurement settled it rather than the argument. "Show active flights in the
    # zoom window" is one of the app's own stated examples. Reported as a dropped word it
    # escalated to the model, which took 25 seconds, cost money, and came back with
    # `list_entities(kind=aircraft)` -- the SAME answer minus the viewport, because tier 1
    # had already applied the bbox and tier 2 had no reason to. Escalating bought a
    # strictly worse plan, slower, for money. A word that cannot narrow anything is not a
    # filter being dropped; it is an adjective.
    "active", "inactive", "live", "ongoing",
}


def _words(text: str) -> list[str]:
    """Tokens, with a NUMBER KEPT WHOLE.

    ⚠️ The number alternative comes first and matches the sign and the decimal point,
    because a naive word pattern turns "-95.9" into "95" and "9" and then reports both
    as words the parser ignored. A coordinate that reads as two dropped words is a
    false alarm on the one command shape where every character matters.
    """
    return re.findall(r"-?\d+(?:\.\d+)?|[a-z][a-z0-9'/-]*", text.lower())


def _variants(word: str) -> set[str]:
    """A word and its obvious plural, so `show_overlay` accounts for "overlays".

    ⚠️ Not a stemmer, and it must never become one. It exists so a singular tool name
    covers the plural an operator actually says; anything cleverer would start
    swallowing words that really were dropped, which is the one thing this must not do.
    """
    return {word, word + "s", word[:-1] if word.endswith("s") else word}


def trace(utterance: str, plan: list[dict[str, Any]] | None) -> dict[str, Any]:
    """What tier 1 matched, and which of the operator's words it threw away.

    🔴 THE `ignored` LIST IS THE POINT OF THIS FUNCTION. A deterministic parser fails in a
    particular way: it matches part of an utterance, answers that part, and presents the
    result as though it had answered all of it. Two requests that differ by three words
    come back byte-identical and nothing anywhere says why.

        "show me all unkown parties"          -> ground parties
        "show me all unkown parties on foot"  -> the same ground parties

    Both dropped "unkown", the second also dropped "on foot", and neither answer admitted
    it. Naming the discarded words turns a confident wrong answer into an obviously wrong
    one, which is the difference between a bug found in a demo and a bug found on a
    recording of the demo.

    🔑 COMPUTED FROM THE PLAN, NOT DECLARED BY EACH BRANCH. Instrumenting thirty branches
    by hand would mean thirty places to forget, and the forgotten ones would report an
    empty `ignored` list, which reads exactly like "nothing was dropped". Deriving it means
    a new branch is covered the day it is written, by nobody.

    A word counts as used if it names the tool, names a parameter, appears in a parameter's
    value, is a synonym for the matched kind, or is filler.
    """
    text = " ".join(utterance.lower().split())
    words = _words(text)

    if not plan:
        # Nothing matched, so nothing was thrown away: the whole utterance went onward.
        return {"tier": "parser", "matched": None, "extracted": {}, "ignored": [], "consumed": []}

    used: set[str] = set(FILLER)
    tools_matched: list[str] = []

    for step in plan:
        name = str(step.get("tool", ""))
        tools_matched.append(name)
        for w in _words(name.replace("_", " ")):
            used.update(_variants(w))
        for key, value in (step.get("params") or {}).items():
            for w in _words(key.replace("_", " ")):
                used.update(_variants(w))
            if isinstance(value, str):
                used.update(_words(value))
            elif isinstance(value, bool):
                # `overdue=True` is the word "overdue" doing its job. The value carries no
                # words of its own, and the key already went in above.
                pass
            elif isinstance(value, (int, float)):
                used.add(str(value))
                used.add(str(int(value)) if float(value).is_integer() else str(value))

    # Every way of saying the kinds this plan actually filtered on. "parties" and "party"
    # both mean `ground_party`, and only the one the operator typed is in the utterance.
    kinds = {
        str(step.get("params", {}).get("kind"))
        for step in plan
        if step.get("params", {}).get("kind")
    }
    for word, kind in KIND_WORDS.items():
        if kind in kinds:
            used.update(_words(word))

    # A duration is parsed into a number of days, so the digits that expressed it
    # ("the last 12 hours") appear nowhere in the plan. With a `days` parameter present
    # the bare numbers in the utterance are that expression, not dropped words.
    if any("days" in (step.get("params") or {}) for step in plan):
        used.update(w for w in words if w.replace(".", "").replace("-", "").isdigit())

    ignored = [w for w in words if w not in used]

    # 🔴 THE PARAMETERS IT INFERRED, WHICH ARE THE HALF NOBODY CAN SEE. The transcript
    # already shows what was heard, so a misread word is visible on screen. What is not
    # visible is what the parser then made of it: a voice test heard "which assets are
    # overdue" as "Hydrophone Lancaster Sound 01 overdue", and the difference between the
    # right answer and the wrong one was an unannounced `kind="hydrophone"`.
    #
    # Merged across steps, later steps winning, because the operator is being shown what
    # the sentence was understood to mean rather than a per-step breakdown they did not ask
    # for. Placeholders are shown UNRESOLVED on purpose: `__viewport__` is what this tier
    # actually decided, and the resolving happens later and elsewhere.
    def _placeholder(value: Any) -> bool:
        if isinstance(value, str):
            return value.startswith("__") and value.endswith("__")
        if isinstance(value, list):
            return bool(value) and all(_placeholder(v) for v in value)
        return False

    extracted: dict[str, Any] = {}
    for step in plan:
        for key, value in (step.get("params") or {}).items():
            # ⚠️ A PLACEHOLDER NEVER OVERWRITES A REAL VALUE. "Isolate daymark 01" resolves
            # its own subject, so the later steps carry `__subject__` on the same key the
            # first step carried "daymark 01". Letting the last write win hid the only part
            # the operator would recognise behind an internal token.
            if key in extracted and _placeholder(value) and not _placeholder(extracted[key]):
                continue
            extracted[key] = value

    return {
        "tier": "parser",
        "matched": tools_matched[0] if len(tools_matched) == 1 else tools_matched,
        "kind": sorted(kinds) or None,
        "extracted": extracted,
        "consumed": [w for w in words if w in used],
        # De-duplicated, order preserved: a word repeated twice is one thing to explain.
        "ignored": list(dict.fromkeys(ignored)),
    }
