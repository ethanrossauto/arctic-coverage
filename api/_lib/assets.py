"""The five asset kinds, and the seeded world.

The world is a Canadian Arctic surveillance picture: a deployable sensor mesh at the
chokepoints, mobile patrols and drones, under-ice acoustic sensors in the narrows,
the maritime contacts they exist to detect, and the existing early-warning radar
line they have to work alongside.

WHY SIX KINDS RATHER THAN ONE. Each has a different geometry, a different mobility
and a different set of questions worth asking about it, and that heterogeneity is the
point: it is what makes a command layer useful rather than decorative. Six kinds
that were all static points with a name would reduce every command to a search box.

    kind        geometry          mobility        the question it answers
    ---------------------------------------------------------------------------
    node        point             static          what has gone quiet, what is
                                                  about to be cut off from the mesh
    patrol      route + position  ~4 km/h         where are my people, who has
                                                  not checked in
    uas         point + path      ~100 km/h       what can I send, and how long
                                                  until it is on top of that
    hydrophone  point + radius    static          what passed through the narrows
                                                  without being seen
    vessel      track + position  10-20 kn        who is out there, and which of
                                                  them is not broadcasting
    radar       point + radius    static          what the existing line already
                                                  covers, and where it does not

🔴 GEOGRAPHY IS NOT DECORATION HERE. Every position is a real place or a real
waterway. The mesh sits on the actual Northwest Passage chokepoints, because that is
where a sensor earns its cost; the hydrophones sit only in the narrows, because a
sensor lowered through the ice anywhere else is money spent watching open water; and
the drones are based at the real northern airfields. Scattering these at random would produce
the same screenshot and would fall apart under one question from anyone who knows
the region.

⚠️ THE SEED IS IDEMPOTENT AND MEANT TO BE RE-RUN. `last_heard` values are offsets
from the moment of seeding, so the interesting states (something stale, something
silent) stay interesting rather than ageing into "everything went quiet three days
ago". Re-running the seed before a demo refreshes them, and doubles as the
reset-to-known-state that a public URL needs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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

_NODE_CLUSTERS: list[tuple[str, str, list[tuple[float, float]]]] = [
    (
        "barrow",
        "Barrow Strait / Lancaster Sound",
        [
            (74.35, -95.60), (74.28, -93.20), (74.20, -90.80), (74.15, -88.00),
            (74.10, -85.20), (74.05, -82.40), (73.95, -80.20),
        ],
    ),
    (
        "pow",
        "Prince of Wales Strait / Amundsen Gulf",
        [
            (73.10, -115.20), (72.60, -117.60), (72.10, -119.20),
            (71.40, -121.00), (70.80, -122.60), (70.30, -124.20),
        ],
    ),
    (
        "victoria",
        "Victoria Strait / Franklin Strait",
        [
            (71.60, -96.20), (70.90, -97.60), (70.20, -99.00),
            (69.60, -100.40), (69.10, -101.80),
        ],
    ),
    (
        "nares",
        "Ellesmere / Nares Strait",
        [
            (76.40, -80.20), (77.60, -77.40), (78.60, -74.20),
            (79.80, -71.00), (81.00, -66.40), (82.10, -62.80),
        ],
    ),
]

# Payloads vary by cluster so that a filter on payload returns something meaningful
# rather than the whole set.
_PAYLOADS = ["eo_ir", "acoustic", "rf", "seismic", "magnetic"]

# Staleness is designed, not random. Most nodes are current, a few are late, and two
# are silent, because "what has gone quiet" is only a real question if the answer is
# neither "nothing" nor "everything".
_NODE_STALENESS: dict[str, tuple[float, str]] = {
    "node-nares-05": (410.0, "degraded"),   # late, and on the hardest cluster to reach
    "node-victoria-03": (1670.0, "silent"), # silent for over a day
    "node-pow-06": (95.0, "degraded"),      # late, low battery below
    "node-barrow-07": (2880.0, "silent"),   # silent for two days
}


def _nodes() -> list[Asset]:
    out: list[Asset] = []
    for cluster_key, cluster_name, points in _NODE_CLUSTERS:
        for i, (lat, lon) in enumerate(points, start=1):
            node_id = f"node-{cluster_key}-{i:02d}"
            stale_minutes, status = _NODE_STALENESS.get(node_id, (float(7 + (i * 3) % 40), "nominal"))
            # Battery tracks status: a silent node is assumed flat until proven
            # otherwise, which is the assumption an operator would make.
            battery = 0 if status == "silent" else (18 if status == "degraded" else 55 + (i * 7) % 40)
            out.append(
                Asset(
                    id=node_id,
                    kind="node",
                    name=f"{cluster_name.split(' /')[0]} {i:02d}",
                    lat=lat,
                    lon=lon,
                    alt_m=float(5 + (i * 11) % 60),
                    status=status,
                    last_heard_minutes_ago=stale_minutes,
                    props={
                        "cluster": cluster_key,
                        "cluster_name": cluster_name,
                        "payload": _PAYLOADS[(i - 1) % len(_PAYLOADS)],
                        "power_source": "solar_wind" if i % 3 else "primary_battery",
                        "battery_pct": battery,
                        # Mesh degree: the ones at a cluster edge have fewer
                        # neighbours, which is what makes them worth watching.
                        "mesh_peers": 1 if i in (1, len(points)) else 2 + (i % 2),
                    },
                )
            )
    return out


# --------------------------------------------------------------------------
# 2. Ranger patrols: 3
# --------------------------------------------------------------------------
# The long-range route is the real one: a 1 Canadian Ranger Patrol Group patrol
# covered roughly 5,200 km from Inuvik to Churchill over 52 days. Two shorter
# community patrols give the map assets that move on a human timescale, which is
# what makes the time controls mean anything next to a satellite at 7 km/s.

def _patrols() -> list[Asset]:
    long_range = [
        SETTLEMENTS[n]
        for n in (
            "Inuvik", "Tuktoyaktuk", "Paulatuk", "Ulukhaktok", "Cambridge Bay",
            "Gjoa Haven", "Baker Lake", "Arviat", "Churchill",
        )
    ]
    resolute_loop = [
        SETTLEMENTS["Resolute Bay"], (75.20, -95.50), (75.55, -94.00),
        (75.05, -92.80), SETTLEMENTS["Resolute Bay"],
    ]
    cambridge_loop = [
        SETTLEMENTS["Cambridge Bay"], (69.60, -106.50), (70.10, -105.00),
        (69.50, -103.50), SETTLEMENTS["Cambridge Bay"],
    ]
    return [
        Asset(
            id="patrol-1crpg-lr",
            kind="patrol",
            name="1 CRPG Long Range",
            lat=SETTLEMENTS["Cambridge Bay"][0],
            lon=SETTLEMENTS["Cambridge Bay"][1],
            geometry=line(long_range),
            last_heard_minutes_ago=52.0,
            props={
                "members": 14,
                "progress_pct": 48,
                "next_waypoint": "Gjoa Haven",
                "transport": "snowmobile",
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
            lat=69.60,
            lon=-106.50,
            geometry=line(cambridge_loop),
            status="degraded",
            last_heard_minutes_ago=316.0,  # overdue: the check-in question, embodied
            props={"members": 5, "progress_pct": 61, "next_waypoint": "Byron Bay", "transport": "snowmobile", "speed_kmh": 20},
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
        ("uas-inuvik", "Ptarmigan 01", "Inuvik", 310, "on_station"),
        ("uas-yellowknife", "Ptarmigan 02", "Yellowknife", 260, "ready"),
        ("uas-iqaluit", "Ptarmigan 03", "Iqaluit", 295, "ready"),
        ("uas-rankin", "Ptarmigan 04", "Rankin Inlet", 180, "maintenance"),
        ("uas-resolute", "Ptarmigan 05", "Resolute Bay", 340, "on_station"),
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
                status="nominal" if state != "maintenance" else "degraded",
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

_NARROWS: list[tuple[str, str, float, float, int]] = [
    ("barrow", "Barrow Strait", 74.30, -94.50, 180),
    ("bellot", "Bellot Strait", 71.99, -94.50, 60),
    ("pow", "Prince of Wales Strait", 72.60, -117.60, 210),
    ("victoria", "Victoria Strait", 69.50, -100.50, 120),
    ("fury", "Fury and Hecla Strait", 69.92, -84.50, 90),
    ("dolphin", "Dolphin and Union Strait", 69.00, -114.50, 75),
    ("peel", "Peel Sound", 73.00, -96.50, 240),
    ("franklin", "Franklin Strait", 71.50, -96.00, 190),
    ("nares", "Nares Strait", 79.50, -70.50, 260),
    ("amundsen", "Amundsen Gulf", 70.50, -123.00, 300),
]


def _hydrophones() -> list[Asset]:
    out = []
    for i, (key, name, lat, lon, depth) in enumerate(_NARROWS, start=1):
        # Two have heard something recently. Those two are the reason the rest exist.
        detected = key in ("barrow", "victoria")
        out.append(
            Asset(
                id=f"hyd-{key}",
                kind="hydrophone",
                name=f"Hydrophone {name}",
                lat=lat,
                lon=lon,
                alt_m=float(-depth),
                status="nominal" if i != 6 else "degraded",
                last_heard_minutes_ago=float(4 + (i * 9) % 50),
                props={
                    "narrows": name,
                    "depth_m": depth,
                    "ice_thickness_cm": 120 + (i * 17) % 90,
                    "detection_radius_km": 18,
                    "last_detection_minutes_ago": 22 if detected else None,
                    "battery_pct": 40 + (i * 13) % 55,
                },
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

_NORTHERN_ROUTE = [
    (74.05, -80.50), (74.15, -85.00), (74.25, -90.00), (74.32, -95.00),
    (74.30, -103.00), (74.10, -110.00), (73.20, -115.00), (72.40, -118.00),
    (71.20, -121.50), (70.40, -124.50),
]
_SOUTHERN_ROUTE = [
    (74.10, -83.00), (74.20, -90.50), (73.60, -96.20), (72.40, -96.40),
    (71.30, -96.30), (70.20, -99.20), (69.30, -102.00), (68.90, -108.00),
    (69.10, -114.00), (69.80, -122.00),
]


def _vessels() -> list[Asset]:
    specs = [
        # id, name, classification, flag, ais, route, position index, speed
        ("vsl-nordic-star", "Nordic Star", "cargo", "Panama", True, _NORTHERN_ROUTE, 3, 12.5),
        ("vsl-polar-quest", "Polar Quest", "cruise", "Bahamas", True, _SOUTHERN_ROUTE, 2, 14.0),
        ("vsl-amundsen", "CCGS Amundsen", "research", "Canada", True, _SOUTHERN_ROUTE, 5, 11.0),
        ("vsl-kiviuq", "Kiviuq", "research", "Canada", True, _NORTHERN_ROUTE, 6, 9.5),
        ("vsl-sea-hawk", "Sea Hawk", "fishing", "Greenland", True, _SOUTHERN_ROUTE, 7, 8.0),
        ("vsl-tundra-maru", "Tundra Maru", "cargo", "Liberia", True, _NORTHERN_ROUTE, 8, 13.0),
        # The two that matter.
        ("vsl-unk-01", "UNKNOWN 01", "unknown", None, False, _NORTHERN_ROUTE, 4, 16.5),
        ("vsl-unk-02", "UNKNOWN 02", "unknown", None, False, _SOUTHERN_ROUTE, 4, 15.0),
    ]
    out = []
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
                status="nominal" if ais else "warning",
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
    ("BAR-1", "Komakuk Beach", 69.60, -140.18, False),
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


def seed_assets() -> list[Asset]:
    """The whole seeded world: 24 nodes, 3 patrols, 5 UAS, 10 hydrophones, 8 vessels, 12 radars."""
    return [*_nodes(), *_patrols(), *_uas(), *_hydrophones(), *_vessels(), *_radars()]


def seed_rows(now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    return [a.row(now) for a in seed_assets()]


KIND_COUNTS = {"node": 24, "patrol": 3, "uas": 5, "hydrophone": 10, "vessel": 8, "radar": 12}
