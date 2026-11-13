"""Tracker network-level logic: bridge state/health/info, coordinator backup, routing scans and evaluate_health."""

from __future__ import annotations

import json
import os

import pytest
from conftest import COORD, ROUTER_A, ROUTER_B, SENSOR, coordinator, events_of, send, z2m_device

# ------------------------------------------------------------------ bridge state / health / info


def test_bridge_offline_and_back(tracker, clock):
    send(tracker, "bridge/state", "online")
    clock.advance(hours=1)
    send(tracker, "bridge/state", '{"state": "offline"}')
    bridge = tracker.data["bridges"]["zigbee2mqtt"]
    assert bridge["outages"] == [{"start": clock.iso(), "end": None}]

    clock.advance(seconds=119)
    assert [i["kind"] for i in tracker.evaluate_health()[0]] == []
    clock.advance(seconds=1)
    issues, changed = tracker.evaluate_health()
    assert [(i["kind"], i["name"], i["detail"][:22]) for i in issues] == [
        ("network_offline", "Main network", "Not responding for 2 m")
    ]
    assert changed
    assert tracker.evaluate_health()[1] is False

    clock.advance(minutes=1)
    send(tracker, "bridge/state", "online")
    assert bridge["outages"][0]["end"] == clock.iso()
    assert bridge["restarts"] == [clock.iso()]
    assert [e["kind"] for e in events_of(tracker)] == ["network offline", "network online"]


def test_bridge_health_detects_restart_and_device_stats(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "S")])
    send(tracker, "bridge/health", {"process": {"uptime_sec": 90000, "memory_used_mb": 120}})
    send(
        tracker,
        "bridge/health",
        {
            "process": {"uptime_sec": 60},
            "mqtt": {"connected": True, "published": 5},
            "devices": {SENSOR.upper().replace("0X", "0x"): {"messages": 12, "leave_count": 1}},
        },
    )
    assert [e["detail"] for e in events_of(tracker, "zigbee2mqtt restarted")] == ["Was up 25 h"]
    assert tracker.data["bridges"]["zigbee2mqtt"]["health"]["uptime_sec"] == 60
    assert tracker.data["devices"][SENSOR]["z2m_stats"]["messages"] == 12


def test_bridge_info_version_change_and_restart_required(tracker):
    info = {"version": "2.1.0", "network": {"channel": 15, "pan_id": 6754}, "coordinator": {"type": "zStack3x0"}}
    send(tracker, "bridge/info", info)
    send(tracker, "bridge/info", {**info, "version": "2.2.0", "restart_required": True})
    assert [e["detail"] for e in events_of(tracker, "zigbee2mqtt updated")] == ["2.1.0 → 2.2.0"]
    issues, _ = tracker.evaluate_health()
    assert [i["kind"] for i in issues] == ["restart_required"]


def test_bridge_info_permit_join(tracker, clock):
    tracker.pairing_sessions["zigbee2mqtt"] = {"active": True, "joined": []}
    send(tracker, "bridge/info", {"permit_join": True, "permit_join_end": int((clock.now + 60) * 1000)})
    clock.advance(60)
    assert tracker.permit["zigbee2mqtt"] == {"on": True, "end": clock.iso()}
    send(tracker, "bridge/info", {"permit_join": False})
    assert tracker.pairing_sessions["zigbee2mqtt"]["active"] is False


def test_network_checks_can_be_disabled(make_tracker, clock):
    t = make_tracker(options={"disabled_checks": ["network_offline", "restart_required"]})
    send(t, "bridge/state", "offline")
    send(t, "bridge/info", {"restart_required": True})
    clock.advance(hours=1)
    assert t.evaluate_health()[0] == []


# ------------------------------------------------------------------ coordinator backup


def z2m_folder(config_dir, name="zigbee2mqtt", base_topic=None):
    folder = config_dir / name
    folder.mkdir()
    lines = ["homeassistant:", "  enabled: true", "mqtt:", "  server: mqtt://core-mosquitto"]
    if base_topic:
        lines.append(f"  base_topic: '{base_topic}'  # comment")
    (folder / "configuration.yaml").write_text("\n".join(lines) + "\n")
    return folder


def write_backup(folder, *, channel=15, pan_id="1a62", key="0123456789abcdef0123456789abcdef", date=None):
    backup = {
        "metadata": {"format": "zigpy/open-coordinator-backup", "source": "zigbee-herdsman@test", "internal": {}},
        "channel": channel,
        "pan_id": pan_id,
        "network_key": {"key": key} if key else {},
        "devices": [{"ieee_address": "00158d0000500001"}],
    }
    if date:
        backup["metadata"]["internal"]["date"] = date
    (folder / "coordinator_backup.json").write_text(json.dumps(backup))


def running_network(tracker, uptime=200000):
    send(tracker, "bridge/state", "online")
    send(tracker, "bridge/info", {"network": {"channel": 15, "pan_id": 6754}})
    send(tracker, "bridge/health", {"process": {"uptime_sec": uptime}})


def backup_problem(tracker, clock) -> str | None:
    tracker.coordinator_backups(force=True)
    issues = [i for i in tracker.evaluate_health()[0] if i["kind"] == "coordinator_backup"]
    assert len(issues) <= 1
    return issues[0]["detail"] if issues else None


def test_z2m_dirs_reads_base_topic(make_tracker, config_dir):
    z2m_folder(config_dir)
    z2m_folder(config_dir, "zigbee2mqtt_upstairs", base_topic="z2m_up")
    (config_dir / "zigbee2mqtt_empty").mkdir()
    (config_dir / "other").mkdir()
    t = make_tracker()
    assert t._z2m_dirs() == {
        "zigbee2mqtt": str(config_dir / "zigbee2mqtt"),
        "z2m_up": str(config_dir / "zigbee2mqtt_upstairs"),
    }


def test_backup_good(tracker, clock, config_dir):
    folder = z2m_folder(config_dir)
    write_backup(folder, date="2026-06-01T03:00:00.000Z")
    running_network(tracker)
    assert backup_problem(tracker, clock) is None
    info = tracker.coordinator_backups()["zigbee2mqtt"]
    assert info["date"] == "2026-06-01T03:00:00+00:00"
    assert (info["folder"], info["channel"], info["pan_id"], info["devices"], info["has_key"]) == (
        "zigbee2mqtt",
        15,
        "1a62",
        1,
        True,
    )


def test_backup_no_z2m_folder_is_not_an_issue(tracker, clock):
    running_network(tracker)
    assert backup_problem(tracker, clock) is None
    assert tracker.coordinator_backups() == {"zigbee2mqtt": {"found": False}}


def test_backup_missing(tracker, clock, config_dir):
    z2m_folder(config_dir)
    assert backup_problem(tracker, clock).startswith("There's no coordinator_backup.json")


def test_backup_unreadable(tracker, clock, config_dir):
    folder = z2m_folder(config_dir)
    (folder / "coordinator_backup.json").write_text("{not json")
    assert backup_problem(tracker, clock).startswith("coordinator_backup.json exists but is unreadable.")


def test_backup_without_key(tracker, clock, config_dir):
    write_backup(z2m_folder(config_dir), key=None)
    assert "no network key" in backup_problem(tracker, clock)


def test_backup_channel_mismatch(tracker, clock, config_dir):
    write_backup(z2m_folder(config_dir), channel=20)
    running_network(tracker)
    assert backup_problem(tracker, clock).startswith("The backup is for channel 20 but the network runs on channel 15.")


def test_backup_pan_id_mismatch(tracker, clock, config_dir):
    write_backup(z2m_folder(config_dir), pan_id="abcd")
    running_network(tracker)
    assert backup_problem(tracker, clock).startswith("The backup's PAN ID (43981) doesn't match")


def test_backup_stale(tracker, clock, config_dir):
    write_backup(z2m_folder(config_dir), date="2026-05-25T12:00:00Z")
    running_network(tracker)
    assert backup_problem(tracker, clock).startswith("The last coordinator backup is 7 days old.")


def test_backup_stale_uses_file_time_without_internal_date(tracker, clock, config_dir):
    folder = z2m_folder(config_dir)
    write_backup(folder)
    old = clock.now - 5 * 86400
    os.utime(folder / "coordinator_backup.json", (old, old))
    running_network(tracker)
    assert backup_problem(tracker, clock).startswith("The last coordinator backup is 5 days old.")


def test_backup_old_but_z2m_just_restarted(tracker, clock, config_dir):
    # Z2M writes a fresh backup within a day of starting, so a young process isn't blamed yet.
    write_backup(z2m_folder(config_dir), date="2026-05-25T12:00:00Z")
    running_network(tracker, uptime=3600)
    assert backup_problem(tracker, clock) is None


def test_backup_results_are_cached(tracker, clock, config_dir):
    assert tracker.coordinator_backups() == {"zigbee2mqtt": {"found": False}}
    z2m_folder(config_dir)
    clock.advance(seconds=599)
    assert tracker.coordinator_backups() == {"zigbee2mqtt": {"found": False}}
    clock.advance(seconds=2)
    assert tracker.coordinator_backups()["zigbee2mqtt"]["found"] is True


# ------------------------------------------------------------------ routing scan / single point of failure


def link(source, target, lqi, relationship=2):
    return {"source": {"ieeeAddr": source}, "target": {"ieeeAddr": target}, "lqi": lqi, "relationship": relationship}


def mesh(tracker, links):
    send(
        tracker,
        "bridge/devices",
        [
            coordinator(),
            z2m_device(ROUTER_A, "Hall plug", "Router"),
            z2m_device(ROUTER_B, "Shed plug", "Router"),
            z2m_device(SENSOR, "Shed sensor"),
        ],
    )
    nodes = [{"ieeeAddr": i} for i in (COORD, ROUTER_A, ROUTER_B, SENSOR)]
    send(tracker, "bridge/response/networkmap", {"status": "ok", "data": {"value": {"nodes": nodes, "links": links}}})


CHAIN = [
    link(ROUTER_A, COORD, 200, 1),
    link(ROUTER_B, ROUTER_A, 120, 1),
    link(SENSOR, ROUTER_B, 90, 1),
]


def test_networkmap_routing(tracker, clock, tmp_path):
    tracker.load_scans(str(tmp_path / "scans.json"))
    mesh(tracker, CHAIN)
    routing = tracker.data["devices"][SENSOR]["routing"]
    assert routing["parent"] == {"ieee": ROUTER_B, "lqi": 90}
    assert routing["scanned_at"] == clock.iso()
    assert tracker.data["devices"][ROUTER_A]["routing"]["children"] == 1
    assert tracker.data["networkmap"]["zigbee2mqtt"]["nodes"] == 4
    scans = json.loads((tmp_path / "scans.json").read_text())
    assert scans["zigbee2mqtt"][0]["parents"][SENSOR] == [ROUTER_B, 90]
    assert len(tracker.scan_history("zigbee2mqtt")) == 1


def test_networkmap_failure(tracker):
    send(tracker, "bridge/response/networkmap", {"status": "error", "error": "Timeout"})
    assert tracker.data["networkmap"]["zigbee2mqtt"]["status"] == "error"
    assert tracker.single_points("zigbee2mqtt") == []


def test_single_point_of_failure(tracker, clock):
    mesh(tracker, CHAIN)
    points = tracker.single_points("zigbee2mqtt")
    assert [(p["name"], [r["name"] for r in p["routers"]], p["devices"]) for p in points] == [
        ("Hall plug", ["Shed plug"], 1)
    ]
    issues, _ = tracker.evaluate_health()
    assert [(i["kind"], i["name"]) for i in issues] == [("single_point", "Hall plug")]
    assert "only path to the coordinator for 1 router (Shed plug), plus 1 battery device" in issues[0]["detail"]


def test_redundant_path_removes_single_point(tracker):
    mesh(tracker, [*CHAIN, link(ROUTER_B, COORD, 40)])
    assert tracker.single_points("zigbee2mqtt") == []


# ------------------------------------------------------------------ evaluate_health end to end


def test_evaluate_health_flapping_and_offline(make_tracker, clock):
    t = make_tracker(options={"offline_alert_minutes": 30})
    send(t, "bridge/devices", [z2m_device(SENSOR, "Shed sensor")])
    send(t, "Shed sensor/availability", "online")
    for _ in range(4):
        clock.advance(minutes=10)
        send(t, "Shed sensor/availability", "offline")
        clock.advance(minutes=1)
        send(t, "Shed sensor/availability", "online")
    send(t, "Shed sensor/availability", "offline")
    clock.advance(minutes=31)
    issues, _ = t.evaluate_health()
    assert sorted(i["kind"] for i in issues) == ["flapping", "offline"]
    assert all(i["name"] == "Shed sensor" and i["instance"] == "zigbee2mqtt" for i in issues)
    # Warnings sort first; a day later the old outages no longer count as flapping.
    clock.advance(days=1)
    assert [i["kind"] for i in t.evaluate_health()[0]] == ["offline"]


def test_evaluate_health_counts_recent_leaves(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "S")])
    for _ in range(2):
        clock.advance(hours=1)
        send(tracker, "bridge/event", {"type": "device_leave", "data": {"ieee_address": SENSOR}})
    assert [i["kind"] for i in tracker.evaluate_health()[0]] == ["flapping"]


def test_evaluate_health_uses_metrics(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "S")])
    for _ in range(24):
        clock.advance(hours=1)
        send(tracker, "S", {"linkquality": 12})
    assert [i["kind"] for i in tracker.evaluate_health()[0]] == ["weak_signal"]


def test_frozen_reading_end_to_end(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "S")])
    for _ in range(26):
        send(tracker, "S", {"temperature": 18.2})
        clock.advance(hours=1)
    assert [i["kind"] for i in tracker.evaluate_health()[0]] == ["frozen"]


# ------------------------------------------------------------------ radio


@pytest.mark.parametrize(
    ("wifi", "sev"),
    [("1", 2), ("6", 0), ("", 0)],
)
def test_radio_overlap(make_tracker, wifi, sev):
    t = make_tracker(options={"wifi_channels": wifi})
    send(t, "bridge/info", {"network": {"channel": 11, "pan_id": 1}})
    result = t.radio()
    assert result["networks"] == [{"topic": "zigbee2mqtt", "channel": 11, "pan_id": 1, "mhz": 2405}]
    assert max(f["sev"] for f in result["findings"]) == sev
