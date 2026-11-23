"""Re-paired or renamed device: availability sent before the new device list still lands (#73)."""

from __future__ import annotations

from conftest import ROUTER_A, SENSOR, coordinator, send, z2m_device

LIST = [coordinator(), z2m_device(ROUTER_A, "Hall plug", "Router"), z2m_device(SENSOR, "Motion")]
WITHOUT_SENSOR = [coordinator(), z2m_device(ROUTER_A, "Hall plug", "Router")]


def test_online_before_device_list_is_applied(tracker, clock):
    send(tracker, "bridge/devices", LIST)
    send(tracker, "Motion/availability", {"state": "offline"})
    rec = tracker.data["devices"][SENSOR]
    assert rec["availability"] == "offline"
    # Removed in Zigbee2MQTT with its configuration kept, then paired again under the same name.
    send(tracker, "bridge/devices", WITHOUT_SENSOR)
    clock.advance(hours=1)
    send(tracker, "Motion/availability", {"state": "online"})
    send(tracker, "bridge/devices", LIST)
    assert rec["status"] == "active"
    assert rec["availability"] == "online"
    assert rec["outages"][-1]["end"]


def test_rejoin_forgets_old_offline(tracker, clock):
    send(tracker, "bridge/devices", LIST)
    send(tracker, "Motion/availability", "offline")
    send(tracker, "bridge/devices", WITHOUT_SENSOR)
    clock.advance(hours=1)
    send(tracker, "bridge/devices", LIST)
    rec = tracker.data["devices"][SENSOR]
    assert rec["availability"] is None
    assert rec["availability_since"] is None
    assert rec["outages"][-1]["end"] == clock.iso()


def test_renamed_device_gets_new_name_availability(tracker):
    send(tracker, "bridge/devices", LIST)
    send(tracker, "Motion/availability", "offline")
    send(tracker, "Landing motion/availability", "online")
    send(tracker, "bridge/devices", [coordinator(), z2m_device(SENSOR, "Landing motion")])
    assert tracker.data["devices"][SENSOR]["availability"] == "online"


def test_first_start_retained_availability_before_list(tracker):
    send(tracker, "Motion/availability", "offline")
    send(tracker, "bridge/devices", LIST)
    assert tracker.data["devices"][SENSOR]["availability"] == "offline"


def test_unlisted_names_are_dropped_after_list(tracker):
    send(tracker, "Ghost/availability", "offline")
    send(tracker, "bridge/devices", LIST)
    assert tracker._unlisted_availability == {}
    send(tracker, "bridge/devices", LIST + [z2m_device("0x00158d0000500099", "Ghost")])
    assert tracker.data["devices"]["0x00158d0000500099"]["availability"] is None
