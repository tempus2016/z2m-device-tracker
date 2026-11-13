"""Pausing alerts: how long a pause lasts, when it ends, what survives a restart, and the count the banner shows."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from conftest import START
from health import notify_kinds

LONDON = ZoneInfo("Europe/London")


def utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def issue(key: str, kind: str) -> dict:
    return {"key": key, "kind": kind, "ieee": "0x1", "name": "Hall sensor", "severity": "warning"}


def test_not_paused_by_default(tracker):
    assert not tracker.alerts_paused()
    assert tracker.alert_pause_state() is None
    assert not tracker.resume_alerts()


def test_pause_for_minutes_ends_on_its_own(tracker, clock):
    before = tracker.revision
    state = tracker.pause_alerts(minutes=60)
    assert state["since"] == utc(START)
    assert state["until"] == utc(START + 3600)
    assert tracker.revision > before  # the UI reloads and shows the banner

    clock.advance(minutes=59)
    assert tracker.alerts_paused()
    clock.advance(minutes=2)
    revision = tracker.revision
    assert not tracker.alerts_paused()
    assert tracker.data["alerts_paused"] is None
    assert tracker.revision > revision  # and drops the banner again
    assert not tracker.resume_alerts()


def test_pause_until_resumed(tracker, clock):
    state = tracker.pause_alerts()
    assert state["until"] is None
    clock.advance(days=40)
    assert tracker.alerts_paused()
    assert tracker.resume_alerts()
    assert not tracker.alerts_paused()
    assert tracker.alert_pause_state() is None


def test_pause_until_a_time(tracker, clock):
    state = tracker.pause_alerts(until=utc(START + 7200))
    assert state["until"] == utc(START + 7200)
    # No zone means the app's local time (UTC in this tracker).
    state = tracker.pause_alerts(until="2026-06-01T15:30")
    assert state["until"] == "2026-06-01T15:30:00+00:00"


def test_pause_until_next_local_time(make_tracker, clock):
    t = make_tracker()
    t.local_tz = LONDON
    # 12:00 UTC is 13:00 in London (summer time): 08:00 is tomorrow, 15:00 is today.
    assert t.pause_alerts(at="08:00")["until"] == "2026-06-02T07:00:00+00:00"
    assert t.pause_alerts(at="15:00")["until"] == "2026-06-01T14:00:00+00:00"
    assert t.pause_alerts(at="13:00")["until"] == "2026-06-02T12:00:00+00:00"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"minutes": 0},
        {"minutes": -5},
        {"minutes": 43201},
        {"minutes": True},
        {"minutes": "60"},
        {"until": "2026-06-01T11:00:00+00:00"},
        {"until": "tomorrow"},
        {"at": "25:00"},
        {"at": "8am"},
    ],
)
def test_bad_pause_is_refused(tracker, kwargs):
    with pytest.raises(ValueError):
        tracker.pause_alerts(**kwargs)
    assert not tracker.alerts_paused()


def test_changing_a_pause_keeps_its_start(tracker, clock):
    tracker.pause_alerts(minutes=30)
    clock.advance(minutes=20)
    state = tracker.pause_alerts(minutes=240)
    assert state["since"] == utc(START)
    assert state["until"] == utc(START + 20 * 60 + 240 * 60)


def test_new_pause_after_one_ended_starts_now(tracker, clock):
    tracker.pause_alerts(minutes=30)
    clock.advance(hours=2)
    state = tracker.pause_alerts(minutes=30)
    assert state["since"] == utc(START + 7200)


def test_pause_survives_a_restart(tracker, make_tracker, clock):
    tracker.pause_alerts(minutes=90)
    tracker.save()
    fresh = make_tracker()
    fresh.load()
    assert fresh.alerts_paused()
    assert fresh.alert_pause_state()["until"] == utc(START + 5400)
    clock.advance(hours=2)
    assert not fresh.alerts_paused()


def test_pending_counts_issues_not_sent_yet(make_tracker, clock):
    t = make_tracker(options={"notify_checks": ["offline", "battery_low"], "disabled_checks": []})
    t.data["alerts_initialised"] = True
    t.data["alerted"] = ["offline:0x1"]
    t.pause_alerts(minutes=60)
    t.issues = [
        issue("offline:0x1", "offline"),  # sent before the pause
        issue("battery_low:0x2", "battery_low"),  # new
        issue("offline:0x3", "offline"),  # new
        issue("weak_signal:0x4", "weak_signal"),  # shown, but never sent
    ]
    assert t.alert_pause_state()["pending"] == 2
    t.options["alerts_enabled"] = False
    assert t.alert_pause_state()["pending"] == 0


def test_pending_is_zero_before_alerts_start(make_tracker, clock):
    t = make_tracker(options={"notify_checks": ["offline"]})
    t.pause_alerts()
    t.issues = [issue("offline:0x1", "offline")]
    assert t.alert_pause_state()["pending"] == 0


def test_snapshot_carries_the_pause(tracker, clock):
    assert json.loads(tracker.snapshot_json())["alerts_paused"] is None
    tracker.pause_alerts(minutes=30)
    assert json.loads(tracker.snapshot_json())["alerts_paused"]["until"] == utc(START + 1800)


def test_restore_keeps_the_current_pause(make_tracker, clock):
    old = make_tracker()
    old.pause_alerts()  # paused forever in the backup
    backup = json.loads(old.backup())

    t = make_tracker()
    assert t.restore(backup)["ok"]
    assert not t.alerts_paused()

    t.pause_alerts(minutes=60)
    backup["history"].pop("alerts_paused")
    assert t.restore(backup)["ok"]
    assert t.alert_pause_state()["until"] == utc(START + 3600)


def test_notify_kinds():
    assert notify_kinds({}) == set()
    assert notify_kinds({"notify_checks": ["offline", "left"], "disabled_checks": ["left"]}) == {"offline"}
    assert notify_kinds({"notify_checks": ["offline"], "alerts_enabled": False}) == set()
