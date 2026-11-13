"""metrics.Metrics: hourly LQI/message buckets, daily battery points, summaries and trends."""

from __future__ import annotations

import json

import metrics as metrics_module
import pytest
from conftest import START
from metrics import Metrics

IEEE = "0x00158d0000500001"
HOUR = 3600
DAY = 86400
# START is 12:00 UTC, so it sits exactly on an hour boundary.
HOUR_NOW = int(START // HOUR * HOUR)


@pytest.fixture
def m(tmp_path) -> Metrics:
    return Metrics(str(tmp_path / "metrics.json"))


# ------------------------------------------------------------------ recording


def test_lqi_same_hour_accumulates(m):
    m.add_lqi(IEEE, 100, now=START + 10)
    m.add_lqi(IEEE, 50, now=START + 20)
    m.add_lqi(IEEE, 80, now=START + HOUR)
    assert m.series[IEEE]["lqi"] == [[HOUR_NOW, 150.0, 2], [HOUR_NOW + HOUR, 80.0, 1]]
    assert m.dirty


@pytest.mark.parametrize("value", [None, "120", True, [1]])
def test_lqi_ignores_non_numbers(m, value):
    m.add_lqi(IEEE, value, now=START)
    assert m.series == {}
    assert not m.dirty


def test_lqi_keeps_two_weeks(m):
    m.add_lqi(IEEE, 10, now=START - (metrics_module.LQI_KEEP_HOURS + 2) * HOUR)
    m.add_lqi(IEEE, 20, now=START)
    assert [p[0] for p in m.series[IEEE]["lqi"]] == [HOUR_NOW]


def test_messages_counted_per_hour_and_pruned(m):
    m.add_message(IEEE, now=START - (metrics_module.MSG_KEEP_HOURS + 1) * HOUR)
    for _ in range(3):
        m.add_message(IEEE, now=START + 5)
    assert m.series[IEEE]["msg"] == [[HOUR_NOW, 3]]


def test_battery_daily_point_and_replacement_detection(m):
    assert m.add_battery(IEEE, 40, now=START) is None
    assert m.add_battery(IEEE, 38, now=START + 60) is None
    assert m.series[IEEE]["bat"] == [[int(START // DAY * DAY), 38.0]]
    # A jump of 25 points or more the next day looks like a new battery.
    assert m.add_battery(IEEE, 100, now=START + DAY) == 38.0
    # Same-day jump too.
    m.add_battery(IEEE, 20, now=START + 2 * DAY)
    assert m.add_battery(IEEE, 99, now=START + 2 * DAY + 60) == 20.0
    assert m.add_battery(IEEE, "full", now=START) is None


def test_battery_keeps_400_days(m):
    m.add_battery(IEEE, 90, now=START - 401 * DAY)
    m.add_battery(IEEE, 80, now=START)
    assert len(m.series[IEEE]["bat"]) == 1


def test_now_defaults_to_patched_clock(m, clock):
    m.add_message(IEEE)
    assert m.series[IEEE]["msg"] == [[HOUR_NOW, 1]]


# ------------------------------------------------------------------ persistence


def test_save_load_round_trip(m, tmp_path):
    m.add_lqi(IEEE, 77, now=START)
    m.save()
    assert not m.dirty
    assert not (tmp_path / "metrics.json.tmp").exists()
    other = Metrics(m.path)
    other.load()
    assert other.series == {IEEE: {"lqi": [[HOUR_NOW, 77.0, 1]]}}


def test_save_skips_when_clean(m, tmp_path):
    m.save()
    assert not (tmp_path / "metrics.json").exists()
    m.save(force=True)
    assert json.loads((tmp_path / "metrics.json").read_text()) == {}


@pytest.mark.parametrize("content", ["not json", "[1, 2]"])
def test_load_tolerates_bad_files(tmp_path, content):
    path = tmp_path / "metrics.json"
    path.write_text(content)
    loaded = Metrics(str(path))
    loaded.load()
    assert loaded.series == {}


def test_load_missing_file(m):
    m.load()
    assert m.series == {}


def test_forget(m):
    m.add_message(IEEE, now=START)
    m.dirty = False
    m.forget("0xunknown")
    assert not m.dirty
    m.forget(IEEE)
    assert m.series == {} and m.dirty


# ------------------------------------------------------------------ summary: lqi


def test_summary_empty(m):
    assert m.summary(IEEE, now=START) == {}


def test_summary_lqi_windows(m):
    # Previous week averages 100, last 24 h averages 40.
    for h in range(8 * 24 - 1, 0, -1):
        m.add_lqi(IEEE, 100 if h > 24 else 40, now=START - h * HOUR)
    out = m.summary(IEEE, now=START)
    assert out["lqi_24h"] == 40.0
    assert out["lqi_prev_7d"] == 100.0
    # 7 days of 4-hour buckets.
    assert 40 <= len(out["lqi_spark"]) <= 43
    assert all(isinstance(v, float) for _, v in out["lqi_spark"])


# ------------------------------------------------------------------ summary: messages


def test_summary_message_rate_counts_partial_hour(m):
    now = START + 30 * 60
    for h in range(10, 0, -1):
        for _ in range(100):
            m.add_message(IEEE, now=START - h * HOUR)
    for _ in range(50):
        m.add_message(IEEE, now=now)
    out = m.summary(IEEE, now=now)
    # Last 5 full hours (500) plus half of the current hour (50) over 5.5 hours.
    assert out["msg_rate_6h"] == 100.0
    assert out["msg_24h"] == 1050
    assert "msg_rate_prev" not in out


def test_summary_message_rate_minimum_partial(m):
    m.add_message(IEEE, now=START)
    # The current hour counts as at least a quarter hour so one message isn't 3600/h.
    assert m.summary(IEEE, now=START)["msg_rate_6h"] == 4.0


def test_summary_usual_message_rate(m):
    for h in range(24 + 71, 23, -1):
        for _ in range(10):
            m.add_message(IEEE, now=START - h * HOUR)
    out = m.summary(IEEE, now=START)
    assert out["msg_rate_prev"] == 10.0


# ------------------------------------------------------------------ summary: battery


def add_daily(m: Metrics, values: list[float], end: float = START) -> None:
    for i, value in enumerate(values):
        m.add_battery(IEEE, value, now=end - (len(values) - 1 - i) * DAY)


def test_battery_estimate(m):
    # Falls 1% a day from 60% over 30 days, now at 31%: about 21 days to 10%.
    add_daily(m, [60 - d for d in range(30)])
    out = m.summary(IEEE, now=START)
    assert out["battery_estimate_days"] == 21.0
    assert len(out["battery_spark"]) == 30


def test_battery_estimate_needs_two_weeks_and_a_real_drop(m):
    add_daily(m, [60 - d for d in range(10)])
    assert m.summary(IEEE, now=START)["battery_estimate_days"] is None


def test_battery_estimate_flat(tmp_path):
    flat = Metrics(str(tmp_path / "m.json"))
    add_daily(flat, [80] * 30)
    assert flat.summary(IEEE, now=START)["battery_estimate_days"] is None


def test_battery_estimate_restarts_after_replacement(m):
    # Old battery ran down, then a fresh one that has barely moved.
    add_daily(m, [50 - d for d in range(30)] + [100] * 20)
    assert m.summary(IEEE, now=START)["battery_estimate_days"] is None


def test_battery_estimate_already_empty(m):
    add_daily(m, [40 - 2 * d for d in range(20)])
    assert m.summary(IEEE, now=START)["battery_estimate_days"] == 0.0


def test_fast_drain_detected(m):
    # 40 days at 0.1%/day, then 14 days at 2%/day.
    usual = [100 - 0.1 * d for d in range(40)]
    recent = [usual[-1] - 2 * (d + 1) for d in range(14)]
    add_daily(m, usual + recent)
    drain = m.summary(IEEE, now=START)["battery_drain"]
    assert drain["days"] == 14
    assert drain["rate"] == pytest.approx(2.0, abs=0.01)
    assert drain["usual_rate"] == pytest.approx(0.1, abs=0.01)
    assert drain["factor"] == pytest.approx(20, abs=0.5)
    assert drain["dropped"] == 26.0


def test_no_drain_at_steady_rate(m):
    add_daily(m, [100 - 0.5 * d for d in range(60)])
    assert "battery_drain" not in m.summary(IEEE, now=START)


def test_no_drain_without_enough_history(m):
    add_daily(m, [100 - 2 * d for d in range(14)])
    assert "battery_drain" not in m.summary(IEEE, now=START)


def test_slope():
    assert Metrics._slope([[0, 50]]) is None
    assert Metrics._slope([[0, 50], [0, 40]]) is None
    assert Metrics._slope([[0, 50], [DAY, 48], [2 * DAY, 46]]) == pytest.approx(-2.0)
