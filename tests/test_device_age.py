"""Device age: age from purchase date or first seen, firmware change tracking, the 2-year flag."""

from __future__ import annotations

import json
from datetime import date

from conftest import SENSOR, send, z2m_device
from reliability import device_ages

TODAY = date(2026, 6, 1)


def rec(**extra) -> dict:
    return {"ieee": "0x1", "friendly_name": "Hall", "status": "active", "type": "EndDevice", **extra}


def test_age_from_purchase_date_else_first_seen():
    (a,) = device_ages([rec(asset={"purchase_date": "2024-06-01"}, first_seen="2025-01-01T00:00:00+00:00")], [], TODAY)
    assert (a["since"], a["source"], a["age_days"]) == ("2024-06-01", "bought", 730)
    (b,) = device_ages([rec(first_seen="2025-06-01T08:00:00+00:00")], [], TODAY)
    assert (b["since"], b["source"], b["age_days"]) == ("2025-06-01", "first_seen", 365)
    # A purchase date in the future is a typo: first seen wins.
    (c,) = device_ages([rec(asset={"purchase_date": "2027-01-01"}, first_seen="2025-06-01T08:00:00+00:00")], [], TODAY)
    assert c["source"] == "first_seen"


def test_skips_removed_coordinator_and_undated():
    records = [
        rec(status="removed", first_seen="2020-01-01T00:00:00+00:00"),
        rec(type="Coordinator", first_seen="2020-01-01T00:00:00+00:00"),
        rec(),
    ]
    assert device_ages(records, [], TODAY) == []


def test_oldest_first_and_stale_flag():
    old = rec(ieee="0xa", friendly_name="Old", first_seen="2023-01-01T00:00:00+00:00")
    updated = rec(
        ieee="0xb",
        friendly_name="Updated",
        first_seen="2022-01-01T00:00:00+00:00",
        firmware_changed="2026-05-30T10:00:00+00:00",
    )
    young = rec(ieee="0xc", friendly_name="Young", first_seen="2025-01-01T00:00:00+00:00")
    out = device_ages([old, updated, young], [], TODAY)
    assert [x["name"] for x in out] == ["Updated", "Old", "Young"]
    assert [x["stale"] for x in out] == [False, True, False]


def test_firmware_change_from_event_log():
    events = [
        {"kind": "firmware updated", "ieee": "0x1", "time": "2023-01-01T00:00:00+00:00"},
        {"kind": "firmware updated", "ieee": "0x1", "time": "2024-01-01T00:00:00+00:00"},
        {"kind": "renamed", "ieee": "0x1", "time": "2026-01-01T00:00:00+00:00"},
    ]
    (a,) = device_ages([rec(first_seen="2022-01-01T00:00:00+00:00")], events, TODAY)
    assert a["firmware_changed"] == "2024-01-01T00:00:00+00:00"
    assert a["stale"] is True  # 2024-01-01 is more than 2 years before 2026-06-01


def test_tracker_records_firmware_change(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall", software_build_id="1.0")])
    assert "firmware_changed" not in tracker.data["devices"][SENSOR]
    clock.advance(days=1)
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall", software_build_id="1.0")])
    assert "firmware_changed" not in tracker.data["devices"][SENSOR]
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall", software_build_id="1.1")])
    assert tracker.data["devices"][SENSOR]["firmware_changed"] == clock.iso()
    (row,) = json.loads(tracker.snapshot_json())["device_ages"]
    assert row["firmware"] == "1.1" and row["firmware_changed"] == clock.iso()
