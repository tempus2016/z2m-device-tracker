"""Daily summary: what needs attention and what changed since the previous day's summary."""

from __future__ import annotations

from datetime import datetime, timezone

import daily_summary


def issue(key: str, name: str, label: str, severity: str = "warning", **extra) -> dict:
    return {"key": key, "name": name, "label": label, "severity": severity, **extra}


OFFLINE = issue("offline:0x1", "Hall plug", "Offline")
BATTERY = issue("battery_low:0x2", "Kitchen button", "Battery low")


def test_snapshot_lists_warnings_only():
    info = issue("update:0x3", "Lamp", "Firmware update", severity="info")
    assert daily_summary.snapshot([OFFLINE, BATTERY, info]) == {
        "offline:0x1": "Hall plug (offline)",
        "battery_low:0x2": "Kitchen button (battery low)",
    }


def test_snapshot_counts_a_device_behind_an_offline_router_with_the_router():
    child = issue("offline:0x9", "Shed sensor", "Offline", behind="0x1")
    assert list(daily_summary.snapshot([OFFLINE, child])) == ["offline:0x1"]


def test_nothing_to_say_sends_nothing():
    assert daily_summary.build({}, {}) is None
    assert daily_summary.build({}, None) is None
    assert daily_summary.build({}, {}, batteries="Batteries to buy: 2 × CR2032.") is None


def test_first_summary_has_no_new_or_cleared_lines():
    title, message = daily_summary.build(daily_summary.snapshot([OFFLINE]), None)
    assert title == "Zigbee daily summary: 1 need attention"
    assert message == "Needs attention (1): Hall plug (offline)"


def test_new_and_cleared_since_yesterday():
    before = daily_summary.snapshot([OFFLINE])
    title, message = daily_summary.build(daily_summary.snapshot([BATTERY]), before, batteries="Batteries: 1 × CR2032.")
    assert title == "Zigbee daily summary: 1 need attention"
    assert message.splitlines() == [
        "Needs attention (1): Kitchen button (battery low)",
        "New since yesterday (1): Kitchen button (battery low)",
        "Cleared since yesterday (1): Hall plug (offline)",
        "Batteries: 1 × CR2032.",
    ]


def test_everything_cleared_is_worth_one_message():
    title, message = daily_summary.build({}, daily_summary.snapshot([OFFLINE]))
    assert title == "Zigbee daily summary: all clear"
    assert message.splitlines() == ["Nothing needs attention.", "Cleared since yesterday (1): Hall plug (offline)"]


def test_same_problems_as_yesterday_are_listed_without_a_new_line():
    same = daily_summary.snapshot([OFFLINE])
    _, message = daily_summary.build(same, dict(same))
    assert message == "Needs attention (1): Hall plug (offline)"


def test_long_lists_are_cut():
    many = {f"offline:{n}": f"Device {n} (offline)" for n in range(12)}
    _, message = daily_summary.build(many, None)
    assert message.startswith("Needs attention (12): Device 0 (offline); ")
    assert message.endswith("Device 7 (offline)…")


def test_clean_state_survives_rubbish():
    empty = {"sent": None, "open": None}
    for bad in (None, "x", [], {"open": ["a"]}, {"open": {"k": 1}}, {"sent": 5}):
        assert daily_summary.clean_state(bad) == empty
    good = {"sent": "2026-06-01", "open": {"offline:0x1": "Hall plug (offline)"}}
    assert daily_summary.clean_state(good) == good


def at(day: int, hour: int) -> datetime:
    return datetime(2026, 6, day, hour, 5, tzinfo=timezone.utc)  # 1 June 2026 is a Monday


ON = {"daily_summary": True, "summary_hour": 9, "summary_day": 0, "weekly_summary": True}


def test_due_only_at_the_summary_hour_once_a_day():
    assert daily_summary.due(ON, at(2, 9), None) == "send"
    assert daily_summary.due(ON, at(2, 9), "2026-06-01") == "send"
    assert daily_summary.due(ON, at(2, 9), "2026-06-02") is None
    assert daily_summary.due(ON, at(2, 8), None) is None
    assert daily_summary.due({**ON, "daily_summary": False}, at(2, 9), None) is None
    assert daily_summary.due({}, at(2, 9), None) is None


def test_weekly_summary_day_skips_the_daily_one():
    assert daily_summary.due(ON, at(1, 9), None) == "skip"
    assert daily_summary.due({**ON, "weekly_summary": False}, at(1, 9), None) == "send"
    assert daily_summary.due(ON, at(1, 9), "2026-06-01") is None


def test_due_ignores_a_broken_hour_setting():
    assert daily_summary.due({**ON, "summary_hour": "x"}, at(2, 9), None) is None
