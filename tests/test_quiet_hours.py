"""Quiet hours: alerts held overnight, critical devices sent at once, one digest when quiet hours end."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from conftest import PLUG, SENSOR, send, z2m_device

ENABLED = {"battery_low", "offline"}
OPTIONS = {"quiet_hours": True, "quiet_start": "23:00", "quiet_end": "07:00", "offline_alert_minutes": 0}


def at(clock, hhmm: str, day: int = 1) -> None:
    """Move the fake clock to a UTC wall time on June `day` (the tests run in UTC)."""
    hour, minute = map(int, hhmm.split(":"))
    clock.now = datetime(2026, 6, day, hour, minute, tzinfo=timezone.utc).timestamp()


def check(tracker) -> dict:
    issues, _ = tracker.evaluate_health()
    return tracker.plan_alerts(issues, ENABLED)


@pytest.fixture
def night(make_tracker, clock):
    tracker = make_tracker(options=dict(OPTIONS))
    at(clock, "22:00")
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Kitchen button"), z2m_device(PLUG, "Hall plug", "Router")])
    send(tracker, "Kitchen button/availability", "online")
    send(tracker, "Hall plug/availability", "online")
    send(tracker, "Kitchen button", {"battery": 80})
    assert check(tracker)["first"]
    return tracker


@pytest.mark.parametrize(
    ("hhmm", "quiet"),
    [("22:59", False), ("23:00", True), ("00:00", True), ("03:30", True), ("06:59", True), ("07:00", False)],
)
def test_window_crosses_midnight(make_tracker, clock, hhmm, quiet):
    tracker = make_tracker(options=dict(OPTIONS))
    at(clock, hhmm)
    assert tracker.in_quiet_hours() is quiet


def test_window_off_same_day_and_bad_values(make_tracker, clock):
    at(clock, "13:30")
    assert make_tracker(options={**OPTIONS, "quiet_start": "13:00", "quiet_end": "14:00"}).in_quiet_hours()
    assert not make_tracker(options={**OPTIONS, "quiet_start": "14:00", "quiet_end": "15:00"}).in_quiet_hours()
    assert not make_tracker(options={**OPTIONS, "quiet_hours": False, "quiet_start": "13:00"}).in_quiet_hours()
    assert not make_tracker(options={**OPTIONS, "quiet_start": "13:00", "quiet_end": "13:00"}).in_quiet_hours()
    assert not make_tracker(options={**OPTIONS, "quiet_start": "25:00"}).in_quiet_hours()


def test_outside_quiet_hours_alerts_go_out(night, clock):
    at(clock, "22:30")
    send(night, "Kitchen button", {"battery": 5})
    plan = check(night)
    assert [i["key"] for i in plan["send"]] == [f"battery_low:{SENSOR}"]
    assert plan["digest"] is None


def test_night_is_held_then_one_digest(night, clock):
    at(clock, "23:30")
    send(night, "Kitchen button", {"battery": 9})
    assert check(night)["send"] == []
    # The plug drops out after midnight and comes back: it is resolved, never alerted.
    at(clock, "00:10", day=2)
    send(night, "Hall plug/availability", "offline")
    assert check(night)["send"] == []
    at(clock, "00:31", day=2)
    send(night, "Hall plug/availability", "online")
    plan = check(night)
    assert plan["send"] == [] and plan["digest"] is None
    at(clock, "06:59", day=2)
    assert check(night)["digest"] is None

    at(clock, "07:00", day=2)
    plan = check(night)
    assert plan["send"] == []
    title, body = plan["digest"]
    assert title == "Zigbee overnight: 1 issue, 1 resolved"
    assert "Still open:\n- Kitchen button - Low battery (since 23:30): Battery at 9%." in body
    assert "Resolved:\n- Hall plug - Offline (00:10-00:31)" in body
    assert "Held alerts" not in body
    # Still-open issues now count as alerted: nothing is sent again one by one.
    at(clock, "07:01", day=2)
    plan = check(night)
    assert plan["send"] == [] and plan["digest"] is None
    assert night.data["quiet"] is None


def test_issue_that_clears_and_returns_is_open(night, clock):
    at(clock, "23:10")
    send(night, "Hall plug/availability", "offline")
    check(night)
    at(clock, "23:20")
    send(night, "Hall plug/availability", "online")
    check(night)
    at(clock, "02:00", day=2)
    send(night, "Hall plug/availability", "offline")
    check(night)
    at(clock, "07:05", day=2)
    title, body = check(night)["digest"]
    assert title == "Zigbee overnight: 1 issue, 0 resolved"
    assert "Hall plug - Offline (since 23:10)" in body


def test_critical_device_alerts_straight_away(night, clock):
    night.set_critical(PLUG, True)
    at(clock, "02:00", day=2)
    send(night, "Hall plug/availability", "offline")
    send(night, "Kitchen button", {"battery": 5})
    check(night)
    # Critical devices keep their own offline delay (5 min by default).
    at(clock, "02:06", day=2)
    plan = check(night)
    assert [i["key"] for i in plan["send"]] == [f"offline:{PLUG}"]
    assert plan["send"][0]["critical"] is True
    # It was sent, so the next check doesn't repeat it and the digest doesn't list it.
    at(clock, "02:07", day=2)
    assert check(night)["send"] == []
    at(clock, "07:00", day=2)
    title, body = check(night)["digest"]
    assert title == "Zigbee overnight: 1 issue, 0 resolved"
    assert "Hall plug" not in body


def test_held_events_are_in_the_digest(night, clock):
    at(clock, "22:59")
    assert not night.hold_event("Zigbee device joined", "X joined. Check it.")
    at(clock, "03:12", day=2)
    assert night.hold_event("Zigbee device left", "Back door left the Main network.")
    at(clock, "07:00", day=2)
    title, body = check(night)["digest"]
    assert title == "Zigbee overnight: 0 issues, 0 resolved"
    assert body == "Held alerts:\n- 03:12 Zigbee device left: Back door left the Main network."


def test_quiet_night_sends_no_digest(night, clock):
    at(clock, "23:30")
    check(night)
    at(clock, "07:00", day=2)
    plan = check(night)
    assert plan["digest"] is None and plan["send"] == []
    assert night.data["quiet"] is None


def test_held_alerts_survive_a_restart(night, make_tracker, clock):
    at(clock, "23:30")
    send(night, "Kitchen button", {"battery": 9})
    check(night)
    night.hold_event("Zigbee device joined", "New sensor joined the Main network.")
    night.save()

    restarted = make_tracker(options=dict(OPTIONS))
    restarted.load()
    at(clock, "07:02", day=2)
    title, body = check(restarted)["digest"]
    assert title == "Zigbee overnight: 1 issue, 0 resolved"
    assert "Kitchen button - Low battery (since 23:30)" in body
    assert "Zigbee device joined" in body


def test_alerts_off_drops_held_state(night, clock):
    at(clock, "23:30")
    send(night, "Kitchen button", {"battery": 9})
    check(night)
    issues, _ = night.evaluate_health()
    night.plan_alerts(issues, set())
    at(clock, "07:00", day=2)
    assert check(night)["digest"] is None


def test_long_lists_are_cut(night, clock):
    at(clock, "23:30")
    for n in range(14):
        night.hold_event("Zigbee device joined", f"Sensor {n} joined.")
    at(clock, "07:00", day=2)
    _, body = check(night)["digest"]
    lines = body.splitlines()
    assert lines[0] == "Held alerts:" and len(lines) == 12 and lines[-1] == "- and 4 more"
