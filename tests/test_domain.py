"""The declaration in `domain.py` is only worth having if something checks against it.

🔑 WHAT THESE TESTS ARE FOR, and it is not the declaration itself. Every bug this file
guards against has the same shape: a kind was added or changed, and one of the places that
needed to know about it was not updated, so an ABSENCE looked exactly like a DECISION.
Nothing could tell the difference by reading, because both are "no entry".

The real case: `launch_site`, `aircraft` and `ground_party` had no overdue threshold, so
none of them could ever be reported late however long they were silent, and a ground party
untracked for hours read nominal beside a vessel in the identical state reading overdue.
`radar` had no threshold either and that one was correct. Four absences, one of them
deliberate, and no way to see which.

A test that names the exception is what separates them.
"""
from __future__ import annotations

import pytest

from api._lib import detect, domain, freshness, mesh, motion


def test_every_kind_that_can_be_late_has_a_threshold():
    """The check that would have caught the three kinds nobody gave a threshold to.

    ⚠️ `radar` IS THE ONE ALLOWED ABSENCE and it is named here rather than inferred, so
    adding a second silent kind fails instead of quietly joining it.
    """
    missing = sorted(domain.REPORTING_KINDS - set(freshness.OVERDUE_MINUTES))
    assert not missing, (
        f"{missing} can carry a last_heard and so can go quiet, but nothing says when to "
        "call them late. Either give each a threshold or declare reports=False with a reason."
    )


def test_the_kind_that_never_reports_has_no_threshold():
    """The other half, and it is the half that keeps the first one honest.

    Handing `radar` a threshold to make the check above pass would put twelve permanently
    overdue sites on the map and bury the assets that really are late.
    """
    silent = sorted(set(domain.KINDS) - domain.REPORTING_KINDS)
    assert silent == ["radar"], "the set of kinds that never report changed; say why"
    for kind in silent:
        assert kind not in freshness.OVERDUE_MINUTES, (
            f"{kind} does not report at all, so a staleness threshold on it can only ever "
            "mark it late for a network it was never on"
        )


def test_no_threshold_names_a_kind_that_does_not_exist():
    """A threshold for a kind that was renamed or removed is dead configuration that still
    reads as a decision."""
    unknown = sorted(set(freshness.OVERDUE_MINUTES) - set(domain.KINDS))
    assert not unknown, f"{unknown} have thresholds but are not kinds"


def test_the_derived_sets_are_the_only_copies():
    """`detect` and `motion` each held their own `CONTACT_KINDS`, and `mesh` its own
    `MESH_KINDS`. Three hand-written sets answering two questions is two chances to drift,
    and nothing claimed to be the original. They are derived now, and this pins that."""
    assert detect.CONTACT_KINDS is domain.CONTACT_KINDS
    assert motion.CONTACT_KINDS is domain.CONTACT_KINDS
    assert mesh.MESH_KINDS is domain.MESH_KINDS


@pytest.mark.parametrize("kind", sorted(domain.KINDS))
def test_every_kind_is_declared_completely(kind: str):
    """Adding a kind means answering every question about it, not the ones that happened to
    come up. A `KindSpec` with a missing answer cannot be constructed, so this checks the
    values are sane rather than merely present."""
    spec = domain.KINDS[kind]
    assert spec.relationship in ("ours", "third_party", "contact")
    # A label only has to be readable, not different. `vessel` and `hydrophone` are already
    # the words a person would use, and demanding a synonym would invent jargon.
    assert spec.label, "every kind needs a label a person can read"
    # Only things we operate carry our traffic. A contact on our mesh would mean we were
    # relaying for something we cannot identify.
    if spec.on_mesh:
        assert spec.relationship == "ours"
    # Anything that moves needs a way to be drawn moving, and anything static must not be.
    assert spec.mobile == (kind in domain.MOBILE_KINDS)


def test_every_sensor_sees_something_that_exists():
    """A sensor declaring it can detect a kind the world does not have is a capability that
    can never fire, and it reads on the page as coverage the network does not have."""
    for name, sensor in detect.SENSORS.items():
        assert sensor.sees, f"{name} detects nothing at all"
        unknown = sorted(sensor.sees - set(domain.KINDS))
        assert not unknown, f"{name} claims to see {unknown}, which are not kinds"
        not_contacts = sorted(sensor.sees - domain.CONTACT_KINDS)
        assert not not_contacts, (
            f"{name} claims to see {not_contacts}, which are ours or a known third party. "
            "Only contacts are detected; everything else reports its own position."
        )


def test_every_contact_kind_is_visible_to_at_least_one_sensor():
    """A contact kind nothing can detect is a hole in the network that looks like a quiet
    ocean. If one is genuinely undetectable, this test is where that gets said out loud."""
    seen = {kind for sensor in detect.SENSORS.values() for kind in sensor.sees}
    blind = sorted(domain.CONTACT_KINDS - seen)
    assert not blind, f"nothing on this map can detect {blind}"
