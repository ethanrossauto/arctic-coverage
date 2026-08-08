"""Terrain and placement rules.

The regression at the centre of this file is worth stating plainly, because it is the kind
that hides: `check_placement` classified kinds into land, water and any, and anything in
none of the three fell through to "belongs in water". So a kind added to the schema and
never classified here got a confident answer nobody had computed, and the visible symptom
was a ground party refused for standing on the ground.

It survived because nothing could place those kinds yet. A dormant fault in a branch nobody
reaches is still a fault, and the test that would have caught it is the coverage one below,
which needs no knowledge of what the next kind will be.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from api._lib import terrain
from api._lib.assets import KIND_COUNTS

SCHEMA = (Path(__file__).resolve().parents[1] / "db" / "schema.sql").read_text()


def _schema_kinds() -> set[str]:
    """Every kind the database will accept, read out of the check constraint itself.

    Parsed rather than duplicated, so this test cannot drift away from the thing it is
    checking. A hand-copied list here would be a third place to forget.
    """
    match = re.search(r"kind in \(([^)]*)\)", SCHEMA)
    assert match, "could not find the kind check constraint in db/schema.sql"
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


def test_every_schema_kind_has_a_medium_classified():
    """🔴 THE ONE THAT WOULD HAVE CAUGHT THE BUG.

    Adding a kind to the schema without classifying it here used to produce a silent
    misclassification. Now it fails on this line, by name, the moment the constraint moves.
    """
    unclassified = _schema_kinds() - terrain.CLASSIFIED_KINDS
    assert not unclassified, (
        f"these kinds can be stored but have no medium in terrain.py: {sorted(unclassified)}"
    )


def test_every_seeded_kind_has_a_medium_classified():
    """The same guarantee from the other direction: what the seed actually creates."""
    unclassified = set(KIND_COUNTS) - terrain.CLASSIFIED_KINDS
    assert not unclassified, f"seeded but unclassified: {sorted(unclassified)}"


def test_the_three_sets_do_not_overlap():
    """A kind in two sets would take whichever branch ran first, which is not a decision."""
    assert not (terrain.LAND_KINDS & terrain.WATER_KINDS)
    assert not (terrain.LAND_KINDS & terrain.ANY_KINDS)
    assert not (terrain.WATER_KINDS & terrain.ANY_KINDS)


def test_an_unknown_kind_raises_rather_than_being_treated_as_water():
    """Absence must not produce a confident answer. This is the fall-through that bit."""
    with pytest.raises(ValueError, match="no medium classified"):
        terrain.check_placement("submarine", 74.0, -95.0)


# ⚠️ THESE MUST BE WELL CLEAR OF A COASTLINE, NOT MERELY ON THE RIGHT SIDE OF ONE.
#
# The first version of this file used a seeded node position for INLAND and the hydrophone
# array for OPEN_WATER, and two tests failed. That was the code being right and the fixture
# being wrong: both sit within `COASTAL_TOLERANCE_KM` of a shore, where the basemap cannot
# resolve which side a point is on, so `check_placement` deliberately allows anything and
# says nothing rather than guessing.
#
# Which is the whole design. A test of the refusal logic therefore has to stand somewhere
# the check is actually willing to commit: 139 km inland and 23 km offshore.
INLAND = (68.0, -120.0)       # deep in the mainland, ~139 km from the sea
OPEN_WATER = (68.0, -114.5)   # Coronation Gulf, ~23 km from any shore


def test_the_fixtures_are_far_enough_from_shore_for_the_check_to_commit():
    """If these drift inside the tolerance, every refusal test below silently passes.

    Asserting the medium is not enough: the check declines to answer near a coast, so the
    fixture has to be proven to be somewhere it will answer at all.
    """
    assert terrain.is_land(*INLAND) is True
    assert terrain.is_land(*OPEN_WATER) is False
    assert terrain.check_placement("vessel", *INLAND) is not None
    assert terrain.check_placement("node", *OPEN_WATER) is not None


def test_near_a_coastline_the_check_declines_to_guess():
    """The tolerance is a feature and deserves a test of its own, not just a comment.

    A seeded node sits on a shoreline by design, so the basemap cannot tell which side of
    the shore it is on. The honest answer there is silence, and every refusal this module
    does make is defensible because of it.
    """
    on_a_modelled_shoreline = (69.9846, -100.9668)
    assert terrain.check_placement("vessel", *on_a_modelled_shoreline) is None


def test_a_ground_party_belongs_on_the_ground():
    """The exact symptom of the regression: refused for standing on land."""
    assert terrain.check_placement("ground_party", *INLAND) is None
    assert terrain.check_placement("ground_party", *OPEN_WATER) is not None


def test_an_aircraft_has_no_opinion_about_the_surface():
    """It is in the air. Refusing one for being over water was backwards."""
    assert terrain.check_placement("aircraft", *INLAND) is None
    assert terrain.check_placement("aircraft", *OPEN_WATER) is None


def test_a_vessel_belongs_in_water_and_a_node_on_land():
    assert terrain.check_placement("vessel", *OPEN_WATER) is None
    assert terrain.check_placement("vessel", *INLAND) is not None
    assert terrain.check_placement("node", *INLAND) is None
    assert terrain.check_placement("node", *OPEN_WATER) is not None


def test_a_refusal_says_which_medium_and_how_far():
    """A refusal an operator cannot act on is barely better than a silent one."""
    reason = terrain.check_placement("vessel", *INLAND)
    assert reason and "km" in reason


def test_an_impossible_position_is_refused_before_any_terrain_lookup():
    assert terrain.check_placement("node", 91.0, 0.0) is not None
    assert terrain.check_placement("node", 0.0, 181.0) is not None
