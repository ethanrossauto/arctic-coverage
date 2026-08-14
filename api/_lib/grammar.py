"""The command language. Every sentence tier 1 answers is declared here and nowhere else.

🔑 WHY THIS MODULE EXISTS. Tier 1 used to be thirty ordered `re.search` calls, each hunting
for its own keywords ANYWHERE in the utterance and returning on the first hit. That shape has
one failure built into it: a rule matches part of a sentence, answers that part, and presents
the result as though it had answered all of it. Every line below is one instance, and each was
fixed by adding a guard to the branch that produced it:

    "what is the system status"          -> describe_entity(target="system status")
    "show me the event log"              -> focus_entity(target="event log")
    "show me all unkowns"                -> focus_entity(target="all unkowns")
    "hide everything except the radar"   -> a highlight, hiding nothing
    "focus FLS Alert and hide the rest"  -> one asset named "fls alert and hide the rest"
    "now just the ones that are overdue" -> all sixteen overdue assets, ignoring "just"

⚠️ THIRTY GUARDS ON THIRTY BRANCHES IS A BEHAVIOUR, NOT A STRUCTURE. Each guard was correct
and none of them generalised, so the same class of bug returned in a new branch twice after
being fixed. An exact grammar is TOTAL BY CONSTRUCTION: an utterance either matches a declared
sentence completely or this tier does not answer it. There is no partial match left to guard,
so the bug class is deleted rather than defended.

🔑 EXACTNESS SITS ON THE VERB, NOT ON THE WHOLE SENTENCE. "One phrasing per tool" means a
small closed grammar, not one literal string: `{kind}`, `{asset}`, `{coord}` and `{duration}`
are real parameters, and a kind can still be said any of the ways an operator says it. What is
fixed is the frame around them.

⚠️ A NAME THAT DOES NOT RESOLVE STILL ESCALATES, AND NEVER REJECTS. This module decides only
whether a sentence is in the language. Whether "Daymark 07" exists is the executor's question,
and `Unresolved` already carries it to tier 2.

🥇 WHAT THIS BUYS, WHICH IS MORE THAN CORRECTNESS. The README claims tier 1 keeps working when
the model is down. That was nearly hollow while nobody could invoke it on purpose: a hidden
cache that answers sometimes is not a control surface. A declared language, printed on the
reference card, makes the deterministic tier a real degraded mode: poor satellite link or an
unreachable API, and an operator still drives the whole console at zero cost. On a
surveillance picture that is a capability, not an optimisation.

⛔ IT IS NOT A RETREAT FROM AN LLM INTERFACE, and must not be described as one. The design is
strict phraseology with a model absorbing everything else, which is how the domain already
works: air traffic control and military voice procedure use fixed phraseology precisely
because ambiguity is expensive. A fuzzy parser trying to be a worse model is the weak middle.

⚠️ THE COST, STATED HONESTLY. Tier 1 answers fewer unrehearsed phrasings than it did, so the
share of commands served with no model call falls. That number changes meaning rather than
degrading: it was "how often do we accidentally catch a phrasing nobody planned for", and it
is now "an operator who knows the language drives every tool at zero cost, and everyone else
gets the model".

🔧 THIS MODULE IMPORTS NOTHING BUT THE STANDARD LIBRARY, for the same reason `domain.py` does:
the parser, the tool registry and the transcription hints all read the language, and a bottom
module lets all three ask without a cycle and without anybody keeping a copy. The reference
card, the spoken vocabulary and the parser are three renderings of this one table.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# The placeholders
# --------------------------------------------------------------------------
# 🔑 WORDS THIS TIER CANNOT RESOLVE, WRITTEN AS TOKENS FOR THE ONE LAYER THAT CAN. The grammar
# is a pure function of the sentence: it does not know what is on screen, what the last answer
# held, or which asset is selected, and it must not, because that is what makes it testable
# with no browser and no database. `executor.resolve_context` binds these at run time.
#
# ⚠️ THEY LIVE HERE RATHER THAN IN `executor` BECAUSE THE GRAMMAR IS WHAT EMITS THEM. The
# executor re-exports them, so every existing reference keeps working.
VIEWPORT = "__viewport__"
RESULT = "__result__"
SUBJECT = "__subject__"


# --------------------------------------------------------------------------
# The closed lexicons
# --------------------------------------------------------------------------

#: Asset kinds as an operator would say them, mapped to what the database calls them. Plurals
#: and the obvious synonyms, because "drones" and "uas" are the same request.
#:
#: 🔑 A LEXICON IS NOT FUZZINESS. Every entry is written down, so a `{kind}` slot is as exact
#: as a literal word: the frame is fixed and the noun inside it is looked up.
#:
#: 🔴 "aircraft" USED TO MEAN "uas" HERE, and it stopped being true when aircraft became a
#: kind of their own. "Show me the aircraft" returned the drones: a confident answer to a
#: different question, from a synonym that was correct when the world had only one thing that
#: flew.
KIND_WORDS: dict[str, str] = {
    "node": "node", "nodes": "node", "sensor": "node", "sensors": "node",
    "mesh node": "node", "mesh nodes": "node",
    "patrol": "patrol", "patrols": "patrol", "ranger": "patrol", "rangers": "patrol",
    "drone": "uas", "drones": "uas", "uas": "uas",
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

#: The singular words for what an operator puts on the map. A placement names one thing, so
#: the plural forms are deliberately absent: "place a markers at" is not a sentence.
#:
#: ⚠️ NARROWER THAN `tools.PLACEABLE_KINDS` ON PURPOSE, and the difference is sayability
#: rather than capability. The tool can place a vessel or an aircraft, and those are contacts
#: the world produces rather than kit an operator drops, so the grammar does not teach them.
#: Tier 2 still reaches the whole set.
PLACEABLE_WORDS: dict[str, str] = {
    "marker": "marker",
    "pin": "marker",
    "node": "node",
    "mesh node": "node",
    "sensor": "node",
    "hydrophone": "hydrophone",
    "launch site": "launch_site",
    "base": "launch_site",
}

# 🔴 A DURATION AND AN ALTITUDE LEXICON USED TO SIT HERE, and they went with the sentences
# that read them. "Where has Daymark 01 been in the last 3 days" and "send Daymark 05 to 73.2
# -95.9 at 500 metres" were second wordings of tools that already had one, so `days` and
# `altitude_m` are parameters the model sets and this tier does not. Deleting the readers rather
# than leaving them declared and unreachable is the point: a slot no rule uses is a slot nobody
# can tell is dead.

#: Words that order one action after another. A name slot may never contain one: "focus FLS
#: Alert and hide everything else" is two instructions, and swallowing the second into the
#: first asset's name is how it used to be lost in silence.
_SEQUENCING = ("and", "then", "also", "plus", "afterwards", "after")

#: Nouns that name the DISPLAY rather than a thing on it. A name slot refuses them, because
#: they cannot resolve to an asset and the attempt is what produced the wrong answer.
#:
#: 🔴 "focus on the entire world" WAS ANSWERED AS AN ASSET SEARCH for something called "entire
#: world". It resolved to nothing and escalated, and the model then read it as "show
#: everything" and brought every hidden kind back, which is a visibility change nobody asked
#: for. Widening the camera and revealing hidden kinds are separate acts. "Zoom out" is the
#: declared sentence for the first one.
_DISPLAY_NOUNS = frozenset(
    {"world", "map", "picture", "arctic", "screen", "view", "viewport", "everything", "thing"}
)

#: Trailing punctuation and whitespace, which are not words and not a second wording. A typed
#: "mesh status?" and a spoken "mesh status" are the same sentence, and `match` has already
#: collapsed runs of whitespace before this is applied.
#:
#: 🔴 A POLITENESS WRAPPER USED TO SIT BESIDE THIS, matching a leading "please", "can you" or
#: "could you". It went with the optional words on the same reasoning Ethan gave for "me": a
#: droppable filler word is a SECOND WORDING, and one wording per tool means one. So "please show
#: the drones" escalates. ⚠️ The cost lands hardest on voice, where politeness is commoner than
#: in typing, and it is stated here rather than discovered: a spoken "can you show the drones"
#: costs a model call.
_TAIL = r"[\s?.!]*"


# --------------------------------------------------------------------------
# The slots
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    """One parameter position in a sentence.

    `pattern` is what it matches inside the anchored sentence regex. `read` turns the matched
    text into the parameters it contributes, and returning None is a REFUSAL: the frame was
    right and the contents were not, so the whole rule fails and the utterance goes to the
    model rather than being answered with whatever could be salvaged.

    🔑 A SLOT'S PARAMETERS ARE KEYED BY THE SLOT'S OWN NAME, so a step referring to `"{kind}"`
    is referring to the `{kind}` slot in the same template. `{coord}` is the one exception,
    because a coordinate is genuinely two numbers, and it says so by producing `lat` and
    `lon`. `_validate` checks every reference against what the slots in that rule produce, so
    a typo here cannot reach a running server.
    """

    pattern: str
    #: matched text -> {name: value}, or None to reject the match.
    read: Callable[[str], dict[str, Any] | None]
    #: What the reference card prints in this position.
    example: str
    #: The names this slot can contribute, for the import-time check.
    produces: tuple[str, ...]


def _read_kind(text: str) -> dict[str, Any] | None:
    kind = KIND_WORDS.get(text.strip())
    return {"kind": kind} if kind else None


def _read_kinds(text: str) -> dict[str, Any] | None:
    kind = KIND_WORDS.get(text.strip())
    # The visibility tools take a list of kinds rather than one, and the grammar says one at a
    # time. Tier 2 is where "hide the radars and the vessels" belongs.
    return {"kinds": [kind]} if kind else None


def _read_placeable(text: str) -> dict[str, Any] | None:
    kind = PLACEABLE_WORDS.get(text.strip())
    return {"placeable": kind} if kind else None


def _not_a_name(text: str) -> bool:
    """Is this slot a KIND or the DISPLAY, rather than the name of one thing?

    🔴 THE PLURAL BUG, MADE STRUCTURAL. "Show me all unknowns" and "focus the drones" are
    filters over many things, and every tool taking a `target` is singular: the tool was wrong
    before spelling was ever a factor. A determiner plus a kind word is not a name, so a name
    slot refuses it and the sentence escalates.

    ⚠️ "a" AND "an" ARE NOT STRIPPED, DELIBERATELY. "Send a drone to 73.2 -95.9" is the
    phrasing on the card and "a drone" is a target the resolver is meant to interpret, where
    "the drones" is plainly a set.
    """
    stripped = text.strip()
    for pattern in (
        r"^(?:on|to|at|in|of|for)\s+",
        r"^(?:the|all|every|any)\s+",
        r"^(?:whole|entire|full)\s+",
    ):
        stripped = re.sub(pattern, "", stripped)
    return stripped in KIND_WORDS or stripped in _DISPLAY_NOUNS


def _read_asset(text: str) -> dict[str, Any] | None:
    name = text.strip(" ?,.")
    if not name:
        return None
    if any(re.search(rf"\b{word}\b", name) for word in _SEQUENCING):
        return None
    if name.startswith("all ") or _not_a_name(name):
        return None
    return {"asset": name}


def _read_coord(text: str) -> dict[str, Any] | None:
    found = re.findall(r"-?\d+(?:\.\d+)?", text)
    if len(found) != 2:
        return None
    lat, lon = float(found[0]), float(found[1])
    # Out of range is not a coordinate. Reading it as one would put an asset somewhere the
    # globe does not have, and a refusal here becomes an escalation rather than an error.
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None
    return {"lat": lat, "lon": lon}


def _lexicon(words: Iterable[str]) -> str:
    """A lexicon as an alternation, longest first so "mesh nodes" beats "nodes"."""
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


SLOTS: dict[str, Slot] = {
    "kind": Slot(_lexicon(KIND_WORDS), _read_kind, "hydrophones", ("kind",)),
    "kinds": Slot(_lexicon(KIND_WORDS), _read_kinds, "radars", ("kinds",)),
    "placeable": Slot(_lexicon(PLACEABLE_WORDS), _read_placeable, "marker", ("placeable",)),
    # A name runs to the end of its position and may hold anything but a sequencing word,
    # which `_read_asset` enforces. Non-greedy, so a literal after it wins the tail.
    "asset": Slot(r".+?", _read_asset, "Daymark 01", ("asset",)),
    "coord": Slot(
        r"-?\d+(?:\.\d+)?\s*,?\s+-?\d+(?:\.\d+)?", _read_coord, "73.2 -95.9", ("lat", "lon")
    ),
}


# --------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """One sentence the deterministic tier answers. One per tool, and no other kind.

    `template` is that sentence, in literal words and slots:

        {slot}   a parameter, from `SLOTS`

    🔒 THERE IS NO OTHER NOTATION, AND THE ABSENCE IS THE MECHANISM. An earlier version of this
    file had `[word]` for an optional and `(a|b)` for alternatives, and within an hour of having
    them I had written four ways to ask which assets are overdue, `kill` beside `take down`, and
    `fix` beside `repair` and `restore`. A notation for synonyms is an invitation to a synonym
    pile, which is what the parser this replaced actually was. With no way to express one, a
    second wording costs a whole rule in a table where two rules for one tool fail the suite.

    `steps` is what it produces, in the plan schema the model also emits. A value of `"{name}"`
    is replaced by what a slot in this template parsed, so a rule that expands into several
    actions stays declarative.
    """

    template: str
    steps: tuple[tuple[str, dict[str, Any]], ...]
    #: Slot examples, where the default in `SLOTS` would read oddly in this sentence.
    example: dict[str, str] = field(default_factory=dict)

    @property
    def tool(self) -> str:
        """The tool this sentence reaches, and the tool it is the only sentence for."""
        return self.steps[0][0]


def _one(tool: str, **params: Any) -> tuple[tuple[str, dict[str, Any]], ...]:
    return ((tool, params),)


# 🔑 THE ONE TABLE, AND THERE IS EXACTLY ONE SENTENCE PER TOOL. The parser
# matches against it, the reference card prints it, and the transcription hints are built from
# its words. A sentence that is not here is not in the language, and that utterance goes to the
# model.
#
# 🔴 IT CARRIED 45 SENTENCES BEFORE THIS, AND 27 OF THEM WERE MINE RATHER THAN ASKED FOR. Some
# were parameters, which are defensible: a filter, a window, a flag. The rest were synonyms:
# `(show|list)`, three wordings each for overdue and isolated and unserviceable, `kill` beside
# `take down`, `fix` beside `repair` and `restore`. That is the pile the old parser was deleted
# for, declared one layer up and slightly smaller, and it does not survive being looked at:
# "one wording per tool" was the instruction, and a table with four ways to ask which assets are
# overdue is not that.
#
# 🔒 THE NOTATION CANNOT EXPRESS A SYNONYM ANY MORE, which is what makes the rule structural
# rather than a habit. There is no alternation and no optional word: a template is literal words
# and slots, so the next person to want a second wording has to add a whole rule, in a table
# where a second rule for one tool is visible at a glance and fails the suite.
#
# ⚠️ ORDER IS FOR READING, NOT FOR PRECEDENCE. Every rule is anchored to the whole utterance, so
# at most one can match and rule order cannot decide an answer. The thirty-branch version did not
# have that property: moving a branch changed what a sentence meant, which is why its ordering
# carried five paragraphs of warnings.
#
# ⚠️ WHAT A PARAMETER COSTS NOW, STATED WHERE IT IS DECIDED. A filter, a window, an altitude and
# the two placement flags are no longer sayable on this tier: "show the overdue nodes", "where
# has Daymark 01 been in the last 3 days" and "place a hydrophone with a backhaul at 74.3 -84.2"
# all go to the model. Every one of them is a real capability reached through the tool's own
# parameters, so tier 2 answers them; what is gone is the deterministic route. That is the price
# of a language an operator can hold in their head, and it was chosen deliberately.
# 🔴 EVERY SENTENCE STARTS WITH A VERB NOBODY ELSE USES, AND THAT IS THE SELECTION RULE FOR
# WHETHER A TOOL EXISTS AT ALL (Ethan, 2026-08-14: *"you cant have three tools all starting with
# 'show', its confusing... if it cant have unique lingo, it probably doesnt belong as a new
# tool"*).
#
# Four sentences used to open with `show`: the asset list, the ice overlay, coverage and the
# unknown contacts. An operator hearing "show" learned nothing about which of four things they
# were about to get, and the card had to carry a gloss per line to undo the collision the
# language had created. A distinct verb says it in the first word.
#
# 🥇 IT IS ALSO A BETTER TEST FOR REDUNDANCY THAN READING THE CODE WAS. Applied to the two tools
# that went, they failed for different reasons and neither was obvious from the implementations:
#
#   show_unknown    no verb of its own exists, because it is not a different action. The only
#                   honest sentence is "list the unknowns", which is `list_entities` with a
#                   filter, and `coverage gaps` already reports that bucket in its answer.
#   frame_entities  a verb DOES exist and "frame the patrols" is good lingo. It fails the other
#                   half: `executor._frame_results` frames whatever any plan returned, so FRAME
#                   and LIST are one camera move. A distinct word with no distinct action is a
#                   synonym in a costume, which is what this table was cut from 45 rules to
#                   avoid.
#
# ⚠️ AND IT SAVED TWO I HAD ALREADY WRITTEN OFF. `backhaul_status` shares its whole computation
# with `mesh_status`, and `clear_fault` is `inject_fault` with the fault taken away, so both read
# as merge candidates from the code. `comms check` against `satcom check`, and `deadline` against
# `restore`, are four words an operator would never confuse: distinct question, distinct verb,
# keep the tool. The lingo knew something the call graph did not.
#
# 🔑 THE VOCABULARY IS REAL, NOT INVENTED. Declutter is a tactical-display function, a sensor is
# slewed, aircraft are vectored, the Army emplaces equipment, and a deadlined vehicle is one out
# of service. Borrowing the words means an operator who has used a console before can guess
# them, and everybody else has the card.
RULES: tuple[Rule, ...] = (
    # --- see: what is on the map -------------------------------------------
    Rule("list the {kind}", _one("list_entities", kind="{kind}")),
    Rule("coverage gaps", _one("coverage")),
    Rule("overlay the ice", _one("show_overlay", layer="ice")),
    Rule("declutter the {kinds}", _one("set_visible_kinds", mode="hide", kinds="{kinds}")),
    # --- look at: where the camera points ----------------------------------
    # 🔴 A CAMERA COMMAND MUST NEVER REACH THE MODEL. It carries no domain question at all, so
    # there is nothing for a reasoning model to reason about, and it is the first thing anyone
    # tries on a map: "zoom the map out completely" once took 7.8 seconds against 1.9 for
    # anything this tier answers.
    Rule("go wide", _one("reset_view")),
    Rule("slew to {asset}", _one("focus_entity", target="{asset}")),
    # The window is stated rather than left to the tool's default, so the trace and the audit
    # row say which day was asked about. A parameter this tier chose silently is a parameter
    # nobody can check afterwards.
    Rule("track history on {asset}", _one("entity_history", target="{asset}", days=1.0)),
    # --- ask: what the console knows ---------------------------------------
    Rule("comms check", _one("mesh_status")),
    Rule("satcom check", _one("backhaul_status")),
    Rule("readout on {asset}", _one("describe_entity", target="{asset}")),
    # ⚠️ `history` IS DELIBERATELY NOT ONE OF THESE WORDS. "Track history on Daymark 01" is one
    # asset's track; this is the world's log, and they are different answers.
    Rule("sitrep", _one("recent_activity", days=1.0)),
    # --- do: change the world ----------------------------------------------
    Rule(
        "emplace a {placeable} at {coord}",
        _one("place_asset", kind="{placeable}", lat="{lat}", lon="{lon}"),
    ),
    Rule(
        "vector {asset} to {coord}",
        _one("task_uas", target="{asset}", lat="{lat}", lon="{lon}"),
        example={"asset": "a drone"},
    ),
    Rule("scrub {asset}", _one("remove_asset", target="{asset}"),
         example={"asset": "Marker 01"}),
    # ⚠️ ONE OF THE TWO FAULTS IS SAYABLE HERE, and that follows from one sentence per tool:
    # `fault` is a parameter, and this sentence fixes it to `silent`. Putting an asset
    # unserviceable is the same tool with the other value, so it goes to the model.
    Rule("deadline {asset}", _one("inject_fault", target="{asset}", fault="silent"),
         example={"asset": "node-barrow-05"}),
    Rule("restore {asset}", _one("clear_fault", target="{asset}"),
         example={"asset": "node-barrow-05"}),
)


# --------------------------------------------------------------------------
# Compiling a template
# --------------------------------------------------------------------------


def _atoms(template: str) -> list[tuple[str, str]]:
    """A template as ordered (kind, text) atoms: `word` or `slot`, and nothing else.

    🔒 THE PARSER FOR THE TEMPLATES IS THIS SMALL ON PURPOSE. Anything it cannot express cannot
    be declared, and what it deliberately cannot express is a synonym: no alternation, no
    optional word. An earlier version had both, and the table grew four ways to ask one question
    before the day was out.

    ⚠️ A LEFTOVER BRACKET IS AN ERROR, NOT A LITERAL. `[me]` used to mean an optional word, so a
    template still carrying one would otherwise compile into a rule matching a square bracket,
    which nobody would type and nothing would report.
    """
    out: list[tuple[str, str]] = []
    for token in re.findall(r"\{[^}]*\}|[^\s{}]+", template):
        if token.startswith("{"):
            out.append(("slot", token[1:-1]))
        elif any(ch in token for ch in "[]()|"):
            raise ValueError(
                f"{template!r} carries {token!r}: the notation for optional words and "
                "alternatives was removed, because one wording per tool is the rule"
            )
        else:
            out.append(("word", token))
    return out


def _fragment(kind: str, text: str, used: list[str]) -> str:
    """One atom as a regex fragment, recording any slot it consumes."""
    if kind == "word":
        return re.escape(text)
    if text not in SLOTS:
        raise ValueError(f"unknown slot {{{text}}}")
    if text in used:
        raise ValueError(f"slot {{{text}}} appears twice in one template")
    used.append(text)
    return f"(?P<{text}>{SLOTS[text].pattern})"


@dataclass(frozen=True)
class Compiled:
    rule: Rule
    regex: re.Pattern[str]
    slots: tuple[str, ...]
    #: What the reference card prints for this rule.
    sentence: str


def _card_sentence(rule: Rule) -> str:
    """The printable sentence: the template with each slot filled by its example.

    🔑 THE CARD IS A RENDERING OF THE GRAMMAR, NOT A SECOND LIST. A reference maintained beside
    the parser drifts the first time anyone renames anything, and the operator is the one who
    finds out. With one sentence per tool there is nothing to choose between, so the card prints
    the whole language.
    """
    return " ".join(
        text if kind == "word" else (rule.example.get(text) or SLOTS[text].example)
        for kind, text in _atoms(rule.template)
    )


def _compile(rule: Rule) -> Compiled:
    used: list[str] = []
    pattern = r"\s+".join(
        _fragment(kind, text, used) for kind, text in _atoms(rule.template)
    )
    return Compiled(
        rule=rule,
        regex=re.compile(rf"^{pattern}{_TAIL}$"),
        slots=tuple(used),
        sentence=_card_sentence(rule),
    )


def _validate(compiled: tuple[Compiled, ...]) -> None:
    """Every `"{name}"` in a step must be something a slot in that template produces.

    🔒 AT IMPORT, SO A TYPO CANNOT REACH A RUNNING SERVER. A reference to a slot the template
    does not carry would raise `KeyError` inside `match` on whichever utterance happened to hit
    that rule, which is a 500 on one phrasing and green tests everywhere else. This is the same
    class of check as the completeness tests over the field schema, and it belongs at import
    for the same reason the field checks belong in the suite: nothing else looks.
    """
    for item in compiled:
        available = {name for slot in item.slots for name in SLOTS[slot].produces}
        for tool, params in item.rule.steps:
            for key, value in params.items():
                for ref in _references(value):
                    if ref not in available:
                        raise ValueError(
                            f'"{item.rule.template}" -> {tool}.{key} refers to {{{ref}}}, '
                            f"which no slot in it produces (it has: {sorted(available) or 'none'})"
                        )


def _references(value: Any) -> list[str]:
    if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
        return [value[1:-1]]
    if isinstance(value, list):
        return [ref for item in value for ref in _references(item)]
    return []


COMPILED: tuple[Compiled, ...] = tuple(_compile(rule) for rule in RULES)
_validate(COMPILED)


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Match:
    rule: Rule
    #: The plan, in the schema the model also emits.
    steps: list[dict[str, Any]]
    #: What the slots parsed, for the trace.
    slots: dict[str, Any]
    #: The card sentence for the rule that matched, which is what an operator can be taught.
    sentence: str


def _substitute(value: Any, slots: dict[str, Any]) -> Any:
    """Fill `"{name}"` references in a declared step from what the slots parsed."""
    if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
        return slots[value[1:-1]]
    if isinstance(value, list):
        return [_substitute(item, slots) for item in value]
    return value


def match(utterance: str) -> Match | None:
    """The one entry point: an utterance to a plan, or None if it is not in the language.

    ⚠️ ANCHORED, SO AT MOST ONE RULE CAN MATCH. Two rules matching one sentence is a bug in the
    table rather than a precedence question, which is why the suite checks for it instead of
    relying on the order of this file.
    """
    text = " ".join(utterance.lower().split())
    if not text:
        return None

    for compiled in COMPILED:
        m = compiled.regex.match(text)
        if not m:
            continue
        slots: dict[str, Any] = {}
        for name in compiled.slots:
            read = SLOTS[name].read(m.group(name))
            if read is None:
                slots = {}
                break
            slots.update(read)
        else:
            steps = [
                {
                    "tool": tool,
                    "params": {
                        key: _substitute(value, slots) for key, value in declared.items()
                    },
                }
                for tool, declared in compiled.rule.steps
            ]
            return Match(
                rule=compiled.rule, steps=steps, slots=slots, sentence=compiled.sentence
            )
        # The frame matched and its contents did not, so this sentence is not in the language
        # after all. Keep looking, then escalate.
    return None


# --------------------------------------------------------------------------
# The other two renderings
# --------------------------------------------------------------------------


def all_matches(utterance: str) -> list[Rule]:
    """Every rule that fully matches, which must never be more than one.

    🔑 `match` returns the first and stops, so nothing in production can see an ambiguity. This
    exists so the suite can: two rules claiming one sentence is a defect in the table, and the
    whole point of anchoring is that it is findable by looking rather than by demoing.

    ⚠️ A FRAME THAT MATCHES WITH CONTENTS THAT ARE REFUSED IS NOT A MATCH. "Focus the drones"
    fits `focus {asset}` until the name slot refuses a set, and refusing it is what sends the
    sentence to the model rather than to the wrong tool.
    """
    text = " ".join(utterance.lower().split())
    out: list[Rule] = []
    for compiled in COMPILED:
        m = compiled.regex.match(text)
        if not m:
            continue
        if all(SLOTS[name].read(m.group(name)) is not None for name in compiled.slots):
            out.append(compiled.rule)
    return out


def card_sentences() -> dict[str, list[str]]:
    """Tool name -> the sentences the reference card prints for it.

    🔑 EXACTLY ONE PER TOOL, AND THE CARD IS THEREFORE THE WHOLE LANGUAGE. Nothing is answerable
    that is not printed, so an operator who reads the card knows every sentence this tier
    recognises. That is a stronger promise than the card could make while the parser also
    accepted phrasings nobody had printed, and it is why the return type stays a list: the
    suite asserts the count is one rather than assuming it.
    """
    out: dict[str, list[str]] = {}
    for compiled in COMPILED:
        out.setdefault(compiled.rule.tool, []).append(compiled.sentence)
    return out


#: Tools worth suggesting to somebody who has just been refused, in the order they are offered.
#:
#: 🔑 THESE THREE BECAUSE THEY NEED NO ARGUMENT AND NO ASSET. A refusal is the worst moment to
#: hand somebody a sentence with a slot in it: they have to invent a value, and inventing one
#: that resolves is exactly what they just failed at.
_SUGGESTED: tuple[str, ...] = ("comms check", "coverage gaps", "sitrep")


def suggestions() -> list[str]:
    """Sentences to offer an operator whose command was refused. Every one is checked.

    🔴 THIS EXISTS BECAUSE THE HAND-TYPED VERSION WAS WRONG WHERE IT MATTERED MOST. `index.py`
    carried three example commands described as "always work, including when the metered layer
    does not", and two of them did not parse at all: they were written for the keyword parser
    and never re-read when the grammar became anchored. The one line whose whole job is to be
    reachable when tier 2 is down recommended two commands that need tier 2.

    🔒 SO IT FILTERS AGAINST THE LANGUAGE RATHER THAN TRUSTING THE LIST. A name here that stops
    being a declared sentence drops out silently rather than being offered, and the suite asserts
    the result is not empty. Getting this wrong quietly is what happened last time.
    """
    declared = {compiled.sentence for compiled in COMPILED}
    return [s for s in _SUGGESTED if s in declared]


#: What a kind is called when a sentence has to name it out loud. The lexicon maps many words
#: to one kind; this picks the one to print back.
SPOKEN_PLURAL: dict[str, str] = {
    "node": "nodes", "hydrophone": "hydrophones", "uas": "drones", "patrol": "patrols",
    "vessel": "vessels", "aircraft": "aircraft", "ground_party": "ground parties",
    "radar": "radars", "marker": "markers", "launch_site": "launch sites",
}


def phrase_for(tool: str, params: dict[str, Any] | None = None) -> str | None:
    """The declared sentence for a tool, filled in with THIS answer's values.

    🔴 THE CARD'S EXAMPLE IS THE WRONG THING TO SHOW AFTER AN ANSWER. The teaching line under an
    escalated reply used to print the card sentence verbatim, so asking about FLS Resolute Bay
    and being told the fast way to do it was `tell me about Daymark 01`: the right verb attached
    to somebody else's asset. The operator then has to work out which part of that sentence was
    the lesson, which is the whole of what the line exists to save them.

    🔒 IT RETURNS None UNLESS THE SENTENCE WOULD PRODUCE THIS EXACT PLAN, and that guard is the
    important half. `list_entities(overdue=True)` has no declared sentence: the one wording for
    that tool is "show the {kind}", which carries no filter. Rendering it anyway suggests "show
    the hydrophones" to somebody who asked what had gone quiet, which is a confidently wrong
    instruction wearing the shape of help. A parameter the sentence cannot say means there is
    nothing to teach, and the model answering was the right outcome.
    """
    params = params or {}
    for compiled in COMPILED:
        rule = compiled.rule
        if rule.tool != tool or len(rule.steps) != 1:
            continue
        declared = rule.steps[0][1]
        # Every declared parameter must be satisfied, every fixed one must match, and the plan
        # may carry nothing extra. Anything else is a different command.
        if set(params) - set(declared):
            return None
        for declared_key, declared_value in declared.items():
            if (
                isinstance(declared_value, str)
                and declared_value.startswith("{")
                and declared_value.endswith("}")
            ):
                if params.get(declared_key) is None:
                    return None
            elif params.get(declared_key) != declared_value:
                return None

        out: list[str] = []
        for kind, text in _atoms(rule.template):
            if kind == "word":
                out.append(text)
                continue
            # A slot may produce more than one value: {coord} is a lat and a lon, taken in
            # declaration order.
            values: list[str] = []
            for name in SLOTS[text].produces:
                key: str | None = next(
                    (k for k, v in declared.items() if v == "{" + name + "}"), None
                )
                if key is None:
                    continue
                value: Any = params[key]
                if isinstance(value, list):
                    value = value[0] if value else None
                if value is None:
                    return None
                values.append(_spoken(text, value))
            out.append(" ".join(values))
        return " ".join(out)
    return None


def _spoken(slot: str, value: Any) -> str:
    """A parameter value as a person would say it.

    ⚠️ PLURAL FOR A FILTER, SINGULAR FOR A PLACEMENT, and getting that wrong reads immediately:
    "place a markers at 71.4 -80.1". `{kind}` names a set and `{placeable}` names one thing.
    """
    text = str(value)
    if slot in ("kind", "kinds"):
        return SPOKEN_PLURAL.get(text, text.replace("_", " "))
    return text.replace("_", " ")


def openings() -> dict[str, list[str]]:
    """First word of a declared sentence -> the sentences that open with it.

    For saying something useful when tier 1 declines: whether the utterance was close to a
    command that exists is the difference between "say it the declared way and it costs
    nothing" and "this genuinely needs the model".
    """
    out: dict[str, list[str]] = {}
    for compiled in COMPILED:
        kind, text = _atoms(compiled.rule.template)[0]
        # A sentence opening with a slot has no fixed first word, so there is nothing to key on.
        # None do today; a future one would simply not appear here rather than crash.
        if kind == "word":
            out.setdefault(text, []).append(compiled.sentence)
    return out


def spoken_terms() -> list[str]:
    """Every literal WORD in the language, for the transcription hints. Words, never sentences.

    🔑 DERIVED, BECAUSE THE HAND-MAINTAINED VERSION COULD NOT BE CHECKED. A command verb is in no
    enum, so nothing noticed when the display learned a word the transcriber had never heard of,
    and a misheard verb loses the whole command even when every name in it was perfect.

    🔴 IT RETURNS SINGLE WORDS, AND THAT IS THE WHOLE OF WHAT THIS FUNCTION LEARNED TODAY. It
    returned the filled card sentences first, which put example VALUES in front of the
    transcriber: "place a marker at 73.2 -95.9". Then it returned literal word runs, which put
    whole COMMANDS there: "show the unknowns". Both are the same mistake one step apart. Spoken
    into the running console, "show me all the hydrophones" came back as "show the hydrophones",
    because a sentence in the hint list is a sentence the model can snap a near miss onto, and
    the operator then reads their own words rewritten into words they did not say.

    ⚠️ THE HINT LIST SPELLS WHAT WAS SAID. IT NEVER SUPPLIES WHAT WAS MEANT. A single word can
    only correct a spelling; a phrase can replace a sentence. Deliberate multi-word terms still
    exist, in `transcribe._NEAR_HOMOPHONES`, where each one is written down on purpose and read
    by a person rather than generated from a table.
    """
    glue = {"a", "an", "the", "me", "in", "to", "at", "has", "are", "been", "about", "with"}
    words = [
        text
        for compiled in COMPILED
        for kind, text in _atoms(compiled.rule.template)
        if kind == "word"
    ]
    return [word for word in dict.fromkeys(words) if word not in glue]
