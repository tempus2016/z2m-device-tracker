"""Critical devices: the stored mark, the shorter offline delay and the flag on their issues."""

from __future__ import annotations

from conftest import PLUG, SENSOR, send, z2m_device


def setup(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Smoke alarm"), z2m_device(PLUG, "Lamp", "Router")])
    send(tracker, "Smoke alarm/availability", "online")
    send(tracker, "Lamp/availability", "online")


def offline_keys(tracker) -> set[str]:
    issues, _ = tracker.evaluate_health()
    return {i["key"] for i in issues if i["kind"] == "offline"}


def test_set_critical_stores_and_removes_the_key(tracker, clock):
    setup(tracker, clock)
    assert tracker.set_critical(SENSOR, True)
    assert tracker.data["devices"][SENSOR]["critical"] is True
    assert tracker.is_critical(SENSOR)
    assert tracker.set_critical(SENSOR, False)
    assert "critical" not in tracker.data["devices"][SENSOR]
    assert not tracker.is_critical(SENSOR)
    assert not tracker.set_critical("0x0000000000000bad", True)


def test_critical_device_uses_the_shorter_offline_delay(make_tracker, clock):
    tracker = make_tracker(options={"offline_alert_minutes": 60, "critical_offline_minutes": 5})
    setup(tracker, clock)
    tracker.set_critical(SENSOR, True)
    send(tracker, "Smoke alarm/availability", "offline")
    send(tracker, "Lamp/availability", "offline")
    clock.advance(minutes=4)
    assert offline_keys(tracker) == set()
    clock.advance(minutes=2)
    assert offline_keys(tracker) == {f"offline:{SENSOR}"}
    clock.advance(minutes=60)
    assert offline_keys(tracker) == {f"offline:{SENSOR}", f"offline:{PLUG}"}


def test_critical_offline_defaults_to_five_minutes(tracker, clock):
    setup(tracker, clock)
    tracker.set_critical(SENSOR, True)
    send(tracker, "Smoke alarm/availability", "offline")
    clock.advance(minutes=6)
    assert offline_keys(tracker) == {f"offline:{SENSOR}"}


def test_issues_of_critical_devices_carry_the_flag(make_tracker, clock):
    tracker = make_tracker(options={"battery_alert_percent": 15})
    setup(tracker, clock)
    send(tracker, "Smoke alarm", {"battery": 5})
    send(tracker, "Lamp", {"battery": 5})
    tracker.set_critical(SENSOR, True)
    issues, _ = tracker.evaluate_health()
    flags = {i["ieee"]: i.get("critical") for i in issues if i["kind"] == "battery_low"}
    assert flags == {SENSOR: True, PLUG: None}


def test_critical_mark_survives_restart_and_is_in_the_csv(tracker, make_tracker, clock):
    setup(tracker, clock)
    tracker.set_critical(SENSOR, True)
    tracker.save()
    fresh = make_tracker()
    fresh.load()
    assert fresh.is_critical(SENSOR)
    rows = fresh.export_csv().decode("utf-8-sig").splitlines()
    header = rows[0].split(",")
    col = header.index("critical")
    by_name = {r.split(",")[1]: r.split(",")[col] for r in rows[1:]}
    assert by_name == {"Smoke alarm": "yes", "Lamp": ""}


def test_replacement_stays_critical(tracker, clock):
    setup(tracker, clock)
    tracker.set_critical(SENSOR, True)
    new = "0x00158d0000900001"
    send(tracker, "bridge/devices", [z2m_device(PLUG, "Lamp", "Router"), z2m_device(new, "New smoke alarm")])
    assert "critical" in tracker.carry_over(SENSOR, new, notes=False, battery=False)
    assert tracker.is_critical(new)
