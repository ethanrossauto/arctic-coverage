"""What each kind of thing on the map *is*. The declaration lives here and nowhere else.

🔑 WHY THIS MODULE EXISTS, and it is the same argument `freshness.py` makes for itself. One
question, "is this ours?", was being answered independently in four places: `detect` held a
`CONTACT_KINDS` set, `motion` held a second copy of the same set, `mesh` held a `MESH_KINDS`
set overlapping it, and the seed wrote an `owned` flag onto radar sites and nothing else.
Four answers to one question is three chances to drift, and the copies could not be checked
against each other because nothing claimed to be the original.

⚠️ THE DUPLICATE WAS DELIBERATE, WHICH IS WHY A BOTTOM MODULE IS THE FIX. `motion` could not
import the set from `detect`, because `detect` imports `motion`. Copying it was the only
move available at the time. This module imports nothing but the standard library, so both
sides can ask it without a cycle and neither has to keep a copy.

🔑 FOUR FACTS, NOT ONE FLAG. A single `owned` boolean puts a NORAD radar site and an
unidentified vessel in the same bucket, and telling those apart is most of what the display
is for. They separate cleanly:

    relationship  ours | third_party | contact   who operates it
    on_mesh       bool                           can it carry our traffic
    mobile        bool                           can it move under its own power
    reports       bool                           does it ever carry a `last_heard`

A radar site is `third_party`, not on the mesh, not mobile, and does not report: a known,
fixed installation we do not operate and nothing here observes. An unidentified vessel is a
`contact`, not on the mesh, mobile, and it does report, because a fix from a sensor is a
fix. Under one boolean both are simply "not ours".

⚠️ EVERY FIELD HERE MUST DECIDE SOMETHING. `mesh.py` records what happened to the last
constant that stopped deciding anything and stayed quoted as the answer: it went stale
silently and was worse than no constant at all. Each of these four is read: `relationship`
by the payload and the banner, `on_mesh` by the link graph, `mobile` by the motion rules,
and `reports` by the check that every kind which can be late has a threshold.

⚠️ `reports` WAS CUT FROM THE FIRST DRAFT OF THIS FILE AS A DUPLICATE OF `on_mesh`, which
was wrong and is worth leaving written down. They agree on the five kinds that are ours and
disagree on all four that are not: contacts are off the mesh and still have a `last_heard`.
Cutting it would have left the completeness check below with no rule to check against, which
is the check that catches a kind nobody gave a threshold to.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Relationship = Literal["ours", "third_party", "contact"]


@dataclass(frozen=True)
class KindSpec:
    """What one kind of thing is, independent of any particular instance."""

    relationship: Relationship
    on_mesh: bool
    mobile: bool
    #: Does this kind ever carry a `last_heard`? Everything we get position fixes for does,
    #: whether it reports to us over the mesh (ours) or is fixed by a sensor or a broadcast
    #: (contacts). A radar site does not: it is not on our network and nothing here observes
    #: it, so it has no freshness at all and cannot be late.
    #
    # ⚠️ THIS IS NOT `on_mesh` UNDER ANOTHER NAME, and it was cut from an earlier draft of
    # this file on exactly that mistaken reasoning. Contacts are not on the mesh and still
    # have a `last_heard`, because a fix from a sensor is a fix. The two columns agree on
    # five kinds and disagree on four, which is the whole reason both have to exist.
    reports: bool
    label: str


# 🔑 THE ONE TABLE. Everything below is derived from it, so adding a kind is one row here
# and the derived sets, the completeness tests and the payload all follow.
KINDS: dict[str, KindSpec] = {
    # Ours: we operate them, they carry our traffic, and they owe us a report.
    "node": KindSpec("ours", on_mesh=True, mobile=False, reports=True, label="mesh node"),
    "hydrophone": KindSpec("ours", on_mesh=True, mobile=False, reports=True, label="hydrophone"),
    "launch_site": KindSpec(
        "ours", on_mesh=True, mobile=False, reports=True, label="forward location site"
    ),
    "uas": KindSpec("ours", on_mesh=True, mobile=True, reports=True, label="uncrewed aircraft"),
    "patrol": KindSpec("ours", on_mesh=True, mobile=True, reports=True, label="ranger patrol"),
    # Third party: known, fixed, cooperating, and not ours to task. Not on our mesh, which
    # is the interoperability problem stated as data rather than as prose.
    "radar": KindSpec(
        "third_party", on_mesh=False, mobile=False, reports=False, label="air surveillance radar"
    ),
    # Contacts: the world being observed. They move whether or not we hold them.
    "vessel": KindSpec("contact", on_mesh=False, mobile=True, reports=True, label="vessel"),
    "aircraft": KindSpec("contact", on_mesh=False, mobile=True, reports=True, label="aircraft"),
    "ground_party": KindSpec(
        "contact", on_mesh=False, mobile=True, reports=True, label="ground party"
    ),
}


def spec(kind: str | None) -> KindSpec | None:
    """The declaration for a kind, or None for something this model does not describe.

    Returning None rather than a default is deliberate: a marker an operator placed is not
    a kind of asset, and guessing a relationship for it would put an invented fact on the
    display.
    """
    return KINDS.get(kind or "")


def relationship_of(kind: str | None) -> Relationship | None:
    s = spec(kind)
    return s.relationship if s else None


def is_ours(kind: str | None) -> bool:
    return relationship_of(kind) == "ours"


def is_contact(kind: str | None) -> bool:
    return relationship_of(kind) == "contact"


#: Kinds that are the world being observed rather than kit of ours. Their motion is ground
#: truth and does not depend on our hearing from them.
CONTACT_KINDS: frozenset[str] = frozenset(k for k, v in KINDS.items() if v.relationship == "contact")

#: Kinds that can carry mesh traffic. Anything absent is not on the network at all, and
#: cannot be overdue to a network it was never on.
MESH_KINDS: frozenset[str] = frozenset(k for k, v in KINDS.items() if v.on_mesh)

#: Kinds that can move under their own power. A static kind has a speed of zero and no
#: heading, and that is a declared fact rather than a missing value.
MOBILE_KINDS: frozenset[str] = frozenset(k for k, v in KINDS.items() if v.mobile)

#: Kinds that ever carry a `last_heard`, and therefore the kinds that can be late. The one
#: absence is `radar`, which is stated in `freshness.OVERDUE_MINUTES` as a deliberate
#: exception and is checked against this set by the suite.
REPORTING_KINDS: frozenset[str] = frozenset(k for k, v in KINDS.items() if v.reports)
