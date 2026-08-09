"""The link model, and the two different questions it answers about connectivity.

These are written against the seeded world rather than against hand-made fixtures wherever
the seed already contains the case, because the seed is what ships and a test that passes on
invented geometry says nothing about it. Where a case needs an asset somewhere the seed does
not put one, the fixture says so and builds only that.
"""
from __future__ import annotations

import copy
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from api._lib import db, freshness, mesh, tools
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


# --------------------------------------------------------------------------
# The backhaul question, which has two honest readings
# --------------------------------------------------------------------------


@pytest.fixture
def seeded(monkeypatch, world) -> list[dict]:
    """The seeded world behind the tool layer, so the counts are real ones."""
    monkeypatch.setattr(db, "fetch_entities", lambda kind=None: world)
    return world


def test_the_second_figure_is_reachability_not_group_membership(seeded):
    """⚠️ THE DISTINCTION IS THE POINT, not a detail of the wording.

    Sitting in a group that contains a gateway is topology. REACHING one means every hop on
    the path is an asset we are currently hearing from. An asset one dead relay away from a
    gateway is in the group and cannot get a message home, and this display exists in order
    not to call that connected.
    """
    result = tools.REGISTRY["backhaul_status"].fn()
    st = mesh.mesh_status(seeded)

    assert result.data["can_reach"] == st["server_reachable"]
    assert result.data["can_reach"] != st["mesh_connected"], (
        "if these ever match, the seed has no asset that is grouped but unreachable, and "
        "this assertion has stopped testing the distinction it was written for"
    )


def test_the_terminals_are_highlighted_and_the_reachable_set_is_not(seeded):
    """Lighting up two thirds of the map says nothing an operator can use. The terminals are
    what "which ones have a backhaul" points at; the reachable set is a number."""
    result = tools.REGISTRY["backhaul_status"].fn()
    carrying = set(result.data["carrying"])

    assert carrying, "the seeded world must contain backhaul terminals"
    assert set(result.ui_effects["highlight"]) == carrying
    assert len(carrying) < len(result.data["can_reach"]), (
        "carrying a terminal is rarer than being able to reach one, which is the whole "
        "reason the question has two answers"
    )


def test_the_message_names_a_few_terminals_rather_than_all_of_them(seeded):
    """Eleven names in a transcript line is a wall nobody reads, and the answer to "how
    many" is the number. The rest are highlighted on the map, which is a better place to
    look at eleven things."""
    result = tools.REGISTRY["backhaul_status"].fn()

    assert "more" in result.message, "a long list must be truncated with a count"
    assert result.message.count(",") <= 6


# --------------------------------------------------------------------------
# Being heard is a property of the PATH, not of the asset
#
# 🔴 THE BUG THESE EXIST TO STOP. Freshness used to be stamped per asset with no reference
# to the link graph, so a unit whose only route home had been dead for two days was
# reported as heard from eight minutes ago. Nine of thirteen cut-off assets were saying
# that at once, while the same response listed them as unreachable.
# --------------------------------------------------------------------------

def _chain() -> list[dict]:
    """A gateway, one relay, and a leaf that can only reach the gateway through the relay.

    Twenty km apart, so each neighbouring pair links at the 25 km ground range and the ends
    sit 40 km apart and do not. The structure is asserted rather than assumed in the first
    test, because geometry that quietly stops linking turns every test below into one that
    passes by testing nothing.
    """
    relay_lat, relay_lon = _north_of(74.0, -95.0, 20.0)
    leaf_lat, leaf_lon = _north_of(74.0, -95.0, 40.0)
    return [
        _asset("node", "gateway", 74.0, -95.0, backhaul="satellite"),
        _asset("node", "relay", relay_lat, relay_lon),
        _asset("node", "leaf", leaf_lat, leaf_lon),
    ]


def test_the_chain_is_a_chain_and_not_a_triangle():
    """The fixture the rest of this section depends on."""
    links = {frozenset((link.a, link.b)) for link in mesh.compute_links(_chain())}

    assert frozenset(("gateway", "relay")) in links
    assert frozenset(("relay", "leaf")) in links
    assert frozenset(("gateway", "leaf")) not in links, (
        "the ends must be out of range of each other, or the relay is not load bearing"
    )


def test_a_whole_live_chain_is_heard_now():
    heard = mesh.heard_through_mesh(
        _chain(), {"gateway": 0.0, "relay": 0.0, "leaf": 0.0}
    )

    assert heard == {"gateway": 0.0, "relay": 0.0, "leaf": 0.0}


def test_a_unit_behind_a_dead_relay_is_exactly_as_stale_as_the_relay():
    """🔑 THE INVARIANT. The leaf's radio is perfect and it has transmitted continuously.
    Nothing it sent has arrived since the relay stopped forwarding, so the console's
    information about it is precisely as old as the relay, and not one second newer."""
    heard = mesh.heard_through_mesh(
        _chain(), {"gateway": 0.0, "relay": 7200.0, "leaf": 0.0}
    )

    assert heard["gateway"] == 0.0, "the gateway is its own way out"
    assert heard["leaf"] == 7200.0, (
        "a healthy leaf behind a two-hour-old relay was last heard two hours ago"
    )


def test_an_asset_with_no_route_home_is_absent_rather_than_fresh():
    """Absent means the caller keeps whatever it already had. Returning zero would claim a
    live fix for something nothing has ever carried a packet from."""
    heard = mesh.heard_through_mesh(_chain(), {"gateway": 0.0, "leaf": 0.0})

    assert "leaf" not in heard
    assert heard["gateway"] == 0.0


def test_the_freshest_route_home_is_the_one_that_counts():
    """Two paths, and the asset is as fresh as the better of them. A bottleneck path is a
    minimax rather than a sum: the worst hop decides a route, the best route decides the
    asset."""
    rows = _chain()
    relay = rows[1]
    rows.append(_asset("node", "spare-relay", relay["lat"], relay["lon"] + 0.198))

    links = {frozenset((link.a, link.b)) for link in mesh.compute_links(rows)}
    assert frozenset(("gateway", "spare-relay")) in links
    assert frozenset(("spare-relay", "leaf")) in links

    heard = mesh.heard_through_mesh(
        rows,
        {"gateway": 0.0, "relay": 7200.0, "spare-relay": 60.0, "leaf": 0.0},
    )

    assert heard["leaf"] == 60.0, "the dead relay must not decide a leaf that has a second route"


@pytest.fixture
def living_world() -> list[dict]:
    """The seeded world with a birth time on every row.

    ⚠️ `seed_rows()` carries no `created_at`, and freshness needs one to tell an asset that
    is working from one that was laid down already silent. Without this single line nothing
    has ever transmitted, and every assertion below passes vacuously.
    """
    rows = seed_rows()
    born = datetime.now(UTC)
    for row in rows:
        row["created_at"] = born
    return rows


def test_nothing_is_both_cut_off_and_freshly_heard(living_world):
    """🔴 THE REGRESSION TEST. This is the contradiction the whole section exists for: the
    console reporting that an asset cannot reach it, and that it heard from that asset a
    moment ago, in one response."""
    rows = freshness.refresh(living_world)
    by_id = {row["id"]: row for row in rows}

    for asset_id in mesh.mesh_status(rows)["unreachable"]:
        age_s = (freshness.minutes_since_heard(by_id[asset_id]) or 0.0) * 60.0
        assert age_s > 30.0, (
            f"{asset_id} is unreachable and was stamped {age_s:.0f}s ago. Anything the "
            "report gate stamped is at most one beacon old, so this asset was refreshed "
            "by something that never asked whether its data arrived"
        )


def test_a_reachable_asset_is_heard_within_one_beacon(living_world):
    """The other half, and the reason the reporting interval had to be named. A working
    asset on a live route is heard every few seconds, not every few minutes: the value used
    to be jittered against a quarter of the OVERDUE threshold, which put healthy nodes at
    half an hour and healthy hydrophones at three quarters of one."""
    rows = freshness.refresh(living_world)
    by_id = {row["id"]: row for row in rows}
    ceiling = 2 * freshness.DEFAULT_REPORT_INTERVAL_SECONDS

    for asset_id in mesh.mesh_status(rows)["server_reachable"]:
        age_s = (freshness.minutes_since_heard(by_id[asset_id]) or 0.0) * 60.0
        assert age_s <= ceiling, f"{asset_id} reaches a gateway but reads {age_s:.0f}s old"


def test_a_placed_contact_does_not_poison_the_json():
    """🔴 THE REGRESSION TEST FOR A 500 THAT TOOK DOWN THE WHOLE WORLD.

    A placed asset arrives with `last_heard: None`. `_stamp` used to decide whether to write
    an ISO string or a `datetime` by asking whether `last_heard` was already a string, which
    None cannot answer, so it wrote a raw datetime into a row on its way to JSON. Every
    request to `/api/entities` then failed for every viewer until the asset was deleted, and
    the only way to trigger it was to use the feature.
    """
    now = datetime.now(UTC)
    placed = {
        "id": "vsl-placed-01",
        "kind": "vessel",
        "name": "Placed Vessel",
        "lat": 74.2,
        "lon": -84.0,
        # Serialised, exactly as the endpoint hands them over: timestamps are strings.
        "created_at": now.isoformat(),
        "last_heard": None,
        "ais_reporting": True,
        "props": {"emitting": True},
        "flag": "nominal",
    }
    # ⚠️ THE SEED IS PUT INTO THE SERIALISED SHAPE FIRST, which is the whole point: this
    # reproduces what `/api/entities` hands to `refresh`, where every timestamp is already
    # an ISO string. A fixture holding raw datetimes would be testing a code path that
    # never runs in the endpoint that broke.
    rows = [*seed_rows(), placed]
    for row in rows:
        row.setdefault("created_at", now.isoformat())
        for field in ("created_at", "last_heard"):
            if isinstance(row.get(field), datetime):
                row[field] = row[field].isoformat()

    freshness.refresh(rows, now)

    for row in rows:
        assert not isinstance(row.get("last_heard"), datetime), (
            f"{row['id']} carries a datetime in a serialised row, which is what made "
            "/api/entities return 500 for everyone"
        )
    json.dumps(rows)  # the actual failure, reproduced end to end


def test_the_client_and_server_agree_on_the_mesh_kinds():
    """The status strip counts our own kit, and it decides what that means from a copy of
    `MESH_KINDS` living in TypeScript.

    🔑 A MIRRORED CONSTANT IS A DRIFT RISK, SO IT IS PINNED RATHER THAN TRUSTED. Adding a
    kind on one side only would not break anything loudly: the strip would keep rendering,
    its two groups would keep summing to each other, and the total would simply stop
    counting a kind of asset nobody noticed was missing. That is the failure this codebase
    keeps paying for, and it costs fifteen lines to make it a red suite instead.
    """
    source = (Path(__file__).resolve().parents[1] / "src" / "assets.ts").read_text()

    block = re.search(
        r"export const MESH_KINDS[^=]*=\s*new Set<AssetKind>\(\[(.*?)\]\)", source, re.S
    )
    assert block, "could not find MESH_KINDS in src/assets.ts: the pin cannot be checked"

    client = set(re.findall(r'"([a-z_]+)"', block.group(1)))
    assert client == set(mesh.MESH_KINDS), (
        f"client MESH_KINDS {sorted(client)} does not match the server's "
        f"{sorted(mesh.MESH_KINDS)}. The status strip counts our own assets from the "
        "client copy, so a kind in one and not the other is a total that silently omits it"
    )


def test_the_reporting_interval_is_not_derived_from_the_overdue_threshold():
    """They are different quantities and the bug was deriving one from the other. A node
    tolerating two hours of silence does not mean it speaks every half hour."""
    for kind, interval in freshness.REPORT_INTERVAL_SECONDS.items():
        threshold_s = freshness.OVERDUE_MINUTES.get(kind, 0) * 60
        assert interval <= 10.0, f"{kind} claims to beacon only every {interval}s"
        if threshold_s:
            assert interval < threshold_s / 100, (
                f"{kind}'s beacon is within two orders of magnitude of its patience "
                "threshold, which is how the two got confused in the first place"
            )
