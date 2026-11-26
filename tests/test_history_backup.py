"""history.json.bak: the file the last save replaced, loaded when history.json can't be read."""

from __future__ import annotations

import json

from conftest import SENSOR, send, z2m_device


def saved_twice(tracker):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    tracker.save(force=True)
    tracker.set_note(SENSOR, "second save")
    tracker.save(force=True)


def test_each_save_keeps_the_file_it_replaces(tracker, data_dir):
    saved_twice(tracker)
    previous = json.loads((data_dir / "history.json.bak").read_text())
    current = json.loads((data_dir / "history.json").read_text())
    assert previous["devices"][SENSOR].get("note") in (None, "")
    assert current["devices"][SENSOR]["note"] == "second save"


def test_unreadable_file_falls_back_to_the_previous_save(tracker, make_tracker, data_dir):
    saved_twice(tracker)
    (data_dir / "history.json").write_text('{"devices": {"0x00', encoding="utf-8")
    fresh = make_tracker()
    assert fresh.load() == "backup"
    assert fresh.data["devices"][SENSOR]["friendly_name"] == "Hall sensor"
    assert [p.name for p in data_dir.iterdir() if ".corrupt-" in p.name]
    # The good copy goes straight back to history.json.
    assert fresh.dirty and fresh.save() is True
    assert make_tracker().load() == "saved"


def test_valid_json_of_the_wrong_shape_counts_as_unreadable(tracker, make_tracker, data_dir):
    saved_twice(tracker)
    for content in ("[]", "null", '{"devices": []}'):
        (data_dir / "history.json").write_text(content, encoding="utf-8")
        fresh = make_tracker()
        assert fresh.load() == "backup", content
        assert SENSOR in fresh.data["devices"]


def test_both_files_unreadable_starts_empty(tracker, make_tracker, data_dir):
    saved_twice(tracker)
    (data_dir / "history.json").write_text("{", encoding="utf-8")
    (data_dir / "history.json.bak").write_text("{", encoding="utf-8")
    fresh = make_tracker()
    assert fresh.load() == "corrupt"
    assert fresh.data["devices"] == {}


def test_missing_file_with_a_previous_save_uses_it(tracker, make_tracker, data_dir):
    saved_twice(tracker)
    (data_dir / "history.json").unlink()
    fresh = make_tracker()
    assert fresh.load() == "backup"
    assert not [p.name for p in data_dir.iterdir() if ".corrupt-" in p.name]


def test_odd_values_inside_a_good_file_do_not_stop_the_load(tracker, make_tracker, data_dir):
    saved_twice(tracker)
    stored = json.loads((data_dir / "history.json").read_text())
    stored["networkmap"] = []
    stored["events"] = [1, "x", {"time": "2026-06-01T12:00:00+00:00", "ieee": SENSOR, "kind": "joined"}]
    (data_dir / "history.json").write_text(json.dumps(stored), encoding="utf-8")
    fresh = make_tracker()
    assert fresh.load() == "saved"
    assert fresh.data["networkmap"] == {} and len(fresh.data["events"]) == 1
