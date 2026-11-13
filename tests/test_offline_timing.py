"""When a device's alerts fire: Zigbee2MQTT's availability settings plus the tracker's own delays."""

from __future__ import annotations

import json

from conftest import PLUG, SENSOR, send, z2m_device
from health import offline_timing, z2m_availability

NOW = 1_780_000_000.0


def info(availability=None, devices=None, version="2.6.0", **config) -> dict:
    config = {"availability": availability, "devices": devices or {}, **config}
    return {"version": version, "config": config}


def device(ieee=SENSOR, dtype="EndDevice", power="Battery", **extra) -> dict:
    return {"ieee": ieee, "status": "active", "type": dtype, "power_source": power, **extra}


def steps(timing: dict) -> list[tuple[str, int]]:
    return [(s["kind"], s["at"]) for s in timing["steps"]]


V2_ON = {"enabled": True, "active": {"timeout": 10, "max_jitter": 30000}, "passive": {"timeout": 1500}}


def test_v2_defaults_mains_and_battery():
    avail = z2m_availability(info(V2_ON))
    mains = offline_timing(device(PLUG, "Router", "Mains (single phase)"), avail, {}, NOW)
    assert mains["mode"] == "active" and mains["source"] == "default"
    assert steps(mains) == [("last", 0), ("z2m", 600), ("offline", 600 + 3600)]
    assert mains["total"] == 4200
    battery = offline_timing(device(), avail, {"offline_alert_minutes": 30}, NOW)
    assert battery["mode"] == "passive"
    assert steps(battery) == [("last", 0), ("z2m", 90_000), ("offline", 90_000 + 1800)]


def test_v2_mains_end_device_is_active_but_v1_only_counts_single_phase():
    dc = device(power="DC Source")
    assert offline_timing(dc, z2m_availability(info(V2_ON)), {}, NOW)["mode"] == "active"
    v1 = z2m_availability(info({}, version="1.42.0"))
    assert offline_timing(dc, v1, {}, NOW)["mode"] == "passive"
    assert offline_timing(device(power="Mains (single phase)"), v1, {}, NOW)["mode"] == "active"
    # A battery router is passive in both.
    assert offline_timing(device(dtype="Router"), v1, {}, NOW)["mode"] == "passive"


def test_custom_network_and_device_timeouts():
    avail = z2m_availability(
        info(
            {"enabled": True, "active": {"timeout": 5}, "passive": {"timeout": 60}},
            {SENSOR: {"availability": {"timeout": 20}}},
        )
    )
    plug = offline_timing(device(PLUG, "Router", "Mains (single phase)"), avail, {}, NOW)
    assert (plug["z2m"], plug["source"]) == (300, "custom")
    sensor = offline_timing(device(), avail, {}, NOW)
    assert (sensor["z2m"], sensor["source"]) == (1200, "device")


def test_report_every_sorts_in_and_disabled_checks_are_marked():
    record = device(PLUG, "Router", "Mains (single phase)", report_every=900)
    timing = offline_timing(record, z2m_availability(info(V2_ON)), {"disabled_checks": ["offline"]}, NOW)
    assert steps(timing) == [("last", 0), ("z2m", 600), ("late", 900), ("offline", 4200)]
    assert [s.get("off") for s in timing["steps"]] == [None, None, False, True]


def test_not_tracked_when_off_globally_or_for_the_device():
    off = offline_timing(device(), z2m_availability(info({"enabled": False})), {}, NOW)
    assert (off["tracked"], off["why"]) == (False, "network")
    assert steps(off) == [("last", 0), ("silent", 2 * 86400)]
    avail = z2m_availability(info(V2_ON, {SENSOR: {"availability": False}, PLUG: {"disabled": True}}))
    assert offline_timing(device(), avail, {}, NOW)["why"] == "device"
    assert offline_timing(device(PLUG), avail, {}, NOW)["why"] == "device"


def test_device_can_turn_it_on_when_network_is_off():
    avail = z2m_availability(info({"enabled": False}, {SENSOR: {"availability": True}}))
    assert offline_timing(device(), avail, {}, NOW)["tracked"] is True


def test_device_options_apply_to_every_device():
    avail = z2m_availability(info(V2_ON, device_options={"availability": {"timeout": 30}}))
    timing = offline_timing(device(), avail, {}, NOW)
    assert (timing["z2m"], timing["source"]) == (1800, "custom")


def test_v1_shapes():
    assert z2m_availability(info(None, version="1.40.0"))["enabled"] is False
    assert z2m_availability(info(True, version="1.40.0"))["enabled"] is True
    legacy = z2m_availability(info(None, version="1.40.0", advanced={"availability_timeout": 60}))
    assert legacy["enabled"] is True and legacy["active"] == 10
    lists = z2m_availability(info({}, version="1.40.0", advanced={"availability_blocklist": [SENSOR]}))
    assert offline_timing(device(), lists, {}, NOW)["tracked"] is False
    assert offline_timing(device(PLUG), lists, {}, NOW)["tracked"] is True
    passed = z2m_availability(info({}, version="1.40.0", advanced={"availability_passlist": ["Hall"]}))
    assert offline_timing(device(friendly_name="Hall"), passed, {}, NOW)["tracked"] is True
    assert offline_timing(device(PLUG), passed, {}, NOW)["tracked"] is False


def test_none_without_config_or_for_coordinator():
    assert z2m_availability({"version": "2.6.0"}) is None
    assert offline_timing(device(), None, {}, NOW) is None
    assert offline_timing(device(dtype="Coordinator"), z2m_availability(info(V2_ON)), {}, NOW) is None


def test_snapshot_carries_timing_from_bridge_info(tracker):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    send(
        tracker,
        "bridge/info",
        {"version": "2.6.0", "config": {"availability": V2_ON, "devices": {SENSOR: {"friendly_name": "Hall sensor"}}}},
    )
    stored = tracker.data["bridges"]["zigbee2mqtt"]["info"]["availability"]
    assert stored == {"major": 2, "enabled": True, "active": 10, "passive": 1500}
    item = next(d for d in json.loads(tracker.snapshot_json())["devices"] if d["ieee"] == SENSOR)
    assert item["offline_timing"]["total"] == 90_000 + 3600
