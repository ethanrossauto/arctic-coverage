"""The nine asset kinds, and the seeded world.

The world is a Canadian Arctic surveillance picture: a deployable sensor mesh at the
chokepoints, mobile patrols and drones, an under-ice acoustic barrier across the
eastern gate, the contacts on the water, in the air and on the ground that all of it
exists to detect, and the existing early-warning radar line it has to work alongside.

⚠️ THE COUNT IN THIS SENTENCE HAS BEEN WRONG BEFORE. It said five while the table
below listed six and the schema seeded seven, because a number written in prose does
not move when a kind is added. `KIND_COUNTS` at the foot of this file is the list that
the seed script asserts against, so it cannot drift; this table is a description and
has to be edited by hand.

WHY NINE KINDS RATHER THAN ONE. Each has a different geometry, a different mobility
and a different set of questions worth asking about it, and that heterogeneity is the
point: it is what makes a command layer useful rather than decorative. Nine kinds
that were all static points with a name would reduce every command to a search box.

    kind          geometry          mobility      the question it answers
    ---------------------------------------------------------------------------
    node          point             static        what has gone quiet, what is
                                                  about to be cut off from the mesh
    patrol        route + position  ~4 km/h       where are my people, who has
                                                  not checked in
    uas           point + path      ~100 km/h     what can I send, and how long
                                                  until it is on top of that
    launch_site   point             static        where can something come from,
                                                  and where does traffic leave
    hydrophone    point + radius    static        what crossed the barrier without
                                                  being seen
    vessel        track + position  10-20 kn      who is out there, and which of
                                                  them is not broadcasting
    aircraft      point             ~300 kn       what is in the air, and which of
                                                  it has no transponder
    ground_party  point             on foot       who is on the ground out there,
                                                  and is anyone holding them
    radar         point + radius    static        what the existing line already
                                                  covers, and where it does not

🔴 GEOGRAPHY IS NOT DECORATION HERE. Every position is a real place or a real
waterway. The mesh sits on the actual Northwest Passage chokepoints, because that is
where a sensor earns its cost; the hydrophones form one barrier across Lancaster Sound,
the eastern gate everything arriving from the Atlantic has to pass; and the drones are
based at the real northern airfields. Scattering these at random would produce the same
screenshot and would fall apart under one question from anyone who knows the region.

⚠️ THE SEED IS IDEMPOTENT AND MEANT TO BE RE-RUN. `last_heard` values are offsets
from the moment of seeding, so the interesting states (something stale, something
silent) stay interesting rather than ageing into "everything went quiet three days
ago". Re-running the seed before a demo refreshes them, and doubles as the
reset-to-known-state that a public URL needs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

# --------------------------------------------------------------------------
# Real places. Used as patrol endpoints, drone bases and label anchors.
# --------------------------------------------------------------------------

SETTLEMENTS: dict[str, tuple[float, float]] = {
    "Inuvik": (68.3607, -133.7230),
    "Tuktoyaktuk": (69.4454, -133.0342),
    "Paulatuk": (69.3519, -124.0736),
    "Ulukhaktok": (70.7369, -117.7728),
    "Cambridge Bay": (69.1169, -105.0597),
    "Gjoa Haven": (68.6258, -95.8797),
    "Baker Lake": (64.3186, -96.0278),
    "Arviat": (61.1078, -94.0592),
    "Churchill": (58.7684, -94.1647),
    "Resolute Bay": (74.6973, -94.8297),
    "Yellowknife": (62.4540, -114.3718),
    "Iqaluit": (63.7467, -68.5170),
    "Rankin Inlet": (62.8092, -92.0853),
    "Alert": (82.5018, -62.3481),
    "Eureka": (79.9833, -85.9333),
    "Kugluktuk": (67.8256, -115.0969),
    "Bathurst Inlet": (66.8333, -108.3667),
}


@dataclass
class Asset:
    """One row of the entities table, before it becomes SQL."""

    id: str
    kind: str
    name: str
    lat: float | None = None
    lon: float | None = None
    alt_m: float | None = None
    status: str = "nominal"
    geometry: dict | None = None
    props: dict[str, Any] = field(default_factory=dict)
    last_heard_minutes_ago: float | None = None
    ais_reporting: bool | None = None
    created_by: str = "seed"

    def row(self, now: datetime) -> dict[str, Any]:
        last_heard = (
            now - timedelta(minutes=self.last_heard_minutes_ago)
            if self.last_heard_minutes_ago is not None
            else None
        )
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "lat": self.lat,
            "lon": self.lon,
            "alt_m": self.alt_m,
            "status": self.status,
            "geometry": self.geometry,
            "props": self.props,
            "last_heard": last_heard,
            "ais_reporting": self.ais_reporting,
            "created_by": self.created_by,
        }


def line(points: list[tuple[float, float]]) -> dict:
    """A GeoJSON LineString from (lat, lon) pairs.

    Takes lat/lon in that order and emits lon/lat, because GeoJSON is lon-first and
    every position in this file is written lat-first to match how coordinates are
    spoken and read. Converting in one place beats remembering to swap at 40 call
    sites.
    """
    return {"type": "LineString", "coordinates": [[lon, lat] for lat, lon in points]}


# --------------------------------------------------------------------------
# 1. Mesh sensor nodes: 24, in four clusters at the chokepoints
# --------------------------------------------------------------------------
# Clustered rather than spread, because a mesh needs neighbours to be a mesh, and
# because the value of a sensor is set by what passes it. The existing radar line
# across the Arctic is a tripwire; this is area coverage of the places a transit
# cannot avoid.
#
# 🔑 THE SPACING IS DERIVED, NOT CHOSEN. Ground to ground reach is 25 km (see
# api/_lib/mesh.py), so nodes sit about 22 km apart, roughly 88% of it, leaving a couple of
# kilometres of margin for a coastline that is not a straight line and for a neighbour going
# down. That is why the clusters look like short chains rather than a scatter across a map.
#
# ⚠️ This paragraph read "two 12 m masts have a radio horizon of 28.5 km, so nodes sit at
# 77% of that" until the link model became a lookup. The spacing did not change; the
# sentence explaining it described a formula that no longer exists.
#
# 🔑 AND THE POSITIONS ARE ON REAL SHORELINES. Each cluster follows the coast of the
# waterway it watches, generated by scripts/place_nodes.py, which walks the same
# coastline the map draws and steps along it by arc length. An earlier version of this
# table put nodes down the MIDDLE of each strait: every one of them was in open water,
# and the whole set was 68 to 158 km apart, so not one node could reach another and the
# mesh was not a mesh. Both faults were invisible until the link graph was computed.
#
# ⚠️ Re-run that script rather than editing coordinates by hand; it checks that every
# point lands on land and that no gap exceeds the horizon.

_NODE_CLUSTERS: list[tuple[str, str, list[tuple[float, float]]]] = [
(
        "barrow",
        "Barrow Strait / Lancaster Sound",
        [
            (74.7562, -91.7248),
            (74.6860, -91.3714),
            (74.8186, -91.0248),
            (74.7350, -90.8789),
            (74.6382, -90.5531),
            (74.5917, -90.0960),
            (74.5862, -89.6076),
        ],
    ),
    (
        "pow",
        "Prince of Wales Strait / Amundsen Gulf",
        [
            (73.1918, -115.4594),
            (73.1045, -116.0666),
            (73.0105, -116.6259),
            (72.8961, -117.1833),
            (72.7648, -117.6034),
            (72.6285, -118.0739),
        ],
    ),
    (
        "victoria",
        "Victoria Strait / Franklin Strait",
        [
            (69.9846, -100.9668),
            (69.8088, -100.9028),
            (69.6917, -101.0675),
            (69.7784, -101.3937),
            (69.8948, -101.5073),
        ],
    ),
    (
        "nares",
        "Ellesmere / Nares Strait",
        [
            (78.4109, -75.8087),
            (78.5406, -76.6073),
            (78.5594, -75.8761),
            (78.5800, -74.9705),
            (78.7007, -74.8470),
            (78.8596, -75.4786),
        ],
    ),
]

# 🔴 THREE PAYLOADS, AND EACH ONE FAILS DIFFERENTLY. That is the entire reason to mix them
# along a shoreline instead of buying more of the best one, and api/_lib/detect.py is what
# makes the difference real rather than decorative.
#
#   eo_ir     15 km, sees anything above the surface, and is the one that identifies
#   rf        45 km, much the longest reach, and useless against a silent target
#   magnetic   4 km, steel hull, and completely indifferent to how quiet the target is
#
# ⚠️ `acoustic` and `seismic` were removed. Acoustic belongs to the hydrophone array, which
# is in the water where acoustics work. Seismic measures ground movement and was never a
# sensible thing to point at a shipping channel; it was in this list because the list had
# five entries and the map wanted variety.
_PAYLOADS = ["eo_ir", "rf", "magnetic"]

# Staleness is designed, not random. Most nodes are current, a few are late, and two
# are silent, because "what has gone quiet" is only a real question if the answer is
# neither "nothing" nor "everything".
# 🔑 BATTERY IS ITS OWN NUMBER, not a function of lateness. It used to be derived from the
# status value, which meant the seed could not say a node was late with a healthy battery,
# and could not say a node was nearly flat while still reporting on time. Those are two
# facts that usually travel together and sometimes do not, and the interesting rows are
# exactly the ones where they come apart.
_NODE_STALENESS: dict[str, tuple[float, int]] = {
    "node-nares-05": (410.0, 44),    # late, but the battery is fine: something else is wrong
    "node-victoria-03": (1670.0, 0), # nothing for over a day, and flat
    "node-pow-06": (95.0, 18),       # late, and the battery is the obvious reason
    # 🔴 THIS ONE IS THE CLUSTER'S UPLINK, AND THAT IS THE WHOLE POINT OF PUTTING IT HERE.
    # Barrow Strait has been off the air for two days, and not because its sensors failed:
    # every other node in that chain is reporting normally to its neighbours. What broke is
    # the one route out. A display that only coloured the dead node would show one problem;
    # the truthful picture is seven assets nobody has heard from, and one reason.
    "node-barrow-05": (2880.0, 0),
}

# 🔑 ONE NODE PER CLUSTER CARRIES THE SATELLITE UPLINK, and the rest relay inward to it.
# See api/_lib/mesh.py for why a cluster needs its own way out rather than depending on a
# long hop to an airfield.
#
# Each of these is the most central node in its chain, which is where you put the terminal
# if you want no member to be more hops from the exit than it has to be.
_BACKHAUL_NODES: frozenset[str] = frozenset(
    {"node-barrow-05", "node-pow-03", "node-victoria-02", "node-nares-03"}
)


def _nodes() -> list[Asset]:
    out: list[Asset] = []
    for cluster_key, cluster_name, points in _NODE_CLUSTERS:
        for i, (lat, lon) in enumerate(points, start=1):
            node_id = f"node-{cluster_key}-{i:02d}"
            stale_minutes, battery = _NODE_STALENESS.get(
                node_id, (float(7 + (i * 3) % 40), 55 + (i * 7) % 40)
            )
            props = {
                "cluster": cluster_key,
                "cluster_name": cluster_name,
                "payload": _PAYLOADS[(i - 1) % len(_PAYLOADS)],
                "power_source": "solar_wind" if i % 3 else "primary_battery",
                "battery_pct": battery,
            }
            if node_id in _BACKHAUL_NODES:
                props["backhaul"] = "satellite"
            out.append(
                Asset(
                    id=node_id,
                    kind="node",
                    name=f"{cluster_name.split(' /')[0]} {i:02d}",
                    lat=lat,
                    lon=lon,
                    alt_m=float(5 + (i * 11) % 60),
                    last_heard_minutes_ago=stale_minutes,
                    props=props,
                )
            )
    return out


# --------------------------------------------------------------------------
# 2. Ranger patrols: 3
# --------------------------------------------------------------------------
# Three patrols, and they are the only assets on the map that a person is standing on.
# They move on a human timescale, tens of kilometres a day rather than the hundreds a
# vessel covers, which is what makes "where has this been" a different question for a
# patrol than for a ship.
#
# 1 Canadian Ranger Patrol Group genuinely runs long-range patrols of thousands of
# kilometres over weeks. The route here is a working leg of one, not the whole thing,
# because a patrol is only on this display while the display can hear it.

def _patrols() -> list[Asset]:
    # ⚠️ THIS ROUTE IS MAINLAND-ONLY, AND THAT IS A CORRECTION.
    #
    # It used to run Inuvik, Tuktoyaktuk, Paulatuk, Ulukhaktok, Cambridge Bay, Gjoa
    # Haven... and Ulukhaktok, Cambridge Bay and Gjoa Haven are all on ISLANDS. That is a
    # real winter route: a snowmobile patrol crosses Amundsen Gulf and Coronation Gulf on
    # sea ice, and the documented 1 CRPG long-range patrol did exactly that.
    #
    # But this scenario is set in AUGUST, when the Northwest Passage is navigable, which
    # is the whole reason eight vessels are transiting it. In August those crossings are
    # open water, so the seeded patrol was violating the same rule the validator refuses
    # commands for. The world has to obey its own physics before it can refuse anyone
    # else's plan.
    #
    # 🔴 AND EVERY PATROL STAYS INSIDE RELAY RANGE OF A WAY OUT, which is the second
    # correction and the one that moved these routes.
    #
    # A patrol is the asset most likely to walk out of contact, because it is the only one
    # that travels. Each route here keeps every waypoint within 100 km of a backhaul: the
    # cluster uplink or a launch site. 100 km is two 50 km air-to-ground hops, so one drone
    # at the midpoint restores the link from anywhere on any of these routes. Checked
    # against the seed rather than eyeballed, and the worst point on any route is 97.7 km.
    #
    # ⚠️ Every waypoint is also on LAND, for the August reason above. The first version of
    # these two routes ran straight down the middle of Victoria Strait and Prince of Wales
    # Strait, which is open water in August, and `is_land` said so on all nine points.
    #
    # A loop is the patrol's current tasking, not the limit of where it can go. The
    # community it works out of is often further than the loop it is working.
    long_range = [
        (72.41, -118.13), (72.41, -117.63), (73.31, -116.38),
        (73.46, -116.63), (73.61, -116.88),
    ]
    resolute_loop = [
        SETTLEMENTS["Resolute Bay"], (75.20, -95.50), (75.55, -94.00),
        (75.05, -92.80), SETTLEMENTS["Resolute Bay"],
    ]
    cambridge_loop = [
        (69.80, -102.00), (69.95, -101.50), (70.10, -101.00),
        (69.95, -101.00), (69.80, -102.00),
    ]
    return [
        Asset(
            id="patrol-1crpg-lr",
            kind="patrol",
            name="1 CRPG Long Range",
            lat=long_range[1][0],
            lon=long_range[1][1],
            geometry=line(long_range),
            last_heard_minutes_ago=52.0,
            props={
                "members": 14,
                "progress_pct": 48,
                "next_waypoint": "Prince of Wales Strait north",
                "transport": "atv",
                "speed_kmh": 22,
            },
        ),
        Asset(
            id="patrol-resolute",
            kind="patrol",
            name="Resolute Bay Patrol",
            lat=75.20,
            lon=-95.50,
            geometry=line(resolute_loop),
            last_heard_minutes_ago=19.0,
            props={"members": 6, "progress_pct": 22, "next_waypoint": "Cape Martyr", "transport": "snowmobile", "speed_kmh": 18},
        ),
        Asset(
            id="patrol-cambridge",
            kind="patrol",
            name="Cambridge Bay Patrol",
            lat=cambridge_loop[1][0],
            lon=cambridge_loop[1][1],
            geometry=line(cambridge_loop),
            last_heard_minutes_ago=316.0,  # overdue: the check-in question, embodied
            props={"members": 5, "progress_pct": 61, "next_waypoint": "Collinson Peninsula", "transport": "atv", "speed_kmh": 20},
        ),
    ]


# --------------------------------------------------------------------------
# 3. Uncrewed aerial systems: 5, at the real northern airfields
# --------------------------------------------------------------------------
# Based where Canadian northern air operations actually stage from, so the basing
# survives a question about it. Endurance differs per airframe, which is what makes
# "which one can reach that contact" a real calculation rather than a nearest-neighbour
# lookup.

def _uas() -> list[Asset]:
    bases = [
        ("uas-inuvik", "Daymark 01", "Inuvik", 310, "on_station"),
        ("uas-yellowknife", "Daymark 02", "Yellowknife", 260, "ready"),
        ("uas-iqaluit", "Daymark 03", "Iqaluit", 295, "ready"),
        ("uas-rankin", "Daymark 04", "Rankin Inlet", 180, "maintenance"),
        ("uas-resolute", "Daymark 05", "Resolute Bay", 340, "on_station"),
    ]
    out = []
    for uid, name, base, endurance, state in bases:
        lat, lon = SETTLEMENTS[base]
        out.append(
            Asset(
                id=uid,
                kind="uas",
                name=name,
                lat=lat,
                lon=lon,
                alt_m=0.0 if state != "on_station" else 3200.0,
                status="maintenance" if state == "maintenance" else "nominal",
                last_heard_minutes_ago=2.0 if state == "on_station" else 26.0,
                props={
                    "base": base,
                    "state": state,
                    "endurance_min_remaining": endurance,
                    "cruise_kmh": 140,
                    "payload": "eo_ir",
                },
            )
        )
    return out


# --------------------------------------------------------------------------
# 4. Hydrophones: 10, in the narrows only
# --------------------------------------------------------------------------
# An ice-penetrating sensor is expensive and awkward to place, so it goes where the
# water is narrow and a transit has no alternative. Putting these in open water would
# be the kind of design error someone who knows the region would spot immediately.

# 🔴 THEY ARE ONE ARRAY, NOT TEN LONE SENSORS, and that is the correction that matters.
#
# These used to sit one per chokepoint across the whole region, which read well on a map
# and was wrong twice over. Seven of the ten were 244 to 546 km from anything, so they
# were on no mesh and could not deliver what they heard. And a single hydrophone does not
# tell you much: it gives you a detection, not a direction, not a speed, and not a track.
#
# An acoustic barrier is a LINE of them across the water a transit cannot avoid. Spacing
# is set so neighbours overlap acoustically and so their surface buoys can talk to each
# other, which turns ten detections into one picture of something moving through.
#
# 🔑 AND THE LINE CARRIES ITS OWN WAY OUT. One unit in the middle has a satellite terminal;
# the rest relay to it along the line. That is why the array survives when the surface
# sensors nearby do not, and it is the same architecture the node clusters use.
#
# Lancaster Sound is the eastern gate of the Northwest Passage. Everything arriving from
# the Atlantic side comes through here, so it is where an array earns its cost first. The
# honest limit, and the README says it: this is ONE barrier. A real deployment replicates
# it per chokepoint, and building one well is how you find out what the second one costs.
_ARRAY_LAT = 74.30
_ARRAY_LON_0 = -87.50
_ARRAY_SPACING_DEG = 0.66  # about 19.9 km at this latitude, inside the 25 km ground range
_ARRAY_BACKHAUL_INDEX = 5  # the middle unit carries the terminal, so no member is far from it

_NARROWS: list[tuple[str, str, float, float, int]] = [
    (
        f"lancaster-{i:02d}",
        f"Lancaster Sound {i:02d}",
        _ARRAY_LAT,
        round(_ARRAY_LON_0 + _ARRAY_SPACING_DEG * (i - 1), 4),
        180 + (i * 23) % 140,
    )
    for i in range(1, 11)
]


def _hydrophones() -> list[Asset]:
    out = []
    for i, (key, name, lat, lon, depth) in enumerate(_NARROWS, start=1):
        # The two units that are currently holding a contact. They are the reason the
        # other eight are in the water.
        detected = i in _DETECTING_UNITS
        props = {
            "array": "Lancaster Sound barrier",
            "position_in_line": i,
            "depth_m": depth,
            # 🔑 THE SENSOR RADIUS, WHICH IS NOT THE RADIO RANGE. About 18 km of useful
            # underwater detection against a quiet vessel here. Entirely separate from how
            # this unit reaches the mesh, which it does over RF through its surface buoy
            # like any other ground asset. Neighbours are 19.9 km apart, so the acoustic
            # footprints very nearly touch and a transit crosses the line rather than
            # slipping between two of them.
            "detection_radius_km": 18,
            "last_detection_minutes_ago": 22 if detected else None,
            "battery_pct": 40 + (i * 13) % 55,
        }
        if i == _ARRAY_BACKHAUL_INDEX:
            props["backhaul"] = "satellite"
        out.append(
            Asset(
                id=f"hyd-{key}",
                kind="hydrophone",
                name=f"Hydrophone {name}",
                lat=lat,
                lon=lon,
                alt_m=float(-depth),
                # One unit is unserviceable. `maintenance` rather than a vaguer word,
                # because the only thing an operator can do about it is send someone.
                status="maintenance" if i == 6 else "nominal",
                last_heard_minutes_ago=float(4 + (i * 9) % 50),
                props=props,
            )
        )
    return out


# --------------------------------------------------------------------------
# 5. Vessel contacts: 8, of which TWO are not broadcasting
# --------------------------------------------------------------------------
# 🥇 `ais_reporting = false` is the most important value in this whole file. A vessel
# with no AIS, held only by an acoustic sensor under the ice, is the exact case the
# system is built for, and it is what makes "show me what is not broadcasting" a real
# question with a real answer.
#
# Two is the deliberate number. One anomaly reads as a fluke; five reads as a game.
#
# Routes follow the two genuine Northwest Passage transits: the deep-draft northern
# route through Barrow Strait and Viscount Melville Sound, and the shallower southern
# route down Peel Sound and through Victoria Strait.

# ⚠️ THIS ROUTE USED TO SAIL THROUGH BANKS ISLAND. Two waypoints, (73.20, -115.00) and
# (72.40, -118.00), were on dry land, and nothing noticed until the placement checker was
# pointed at the seed. A vessel track that crosses an island is the single most obvious
# error anyone who knows the Arctic could spot, and it was invisible on screen at the
# zoom the demo opens at.
#
# The corrected line is the real deep-draft route: west along Lancaster Sound and
# Viscount Melville Sound, then out through M'Clure Strait NORTH of Banks Island into the
# Beaufort Sea. The southern route below is the shallow alternative through Peel Sound
# and Victoria Strait, which is what most transits actually use.
_NORTHERN_ROUTE = [
    (74.05, -80.50), (74.15, -85.00), (74.25, -90.00), (74.32, -95.00),
    (74.30, -103.00), (74.10, -110.00), (74.20, -115.00), (74.60, -121.00),
    (73.20, -125.00), (71.80, -127.00),
]
_SOUTHERN_ROUTE = [
    (74.10, -83.00), (74.20, -90.50), (73.60, -96.20), (72.40, -96.40),
    (71.20, -97.20), (70.20, -99.20), (69.30, -102.00), (68.90, -108.00),
    (69.10, -114.00), (70.10, -122.00),
]


# 🔴 THE DARK CONTACTS ARE PLACED BY THEIR DETECTOR, NOT BY A ROUTE INDEX.
#
# This is the correction that mattered most in the whole seed. UNKNOWN 01 was credited to
# a hydrophone 355.7 km away and UNKNOWN 02 to one 193.8 km away, against a detection
# radius of 18 km. Neither contact could have been heard by the sensor the data said heard
# it, in the one feature this console is built around. Anyone who checked the numbers would
# have found it, and checking the numbers is what the audience for this does for a living.
#
# So each is positioned relative to the array unit holding it, which makes the provenance
# true BY CONSTRUCTION rather than by a coincidence a later edit can quietly break. Move
# the array and the contacts move with it.
_DARK_CONTACTS: list[tuple[str, str, int, float, float]] = [
    # id, name, array unit holding it (1-based), km south of the line, speed in knots
    ("vsl-unk-01", "UNKNOWN 01", 3, 12.0, 16.5),
    ("vsl-unk-02", "UNKNOWN 02", 8, 11.0, 15.0),
]
_DETECTING_UNITS: frozenset[int] = frozenset(u for _, _, u, _, _ in _DARK_CONTACTS)

_KM_PER_DEG_LAT = 111.19


def _dark_contacts() -> list[Asset]:
    """The two vessels running without AIS, each sitting inside its detector's radius.

    The observed track is short on purpose: a contact with no transponder is known only
    from the moment a sensor first heard it, so there is no history before that. A broadcast
    vessel gets its whole route because it has been announcing itself the entire way.
    """
    out = []
    for vid, name, unit, south_km, speed in _DARK_CONTACTS:
        _, _, u_lat, u_lon, _ = _NARROWS[unit - 1]
        lat = u_lat - south_km / _KM_PER_DEG_LAT
        km_per_deg_lon = _KM_PER_DEG_LAT * math.cos(math.radians(lat))
        # Approaching from the Atlantic side, so the observed leg runs east to west.
        track = [
            (round(lat - 0.04, 4), round(u_lon + 40.0 / km_per_deg_lon, 4)),
            (round(lat - 0.02, 4), round(u_lon + 20.0 / km_per_deg_lon, 4)),
            (round(lat, 4), u_lon),
        ]
        out.append(
            Asset(
                id=vid,
                kind="vessel",
                name=name,
                lat=round(lat, 4),
                lon=u_lon,
                status="nominal",
                geometry=line(track),
                ais_reporting=False,
                last_heard_minutes_ago=22.0,
                props={
                    "classification": "unknown",
                    "flag": None,
                    "speed_kn": speed,
                    "heading_deg": 270,
                    "held_by": f"hyd-{_NARROWS[unit - 1][0]}",
                    "track_source": "acoustic",
                    # Running dark through a chokepoint is the behaviour that earns this
                    # label. It is a property of an ordinary vessel rather than a kind of
                    # its own, so nothing in terrain, motion, detection or the mesh has to
                    # learn a new word to handle it.
                    "hostile": True,
                },
            )
        )

    # 🔴 THE THIRD ONE IS THE INTERESTING ONE, AND IT IS IN THE DARK CLUSTER.
    #
    # It sits 10 km south of `node-barrow-04`, well inside that node's 15 km camera. So a
    # working sensor is holding it right now, and Barrow Strait's uplink has been down for
    # two days, which means nothing that sensor sees is reaching this display.
    #
    # That is the state worth putting in the seed, because it is the one a console can most
    # easily lie about. An empty patch of ocean and a patch of ocean nobody can hear from
    # look identical unless the display insists on telling them apart, and `detect.py`
    # reports it as `detected_not_reported` rather than folding it into either.
    b_lat, b_lon = _NODE_CLUSTERS[0][2][3]
    out.append(
        Asset(
            id="vsl-unk-03",
            kind="vessel",
            name="UNKNOWN 03",
            lat=round(b_lat - 10.0 / _KM_PER_DEG_LAT, 4),
            lon=b_lon,
            status="nominal",
            geometry=line(
                [
                    (round(b_lat - 0.16, 4), round(b_lon + 0.9, 4)),
                    (round(b_lat - 0.12, 4), round(b_lon + 0.45, 4)),
                    (round(b_lat - 10.0 / _KM_PER_DEG_LAT, 4), b_lon),
                ]
            ),
            ais_reporting=False,
            last_heard_minutes_ago=14.0,
            props={
                "classification": "unknown",
                "flag": None,
                "speed_kn": 11.0,
                "heading_deg": 250,
                "emitting": False,
                "track_source": "electro-optical",
                "hostile": True,
            },
        )
    )
    return out


def _vessels() -> list[Asset]:
    specs = [
        # id, name, classification, flag, ais, route, position index, speed
        ("vsl-nordic-star", "Nordic Star", "cargo", "Panama", True, _NORTHERN_ROUTE, 3, 12.5),
        ("vsl-polar-quest", "Polar Quest", "cruise", "Bahamas", True, _SOUTHERN_ROUTE, 2, 14.0),
        ("vsl-amundsen", "CCGS Amundsen", "research", "Canada", True, _SOUTHERN_ROUTE, 5, 11.0),
        ("vsl-kiviuq", "Kiviuq", "research", "Canada", True, _NORTHERN_ROUTE, 6, 9.5),
        ("vsl-sea-hawk", "Sea Hawk", "fishing", "Greenland", True, _SOUTHERN_ROUTE, 7, 8.0),
        ("vsl-tundra-maru", "Tundra Maru", "cargo", "Liberia", True, _NORTHERN_ROUTE, 8, 13.0),
    ]
    out = _dark_contacts()
    for vid, name, classification, flag, ais, route, idx, speed in specs:
        lat, lon = route[idx]
        # A non-broadcasting contact is held by a sensor, not by its own report, so
        # its track is only what was observed: the route up to where it is now.
        track = route[: idx + 1] if not ais else route
        out.append(
            Asset(
                id=vid,
                kind="vessel",
                name=name,
                lat=lat,
                lon=lon,
                # 🔑 NOT BROADCASTING IS NOT A CONDITION OF THE VESSEL. It is already its
                # own field, its own filter and its own colour on the map. Encoding it a
                # second time as a status made one fact answer to two names, and the two
                # could disagree.
                status="nominal",
                geometry=line(track),
                ais_reporting=ais,
                last_heard_minutes_ago=3.0 if ais else 22.0,
                props={
                    "classification": classification,
                    "flag": flag,
                    "speed_kn": speed,
                    "heading_deg": 270 if route is _NORTHERN_ROUTE else 250,
                    "route": "northern" if route is _NORTHERN_ROUTE else "southern",
                    # Provenance matters: a contact nobody can name is more
                    # interesting when you know which sensor is holding it.
                    "first_detected_by": "hyd-barrow" if not ais and route is _NORTHERN_ROUTE
                    else ("hyd-victoria" if not ais else "ais"),
                },
            )
        )
    return out


# --------------------------------------------------------------------------
# 6. Early-warning radar: 12 sites of the existing North Warning System
# --------------------------------------------------------------------------
# 🔑 THESE ARE NOT OWNED ASSETS. They are existing government infrastructure that a
# deployable sensor layer has to work alongside, so they carry `owned: False` and a
# separate operator. Modelling them earns its place by making the gap visible: the
# North Warning System is a line roughly 4,800 km long and 320 km wide, and a line
# is a tripwire. The mesh, the hydrophones and the patrols above are area coverage of
# the places a transit cannot avoid. Put both on one map and the difference argues
# itself without a caption.
#
# ⚠️ HONEST LIMITS OF THIS DATA, because a reader who knows the system will spot them
# faster than they will spot a claim that it is exact:
#
#   * The SITE NAMES and their DEW Line designators are public and well documented.
#     The COORDINATES here are approximate placements, good enough to put each site on
#     the right stretch of coast and not good enough to navigate by.
#   * Canada operates 47 of these: 11 long-range and 36 short-range gap fillers. Only
#     12 named sites are modelled. The remaining short-range sites are OMITTED RATHER
#     THAN INVENTED, because a plausible-looking coordinate for a site I cannot place
#     is worse than an absent one.
#   * The long-range versus short-range split below is inferred from the historic "-M"
#     main-station designator rather than from a published per-site list. It is
#     flagged in props as inferred.
#
# Nominal ranges are the published figures for the two radar families: about 470 km
# for the long-range AN/FPS-117 and about 110 km for the short-range AN/FPS-124.

_NWS_SITES: list[tuple[str, str, float, float, bool]] = [
    # designator, place, lat, lon, is_main ("-M" historically meant a main station)
    # ⚠️ Nudged ~6 km south of the published position (69.58, -140.18). The Yukon north
    # coast is sampled coarsely enough in this basemap that the real site falls inside a
    # simplified bay and tests as 32 km offshore. Moving the marker rather than widening
    # the tolerance keeps the constraint check strict; `position_accuracy: approximate`
    # below already says these are placements rather than surveyed coordinates.
    ("BAR-1", "Komakuk Beach", 69.55, -140.18, False),
    ("BAR-3", "Shingle Point", 68.95, -137.22, False),
    ("PIN-M", "Cape Parry", 70.17, -124.72, True),
    ("PIN-3", "Tuktoyaktuk", 69.44, -133.03, False),
    ("CAM-M", "Cambridge Bay", 69.11, -105.14, True),
    ("CAM-3", "Gladman Point", 68.67, -97.80, False),
    ("CAM-4", "Jenny Lind Island", 68.65, -101.73, False),
    ("FOX-M", "Hall Beach", 68.78, -81.24, True),
    ("FOX-3", "Longstaff Bluff", 68.90, -75.13, False),
    ("FOX-5", "Dewar Lakes", 68.65, -71.17, False),
    ("DYE-M", "Cape Dyer", 66.58, -61.62, True),
    ("BAF-3", "Brevoort Island", 63.33, -64.13, False),
]


def _radars() -> list[Asset]:
    out = []
    for designator, place, lat, lon, is_main in _NWS_SITES:
        out.append(
            Asset(
                id=f"radar-{designator.lower()}",
                kind="radar",
                name=f"{designator} {place}",
                lat=lat,
                lon=lon,
                alt_m=None,
                status="nominal",
                # No last_heard: this layer does not report into the mesh, which is
                # the interoperability problem in one field. Leaving it null rather
                # than inventing a heartbeat keeps "what has gone quiet" honest,
                # since a radar site cannot be overdue to a network it is not on.
                last_heard_minutes_ago=None,
                props={
                    "designator": designator,
                    "place": place,
                    "operator": "NORAD",
                    "owned": False,
                    "radar_type": "AN/FPS-117" if is_main else "AN/FPS-124",
                    "range_km": 470 if is_main else 110,
                    "coverage_class": "long_range" if is_main else "short_range",
                    "class_inferred_from": "historic -M main-station designator",
                    "position_accuracy": "approximate",
                },
            )
        )
    return out


# --------------------------------------------------------------------------
# 7. Forward launch sites: 6, where northern air operations actually stage from
# --------------------------------------------------------------------------
# A drone is not an independent asset: it is fuel, a crew and a place to land. Modelling
# the site is what makes "which drone can reach that contact" answerable, because the
# answer depends on where it has to come back to.
#
# 🔑 FOUR OF THESE ARE THE REAL NORAD FORWARD OPERATING LOCATIONS: Inuvik, Yellowknife,
# Rankin Inlet and Iqaluit. That is where northern air operations genuinely stage from,
# so the basing survives a question from someone who knows the file. Resolute Bay and
# Alert are added as the high-latitude staging points, which is also what they are used
# for in reality.
#
# ⚠️ "Forward launch site" is deliberately generic doctrine vocabulary. A UAS detachment
# splits into a launch and recovery element and a mission control element; this models
# the first. No vendor's product name appears here or anywhere else in this repo.
#
# 🔑 EVERY LAUNCH SITE HAS BACKHAUL, so every one of them is a way out of the theatre.
# There is no per-site flag any more: a field that is true for every row of its kind
# carries no information, and this one used to leak into `status`, where a site with no
# backhaul was written up as a condition of the site rather than a fact about the network.
# See api/_lib/mesh.py, which decides what a gateway is.
#
# What still makes Alert the interesting row is that nothing is based there and its fuel
# is limited, both of which are in `props` where they belong.

_LAUNCH_SITES: list[tuple[str, str, str, int, int]] = [
    # id suffix, display name, settlement, runway m, drones based
    ("inuvik", "FLS Inuvik", "Inuvik", 1830, 1),
    ("yellowknife", "FLS Yellowknife", "Yellowknife", 2286, 1),
    ("rankin", "FLS Rankin Inlet", "Rankin Inlet", 1798, 1),
    ("iqaluit", "FLS Iqaluit", "Iqaluit", 2743, 1),
    ("resolute", "FLS Resolute Bay", "Resolute Bay", 1981, 1),
    ("alert", "FLS Alert", "Alert", 1646, 0),
]


def _launch_sites() -> list[Asset]:
    out = []
    for key, name, settlement, runway, based in _LAUNCH_SITES:
        lat, lon = SETTLEMENTS[settlement]
        out.append(
            Asset(
                id=f"fls-{key}",
                kind="launch_site",
                name=name,
                lat=lat,
                lon=lon,
                alt_m=0.0,
                status="nominal",
                last_heard_minutes_ago=float(3 + len(key) % 11),
                props={
                    "settlement": settlement,
                    "runway_m": runway,
                    # Every launch site is a way out of the theatre. Carried as a property
                    # rather than inferred from the kind, so one field answers "is this a
                    # gateway" for a launch site, a sensor node and a hydrophone alike.
                    "backhaul": "satellite",
                    "uas_capacity": 2,
                    "uas_based": based,
                    "fuel_state": "full" if based else "limited",
                    "position_accuracy": "settlement centroid",
                },
            )
        )
    return out


# --------------------------------------------------------------------------
# 8. Air and ground contacts
# --------------------------------------------------------------------------
# 🔴 THE THREE DOMAINS EXIST SO THE SENSOR MIX HAS SOMETHING TO PROVE. A shoreline carrying
# three kinds of sensor is only worth explaining if the three disagree, and they only get to
# disagree if there is something in the air, something on the ground and something on the
# water. See api/_lib/detect.py, which decides who is holding what.
#
# `props.emitting` is the field that matters here, and it is the air and land twin of
# `ais_reporting`. Something transmitting is visible to RF from 45 km. Something that has
# switched everything off is invisible to the longest-ranged sensor on the map and can only
# be caught by looking at it or by passing within a few kilometres of it.
#
# ⚠️ AIRCRAFT ARE NOT ON THE MESH and neither are ground contacts, for the same reason
# vessels are not: they are what the network is FOR, not participants in it.

# ⚠️ TRANSIT OR ORBIT, AND THE UNKNOWN ONE ORBITS FOR A REASON. A jet at 340 knots crosses
# a 15 km camera in about ninety seconds, so an air contact modelled as a fast transit is
# detected and gone before anyone looks at the screen. That is true of real jets and it makes
# for a display that cannot demonstrate its own air picture.
#
# A slow aircraft holding station over a chokepoint is both the more realistic adversary and
# the one worth drawing: loitering is what you do when you are watching something, and a
# transit is what you do when you are going somewhere. So the unidentified contact orbits at
# 70 knots and stays inside the sensor that holds it.
_AIRCRAFT: list[tuple[str, str, str, bool, float, float, int, int, str]] = [
    # id, name, classification, emitting, lat, lon, alt m, speed kn, path
    ("air-arctic-214", "Arctic Air 214", "cargo", True, 74.20, -93.40, 9100, 310, "transit"),
    ("air-medevac-07", "Medevac 07", "medical", True, 66.10, -66.80, 7600, 280, "transit"),
    ("air-survey-03", "Survey 03", "research", True, 72.80, -117.90, 4200, 190, "transit"),
    # The one that is not talking, holding station over Victoria Strait. Silent, so the
    # 45 km RF sensor cannot see it at all; the 15 km camera can, and does.
    ("air-unk-01", "UNKNOWN AIR 01", "unknown", False, 69.9200, -100.9668, 3200, 70, "orbit"),
]

_GROUND_PARTIES: list[tuple[str, str, str, bool, float, float, int]] = [
    # id, name, classification, emitting, lat, lon, party size
    ("gnd-survey-alpha", "Survey Team Alpha", "survey", True, 78.5500, -75.5000, 6),
    # Held by a Nares node's camera despite running silent.
    ("gnd-unk-01", "UNKNOWN PARTY 01", "unknown", False, 78.6500, -75.2000, 4),
    # 🔑 AND THIS ONE IS HELD BY NOTHING AT ALL, which is the honest third answer. It sits
    # 278 km from the nearest sensor of any kind. Not a gap in the data: a gap in the
    # coverage, and the display should say so rather than imply the ground is empty.
    ("gnd-unk-02", "UNKNOWN PARTY 02", "unknown", False, 71.2000, -110.5000, 3),
]


def _aircraft() -> list[Asset]:
    out = []
    for aid, name, classification, emitting, lat, lon, alt, speed, path in _AIRCRAFT:
        if path == "orbit":
            # A closed racetrack about 8 km across, so it stays well inside a 15 km camera
            # and reads as holding station rather than going anywhere.
            dlat = 4.0 / _KM_PER_DEG_LAT
            dlon = dlat / math.cos(math.radians(lat))
            track = [
                (round(lat + dlat, 4), round(lon - dlon, 4)),
                (round(lat + dlat, 4), round(lon + dlon, 4)),
                (round(lat - dlat, 4), round(lon + dlon, 4)),
                (round(lat - dlat, 4), round(lon - dlon, 4)),
                (round(lat + dlat, 4), round(lon - dlon, 4)),
            ]
        else:
            # A long straight leg through the seeded position. Long enough that an airliner
            # does not run off the end and wrap back to the start inside one demo, which
            # looks like a teleport and is the artefact of modelling a route as a loop.
            track = [
                (round(lat - 5.0, 4), round(lon - 18.0, 4)),
                (lat, lon),
                (round(lat + 5.0, 4), round(lon + 18.0, 4)),
            ]
        out.append(
            Asset(
                id=aid,
                kind="aircraft",
                name=name,
                lat=lat,
                lon=lon,
                alt_m=float(alt),
                geometry=line(track),
                status="nominal",
                last_heard_minutes_ago=4.0 if emitting else 31.0,
                props={
                    "classification": classification,
                    "emitting": emitting,
                    "transponder": emitting,
                    "hostile": classification == "unknown",
                    "speed_kn": speed,
                    "altitude_m": alt,
                },
            )
        )
    return out


def _ground_parties() -> list[Asset]:
    out = []
    for gid, name, classification, emitting, lat, lon, size in _GROUND_PARTIES:
        out.append(
            Asset(
                id=gid,
                kind="ground_party",
                name=name,
                lat=lat,
                lon=lon,
                alt_m=0.0,
                status="nominal",
                last_heard_minutes_ago=11.0 if emitting else 96.0,
                props={
                    "classification": classification,
                    "emitting": emitting,
                    "hostile": classification == "unknown",
                    "party_size": size,
                    "transport": "on foot",
                },
            )
        )
    return out


def seed_assets() -> list[Asset]:
    """The whole seeded world. Counts are asserted against KIND_COUNTS by the seed script."""
    return [
        *_nodes(),
        *_patrols(),
        *_uas(),
        *_launch_sites(),
        *_hydrophones(),
        *_vessels(),
        *_radars(),
        *_aircraft(),
        *_ground_parties(),
    ]

def seed_rows(now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    return [a.row(now) for a in seed_assets()]


KIND_COUNTS = {
    "node": 24,
    "patrol": 3,
    "uas": 5,
    "launch_site": 6,
    "hydrophone": 10,
    "vessel": 9,
    "radar": 12,
    "aircraft": 4,
    "ground_party": 3,
}
