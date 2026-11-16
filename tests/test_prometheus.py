"""Prometheus text output: tracker snapshot, sample collection, label escaping and format."""

from __future__ import annotations

import json

import prometheus
from conftest import COORD, PLUG, SENSOR, coordinator, send, z2m_device


def _lines(text: str, family: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(family + "{") or line.startswith(family + " ")]


def _setup(tracker, clock):
    send(tracker, "bridge/state", {"state": "online"})
    send(
        tracker,
        "bridge/devices",
        [coordinator(), z2m_device(SENSOR, "Hall sensor"), z2m_device(PLUG, "Lamp", "Router")],
    )
    clock.advance(minutes=1)
    send(tracker, "Hall sensor", {"linkquality": 120, "battery": 87})
    send(tracker, "Lamp", {"linkquality": 200})
    send(tracker, "Hall sensor/availability", {"state": "online"})
    send(tracker, "Lamp/availability", {"state": "offline"})


def test_snapshot_skips_coordinator_and_reads_live_values(tracker, clock):
    _setup(tracker, clock)
    snap = tracker.prometheus_snapshot()
    by_ieee = {d["ieee"]: d for d in snap["devices"]}
    assert COORD not in by_ieee
    assert by_ieee[SENSOR]["lqi"] == 120
    assert by_ieee[SENSOR]["battery"] == 87
    assert by_ieee[SENSOR]["availability"] == "online"
    assert by_ieee[PLUG]["availability"] == "offline"
    assert snap["bridges"] == {"zigbee2mqtt": "online"}
    assert snap["alerts_paused"] is False


def test_render_device_gauges(tracker, clock):
    _setup(tracker, clock)
    text = prometheus.render(prometheus.collect(tracker.prometheus_snapshot(), "9.9.9"))
    assert 'z2m_tracker_info{version="9.9.9"} 1' in text
    assert 'z2m_tracker_network_online{network="zigbee2mqtt"} 1' in text
    assert f'z2m_tracker_device_linkquality{{ieee="{SENSOR}",name="Hall sensor",network="zigbee2mqtt"}} 120' in text
    assert f'z2m_tracker_device_battery_percent{{ieee="{SENSOR}",name="Hall sensor",network="zigbee2mqtt"}} 87' in text
    assert f'z2m_tracker_device_online{{ieee="{PLUG}",name="Lamp",network="zigbee2mqtt"}} 0' in text
    assert 'z2m_tracker_network_devices{network="zigbee2mqtt",status="active"} 2' in text
    # Each family that has samples gets exactly one HELP and one TYPE line, before its samples.
    for name, kind, _ in prometheus.FAMILIES:
        if _lines(text, name):
            assert text.count(f"# TYPE {name} {kind}\n") == 1
            assert text.index(f"# TYPE {name} ") < text.index(_lines(text, name)[0])
    assert text.endswith("\n")


def test_missing_values_are_left_out(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    text = prometheus.render(prometheus.collect(tracker.prometheus_snapshot(), "1"))
    assert _lines(text, "z2m_tracker_device_battery_percent") == []
    assert _lines(text, "z2m_tracker_device_online") == []
    assert "# TYPE z2m_tracker_device_battery_percent" not in text
    assert len(_lines(text, "z2m_tracker_device_info")) == 1


def test_removed_devices_only_count(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor"), z2m_device(PLUG, "Lamp", "Router")])
    clock.advance(minutes=5)
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    text = prometheus.render(prometheus.collect(tracker.prometheus_snapshot(), "1"))
    assert 'z2m_tracker_network_devices{network="zigbee2mqtt",status="removed"} 1' in text
    assert PLUG not in "\n".join(_lines(text, "z2m_tracker_device_info"))


def test_label_values_are_escaped():
    samples = [("z2m_tracker_device_info", {"ieee": "0x1", "name": 'Say "hi"\\\nnow'}, 1)]
    text = prometheus.render(samples)
    assert 'name="Say \\"hi\\"\\\\\\nnow"' in text


def test_non_numbers_are_dropped():
    samples = [
        ("z2m_tracker_device_linkquality", {"ieee": "a"}, None),
        ("z2m_tracker_device_linkquality", {"ieee": "b"}, "12"),
        ("z2m_tracker_device_linkquality", {"ieee": "c"}, float("nan")),
        ("z2m_tracker_device_linkquality", {"ieee": "d"}, 12.5),
        ("not_a_family", {}, 1),
    ]
    assert _lines(prometheus.render(samples), "z2m_tracker_device_linkquality") == [
        'z2m_tracker_device_linkquality{ieee="d"} 12.5'
    ]
    assert "not_a_family" not in prometheus.render(samples)


def test_issue_counts(tracker, clock):
    snapshot = {
        "devices": [],
        "bridges": {},
        "issues": [
            {"ieee": SENSOR, "kind": "offline", "severity": "error"},
            {"ieee": PLUG, "kind": "offline", "severity": "error"},
            {"ieee": PLUG, "kind": "battery_low", "severity": "warning"},
        ],
    }
    text = prometheus.render(prometheus.collect(snapshot, "1"))
    assert 'z2m_tracker_issues{check="offline",severity="error"} 2' in text
    assert 'z2m_tracker_issues{check="battery_low",severity="warning"} 1' in text


def test_sidebar_snapshot_leaves_out_secret_options(make_tracker):
    tracker = make_tracker(options={"mqtt_password": "hunter2", "metrics_token": "x" * 20, "mqtt_user": "z2m"})
    options = json.loads(tracker.snapshot_json())["options"]
    assert "mqtt_password" not in options
    assert "metrics_token" not in options
    assert options["mqtt_user"] == "z2m"
