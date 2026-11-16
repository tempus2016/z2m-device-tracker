"""reliability.model_reliability: per-model uptime, dropouts, battery life and re-pairs."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from conftest import PLUG, SENSOR, START, send, z2m_device
from reliability import model_reliability

DAY = 86400


def iso(seconds_ago: float) -> str:
    return datetime.fromtimestamp(START - seconds_ago, tz=timezone.utc).isoformat(timespec="seconds")


def rec(ieee: str, model: str = "FK-1", vendor: str = "FakeCo", **extra) -> dict:
    base = {
        "ieee": ieee,
        "type": "EndDevice",
        "status": "active",
        "vendor": vendor,
        "model": model,
        "first_seen": iso(60 * DAY),
        "outages": [],
        "battery_info": {},
    }
    base.update(extra)
    return base


def outage(start_ago: float, hours: float) -> dict:
    return {"start": iso(start_ago), "end": iso(start_ago - hours * 3600)}


def test_groups_by_vendor_and_model():
    rows = model_reliability(
        [rec("0x1"), rec("0x2"), rec("0x3", model="OTHER"), rec("0x4", vendor="Acme")],
        [],
        START,
    )
    assert {(r["vendor"], r["model"], r["devices"]) for r in rows} == {
        ("FakeCo", "FK-1", 2),
        ("FakeCo", "OTHER", 1),
        ("Acme", "FK-1", 1),
    }
    pair = next(r for r in rows if r["devices"] == 2)
    assert pair["ieees"] == ["0x1", "0x2"]
    assert pair["uptime_30d"] == 100.0
    assert pair["dropouts_30d"] == 0
    assert pair["dropouts_per_month"] == 0.0


def test_skips_removed_coordinator_and_unknown_model():
    rows = model_reliability(
        [
            rec("0x1", status="removed"),
            rec("0x2", type="Coordinator"),
            rec("0x3", model=None),
            rec("0x4", model=None, model_id="lumi.x", vendor=None, manufacturer="LUMI"),
        ],
        [],
        START,
    )
    assert [(r["vendor"], r["model"]) for r in rows] == [("LUMI", "lumi.x")]


def test_uptime_and_dropouts_per_device_per_month():
    # Device 1: 3 outages in the window (one 30-hour outage), one older than the window.
    flaky = rec(
        "0x1",
        outages=[outage(40 * DAY, 2), outage(20 * DAY, 30), outage(10 * DAY, 1), outage(5 * DAY, 1)],
    )
    rows = model_reliability([flaky, rec("0x2")], [], START)
    (row,) = rows
    assert row["dropouts_30d"] == 3
    # 3 dropouts over two devices watched for 30 days each.
    assert row["dropouts_per_month"] == 1.5
    # Device 1 was down 32 hours of 720; device 2 never.
    assert row["uptime_30d"] == round((100 * (1 - 32 / 720) + 100) / 2, 2)


def test_new_device_has_no_dropout_rate_or_uptime():
    rows = model_reliability([rec("0x1", first_seen=iso(1800), outages=[outage(900, 0.1)])], [], START)
    assert rows[0]["uptime_30d"] is None
    assert rows[0]["dropouts_per_month"] is None
    assert rows[0]["devices"] == 1


def test_short_history_scales_to_a_month():
    rows = model_reliability([rec("0x1", first_seen=iso(10 * DAY), outages=[outage(5 * DAY, 1)])], [], START)
    assert rows[0]["dropouts_per_month"] == 3.0


def test_battery_life_needs_two_dates():
    one = rec("0x1", battery_info={"replacements": [{"date": "2026-01-01"}]})
    two = rec("0x2", battery_info={"installed": "2025-01-01", "replacements": [{"date": "2025-07-01"}]})
    three = rec(
        "0x3",
        battery_info={"replacements": [{"date": "2025-01-01"}, {"date": "2025-05-01"}, {"date": "2025-05-01"}]},
    )
    assert model_reliability([one], [], START)[0]["battery_life_days"] is None
    assert model_reliability([one], [], START)[0]["battery_samples"] == 0
    (row,) = model_reliability([one, two, three], [], START)
    # 181 days (Jan-Jul) and 120 days (Jan-May); the duplicate date adds nothing.
    assert row["battery_samples"] == 2
    assert row["battery_life_days"] == round((181 + 120) / 2)


def test_interview_failures_and_repairs():
    events = [
        {"time": iso(60 * DAY), "ieee": "0x1", "kind": "joined"},  # first pairing
        {"time": iso(9 * DAY), "ieee": "0x1", "kind": "re-paired"},
        {"time": iso(9 * DAY - 1), "ieee": "0x1", "kind": "joined"},  # same re-pair
        {"time": iso(2 * DAY), "ieee": "0x1", "kind": "joined"},
        {"time": iso(DAY), "ieee": "0x1", "kind": "interview failed"},
        {"time": iso(DAY), "ieee": "0x2", "kind": "interview failed"},
        {"time": iso(DAY), "ieee": "0x9", "kind": "joined"},
    ]
    (row,) = model_reliability([rec("0x1"), rec("0x2")], events, START)
    assert row["repairs"] == 2
    assert row["interview_failures"] == 2


def test_worst_first():
    good = rec("0x1", model="GOOD")
    bad = rec("0x2", model="BAD", outages=[outage(5 * DAY, 48)])
    flaky = rec("0x3", model="FLAKY", outages=[outage(5 * DAY, 0.01), outage(4 * DAY, 0.01), outage(3 * DAY, 0.01)])
    fresh = rec("0x4", model="FRESH", first_seen=iso(60))
    rows = model_reliability([fresh, good, flaky, bad], [], START)
    assert [r["model"] for r in rows] == ["BAD", "FLAKY", "GOOD", "FRESH"]


def test_snapshot_includes_model_reliability(tracker, clock):
    send(
        tracker,
        "bridge/devices",
        [z2m_device(SENSOR, "Kitchen sensor"), z2m_device(PLUG, "Hall plug", "Router")],
    )
    clock.advance(days=2)
    send(tracker, "Kitchen sensor/availability", "offline")
    clock.advance(hours=1)
    send(tracker, "Kitchen sensor/availability", "online")
    rows = json.loads(tracker.snapshot_json())["model_reliability"]
    assert len(rows) == 1
    row = rows[0]
    assert (row["vendor"], row["model"], row["devices"]) == ("FakeCo", "FK-1", 2)
    assert row["dropouts_30d"] == 1
    assert row["uptime_30d"] is not None and row["uptime_30d"] < 100
