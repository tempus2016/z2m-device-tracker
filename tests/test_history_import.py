"""history_import: parsers for Z2M config/database/state/logs and HA registries, against tests/fixtures/ha_config."""

from __future__ import annotations

import shutil
from datetime import timedelta, timezone

import history_import as hi
import pytest
from conftest import FIXTURES, events_of, send, z2m_device

HA_CONFIG = FIXTURES / "ha_config"
SENSOR = "0x00158d0000500001"
PLUG = "0x00158d0000600001"
BULB = "0x00158d0000700001"
UNIDENTIFIED = "0x00158d0000900001"
PORCH = "0x00158d0000990001"
COORD = "0x00124b0000c00001"


@pytest.fixture(scope="module")
def result() -> dict:
    return hi.collect_history(str(HA_CONFIG), ["zigbee2mqtt"], timezone.utc)


# ------------------------------------------------------------------ helpers


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1780000000, "2026-05-28T20:26:40+00:00"),
        (1780000000000, "2026-05-28T20:26:40+00:00"),
        ("1780000000", "2026-05-28T20:26:40+00:00"),
        (0, None),
        (-5, None),
        (12345, None),  # 1970
        (None, None),
        ("soon", None),
    ],
)
def test_iso_from_epoch(value, expected):
    assert hi._iso_from_epoch(value) == expected


def test_iso_from_string():
    plus_one = timezone(timedelta(hours=1))
    assert hi._iso_from_string("2026-05-30T08:00:00Z", plus_one) == "2026-05-30T08:00:00+00:00"
    assert hi._iso_from_string("2026-05-30T08:00:00", plus_one) == "2026-05-30T07:00:00+00:00"
    assert hi._iso_from_string("1999-01-01T00:00:00Z", timezone.utc) is None
    assert hi._iso_from_string("yesterday", timezone.utc) is None
    assert hi._iso_from_string(123, timezone.utc) is None


def test_local_log_time():
    plus_one = timezone(timedelta(hours=1))
    assert hi._local_log_time("2026-05-01 10:01:00", plus_one) == "2026-05-01T09:01:00+00:00"
    assert hi._local_log_time("01/05/2026", plus_one) is None


def test_features():
    exposes = [
        {"property": "temperature"},
        {"type": "light", "features": []},
        {"type": "composite"},
        {"property": "linkquality"},
        {"property": "temperature"},
        "junk",
    ]
    assert hi._features(exposes) == ["temperature", "light"]
    assert hi._features(None) == []
    assert len(hi._features([{"property": f"p{i}"} for i in range(40)])) == 30


def test_name_from_slug():
    assert hi._name_from_slug("porch_motion__sensor") == "Porch Motion Sensor"


def test_lenient_yaml_loader_ignores_secrets(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("mqtt:\n  user: !secret mqtt_user\n  port: 1883\n")
    assert hi._load_yaml(str(path)) == {"mqtt": {"user": None, "port": 1883}}
    assert hi._load_yaml(str(tmp_path / "missing.yaml")) is None


def test_devices_from_config_list_form(tmp_path):
    (tmp_path / "a.yaml").write_text("'0x0000000000000001': {friendly_name: A}\n")
    (tmp_path / "b.yaml").write_text("'0x0000000000000002': {friendly_name: B}\n")
    (tmp_path / "configuration.yaml").write_text("devices: [a.yaml, b.yaml, missing.yaml]\n")
    devices = hi._devices_from_config(str(tmp_path / "configuration.yaml"), str(tmp_path))
    assert set(devices) == {"0x0000000000000001", "0x0000000000000002"}


# ------------------------------------------------------------------ collect_history


def test_found_devices(result):
    assert set(result["devices"]) == {SENSOR, PLUG, BULB, UNIDENTIFIED, PORCH, COORD}


def test_sensor_from_config_database_state_and_registry(result):
    info = result["devices"][SENSOR]
    assert info["instance"] == "zigbee2mqtt"
    assert info["fields"] == {
        "z2m_description": "On the fridge",
        "type": "EndDevice",
        "manufacturer": "FakeCo",
        "model_id": "lumi.fake",
        "power_source": "Battery",
        "date_code": "20240101",
        "software_build_id": "1.2",
        "description": "Fake sensor",
        "model": "FK-1",
        "vendor": "FakeCo",
    }
    assert sorted(info["last"]) == ["2026-05-28T20:26:40+00:00", "2026-05-28T20:35:00+00:00"]
    assert info["times"] == ["2025-03-01T09:00:00+00:00"]
    assert info["ha_name"] == "Fridge thermometer"
    assert info["area"] == "Kitchen"
    assert ("Fridge thermometer", None, "HA device registry") in info["names"]
    assert ("Kitchen sensor", None, "Z2M configuration") in info["names"]
    assert info["sources"] == ["HA device registry", "Z2M configuration", "Z2M database"]


def test_plug_from_logs(result):
    info = result["devices"][PLUG]
    assert info["fields"]["model"] == "PLUG-1"
    assert info["fields"]["vendor"] == "PlugCo"
    assert info["fields"]["features"] == ["state", "power"]
    assert "2026-05-30T08:00:00+00:00" in info["last"]  # from state.json
    assert min(info["times"]) == "2026-05-01T10:01:00+00:00"
    assert "Z2M logs" in info["sources"]
    # Rename chain 0x… -> Hall lamp -> Lamp is resolved back to this device through the current name.
    names = [(n, w) for n, w, s in info["names"] if s == "Z2M logs (rename)"]
    assert ("Lamp", "2026-05-01T10:06:00+00:00") in names
    assert ("Hall lamp", "2026-05-01T10:05:00+00:00") in names


def test_log_events(result):
    events = [(e["time"][11:19], e["ieee"], e["kind"], e["detail"]) for e in result["events"]]
    assert events == [
        ("10:01:00", PLUG, "joined", ""),
        ("10:01:05", PLUG, "interview started", ""),
        ("10:01:30", PLUG, "interview successful", "PlugCo PLUG-1"),
        ("11:00:00", BULB, "left", ""),
        ("10:06:00", PLUG, "renamed", "Hall lamp → Lamp"),
        ("10:05:00", PLUG, "renamed", f"{PLUG} → Hall lamp"),
    ]
    assert all(e["instance"] == "zigbee2mqtt" for e in result["events"])


def test_log_times_use_local_timezone():
    plus_one = timezone(timedelta(hours=1))
    events = hi.collect_history(str(HA_CONFIG), ["zigbee2mqtt"], plus_one)["events"]
    assert events[0]["time"] == "2026-05-01T09:01:00+00:00"


def test_removed_bulb(result):
    info = result["devices"][BULB]
    assert info["instance"] == "zigbee2mqtt"  # from the old configuration backup
    assert info["fields"]["type"] == "Router"
    assert ("Old bulb", None, "Z2M configuration") in info["names"]
    assert info["removed_hint"] == "2025-05-01T11:46:40+00:00"  # orphaned_timestamp
    assert "HA deleted devices" in info["sources"]


def test_unidentified_device(result):
    info = result["devices"][UNIDENTIFIED]
    assert info["fields"]["interview_state"] == "FAILED"
    assert info["names"] == []  # its only name was its IEEE address


def test_old_entity_names_and_wal_skipped(result):
    info = result["devices"][PORCH]
    assert info["instance"] is None  # only HA knows it; the -wal database file is skipped
    assert info["names"] == [("Porch Motion", None, "HA old entity names")]
    assert info["removed_hint"] == "2024-02-01T00:00:00+00:00"  # modified_at fallback
    assert info["sources"] == ["HA deleted devices", "HA deleted entities"]


def test_missing_folders(tmp_path):
    assert hi.collect_history(str(tmp_path), ["zigbee2mqtt"], timezone.utc) == {"devices": {}, "events": []}


# ------------------------------------------------------------------ Tracker.import_history


def test_tracker_import_history(tracker, config_dir):
    shutil.copytree(HA_CONFIG, config_dir, dirs_exist_ok=True)
    send(tracker, "bridge/devices", [z2m_device(PLUG, "Lamp", "Router")])
    summary = tracker.import_history()
    assert summary == {"devices_found": 6, "new_devices": 5, "new_events": 6}

    plug = tracker.data["devices"][PLUG]
    assert plug["status"] == "active"
    assert plug["first_seen"] == "2026-05-01T10:01:00+00:00"  # earlier than the live first_seen
    assert [n["name"] for n in plug["names"]] == ["Hall lamp", "Lamp"]  # IEEE-like names dropped
    assert plug["model"] == "FK-1"  # live data is not overwritten

    bulb = tracker.data["devices"][BULB]
    assert bulb["status"] == "removed"
    assert bulb["removed_at"] == "2025-05-01T11:46:40+00:00"
    assert tracker.data["devices"][PORCH]["instance"] == "unknown"
    assert tracker.data["devices"][SENSOR]["area"] == "Kitchen"

    # Importing again adds nothing new.
    assert tracker.import_history() == {"devices_found": 6, "new_devices": 0, "new_events": 0}
    assert len(events_of(tracker, "renamed")) == 2
