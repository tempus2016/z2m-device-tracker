"""Running costs: price parsing, battery prices and the yearly battery spend."""

from __future__ import annotations

import json
from datetime import date

import pytest
from conftest import PLUG, SENSOR, send, z2m_device
from costs import clean_battery_prices, parse_price, running_costs

TODAY = date(2026, 6, 1)


@pytest.mark.parametrize(
    ("text", "amount", "mark"),
    [
        ("£14.99", 14.99, "£"),
        ("14,99 €", 14.99, "€"),
        ("EUR 1.234,50", 1234.5, "EUR"),
        ("$1,234.50", 1234.5, "$"),
        ("1,234", 1234.0, ""),
        ("12", 12.0, ""),
        ("£ 12.", 12.0, "£"),
        ("1.234.567", None, ""),  # over the 100000 cap
        ("2.345.000", None, ""),
        ("", None, ""),
        ("free", None, ""),
        (None, None, ""),
        ("999999999", None, ""),
    ],
)
def test_parse_price(text, amount, mark):
    assert parse_price(text) == (amount, mark)


def test_clean_battery_prices():
    assert clean_battery_prices({"CR2032": 0.8, "AAA": "0.45", "": 1, "X": -1, "Y": True, "Z": 5000, "A" * 50: 1}) == {
        "CR2032": 0.8,
        "A" * 40: 1.0,
    }
    assert clean_battery_prices("nope") == {}


def item(**extra) -> dict:
    return {
        "model": "Sim Motion",
        "active": True,
        "battery": True,
        "type": "CR2450",
        "quantity": 1,
        "changes": [],
        **extra,
    }


def test_device_total_and_currency():
    out = running_costs(
        [item(price="£10"), item(price="£4.50", battery=False), item(price="€3"), item(price="")], {}, TODAY
    )
    assert out["devices_total"] == 17.5
    assert out["devices_priced"] == 3
    assert out["currency"] == "£"


def test_last_12_months():
    devices = [
        item(changes=["2025-05-01", "2025-07-01", "2026-03-01"], quantity=2),  # 2 changes in the window
        item(type="AAA", changes=["2026-01-01"], active=False),  # removed devices still cost money
    ]
    out = running_costs(devices, {"cr2450": 1.5}, TODAY)
    assert out["battery_last_count"] == 5
    assert out["battery_last_12m"] == 6.0
    assert out["unpriced"] == ["AAA"]


def test_next_12_months_from_life_and_prediction():
    # Life 100 days (2026-01-01 -> 2026-04-11); predicted low in 30 days: 1 + floor(335 / 100) = 4 changes.
    a = item(changes=["2026-01-01", "2026-04-11"], estimate_days=30)
    # No prediction: 365 / 100 = 3.65 a year.
    b = item(changes=["2026-01-01", "2026-04-11"])
    # Predicted beyond the year: none.
    c = item(changes=["2026-01-01", "2026-04-11"], estimate_days=400)
    # Nothing known: left out and counted.
    d = item(model="Other")
    out = running_costs([a, b, c, d], {"CR2450": 2.0}, TODAY)
    assert out["battery_next_count"] == round(4 + 3.65, 1)
    assert out["battery_next_12m"] == round((4 + 3.65) * 2, 2)
    assert out["unestimated"] == 1
    (row,) = out["models"]
    assert row == {"model": "Sim Motion", "type": "CR2450", "devices": 3, "per_year": 3.6, "cost_year": 7.3}


def test_model_life_fills_in_for_a_device_without_history():
    known = item(changes=["2026-01-01", "2026-04-11"])
    fresh = item()
    out = running_costs([known, fresh], {}, TODAY)
    assert out["battery_next_count"] == 7.3
    assert out["models"][0]["cost_year"] is None
    assert out["unpriced"] == ["CR2450"]


def test_installed_date_counts_as_first_change():
    out = running_costs([item(installed="2026-01-01", changes=["2026-07-20"])], {}, date(2026, 8, 1))
    assert out["models"][0]["per_year"] == 1.8  # 200 days


def test_changes_days_apart_are_one_change():
    # Logged 14 Nov, detected again 16 Nov: one change, not a 2-day battery life.
    dup = item(type="AA", quantity=4, changes=["2026-05-14", "2026-05-16"])
    out = running_costs([dup], {"AA": 0.7}, TODAY)
    assert out["models"] == []
    assert out["unestimated"] == 1
    assert out["battery_last_count"] == 4


def test_merged_change_keeps_the_real_life():
    out = running_costs([item(changes=["2026-01-01", "2026-04-11", "2026-04-13"])], {}, TODAY)
    assert out["models"][0]["per_year"] == 3.6  # 100 days, not the 2-day gap


# ------------------------------------------------------------------ tracker


def test_tracker_prices_and_snapshot(tracker):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall motion"), z2m_device(PLUG, "Plug", "Router")])
    tracker.set_battery_info(SENSOR, {"type": "CR2450", "quantity": 1, "installed": "2025-06-01"})
    tracker.add_battery_replacement(SENSOR, "2026-01-01", "Manual")
    tracker.set_asset(PLUG, {"price": "£14.99"})
    assert tracker.set_battery_prices({"CR2450": 1.7, "AAA": None})["prices"] == {"CR2450": 1.7}
    assert tracker.set_battery_prices("x")["ok"] is False
    snap = json.loads(tracker.snapshot_json())
    assert snap["battery_prices"] == {"CR2450": 1.7}
    costs = snap["running_costs"]
    assert costs["devices_total"] == 14.99 and costs["currency"] == "£"
    assert costs["battery_last_count"] == 1 and costs["battery_last_12m"] == 1.7
    assert costs["types"] == ["CR2450"]


def test_battery_prices_survive_backup(tracker):
    tracker.set_battery_prices({"CR2032": 0.8})
    payload = json.loads(tracker.backup())
    tracker.set_battery_prices({})
    assert tracker.restore(payload)["ok"]
    assert tracker.data["battery_prices"] == {"CR2032": 0.8}
