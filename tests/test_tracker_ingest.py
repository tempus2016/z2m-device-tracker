"""Public ingest methods: how a non-MQTT source (ZHA) feeds the tracker."""

from __future__ import annotations

from conftest import events_of, send, z2m_device

A = "0xaabbcc0000000002"
B = "0xaabbcc0000000003"


def _listed(tracker):
    tracker.apply_device_list("zha", [z2m_device(A, "Air", "Router"), z2m_device(B, "Hall/Lamp")])


def test_apply_device_list_adds_devices(make_tracker):
    tracker = make_tracker(["zha"])
    _listed(tracker)
    records = tracker.data["devices"]
    assert {A, B} <= set(records)
    assert records[A]["instance"] == "zha" and records[A]["status"] == "active"


def test_apply_availability_offline_event(make_tracker):
    tracker = make_tracker(["zha"])
    _listed(tracker)
    tracker.apply_availability("zha", "Air", "online")
    tracker.apply_availability("zha", "Air", "offline")
    assert len(events_of(tracker, "offline")) == 1
    assert tracker.data["devices"][A]["availability"] == "offline"


def test_apply_state_sets_lqi_and_battery(make_tracker):
    tracker = make_tracker(["zha"])
    _listed(tracker)
    tracker.apply_state("zha", "Air", {"linkquality": 120, "battery": 80})
    record = tracker.data["devices"][A]
    assert record["lqi"] == 120
    assert record["battery"] == 80


def test_apply_state_with_slash_in_name(make_tracker):
    tracker = make_tracker(["zha"])
    _listed(tracker)
    tracker.apply_state("zha", "Hall/Lamp", {"linkquality": 90})
    assert tracker.data["devices"][B]["lqi"] == 90


def test_apply_networkmap_sets_parent(make_tracker):
    tracker = make_tracker(["zha"])
    _listed(tracker)
    tracker.apply_networkmap(
        "zha",
        {
            "nodes": [{"ieeeAddr": A}, {"ieeeAddr": B}],
            "links": [{"sourceIeeeAddr": B, "targetIeeeAddr": A, "lqi": 150, "relationship": 1}],
        },
    )
    assert tracker.data["devices"][B]["routing"]["parent"]["ieee"] == A


def test_set_bridge_state_offline_issue(make_tracker, clock):
    tracker = make_tracker(["zha"])
    _listed(tracker)
    tracker.set_bridge_state("zha", "online")
    tracker.set_bridge_state("zha", "offline")
    clock.advance(minutes=3)
    issues, _ = tracker.evaluate_health()
    assert [i for i in issues if i["kind"] == "network_offline" and i["instance"] == "zha"]


# ------------------------------------------------------------------ wording for ZHA networks


def test_zha_events_say_zha(make_tracker):
    tracker = make_tracker(["zha"])
    tracker.apply_device_list("zha", [z2m_device(A, "Air", "Router")])
    tracker.apply_device_list("zha", [z2m_device(A, "Air", "Router"), z2m_device(B, "Lamp")])
    tracker.apply_device_list("zha", [z2m_device(A, "Air", "Router")])
    assert events_of(tracker, "added")[0]["detail"] == "Appeared in the ZHA device list"
    assert events_of(tracker, "removed")[0]["detail"] == "Gone from the ZHA device list"
    assert tracker.data["devices"][A]["sources"] == ["ZHA live"]
    tracker.set_bridge_state("zha", "online")
    tracker.set_bridge_state("zha", "offline")
    tracker.set_bridge_state("zha", "online")
    details = [e["detail"] for e in tracker.data["events"] if e["kind"].startswith("network")]
    assert details == [
        "ZHA stopped (coordinator or Home Assistant's ZHA integration unavailable)",
        "ZHA is running again",
    ]


def test_z2m_events_unchanged(tracker):
    send(tracker, "bridge/devices", [z2m_device(A, "Air", "Router")])
    send(tracker, "bridge/devices", [z2m_device(A, "Air", "Router"), z2m_device(B, "Lamp")])
    send(tracker, "bridge/devices", [z2m_device(A, "Air", "Router")])
    assert events_of(tracker, "added")[0]["detail"] == "Appeared in the Z2M device list"
    assert events_of(tracker, "removed")[0]["detail"] == "Gone from the Z2M device list"
    assert tracker.data["devices"][A]["sources"] == ["Z2M live"]
    send(tracker, "bridge/state", "online")
    send(tracker, "bridge/state", "offline")
    send(tracker, "bridge/state", "online")
    details = [e["detail"] for e in tracker.data["events"] if e["kind"].startswith("network")]
    assert details == ["Zigbee2MQTT stopped (coordinator or service offline)", "Zigbee2MQTT is running again"]


def test_zha_network_offline_title(make_tracker, clock):
    tracker = make_tracker(["zha"])
    tracker.set_bridge_state("zha", "online")
    tracker.set_bridge_state("zha", "offline")
    clock.advance(minutes=3)
    issues, _ = tracker.evaluate_health()
    issue = next(i for i in issues if i["kind"] == "network_offline")
    assert issue["title"] == "ZHA offline"
    assert issue["name"] == "ZHA network"
    assert issue["msg"] == "network_offline_zha"
