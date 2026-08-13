"""The field declaration is only worth having if it is the same list the world produces.

🔑 THE FAILURE THIS CLOSES. `props` reached 48 keys across 9 kinds with nothing saying what
should be there, so nobody could answer two questions that matter: is this field supposed to
exist, and is it missing because it does not apply or because someone forgot? Both look
identical when the only record of a field is that somebody wrote it once.

So this pins the declaration to the seed in BOTH directions. A key the world produces and
the declaration has never heard of is an undeclared field; a field declared for a kind the
world never gives it is a promise the panel cannot keep. Either way the answer is a failing
test rather than a blank row somebody notices during a demo.
"""
from __future__ import annotations

from collections import defaultdict

import pytest

from api._lib import assets as seedlib
from api._lib import domain, fields


#: What the seed actually builds, as {prop key: {kinds that carry it}}.
def _seeded_props() -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    # 🔴 FAILS CLOSED ON A NAME IT CANNOT FIND, and the first version did not. It listed
    # `_radar`; the builder is `_radars`, so twelve assets were silently skipped and every
    # check below reported clean on a world it had never looked at. A check that cannot tell
    # "nothing wrong" from "I could not look" is not a check.
    builders = (
        "_nodes", "_patrols", "_uas", "_hydrophones", "_vessels",
        "_aircraft", "_ground_parties", "_radars", "_launch_sites",
    )
    missing_builders = [n for n in builders if not hasattr(seedlib, n)]
    assert not missing_builders, (
        f"{missing_builders} are not in the seed, so this test would examine a partial world "
        "and report clean"
    )
    for name in builders:
        builder = getattr(seedlib, name)
        for asset in builder():
            for key in (asset.props or {}):
                out[key].add(asset.kind)
    return dict(out)


#: Keys the seed writes that are machinery rather than an operator-facing field. Each one is
#: named here with a reason, so "not on the panel" stays a decision somebody made.
NOT_DISPLAYED = {
    # Set by the command layer when a person positions something, and read only by the
    # motion rules to leave it alone. It describes an intervention, not the asset.
    "motion_frozen",
    "station",
    "eta_min",
    "placed_by",
    # The raw judgement behind `threat`, which is the field the panel shows. Displaying
    # both would put one fact on screen twice under two names.
    "hostile",
    # The aircraft equivalent of `emitting`, folded into that row on the panel.
    "transponder",
}


def test_every_seeded_field_is_declared():
    """A key in the world that the declaration has never heard of."""
    undeclared = sorted(set(_seeded_props()) - set(fields.BY_NAME) - NOT_DISPLAYED)
    assert not undeclared, (
        f"the seed writes {undeclared} and nothing declares them, so no test can say whether "
        "they belong, and the panel cannot show them in any particular place"
    )


def test_every_declared_field_is_produced_where_it_applies():
    """A field promised for a kind the world never gives it.

    ⚠️ TOP-LEVEL COLUMNS AND DERIVED VALUES ARE EXEMPT, because they do not live in `props`:
    `alt_m` is a column, `threat` is computed at read time, `position` is two columns. What
    is checked here is the half the seed actually owns.
    """
    seeded = _seeded_props()
    # 🔑 DERIVED FROM THE DECLARATION RATHER THAN LISTED AGAIN. A hand-kept exemption list is
    # the same maintenance problem this whole file exists to remove: it went stale the moment
    # `detection_radius_km` became derived, and the test failed for a reason that had nothing
    # to do with the world.
    # A field that is derived on ANY of its origins is computed at read time for at least
    # some assets, so the seed is not the place to look for it.
    derived_or_column = {f.name for f in fields.FIELDS if "derived" in f.origins} | {
        # Top-level columns rather than props, so the seed writes them through `Asset`
        # fields instead of the dict this walks.
        "last_heard",
        "ais_reporting",
    }
    missing = []
    for field in fields.FIELDS:
        if field.name in derived_or_column:
            continue
        carriers = seeded.get(field.name, set())
        promised = field.applies or set(domain.KINDS)
        # 🔴 EVERY PROMISED KIND, NOT JUST ONE OF THEM. This used to break out of the loop on
        # the first kind that carried the field, so a field declared for three kinds passed
        # while two of them never produced it. `detection_radius_km` was declared for node,
        # radar and hydrophone; only hydrophone had it, and the panel read N/A on a node that
        # detects to 15 km. A test that stops at the first success is a test that only proves
        # the field exists somewhere.
        for kind in sorted(promised):
            if kind not in carriers:
                missing.append(f"{field.name} promised to {kind} and never produced")
    assert not missing, (
        f"declared but never produced: {missing}. Either the seed should write it or the "
        "declaration should not promise it."
    )


@pytest.mark.parametrize("field", fields.FIELDS, ids=[f.name for f in fields.FIELDS])
def test_every_field_is_declared_completely(field: fields.Field):
    # ⚠️ ONLY THAT IT EXISTS. An earlier version required the label to DIFFER from the name,
    # which fails on `position`, `payload` and `emitting`: those names are already the words
    # a person would use, and demanding a synonym would invent jargon to satisfy a test.
    assert field.label, "every field needs a label a person can read"
    assert field.type in ("text", "number", "boolean", "enum", "time", "position")
    assert field.origins, "every field says where its value comes from"
    for origin in field.origins:
        assert origin in ("observed", "derived", "asserted", "seeded", "defaulted")
    unknown = sorted(field.applies - set(domain.KINDS))
    assert not unknown, f"{field.name} applies to {unknown}, which are not kinds"
    # A measurement without a unit is a number nobody can check.
    if field.type == "number" and field.name not in ("connections",):
        assert field.unit, f"{field.name} is a number and says nothing about its unit"


def test_observed_and_derived_values_are_never_editable():
    """🔒 THE RULE THAT MAKES THE CAPABILITY QUESTION FINITE, rather than 20 fields argued
    one at a time. Editing an observed value is falsifying the record: a heading that came
    over AIS is a fact about a report, and rewriting it makes the display say a ship was
    pointing somewhere it never was. A derived value is edited through its inputs or not at
    all, or the two disagree the moment anything recomputes."""
    for field in fields.FIELDS:
        if any(o in ("observed", "derived") for o in field.origins):
            assert not field.editable, f"{field.name} is {field.origins} and must not be editable"
            assert not field.settable_on_place, f"{field.name} is {field.origins}"


def test_one_quantity_is_not_declared_twice():
    """The three-names-for-speed failure, as a test.

    `speed_kn`, `speed_kmh` and `cruise_kmh` were one quantity under three names in two
    units, and every consumer needed the same conversion table. Nothing said they were one
    thing, so nothing could notice.
    """
    labels = [f.label for f in fields.FIELDS]
    assert len(labels) == len(set(labels)), "two fields share a label, so one is a synonym"
    names = [f.name for f in fields.FIELDS]
    assert len(names) == len(set(names))
