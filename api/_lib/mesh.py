"""The mesh link graph: who can talk to whom, computed rather than declared.

WHY THIS IS COMPUTED AND NOT A COLUMN. The seed used to carry `props.mesh_peers`, an
integer someone typed. It was decorative: it could not disagree with reality because it
was never checked against anything, and it could not answer the question an operator
actually has, which is not "how many neighbours does this node have" but "what is about
to be cut off from the network."

A derived graph can answer that, and it can be wrong in a way you can see, which is the
property that makes it worth having.

THE MODEL IS A TABLE LOOKUP, ON PURPOSE. Two assets are linked when the distance between
them is inside the range for their pair of states, and that is the whole rule:

    ground to ground     25 km
    ground to air        50 km
    air to air          100 km

Everything sits on the ground except a drone, and a drone is on the ground or in the air.
There is no third state.

WHY 25 KM, AND WHY A LOOKUP AT ALL. 25 km is a representative published figure for a
mast-mounted tactical mesh radio. The model is deliberately a lookup rather than a
calculation because this is a planning display, not a link planner: a horizon formula
would invite a conversation about Fresnel clearance, fade margin, antenna patterns,
terrain masking and auroral absorption, and this project carries the terrain data for
none of it. A number you can defend in one sentence beats a formula you cannot defend at
all.

⚠️ SO THE GRAPH IS AN APPROXIMATION AND SAYS SO. Real links at these ranges are worse
than a clean distance test predicts, never better. Read it as an optimistic upper bound
on connectivity rather than a prediction about any particular radio.

CONNECTED MEANS CONNECTED TO THIS DISPLAY. An asset is reachable when a path exists from
it to a gateway, and every hop along that path is one we are currently hearing from. So a
relay going quiet takes its neighbours with it, which is the honest answer: if the only
route home is through a node that stopped reporting, nothing behind it is reaching us
either.

🔑 AN UNREACHABLE ASSET IS DRAWN WHERE IT WAS LAST SEEN, AND IT STOPS MOVING. That falls
out of the same idea rather than being a second rule. A console shows what it knows, and
what it knows about a silent asset is where that asset was when it last reported. Animating
it onward would be the display inventing a position, which is the one thing an operational
picture must never do.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# Mean earth radius, km. WGS84 would be spurious precision against a model that already
# ignores terrain.
EARTH_R_KM = 6371.0088

# ⚠️ KINDS ABSENT FROM THIS SET ARE NOT ON THE MESH, and that is a design position
# rather than an oversight:
#
#   vessel:  a contact, not a participant. It is what the network is FOR. A vessel
#            broadcasting AIS is being received, which is not the same as being a peer.
#   radar:   existing government infrastructure the deployable layer works alongside.
#            It reports to its own operator, not into this mesh, which is exactly the
#            interoperability gap the display exists to make visible. Giving it an edge
#            here would erase the one thing it is on the map to show.
#   marker:  a pin someone dropped. It has no radio because it is not a thing, it is a
#            note about a place.
#
# 🔑 A HYDROPHONE IS ON THE MESH AS A GROUND ASSET. What is on the mesh is its surface
# buoy, which is an ordinary short-range radio at 25 km like everything else. The 1 to
# 3 km acoustic figure people reach for is the vertical hop from the seabed unit to its
# own buoy, and that is not a mesh hop.
#
# ⚠️ This paragraph used to end "most hydrophones come back unlinked, which is a true
# finding rather than a bug". It was true when they were scattered one per chokepoint and
# stopped being true when they became a single array spaced to reach each other. A comment
# describing a finding has to be deleted when the finding is designed out.
MESH_KINDS: frozenset[str] = frozenset(
    {"node", "patrol", "uas", "launch_site", "hydrophone"}
)

# 🔑 WHY A SENSOR CLUSTER CARRIES ITS OWN UPLINK, which is the part worth being able to
# defend. Above roughly 70 degrees north a geostationary satellite sits below the horizon,
# so the ordinary maritime service does not work at all up here and a polar-orbit
# constellation is the only option. It is low bandwidth, a few hundred bytes a message,
# which happens to suit sensor telemetry: a detection report is small.
#
# That gives the standard shape for a remote sensor network. Cheap short-range radios
# handle local peer to peer, and one asset per cluster carries the satellite terminal that
# everything else relays inward to. Building a chain whose only route home was a 90 km hop
# to an airfield would put the whole cluster behind one aircraft and one weather forecast.
#
# ⚠️ There is no GATEWAY_KINDS constant, and that is deliberate. There was one, holding
# `{"launch_site"}`, and it went stale the moment nodes and a hydrophone started carrying
# terminals: `is_gateway` had already stopped reading it, while `model()` was still
# publishing it on the wire as the definitive list. A constant that no longer decides
# anything but is still quoted as the answer is worse than no constant at all.

GROUND = "ground"
AIR = "air"

# Range in km by the pair of states involved, order-independent.
LINK_RANGE_KM: dict[frozenset[str], float] = {
    frozenset({GROUND}): 25.0,
    frozenset({GROUND, AIR}): 50.0,
    frozenset({AIR}): 100.0,
}


def model() -> dict[str, Any]:
    """How links are decided, described by the module that decides them.

    🔒 THIS EXISTS SO THE API CANNOT PUBLISH A MODEL THE CODE NO LONGER USES. The endpoint
    that serves it holds no description of its own and asks for this one instead, so the
    wire and the implementation cannot drift apart: there is only one of them.
    """
    return {
        "type": "range lookup",
        "ranges_km": {
            "ground_to_ground": LINK_RANGE_KM[frozenset({GROUND})],
            "ground_to_air": LINK_RANGE_KM[frozenset({GROUND, AIR})],
            "air_to_air": LINK_RANGE_KM[frozenset({AIR})],
        },
        "states": (
            "every asset is ground except a drone in flight, which is air. A drone on "
            "the ground has a ground asset's reach."
        ),
        "note": (
            "Links are a distance test against the table above, not a link budget. "
            "25 km is a representative published figure for a mast-mounted tactical mesh "
            "radio. Terrain masking, Fresnel clearance, fade margin and auroral "
            "absorption are not modelled, and every one of them makes a real link worse, "
            "so treat this graph as an optimistic upper bound on connectivity."
        ),
        "mesh_kinds": sorted(MESH_KINDS),
        "gateway_note": (
            "a gateway is any asset carrying a satellite terminal, whatever kind it is: "
            "every launch site, one node per sensor cluster, and the middle unit of the "
            "hydrophone array. An asset counts as reachable when a path through assets we "
            "are currently hearing from leads to one of them."
        ),
    }


def link_state(asset: dict[str, Any]) -> str:
    """Ground or air. Only a drone can be air, and only while it is actually up.

    Driven by `alt_m` rather than by `props.state` so that it stays true after a user
    moves a drone or puts one somewhere the seed never imagined. A drone on the ground is
    a ground asset with a ground asset's reach, which is the point of basing them.
    """
    if asset.get("kind") != "uas":
        return GROUND
    return AIR if float(asset.get("alt_m") or 0.0) > 0.0 else GROUND


def is_gateway(asset: dict[str, Any]) -> bool:
    """Whether traffic reaching this asset can leave the theatre.

    🔑 ONE FIELD, AND IT IS A ROLE RATHER THAN A KIND. Any asset carrying `props.backhaul`
    is a way out, whatever kind it is: a launch site, a sensor node with a terminal bolted
    to it, or the hydrophone array's middle unit. This was briefly keyed on `kind` instead,
    which was wrong in a way worth recording: it forced every reader, including the map
    drawing the badge, to know that one particular kind implies the role. Carrying a
    satellite terminal is a fact about the asset, so it is stored on the asset.

    The value says HOW rather than just yes, because "satellite" and "fibre" would behave
    very differently in a real outage and a bare `true` could never grow into that.
    """
    return bool((asset.get("props") or {}).get("backhaul"))


def is_heard(asset: dict[str, Any]) -> bool | None:
    """Are we currently receiving from this asset, or None if this row cannot say?

    🔑 READS THE ANSWER RATHER THAN RECOMPUTING IT. Whether an asset is overdue depends on
    a reporting interval that differs per kind, and that interval is defined in exactly one
    place. Deriving it a second time here would put two answers to one question in the same
    codebase, which is the failure this round exists to clean up.

    Returns None when the row carries neither field, so the caller can say it did not know
    instead of guessing. A missing answer is not the same as a healthy asset.
    """
    flag = asset.get("flag")
    if flag is not None:
        return flag != "overdue"
    overdue = asset.get("overdue")
    if overdue is not None:
        return not bool(overdue)
    return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance. Haversine rather than the spherical law of cosines, which
    loses precision catastrophically at the short distances that decide a mesh link."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(math.sqrt(a))


def link_range_km(a: dict[str, Any], b: dict[str, Any]) -> float | None:
    """The range for this pair, or None if either end is not on the mesh."""
    if a.get("kind") not in MESH_KINDS or b.get("kind") not in MESH_KINDS:
        return None
    return LINK_RANGE_KM[frozenset({link_state(a), link_state(b)})]


@dataclass(frozen=True)
class Link:
    a: str
    b: str
    distance_km: float
    range_km: float

    @property
    def margin_km(self) -> float:
        """How much further apart these two could drift before the link drops.

        🔑 This is the field that answers "what is about to be cut off". A link at 2 km
        of margin is a link that a moving patrol or a descending drone will take away,
        and it reads completely differently from one at 20 km even though both are
        currently up.
        """
        return self.range_km - self.distance_km


def _positioned(assets: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mesh-capable assets that have a position right now."""
    return [
        a
        for a in assets
        if a.get("kind") in MESH_KINDS
        and a.get("lat") is not None
        and a.get("lon") is not None
    ]


def compute_links(assets: Iterable[dict[str, Any]]) -> list[Link]:
    """Every link that is currently up.

    O(n^2) over the mesh-capable set, which is about 48 assets here, so roughly 1,100
    pairs and microseconds of work. Stated plainly rather than dressed up: above a few
    thousand nodes this wants a spatial index, and at this size an index would be slower
    than the scan it replaced.
    """
    nodes = _positioned(assets)
    links: list[Link] = []
    for i, a in enumerate(nodes):
        for b in nodes[i + 1 :]:
            reach = link_range_km(a, b)
            if reach is None:
                continue
            d = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
            if d <= reach:
                links.append(Link(a=a["id"], b=b["id"], distance_km=d, range_km=reach))
    return links


class _Union:
    """Union-find. Fifteen lines, so no graph library is worth the dependency."""

    def __init__(self, ids: Iterable[str]) -> None:
        self.parent = {i: i for i in ids}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]  # path halving
            x = self.parent[x]
        return x

    def union(self, x: str, y: str) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self.parent[rx] = ry


def _reachability(
    nodes: list[dict[str, Any]], links: list[Link]
) -> tuple[list[str], list[str], list[str]]:
    """Who can still get a message home. Returns (reachable, unreachable, unknown).

    🔴 A SECOND GRAPH, AND THE DIFFERENCE BETWEEN THE TWO IS THE POINT. The link graph is
    geometric: who is close enough to whom, regardless of whether anything is working. This
    one keeps only the assets we are currently hearing from, so a relay that has gone quiet
    stops carrying traffic for its neighbours. That is what makes one dead uplink grey out
    a whole cluster rather than only itself.

    `unknown` holds ids whose rows could not say whether they are being heard from, so a
    caller can report "nothing could be checked" instead of it looking like "nothing is
    wrong".
    """
    heard = {a["id"]: is_heard(a) for a in nodes}
    unknown = sorted(i for i, v in heard.items() if v is None)
    live_ids = {i for i, v in heard.items() if v is not False}

    live_uf = _Union(live_ids)
    for link in links:
        if link.a in live_ids and link.b in live_ids:
            live_uf.union(link.a, link.b)

    # A gateway is trivially reachable through itself, so a lone base with no neighbours
    # still reaches the outside world and must not be counted as cut off.
    gateway_roots = {
        live_uf.find(a["id"]) for a in nodes if a["id"] in live_ids and is_gateway(a)
    }
    reachable = sorted(
        a["id"]
        for a in nodes
        if a["id"] in live_ids and live_uf.find(a["id"]) in gateway_roots
    )
    reachable_set = set(reachable)
    unreachable = sorted(a["id"] for a in nodes if a["id"] not in reachable_set)
    return reachable, unreachable, unknown


def mesh_status(assets: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """The whole picture: links, connected groups, what reaches a gateway, and what is on
    no mesh at all.

    🔑 UNLINKED ASSETS ARE REPORTED SEPARATELY RATHER THAN AS ONE-MEMBER GROUPS, and the
    distinction is the operationally useful one. A review of an earlier version of this
    expected one component per seeded cluster; the truthful answer is a handful of
    multi-member groups plus a long tail of singletons, because most hydrophones sit in
    narrows further from the nearest node than any ground radio reaches.

    Folding those into the component count would have buried the finding inside a
    number. "These assets are on no mesh at all" is precisely what this display exists to
    surface, so it gets its own field.

    🔑 `isolated` AND `unreachable` ARE DIFFERENT QUESTIONS AND BOTH ARE WORTH ASKING.
    `isolated` is geometric: this asset has no neighbour close enough to talk to, which is
    a coverage problem you solve by moving something. `unreachable` means nothing we hear
    from leads back to a gateway, which is usually a failure somewhere along a chain that
    is otherwise perfectly well placed. The first wants a different site; the second wants
    someone to go and look at a specific node.
    """
    nodes = _positioned(assets)
    links = compute_links(nodes)

    uf = _Union(a["id"] for a in nodes)
    for link in links:
        uf.union(link.a, link.b)

    groups: dict[str, list[str]] = {}
    for a in nodes:
        groups.setdefault(uf.find(a["id"]), []).append(a["id"])

    connected = sorted(
        (sorted(members) for members in groups.values() if len(members) > 1),
        key=len,
        reverse=True,
    )
    isolated = sorted(m[0] for m in groups.values() if len(m) == 1)
    isolated_set = set(isolated)
    mesh_connected = sorted(a["id"] for a in nodes if a["id"] not in isolated_set)

    reachable, unreachable, unknown = _reachability(nodes, links)
    reachable_set = set(reachable)
    by_id = {a["id"]: a for a in nodes}

    return {
        "links": [
            {
                "a": link.a,
                "b": link.b,
                "distance_km": round(link.distance_km, 1),
                "range_km": round(link.range_km, 1),
                "margin_km": round(link.margin_km, 1),
            }
            for link in sorted(links, key=lambda link: link.margin_km)
        ],
        "groups": [
            {
                "size": len(members),
                "members": members,
                # Named for the largest cluster represented in it, so a component has a
                # handle an operator can say out loud rather than a hash.
                "label": _label_for(members, by_id),
                "has_gateway": any(m in reachable_set for m in members),
            }
            for members in connected
        ],
        "isolated": isolated,
        # 🔑 TWO DIFFERENT QUESTIONS, TWO FLAGS, AND AN ASSET CAN FAIL EITHER ONE ALONE.
        #
        #   mesh_connected   has at least one neighbour it can talk to. Purely geometric,
        #                    and it does not care whether anything is working.
        #   server_reachable something we are still hearing from leads all the way back to
        #                    a gateway, so what this asset knows can reach this display.
        #
        # A launch site with backhaul and no neighbours is NOT mesh connected and IS server
        # reachable. Every node in a cluster whose uplink has died is mesh connected and is
        # NOT server reachable. Collapsing them into one word loses the difference between
        # "put something closer to it" and "go and look at one specific node".
        "mesh_connected": mesh_connected,
        "server_reachable": reachable,
        "unreachable": unreachable,
        # ⚠️ SAYS WHAT IT KNEW, not just what it concluded. Reachability depends on which
        # assets we are hearing from, and a row that carries neither `flag` nor `overdue`
        # cannot answer that. Those ids are listed rather than quietly assumed healthy, so
        # "nothing is grey" can be told apart from "nothing could be checked".
        "reachability_unknown": unknown,
        "mesh_capable": len(nodes),
        "ranges_km": {
            "ground_ground": LINK_RANGE_KM[frozenset({GROUND})],
            "ground_air": LINK_RANGE_KM[frozenset({GROUND, AIR})],
            "air_air": LINK_RANGE_KM[frozenset({AIR})],
        },
        "counts": {
            "links": len(links),
            "groups": len(connected),
            "isolated": len(isolated),
            "mesh_connected": len(mesh_connected),
            "server_reachable": len(reachable),
            "unreachable": len(unreachable),
        },
    }


def asset_flags(assets: Iterable[dict[str, Any]]) -> dict[str, dict[str, bool]]:
    """Both connectivity flags per asset id, for a caller that renders one row at a time.

    `mesh_status` answers in lists because that is the shape of the question it is asked at
    the fleet level. A map colouring one icon wants the answer for that icon, and building
    two sets per frame to get it is work the server has already done.

    ⚠️ Assets that are not on the mesh at all, a vessel or a radar site, are absent rather
    than present-and-false. They have no radio, so neither flag is a fact about them, and
    `False` would read as "we checked and it cannot reach us".

    🔴 PASS THE WHOLE WORLD, NEVER A FILTERED SUBSET. Connectivity is a property of the
    entire graph, so calling this with only the nodes computes reachability against a world
    where every relay, launch site and drone has been deleted. The answer comes back
    confidently wrong rather than obviously missing, which is the worse of the two failures.
    Filter the result, not the input.
    """
    status = mesh_status(assets)
    connected = set(status["mesh_connected"])
    reachable = set(status["server_reachable"])
    return {
        aid: {"mesh_connected": aid in connected, "server_reachable": aid in reachable}
        for aid in (a["id"] for a in _positioned(assets))
    }


def _label_for(members: list[str], by_id: dict[str, dict[str, Any]]) -> str:
    """A human handle for a connected group.

    Uses the cluster name most of its members share, because "Barrow Strait group" is
    something an operator can repeat back and "group 3" is not.
    """
    names: dict[str, int] = {}
    for mid in members:
        asset = by_id.get(mid, {})
        cluster = (asset.get("props") or {}).get("cluster_name")
        if cluster:
            names[cluster] = names.get(cluster, 0) + 1
    if not names:
        return f"{len(members)} assets"
    best = max(names.items(), key=lambda kv: kv[1])[0]
    return str(best).split(" /")[0]
