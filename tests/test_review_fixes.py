"""Fixes from the November 2026 code review: restore, failed saves, bad input, alert repeats, map gate."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest
import tracker_base
from conftest import PLUG, SENSOR, START, send, z2m_device
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


def test_deferred_health_runs_once_before_the_issues_are_read(tracker, monkeypatch):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    runs = []
    real = tracker.evaluate_health
    monkeypatch.setattr(tracker, "evaluate_health", lambda: runs.append(1) or real())
    tracker.defer_health = True
    for kind in ("offline", "battery_low", "weak_signal"):
        tracker.set_muted(SENSOR, kind, True)
    assert runs == []
    tracker.snapshot_json()
    assert runs == [1]
    tracker.flush_health()
    assert runs == [1]


def test_snapshot_parts_make_one_document(tracker):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor"), z2m_device(PLUG, "Hall plug", "Router")])
    data = json.loads(tracker.snapshot_json())
    assert [d["ieee"] for d in data["devices"]] == [SENSOR, PLUG]
    assert data["version"] == tracker.version
    for key in ("events", "issues", "battery_shopping", "model_reliability", "groups", "options", "alerts_paused"):
        assert key in data


# ---------------------------------------------------------------- the lower-priority review fixes


def _garage(now: float, **critical) -> tuple[dict, list[dict]]:
    """Three mains devices in one area that dropped together and a battery device still online."""
    import health

    when = datetime.fromtimestamp(now - 400, tz=timezone.utc).isoformat(timespec="seconds")
    devices = {}
    for n, ieee in enumerate(("0xa1", "0xa2", "0xa3")):
        since = datetime.fromtimestamp(now - 400 + n, tz=timezone.utc).isoformat(timespec="seconds")
        devices[ieee] = {
            "ieee": ieee,
            "type": "Router",
            "status": "active",
            "area": "Garage",
            "power_source": "Mains (single phase)",
            "friendly_name": f"Plug {n}",
            "availability": "offline",
            "availability_since": since,
        }
    devices["0xb1"] = {
        "ieee": "0xb1",
        "type": "EndDevice",
        "status": "active",
        "area": "Garage",
        "power_source": "Battery",
        "availability": "online",
        "availability_since": when,
    }
    assert health.is_mains(devices["0xa1"]) and health.is_battery(devices["0xb1"])
    return devices, []


def test_power_loss_never_holds_a_critical_alert_back():
    import health

    now = START
    devices, issues = _garage(now)
    # Only the critical device's shorter delay has passed: the others have no issue yet.
    issues.append({**issue("offline", "0xa3"), "critical": True})
    health.group_power_loss(issues, devices, {}, now)
    assert "behind" not in issues[0]
    assert issues[0]["power_area"] == "Garage"
    assert issues[0]["notify_title"] == "Critical: Power loss in Garage?"
    # Later the others alert too: they fold in behind the critical one, which still leads.
    issues += [issue("offline", "0xa1"), issue("offline", "0xa2")]
    for one in issues:
        one.pop("behind", None)
    health.group_power_loss(issues, devices, {}, now)
    assert [i.get("behind") for i in issues] == [None, "0xa3", "0xa3"]
    assert sorted(issues[0]["power_devices"]) == ["0xa1", "0xa2"]


def test_power_loss_lead_must_have_an_issue():
    import health

    devices, issues = _garage(START)
    issues.append(issue("offline", "0xa2"))  # 0xa1 dropped first but has no issue yet
    health.group_power_loss(issues, devices, {}, START)
    assert "behind" not in issues[0] and issues[0]["power_area"] == "Garage"


def test_request_keeps_waiting_while_asked_to(tracker):
    import threading

    sent = []
    tracker.publish = lambda topic, payload: sent.append(payload)
    asked = []

    def keep_waiting():
        asked.append(1)
        if len(asked) == 1:
            # The answer arrives during the extra wait, for the same transaction.
            waiter = tracker._pending[sent[0]["transaction"]]
            waiter["result"] = {"status": "ok"}
            threading.Timer(0.01, waiter["event"].set).start()
            return 1.0
        return None

    assert tracker.request("zigbee2mqtt", "device/ota_update/update", {"id": "x"}, 0.01, keep_waiting) == {
        "status": "ok"
    }
    assert asked == [1]
    assert tracker.request("zigbee2mqtt", "device/ota_update/update", {"id": "x"}, 0.01, lambda: None) is None
    assert tracker.request("zigbee2mqtt", "device/ota_update/update", {"id": "x"}, 0.01) is None


def test_login_throttle_counts_the_visitor_behind_a_proxy():
    from standalone import LOGIN_TRIES, LoginThrottle, throttle_keys

    assert throttle_keys("8.8.4.4", None) == [("8.8.4.4", LOGIN_TRIES)]
    # A caller on a public address can't choose what it is counted as.
    assert throttle_keys("8.8.4.4", "10.0.0.1") == [("8.8.4.4", LOGIN_TRIES)]
    # The proxy's own entry is the last one; what the visitor sent before it is ignored.
    assert throttle_keys("172.18.0.2", "1.1.1.1, 198.51.100.7") == [
        ("172.18.0.2>198.51.100.7", LOGIN_TRIES),
        ("172.18.0.2>*", LOGIN_TRIES * 10),
    ]
    assert throttle_keys("172.18.0.2", "not an address") == [("172.18.0.2", LOGIN_TRIES)]
    throttle = LoginThrottle(lambda: 1000.0)

    def miss(forwarded):
        for key, _tries in throttle_keys("172.18.0.2", forwarded):
            throttle.failed(key)

    def blocked(forwarded):
        return any(throttle.blocked(key, tries) for key, tries in throttle_keys("172.18.0.2", forwarded))

    for _ in range(LOGIN_TRIES):
        miss("198.51.100.7")
    assert blocked("198.51.100.7")
    assert not blocked("198.51.100.8")  # someone else behind the same proxy still gets in
    for n in range(LOGIN_TRIES * 10):
        miss(f"198.51.100.{20 + n}")  # made-up addresses: the proxy's own limit stops it
    assert blocked("198.51.100.200")


def test_alert_repeat_hours_sets_the_wait_and_zero_turns_it_off(make_tracker, clock):
    enabled = {"battery_low"}
    low = [issue("battery_low")]

    def cycle(tracker) -> int:
        sent = len(tracker.plan_alerts(low, enabled)["send"])
        tracker.plan_alerts([], enabled)
        return sent

    short = make_tracker(options={"alert_repeat_hours": 2})
    short.plan_alerts([], enabled)
    assert cycle(short) == 1
    clock.advance(hours=1)
    assert cycle(short) == 0
    clock.advance(hours=3)
    assert cycle(short) == 1

    off = make_tracker(options={"alert_repeat_hours": 0})
    off.plan_alerts([], enabled)
    assert cycle(off) == 1
    assert cycle(off) == 1
