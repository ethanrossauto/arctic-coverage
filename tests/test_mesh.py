"""The link model, and the two different questions it answers about connectivity.

These are written against the seeded world rather than against hand-made fixtures wherever
the seed already contains the case, because the seed is what ships and a test that passes on
invented geometry says nothing about it. Where a case needs an asset somewhere the seed does
not put one, the fixture says so and builds only that.
"""
from __future__ import annotations

import copy
import math

import pytest

from api._lib import mesh
from api._lib.assets import seed_rows


@pytest.fixture
def world() -> list[dict]:
    """The seeded world with everything currently being heard from.

    ⚠️ `flag` matters. Reachability is computed over the assets we can hear, so a fixture
    that omits it is testing the "we could not tell" path rather than the healthy one.
    """
    rows = seed_rows()
    for row in rows:
        row["flag"] = "nominal"
    return rows


def _asset(kind: str, aid: str, lat: float, lon: float, alt: float = 0.0, **props) -> dict:
    return {
        "id": aid,
        "kind": kind,
        "name": aid,
        "lat": lat,
        "lon": lon,
        "alt_m": alt,
        "props": props,
        "flag": "nominal",
    }


# --------------------------------------------------------------------------
# The link model itself
# --------------------------------------------------------------------------

def test_the_three_ranges_are_the_whole_model():
    """25 ground to ground, 50 ground to air, 100 air to air. Nothing else decides a link."""
    ground = _asset("node", "g", 74.0, -95.0)
    air = _asset("uas", "a", 74.0, -95.0, alt=3200.0)
    assert mesh.link_range_km(ground, ground) == 25.0
    assert mesh.link_range_km(ground, air) == 50.0
    assert mesh.link_range_km(air, ground) == 50.0  # order must not matter
    assert mesh.link_range_km(air, air) == 100.0


def _north_of(lat: float, lon: float, km: float) -> tuple[float, float]:
    """A point exactly `km` due north, using the module's own earth radius.

    ⚠️ NOT AN APPROXIMATE 111.19 KM PER DEGREE. That is what this test used first, and the
    25.0 km case failed: the real figure is 111.195, so "25 km" was 25.0004 km and fell
    outside an inclusive boundary by four tenths of a metre. The code was right and the
    fixture was wrong, which is the more dangerous way round because the instinct is to
    loosen the assertion until it passes.
    """
    return lat + math.degrees(km / mesh.EARTH_R_KM), lon


@pytest.mark.parametrize(
    "distance_km, expect_link",
    [(24.0, True), (25.0, True), (25.1, False)],
)
def test_a_ground_pair_links_up_to_exactly_25_km(distance_km, expect_link):
    """The boundary is inclusive, and it is worth pinning rather than inferring."""
    a = _asset("node", "a", 74.0, -95.0)
    b = _asset("node", "b", *_north_of(74.0, -95.0, distance_km))
    links = mesh.compute_links([a, b])
    assert bool(links) is expect_link


def test_only_a_drone_can_be_air_and_only_while_it_is_up():
    """Altitude decides it, not `props.state`, so it stays true after a user moves one."""
    assert mesh.link_state(_asset("uas", "flying", 74.0, -95.0, alt=3200.0)) == mesh.AIR
    assert mesh.link_state(_asset("uas", "parked", 74.0, -95.0, alt=0.0)) == mesh.GROUND
    # A node on a 400 m hill is still a ground asset. Terrain is not modelled here.
    assert mesh.link_state(_asset("node", "hill", 74.0, -95.0, alt=400.0)) == mesh.GROUND


def test_a_vessel_and_a_radar_site_are_not_on_the_mesh():
    """A design position, not an oversight: they are what the network is FOR."""
    node = _asset("node", "n", 74.0, -95.0)
    assert mesh.link_range_km(node, _asset("vessel", "v", 74.0, -95.0)) is None
    assert mesh.link_range_km(node, _asset("radar", "r", 74.0, -95.0)) is None


def test_margin_is_how_much_further_they_could_drift():
    a = _asset("node", "a", 74.0, -95.0)
    b = _asset("node", "b", *_north_of(74.0, -95.0, 10.0))
    link = mesh.compute_links([a, b])[0]
    assert link.range_km == 25.0
    assert link.margin_km == pytest.approx(25.0 - link.distance_km)
    assert link.margin_km == pytest.approx(15.0, abs=0.2)


# --------------------------------------------------------------------------
# The seeded world holds together
# --------------------------------------------------------------------------

def test_every_node_cluster_is_a_single_connected_chain(world):
    """The 22 km spacing exists to guarantee this, so it is checked rather than assumed.

    A cluster that quietly split would still render as a map full of dots and would only be
    visible as a mesh finding, which is exactly how the original seed shipped with nodes
    68 to 158 km apart and no links at all.
    """
    status = mesh.mesh_status(world)
    groups = [set(g["members"]) for g in status["groups"]]
    for cluster in ("barrow", "pow", "victoria", "nares"):
        members = {r["id"] for r in world if r["id"].startswith(f"node-{cluster}-")}
        assert any(members <= g for g in groups), f"{cluster} is not one connected group"


def test_the_hydrophone_array_meshes_with_itself(world):
    """It is a line of sensors 19.9 km apart; that spacing is what makes it one network."""
    status = mesh.mesh_status(world)
    array = {r["id"] for r in world if r["id"].startswith("hyd-lancaster-")}
    assert not (array & set(status["isolated"]))
    assert any(array <= set(g["members"]) for g in status["groups"])


def test_clusters_stay_separate_from_each_other(world):
    """They are hundreds of kilometres apart, and no code may imply otherwise."""
    status = mesh.mesh_status(world)
    for group in status["groups"]:
        clusters = {
            m.split("-")[1] for m in group["members"] if m.startswith("node-")
        }
        assert len(clusters) <= 1, f"two clusters merged into one group: {clusters}"


# --------------------------------------------------------------------------
# The two questions: linked, and able to reach us
# --------------------------------------------------------------------------

def test_isolated_and_unreachable_are_different_questions(world):
    """🔑 `fls-alert` is the case that proves it, and it is in the seed.

    No mesh neighbours at all, and its own satellite backhaul. One word could not describe
    both halves, which is why there are two flags.
    """
    status = mesh.mesh_status(world)
    assert "fls-alert" in status["isolated"]
    assert "fls-alert" in status["server_reachable"]
    assert "fls-alert" not in status["mesh_connected"]


def test_a_gateway_going_quiet_takes_its_whole_cluster_with_it(world):
    """The Barrow Strait story, which is the demo beat and therefore worth pinning.

    Every node in that chain is talking to its neighbours perfectly well. What broke is the
    one route out, and the honest picture is seven assets nobody can hear rather than one.
    """
    barrow = {r["id"] for r in world if r["id"].startswith("node-barrow-")}

    healthy = mesh.mesh_status(world)
    assert not (barrow & set(healthy["unreachable"]))

    dark = copy.deepcopy(world)
    for row in dark:
        if row["id"] == "node-barrow-05":  # the cluster's satellite uplink
            row["flag"] = "overdue"
    after = mesh.mesh_status(dark)

    assert barrow <= set(after["unreachable"])
    # Still linked to each other. That is the distinction the two flags exist for.
    assert barrow <= set(after["mesh_connected"])


def test_an_ordinary_node_going_quiet_takes_only_itself(world):
    """The counterpart to the test above: the cascade must not be indiscriminate."""
    dark = copy.deepcopy(world)
    for row in dark:
        if row["id"] == "node-barrow-02":
            row["flag"] = "overdue"
    unreachable = set(mesh.mesh_status(dark)["unreachable"])
    others = {f"node-barrow-0{i}" for i in (1, 3, 4)}
    assert "node-barrow-02" in unreachable
    assert not (others & unreachable)


def test_reachability_says_when_it_could_not_tell(world):
    """A row carrying neither `flag` nor `overdue` cannot answer, and is listed as such.

    Silence would make "nothing is grey" indistinguishable from "nothing was checked".
    """
    unknown = [{k: v for k, v in r.items() if k != "flag"} for r in world]
    status = mesh.mesh_status(unknown)
    assert len(status["reachability_unknown"]) == status["mesh_capable"]
    assert not mesh.mesh_status(world)["reachability_unknown"]


# --------------------------------------------------------------------------
# The drone relay: what the aircraft is actually for
# --------------------------------------------------------------------------

def test_a_drone_at_the_midpoint_brings_the_resolute_patrol_onto_the_mesh(world):
    """🥇 THE DEMO BEAT, PINNED.

    `patrol-resolute` sits 59.2 km from its own base, which is beyond the 25 km ground
    range, so it is on no mesh at all. One drone airborne between the two gives legs of about
    29.6 km, and 50 km ground-to-air covers both. This is the single clearest argument for
    having an aircraft in the picture, so it gets a test rather than a paragraph.
    """
    by_id = {r["id"]: r for r in world}
    patrol, base = by_id["patrol-resolute"], by_id["fls-resolute"]

    before = mesh.mesh_status(world)
    assert patrol["id"] in before["isolated"]
    assert patrol["id"] in before["unreachable"]

    relay = _asset(
        "uas",
        "uas-relay",
        (patrol["lat"] + base["lat"]) / 2,
        (patrol["lon"] + base["lon"]) / 2,
        alt=3200.0,
    )
    after = mesh.mesh_status([*world, relay])

    assert patrol["id"] not in after["isolated"]
    assert patrol["id"] in after["server_reachable"], "the patrol should now reach its base"


def test_the_same_drone_on_the_ground_does_not_bridge_anything(world):
    """It is the altitude that does the work, and the test proves it rather than asserting it."""
    by_id = {r["id"]: r for r in world}
    patrol, base = by_id["patrol-resolute"], by_id["fls-resolute"]
    landed = _asset(
        "uas",
        "uas-relay",
        (patrol["lat"] + base["lat"]) / 2,
        (patrol["lon"] + base["lon"]) / 2,
        alt=0.0,
    )
    after = mesh.mesh_status([*world, landed])
    assert patrol["id"] in after["unreachable"]


# --------------------------------------------------------------------------
# What the endpoint publishes
# --------------------------------------------------------------------------

def test_the_published_model_matches_the_ranges_actually_used():
    """The wire description is generated from the same table the links come from.

    It went false once already, when `/api/mesh` kept publishing a horizon formula after the
    model became a lookup. This asserts the two cannot drift apart again.
    """
    published = mesh.model()["ranges_km"]
    assert published["ground_to_ground"] == mesh.LINK_RANGE_KM[frozenset({mesh.GROUND})]
    assert published["ground_to_air"] == mesh.LINK_RANGE_KM[frozenset({mesh.GROUND, mesh.AIR})]
    assert published["air_to_air"] == mesh.LINK_RANGE_KM[frozenset({mesh.AIR})]


def test_the_published_model_does_not_describe_a_formula():
    """No horizon, no Fresnel, no path loss. If one comes back, this fails loudly."""
    blob = repr(mesh.model()).lower()
    for word in ("horizon", "fresnel", "sqrt", "path loss"):
        assert word not in blob or "not modelled" in blob


def test_gateways_are_found_by_property_and_not_by_kind(world):
    """A launch site, a cluster uplink node and a hydrophone are all gateways.

    Keyed on `props.backhaul`, so nothing has to know that one particular kind implies the
    role. That was a real bug: the map's badge keyed on a field the domain had stopped using.
    """
    gateways = {r["id"] for r in world if mesh.is_gateway(r)}
    assert "fls-alert" in gateways
    assert "node-barrow-05" in gateways
    assert "hyd-lancaster-05" in gateways
    assert "node-barrow-01" not in gateways
    kinds = {r["kind"] for r in world if r["id"] in gateways}
    assert len(kinds) > 1, "gateway must not be reducible to a single kind"


def test_asset_flags_covers_every_mesh_asset_and_nothing_else(world):
    flags = mesh.asset_flags(world)
    assert "vsl-unk-01" not in flags, "a vessel has no radio; absent beats a misleading False"
    assert flags["fls-alert"] == {"mesh_connected": False, "server_reachable": True}
    assert set(flags) == {r["id"] for r in world if r["kind"] in mesh.MESH_KINDS}
