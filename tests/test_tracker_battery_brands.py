"""Battery brands: recorded on each change, cleaned, compared by average life per battery type."""

from __future__ import annotations

import csv
import io
import json

import pytest
from conftest import send, z2m_device

DEVICES = {f"0x00158d00000000{i:02x}": f"Fake sensor {i}" for i in range(1, 5)}
A, B, C, D = DEVICES


@pytest.fixture
def net(tracker):
    send(tracker, "bridge/devices", [z2m_device(ieee, name) for ieee, name in DEVICES.items()])
    for ieee in (A, B, C):
        tracker.set_battery_info(ieee, {"type": "CR2032", "quantity": 1})
    tracker.set_battery_info(D, {"type": "AAA", "quantity": 2})
    return tracker


def add(tracker, ieee, date, brand=None, source="Zigbee Tracker"):
    return tracker.add_battery_replacement(ieee, date, source, "", brand)


def brands_of(tracker, btype):
    for group in tracker.battery_brands()["types"]:
        if group["type"] == btype:
            return {b["brand"]: b for b in group["brands"]}
    return {}


# ------------------------------------------------------------------ recording


def test_brand_is_stored_and_cleaned(net):
    entry = add(net, A, "2025-01-01", "  Pana\x00sonic  " + "x" * 60)
    assert entry["brand"] == ("Pana sonic  " + "x" * 60)[:40]
    assert len(entry["brand"]) == 40


@pytest.mark.parametrize("brand", [None, "", "   ", 5, ["Panasonic"]])
def test_no_brand_leaves_the_field_out(net, brand):
    entry = add(net, A, "2025-01-01", brand)
    assert "brand" not in entry


def test_set_brand_on_a_past_entry(net):
    add(net, A, "2025-01-01")
    assert net.set_battery_brand(A, 0, " IKEA Ladda ") is True
    assert net.data["devices"][A]["battery_info"]["replacements"][0]["brand"] == "IKEA Ladda"
    assert net.set_battery_brand(A, 0, "") is True
    assert "brand" not in net.data["devices"][A]["battery_info"]["replacements"][0]


@pytest.mark.parametrize(
    ("ieee", "index", "brand"), [(A, 1, "X"), (A, -1, "X"), (A, "0", "X"), (A, True, "X"), (A, 0, 5)]
)
def test_set_brand_rejects_bad_input(net, ieee, index, brand):
    add(net, A, "2025-01-01")
    assert net.set_battery_brand(ieee, index, brand) is False


def test_set_brand_unknown_device(net):
    assert net.set_battery_brand("0x00158d00000000ff", 0, "X") is False


# ------------------------------------------------------------------ comparison


def test_average_from_completed_intervals_only(net):
    add(net, A, "2025-01-01", "Panasonic")
    add(net, A, "2025-11-01", "Panasonic")  # 304 days
    add(net, A, "2026-05-01", "Duracell")  # 181 days for the second Panasonic
    add(net, B, "2025-02-01", "Panasonic")
    add(net, B, "2025-12-01", "Duracell")  # 303 days
    by_brand = brands_of(net, "CR2032")
    pana = by_brand["Panasonic"]
    assert pana["intervals"] == 3
    assert pana["average_days"] == round((304 + 181 + 303) / 3)
    assert (pana["shortest_days"], pana["longest_days"]) == (181, 304)
    # Duracell is still in both devices: nothing finished yet.
    duracell = by_brand["Duracell"]
    assert duracell == {
        "brand": "Duracell",
        "intervals": 0,
        "in_use": 2,
        "average_days": None,
        "shortest_days": None,
        "longest_days": None,
    }


def test_one_interval_is_not_enough(net):
    add(net, A, "2025-01-01", "Varta")
    add(net, A, "2025-06-01")
    varta = brands_of(net, "CR2032")["Varta"]
    assert varta["intervals"] == 1
    assert varta["average_days"] is None


def test_unbranded_interval_is_not_counted(net):
    add(net, A, "2025-01-01")
    add(net, A, "2025-06-01", "Varta")
    add(net, A, "2025-12-01")
    assert brands_of(net, "CR2032")["Varta"]["intervals"] == 1


def test_detected_and_ticked_are_one_change(net):
    add(net, A, "2025-01-01", "Panasonic")
    add(net, A, "2025-06-01", source="Detected")
    add(net, A, "2025-06-02", "Duracell")  # the same change, ticked a day later
    add(net, A, "2025-12-01", "Duracell")
    by_brand = brands_of(net, "CR2032")
    assert by_brand["Panasonic"]["intervals"] == 1
    assert by_brand["Duracell"]["intervals"] == 1  # 2025-06-01 -> 2025-12-01, not a 1-day interval


def test_types_are_kept_apart_and_brands_merge_without_case(net):
    add(net, A, "2025-01-01", "Panasonic")
    add(net, A, "2025-06-01", "panasonic")
    add(net, D, "2025-01-01", "Panasonic")
    add(net, D, "2025-03-01", "Panasonic")
    cr = brands_of(net, "CR2032")
    aaa = brands_of(net, "AAA")
    assert list(cr) == ["panasonic"]  # latest spelling
    assert cr["panasonic"]["intervals"] == 1
    assert aaa["Panasonic"]["intervals"] == 1


def test_brands_sorted_longest_first(net):
    for ieee, brand, days in (
        (A, "Short", ("2025-01-01", "2025-03-01", "2025-05-01")),
        (B, "Long", ("2024-01-01", "2025-01-01", "2026-01-01")),
    ):
        for when in days:
            add(net, ieee, when, brand)
    names = [b["brand"] for b in net.battery_brands()["types"][0]["brands"]]
    assert names == ["Long", "Short"]


def test_last_brand_per_type(net):
    add(net, A, "2025-01-01", "Panasonic")
    add(net, B, "2025-06-01", "Duracell")
    add(net, B, "2025-07-01")
    add(net, D, "2025-03-01", "Amazon Basics")
    assert net.battery_brands()["last_brand"] == {"CR2032": "Duracell", "AAA": "Amazon Basics"}


def test_no_brands_no_comparison(net):
    add(net, A, "2025-01-01")
    assert net.battery_brands() == {"types": [], "last_brand": {}}


def test_snapshot_carries_brands(net):
    add(net, A, "2025-01-01", "Panasonic")
    snap = json.loads(net.snapshot_json())
    assert snap["battery_brands"]["last_brand"] == {"CR2032": "Panasonic"}


# ------------------------------------------------------------------ export, backup


def test_csv_has_last_brand(net):
    add(net, A, "2025-01-01", "Panasonic")
    add(net, A, "2025-06-01")
    add(net, B, "2025-01-01", "Varta")
    rows = {r["ieee"]: r for r in csv.DictReader(io.StringIO(net.export_csv().decode("utf-8-sig")))}
    assert rows[A]["battery_brand"] == "Panasonic"
    assert rows[B]["battery_brand"] == "Varta"
    assert rows[C]["battery_brand"] == ""


def test_backup_restore_keeps_and_cleans_brands(net, make_tracker):
    add(net, A, "2025-01-01", "Panasonic")
    add(net, A, "2025-06-01", "Varta")
    backup = json.loads(net.backup())
    backup["history"]["devices"][A]["battery_info"]["replacements"][1]["brand"] = 7
    backup["history"]["devices"][B]["battery_info"]["replacements"] = [
        "junk",
        {"date": "2025-01-01", "brand": "x\x07" * 40},
    ]
    fresh = make_tracker()
    assert fresh.restore(backup)["ok"] is True
    a = fresh.data["devices"][A]["battery_info"]["replacements"]
    assert a[0]["brand"] == "Panasonic"
    assert "brand" not in a[1]
    b = fresh.data["devices"][B]["battery_info"]["replacements"]
    assert len(b) == 1 and len(b[0]["brand"]) == 40
    assert brands_of(fresh, "CR2032")["Panasonic"]["intervals"] == 1


# ------------------------------------------------------------------ saved brand list


def test_list_seeds_from_recorded_brands(net):
    net.data["devices"][A]["battery_info"]["replacements"] = [
        {"date": "2025-01-01", "brand": "Varta"},
        {"date": "2025-06-01", "brand": "panasonic"},
        {"date": "2025-09-01"},
    ]
    net.data["devices"][B]["battery_info"]["replacements"] = [{"date": "2025-01-01", "brand": "Panasonic"}]
    assert "battery_brand_list" not in net.data
    assert net.brand_list() == ["panasonic", "Varta"]
    assert net.data["battery_brand_list"] == ["panasonic", "Varta"]


def test_seeding_happens_once(net):
    assert net.brand_list() == []
    net.data["devices"][A]["battery_info"]["replacements"] = [{"date": "2025-01-01", "brand": "Varta"}]
    assert net.brand_list() == []  # a saved (empty) list is not re-seeded


def test_add_and_remove(net):
    assert net.add_brand("  Panasonic ") == {"ok": True, "brands": ["Panasonic"]}
    assert net.add_brand("Amazon Basics")["brands"] == ["Amazon Basics", "Panasonic"]
    assert net.remove_brand("panasonic") == {"ok": True, "brands": ["Amazon Basics"]}
    assert net.remove_brand("Panasonic")["ok"] is False


@pytest.mark.parametrize("brand", [None, "", "  ", 5, ["x"]])
def test_add_rejects_bad_input(net, brand):
    assert net.add_brand(brand)["ok"] is False
    assert net.brand_list() == []


def test_add_dedupes_without_case(net):
    net.add_brand("Panasonic")
    result = net.add_brand("PANASONIC")
    assert result == {"ok": True, "brands": ["Panasonic"]}


def test_add_cleans_and_caps_length(net):
    brand = net.add_brand("Ik\x07ea " + "y" * 60)["brands"][0]
    assert brand.startswith("Ik ea ") and len(brand) == 40


def test_list_is_capped(net):
    for i in range(50):
        assert net.add_brand(f"Brand {i:02d}")["ok"] is True
    result = net.add_brand("One too many")
    assert result["ok"] is False and "50" in result["error"]
    assert len(net.brand_list()) == 50
    assert net.add_brand("brand 07")["ok"] is True  # already there: fine even when full


def test_removing_a_brand_keeps_past_records(net):
    add(net, A, "2025-01-01", "Varta")
    net.remove_brand("Varta")
    assert net.data["devices"][A]["battery_info"]["replacements"][0]["brand"] == "Varta"
    assert net.brand_list() == []


def test_recording_a_new_brand_adds_it(net):
    net.add_brand("Panasonic")
    add(net, A, "2025-01-01", "Energizer")
    add(net, A, "2025-06-01", "panasonic")
    assert net.brand_list() == ["Energizer", "Panasonic"]


def test_editing_a_past_record_adds_its_brand(net):
    add(net, A, "2025-01-01")
    net.set_battery_brand(A, 0, "GP")
    assert net.brand_list() == ["GP"]


def test_recording_when_the_list_is_full_still_records(net):
    for i in range(50):
        net.add_brand(f"Brand {i:02d}")
    entry = add(net, A, "2025-01-01", "Extra")
    assert entry["brand"] == "Extra"
    assert "Extra" not in net.brand_list()


def test_snapshot_carries_the_list(net):
    net.add_brand("Varta")
    assert json.loads(net.snapshot_json())["battery_brand_list"] == ["Varta"]


def test_restore_brings_back_and_cleans_the_list(net, make_tracker):
    net.add_brand("Varta")
    backup = json.loads(net.backup())
    backup["history"]["battery_brand_list"] = ["Varta", "varta", 7, "", "Ikea\x00", *[f"B{i:02d}" for i in range(60)]]
    fresh = make_tracker()
    assert fresh.restore(backup)["ok"] is True
    brands = fresh.brand_list()
    assert len(brands) == 50
    assert "Varta" in brands and "varta" not in brands and "Ikea" in brands


def test_restore_rejects_a_list_of_the_wrong_type(net, make_tracker):
    add(net, A, "2025-01-01", "Varta")
    backup = json.loads(net.backup())
    backup["history"]["battery_brand_list"] = {"Varta": 1}
    fresh = make_tracker()
    fresh.add_brand("Old")
    assert fresh.restore(backup)["ok"] is True
    assert fresh.brand_list() == ["Varta"]  # seeded from the restored records


def test_list_survives_save_and_load(net, make_tracker):
    net.add_brand("Varta")
    net.save(force=True)
    again = make_tracker()
    again.load()
    assert again.brand_list() == ["Varta"]
