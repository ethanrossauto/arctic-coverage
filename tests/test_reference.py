"""The command reference has to be true, and this is what makes it true.

🔑 A REFERENCE THAT LISTS A PHRASING THE PARSER DOES NOT ANSWER IS WORSE THAN NO REFERENCE.
The card exists to show an operator how to reach the deterministic tier: the fast, free path
that keeps working when the model is unreachable. If a line on it escalates or fails, the
operator has been taught a command that does not work, and the next thing they learn is not
to trust the card. Then the whole mechanism is dead weight.

So every phrasing shown is exercised against the real parser here. `parser.parse` is pure,
takes no database and touches no network, so this costs nothing and cannot be skipped.

⚠️ WHAT CHANGED, AND WHY THESE TESTS STILL EARN THEIR PLACE. The card used to be a `says`
tuple beside each tool: a second copy of a sentence the parser also had to know, which is why
these tests were written to check that the two agreed. The sentence now lives once, in
`grammar.RULES`, and the card is a rendering of it, so agreement is structural rather than
checked. What is left to check is the part rendering cannot guarantee: that the sentence a
person reads off the screen still runs, that every tool has one, and that none of them is
recognised only in order to be refused.

⚠️ THIS IS THE SAME SHAPE AS THE SPOKEN-VOCABULARY TEST, and for the same reason: a surface a
person reads off the screen has to be pinned to the code that answers it, or the two drift and
only a live demo finds out.
"""
from __future__ import annotations

import pytest

from api._lib import grammar, parser, tools

CASES = [
    (name, phrase) for name in sorted(tools.REGISTRY) for phrase in tools.says(name)
]


def test_every_tool_offers_at_least_one_phrasing():
    """A tool nobody can be told how to reach is a tool an operator cannot use deliberately.

    Tier 1 is only a control surface if its vocabulary is visible; otherwise it is a cache
    that happens to answer sometimes.
    """
    # ⚠️ ONE EXEMPTION, NAMED. `edit_asset` is reached by the pencil in the panel and by
    # nothing else, so it has no phrasing to teach and must not appear on a card whose whole
    # promise is that everything printed on it can be said.
    mouse_only = {"edit_asset"}
    silent = sorted(
        name for name in tools.REGISTRY if not tools.says(name) and name not in mouse_only
    )
    assert not silent, f"{silent} have no sayable phrasing, so the reference cannot show them"


def test_each_tool_shows_exactly_one_phrasing():
    """One canonical sentence per tool, not a menu of synonyms.

    🔑 THE GRAMMAR STILL ACCEPTS MORE THAN THIS, AND THE DIFFERENCE IS PARAMETERS RATHER THAN
    SYNONYMS. "Show the overdue nodes" and "which nodes are down" are declared variants of the
    sentence printed for `list_entities`: same verb, extra filter. What the card must not become
    is three ways to say one thing, because then an operator has to choose before they can act
    and the one they remember is whichever they read last.
    """
    extra = {
        name: tools.says(name)
        for name in tools.REGISTRY
        if len(tools.says(name)) != 1 and name != "edit_asset"
    }
    assert not extra, f"one phrasing per tool on the card; these have another count: {extra}"


def test_no_two_sentences_open_with_the_same_word():
    """🔴 THE RULE THAT DECIDES WHETHER A TOOL EXISTS AT ALL (Ethan, 2026-08-14).

    Four sentences used to start with `show`: the asset list, the ice overlay, coverage and the
    unknown contacts. An operator hearing "show" learned nothing about which of four things they
    were about to get, and the reference card had to carry a gloss per line to undo a collision
    the language itself had created.

    🥇 IT IS ALSO THE REDUNDANCY TEST, WHICH IS WHY IT IS PINNED RATHER THAN LEFT AS A HABIT. A
    tool that cannot be given a verb nobody else uses is a filter or a synonym wearing a tool's
    clothes: `show_unknown` failed it and was deleted the same day. The next tool to be added
    here has to earn a word before it can earn a sentence.
    """
    openings: dict[str, list[str]] = {}
    for rule in grammar.RULES:
        openings.setdefault(rule.template.split()[0], []).append(rule.template)
    clashes = {word: says for word, says in openings.items() if len(says) > 1}
    assert not clashes, f"one verb, several commands: {clashes}"


def test_the_commands_a_refusal_offers_are_commands_tier_one_answers():
    """🔴 THE ONE PLACE A STALE EXAMPLE IS UNFORGIVABLE, and it was stale for weeks.

    This list is printed when tier 2 is unavailable, described as commands that work "including
    when the metered layer does not". Two of its three did not parse at all: they were written
    against the keyword parser, which matched "overdue" anywhere in a sentence and dropped "me"
    as filler, and nothing re-read them when the grammar became anchored. So the sentence shown
    to an operator BECAUSE the model was down recommended two commands that need the model.
    """
    offered = grammar.suggestions()
    assert offered, "a refusal that suggests nothing is not doing its job"
    for command in offered:
        plan = parser.parse(command)
        assert plan is not None, (
            f"a refusal offers {command!r} as a command that works without the model, "
            "and tier 1 does not answer it"
        )


def test_every_tool_has_an_operator_facing_name():
    """A tool the card cannot name prints its Python identifier at a visitor.

    🔴 THE FALLBACK IS THE REASON THIS EXISTS. `reference()` drops back to `t.name` when a tool
    is missing from `CARD`, which is the right thing for it to do and completely silent: the
    card renders, nothing errors, and one line reads `(list_entities - )` in a panel that is
    the first thing a stranger opens. A new tool is exactly when this happens, and a new tool
    is exactly when nobody is looking at the reference card.
    """
    missing = sorted(set(tools.REGISTRY) - set(tools.CARD))
    assert not missing, (
        f"{missing} have no entry in CARD, so the reference card would print their Python "
        "identifier and an empty description"
    )


def test_no_two_tools_share_an_operator_facing_name():
    """Two tools with one name is the confusion the card exists to remove."""
    seen: dict[str, list[str]] = {}
    for name, card in tools.CARD.items():
        seen.setdefault(card.label, []).append(name)
    clashes = {label: names for label, names in seen.items() if len(names) > 1}
    assert not clashes, f"one name, two tools: {clashes}"


def test_every_phrasing_belongs_to_a_real_group():
    known = {key for key, _ in tools.GROUPS}
    wrong = sorted(
        f"{name}:{t.group}" for name, t in tools.REGISTRY.items() if t.group not in known
    )
    assert not wrong, f"{wrong} sit under a heading the card does not render"


def test_the_card_shows_every_tool_the_grammar_teaches():
    """No tool reachable by a printed sentence may be missing from the card, or the reverse.

    The card is rendered from the grammar, so this cannot drift by an edit to one of two lists.
    What it can still catch is a rule declared `card=True` for a tool the registry does not
    have, which is a typo in a tool name and would otherwise show a heading with a sentence
    under it that reaches nothing.
    """
    taught = set(grammar.card_sentences())
    unknown = sorted(taught - set(tools.REGISTRY))
    assert not unknown, f"the grammar prints sentences for tools that do not exist: {unknown}"


@pytest.mark.parametrize(("name", "phrase"), CASES, ids=[f"{n}: {p}" for n, p in CASES])
def test_the_phrasing_on_the_card_actually_works(name: str, phrase: str):
    """The load-bearing one. Say what is printed and the deterministic tier answers it.

    ⚠️ MEMBERSHIP, NOT EQUALITY, because one sentence may legitimately expand into several
    steps: "isolate Daymark 01" is a camera move, a resolve and a detail open, and the tool it
    is filed under is the first of the three. What matters is that the tool the operator was
    pointed at is in the plan.
    """
    plan = parser.parse(phrase)
    assert plan is not None, (
        f'the reference shows "{phrase}" under {name}, but tier 1 does not recognise it, so '
        "it escalates to the model: slower, paid, and not what the card promised"
    )
    used = [step.get("tool") for step in plan]
    assert name in used, (
        f'the reference shows "{phrase}" under {name}, but tier 1 answers it with {used}'
    )


def test_no_phrasing_is_refused_outright():
    """A phrase the parser recognises in order to DECLINE would be a card teaching a refusal."""
    for name, phrase in CASES:
        refusal = parser.unsupported(phrase)
        assert refusal is None, f'"{phrase}" ({name}) is declined by the parser: {refusal}'
