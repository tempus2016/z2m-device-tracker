"""Zigbee2MQTT groups: membership from bridge/groups, members that left a group, and the group_member_offline check."""

from __future__ import annotations

import json

from conftest import PLUG, ROUTER_A, ROUTER_B, SENSOR, send, z2m_device

SPOTS = [ROUTER_A, ROUTER_B, PLUG]


def _devices(tracker, ieees=SPOTS):
    send(tracker, "bridge/devices", [z2m_device(ieee, f"Spot {n}", "Router") for n, ieee in enumerate(ieees, 1)])


def _groups(tracker, members_by_group: dict[int, list[str]], names: dict[int, str] | None = None):
    payload = [
        {
            "id": gid,
            "friendly_name": (names or {}).get(gid, f"Group {gid}"),
            "description": None,
            "scenes": [{"id": 1, "name": "Evening"}],
            "members": [{"ieee_address": ieee.upper().replace("0X", "0x"), "endpoint": 1} for ieee in members],
        }
        for gid, members in members_by_group.items()
    ]
    send(tracker, "bridge/groups", payload)


def _health(tracker, clock):
    return tracker.group_health(clock.time())


# ------------------------------------------------------------------ membership parsing


def test_groups_payload_keeps_names_and_members(tracker, clock):
    _devices(tracker)
    send(
        tracker,
        "bridge/groups",
        [
            {
                "id": 3,
                "friendly_name": "Kitchen lights",
                "scenes": [],
                "members": [
                    {"ieee_address": ROUTER_A, "endpoint": 1},
                    {"ieee_address": ROUTER_A, "endpoint": 2},
                    {"ieee_address": ROUTER_B, "endpoint": 1},
                ],
            },
            {"id": 4, "friendly_name": "Empty", "members": []},
            "junk",
            {"friendly_name": "No id"},
        ],
    )
    assert tracker.groups["zigbee2mqtt"] == {"3": "Kitchen lights", "4": "Empty"}
    assert tracker.group_members["zigbee2mqtt"] == {"3": [ROUTER_A, ROUTER_B], "4": []}
    # Membership stays in memory, not in history.json.
    assert "group_members" not in json.dumps(tracker.data)

    health = _health(tracker, clock)
    assert [(g["name"], g["total"], g["offline"]) for g in health] == [("Kitchen lights", 2, 0)]
    assert [m["name"] for m in health[0]["members"]] == ["Spot 1", "Spot 2"]
    snapshot = json.loads(tracker.snapshot_json())
    assert snapshot["group_health"][0]["id"] == "3"


def test_offline_member_counts_and_sorting(tracker, clock):
    _devices(tracker)
    _groups(tracker, {1: [ROUTER_A, ROUTER_B], 2: [PLUG]}, {1: "Living room", 2: "Hall"})
    send(tracker, "Spot 3/availability", "online")
    clock.advance(minutes=5)
    send(tracker, "Spot 3/availability", '{"state": "offline"}')
    clock.advance(hours=2)
    health = _health(tracker, clock)
    assert [(g["name"], g["offline"]) for g in health] == [("Hall", 1), ("Living room", 0)]
    assert health[0]["members"][0] == {"ieee": PLUG, "name": "Spot 3", "offline": True, "offline_seconds": 7200}


# ------------------------------------------------------------------ left the group


def test_member_that_left_the_group_is_flagged(tracker, clock):
    _devices(tracker)
    _groups(tracker, {1: SPOTS}, {1: "Kitchen"})
    assert tracker.data.get("group_left") in (None, [])
    clock.advance(hours=1)
    _groups(tracker, {1: [ROUTER_A, ROUTER_B]}, {1: "Kitchen"})
    assert tracker.data["group_left"] == [{"instance": "zigbee2mqtt", "group": "1", "ieee": PLUG, "at": clock.iso()}]
    group = _health(tracker, clock)[0]
    assert group["total"] == 2
    assert group["left"] == [{"ieee": PLUG, "name": "Spot 3", "at": clock.iso()}]

    # Back in the group: the fact goes.
    _groups(tracker, {1: SPOTS}, {1: "Kitchen"})
    assert tracker.data["group_left"] == []
    assert _health(tracker, clock)[0]["left"] == []


def test_device_that_left_the_network_is_not_flagged_until_it_returns(tracker, clock):
    _devices(tracker)
    _groups(tracker, {1: SPOTS})
    # Z2M publishes bridge/groups before bridge/devices when a device leaves.
    _groups(tracker, {1: [ROUTER_A, ROUTER_B]})
    _devices(tracker, [ROUTER_A, ROUTER_B])
    assert tracker.data["devices"][PLUG]["status"] == "removed"
    assert _health(tracker, clock)[0]["left"] == []

    # Re-paired but not put back in the group: now it's flagged.
    clock.advance(days=1)
    _devices(tracker)
    assert [m["ieee"] for m in _health(tracker, clock)[0]["left"]] == [PLUG]


def test_left_facts_expire_and_go_with_their_group(tracker, clock):
    _devices(tracker)
    _groups(tracker, {1: SPOTS, 2: [ROUTER_A, SENSOR]})
    _groups(tracker, {1: [ROUTER_A], 2: [ROUTER_A, SENSOR]})
    assert len(tracker.data["group_left"]) == 2
    clock.advance(days=31)
    assert all(g["left"] == [] for g in _health(tracker, clock))

    # Group 1 deleted: its facts are dropped from history.json.
    _groups(tracker, {2: [ROUTER_A, SENSOR]})
    assert tracker.data["group_left"] == []


def test_first_groups_message_does_not_invent_leaves(tracker, clock):
    _devices(tracker)
    _groups(tracker, {1: [ROUTER_A]})
    assert not tracker.data.get("group_left")


def test_left_facts_are_capped(tracker, clock, monkeypatch):
    import tracker as tracker_module

    monkeypatch.setattr(tracker_module, "MAX_GROUP_LEFT", 3)
    _devices(tracker)
    _groups(tracker, {g: SPOTS for g in range(1, 4)})
    _groups(tracker, {g: [] for g in range(1, 4)})
    assert len(tracker.data["group_left"]) == 3


def test_left_facts_survive_save_and_load(tracker, make_tracker, clock):
    _devices(tracker)
    _groups(tracker, {1: SPOTS})
    _groups(tracker, {1: [ROUTER_A]})
    tracker.save()
    again = make_tracker()
    again.load()
    assert [f["ieee"] for f in again.data["group_left"]] == [ROUTER_B, PLUG]


# ------------------------------------------------------------------ group_member_offline issue


def _offline(tracker, name):
    send(tracker, f"{name}/availability", "online")
    send(tracker, f"{name}/availability", "offline")


def test_group_member_offline_issue(make_tracker, clock):
    tracker = make_tracker(options={"offline_alert_minutes": 60})
    _devices(tracker)
    _groups(tracker, {5: SPOTS}, {5: "Kitchen lights"})
    _offline(tracker, "Spot 1")
    _offline(tracker, "Spot 3")
    clock.advance(minutes=59)
    assert [i["kind"] for i in tracker.evaluate_health()[0] if i["kind"] == "group_member_offline"] == []

    clock.advance(minutes=1)
    issues = [i for i in tracker.evaluate_health()[0] if i["kind"] == "group_member_offline"]
    assert len(issues) == 1
    issue = issues[0]
    assert issue["key"] == "group_member_offline:zigbee2mqtt:5"
    assert issue["ieee"] == "group:zigbee2mqtt:5"
    assert issue["severity"] == "info"
    assert issue["name"] == "Kitchen lights"
    assert issue["instance"] == "zigbee2mqtt"
    assert issue["detail"] == "2 of 3 members offline: Spot 1, Spot 3."
    assert issue["params"] == {"n": 2, "total": 3, "names": "Spot 1, Spot 3"}

    # The member devices' own offline issues are still per device; the snapshot doesn't attach the group issue to one.
    snapshot = json.loads(tracker.snapshot_json())
    assert all(i["kind"] != "group_member_offline" for d in snapshot["devices"] for i in d["issues"])

    send(tracker, "Spot 1/availability", "online")
    send(tracker, "Spot 3/availability", "online")
    assert [i for i in tracker.evaluate_health()[0] if i["kind"] == "group_member_offline"] == []


def test_group_member_offline_can_be_disabled(make_tracker, clock):
    tracker = make_tracker(options={"offline_alert_minutes": 0, "disabled_checks": ["group_member_offline"]})
    _devices(tracker)
    _groups(tracker, {1: SPOTS})
    _offline(tracker, "Spot 2")
    assert [i for i in tracker.evaluate_health()[0] if i["kind"] == "group_member_offline"] == []


def test_group_member_offline_is_not_a_default_alert():
    from health import ALERT_KINDS, CHECKS, DEFAULT_NOTIFY, KIND_LABELS

    assert "group_member_offline" not in ALERT_KINDS
    assert "group_member_offline" not in DEFAULT_NOTIFY
    assert "group_member_offline" in KIND_LABELS
    assert any(c[0] == "group_member_offline" for c in CHECKS)
