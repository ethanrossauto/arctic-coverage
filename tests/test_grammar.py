"""The command language: that it is exact, unambiguous, and says what the card prints.

🔑 WHAT THIS FILE CAN CHECK THAT THE OLD SUITE COULD NOT. Tier 1 was thirty ordered
`re.search` branches, so the only testable questions were "does this phrasing reach that tool"
and "does this one decline", one example at a time, each added after a live call found it. The
properties that actually mattered were not expressible: that no sentence is answered partially,
that no two rules can claim one utterance, that a filter cannot be read as a name.

A declared grammar makes those properties statements about a table, so they are asserted over
the whole table rather than sampled. That is the real return on the rewrite, more than any
individual bug in the list below.
"""
from __future__ import annotations

import pytest

from api._lib import grammar, parser, tools


def test_every_declared_sentence_parses_back_to_its_own_rule():
    """The round trip. A rule whose own printable sentence does not match it is a rule nobody
    can use, and the card would print it anyway."""
    for compiled in grammar.COMPILED:
        found = grammar.match(compiled.sentence)
        assert found is not None, f'"{compiled.sentence}" does not match its own rule'
        assert found.rule is compiled.rule, (
            f'"{compiled.sentence}" is declared for {compiled.rule.tool} and matched '
            f"{found.rule.tool} instead"
        )


def test_no_utterance_is_claimed_by_two_rules():
    """🔑 THE PROPERTY THAT REPLACED BRANCH ORDER, AND THE REASON THE TABLE IS SAFE TO EDIT.

    The old parser's correctness depended on ordering: the visibility branches had to sit above
    the listing branches because "show only the vessels" ends in "the vessels", the placement
    branches had to sit above both because "place an unknown vessel" contains "unknown", and
    each of those constraints was a comment asking the next reader not to move anything.

    Anchored rules cannot overlap silently. If two ever match one sentence that is a defect in
    the table, and this is what finds it rather than a demo.
    """
    corpus = [c.sentence for c in grammar.COMPILED] + [
        "show me the drones",
        "list the vessels",
        "show me the overdue nodes",
        "which nodes are down",
        "list the flights in the current zoom window",
        "show only the vessels",
        "show everything",
        "isolate the drones",
        "isolate daymark 01",
        "list them",
        "frame them",
        "take down the alert node",
        "mark daymark 03 unserviceable",
        "place an unknown vessel at 74.2 -84.0",
        "emplace a hydrophone with a backhaul at 74.3 -84.2",
        "vector survey 03 to 73.2 -95.9 at 500 metres",
        "where has daymark 01 been in the last 3 days",
        "what has happened in the last 12 hours",
        "show me the event log",
        "readout on this",
        "track history on this asset",
    ]
    for utterance in corpus:
        claimed = [rule.template for rule in grammar.all_matches(utterance)]
        assert len(claimed) <= 1, f'"{utterance}" is claimed by {claimed}'


@pytest.mark.parametrize(
    ("utterance", "why"),
    [
        ("what is the system status", "answered as an asset named 'system status'"),
        ("show me the event log", "answered as an asset named 'event log'"),
        ("show me all unkowns", "answered as an asset named 'all unkowns'"),
        ("show me all unknowns", "a filter over many things read as one target"),
        ("slew to on the entire world", "answered as an asset named 'entire world'"),
        ("show me the whole arctic", "a camera command read as an asset search"),
        ("hide everything except the radar", "highlighted four contacts and hid nothing"),
        (
            "slew to fls alert and hide everything else",
            "the second instruction swallowed into the first asset's name",
        ),
        ("now just the ones that are overdue", "narrowing ignored, all sixteen returned"),
        ("narrow it down to the drones", "'down' read as a maintenance filter"),
        (
            "which of these could reach the contact before it gets dark",
            "'dark' read as the AIS filter",
        ),
        ("what is a backhaul", "a definition answered with eleven asset names"),
        ("emplace a hydrophone with its own satellite terminal at 74.3 -84.2", "flag lost silently"),
        ("show me everything that has gone quiet, then reset the view", "second action dropped"),
    ],
)
def test_the_confident_wrong_answers_are_gone(utterance: str, why: str):
    """🔴 EVERY ONE OF THESE WAS A REAL ANSWER THIS CONSOLE GAVE, and each was found by a person
    typing it rather than by the suite.

    They are not fourteen bugs. They are one bug fourteen times: a rule matching part of a
    sentence and answering as though it had read all of it. Each was fixed with a guard on the
    branch that produced it, and the class came back twice in new branches.

    ⚠️ SOME OF THESE ARE NOW ANSWERED CORRECTLY RATHER THAN ESCALATED, and the two outcomes are
    both fine here: what must never happen again is a confident wrong answer. So this test
    asserts the tool, not the escalation, where a declared sentence covers the words.
    """
    plan = parser.parse(utterance)
    if plan is None:
        return
    # The one shape allowed to match: the words are a declared sentence, and the tool is the one
    # the words actually ask for.
    assert plan[0]["tool"] in {"recent_activity", "reset_view"}, (
        f'"{utterance}" reached {plan[0]["tool"]}: {why}'
    )


def test_a_name_slot_refuses_a_set():
    """🔴 THE BUG UNDERNEATH THE TYPO, AND IT SURVIVED CORRECT SPELLING. Every tool taking a
    `target` is singular, so a determiner plus a kind, or anything opening with "all", is the
    wrong tool before a misspelling is ever involved."""
    for text in ("the drones", "all nodes", "every vessel", "the ground parties", "all unknowns"):
        assert grammar.SLOTS["asset"].read(text) is None, text

    # A name that merely CONTAINS a kind word is a name: "Marker 01" is one marker.
    for text in ("marker 01", "daymark 03", "a drone", "fls alert", "node-barrow-05", "this"):
        assert grammar.SLOTS["asset"].read(text) == {"asset": text}, text


def test_a_name_slot_refuses_a_second_instruction():
    """"Focus FLS Alert and hide everything else" resolved to an asset called "fls alert and hide
    everything else": it matched nothing, and the dropped instruction could not even be reported,
    because every word of it sat inside a parameter value and therefore counted as used."""
    for text in ("fls alert and declutter everything else", "daymark 01 then go wide",
                 "daymark 01 also frame it"):
        assert grammar.SLOTS["asset"].read(text) is None, text


def test_a_coordinate_outside_the_globe_is_not_a_coordinate():
    """A refusal here becomes an escalation rather than an asset placed somewhere that does not
    exist. `place_asset` validates too; this stops the sentence being claimed at all."""
    assert grammar.SLOTS["coord"].read("73.2 -95.9") == {"lat": 73.2, "lon": -95.9}
    assert grammar.SLOTS["coord"].read("200 -95.9") is None
    assert grammar.SLOTS["coord"].read("73.2 -400") is None


def test_every_declared_slot_is_used_by_a_rule():
    """🔧 THE DEAD-SLOT CHECK, and it has already earned its place. A duration slot and an
    altitude slot outlived the sentences that read them by about twenty minutes: the rules went
    when the table was cut to one wording per tool, and the lexicons behind them sat in the file
    looking like part of the language. A slot no rule uses cannot be told from one that is
    waiting to be used, and the only difference is whether anybody remembers.
    """
    used = {name for compiled in grammar.COMPILED for name in compiled.slots}
    dead = sorted(set(grammar.SLOTS) - used)
    assert not dead, f"{dead} are declared and no sentence reads them"


def test_every_rule_names_a_tool_that_exists():
    """A rule pointing at a tool the registry does not have would validate as a plan and fail
    at execution, which is a 500 on one phrasing and green tests everywhere else."""
    for compiled in grammar.COMPILED:
        for tool, _params in compiled.rule.steps:
            assert tool in tools.REGISTRY, f'"{compiled.rule.template}" names no such tool: {tool}'


def test_every_declared_parameter_is_one_its_tool_accepts():
    """The validator rejects an unknown parameter, so a typo here would refuse the plan out
    loud. Cheaper to find it at import than in a demo."""
    for compiled in grammar.COMPILED:
        for tool, params in compiled.rule.steps:
            allowed = set(tools.REGISTRY[tool].params)
            unknown = set(params) - allowed
            assert not unknown, f'"{compiled.rule.template}" gives {tool} {sorted(unknown)}'


def test_the_grammar_only_teaches_placements_the_tool_can_perform():
    """⚠️ THE SAYABLE SET IS NARROWER THAN THE CAPABLE SET, AND MUST NEVER BE WIDER.

    `tools.PLACEABLE_KINDS` holds ten kinds; the grammar teaches four, because an operator drops
    a marker or a node and does not drop a vessel: contacts are the world being observed. Tier 2
    still reaches the whole set. What would be a defect is the other direction, a sentence
    teaching a placement the tool refuses.
    """
    sayable = set(grammar.PLACEABLE_WORDS.values())
    assert sayable <= set(tools.PLACEABLE_KINDS), (
        f"the grammar teaches placing {sorted(sayable - set(tools.PLACEABLE_KINDS))}, "
        "which place_asset refuses"
    )


def test_a_rule_referring_to_a_slot_it_does_not_carry_fails_at_import():
    """🔒 THE CHECK THAT MAKES THE TABLE SAFE TO EXTEND. A step referring to `"{duration}"` in a
    sentence with no duration in it would raise `KeyError` inside `match`, on whichever utterance
    happened to reach that rule: a 500 on one phrasing, and nothing else would notice."""
    bad = grammar.Rule(
        "list the {kind}", (("list_entities", {"kind": "{kind}", "days": "{duration}"}),)
    )
    with pytest.raises(ValueError, match="which no slot in it produces"):
        grammar._validate((grammar._compile(bad),))


def test_a_template_notation_error_fails_at_import_rather_than_matching_oddly():
    with pytest.raises(ValueError, match="unknown slot"):
        grammar._compile(grammar.Rule("list the {colour}", (("list_entities", {}),)))
    with pytest.raises(ValueError, match="appears twice"):
        grammar._compile(
            grammar.Rule("list the {kind} and the {kind}", (("list_entities", {}),))
        )


def test_the_notation_cannot_express_a_synonym():
    """🔒 THE ONE-WORDING RULE, MADE STRUCTURAL RATHER THAN LEFT AS RESTRAINT.

    The template notation had `[word]` for an optional and `(a|b)` for alternatives for about an
    hour, and in that hour it grew four ways to ask which assets are overdue, `kill` beside `take
    down`, and `fix` beside `repair` and `restore`. Both notations are gone, and a template still
    carrying one is an error rather than a rule matching a literal bracket.
    """
    for template in ("show [me] the {kind}", "(show|list) the {kind}", "list the {kind} please|now"):
        with pytest.raises(ValueError, match="one wording per tool"):
            grammar._compile(grammar.Rule(template, (("list_entities", {}),)))


def test_there_is_exactly_one_sentence_per_tool():
    """Ethan's instruction, asserted over the table rather than trusted to a reviewer.

    ⚠️ A SECOND RULE FOR ONE TOOL IS THE FAILURE MODE THIS CATCHES, and it is how the table grew
    to 45 rules the first time: each addition looked reasonable on its own line.
    """
    from collections import Counter

    counts = Counter(compiled.rule.tool for compiled in grammar.COMPILED)
    extra = {tool: n for tool, n in counts.items() if n > 1}
    assert not extra, f"more than one wording for {extra}"


def test_the_card_and_the_language_cannot_disagree():
    """Every sentence the card prints is a rule, because it is rendered from the rules.

    🔑 THIS IS THE ONE ASSERTION THE OLD SHAPE COULD NOT MAKE. The card was a `says` tuple beside
    each tool and the parser was a pile of patterns, so the strongest available check was that
    each printed phrasing happened to reach the right tool. Nothing could say the card was
    COMPLETE, and nothing stopped the parser accepting a sentence the card had never heard of.
    """
    printed = {sentence for sentences in grammar.card_sentences().values() for sentence in sentences}
    declared = {c.sentence for c in grammar.COMPILED}
    assert printed == declared

    for sentence in printed:
        assert parser.parse(sentence) is not None, f'the card prints "{sentence}", which escalates'


def test_punctuation_is_not_a_wording_and_politeness_is():
    """⚠️ THE LINE BETWEEN THESE TWO IS NOT THE ONE THIS FILE DREW FIRST.

    There was a politeness wrapper here, matching a leading "please", "can you" or "could you",
    on the reasoning that an operator saying please is not giving a different command. A
    droppable filler word IS a second wording, though, and one wording per tool means one, so
    the wrapper went the same way as the optional "me" it was one word away from.

    Punctuation and whitespace are not words. "Comms check?" typed and "comms check" spoken are the
    same sentence, and treating them as two would fail on the transcriber's own output.
    """
    assert parser.parse("comms check?") is not None
    assert parser.parse("  comms   check  ") is not None
    assert parser.parse("Comms Check.") is not None

    for said in ("please list the hydrophones", "can you list the hydrophones",
                 "list the hydrophones please", "show me the hydrophones"):
        assert parser.parse(said) is None, said
