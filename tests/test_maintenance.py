"""Maintenance tasks: input checks, due dates, mark done, the maintenance_due check, backup/restore."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from datetime import date
from pathlib import Path

import pytest
from conftest import SENSOR, events_of, send, z2m_device
from health import ALERT_KINDS, CHECKS, DEFAULT_NOTIFY
from tracker import MAX_TASKS, add_interval, clean_task

TODAY = date(2026, 6, 1)  # conftest START


@pytest.fixture
def dev(tracker):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall smoke")])
    return tracker


def task(**extra) -> dict:
    return {"title": "Test smoke alarm", "every": 1, "unit": "months", **extra}


def overdue_issues(tracker) -> list[dict]:
    return [i for i in tracker.evaluate_health()[0] if i["kind"] == "maintenance_due"]


# ------------------------------------------------------------------ intervals


def test_add_interval():
    assert add_interval(date(2026, 1, 31), 1, "months") == date(2026, 2, 28)
    assert add_interval(date(2024, 2, 29), 1, "years") == date(2025, 2, 28)
    assert add_interval(date(2026, 11, 15), 2, "months") == date(2027, 1, 15)
    assert add_interval(date(2026, 12, 31), 12, "months") == date(2027, 12, 31)
    assert add_interval(date(2026, 6, 1), 2, "weeks") == date(2026, 6, 15)
    assert add_interval(date(2026, 6, 1), 10, "days") == date(2026, 6, 11)


# ------------------------------------------------------------------ input checks


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "x",
        task(title=""),
        task(title=42),
        task(unit="fortnights"),
        task(every=0),
        task(every=True),
        task(every="3"),
        task(every=1.5),
        task(every=3651, unit="days"),
        task(every=522, unit="weeks"),
        task(every=121, unit="months"),
        task(every=11, unit="years"),
        task(last_done="2026-13-01"),
        task(last_done="1/6/2026"),
        task(last_done="2026-06-02"),
    ],
)
def test_bad_input_is_refused(raw):
    found, error = clean_task(raw, TODAY)
    assert found is None and error


def test_input_is_cleaned():
    found, error = clean_task(
        task(title="  Test\x00 alarm" + "x" * 200, note="n" * 900, every=3.0, id="nothex!!"), TODAY
    )
    assert error is None
    assert found["title"].startswith("Test  alarm") and len(found["title"]) == 80
    assert len(found["note"]) == 500
    assert found["every"] == 3
    assert found["id"] != "nothex!!" and len(found["id"]) == 8
    assert found["created"] == "2026-06-01" and found["last_done"] == ""


def test_add_edit_remove(dev):
    result = dev.set_maintenance_task(SENSOR, task())
    assert result["ok"]
    tid = result["task"]["id"]
    assert dev.data["devices"][SENSOR]["maintenance"][0]["title"] == "Test smoke alarm"
    # Editing keeps the added date and last done.
    dev.maintenance_done(SENSOR, tid)
    edited = dev.set_maintenance_task(SENSOR, {"id": tid, "title": "Test alarm", "every": 2, "unit": "weeks"})
    assert edited["ok"] and edited["task"]["last_done"] == "2026-06-01" and edited["task"]["id"] == tid
    assert len(dev.data["devices"][SENSOR]["maintenance"]) == 1
    assert dev.remove_maintenance_task(SENSOR, tid)
    assert not dev.remove_maintenance_task(SENSOR, tid)
    assert dev.data["devices"][SENSOR]["maintenance"] == []


def test_unknown_device_and_limit(dev):
    assert dev.set_maintenance_task("0x0000000000000bad", task()) == {"ok": False, "error": "Unknown device"}
    for _ in range(MAX_TASKS):
        assert dev.set_maintenance_task(SENSOR, task())["ok"]
    assert not dev.set_maintenance_task(SENSOR, task())["ok"]
    # A new task can't take an existing id or backdate its added date.
    first = dev.data["devices"][SENSOR]["maintenance"][0]
    assert dev.remove_maintenance_task(SENSOR, first["id"])
    added = dev.set_maintenance_task(SENSOR, task(created="2000-01-01"))
    assert added["task"]["created"] == "2026-06-01"


# ------------------------------------------------------------------ due dates and done


def test_due_and_done(dev, clock):
    tid = dev.set_maintenance_task(SENSOR, task())["task"]["id"]
    (item,) = dev.maintenance_list()
    assert item["due"] == "2026-07-01" and item["days"] == 30 and item["name"] == "Hall smoke"
    clock.advance(days=35)
    assert dev.maintenance_list()[0]["days"] == -5
    done = dev.maintenance_done(SENSOR, tid)
    assert done["ok"] and done["task"]["last_done"] == "2026-07-06" and done["due"] == "2026-08-06"
    (event,) = events_of(dev, "maintenance done")
    assert event["detail"] == "Test smoke alarm" and event["ieee"] == SENSOR
    assert dev.maintenance_done(SENSOR, tid, "2026-07-01")["task"]["last_done"] == "2026-07-01"
    assert events_of(dev, "maintenance done")[-1]["detail"] == "Test smoke alarm (2026-07-01)"
    assert not dev.maintenance_done(SENSOR, tid, "2026-07-07")["ok"]
    assert not dev.maintenance_done(SENSOR, tid, "July")["ok"]
    assert not dev.maintenance_done(SENSOR, "deadbeef")["ok"]


def test_removed_device_left_out(dev):
    dev.set_maintenance_task(SENSOR, task())
    send(dev, "bridge/devices", [])
    dev.data["devices"][SENSOR]["status"] = "removed"
    assert dev.maintenance_list() == []


# ------------------------------------------------------------------ health check


def test_check_is_listed_but_does_not_notify():
    assert "maintenance_due" in {c[0] for c in CHECKS}
    assert "maintenance_due" not in ALERT_KINDS
    assert "maintenance_due" not in DEFAULT_NOTIFY


def test_overdue_issue(dev, clock):
    tid = dev.set_maintenance_task(SENSOR, task(last_done="2026-05-01"))["task"]["id"]
    assert overdue_issues(dev) == []  # due today, not overdue yet
    clock.advance(days=3)
    (issue,) = overdue_issues(dev)
    assert issue["severity"] == "info" and issue["params"] == {"titles": "Test smoke alarm", "n": 3}
    assert issue["detail"] == "Overdue: Test smoke alarm. Due 3 days ago."
    dev.maintenance_done(SENSOR, tid)
    assert overdue_issues(dev) == []


def test_overdue_respects_mute_and_disable(dev, clock):
    dev.set_maintenance_task(SENSOR, task(last_done="2026-01-01"))
    assert overdue_issues(dev)
    dev.set_muted(SENSOR, "maintenance_due", True)
    assert overdue_issues(dev) == []
    dev.set_muted(SENSOR, "maintenance_due", False)
    dev.options["disabled_checks"] = ["maintenance_due"]
    assert overdue_issues(dev) == []


def test_overdue_does_not_alert(dev, clock):
    dev.plan_alerts(dev.evaluate_health()[0], set(DEFAULT_NOTIFY))  # first check only records
    dev.set_maintenance_task(SENSOR, task(last_done="2026-01-01"))
    issues, _ = dev.evaluate_health()
    assert [i["kind"] for i in dev.plan_alerts(issues, set(DEFAULT_NOTIFY))["send"]] == []
    # Turned on in Settings, it notifies like any other check.
    sent = dev.plan_alerts(issues, set(DEFAULT_NOTIFY) | {"maintenance_due"})["send"]
    assert [i["kind"] for i in sent] == ["maintenance_due"]


# ------------------------------------------------------------------ persistence


def test_saved_and_restored(dev, make_tracker):
    dev.set_maintenance_task(SENSOR, task(note="Button on the side"))
    backup = json.loads(dev.backup())
    record = backup["history"]["devices"][SENSOR]
    record["maintenance"].append({"title": "x", "every": 99999, "unit": "days"})
    record["maintenance"].append("junk")
    other = make_tracker()
    assert other.restore(backup)["ok"]
    (kept,) = other.data["devices"][SENSOR]["maintenance"]
    assert kept["note"] == "Button on the side"
    # A record from before maintenance tasks, or with a broken list, loads with an empty one.
    backup["history"]["devices"][SENSOR]["maintenance"] = {"bad": 1}
    assert other.restore(backup)["ok"]
    assert other.data["devices"][SENSOR]["maintenance"] == []
    dev.save(force=True)
    again = make_tracker()
    again.load()
    assert again.data["devices"][SENSOR]["maintenance"][0]["title"] == "Test smoke alarm"


def test_snapshot_has_list(dev):
    dev.set_maintenance_task(SENSOR, task())
    payload = json.loads(dev.snapshot_json())
    assert payload["maintenance"][0]["due"] == "2026-07-01"


# ------------------------------------------------------------------ integration helpers


@pytest.fixture(scope="module")
def maint():
    """Load the integration's maintenance.py (pure Python) without importing Home Assistant."""
    root = Path(__file__).parent.parent / "custom_components" / "z2m_tracker"
    pkg = types.ModuleType("z2mt_maint")
    pkg.__path__ = [str(root)]
    sys.modules["z2mt_maint"] = pkg
    spec = importlib.util.spec_from_file_location("z2mt_maint.maintenance", root / "maintenance.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["z2mt_maint.maintenance"] = module
    spec.loader.exec_module(module)
    yield module
    for name in ("z2mt_maint", "z2mt_maint.maintenance"):
        sys.modules.pop(name, None)


def test_integration_lists(maint):
    tasks = [
        {
            "ieee": "a",
            "id": "00000001",
            "name": "Hall",
            "title": "Test",
            "due": "2026-05-25",
            "days": -7,
            "every": 1,
            "unit": "months",
            "last_done": "2026-04-25",
        },
        {
            "ieee": "a",
            "id": "00000002",
            "name": "Hall",
            "title": "Clean",
            "due": "2026-06-10",
            "days": 9,
            "every": 6,
            "unit": "months",
            "last_done": "",
        },
        {
            "ieee": "b",
            "id": "00000003",
            "name": "Door",
            "title": "Magnet",
            "due": "2027-01-01",
            "days": 214,
            "every": 1,
            "unit": "years",
            "last_done": "2026-05-30",
        },
        {
            "ieee": "b",
            "id": "bad",
            "name": "Door",
            "title": "Broken",
            "due": "nope",
            "days": 0,
            "every": 1,
            "unit": "years",
        },
    ]
    data = {"maintenance": tasks}
    due = maint.due_tasks(data, TODAY)
    assert [x["uid"] for x in due] == ["a_00000001", "a_00000002"]
    assert due[0]["due"] == date(2026, 5, 25)
    done = maint.recently_done(data, TODAY)
    assert [x["uid"] for x in done] == ["b_00000003"]
    assert maint.interval_text(tasks[1]) == "every 6 months"
    assert maint.interval_text(tasks[2]) == "every year"
    assert [e["date"] for e in maint.calendar_items(data)] == [
        date(2026, 4, 25),
        date(2026, 5, 25),
        date(2026, 5, 30),
        date(2026, 6, 10),
        date(2027, 1, 1),
    ]


def test_replace_moves_tasks_with_notes(tracker):
    old, new = SENSOR, "0x00158d0000700001"
    send(tracker, "bridge/devices", [z2m_device(old, "Leak"), z2m_device(new, "New leak")])
    tracker.set_maintenance_task(old, {"title": "Test with water", "every": 6, "unit": "months"})
    tracker.set_maintenance_task(new, {"title": "test WITH water", "every": 1, "unit": "years"})
    tracker.set_maintenance_task(old, {"title": "Clean contacts", "every": 1, "unit": "years"})

    assert "maintenance" not in tracker.carry_over(old, new, notes=False, battery=False)
    assert len(tracker.data["devices"][old]["maintenance"]) == 2

    assert "maintenance" in tracker.carry_over(old, new, notes=True, battery=False)
    moved = tracker.data["devices"][new]["maintenance"]
    # Same title (any case) isn't doubled; the other task moves with a fresh id.
    assert sorted(t["title"] for t in moved) == ["Clean contacts", "test WITH water"]
    assert len({t["id"] for t in moved}) == 2
    assert tracker.data["devices"][old]["maintenance"] == []
