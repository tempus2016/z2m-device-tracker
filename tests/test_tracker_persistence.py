"""Tracker persistence: save/load round trip, older files, migration, backup/restore and record edits."""

from __future__ import annotations

import json
import shutil

import pytest
from conftest import FIXTURES, PLUG, SENSOR, events_of, send, z2m_device
from tracker import _new_record

OLD_SENSOR = "0x00158d0000700001"


def test_new_install(tracker):
    assert tracker.load() == "new"
    assert tracker.data["devices"] == {}
    assert not tracker.save()  # nothing changed, nothing written


def test_round_trip(tracker, make_tracker, data_dir, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor"), z2m_device(PLUG, "Lamp", "Router")])
    send(tracker, "Hall sensor", {"battery": 50, "temperature": 19})
    tracker.set_note(SENSOR, "Café shelf")
    assert tracker.save()
    assert not (data_dir / "history.json.tmp").exists()
    # Non-ASCII stays readable on disk.
    assert "Café" in (data_dir / "history.json").read_text(encoding="utf-8")

    fresh = make_tracker()
    assert fresh.load() == "saved"
    assert fresh.data == tracker.data
    assert fresh.names == {"zigbee2mqtt": {"Hall sensor": SENSOR, "Lamp": PLUG}}
    assert not fresh.dirty


def test_load_older_file_fills_defaults(make_tracker, data_dir):
    shutil.copy(FIXTURES / "history_old.json", data_dir / "history.json")
    t = make_tracker()
    assert t.load() == "saved"
    rec = t.data["devices"][SENSOR]
    # Every key a current record has, with stored values kept.
    assert set(_new_record(SENSOR, None)) <= set(rec)
    assert rec["note"] == "Behind the radiator"
    assert rec["lost_bindings"] == [] and rec["bindings"] is None and rec["battery_info"] == {}
    # Missing top-level keys default; unknown ones are carried through.
    assert t.data["alerted"] == [] and t.data["networkmap"] == {}
    assert t.data["bridges"]["zigbee2mqtt"]["state"] == "online"
    # Only active devices are reachable by name.
    assert t.names == {"zigbee2mqtt": {"Hall sensor": SENSOR}}
    assert t.data["devices"][OLD_SENSOR]["status"] == "removed"


def test_older_file_list_mutes_still_work(make_tracker, data_dir, clock):
    shutil.copy(FIXTURES / "history_old.json", data_dir / "history.json")
    t = make_tracker()
    t.load()
    issues, _ = t.evaluate_health()
    # Battery is 9% but battery_low was muted forever in the old list format.
    assert [i["kind"] for i in issues if i["ieee"] == SENSOR] == []
    assert t.set_muted(SENSOR, "battery_low", False)
    assert t.data["devices"][SENSOR]["muted"] == {}
    assert [i["kind"] for i in t.issues if i["ieee"] == SENSOR] == ["battery_low"]


def test_migrate_from_ha_storage(make_tracker, config_dir):
    storage = config_dir / ".storage"
    storage.mkdir()
    shutil.copy(FIXTURES / "ha_store_history.json", storage / "z2m_tracker")
    t = make_tracker()
    assert t.load(migrate_from=str(storage / "z2m_tracker")) == "migrated"
    assert set(t.data["devices"]) == {SENSOR, OLD_SENSOR}
    assert t.dirty  # so the first save writes /data/history.json


def test_saved_file_wins_over_migration(make_tracker, data_dir, config_dir):
    (data_dir / "history.json").write_text(json.dumps({"devices": {}}))
    shutil.copy(FIXTURES / "ha_store_history.json", config_dir / "old")
    t = make_tracker()
    assert t.load(migrate_from=str(config_dir / "old")) == "saved"
    assert t.data["devices"] == {}


def test_backup_and_restore(tracker, make_tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    send(tracker, "Hall sensor", {"linkquality": 99})
    blob = json.loads(tracker.backup())
    assert blob["format"] == "z2m-tracker-backup"

    other = make_tracker()
    result = other.restore(blob)
    assert result == {"ok": True, "devices": 1, "events": 0}
    assert other.data["devices"][SENSOR]["friendly_name"] == "Hall sensor"
    assert other.names == {"zigbee2mqtt": {"Hall sensor": SENSOR}}
    assert other.metrics.series[SENSOR]["lqi"][0][1] == 99.0
    # Restore writes both files straight away.
    reloaded = make_tracker()
    assert reloaded.load() == "saved"
    assert SENSOR in reloaded.data["devices"]


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"format": "something else"}, "This isn't a Zigbee Tracker backup file"),
        ({"format": "z2m-tracker-backup", "history": {}}, "The backup has no device list"),
    ],
)
def test_restore_rejects_bad_files(tracker, payload, error):
    assert tracker.restore(payload) == {"ok": False, "error": error}


# ------------------------------------------------------------------ record edits


def test_set_asset_cleans_fields(tracker):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "S")])
    assert tracker.set_asset(SENSOR, {"shop": "  Corner shop ", "price": "", "bogus": "x", "serial": "A" * 300})
    assert tracker.data["devices"][SENSOR]["asset"] == {"shop": "Corner shop", "serial": "A" * 200}
    assert not tracker.set_asset("0xmissing", {})


def test_set_battery_info_validates(tracker):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "S")])
    tracker.set_battery_info(SENSOR, {"type": "CR2032", "quantity": "2", "installed": "2026-01-02"})
    assert tracker.data["devices"][SENSOR]["battery_info"] == {
        "type": "CR2032",
        "quantity": 2,
        "installed": "2026-01-02",
    }
    tracker.set_battery_info(SENSOR, {"type": "AA", "quantity": "lots", "installed": "yesterday"})
    assert tracker.data["devices"][SENSOR]["battery_info"] == {"type": "AA", "quantity": None, "installed": ""}


def test_battery_replacements(tracker):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "S")])
    assert tracker.add_battery_replacement(SENSOR, "not a date", "Manual") is None
    tracker.add_battery_replacement(SENSOR, "2026-03-01", "Manual", "New pack")
    tracker.add_battery_replacement(SENSOR, "2026-01-01", "Manual")
    dates = [r["date"] for r in tracker.data["devices"][SENSOR]["battery_info"]["replacements"]]
    assert dates == ["2026-01-01", "2026-03-01"]
    assert tracker.remove_battery_replacement(SENSOR, 0)
    assert not tracker.remove_battery_replacement(SENSOR, 5)
    summary = tracker.battery_summary(tracker.data["devices"][SENSOR])
    assert summary["last_replaced"] == "2026-03-01"


def test_battery_summary_sources(tracker):
    from battery_library import BatteryLibrary

    send(tracker, "bridge/devices", [z2m_device(SENSOR, "S")])
    rec = tracker.data["devices"][SENSOR]
    assert tracker.battery_summary(rec)["source"] is None

    tracker.battery_lib = BatteryLibrary(FIXTURES / "battery_library.json")
    rec["vendor"], rec["model"] = "Acme", "DS-01"
    assert tracker.battery_summary(rec)["source"] == "library"

    tracker.ha_battery[SENSOR] = {"type": "2× AAA", "last_replaced": "2026-04-01T10:00:00"}
    summary = tracker.battery_summary(rec)
    assert (summary["type"], summary["quantity"], summary["label"]) == ("AAA", 2, "2× AAA")
    assert summary["from_battery_notes"] and summary["last_replaced"] == "2026-04-01"

    tracker.set_battery_info(SENSOR, {"type": "CR2450", "quantity": 1})
    assert tracker.battery_summary(rec)["source"] == "you"


def test_mute_for_days_uses_clock(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "S")])
    tracker.set_muted(SENSOR, "battery_low", True, days=1)
    clock.advance(days=1)
    assert tracker.data["devices"][SENSOR]["muted"] == {"battery_low": clock.iso()}
    assert not tracker.set_muted(SENSOR, "Bad Kind!", True)


def test_link_unlink_and_forget(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor"), z2m_device(PLUG, "Lamp")])
    send(tracker, "bridge/devices", [z2m_device(PLUG, "Lamp")])  # SENSOR removed
    clock.advance(60)
    new = "0x00158d0000500002"
    send(tracker, "bridge/devices", [z2m_device(PLUG, "Lamp"), z2m_device(new, "Hall sensor")])

    tracker.evaluate_health()
    assert [(i["kind"], i["ieee"]) for i in tracker.issues] == [("replacement", new)]

    tracker.set_note(SENSOR, "Old note")
    assert tracker.link(SENSOR, new) == {"ok": True}
    assert tracker.data["devices"][new]["predecessors"] == [SENSOR]
    assert tracker.data["devices"][new]["note"] == "Old note"
    assert tracker.issues == []

    tracker.unlink(SENSOR, new)
    assert tracker.data["devices"][SENSOR]["successor"] is None
    assert tracker.link(SENSOR, SENSOR)["ok"] is False

    tracker.link(SENSOR, new)
    tracker.metrics.add_message(SENSOR)
    assert tracker.forget(SENSOR)
    assert SENSOR not in tracker.data["devices"]
    assert all(e["ieee"] != SENSOR for e in events_of(tracker))
    assert tracker.data["devices"][new]["predecessors"] == []
    assert SENSOR not in tracker.metrics.series
    assert not tracker.forget(SENSOR)
