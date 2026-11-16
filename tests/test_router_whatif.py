"""Router what-if (route_whatif.py) and where to add a router (router_placement.py)."""

from __future__ import annotations

import json

import route_whatif
import router_placement
from conftest import COORD, ROUTER_A, ROUTER_B, SENSOR, coordinator, send, z2m_device

ROUTER_D = "0x00124b0000d00001"
S2 = "0x00158d0000500002"
S3 = "0x00158d0000500003"
S4 = "0x00158d0000500004"


def node(dtype, name, parent=None, **nbrs):
    return {"type": dtype, "name": name, "parent": parent, "neighbours": nbrs}


def sample_nodes():
    """Garage plug (A) carries Shed plug (D) to the mesh; Hall plug (B) hangs off the coordinator."""
    return {
        COORD: node("Coordinator", "Coordinator"),
        ROUTER_A: node("Router", "Garage plug", COORD, **{COORD: 180, ROUTER_D: 120}),
        ROUTER_B: node("Router", "Hall plug", COORD, **{COORD: 200}),
        ROUTER_D: node("Router", "Shed plug", ROUTER_A, **{ROUTER_A: 120}),
        SENSOR: node("EndDevice", "Porch sensor", ROUTER_A, **{ROUTER_A: 90, ROUTER_B: 80, COORD: 40}),
        S2: node("EndDevice", "Garage sensor", ROUTER_A, **{ROUTER_A: 100}),
        S3: node("EndDevice", "Shed sensor", ROUTER_D, **{ROUTER_D: 70}),
        S4: node("EndDevice", "Shed door", ROUTER_D, **{ROUTER_D: 60, ROUTER_B: 30}),
    }


def test_hearing_is_symmetric_and_drops_zero_and_unknown():
    hear = route_whatif.hearing(
        {
            "a": node("Router", "A", None, b=50, c=0, zz=90),
            "b": node("Router", "B", None, a=70),
            "c": node("Router", "C"),
        }
    )
    assert hear == {"a": {"b": 70}, "b": {"a": 70}, "c": {}}


def test_what_if_moves_orphans_and_targets():
    result = route_whatif.what_if(sample_nodes(), COORD, ROUTER_A)
    assert result["name"] == "Garage plug"
    assert [(m["name"], m["reason"], m["to_name"], m["lqi"]) for m in result["moves"]] == [
        ("Porch sensor", "child", "Hall plug", 80),
        ("Shed door", "behind", "Hall plug", 30),
    ]
    assert [(o["name"], o["reason"]) for o in result["orphans"]] == [("Shed plug", "cut_off")]
    # The scan only shows an end device's link to its parent, so one with no other link isn't orphaned.
    assert [(x["name"], x["reason"]) for x in result["rejoin"]] == [
        ("Garage sensor", "child"),
        ("Shed sensor", "behind"),
    ]
    assert result["targets"] == [{"ieee": ROUTER_B, "name": "Hall plug", "count": 2}]


def test_what_if_leaf_router_only_moves_its_children():
    nodes = sample_nodes()
    result = route_whatif.what_if(nodes, COORD, ROUTER_B)
    assert result["moves"] == [] and result["orphans"] == [] and result["rejoin"] == []
    nodes[S2]["parent"] = ROUTER_B
    nodes[S2]["neighbours"] = {ROUTER_B: 90, COORD: 90}
    moved = route_whatif.what_if(nodes, COORD, ROUTER_B)["moves"]
    assert [(m["name"], m["to_name"]) for m in moved] == [
        ("Garage sensor", "Coordinator")
    ]  # tie goes to the coordinator


def test_child_router_with_another_path_moves():
    nodes = sample_nodes()
    nodes[ROUTER_D]["neighbours"][ROUTER_B] = 55
    result = route_whatif.what_if(nodes, COORD, ROUTER_A)
    assert ("Shed plug", "Hall plug") in [(m["name"], m["to_name"]) for m in result["moves"]]
    # Shed plug still reaches the coordinator, so its own children stay put.
    assert {x["name"] for k in ("moves", "orphans", "rejoin") for x in result[k]} == {
        "Porch sensor",
        "Garage sensor",
        "Shed plug",
    }


# ------------------------------------------------------------------ tracker + scan


def link(source, target, lqi, relationship=2):
    return {"source": {"ieeeAddr": source}, "target": {"ieeeAddr": target}, "lqi": lqi, "relationship": relationship}


def scan(tracker, links, areas=None):
    devices = [
        coordinator(),
        z2m_device(ROUTER_A, "Garage plug", "Router"),
        z2m_device(ROUTER_B, "Hall plug", "Router"),
        z2m_device(SENSOR, "Shed sensor"),
        z2m_device(S2, "Utility sensor"),
    ]
    send(tracker, "bridge/devices", devices)
    for ieee, area in (areas or {}).items():
        tracker.data["devices"][ieee]["area"] = area
    nodes = [{"ieeeAddr": i} for i in (COORD, ROUTER_A, ROUTER_B, SENSOR, S2)]
    send(tracker, "bridge/response/networkmap", {"status": "ok", "data": {"value": {"nodes": nodes, "links": links}}})


MESH = [
    link(ROUTER_A, COORD, 200, 1),
    link(ROUTER_B, COORD, 180, 1),
    link(ROUTER_A, ROUTER_B, 120),
    link(SENSOR, ROUTER_A, 45, 1),
    link(S2, ROUTER_A, 40, 1),
    link(S2, ROUTER_B, 70),
]


def test_tracker_what_if_from_latest_scan(tracker, clock):
    scan(tracker, MESH)
    result = tracker.router_what_if("zigbee2mqtt", ROUTER_A)
    assert result["scanned_at"] == clock.iso() and result["net"] == "zigbee2mqtt"
    assert [(m["name"], m["to_name"], m["lqi"]) for m in result["moves"]] == [("Utility sensor", "Hall plug", 70)]
    assert result["orphans"] == []
    assert [x["name"] for x in result["rejoin"]] == ["Shed sensor"]
    # Only routers in the latest scan, on that network.
    assert tracker.router_what_if("zigbee2mqtt", SENSOR) is None
    assert tracker.router_what_if("zigbee2mqtt", COORD) is None
    assert tracker.router_what_if("other", ROUTER_A) is None


def test_what_if_needs_a_scan(tracker):
    send(tracker, "bridge/devices", [coordinator(), z2m_device(ROUTER_A, "Garage plug", "Router")])
    assert tracker.router_what_if("zigbee2mqtt", ROUTER_A) is None
    assert tracker.router_placement("zigbee2mqtt", 0) == {"areas": [], "no_area": 0}


def test_placement_ranks_areas_from_scan(tracker, clock):
    scan(tracker, MESH, {SENSOR: "Shed", S2: "Utility room", ROUTER_A: "Garage", ROUTER_B: "Hall"})
    placement = tracker.router_placement("zigbee2mqtt", clock.now)
    # Both sensors have a weak link. Shed sensor only shows its parent in the scan, but that's all a
    # scan ever shows for an end device, so it isn't counted as having no backup route.
    assert [(a["area"], a["score"], a["weak"], a["no_backup"]) for a in placement["areas"]] == [
        ("Shed", 2, 1, 0),
        ("Utility room", 2, 1, 0),
    ]
    assert placement["areas"][0]["devices"] == [{"ieee": SENSOR, "name": "Shed sensor", "reasons": ["weak"]}]
    snapshot_keys = json.loads(tracker.snapshot_json())["router_placement"]
    assert snapshot_keys["zigbee2mqtt"]["areas"][0]["area"] == "Shed"


def test_rank_areas_counts_and_limits():
    rows = [
        {"ieee": "a", "name": "A", "area": "Loft", "type": "EndDevice", "link_lqi": 30, "hops": 4},
        {"ieee": "b", "name": "B", "area": "Loft", "type": "EndDevice", "link_lqi": 90, "offline": True},
        {"ieee": "c", "name": "C", "area": "Loft", "type": "Router", "link_lqi": 120},
        {"ieee": "d", "name": "D", "area": "Hall", "type": "EndDevice", "link_lqi": 90, "flapping": True},
        {"ieee": "e", "name": "E", "area": None, "type": "EndDevice", "link_lqi": 10},
        {"ieee": "f", "name": "F", "area": "Den", "type": "EndDevice", "no_backup": True},
        {"ieee": "g", "name": "G", "area": "Attic", "type": "Router", "link_lqi": 20},
        {"ieee": "h", "name": "H", "area": "Porch", "type": "EndDevice", "link_lqi": 49},
    ]
    result = router_placement.rank_areas(rows)
    assert result["no_area"] == 1
    # Hall (one dropout, score 1) is below the minimum; Attic/Porch tie on 2 and lose to the top three by name.
    assert [(a["area"], a["score"]) for a in result["areas"]] == [("Loft", 5), ("Den", 3), ("Attic", 2)]
    loft = result["areas"][0]
    assert (loft["weak"], loft["hopping"], loft["unstable"], loft["routers"]) == (1, 1, 1, 1)
    assert [d["name"] for d in loft["devices"]] == ["A", "B"]
    assert len(router_placement.rank_areas(rows, top=10)["areas"]) == 4
