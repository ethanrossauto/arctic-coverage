"""Which sensor is holding which contact, computed rather than written into the seed.

WHY THIS MODULE EXISTS. The nodes used to be scenery. Twenty-four of them sat on real
shorelines, meshed correctly, carried a `payload` string, and detected nothing, because
nothing read that string. The two contacts the console is built around were credited to a
hydrophone by a hand-written field, and one of those credits named a sensor 355 km away.

A seeded credit cannot be wrong in a way you can see. A computed one can, which is the
property that makes it worth having, and it is the same argument the link graph makes.

🔑 THE POINT IS THAT THE SENSORS DISAGREE. A contact running with everything switched off
is invisible to the longest-ranged sensor here and visible only to the shortest. That is
the whole argument for putting three kinds of sensor on one shoreline instead of buying
more of the best one, and until now this project asserted it in prose without ever
demonstrating it.

    rf         45 km   but ONLY against something that is transmitting
    eo_ir      15 km   sees anything above the surface, and is the one that identifies
    magnetic    4 km   steel hull or vehicle, works on something completely silent
    acoustic   18 km   the hydrophone array, and only against something in the water

⚠️ WHAT THIS DELIBERATELY DOES NOT MODEL: weather, darkness, sea state, target size, the
difference between detecting something and identifying it, and the fact that a real EO/IR
range collapses in fog. Every one of those makes detection WORSE than this predicts, so
the picture here is an optimistic upper bound, exactly like the link graph.

🔴 A DETECTION IS NOT THE SAME AS A REPORT. A sensor in a cluster whose uplink is down is
still detecting; nothing it detects is reaching this display. Those are kept apart rather
than collapsed, because the gap between them is the argument for going and fixing the
link, and hiding it would make a broken network look like an empty ocean.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .mesh import haversine_km, mesh_status

# Anything that can be held by a sensor. Everything else on the map is ours.
CONTACT_KINDS: frozenset[str] = frozenset({"vessel", "aircraft", "ground_party"})


@dataclass(frozen=True)
class Sensor:
    range_km: float
    sees: frozenset[str]
    # True when the sensor is passive against emissions: it hears a transmitter and
    # nothing else. A contact that has switched everything off is invisible to it.
    needs_emission: bool
    label: str


SENSORS: dict[str, Sensor] = {
    # Electro-optical and infrared. The one that tells you WHAT something is rather than
    # only that it is there, which is why it is worth its short range.
    "eo_ir": Sensor(
        range_km=15.0,
        sees=frozenset({"vessel", "aircraft", "ground_party"}),
        needs_emission=False,
        label="electro-optical / infrared",
    ),
    # Picks up radar and communications emissions. Much the longest reach here, and
    # completely defeated by switching the transmitter off.
    "rf": Sensor(
        range_km=45.0,
        sees=frozenset({"vessel", "aircraft", "ground_party"}),
        needs_emission=True,
        label="RF emission",
    ),
    # Magnetic anomaly: a steel hull or a vehicle distorting the local field. Very short,
    # and it does not care in the slightest how quiet the target is being.
    "magnetic": Sensor(
        range_km=4.0,
        sees=frozenset({"vessel", "ground_party"}),
        needs_emission=False,
        label="magnetic anomaly",
    ),
    # The hydrophone array. Underwater sound, so a surface ship radiates into it whether
    # it wants to or not, and an aircraft is simply not in the medium.
    "acoustic": Sensor(
        range_km=18.0,
        sees=frozenset({"vessel"}),
        needs_emission=False,
        label="acoustic",
    ),
}


def sensor_for(asset: dict[str, Any]) -> tuple[str, Sensor] | None:
    """The sensor this asset carries, or None if it is not a sensor.

    A hydrophone's payload is its whole reason for existing so it is not written in the
    seed; a node's is, because a node is a mast that could carry any of them.
    """
    kind = asset.get("kind")
    if kind == "hydrophone":
        return "acoustic", SENSORS["acoustic"]
    if kind != "node":
        return None
    payload = (asset.get("props") or {}).get("payload")
    if not isinstance(payload, str):
        # A node with no payload carries no sensor. Returning None says that; guessing a
        # default would invent a detection capability nobody configured.
        return None
    sensor = SENSORS.get(payload)
    return (payload, sensor) if sensor else None


def _emitting(contact: dict[str, Any]) -> bool:
    """Is this contact transmitting anything an RF sensor could hear?

    Defaults to True, because transmitting is the normal state of a ship or an aircraft
    and going silent is the exception worth marking in the data.
    """
    props = contact.get("props") or {}
    if "emitting" in props:
        return bool(props["emitting"])
    return True


def _range_for(sensor: Sensor, asset: dict[str, Any]) -> float:
    """The sensor's range, letting a single asset override it in `props`.

    The hydrophone array already carries `detection_radius_km` and it is quoted in the
    interface, so the seed stays the authority for that one rather than this table
    silently disagreeing with a number already on screen.
    """
    override = (asset.get("props") or {}).get("detection_radius_km")
    return float(override) if override is not None else sensor.range_km


def detections(assets: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every sensor-to-contact pair currently in range, nearest first.

    Each item says which sensor, which contact, how far, and crucially whether the
    detection is REACHING us. `reported` is false when the sensor is holding something and
    its route back to a gateway is down, which is the state worth surfacing rather than
    hiding: the ocean is not empty, we simply cannot hear the thing that can see it.
    """
    rows = list(assets)
    contacts = [
        a
        for a in rows
        if a.get("kind") in CONTACT_KINDS
        and a.get("lat") is not None
        and a.get("lon") is not None
    ]
    if not contacts:
        return []

    reachable = set(mesh_status(rows)["server_reachable"])

    out: list[dict[str, Any]] = []
    for asset in rows:
        found = sensor_for(asset)
        if found is None or asset.get("lat") is None:
            continue
        payload, sensor = found
        reach = _range_for(sensor, asset)
        for contact in contacts:
            if contact["kind"] not in sensor.sees:
                continue
            if sensor.needs_emission and not _emitting(contact):
                continue
            d = haversine_km(asset["lat"], asset["lon"], contact["lat"], contact["lon"])
            if d > reach:
                continue
            out.append(
                {
                    "sensor_id": asset["id"],
                    "sensor_name": asset.get("name"),
                    "sensor_type": payload,
                    "sensor_label": sensor.label,
                    "contact_id": contact["id"],
                    "contact_name": contact.get("name"),
                    "contact_kind": contact["kind"],
                    "distance_km": round(d, 1),
                    "range_km": reach,
                    "reported": asset["id"] in reachable,
                }
            )
    out.sort(key=lambda d: (d["contact_id"], d["distance_km"]))
    return out


def held_by(assets: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Detections grouped by contact, so "what is holding this" is one lookup.

    A contact with an empty list is being tracked by nothing at all, which for something
    running dark is the answer that matters most.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for d in detections(assets):
        grouped.setdefault(d["contact_id"], []).append(d)
    for a in assets:
        if a.get("kind") in CONTACT_KINDS:
            grouped.setdefault(a["id"], [])
    return grouped


def _self_reporting(contact: dict[str, Any]) -> bool:
    """Is this contact announcing its own identity and position?

    A ship with AIS on and an aircraft with a transponder on are telling us where they
    are. We do not need a sensor to hold them and it is not interesting that none does.
    """
    if contact.get("ais_reporting") is not None:
        return bool(contact["ais_reporting"])
    props = contact.get("props") or {}
    if "transponder" in props:
        return bool(props["transponder"])
    return bool(props.get("emitting", False))


def coverage_summary(assets: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """One line for the status strip: which contacts do we actually have, and how.

    🔑 FOUR BUCKETS, BECAUSE AN OPERATOR DOES SOMETHING DIFFERENT ABOUT EACH.

        self_reporting          it is telling us where it is. Nothing to do
        tracked                 it is not talking, but a sensor holds it and that
                                detection is reaching us. This is the system working
        detected_not_reported   a sensor holds it and cannot deliver. A LINK problem:
                                go and restore the route, the information already exists
        untracked               nothing holds it and it is not talking. A COVERAGE
                                problem, and the only one of the four worth an alarm

    ⚠️ SEPARATING THE FIRST BUCKET IS NOT COSMETIC. An earlier version of this counted a
    broadcasting cargo ship as "untracked" because no sensor happened to be near it, which
    made the number meaningless: it was dominated by vessels whose position was never in
    doubt, and it buried the one contact that genuinely nobody can see.
    """
    rows = list(assets)
    grouped = held_by(rows)
    by_id = {a["id"]: a for a in rows if a.get("kind") in CONTACT_KINDS}

    announcing, tracked, stranded, untracked = [], [], [], []
    for cid, ds in grouped.items():
        if _self_reporting(by_id.get(cid, {})):
            announcing.append(cid)
        elif any(d["reported"] for d in ds):
            tracked.append(cid)
        elif ds:
            stranded.append(cid)
        else:
            untracked.append(cid)
    return {
        "self_reporting": sorted(announcing),
        "tracked": sorted(tracked),
        "detected_not_reported": sorted(stranded),
        "untracked": sorted(untracked),
        "counts": {
            "contacts": len(grouped),
            "self_reporting": len(announcing),
            "tracked": len(tracked),
            "detected_not_reported": len(stranded),
            "untracked": len(untracked),
        },
    }
