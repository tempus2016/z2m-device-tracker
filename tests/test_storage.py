"""Stored devices and storage groups: no checks while stored, coming back by itself, groups."""

from __future__ import annotations

import json

from conftest import PLUG, SENSOR, events_of, send, z2m_device
from storage import check_group, clean_groups, clean_stored

LIGHT = "0x00158d0000700001"


def setup(tracker):
    send(
        tracker,
        "bridge/devices",
        [z2m_device(SENSOR, "Door"), z2m_device(PLUG, "Tree lights", "Router"), z2m_device(LIGHT, "Star", "Router")],
    )
    for name in ("Door", "Tree lights", "Star"):
        send(tracker, f"{name}/availability", "online")


def offline_keys(tracker) -> set[str]:
    issues, _ = tracker.evaluate_health()
    return {i["key"] for i in issues if i["kind"] == "offline"}


def test_a_stored_device_raises_no_offline_issue(tracker, clock):
    setup(tracker)
    assert tracker.set_stored([PLUG], True) == 1
    send(tracker, "Tree lights/availability", "offline")
    send(tracker, "Door/availability", "offline")
    clock.advance(hours=3)
    assert offline_keys(tracker) == {f"offline:{SENSOR}"}


def test_store_and_bring_back_log_events_and_restore_checks(tracker, clock):
    setup(tracker)
    send(tracker, "Tree lights/availability", "offline")
    clock.advance(hours=2)
    assert f"offline:{PLUG}" in offline_keys(tracker)
    tracker.set_stored([PLUG], True)
    assert f"offline:{PLUG}" not in offline_keys(tracker)
    assert tracker.data["devices"][PLUG]["stored"]["away"]
    assert tracker.set_stored([PLUG], False) == 1
    assert "stored" not in tracker.data["devices"][PLUG]
    assert f"offline:{PLUG}" in offline_keys(tracker)
    assert [e["kind"] for e in events_of(tracker) if e["kind"] in ("stored", "unstored")] == ["stored", "unstored"]


def test_storing_twice_or_unknown_devices_changes_nothing(tracker):
    setup(tracker)
    assert tracker.set_stored([PLUG, PLUG, "0x0000000000000bad"], True) == 1
    assert tracker.set_stored([PLUG], True) == 0
    assert tracker.set_stored("not a list", True) == 0


def test_comes_back_by_itself_once_online_after_being_away(tracker, clock):
    setup(tracker)
    tracker.set_stored([PLUG], True)
    # Still online when stored: a message or an online doesn't end storage.
    send(tracker, "Tree lights", {"state": "ON"})
    assert tracker.data["devices"][PLUG].get("stored")
    send(tracker, "Tree lights/availability", "offline")
    clock.advance(days=200)
    assert tracker.data["devices"][PLUG]["stored"]["away"]
    send(tracker, "Tree lights/availability", "online")
    assert "stored" not in tracker.data["devices"][PLUG]
    assert events_of(tracker, "unstored")[-1]["detail"] == "came back online"


def test_manual_mode_keeps_it_stored(make_tracker, clock):
    tracker = make_tracker(options={"storage_auto_resume": False})
    setup(tracker)
    send(tracker, "Tree lights/availability", "offline")
    tracker.set_stored([PLUG], True)
    send(tracker, "Tree lights/availability", "online")
    assert tracker.data["devices"][PLUG].get("stored")


def test_stored_router_is_not_a_router_outage(tracker, clock):
    setup(tracker)
    tracker.set_stored([PLUG], True)
    send(tracker, "Tree lights/availability", "offline")
    clock.advance(hours=2)
    issues, _ = tracker.evaluate_health()
    assert not [i for i in issues if i.get("ieee") == PLUG]


def test_groups_store_and_bring_back_all_members(tracker):
    setup(tracker)
    result = tracker.save_storage_group({"name": " Christmas  lights ", "devices": [PLUG, LIGHT, "0xnothere"]})
    assert result["ok"]
    gid = result["id"]
    [group] = tracker.storage_groups()
    assert group == {"id": gid, "name": "Christmas lights", "devices": [PLUG, LIGHT], "stored": 0, "active": 2}
    assert tracker.store_group(gid, True) == 2
    assert tracker.data["devices"][LIGHT]["stored"]["group"] == gid
    assert tracker.storage_groups()[0]["stored"] == 2
    assert tracker.store_group(gid, False) == 2
    assert tracker.storage_groups()[0]["stored"] == 0
    assert tracker.store_group("nope", True) is None


def test_group_names_are_unique_and_edits_keep_the_id(tracker):
    setup(tracker)
    gid = tracker.save_storage_group({"name": "Summer", "devices": [LIGHT]})["id"]
    assert tracker.save_storage_group({"name": "summer", "devices": []})["error"]
    assert tracker.save_storage_group({"name": "", "devices": []})["error"]
    assert tracker.save_storage_group({"id": "unknown1", "name": "X", "devices": []})["error"]
    assert tracker.save_storage_group({"id": gid, "name": "Summer garden", "devices": [LIGHT, PLUG]})["ok"]
    assert tracker.storage_groups()[0]["name"] == "Summer garden"
    assert tracker.storage_groups()[0]["devices"] == [LIGHT, PLUG]


def test_removing_a_group_leaves_its_devices_stored(tracker):
    setup(tracker)
    gid = tracker.save_storage_group({"name": "Xmas", "devices": [PLUG]})["id"]
    tracker.store_group(gid, True)
    assert tracker.remove_storage_group(gid)
    assert not tracker.remove_storage_group(gid)
    assert tracker.storage_groups() == []
    assert tracker.data["devices"][PLUG]["stored"]["group"] is None


def test_stored_mark_and_groups_survive_a_restart(make_tracker, data_dir):
    tracker = make_tracker()
    setup(tracker)
    gid = tracker.save_storage_group({"name": "Xmas", "devices": [PLUG]})["id"]
    tracker.store_group(gid, True)
    tracker.save(force=True)
    again = make_tracker()
    again.load()
    assert again.data["devices"][PLUG]["stored"]["group"] == gid
    assert again.storage_groups()[0]["stored"] == 1
    snapshot = json.loads(again.snapshot_json())
    assert snapshot["storage_groups"][0]["name"] == "Xmas"


def test_cleaning_drops_bad_saved_data():
    assert clean_stored({"since": 5}) is None
    assert clean_stored("yes") is None
    assert clean_stored({"since": "2026-01-01T00:00:00+00:00", "group": "BAD id", "away": 3}) == {
        "since": "2026-01-01T00:00:00+00:00",
        "group": None,
        "away": None,
    }
    groups = clean_groups([{"id": "a1", "name": "A", "devices": [1, "0x1"]}, {"id": "a1", "name": "B"}, "x"])
    assert groups == [{"id": "a1", "name": "A", "devices": ["0x1"]}]
    assert check_group({"name": "A", "devices": "x"}, set(), [])[1] == "devices must be a list"
