"""Impossible readings: temperatures outside the set range, humidity or battery outside 0-100 %,
and humidity stuck at 0 or 100 %."""

from __future__ import annotations

import json

import health
import odd_readings
from conftest import SENSOR, START, coordinator, send, z2m_device

HOUR = 3600


def setup(tracker) -> None:
    send(tracker, "bridge/devices", [coordinator(), z2m_device(SENSOR, "Garage")])


def impossible(tracker) -> list[dict]:
    issues, _ = tracker.evaluate_health()
    return [i for i in issues if i["kind"] == "impossible_reading"]


def test_impossible_temperature_with_count_and_usual_range(tracker, clock):
    setup(tracker)
    for value in (6.2, 9.0, 7.5):
        send(tracker, "Garage", {"temperature": value})
        clock.advance(minutes=10)
    for _ in range(3):
        send(tracker, "Garage", {"temperature": -40.0})
        clock.advance(minutes=5)
    [issue] = impossible(tracker)
    assert issue["severity"] == "warning"
    assert issue["msg"] == "impossible_reading"
    assert issue["params"] == {"reading": "temperature", "value": -40.0, "n": 3, "unit": "°C", "lo": 6.2, "hi": 9.0}
    assert issue["detail"].startswith("Temperature -40 °C, 3 times in 24 h. Its other readings: 6.2 to 9 °C.")


def test_issue_clears_a_day_after_the_last_impossible_value(tracker, clock):
    setup(tracker)
    send(tracker, "Garage", {"temperature": 85})
    clock.advance(hours=23)
    assert impossible(tracker)
    clock.advance(hours=2)
    send(tracker, "Garage", {"temperature": 20})
    assert impossible(tracker) == []


def test_limits_come_from_the_options(make_tracker, clock):
    tracker = make_tracker(options={"impossible_temp_min": -10, "impossible_temp_max": 40})
    setup(tracker)
    send(tracker, "Garage", {"temperature": 45})
    [issue] = impossible(tracker)
    assert issue["msg"] == "impossible_reading_no_range"
    tracker.options["impossible_temp_max"] = 50
    send(tracker, "Garage", {"temperature": 45})
    assert impossible(tracker)[0]["params"]["n"] == 1


def test_impossible_battery_is_flagged_and_kept_out_of_estimates(tracker, clock):
    setup(tracker)
    send(tracker, "Garage", {"battery": 80})
    send(tracker, "Garage", {"battery": 255})
    record = tracker.data["devices"][SENSOR]
    assert record["battery"] == 80
    assert [p[1] for p in tracker.metrics.series[SENSOR]["bat"]] == [80.0]
    [issue] = impossible(tracker)
    assert issue["msg"] == "impossible_battery"
    assert issue["params"]["value"] == 255
    # No "battery replaced" from a jump to 255 %.
    assert record["battery_info"].get("replacements") in (None, [])


def test_old_impossible_battery_points_are_ignored(tracker, clock):
    day = int(START // 86400 * 86400)
    tracker.metrics.series[SENSOR] = {"bat": [[day - 2 * 86400, 90.0], [day - 86400, 255.0], [day, 88.0]]}
    summary = tracker.metrics.summary(SENSOR, START)
    assert [p[1] for p in summary["battery_spark"]] == [90.0, 88.0]


def test_humidity_stuck_at_100(tracker, clock):
    setup(tracker)
    send(tracker, "Garage", {"humidity": 100})
    clock.advance(hours=5)
    send(tracker, "Garage", {"humidity": 100})
    assert impossible(tracker) == []
    clock.advance(hours=4)
    [issue] = impossible(tracker)
    assert issue["msg"] == "humidity_stuck"
    assert issue["params"]["seconds"] == 9 * HOUR
    assert "Condensation" in issue["detail"]
    send(tracker, "Garage", {"humidity": 97})
    assert impossible(tracker) == []


def test_humidity_above_100_is_impossible(tracker, clock):
    setup(tracker)
    send(tracker, "Garage", {"humidity": 160})
    assert impossible(tracker)[0]["params"]["reading"] == "humidity"


def test_check_can_be_turned_off(make_tracker, clock):
    tracker = make_tracker(options={"disabled_checks": ["impossible_reading"]})
    setup(tracker)
    send(tracker, "Garage", {"temperature": 99})
    assert impossible(tracker) == []


def test_range_rolls_over_to_the_previous_day():
    store: dict = {}
    odd_readings.note(store, "temperature", 5, {}, START, "2026-06-01")
    odd_readings.note(store, "temperature", 8, {}, START, "2026-06-01")
    odd_readings.note(store, "temperature", 12, {}, START + 86400, "2026-06-02")
    assert store["temperature"] == {"day": "2026-06-02", "lo": 12, "hi": 12, "plo": 5, "phi": 8}


def test_bad_times_are_capped():
    store: dict = {}
    for n in range(odd_readings.MAX_BAD + 10):
        odd_readings.note(store, "battery", 300, {}, START + n, "2026-06-01")
    assert len(store["battery"]["bad"]) == odd_readings.MAX_BAD


def test_stored_data_is_cleaned_and_survives_a_restart(make_tracker, tracker, clock, data_dir):
    setup(tracker)
    send(tracker, "Garage", {"temperature": -45})
    tracker.save(force=True)
    raw = json.loads((data_dir / "history.json").read_text())
    raw["devices"][SENSOR]["odd_readings"]["pressure"] = {"bad": [1]}
    raw["devices"][SENSOR]["odd_readings"]["temperature"]["bad"].append("junk")
    (data_dir / "history.json").write_text(json.dumps(raw))
    again = make_tracker()
    again.load()
    store = again.data["devices"][SENSOR]["odd_readings"]
    assert set(store) == {"temperature"}
    assert store["temperature"]["bad"] == [int(clock.time())]
    assert impossible(again)


def test_health_without_odd_readings_is_unchanged():
    record = {"ieee": SENSOR, "status": "active", "readings": {}}
    assert [i for i in health.device_issues(record, {}, {}, START, []) if i["kind"] == "impossible_reading"] == []


def test_snapshot_leaves_the_store_out(tracker, clock):
    setup(tracker)
    send(tracker, "Garage", {"temperature": 20})
    snap = json.loads(tracker.snapshot_json())
    device = next(d for d in snap["devices"] if d["ieee"] == SENSOR)
    assert "odd_readings" not in device
