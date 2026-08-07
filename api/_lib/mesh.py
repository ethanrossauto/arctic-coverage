"""The mesh link graph: who can talk to whom, computed rather than declared.

WHY THIS IS COMPUTED AND NOT A COLUMN. The seed used to carry `props.mesh_peers`, an
integer someone typed. It was decorative: it could not disagree with reality because it
was never checked against anything, and it could not answer the question an operator
actually has, which is not "how many neighbours does this node have" but "what is about
to be cut off from the network."

A derived graph can answer that, and it can be wrong in a way you can see, which is the
property that makes it worth having.

THE MODEL, AND ITS ONE HONEST SIMPLIFICATION. A link exists when two mesh-capable assets
are close enough for the radio to close, and the binding constraint at these ranges is
almost always the RADIO HORIZON rather than transmit power:

    d_horizon_km  =  4.12 x ( sqrt(h1_m) + sqrt(h2_m) )

That is the geometric horizon `3.57*sqrt(h)` scaled by `sqrt(4/3)`, the standard
effective-earth-radius correction for atmospheric refraction bending signals slightly
around the curve. Then the link also has to close on power, so the usable range is
`min(horizon, the lower of the two radios' rated range)`.

⚠️ WHAT THIS DELIBERATELY DOES NOT MODEL: terrain masking, Fresnel-zone clearance, fade
margin, antenna patterns, interference, and the auroral absorption that genuinely does
degrade high-latitude HF. Every one of those makes real links WORSE than this predicts,
so the graph here is an optimistic upper bound and the README says so. Adding any of
them would need terrain data this project does not carry, and would trade a defensible
approximation for an undefensible one.

🔴 THE NUMBERS ARE THE INTERESTING PART, and they are what set the seed layout:

    node  <-> node   (12 m masts)      28.5 km
    node  <-> patrol (12 m, 2 m)       20.1 km
    node  <-> UAS    (12 m, 3200 m)   247.3 km
    UAS   <-> UAS    (3200 m both)    466.1 km

A ground mesh in the Arctic does not reach across the Arctic: chokepoints hundreds of
kilometres apart cannot see each other at all. Put an aircraft at altitude and its
horizon is an order of magnitude larger. **The aircraft is not a sensor in this picture,
it is the backbone**, and node spacing on the ground is set by that 28.5 km rather than
chosen for looks.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

# Mean earth radius, km. WGS84 would be spurious precision against a model that already
# ignores terrain.
EARTH_R_KM = 6371.0088

# sqrt(2 * k * R) with k = 4/3, expressed for h in METRES and d in KILOMETRES.
# Kept as a computed constant rather than the literal 4.12 so the derivation is visible
# and a change to k does not require re-deriving a magic number by hand.
HORIZON_K = math.sqrt(2.0 * (4.0 / 3.0) * EARTH_R_KM / 1000.0)


@dataclass(frozen=True)
class RadioProfile:
    """What a kind's radio and mounting look like.

    `height_m` is the antenna height above the surface it sits on, which is what the
    horizon actually depends on. For an aircraft it is overridden per-asset by its real
    altitude, because that is the entire point of putting one up.

    `max_range_km` is the rated link distance, and it is the constraint that binds when
    two high things can see each other from far beyond what their power supports.
    """

    height_m: float
    max_range_km: float
    role: str  # "relay" carries traffic for others; "leaf" only originates its own


# ⚠️ KINDS ABSENT FROM THIS TABLE ARE NOT ON THE MESH, and that is a design position
# rather than an oversight:
#
#   vessel:  a contact, not a participant. It is what the network is FOR. A vessel
#            broadcasting AIS is being received, which is not the same as being a peer.
#   radar:   existing government infrastructure the deployable layer works alongside.
#            It reports to its own operator, not into this mesh, which is exactly the
#            interoperability gap the display exists to make visible. Giving it an edge
#            here would erase the one thing it is on the map to show.
RADIO: dict[str, RadioProfile] = {
    # Deployable sensor node on a short guyed mast. 12 m is what one person can put up.
    "node": RadioProfile(height_m=12.0, max_range_km=40.0, role="relay"),
    # Man-portable set carried by a patrol. Antenna height is roughly a standing person.
    "patrol": RadioProfile(height_m=2.0, max_range_km=25.0, role="leaf"),
    # Overridden per-asset by altitude when airborne; this is the on-the-ground case.
    "uas": RadioProfile(height_m=2.0, max_range_km=300.0, role="relay"),
    # Fixed infrastructure, so a taller mast than a deployable node gets.
    "launch_site": RadioProfile(height_m=15.0, max_range_km=60.0, role="relay"),
    # A hydrophone's acoustic half is under the ice and reaches nothing by radio. What
    # is ON the mesh is its surface buoy, which is small, low and easily buried in snow.
    # 🔑 This is why most hydrophones come back unlinked, and that is a true finding
    # about the design rather than a bug in the graph.
    "hydrophone": RadioProfile(height_m=1.5, max_range_km=20.0, role="leaf"),
}


def radio_horizon_km(h1_m: float, h2_m: float) -> float:
    """Line-of-sight distance between two antennas, with refraction accounted for.

    Negative heights are clamped to zero: a hydrophone's `alt_m` is its depth BELOW the
    surface, and feeding that in unclamped produces a NaN from sqrt and poisons every
    comparison downstream silently.
    """
    return HORIZON_K * (math.sqrt(max(h1_m, 0.0)) + math.sqrt(max(h2_m, 0.0)))


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance. Haversine rather than the spherical law of cosines, which
    loses precision catastrophically at the short distances that decide a mesh link."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(math.sqrt(a))


def antenna_height_m(asset: dict[str, Any]) -> float | None:
    """How high this asset's radio actually is, or None if it has no radio.

    An airborne UAS uses its real altitude, which is what makes it a backbone rather
    than another ground station. Everything else uses its kind's mast height; a node on
    a 300 m hill still only has a 12 m mast, and pretending otherwise would be modelling
    terrain, which this module explicitly does not do.
    """
    profile = RADIO.get(asset["kind"])
    if profile is None:
        return None
    if asset["kind"] == "uas":
        alt = asset.get("alt_m") or 0.0
        # On the ground, a drone's radio is at roughly person height, not at zero.
        return max(float(alt), profile.height_m)
    return profile.height_m


def link_range_km(a: dict[str, Any], b: dict[str, Any]) -> float | None:
    """The usable range for this pair, or None if either end is not on the mesh."""
    ha, hb = antenna_height_m(a), antenna_height_m(b)
    if ha is None or hb is None:
        return None
    rated = min(RADIO[a["kind"]].max_range_km, RADIO[b["kind"]].max_range_km)
    return min(radio_horizon_km(ha, hb), rated)


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
        of margin is a link that a moving patrol or a rising snowbank will take away,
        and it reads completely differently from one at 20 km even though both are
        currently up.
        """
        return self.range_km - self.distance_km


def _positioned(assets: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mesh-capable assets that have a position right now."""
    return [
        a
        for a in assets
        if a["kind"] in RADIO and a.get("lat") is not None and a.get("lon") is not None
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


def mesh_status(assets: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """The whole picture: links, connected groups, and what is on no mesh at all.

    🔑 UNLINKED ASSETS ARE REPORTED SEPARATELY RATHER THAN AS ONE-MEMBER GROUPS, and the
    distinction is the operationally useful one. A review of an earlier version of this
    expected "four components" for the four seeded clusters; the truthful answer is four
    multi-member groups plus roughly a dozen singletons, because a hydrophone buoy at
    1.5 m reaches 20 km and most narrows are further than that from the nearest node.

    Folding those into the component count would have buried the finding inside a
    number. "Nine assets are on no mesh at all" is precisely what this display exists to
    surface, so it gets its own field.
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
            }
            for members in connected
        ],
        "isolated": isolated,
        "mesh_capable": len(nodes),
        "counts": {
            "links": len(links),
            "groups": len(connected),
            "isolated": len(isolated),
        },
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
