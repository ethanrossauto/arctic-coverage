"""Every field an asset can carry, declared once.

🔑 WHY THIS EXISTS, AND IT IS THE SAME ARGUMENT AS `domain.py` ONE LEVEL DOWN. `domain`
declares what each KIND is; nothing declared what each FIELD is. So a value's type, its
unit, which kinds it applies to and where it came from were all facts the code knew and
nothing could ask it. The consequences were not subtle:

  * `props` reached 48 keys across 9 kinds with no list of what should be there, so an
    absence and a decision looked identical and neither could be tested.
  * One quantity was stored under three names in two units (`speed_kn`, `speed_kmh`,
    `cruise_kmh`), because nothing said there was one quantity.
  * `flag` meant a vessel's country in `props` and a freshness state at the top level, on
    the same object, because nothing owned the name.
  * The detail panel kept its own hand-written row order, so the operator-facing shape of an
    asset lived in a React component and the server could not see it.

⚠️ IT DECLARES SHAPE, NOT COMPUTATION. Several of these are derived by real code with real
reasons: freshness walks a mesh graph, `held by` runs the detection model. Trying to express
that here would produce a worse version of the code that already does it. What this owns is
the FIELD LIST, its order, and what may be done with each one; the getters live where the
work lives, and `test_fields.py` pins the two together so neither can grow a member the
other has not heard of.

🔑 ORIGIN IS THE INTERESTING COLUMN, and it is what makes the capability question finite.
Rather than arguing 19 fields x 3 capabilities one cell at a time, each field says where its
value came from and the capabilities follow:

    observed   arrived in a report      filterable, NEVER editable
    derived    computed from others     filterable, NEVER editable (edit the inputs)
    asserted   an operator set it       filterable, editable, settable when placing
    seeded     the scenario made it     filterable, editable
    defaulted  nothing said, so a fallback applied

Editing an `observed` value is not a feature, it is falsifying the record: a heading that
arrived over AIS is a fact about a report, and changing it makes the display say a ship was
pointing somewhere it was not. That is why "it is on screen so why can I not edit it" has an
answer rather than an apology.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from . import domain

Origin = Literal["observed", "derived", "asserted", "seeded", "defaulted"]


@dataclass(frozen=True)
class Field:
    """One field, as an operator sees it and as the code stores it."""

    #: The key this travels under. A top-level column, or a key inside `props`.
    name: str
    #: What a person reads on the panel. Never the key: `detection_radius_km` is a column
    #: name and "detects to" is what somebody would say.
    label: str
    type: Literal["text", "number", "boolean", "enum", "time", "position"]
    origin: Origin | tuple[Origin, ...]
    #: Which kinds carry it. Empty means every kind, which is what "same fields, same order"
    #: needs: the panel shows the lot and says N/A where a field does not apply, so a gap is
    #: something you can see rather than something you would have to go looking for.
    applies: frozenset[str] = frozenset()
    unit: str = ""
    note: str = ""
    #: The values this field may take, when they are a closed set.
    #:
    #: 🔑 THIS IS WHY EDITING IS MOUSE-ONLY. Most of what is editable here is a CHOICE from a
    #: short list, not a free value: a payload is one of the sensors that exist, a backhaul is
    #: on or off. Picking from a list is what a dropdown is for and what speech is worst at,
    #: since saying "electro optical infrared" reliably is harder than clicking it and a
    #: mistyped value would be a field set to something no sensor answers to.
    #:
    #: Empty means free entry, bounded by `type`.
    choices: tuple[str, ...] = ()

    def applies_to(self, kind: str) -> bool:
        return not self.applies or kind in self.applies

    @property
    def origins(self) -> tuple[Origin, ...]:
        """Every origin this field's value may have.

        🔑 MORE THAN ONE IS THE NORMAL CASE FOR SOME FIELDS, not an edge case. A ship
        broadcasting AIS reports its own heading, which is observed; a route-follower's is
        derived from the leg it is on. Forcing one origin per field is the same mistake as
        one relationship per kind: it is a level too coarse for the thing being described,
        and it makes the display state as fact something it worked out.
        """
        return self.origin if isinstance(self.origin, tuple) else (self.origin,)

    @property
    def editable(self) -> bool:
        """Observed and derived values are never editable. See the module docstring.

        ⚠️ THE STRICTEST ORIGIN WINS. A field that is sometimes observed is not editable on
        the occasions it happens to be seeded, because an operator cannot see which case
        they are in and the control would appear and disappear under them.
        """
        return all(o in ("asserted", "seeded", "defaulted") for o in self.origins)

    @property
    def settable_on_place(self) -> bool:
        """Only what an operator asserts can be set at the moment of placing something."""
        return self.origins == ("asserted",)


_OURS = frozenset(k for k, v in domain.KINDS.items() if v.relationship == "ours")
_SENSORS = frozenset({"hydrophone", "radar", "node"})


#: 🔑 THE ORDER IS THE PANEL'S ORDER, and it is deliberate rather than historical: what this
#: is, then how we regard it, then how we know about it, then what it is doing, then what it
#: can do, then where it is. Reordering here reorders the display, which is the point of
#: having one list instead of two.
FIELDS: tuple[Field, ...] = (
    Field("relationship", "operated by", "enum", "derived",
          note="ours, another operator, or a contact we merely observe"),
    Field("threat", "regarded as", "enum", "derived",
          note="friendly, hostile, or unknown when nobody has judged it"),
    Field("classification", "class", "text", "observed", applies=domain.CONTACT_KINDS),

    Field("last_heard", "last heard", "time", "observed", unit="min",
          applies=domain.REPORTING_KINDS),
    Field("connections", "connections", "number", "derived", applies=domain.MESH_KINDS,
          note="how many assets it can reach directly on the mesh right now"),
    Field("mesh_connected", "in range of", "boolean", "derived", applies=domain.MESH_KINDS),
    Field("avg_gateway_minutes", "avg connected", "number", "derived", unit="min",
          applies=domain.MESH_KINDS),
    Field("ais_reporting", "broadcasting", "boolean", "observed", applies=frozenset({"vessel"})),
    Field("emitting", "emitting", "boolean", "observed", applies=domain.CONTACT_KINDS),
    Field("confirmed", "confirmed", "enum", "derived", applies=domain.CONTACT_KINDS,
          note="whether the sensor network can actually confirm this contact"),
    Field("held_by", "held by", "text", "derived", applies=domain.CONTACT_KINDS,
          note="the sensor currently holding it"),

    Field("speed_kmh", "speed", "number", "observed", unit="km/h", applies=domain.MOBILE_KINDS),
    # 🔑 OBSERVED **OR** DERIVED, WHICH IS WHY IT IS BACK ON EVERY MOBILE KIND. A ship
    # broadcasting AIS reports its heading; anything following a route has one computed from
    # the leg it is on. Declaring it observed-only forced a choice that is not the world's:
    # it was narrowed to vessels because nothing else reports one, which left four moving
    # kinds showing N/A for a direction the route knew all along.
    #
    # 🔴 AND THE STORED NUMBER WAS OFTEN WRONG. Seeded once and never updated while the
    # vessel walked a route that turns, it had two ships drawn more than 55 degrees off
    # their actual course. `motion.heading_of` computes it now and says which origin applied.
    Field("heading_deg", "heading", "number", ("observed", "derived"), unit="deg",
          applies=domain.MOBILE_KINDS),
    Field("alt_m", "altitude", "number", "derived", unit="m"),

    # 🔑 DERIVED, NOT SEEDED. A hydrophone and a radar carry the number; a node's comes from
    # its payload through the sensor table. The server computes the effective one so the
    # panel cannot say N/A about an asset that is demonstrably detecting something.
    Field("detection_radius_km", "detects to", "number", "derived", unit="km",
          applies=_SENSORS),
    Field("flight_radius_km", "can reach", "number", "asserted", unit="km",
          applies=frozenset({"uas"}), note="how far it can be tasked, out and back"),
    # ⚠️ NODES ONLY. A drone's payload was in the seed and read by nothing:
    # `detect.sensor_for` handles hydrophones and nodes, so a drone detected nothing. It went
    # with the props trim, and promising it here would put N/A on a row nothing can fill.
    # ⚠️ DERIVED FROM THE SENSOR TABLE, NOT LISTED AGAIN. A payload this display does not
    # model is a mast that detects nothing, and `detect.sensor_for` returning None for an
    # unknown string is a silent capability loss rather than an error.
    Field("payload", "payload", "text", "seeded", applies=frozenset({"node"}),
          choices=("eo_ir", "rf", "magnetic")),
    Field("cluster_name", "cluster", "text", "seeded", applies=frozenset({"node"})),
    # ⚠️ NOT EVERY ASSET OF OURS. A drone carries no satellite terminal, so `_OURS` was too
    # wide and promised a field four kinds never produce.
    Field("backhaul", "backhaul", "text", "seeded",
          applies=frozenset({"hydrophone", "launch_site", "node", "patrol"}),
          choices=("satellite", "none")),

    Field("position", "position", "position", "derived", note="latitude and longitude"),
)

BY_NAME: dict[str, Field] = {f.name: f for f in FIELDS}


def schema() -> list[dict[str, object]]:
    """The declaration as data, for the client and for anything that wants to check it."""
    return [
        {
            "name": f.name,
            "label": f.label,
            "type": f.type,
            "unit": f.unit,
            "origin": list(f.origins),
            "applies": sorted(f.applies) if f.applies else [],
            "editable": f.editable,
            "settable_on_place": f.settable_on_place,
            "note": f.note,
            "choices": list(f.choices),
        }
        for f in FIELDS
    ]
