"""Zigbee2MQTT settings history: flatten, secrets, diff, and the tracker's log and events."""

from __future__ import annotations

import copy
import json

import z2m_config
from conftest import SENSOR, events_of, send, z2m_device

BASE_CONFIG = {
    "advanced": {"channel": 15, "log_level": "info", "network_key": [1, 2, 3], "pan_id": 4660},
    "mqtt": {"server": "mqtt://core-mosquitto:1883", "user": "z2m", "password": "hunter2"},
    "frontend": {"enabled": True, "port": 8080, "auth_token": "abc"},
    "availability": {"enabled": True},
    "groups": {"1": {"friendly_name": "Lights"}},
    "devices": {SENSOR: {"friendly_name": "Hall sensor", "retain": False}},
}


def _info(config: dict) -> dict:
    return {"version": "2.6.0", "coordinator": {"type": "EmberZNet"}, "network": {"channel": 15}, "config": config}


def test_flatten_hides_secrets_and_skips_ignored():
    flat = z2m_config.flatten(BASE_CONFIG)
    assert flat["advanced.log_level"] == '"info"'
    assert flat["advanced.channel"] == "15"
    assert flat["mqtt.server"] == '"mqtt://core-mosquitto:1883"'
    for secret in ("mqtt.password", "advanced.network_key", "advanced.pan_id", "frontend.auth_token"):
        assert flat[secret].startswith("#")
    text = json.dumps(flat)
    assert "hunter2" not in text and "abc" not in text
    assert not any(k.startswith("groups.") for k in flat)
    assert f"devices.{SENSOR}.friendly_name" not in flat
    assert flat[f"devices.{SENSOR}.retain"] == "false"


def test_empty_section_filling_up_is_not_a_removal():
    old = z2m_config.flatten({"devices": {}})
    new = z2m_config.flatten({"devices": {SENSOR: {"friendly_name": "Hall", "retain": True}}})
    assert [(c["path"], c["change"]) for c in z2m_config.diff(old, new)] == [(f"devices.{SENSOR}.retain", "added")]


def test_flatten_caps_long_values():
    flat = z2m_config.flatten({"advanced": {"note": "x" * 500}})
    assert len(flat["advanced.note"]) == z2m_config.VALUE_MAX


def test_diff_and_describe():
    old = z2m_config.flatten(BASE_CONFIG)
    new_config = copy.deepcopy(BASE_CONFIG)
    new_config["advanced"]["log_level"] = "debug"
    new_config["mqtt"]["password"] = "changed"
    new_config["advanced"]["transmit_power"] = 20
    del new_config["frontend"]["port"]
    new_config["devices"][SENSOR]["retain"] = True
    changes = {c["path"]: c for c in z2m_config.diff(old, z2m_config.flatten(new_config))}
    assert set(changes) == {
        "advanced.log_level",
        "mqtt.password",
        "advanced.transmit_power",
        "frontend.port",
        f"devices.{SENSOR}.retain",
    }
    assert z2m_config.describe(changes["advanced.log_level"]) == "advanced.log_level: info → debug"
    assert changes["mqtt.password"]["old"] is None and changes["mqtt.password"]["new"] is None
    assert z2m_config.describe(changes["mqtt.password"]) == "mqtt.password changed"
    assert z2m_config.describe(changes["advanced.transmit_power"]) == "advanced.transmit_power set to 20"
    assert z2m_config.describe(changes["frontend.port"]) == "frontend.port removed (was 8080)"
    device = changes[f"devices.{SENSOR}.retain"]
    assert device["ieee"] == SENSOR and device["setting"] == "retain"
    assert z2m_config.describe(device) == "retain: false → true"


def test_first_config_is_baseline(tracker):
    send(tracker, "bridge/info", _info(BASE_CONFIG))
    assert tracker.data["z2m_config"]["zigbee2mqtt"]["advanced.channel"] == "15"
    assert tracker.data["bridges"]["zigbee2mqtt"].get("config_changes") in (None, [])
    assert events_of(tracker, "settings changed") == []


def test_changes_are_logged_with_events(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    send(tracker, "bridge/info", _info(BASE_CONFIG))
    clock.advance(minutes=10)
    changed = copy.deepcopy(BASE_CONFIG)
    changed["advanced"]["log_level"] = "debug"
    changed["devices"][SENSOR]["retain"] = True
    send(tracker, "bridge/info", _info(changed))

    log = tracker.data["bridges"]["zigbee2mqtt"]["config_changes"]
    assert [c["path"] for c in log] == ["advanced.log_level", f"devices.{SENSOR}.retain"]
    assert all(c["time"] == clock.iso() for c in log)
    events = events_of(tracker, "settings changed")
    assert [(e["ieee"], e["detail"]) for e in events] == [
        ("network", "advanced.log_level: info → debug"),
        (SENSOR, "retain: false → true"),
    ]

    # Same settings again: nothing new.
    send(tracker, "bridge/info", _info(changed))
    assert len(tracker.data["bridges"]["zigbee2mqtt"]["config_changes"]) == 2


def test_many_network_changes_make_one_event(tracker):
    send(tracker, "bridge/info", _info({"advanced": {f"opt{i}": i for i in range(6)}}))
    send(tracker, "bridge/info", _info({"advanced": {f"opt{i}": i + 1 for i in range(6)}}))
    events = events_of(tracker, "settings changed")
    assert len(events) == 1
    assert events[0]["detail"].endswith("(+3 more)")


def test_change_log_is_capped(tracker):
    send(tracker, "bridge/info", _info({"advanced": {"n": 0}}))
    for n in range(1, z2m_config.KEEP_CHANGES + 20):
        send(tracker, "bridge/info", _info({"advanced": {"n": n}}))
    assert len(tracker.data["bridges"]["zigbee2mqtt"]["config_changes"]) == z2m_config.KEEP_CHANGES


def test_info_without_config_leaves_history_alone(tracker):
    send(tracker, "bridge/info", _info(BASE_CONFIG))
    info = _info(BASE_CONFIG)
    del info["config"]
    send(tracker, "bridge/info", info)
    assert tracker.data["bridges"]["zigbee2mqtt"].get("config_changes") in (None, [])
