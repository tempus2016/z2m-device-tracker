"""Stale retained messages: what a broker scan keeps, and which topics belong to no current device."""

from __future__ import annotations

import json

from conftest import PLUG, SENSOR, coordinator, send, z2m_device
from retained import DISCOVERY, MAX_TOPICS, OLD_NAME, REMOVED, RetainedStore, collect, counts

GONE = "0x00158d0000700001"
STRANGER = "0x00158d0000800001"
BASES = ["zigbee2mqtt"]


def scan(*items: tuple[str, object]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for topic, payload in items:
        raw = json.dumps(payload).encode() if isinstance(payload, (dict, list)) else str(payload).encode()
        collect(found, topic, raw, BASES, "homeassistant")
    return found


def discovery_config(name: str) -> dict:
    return {
        "name": "Battery",
        "state_topic": f"zigbee2mqtt/{name}",
        "availability": [{"topic": "zigbee2mqtt/bridge/state"}],
    }


def setup_network(tracker, clock):
    """Hall Motion renamed to Sim Motion Hall, a removed device, a group, and a plug."""
    send(tracker, "bridge/devices", [coordinator(), z2m_device(SENSOR, "Hall Motion"), z2m_device(GONE, "Leak Garage")])
    clock.advance(days=1)
    send(
        tracker,
        "bridge/devices",
        [coordinator(), z2m_device(SENSOR, "Sim Motion Hall"), z2m_device(PLUG, "Plug", "Router")],
    )
    send(tracker, "bridge/groups", [{"id": 1, "friendly_name": "Lounge lights", "members": []}])
    send(tracker, "Sim Motion Hall/availability", {"state": "online"})


def stale_of(tracker, found):
    store = RetainedStore()
    store.replace(found, False)
    tracker.retained = store
    return tracker.stale_retained()


def test_collect_skips_bridge_empty_and_foreign_topics():
    found = scan(
        ("zigbee2mqtt/bridge/devices", []),
        ("zigbee2mqtt/bridge/state", "online"),
        ("zigbee2mqtt/Plug", {"state": "ON"}),
        ("zigbee2mqtt/Old", ""),
        ("other/topic", "x"),
        ("homeassistant/sensor/z2m_tracker/0x00158d0000500001_battery/config", {"state_topic": "z2m_tracker/x"}),
        (f"homeassistant/sensor/{GONE}/battery/config", discovery_config("Leak Garage")),
        (f"homeassistant/device/{GONE}/config", discovery_config("Leak Garage")),
    )
    assert set(found) == {
        "zigbee2mqtt/Plug",
        f"homeassistant/sensor/{GONE}/battery/config",
        f"homeassistant/device/{GONE}/config",
    }
    assert found[f"homeassistant/sensor/{GONE}/battery/config"]["refs"] == ["zigbee2mqtt"]
    assert found[f"homeassistant/sensor/{GONE}/battery/config"]["ieee"] == GONE


def test_collect_trims_payload_and_stops_when_full():
    found: dict[str, dict] = {}
    assert collect(found, "zigbee2mqtt/Big", b"x" * 5000, BASES, "homeassistant")
    assert len(found["zigbee2mqtt/Big"]["payload"]) == 160
    assert found["zigbee2mqtt/Big"]["size"] == 5000
    for i in range(MAX_TOPICS - 1):
        found[f"zigbee2mqtt/d{i}"] = {"base": "zigbee2mqtt"}
    assert not collect(found, "zigbee2mqtt/one-too-many", b"1", BASES, "homeassistant")
    assert len(found) == MAX_TOPICS
    # A topic already kept is updated, not refused.
    assert collect(found, "zigbee2mqtt/Big", b"y", BASES, "homeassistant")


def test_old_name_removed_and_discovery(tracker, clock):
    setup_network(tracker, clock)
    found = scan(
        ("zigbee2mqtt/Hall Motion/availability", {"state": "offline"}),
        ("zigbee2mqtt/Hall Motion", {"occupancy": False}),
        ("zigbee2mqtt/Leak Garage/availability", {"state": "offline"}),
        (f"zigbee2mqtt/{GONE}/set", {"state": "ON"}),
        (f"homeassistant/sensor/{GONE}/battery/config", discovery_config("Leak Garage")),
    )
    stale = stale_of(tracker, found)
    by_topic = {e["topic"]: e for e in stale}
    hall = by_topic["zigbee2mqtt/Hall Motion/availability"]
    assert hall["reason"] == OLD_NAME
    assert hall["name"] == "Sim Motion Hall"
    assert hall["old_name"] == "Hall Motion"
    assert hall["availability"] == "online"
    assert hall["since"]  # when the name stopped being used
    assert by_topic["zigbee2mqtt/Hall Motion"]["reason"] == OLD_NAME
    assert by_topic["zigbee2mqtt/Leak Garage/availability"]["reason"] == REMOVED
    assert by_topic[f"zigbee2mqtt/{GONE}/set"]["reason"] == REMOVED
    assert by_topic[f"homeassistant/sensor/{GONE}/battery/config"]["reason"] == DISCOVERY
    assert by_topic[f"homeassistant/sensor/{GONE}/battery/config"]["name"] == "Leak Garage"
    assert counts(stale) == {OLD_NAME: 2, REMOVED: 2, DISCOVERY: 1}
    # Old names first, then removed devices, then discovery.
    assert [e["reason"] for e in stale] == [OLD_NAME, OLD_NAME, REMOVED, REMOVED, DISCOVERY]


def test_current_devices_groups_and_unknown_names_are_never_listed(tracker, clock):
    setup_network(tracker, clock)
    found = scan(
        ("zigbee2mqtt/Sim Motion Hall/availability", {"state": "online"}),
        ("zigbee2mqtt/Sim Motion Hall", {"occupancy": True}),
        ("zigbee2mqtt/Plug/set", {"state": "ON"}),
        (f"zigbee2mqtt/{PLUG}/set", {"state": "ON"}),
        ("zigbee2mqtt/Lounge lights", {"state": "ON"}),
        ("zigbee2mqtt/Something Else/availability", "offline"),
        (f"homeassistant/sensor/{SENSOR}/battery/config", discovery_config("Sim Motion Hall")),
    )
    assert stale_of(tracker, found) == []


def test_old_name_reused_by_another_device_is_current(tracker, clock):
    setup_network(tracker, clock)
    clock.advance(hours=1)
    send(
        tracker,
        "bridge/devices",
        [
            coordinator(),
            z2m_device(SENSOR, "Sim Motion Hall"),
            z2m_device(PLUG, "Hall Motion", "Router"),
        ],
    )
    found = scan(("zigbee2mqtt/Hall Motion/availability", {"state": "online"}), ("zigbee2mqtt/Plug", {"state": "ON"}))
    stale = stale_of(tracker, found)
    assert [(e["topic"], e["reason"]) for e in stale] == [("zigbee2mqtt/Plug", OLD_NAME)]
    assert stale[0]["name"] == "Hall Motion"


def test_name_with_slash_matches_longest_known_name(tracker, clock):
    send(
        tracker,
        "bridge/devices",
        [coordinator(), z2m_device(SENSOR, "Hall/Motion"), z2m_device(PLUG, "Hall", "Router")],
    )
    clock.advance(hours=1)
    send(
        tracker,
        "bridge/devices",
        [coordinator(), z2m_device(SENSOR, "Hall/Sensor"), z2m_device(PLUG, "Hall", "Router")],
    )
    found = scan(
        ("zigbee2mqtt/Hall/Motion/availability", {"state": "offline"}),
        ("zigbee2mqtt/Hall/availability", {"state": "online"}),
        ("zigbee2mqtt/Hall/Sensor", {"temperature": 20}),
    )
    stale = stale_of(tracker, found)
    assert [e["topic"] for e in stale] == ["zigbee2mqtt/Hall/Motion/availability"]


def test_discovery_needs_a_tracked_base_topic(tracker, clock):
    setup_network(tracker, clock)
    other = {"name": "Battery", "state_topic": "zigbee2mqtt_other/Neighbour"}
    found = scan(
        # Removed here, but the config points at a network the app doesn't track: maybe it moved there.
        (f"homeassistant/sensor/{GONE}/battery/config", other),
        # Never seen by the tracker, but its config points at a tracked network: left behind.
        (f"homeassistant/sensor/{STRANGER}/battery/config", discovery_config("Stranger")),
    )
    stale = stale_of(tracker, found)
    assert [(e["ieee"], e["reason"], e["name"]) for e in stale] == [(STRANGER, DISCOVERY, None)]


def test_nothing_until_lists_and_scan(tracker, clock):
    assert tracker.stale_retained() is None
    tracker.retained = RetainedStore()
    assert tracker.stale_retained() is None  # never scanned
    tracker.retained.replace({}, False)
    assert tracker.stale_retained() is None  # no device list yet
    payload = tracker.retained_payload()
    assert payload["ready"] is False and payload["total"] == 0


def test_health_issue_and_check_toggle(make_tracker, clock):
    for disabled, expected in (([], 1), (["stale_retained"], 0)):
        tracker = make_tracker(options={"disabled_checks": disabled})
        setup_network(tracker, clock)
        stale_of(tracker, scan(("zigbee2mqtt/Hall Motion/availability", {"state": "offline"})))
        issues, _ = tracker.evaluate_health()
        found = [i for i in issues if i["kind"] == "stale_retained"]
        assert len(found) == expected
        if found:
            issue = found[0]
            assert issue["severity"] == "info"
            assert issue["ieee"] == "network:broker"
            assert issue["params"] == {"n": 1, "topic": "zigbee2mqtt/Hall Motion/availability"}


def test_store_forget_and_payload(tracker, clock):
    setup_network(tracker, clock)
    found = scan(
        ("zigbee2mqtt/Hall Motion/availability", {"state": "offline"}),
        ("zigbee2mqtt/Leak Garage", {"water_leak": False}),
    )
    stale_of(tracker, found)
    payload = tracker.retained_payload()
    assert payload["ready"] and payload["total"] == 2
    assert payload["counts"][OLD_NAME] == 1 and payload["counts"][REMOVED] == 1
    tracker.retained.forget(["zigbee2mqtt/Leak Garage"])
    assert [e["topic"] for e in tracker.stale_retained()] == ["zigbee2mqtt/Hall Motion/availability"]
