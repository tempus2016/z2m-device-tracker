"""Per-device report interval: setting it, and running health as soon as a device is late."""

from __future__ import annotations

from conftest import PLUG, SENSOR, send, z2m_device


def setup(tracker) -> None:
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hive thermostat")])
    send(tracker, "Hive thermostat", {"temperature": 20.5})


def late(tracker) -> list[str]:
    issues, _ = tracker.evaluate_health()
    return [i["ieee"] for i in issues if i["kind"] == "late"]


def test_set_and_clear(tracker):
    setup(tracker)
    assert tracker.set_report_every(SENSOR, 900)
    assert tracker.data["devices"][SENSOR]["report_every"] == 900
    assert tracker.set_report_every(SENSOR, None)
    assert "report_every" not in tracker.data["devices"][SENSOR]


def test_rejects_out_of_range_and_unknown_devices(tracker):
    setup(tracker)
    assert not tracker.set_report_every(SENSOR, 29)
    assert not tracker.set_report_every(SENSOR, 7 * 86400 + 1)
    assert not tracker.set_report_every("0x0000000000000bad", 900)
    assert "report_every" not in tracker.data["devices"][SENSOR]


def test_flags_late_and_clears_when_it_reports_again(tracker, clock):
    setup(tracker)
    tracker.set_report_every(SENSOR, 60)
    clock.advance(59)
    assert late(tracker) == []
    clock.advance(2)
    assert late(tracker) == [SENSOR]
    send(tracker, "Hive thermostat", {"temperature": 20.6})
    assert late(tracker) == []


def test_late_due_fires_once_per_deadline(tracker, clock):
    setup(tracker)
    tracker.set_report_every(SENSOR, 60)
    assert not tracker.late_due(clock.time())
    clock.advance(61)
    assert tracker.late_due(clock.time())
    assert not tracker.late_due(clock.time())  # already checked; the 60 s health loop takes over
    send(tracker, "Hive thermostat", {"temperature": 20.6})
    clock.advance(61)
    assert tracker.late_due(clock.time())


def test_interval_survives_save_and_load(make_tracker, tracker):
    setup(tracker)
    tracker.set_report_every(SENSOR, 900)
    tracker.save(force=True)
    again = make_tracker()
    again.load()
    assert again.data["devices"][SENSOR]["report_every"] == 900


def test_many_applies_valid_items_and_reports_invalid_ones(tracker):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hive thermostat"), z2m_device(PLUG, "Landing plug", "Router")])
    tracker.set_report_every(PLUG, 600)
    calls = []
    original = tracker.evaluate_health
    tracker.evaluate_health = lambda: calls.append(1) or original()
    result = tracker.set_report_every_many(
        [
            {"ieee": SENSOR, "seconds": 900},
            {"ieee": PLUG, "seconds": None},
            {"ieee": "0x0000000000000bad", "seconds": 900},
            {"ieee": SENSOR, "seconds": 10},
            {"ieee": SENSOR, "seconds": True},
            "junk",
        ]
    )
    assert result["applied"] == 2
    assert not result["ok"]
    assert [e["error"] for e in result["errors"]] == [
        "unknown device",
        "seconds must be 30 to 604800",
        "seconds must be a number",
        "unknown device",
    ]
    assert tracker.data["devices"][SENSOR]["report_every"] == 900
    assert "report_every" not in tracker.data["devices"][PLUG]
    assert calls == [1]  # one health run for the whole batch


def test_many_with_nothing_valid_changes_nothing(tracker):
    setup(tracker)
    calls = []
    tracker.evaluate_health = lambda: calls.append(1)
    result = tracker.set_report_every_many([{"ieee": SENSOR, "seconds": 5}])
    assert result == {"ok": False, "applied": 0, "errors": [{"ieee": SENSOR, "error": "seconds must be 30 to 604800"}]}
    assert calls == []
