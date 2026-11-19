"""Devices from a network the app doesn't listen to are frozen; moves across stacks clear Z2M-only data."""

from __future__ import annotations

import json

from conftest import SENSOR, events_of, send, z2m_device

SECOND = "0xaabbcc0000000010"


def _offline_on_second(make_tracker, clock):
    """A device on base topic "second" that went offline, saved to disk."""
    tracker = make_tracker(["zigbee2mqtt", "second"])
    send(tracker, "bridge/devices", [z2m_device(SECOND, "Shed sensor")], base="second")
    send(tracker, "Shed sensor/availability", "online", base="second")
    send(tracker, "Shed sensor/availability", "offline", base="second")
    tracker.save(force=True)
    clock.advance(days=3)


def _issues_for(tracker, ieee):
    issues, _ = tracker.evaluate_health()
    return [i for i in issues if i["ieee"] == ieee]


def test_removed_topic_records_are_frozen(make_tracker, clock):
    _offline_on_second(make_tracker, clock)
    tracker = make_tracker(["zigbee2mqtt"])
    tracker.load()
    record = tracker.data["devices"][SECOND]
    assert tracker.is_frozen(record)
    assert _issues_for(tracker, SECOND) == []


def test_frozen_thaws_when_topic_returns(make_tracker, clock):
    _offline_on_second(make_tracker, clock)
    tracker = make_tracker(["zigbee2mqtt", "second"])
    tracker.load()
    assert not tracker.is_frozen(tracker.data["devices"][SECOND])
    assert _issues_for(tracker, SECOND)


def test_unknown_instance_not_frozen(tracker):
    assert not tracker.is_frozen({"instance": "unknown"})
    assert not tracker.is_frozen({"instance": None})
    assert not tracker.is_frozen({"instance": "zigbee2mqtt"})
    assert tracker.is_frozen({"instance": "zha"})


def test_move_to_zha_clears_z2m_fields(make_tracker):
    tracker = make_tracker(["zigbee2mqtt", "zha"])
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    send(tracker, "Hall sensor/availability", "online")
    send(tracker, "Hall sensor/availability", "offline")
    record = tracker.data["devices"][SENSOR]
    record["bindings"] = [{"cluster": "genOnOff"}]
    record["payload_keys"] = {"temperature": 3}
    record["update"] = {"state": "available"}
    tracker.apply_device_list("zha", [{"ieee_address": SENSOR, "friendly_name": "Hall sensor", "type": "EndDevice"}])
    record = tracker.data["devices"][SENSOR]
    assert record["instance"] == "zha"
    assert events_of(tracker, "moved")
    assert record["bindings"] is None
    assert not record.get("payload_keys")
    assert record["update"] is None
    assert record["features"] == []
    assert record["outages"]
    assert record["names"]


def test_move_between_z2m_topics_keeps_fields(make_tracker):
    tracker = make_tracker(["zigbee2mqtt", "second"])
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    tracker.data["devices"][SENSOR]["bindings"] = [{"cluster": "genOnOff"}]
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")], base="second")
    assert tracker.data["devices"][SENSOR]["instance"] == "second"
    assert tracker.data["devices"][SENSOR]["bindings"] == [{"cluster": "genOnOff"}]


def test_snapshot_marks_frozen(make_tracker, clock):
    _offline_on_second(make_tracker, clock)
    tracker = make_tracker(["zigbee2mqtt"])
    tracker.load()
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    devices = {d["ieee"]: d for d in json.loads(tracker.snapshot_json())["devices"]}
    assert devices[SECOND]["frozen"] is True
    assert devices[SENSOR]["frozen"] is False


def test_frozen_devices_not_in_battery_shopping(make_tracker, clock):
    tracker = make_tracker(["zigbee2mqtt", "second"])
    send(tracker, "bridge/devices", [z2m_device(SECOND, "Shed sensor")], base="second")
    send(tracker, "Shed sensor", {"battery": 5}, base="second")
    assert any(d["ieee"] == SECOND for g in tracker.battery_shopping(60) for d in g["devices"])
    tracker.save(force=True)
    frozen = make_tracker(["zigbee2mqtt"])
    frozen.load()
    assert not any(d["ieee"] == SECOND for g in frozen.battery_shopping(60) for d in g["devices"])


def test_restore_keeps_other_networks_frozen(make_tracker, clock):
    # Restoring a Zigbee2MQTT backup while on ZHA: history comes back, nothing alerts.
    old = make_tracker(["zigbee2mqtt"])
    send(old, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    send(old, "Hall sensor/availability", "online")
    send(old, "Hall sensor/availability", "offline")
    tracker = make_tracker(["zha"], options={"zigbee_stack": "zha"})
    assert tracker.restore(json.loads(old.backup()))["ok"]
    record = tracker.data["devices"][SENSOR]
    assert record["instance"] == "zigbee2mqtt"
    assert tracker.is_frozen(record)
    clock.advance(hours=3)
    assert _issues_for(tracker, SENSOR) == []


def test_no_requests_to_a_network_not_listened_to(make_tracker):
    import pytest

    tracker = make_tracker(["zigbee2mqtt"])
    sent = []
    tracker.publish = lambda topic, payload: sent.append(topic)
    with pytest.raises(RuntimeError):
        tracker.request("second", "device/configure", {"id": SECOND}, timeout=0.01)
    assert sent == []
    tracker.request("zigbee2mqtt", "device/configure", {"id": SENSOR}, timeout=0.01)
    assert sent == ["zigbee2mqtt/bridge/request/device/configure"]


def test_frozen_devices_left_out_of_upkeep_lists(make_tracker, clock):
    # Warranties, models and ages on the Health and Upkeep tabs are about the network in use.
    both = make_tracker(["zigbee2mqtt", "second"])
    send(both, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    send(both, "bridge/devices", [z2m_device(SECOND, "Shed sensor", model_id="shed.model")], base="second")
    both.set_asset(SECOND, {"warranty_until": "2026-06-19", "purchase_date": "2024-06-19"})
    both.set_asset(SENSOR, {"warranty_until": "2026-06-20", "purchase_date": "2024-06-20"})
    assert {w["ieee"] for w in both.warranty_list()} == {SENSOR, SECOND}
    both.save(force=True)
    tracker = make_tracker(["zigbee2mqtt"])
    tracker.load()
    assert {w["ieee"] for w in tracker.warranty_list()} == {SENSOR}
    snapshot = json.loads(tracker.snapshot_json())
    assert SECOND not in json.dumps(snapshot["model_reliability"])
    assert SECOND not in json.dumps(snapshot["device_ages"])
    assert SECOND not in json.dumps(snapshot["maintenance"])


def test_no_home_assistant_entities_for_frozen_devices(make_tracker, clock):
    # Back on Zigbee2MQTT after trying ZHA: the ZHA devices must not get MQTT devices in HA.
    from discovery import Discovery

    tracker = make_tracker(["zigbee2mqtt", "zha"])
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    tracker.apply_device_list("zha", [z2m_device(SECOND, "ZHA button")])
    tracker.save(force=True)
    z2m_only = make_tracker(["zigbee2mqtt"])
    z2m_only.load()
    published = {}
    discovery = Discovery(z2m_only, "2.1.0", True)
    discovery.publish = lambda topic, payload, retain: published.__setitem__(topic, payload)
    discovery.sync()
    assert any(SENSOR in topic for topic in published)
    assert not any(SECOND in topic for topic in published)


def test_frozen_devices_not_in_prometheus_metrics(make_tracker, clock):
    import prometheus

    _offline_on_second(make_tracker, clock)
    tracker = make_tracker(["zigbee2mqtt"])
    tracker.load()
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    text = prometheus.render(prometheus.collect(tracker.prometheus_snapshot(), "1"))
    assert SENSOR in text
    assert SECOND not in text
    assert 'network="second"' not in text


def test_cleanup_topics_cover_what_the_app_published(make_tracker):
    # With ZHA the app removes, once, the sensors it made over MQTT while on Zigbee2MQTT.
    from discovery import Discovery

    tracker = make_tracker(["zigbee2mqtt"])
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    discovery = Discovery(tracker, "2.1.0", True)
    published = {}
    discovery.publish = lambda topic, payload, retain: published.__setitem__(topic, payload)
    discovery.sync()
    made = {topic for topic, payload in published.items() if payload}
    assert made
    fresh = Discovery(tracker, "2.1.0", True)
    assert made <= set(fresh.cleanup_topics())
    tracker.data["discovery_topics"] = []
    assert fresh.cleanup_topics() == []
