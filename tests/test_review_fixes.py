"""Fixes from the November 2026 code review: restore, failed saves, bad input, alert repeats, map gate."""

from __future__ import annotations

import json
import os

import pytest
import tracker_base
from conftest import PLUG, SENSOR, send, z2m_device
from map_gate import MapGate
from reliability import _battery_intervals


def issue(kind: str, ieee: str = SENSOR, **extra) -> dict:
    return {"key": f"{kind}:{ieee}", "kind": kind, "ieee": ieee, "name": "Hall", "label": kind, "detail": "", **extra}


def test_restore_keeps_notes_defaults_and_history(tracker, make_tracker):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    note = {"id": "n1", "model": "lumi.fake", "version": "1.0", "text": "bricks the device"}
    tracker.data["firmware_notes"] = [note]
    tracker.data["report_profile_defaults"] = {"climate": "slow"}
    tracker.data["mesh_scores"] = {"zigbee2mqtt": [["2026-06-01", 80]]}
    blob = json.loads(tracker.backup())

    other = make_tracker()
    other.data["escalation"] = {"offline:x": {"step": 1}}
    assert other.restore(blob)["ok"]
    assert other.data["firmware_notes"] == [note]
    assert other.data["report_profile_defaults"] == {"climate": "slow"}
    assert "mesh_scores" in other.data
    # Alert state is about now: what was loaded stays.
    assert other.data["escalation"] == {"offline:x": {"step": 1}}


def test_failed_save_is_tried_again(tracker, monkeypatch, data_dir):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    real = os.replace

    def full_disk(*_args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tracker_base.os, "replace", full_disk)
    with pytest.raises(OSError):
        tracker.save()
    assert tracker.dirty
    assert [p.name for p in data_dir.iterdir() if p.name.endswith(".tmp")] == []
    monkeypatch.setattr(tracker_base.os, "replace", real)
    assert tracker.save() is True
    assert not tracker.dirty


def test_group_with_a_name_that_is_not_text(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    send(tracker, "bridge/groups", [{"id": 1, "friendly_name": 5, "members": [{"ieee_address": SENSOR}]}])
    assert [g["name"] for g in tracker.group_health(clock.now)] == ["Group 1"]
    tracker.evaluate_health()
    json.loads(tracker.snapshot_json())


def test_battery_dates_that_do_not_exist_are_skipped():
    record = {
        "battery_info": {"installed": "2026-02-30", "replacements": [{"date": "2026-01-01"}, {"date": "2026-01-11"}]}
    }
    assert _battery_intervals(record) == [10]


def test_threshold_alert_is_not_repeated_within_a_day(tracker, clock):
    enabled = {"battery_low", "offline"}
    tracker.plan_alerts([], enabled)  # the first check only records
    low = [issue("battery_low")]
    assert len(tracker.plan_alerts(low, enabled)["send"]) == 1
    assert tracker.plan_alerts([], enabled)["dismiss"] == ["battery_low:" + SENSOR]
    clock.advance(hours=1)
    assert tracker.plan_alerts(low, enabled)["send"] == []
    tracker.plan_alerts([], enabled)
    clock.advance(hours=25)
    assert len(tracker.plan_alerts(low, enabled)["send"]) == 1


def test_offline_alert_repeats_at_once(tracker, clock):
    enabled = {"offline"}
    tracker.plan_alerts([], enabled)
    down = [issue("offline", PLUG)]
    assert len(tracker.plan_alerts(down, enabled)["send"]) == 1
    tracker.plan_alerts([], enabled)
    clock.advance(minutes=5)
    assert len(tracker.plan_alerts(down, enabled)["send"]) == 1


def test_map_gate_waits_for_two_equal_reads_then_a_day(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("map_gate.time.monotonic", lambda: now[0])
    gate = MapGate()
    one = {"links": [{"sourceIeeeAddr": "a", "targetIeeeAddr": "b", "relationship": 1}]}
    two = {"links": [{"sourceIeeeAddr": "a", "targetIeeeAddr": "c", "relationship": 1}]}
    assert gate.due(one) is False
    assert gate.due(one) is True
    assert gate.due(one) is False
    assert gate.due(two) is False
    assert gate.due(two) is True
    now[0] += 25 * 3600
    assert gate.due(two) is True


def test_soft_changes_wait_and_real_changes_do_not(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    assert tracker.save() is True
    send(tracker, "Hall sensor", {"linkquality": 90})
    assert tracker.dirty
    assert tracker.save() is False  # a reading only: it waits
    clock.advance(seconds=tracker_base.SOFT_SAVE_SECONDS + 1)
    assert tracker.save() is True
    send(tracker, "Hall sensor", {"linkquality": 91})
    tracker.set_note(SENSOR, "by the door")
    assert tracker.save() is True  # something the user did: at the next save
    send(tracker, "Hall sensor", {"linkquality": 92})
    assert tracker.save(force=True) is True  # stopping saves everything


def test_alert_check_with_nothing_new_writes_nothing(tracker):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    tracker.plan_alerts([], {"offline"})
    tracker.save(force=True)
    tracker.plan_alerts([], {"offline"})
    assert not tracker.dirty
    tracker.plan_alerts([issue("offline")], {"offline"})
    assert tracker.dirty


def test_snapshot_is_shared_until_something_changes(tracker, clock):
    import gzip

    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    tracker.snapshot_seconds = 5
    first = tracker.snapshot_json()
    assert tracker.snapshot_json() is first
    assert gzip.decompress(tracker.snapshot_json(gzipped=True)) == first
    tracker.set_note(SENSOR, "by the door")
    assert b"by the door" in tracker.snapshot_json()
    kept = tracker.snapshot_json()
    clock.advance(seconds=6)
    assert tracker.snapshot_json() is not kept


def test_stock_count_that_is_not_a_number_is_refused(tracker):
    assert tracker.set_battery_stock("CR2032", "inf")["ok"] is False
    assert tracker.set_battery_stock("CR2032", "nan")["ok"] is False
    assert tracker.set_battery_stock("CR2032", 3)["ok"] is True
