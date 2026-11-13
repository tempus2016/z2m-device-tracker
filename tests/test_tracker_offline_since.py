"""Offline device with no availability_since: counts from first seen offline."""

from __future__ import annotations

import json

import health
from conftest import SENSOR, send, z2m_device


def offline_kinds(tracker, clock):
    rec = tracker.data["devices"][SENSOR]
    return [i["kind"] for i in health.device_issues(rec, {}, {"offline_alert_minutes": 60}, clock.time(), [])]


def write_old_file(data_dir, since=None):
    record = {
        "ieee": SENSOR,
        "instance": "zigbee2mqtt",
        "friendly_name": "Hall sensor",
        "type": "EndDevice",
        "status": "active",
        "availability": "offline",
        "availability_since": since,
        "interview_state": "SUCCESSFUL",
    }
    (data_dir / "history.json").write_text(json.dumps({"devices": {SENSOR: record}, "events": []}))


def test_load_stamps_missing_since_and_alerts_after_threshold(make_tracker, data_dir, clock):
    write_old_file(data_dir)
    tracker = make_tracker()
    tracker.load()
    rec = tracker.data["devices"][SENSOR]
    first_seen = clock.iso()
    assert rec["availability_since"] == first_seen
    assert tracker.dirty  # persisted on next save
    clock.advance(minutes=59)
    assert "offline" not in offline_kinds(tracker, clock)
    clock.advance(minutes=2)
    assert "offline" in offline_kinds(tracker, clock)


def test_restart_keeps_recorded_time(make_tracker, data_dir, clock):
    write_old_file(data_dir)
    tracker = make_tracker()
    tracker.load()
    first_seen = clock.iso()
    tracker.save()
    clock.advance(minutes=30)
    restarted = make_tracker()
    restarted.load()
    assert restarted.data["devices"][SENSOR]["availability_since"] == first_seen
    assert not restarted.dirty
    # Retained availability message after restart must not reset it either.
    send(restarted, "Hall sensor/availability", "offline")
    assert restarted.data["devices"][SENSOR]["availability_since"] == first_seen
    clock.advance(minutes=31)
    assert "offline" in offline_kinds(restarted, clock)


def test_existing_since_untouched(make_tracker, data_dir, clock):
    write_old_file(data_dir, since="2026-05-01T00:00:00+00:00")
    tracker = make_tracker()
    tracker.load()
    assert tracker.data["devices"][SENSOR]["availability_since"] == "2026-05-01T00:00:00+00:00"
    assert not tracker.dirty


def test_repeat_offline_message_stamps_missing_since(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    rec = tracker.data["devices"][SENSOR]
    rec["availability"] = "offline"
    rec["availability_since"] = None
    tracker.dirty = False
    send(tracker, "Hall sensor/availability", "offline")
    assert rec["availability_since"] == clock.iso()
    assert tracker.dirty
    clock.advance(minutes=61)
    assert "offline" in offline_kinds(tracker, clock)


def test_coming_online_clears_offline_timing(make_tracker, data_dir, clock):
    write_old_file(data_dir)
    tracker = make_tracker()
    tracker.load()
    clock.advance(hours=2)
    assert "offline" in offline_kinds(tracker, clock)
    send(tracker, "Hall sensor/availability", "online")
    rec = tracker.data["devices"][SENSOR]
    assert rec["availability"] == "online"
    assert rec["availability_since"] == clock.iso()
    assert "offline" not in offline_kinds(tracker, clock)
    # Next drop starts a fresh count.
    clock.advance(minutes=5)
    send(tracker, "Hall sensor/availability", "offline")
    assert rec["availability_since"] == clock.iso()
    clock.advance(minutes=10)
    assert "offline" not in offline_kinds(tracker, clock)
