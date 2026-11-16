"""Kept copies of coordinator_backup.json: when a copy is kept, what changed, pruning and the sidebar rows."""

from __future__ import annotations

import json
import os

from backup_history import KEEP, BackupHistory, changes, fingerprint
from conftest import START
from test_tracker_network import z2m_folder

KEY = "0123456789abcdef0123456789abcdef"


def backup(devices=("00158d0000500001",), key=KEY, pan_id="1a62", channel=15, ext="dddddddddddddddd"):
    return {
        "metadata": {"internal": {}},
        "channel": channel,
        "pan_id": pan_id,
        "extended_pan_id": ext,
        "network_key": {"key": key, "frame_counter": 1},
        "devices": [{"ieee_address": d} for d in devices],
    }


def keep(history, b, date, now, names=None, bt="zigbee2mqtt"):
    return history.consider(bt, json.dumps(b).encode(), b, date, now, names)


def test_fingerprint_holds_no_key():
    fp = fingerprint(backup())
    assert fp["devices"] == ["0x00158d0000500001"]
    assert fp["key"] and KEY not in json.dumps(fp)
    assert (fp["pan_id"], fp["ext_pan"], fp["channel"]) == ("1a62", "dddddddddddddddd", 15)


def test_changes_lists_devices_and_flags():
    a = fingerprint(backup(devices=("00158d0000500001", "00158d0000500002")))
    b = fingerprint(backup(devices=("00158d0000500002", "0x00158d0000500003"), key="f" * 32, pan_id="abcd"))
    c = changes(a, b, {"0x00158d0000500001": "Leak Garage"})
    assert c["added"] == [{"ieee": "0x00158d0000500003", "name": None}]
    assert c["removed"] == [{"ieee": "0x00158d0000500001", "name": "Leak Garage"}]
    assert (c["key_changed"], c["pan_changed"], c["channel_changed"]) == (True, True, False)
    assert changes(None, b) is None


def test_keeps_copy_only_when_it_matters(tmp_path):
    h = BackupHistory(str(tmp_path))
    assert keep(h, backup(), "2026-06-01T03:00:00+00:00", START)
    # Same content, same date: nothing new. Same content a day later (frame counter moved): still nothing.
    assert not keep(h, backup(), "2026-06-01T03:00:00+00:00", START + 600)
    assert not keep(h, backup(), "2026-06-02T03:00:00+00:00", START + 86400)
    # A device joined: kept.
    assert keep(
        h, backup(devices=("00158d0000500001", "00158d0000500002")), "2026-06-03T03:00:00+00:00", START + 2 * 86400
    )
    # Unchanged but a week newer than the last copy: kept so a fresh one exists.
    assert keep(
        h, backup(devices=("00158d0000500001", "00158d0000500002")), "2026-06-10T03:00:00+00:00", START + 9 * 86400
    )
    rows = h.rows("zigbee2mqtt")
    assert [r["devices"] for r in rows] == [2, 2, 1]
    assert rows[0]["change"] == {
        "added": [],
        "removed": [],
        "key_changed": False,
        "pan_changed": False,
        "channel_changed": False,
    }
    assert rows[1]["change"]["added"] == [{"ieee": "0x00158d0000500002", "name": None}]
    assert rows[2]["change"] is None


def test_rows_never_carry_the_key(tmp_path):
    h = BackupHistory(str(tmp_path))
    keep(h, backup(), "2026-06-01T03:00:00+00:00", START)
    keep(h, backup(key="f" * 32), "2026-06-02T03:00:00+00:00", START + 86400)
    rows = h.rows("zigbee2mqtt")
    assert rows[0]["change"]["key_changed"] is True
    text = json.dumps(rows)
    assert KEY not in text and "f" * 32 not in text and '"key"' not in text
    index = (tmp_path / "index.json").read_text()
    assert KEY not in index and "f" * 32 not in index


def test_prunes_to_keep_and_deletes_files(tmp_path):
    h = BackupHistory(str(tmp_path))
    for n in range(KEEP + 3):
        keep(h, backup(devices=[f"00158d00005000{n:02x}"]), f"2026-06-{n + 1:02d}T03:00:00+00:00", START + n * 86400)
    rows = h.rows("zigbee2mqtt")
    assert len(rows) == KEEP
    copies = [f for f in os.listdir(tmp_path) if f != "index.json"]
    assert len(copies) == KEEP
    # The oldest kept copy still says what changed before it, though that copy is gone.
    assert rows[-1]["change"]["added"]


def test_index_survives_reload_and_path_checks_id(tmp_path):
    h = BackupHistory(str(tmp_path))
    b = backup()
    keep(h, b, "2026-06-01T03:00:00+00:00", START)
    again = BackupHistory(str(tmp_path))
    row = again.rows("zigbee2mqtt")[0]
    path = again.path("zigbee2mqtt", row["id"])
    assert path and json.loads(open(path, encoding="utf-8").read()) == b
    assert again.path("zigbee2mqtt", "../index") is None
    assert again.path("other", row["id"]) is None


def test_unreadable_index_starts_again(tmp_path):
    (tmp_path / "index.json").write_text("{nope")
    h = BackupHistory(str(tmp_path))
    assert h.rows("zigbee2mqtt") == []
    assert keep(h, backup(), "2026-06-01T03:00:00+00:00", START)


def test_rows_use_current_names(tmp_path):
    h = BackupHistory(str(tmp_path))
    keep(h, backup(devices=()), "2026-06-01T03:00:00+00:00", START)
    keep(h, backup(), "2026-06-02T03:00:00+00:00", START + 86400, names={"0x00158d0000500001": "Old"})
    assert h.rows("zigbee2mqtt", {"0x00158d0000500001": "New"})[0]["change"]["added"][0]["name"] == "New"
    assert h.rows("zigbee2mqtt")[0]["change"]["added"][0]["name"] == "Old"


def test_tracker_keeps_copies_while_checking(tracker, clock, config_dir, data_dir):
    folder = z2m_folder(config_dir)
    (folder / "coordinator_backup.json").write_text(json.dumps(backup()))
    tracker.coordinator_backups(force=True)
    copies = tracker.backup_copies()["zigbee2mqtt"]
    assert len(copies) == 1 and copies[0]["devices"] == 1
    assert (data_dir / "coordinator_backups" / "index.json").exists()
    snap = json.loads(tracker.snapshot_json())
    assert snap["backup_copies"]["zigbee2mqtt"][0]["id"] == copies[0]["id"]
    assert KEY not in tracker.snapshot_json().decode()
