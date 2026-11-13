"""Spare battery stock: setting it, taking from it once per battery change, and the shopping list against it."""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import send, z2m_device
from tracker import shopping_text

DEVICES = {f"0x00158d00000000{i:02x}": f"Fake sensor {i}" for i in range(1, 4)}
A, B, C = DEVICES


@pytest.fixture
def shop(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(ieee, name) for ieee, name in DEVICES.items()])
    for ieee in DEVICES:
        send(tracker, DEVICES[ieee], {"battery": 15})
        tracker.set_battery_info(ieee, {"type": "CR2032", "quantity": 1})
    tracker.set_battery_info(C, {"type": "AAA", "quantity": 2})
    clock.advance(minutes=1)
    return tracker


def day(clock, offset_days: int = 0) -> str:
    return (datetime.fromtimestamp(clock.time(), tz=timezone.utc) + timedelta(days=offset_days)).date().isoformat()


def stock(tracker) -> dict:
    return tracker.data.get("battery_stock") or {}


# ------------------------------------------------------------------ setting stock


def test_set_and_add(tracker):
    assert tracker.set_battery_stock("CR2032", count=2) == {"ok": True, "type": "CR2032", "count": 2}
    assert tracker.set_battery_stock("CR2032", delta=3)["count"] == 5
    assert tracker.set_battery_stock("CR2032", delta=-2)["count"] == 3
    assert stock(tracker) == {"CR2032": 3}


def test_never_below_zero(tracker):
    tracker.set_battery_stock("AAA", count=1)
    assert tracker.set_battery_stock("AAA", delta=-5)["count"] == 0
    assert tracker.set_battery_stock("AAA", count=-4)["count"] == 0


def test_type_matches_without_case(tracker):
    tracker.set_battery_stock("CR2032", count=2)
    result = tracker.set_battery_stock("cr2032", delta=1)
    assert result["type"] == "CR2032"
    assert stock(tracker) == {"CR2032": 3}


def test_new_type_takes_device_spelling(shop):
    assert shop.set_battery_stock("aaa", delta=2)["type"] == "AAA"
    assert shop.set_battery_stock("cr9999", delta=1)["type"] == "cr9999"


@pytest.mark.parametrize(
    ("btype", "kwargs"),
    [
        ("", {"count": 1}),
        ("Unknown type", {"count": 1}),
        ("CR2032", {}),
        ("CR2032", {"count": "lots"}),
        ("CR2032", {"count": 1.5}),
        ("CR2032", {"delta": True}),
    ],
)
def test_bad_input(tracker, btype, kwargs):
    assert tracker.set_battery_stock(btype, **kwargs)["ok"] is False
    assert stock(tracker) == {}


def test_stock_is_saved(tracker, make_tracker):
    tracker.set_battery_stock("CR2450", count=4)
    tracker.save()
    again = make_tracker()
    again.load()
    assert again.data["battery_stock"] == {"CR2450": 4}


# ------------------------------------------------------- taking from stock


def test_replacement_takes_device_quantity(shop, clock):
    shop.set_battery_stock("CR2032", count=5)
    shop.set_battery_stock("AAA", count=5)
    shop.add_battery_replacement(A, day(clock), "Zigbee Tracker")
    shop.add_battery_replacement(C, day(clock), "Home Assistant")
    assert stock(shop) == {"CR2032": 4, "AAA": 3}


def test_replacement_stops_at_zero(shop, clock):
    shop.set_battery_stock("AAA", count=1)
    entry = shop.add_battery_replacement(C, day(clock), "Zigbee Tracker")
    assert stock(shop)["AAA"] == 0
    assert entry["stock_used"] == 1


def test_unstocked_type_is_left_alone(shop, clock):
    shop.add_battery_replacement(A, day(clock), "Zigbee Tracker")
    assert stock(shop) == {}


def test_detected_then_ticked_takes_once(shop, clock):
    shop.set_battery_stock("CR2032", count=3)
    clock.advance(days=1)
    send(shop, DEVICES[A], {"battery": 100})  # the app sees the jump and records "Detected"
    assert stock(shop)["CR2032"] == 2
    clock.advance(days=1)
    shop.add_battery_replacement(A, day(clock), "Zigbee Tracker", "Ticked on the Home Assistant to-do list")
    assert stock(shop)["CR2032"] == 2
    sources = [r["source"] for r in shop.data["devices"][A]["battery_info"]["replacements"]]
    assert sources == ["Detected", "Zigbee Tracker"]


def test_ticked_then_detected_takes_once(shop, clock):
    shop.set_battery_stock("CR2032", count=3)
    shop.add_battery_replacement(A, day(clock), "Zigbee Tracker")
    clock.advance(days=1)
    send(shop, DEVICES[A], {"battery": 100})
    assert stock(shop)["CR2032"] == 2


def test_same_day_twice_takes_once(shop, clock):
    shop.set_battery_stock("CR2032", count=3)
    shop.add_battery_replacement(A, day(clock), "Zigbee Tracker")
    shop.add_battery_replacement(A, day(clock), "Home Assistant")
    assert stock(shop)["CR2032"] == 2


def test_later_change_takes_again(shop, clock):
    shop.set_battery_stock("CR2032", count=3)
    shop.add_battery_replacement(A, day(clock), "Zigbee Tracker")
    clock.advance(days=40)
    shop.add_battery_replacement(A, day(clock), "Zigbee Tracker")
    assert stock(shop)["CR2032"] == 1


def test_old_backfilled_change_leaves_stock(shop, clock):
    shop.set_battery_stock("CR2032", count=3)
    shop.add_battery_replacement(A, day(clock, -100), "Zigbee Tracker")
    assert stock(shop)["CR2032"] == 3


def test_removing_an_entry_puts_batteries_back(shop, clock):
    shop.set_battery_stock("AAA", count=4)
    shop.add_battery_replacement(C, day(clock), "Zigbee Tracker")
    assert stock(shop)["AAA"] == 2
    shop.remove_battery_replacement(C, 0)
    assert stock(shop)["AAA"] == 4


def test_carried_over_history_is_not_refunded(shop, clock):
    shop.set_battery_stock("CR2032", count=3)
    shop.add_battery_replacement(A, day(clock), "Zigbee Tracker")
    shop.carry_over(A, B, notes=False, battery=True)
    copied = shop.data["devices"][B]["battery_info"]["replacements"]
    assert all("stock_used" not in r for r in copied)


# ------------------------------------------------------------- shopping list


def groups(tracker) -> dict[str, dict]:
    return {g["type"]: g for g in tracker.battery_shopping(60)}


def test_shopping_without_stock(shop):
    g = groups(shop)
    assert (g["CR2032"]["count"], g["CR2032"]["stock"], g["CR2032"]["to_buy"]) == (2, None, None)
    assert g["AAA"]["count"] == 2


def test_shopping_with_stock(shop):
    shop.set_battery_stock("cr2032", count=1)
    shop.set_battery_stock("AAA", count=5)
    g = groups(shop)
    assert (g["CR2032"]["count"], g["CR2032"]["stock"], g["CR2032"]["to_buy"]) == (2, 1, 1)
    assert (g["AAA"]["count"], g["AAA"]["stock"], g["AAA"]["to_buy"]) == (2, 5, 0)


def test_snapshot_carries_stock(shop):
    import json

    shop.set_battery_stock("CR2450", count=4)
    payload = json.loads(shop.snapshot_json())
    assert payload["battery_stock"] == {"CR2450": 4}


def test_shopping_text():
    plain = [{"type": "CR2032", "count": 3, "stock": None, "to_buy": None}]
    assert shopping_text(plain) == "Batteries for the next 60 days: 3× CR2032."
    mixed = [
        {"type": "CR2032", "count": 3, "stock": 1, "to_buy": 2},
        {"type": "AAA", "count": 2, "stock": 4, "to_buy": 0},
        {"type": "CR2450", "count": 1, "stock": None, "to_buy": None},
    ]
    assert shopping_text(mixed) == (
        "Batteries for the next 60 days: 2× CR2032 to buy (1 in stock), AAA covered by stock, 1× CR2450 needed."
    )
    assert shopping_text([]) == ""


# ------------------------------------------------------ integration to-do text


@pytest.fixture(scope="module")
def batteries():
    """Load the integration's batteries.py (pure Python) without importing Home Assistant."""
    root = Path(__file__).parent.parent / "custom_components" / "z2m_tracker"
    pkg = types.ModuleType("z2mt_int")
    pkg.__path__ = [str(root)]
    sys.modules["z2mt_int"] = pkg
    for name in ("const", "batteries"):
        spec = importlib.util.spec_from_file_location(f"z2mt_int.{name}", root / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"z2mt_int.{name}"] = module
        spec.loader.exec_module(module)
    yield sys.modules["z2mt_int.batteries"]
    for name in ("z2mt_int", "z2mt_int.const", "z2mt_int.batteries"):
        sys.modules.pop(name, None)


def test_todo_text(batteries):
    devices = [
        {"ieee": A, "name": "Door", "battery": 10, "days": 5, "quantity": 1},
        {"ieee": B, "name": "Window", "battery": 12, "days": 9, "quantity": 2},
    ]
    data = {
        "battery_shopping": [
            {"type": "CR2032", "count": 3, "stock": 1, "devices": devices},
            {"type": "AAA", "count": 3, "stock": 4, "devices": devices},
            {"type": "CR2450", "count": 3, "stock": None, "devices": devices},
        ]
    }
    found = {g["type"]: g for g in batteries.shopping_groups(data, date(2026, 6, 1))}
    assert batteries.group_summary(found["CR2032"]) == "CR2032 × 2 to buy"
    assert batteries.group_summary(found["AAA"]) == "AAA × 3, in stock"
    assert batteries.group_summary(found["CR2450"]) == "CR2450 × 3"
    assert batteries.stock_line(found["CR2032"]) == "3 needed, 1 in stock. "
    assert batteries.stock_line(found["CR2450"]) == ""
    # Ticking hides a device; to-buy follows what's left.
    hidden = {g["type"]: g for g in batteries.shopping_groups(data, date(2026, 6, 1), {B})}
    assert hidden["CR2032"]["to_buy"] == 0
