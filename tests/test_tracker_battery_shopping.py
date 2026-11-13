"""Battery shopping list: a ticked battery stays off the list until the device reports again."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from conftest import send, z2m_device

DEVICES = {f"0x00158d00000000{i:02x}": f"Fake sensor {i}" for i in range(1, 4)}
A, B, C = DEVICES


@pytest.fixture
def shop(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(ieee, name) for ieee, name in DEVICES.items()])
    for ieee in DEVICES:
        report(tracker, ieee, 15)
    clock.advance(minutes=1)
    return tracker


def report(tracker, ieee: str, battery: int) -> None:
    send(tracker, DEVICES[ieee], {"battery": battery})


def listed(tracker) -> set[str]:
    return {d["ieee"] for g in tracker.battery_shopping(60) for d in g["devices"]}


def day(clock, offset_days: int = 0) -> str:
    return (datetime.fromtimestamp(clock.time(), tz=timezone.utc) + timedelta(days=offset_days)).date().isoformat()


def test_low_devices_are_listed(shop):
    assert listed(shop) == set(DEVICES)
    assert all(shop.data["devices"][i].get("battery_reported") for i in DEVICES)


def test_ticked_device_leaves_the_list_until_it_reports_again(shop, clock):
    shop.add_battery_replacement(A, day(clock), "Zigbee Tracker", "Ticked on the Home Assistant to-do list")
    shop.add_battery_replacement(B, day(clock), "Zigbee Tracker")
    assert A not in listed(shop)
    assert B not in listed(shop)
    clock.advance(minutes=1)
    report(shop, A, 100)
    report(shop, B, 14)
    assert A not in listed(shop)
    assert B in listed(shop)  # still low after the change, so it comes back


def test_backdated_replacement_before_the_last_reading_keeps_it_listed(shop, clock):
    shop.add_battery_replacement(A, day(clock, -10), "Zigbee Tracker")
    assert A in listed(shop)


def test_detected_replacement_never_hides(shop, clock):
    shop.add_battery_replacement(A, day(clock), "Detected", "jump")
    assert A in listed(shop)


def test_battery_notes_date_counts(shop, clock):
    shop.ha_battery[A] = {"last_replaced": clock.iso()}
    assert A not in listed(shop)
    shop.ha_battery[A] = {"last_replaced": f"{day(clock, -10)}T09:00:00"}
    assert A in listed(shop)


def test_old_records_without_battery_reported(shop, clock):
    # Data files from before battery_reported fall back to the latest metrics point.
    rec = shop.data["devices"][C]
    rec.pop("battery_reported")
    yesterday = day(clock, -1)
    rec["battery_info"] = {"replacements": [{"date": yesterday, "source": "Zigbee Tracker", "note": ""}]}
    assert C in listed(shop)
    shop.add_battery_replacement(C, day(clock), "Zigbee Tracker")
    assert C not in listed(shop)
