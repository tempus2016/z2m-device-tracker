"""Forgetting devices: one at a time, all removed ones at once, and staying gone after a history import (#101)."""

from __future__ import annotations

import shutil

from conftest import FIXTURES, send, z2m_device

HA_CONFIG = FIXTURES / "ha_config"
PLUG = "0x00158d0000600001"
BULB = "0x00158d0000700001"
PORCH = "0x00158d0000990001"


def _imported(tracker, config_dir):
    shutil.copytree(HA_CONFIG, config_dir, dirs_exist_ok=True)
    send(tracker, "bridge/devices", [z2m_device(PLUG, "Lamp", "Router")])
    tracker.import_history()
    return [i for i, r in tracker.data["devices"].items() if r["status"] == "removed"]


def test_forget_is_remembered(tracker, config_dir):
    _imported(tracker, config_dir)
    assert tracker.forget(BULB)
    assert BULB not in tracker.data["devices"]
    assert not any(e.get("ieee") == BULB for e in tracker.data["events"])
    assert tracker.data["forgotten"] == [BULB]
    assert not tracker.forget(BULB)


def test_import_skips_forgotten(tracker, config_dir):
    removed = _imported(tracker, config_dir)
    assert BULB in removed
    tracker.forget(BULB)
    summary = tracker.import_history()
    assert summary["new_devices"] == 0
    assert BULB not in tracker.data["devices"]
    assert not any(e.get("ieee") == BULB for e in tracker.data["events"])


def test_forgotten_device_that_rejoins_comes_back(tracker, config_dir):
    _imported(tracker, config_dir)
    tracker.forget(BULB)
    send(tracker, "bridge/devices", [z2m_device(PLUG, "Lamp", "Router"), z2m_device(BULB, "Bulb", "Router")])
    assert tracker.data["devices"][BULB]["status"] == "active"
    assert BULB not in tracker.data["forgotten"]


def test_forget_removed_skips_live_devices(tracker, config_dir):
    removed = _imported(tracker, config_dir)
    result = tracker.forget_removed([*removed, PLUG, "0xdeadbeefdeadbeef"])
    assert sorted(result["forgotten"]) == sorted(removed)
    assert result["skipped"] == [PLUG, "0xdeadbeefdeadbeef"]
    assert list(tracker.data["devices"]) == [PLUG]
    assert set(tracker.data["forgotten"]) == set(removed)
    assert tracker.import_history()["new_devices"] == 0
    assert list(tracker.data["devices"]) == [PLUG]


def test_forgotten_survives_restart(make_tracker, config_dir):
    tracker = make_tracker()
    _imported(tracker, config_dir)
    tracker.forget(PORCH)
    tracker.save()
    again = make_tracker()
    again.load()
    assert again.data["forgotten"] == [PORCH]
