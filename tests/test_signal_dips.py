"""Network-wide daily signal dips: Metrics.network_hours, the signal_dip check and its hourly cache."""

from __future__ import annotations

from datetime import timezone

from conftest import START, coordinator, send, z2m_device
from metrics import Metrics

HOUR = 3600
DAY = 86400
# START is 2026-06-01 12:00 UTC, so the 7 complete days run from 2026-05-25 00:00 to 2026-06-01 00:00.
WINDOW_START = int(START // DAY * DAY) - 7 * DAY


def seed(m: Metrics, ieee: str, base: float, dip_hours=(), dip_days=range(7), factor=0.5, skip_hours=(), days=7):
    """Hourly LQI for one device: `base`, times `factor` in `dip_hours` on `dip_days`, nothing in `skip_hours`."""
    points = []
    for day in range(7 - days, 7):
        for h in range(24):
            if h in skip_hours:
                continue
            value = base * (factor if h in dip_hours and day in dip_days else 1)
            points.append([WINDOW_START + day * DAY + h * HOUR, value * 4, 4])
    m.series[ieee] = {"lqi": points}


def ieees(n: int) -> list[str]:
    return [f"0x00158d00007000{i:02d}" for i in range(n)]


def test_evening_dip_found(tmp_path):
    m = Metrics(str(tmp_path / "metrics.json"))
    devices = ieees(10)
    for i, ieee in enumerate(devices):
        # Six of ten devices lose half their link quality 19:00-21:00 on five days.
        seed(m, ieee, 60 + i * 15, dip_hours=(19, 20) if i < 6 else (), dip_days=(0, 1, 3, 4, 6))
    out = m.network_hours(devices, timezone.utc, START)
    assert out["devices"] == 10 and out["days"] == 7
    assert out["dips"] == [{"start": 19, "hours": 2, "devices": 6, "days": 5, "drop": 22}]
    assert len(out["hours"]) == 24
    assert out["hours"][12]["drop"] == 0
    assert out["hours"][19]["lqi"] < out["hours"][12]["lqi"]


def test_dip_on_three_days_only_is_not_flagged(tmp_path):
    m = Metrics(str(tmp_path / "metrics.json"))
    devices = ieees(6)
    for i, ieee in enumerate(devices):
        seed(m, ieee, 100 + i, dip_hours=(19,), dip_days=(0, 2, 4))
    out = m.network_hours(devices, timezone.utc, START)
    assert out is not None and out["dips"] == []


def test_long_low_stretch_is_not_a_dip(tmp_path):
    m = Metrics(str(tmp_path / "metrics.json"))
    devices = ieees(6)
    for i, ieee in enumerate(devices):
        seed(m, ieee, 100 + i, dip_hours=range(1, 7))
    assert m.network_hours(devices, timezone.utc, START)["dips"] == []


def test_dip_past_midnight(tmp_path):
    m = Metrics(str(tmp_path / "metrics.json"))
    devices = ieees(5)
    for i, ieee in enumerate(devices):
        seed(m, ieee, 90 + i, dip_hours=(23, 0))
    dips = m.network_hours(devices, timezone.utc, START)["dips"]
    assert [(d["start"], d["hours"], d["devices"]) for d in dips] == [(23, 2, 5)]


def test_normalised_per_device(tmp_path):
    """Strong devices that don't report 19:00-21:00 mustn't look like a dip in the network average."""
    m = Metrics(str(tmp_path / "metrics.json"))
    devices = ieees(5)
    for ieee in devices[:3]:
        seed(m, ieee, 40)
    for ieee in devices[3:]:
        seed(m, ieee, 220, skip_hours=(19, 20))
    out = m.network_hours(devices, timezone.utc, START)
    assert out["dips"] == []
    assert out["hours"][19]["drop"] == 0


def test_not_enough_data(tmp_path):
    m = Metrics(str(tmp_path / "metrics.json"))
    devices = ieees(6)
    for ieee in devices:
        seed(m, ieee, 100, dip_hours=(19,), days=3)
    assert m.network_hours(devices, timezone.utc, START) is None
    # Plenty of history but too few devices.
    for ieee in devices[:2]:
        seed(m, ieee, 100)
    assert m.network_hours(devices[:2], timezone.utc, START) is None


def _network(tracker, n=10):
    devices = ieees(n)
    send(
        tracker, "bridge/devices", [coordinator()] + [z2m_device(ieee, f"Sensor {i}") for i, ieee in enumerate(devices)]
    )
    for i, ieee in enumerate(devices):
        seed(tracker.metrics, ieee, 80 + i * 10, dip_hours=(18, 19, 20) if i < 7 else ())
    return devices


def test_signal_dip_issue(tracker):
    _network(tracker)
    issues = [i for i in tracker.evaluate_health()[0] if i["kind"] == "signal_dip"]
    assert len(issues) == 1
    issue = issues[0]
    assert issue["key"] == "signal_dip:zigbee2mqtt" and issue["severity"] == "info"
    assert issue["params"] == {"n": 7, "period": "evenings", "start": "18:00", "end": "21:00"}
    assert issue["detail"].startswith("Signal drops across 7 devices most evenings, 18:00–21:00. Something switches on")
    assert "network_hours" in tracker.snapshot_json().decode()


def test_signal_dip_disabled(make_tracker):
    tracker = make_tracker(options={"disabled_checks": ["signal_dip"]})
    _network(tracker)
    assert [i for i in tracker.evaluate_health()[0] if i["kind"] == "signal_dip"] == []


def test_network_hours_cached_per_hour(tracker, clock):
    devices = _network(tracker)
    first = tracker.network_hours(clock.time())
    assert first["zigbee2mqtt"]["devices"] == 10
    tracker.metrics.series.pop(devices[0])
    clock.advance(minutes=30)
    assert tracker.network_hours(clock.time()) is first
    clock.advance(minutes=30)
    assert tracker.network_hours(clock.time())["zigbee2mqtt"]["devices"] == 9


def test_dip_text_periods():
    from tracker import Tracker

    words = {
        start: Tracker._dip_text({"start": start, "hours": 2, "devices": 1})[1]["period"] for start in (7, 13, 19, 2)
    }
    assert words == {7: "mornings", 13: "afternoons", 19: "evenings", 2: "nights"}
    assert Tracker._dip_text({"start": 7, "hours": 2, "devices": 1})[0].startswith("Signal drops across 1 device most")
