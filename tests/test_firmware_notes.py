"""Firmware notes: your notes against a model and firmware version, and what they change."""

from __future__ import annotations

import firmware_notes as fn
from conftest import coordinator, send, z2m_device
from ota import OtaManager

KITCHEN = "0x00124b0000e00001"
HALL = "0x00124b0000e00002"
LOUNGE = "0x00124b0000e00003"
NOW = "2026-11-15T10:00:00+00:00"


def rec(ieee, model="Sim Plug", build=None, installed=None, latest=None, state=None, **extra):
    record = {"ieee": ieee, "status": "active", "type": "Router", "model": model, "software_build_id": build}
    if installed is not None or state:
        record["update"] = {"state": state or "idle", "installed_version": installed, "latest_version": latest}
    record.update(extra)
    return record


def devices():
    return {
        KITCHEN: rec(KITCHEN, build="2.1.4", installed=33, latest=33, state="idle"),
        HALL: rec(HALL, build="2.1.3", installed=32, latest=33, state="available"),
        LOUNGE: rec(LOUNGE, model="Sim Bulb", build="1.0.9", installed=9, latest=10, state="available"),
    }


def note(**extra):
    notes: list = []
    saved, error = fn.save_note(
        notes, {"model": "sim plug", "version": "2.1.4", "text": "Drops off the mesh. Stay on 2.1.3.", **extra}, NOW
    )
    assert error is None
    return notes, saved


def test_save_validates_and_edits():
    notes: list = []
    assert fn.save_note(notes, {"model": "", "version": "1", "text": "x"}, NOW) == (None, "Choose a model")
    assert fn.save_note(notes, {"model": "M", "version": " ", "text": "x"}, NOW) == (None, "Enter a firmware version")
    assert fn.save_note(notes, {"model": "M", "version": "1", "text": ""}, NOW) == (None, "Write the note")
    assert fn.save_note(notes, "nope", NOW) == (None, "Invalid note")
    saved, _ = fn.save_note(notes, {"model": " M\n", "version": "1", "text": "a" * 900}, NOW)
    assert saved["model"] == "M" and len(saved["text"]) == fn.MAX_TEXT and saved["added"] == NOW
    edited, _ = fn.save_note(notes, {"id": saved["id"], "model": "M", "version": "2", "text": "b"}, "later")
    assert edited is saved and saved["version"] == "2" and saved["updated"] == "later" and len(notes) == 1
    assert fn.save_note(notes, {"id": "missing", "model": "M", "version": "2", "text": "b"}, NOW) == (
        None,
        "No such note",
    )
    assert fn.delete_note(notes, saved["id"]) and notes == []
    assert not fn.delete_note(notes, saved["id"])


def test_runs_and_offered_through_the_file_version_alias():
    notes, saved = note()
    found = fn.matches(notes, devices())
    assert found == {KITCHEN: {"runs": [saved["id"]], "offered": []}, HALL: {"runs": [], "offered": [saved["id"]]}}


def test_offered_needs_the_alias_unless_the_note_names_the_file_version():
    notes, _ = note()
    only_hall = {HALL: devices()[HALL]}
    assert fn.matches(notes, only_hall) == {}
    notes, saved = note(version="33")
    assert fn.matches(notes, only_hall) == {HALL: {"runs": [], "offered": [saved["id"]]}}


def test_ota_block_only_for_the_offered_update():
    notes, _ = note()
    devs = devices()
    assert fn.ota_block(notes, devs[HALL], devs) == "You noted problems with 2.1.4"
    assert fn.ota_block(notes, devs[LOUNGE], devs) is None
    assert fn.ota_block(notes, devs[KITCHEN], devs) is None
    assert fn.ota_block([], devs[HALL], devs) is None


def test_note_issue_for_devices_on_the_noted_version():
    notes, saved = note()
    devs = devices()
    [issue] = fn.note_issues(notes, devs, {}, 0)
    assert issue["key"] == f"firmware_note:{KITCHEN}" and issue["severity"] == "info"
    assert issue["detail"] == "Your note on 2.1.4: “Drops off the mesh. Stay on 2.1.3.”"
    assert issue["params"]["note"] == saved["id"]
    assert fn.note_issues(notes, devs, {"disabled_checks": ["firmware_note"]}, 0) == []
    devs[KITCHEN]["muted"] = {"firmware_note": None}
    assert fn.note_issues(notes, devs, {}, 0) == []


def test_clean_notes_handles_old_and_bad_data():
    assert fn.clean_notes(None) == []
    assert fn.clean_notes([{"id": "a"}, "x", {"id": "b", "model": "M", "version": "1", "text": "t"}]) == [
        {"id": "b", "model": "M", "version": "1", "text": "t"}
    ]


# ---------------------------------------------------------------- tracker and OTA queue


def build(tracker):
    plug = {"model": "Sim Plug", "vendor": "Sim", "description": "Plug", "exposes": []}
    send(
        tracker,
        "bridge/devices",
        [
            coordinator(),
            z2m_device(KITCHEN, "Plug Kitchen", "Router", software_build_id="2.1.4", definition=plug),
            z2m_device(HALL, "Plug Hall", "Router", software_build_id="2.1.3", definition=plug),
        ],
    )
    send(tracker, "Plug Kitchen", {"update": {"state": "idle", "installed_version": 33, "latest_version": 33}})
    send(tracker, "Plug Hall", {"update": {"state": "available", "installed_version": 32, "latest_version": 33}})
    for name in ("Plug Kitchen", "Plug Hall"):
        send(tracker, f"{name}/availability", "online")


def test_tracker_notes_issue_and_update_all(make_tracker, clock):
    t = make_tracker()
    build(t)
    result = t.save_firmware_note({"model": "Sim Plug", "version": "2.1.4", "text": "Drops off."})
    assert result["ok"]
    issues, _ = t.evaluate_health()
    assert [i["ieee"] for i in issues if i["kind"] == "firmware_note"] == [KITCHEN]
    assert t.firmware_note_block(t.data["devices"][HALL]) == "You noted problems with 2.1.4"

    ota = OtaManager(t, {})
    queued = ota.enqueue_all()
    assert queued["queued"] == []
    assert queued["skipped"] == [{"ieee": HALL, "reason": "You noted problems with 2.1.4"}]
    assert ota.snapshot()["note_blocked"] == {HALL: "You noted problems with 2.1.4"}
    # "Update anyway" for that one device still works.
    assert ota.enqueue([HALL], force=True)["queued"] == [HALL]

    assert t.delete_firmware_note(result["note"]["id"])
    issues, _ = t.evaluate_health()
    assert not [i for i in issues if i["kind"] == "firmware_note"]
    assert t.firmware_note_block(t.data["devices"][HALL]) is None


def test_notes_survive_a_restart(make_tracker, clock):
    t = make_tracker()
    build(t)
    t.save_firmware_note({"model": "Sim Plug", "version": "2.1.4", "text": "Drops off."})
    assert t.save()
    again = make_tracker()
    assert again.load() == "saved"
    assert [n["version"] for n in again.data["firmware_notes"]] == ["2.1.4"]
