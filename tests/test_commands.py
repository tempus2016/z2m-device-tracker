"""Command reply times: <name>/set to the device's next state message, hourly storage, and the
slow_commands / missed_commands checks."""

from __future__ import annotations

import csv
import io
import json

import health
import metrics as metrics_module
import pytest
import tracker as tracker_module
from conftest import PLUG, SENSOR, START, coordinator, send, z2m_device
from metrics import Metrics

HOUR = 3600
DAY = 86400
HOUR_NOW = int(START // HOUR * HOUR)


@pytest.fixture
def m(tmp_path) -> Metrics:
    return Metrics(str(tmp_path / "metrics.json"))


def setup(tracker) -> None:
    send(
        tracker,
        "bridge/devices",
        [coordinator(), z2m_device(PLUG, "Lamp", "Router"), z2m_device(SENSOR, "Door")],
    )
    send(tracker, "Lamp/availability", {"state": "online"})


def cmd(tracker) -> list:
    return tracker.metrics.series.get(PLUG, {}).get("cmd", [])


# ------------------------------------------------------------------ tracker timing


def test_set_then_state_records_reply_time(tracker, clock):
    setup(tracker)
    send(tracker, "Lamp/set", {"state": "ON"})
    clock.advance(0.25)
    send(tracker, "Lamp", {"state": "ON", "linkquality": 120})
    assert cmd(tracker) == [[HOUR_NOW, 1, 0, [250]]]
    # The next plain report isn't a reply to anything.
    clock.advance(5)
    send(tracker, "Lamp", {"state": "ON"})
    assert cmd(tracker) == [[HOUR_NOW, 1, 0, [250]]]


def test_set_without_reply_is_missed_after_ten_seconds(tracker, clock):
    setup(tracker)
    send(tracker, "Lamp/set", {"state": "OFF"})
    clock.advance(9)
    tracker.expire_commands()
    assert cmd(tracker) == []
    clock.advance(2)
    tracker.expire_commands()
    assert cmd(tracker) == [[HOUR_NOW, 1, 1, []]]
    # A state message after the timeout doesn't count as a reply.
    send(tracker, "Lamp", {"state": "OFF"})
    assert cmd(tracker) == [[HOUR_NOW, 1, 1, []]]


def test_late_reply_seen_before_expiry_counts_as_missed(tracker, clock):
    setup(tracker)
    send(tracker, "Lamp/set", {"state": "OFF"})
    clock.advance(12)
    send(tracker, "Lamp", {"state": "OFF"})
    assert cmd(tracker) == [[HOUR_NOW, 1, 1, []]]


def test_snapshot_and_health_expire_old_commands(tracker, clock):
    setup(tracker)
    send(tracker, "Lamp/set", {"state": "OFF"})
    clock.advance(11)
    tracker.evaluate_health()
    assert cmd(tracker) == [[HOUR_NOW, 1, 1, []]]
    send(tracker, "Lamp/set", {"state": "ON"})
    clock.advance(11)
    snap = json.loads(tracker.snapshot_json())
    lamp = next(d for d in snap["devices"] if d["ieee"] == PLUG)
    assert lamp["metrics"]["cmd"]["commands_24h"] == 2
    assert lamp["metrics"]["cmd"]["missed_24h"] == 2


def test_burst_of_commands_is_timed_from_the_first(tracker, clock):
    setup(tracker)
    send(tracker, "Lamp/set", {"brightness": 10})
    clock.advance(0.1)
    send(tracker, "Lamp/set", {"brightness": 20})
    clock.advance(0.2)
    send(tracker, "Lamp", {"brightness": 20})
    assert cmd(tracker) == [[HOUR_NOW, 1, 0, [300]]]


@pytest.mark.parametrize("topic", ["Lamp/set/state", "Lamp/l1/set"])
def test_attribute_and_endpoint_set_topics(tracker, clock, topic):
    setup(tracker)
    send(tracker, topic, "ON")
    clock.advance(0.5)
    send(tracker, "Lamp", {"state": "ON"})
    assert cmd(tracker) == [[HOUR_NOW, 1, 0, [500]]]


def test_set_and_get_topics_are_not_state_messages(tracker, clock):
    setup(tracker)
    before = tracker.metrics.series.get(PLUG, {}).get("msg")
    clock.advance(60)
    send(tracker, "Lamp/get", {"state": ""})
    send(tracker, "Lamp/set", {"state": "ON"})
    assert tracker.metrics.series.get(PLUG, {}).get("msg") == before


def test_retained_set_is_ignored(tracker, clock):
    setup(tracker)
    tracker.handle_message("zigbee2mqtt", "zigbee2mqtt/Lamp/set", b'{"state":"ON"}', retained=True)
    clock.advance(11)
    tracker.expire_commands()
    assert cmd(tracker) == []


def test_unknown_offline_and_group_targets_are_ignored(tracker, clock):
    setup(tracker)
    send(tracker, "Kitchen lights/set", {"state": "ON"})
    send(tracker, "Lamp/availability", {"state": "offline"})
    send(tracker, "Lamp/set", {"state": "ON"})
    clock.advance(11)
    tracker.expire_commands()
    assert tracker.metrics.series.get(PLUG, {}).get("cmd") is None
    assert tracker._commands == {}


def test_forget_drops_pending_command(tracker, clock):
    setup(tracker)
    send(tracker, "Lamp/set", {"state": "ON"})
    tracker.forget(PLUG)
    clock.advance(11)
    tracker.expire_commands()
    assert PLUG not in tracker.metrics.series


def test_csv_has_command_columns(tracker, clock):
    setup(tracker)
    for ms in (100, 200, 300):
        send(tracker, "Lamp/set", {"state": "ON"})
        clock.advance(ms / 1000)
        send(tracker, "Lamp", {"state": "ON"})
    send(tracker, "Lamp/set", {"state": "ON"})
    clock.advance(11)
    tracker.expire_commands()
    rows = list(csv.reader(io.StringIO(tracker.export_csv().decode("utf-8-sig"))))
    header = rows[0]
    lamp = next(r for r in rows[1:] if r[1] == "Lamp")
    door = next(r for r in rows[1:] if r[1] == "Door")
    assert lamp[header.index("commands_24h")] == "4"
    assert lamp[header.index("command_typical_ms_24h")] == "200"
    assert lamp[header.index("commands_missed_24h")] == "1"
    assert door[header.index("commands_24h")] == ""


# ------------------------------------------------------------------ storage


def test_add_command_hourly_buckets_and_two_weeks(m):
    m.add_command(PLUG, 120.4, now=START - (metrics_module.CMD_KEEP_HOURS + 1) * HOUR)
    m.add_command(PLUG, 120.4, now=START)
    m.add_command(PLUG, None, now=START + 10)
    m.add_command(PLUG, 80, now=START + HOUR)
    assert m.series[PLUG]["cmd"] == [[HOUR_NOW, 2, 1, [120]], [HOUR_NOW + HOUR, 1, 0, [80]]]
    assert m.dirty


@pytest.mark.parametrize("value", ["12", True, -5, float("nan")])
def test_add_command_ignores_bad_values(m, value):
    m.add_command(PLUG, value, now=START)
    assert m.series == {}


def test_add_command_caps_samples_per_hour(m):
    for i in range(metrics_module.CMD_SAMPLES_PER_HOUR + 40):
        m.add_command(PLUG, 100 + i, now=START)
    entry = m.series[PLUG]["cmd"][0]
    assert entry[1] == metrics_module.CMD_SAMPLES_PER_HOUR + 40
    assert len(entry[3]) == metrics_module.CMD_SAMPLES_PER_HOUR


def test_commands_survive_save_and_load(m, tmp_path):
    m.add_command(PLUG, 42, now=START)
    m.save()
    again = Metrics(str(tmp_path / "metrics.json"))
    again.load()
    assert again.series[PLUG]["cmd"] == [[HOUR_NOW, 1, 0, [42]]]


def test_summary_skips_broken_rows(m):
    m.series[PLUG] = {"cmd": [[HOUR_NOW, 1, 0, [100]], [HOUR_NOW, "x", 0, []], "junk", [HOUR_NOW, 1, 0, 5]]}
    assert m.summary(PLUG, START)["cmd"]["commands_24h"] == 1


# ------------------------------------------------------------------ stats


def rows(*hours: tuple[int, int, int, list]) -> list[list]:
    """(hours ago, commands, missed, samples) -> stored rows."""
    return [[HOUR_NOW - ago * HOUR, n, missed, samples] for ago, n, missed, samples in hours]


def test_stats_median_p90_and_daily():
    points = rows((1, 10, 0, [100, 110, 120, 130, 140, 150, 160, 170, 180, 900]), (30, 4, 1, [50, 60, 70]))
    out = Metrics.command_stats(points, START)
    assert out["commands_24h"] == 10
    assert out["missed_24h"] == 0
    assert out["median_ms"] == 145
    assert out["p90_ms"] == 180
    assert out["usual_ms"] is None  # under CMD_BASE_MIN answered in the week before
    today = int(START // DAY * DAY)
    assert out["daily"] == [[today - DAY, 60, 4, 1], [today, 145, 10, 0]]
    assert "slow" not in out and "missed_share" not in out


def test_stats_slow_absolute():
    out = Metrics.command_stats(rows((2, 10, 0, [1500] * 10)), START)
    assert out["slow"] == {"median_ms": 1500, "usual_ms": None, "n": 10}


def test_stats_slow_needs_ten_commands():
    out = Metrics.command_stats(rows((2, 9, 0, [1500] * 9)), START)
    assert "slow" not in out


def test_stats_slow_against_own_baseline():
    points = rows((48, 20, 0, [100] * 20), (2, 12, 0, [400] * 12))
    out = Metrics.command_stats(points, START)
    assert out["usual_ms"] == 100
    assert out["slow"] == {"median_ms": 400, "usual_ms": 100, "n": 12, "rise": True}


def test_stats_rise_below_floor_is_not_slow():
    points = rows((48, 20, 0, [40] * 20), (2, 12, 0, [200] * 12))
    assert "slow" not in Metrics.command_stats(points, START)


def test_stats_missed_share():
    out = Metrics.command_stats(rows((3, 10, 2, [100] * 8)), START)
    assert out["missed_share"] == {"missed": 2, "n": 10, "percent": 20}
    assert "missed_share" not in Metrics.command_stats(rows((3, 10, 1, [100] * 9)), START)
    assert "missed_share" not in Metrics.command_stats(rows((3, 5, 5, [])), START)


def test_stats_day_with_only_missed_commands():
    out = Metrics.command_stats(rows((1, 3, 3, [])), START)
    assert out["median_ms"] is None
    assert out["daily"] == [[int(START // DAY * DAY), None, 3, 3]]


# ------------------------------------------------------------------ health checks


def plug_record() -> dict:
    return {"ieee": PLUG, "type": "Router", "status": "active", "availability": "online", "readings": {}}


def test_slow_commands_issue():
    summary = {"cmd": {"slow": {"median_ms": 1500, "usual_ms": None, "n": 12}}}
    issues = health.device_issues(plug_record(), summary, {}, START, [])
    assert [i["kind"] for i in issues] == ["slow_commands"]
    issue = issues[0]
    assert issue["severity"] == health.INFO
    assert issue["msg"] == "slow_commands"
    assert issue["params"] == {"ms": 1500, "n": 12}
    assert issue["detail"].startswith("Typical reply to commands 1500 ms over the last 24 h (12 commands).")


def test_slow_commands_rise_issue():
    summary = {"cmd": {"slow": {"median_ms": 400, "usual_ms": 100, "n": 12, "rise": True}}}
    issue = health.device_issues(plug_record(), summary, {}, START, [])[0]
    assert issue["msg"] == "slow_commands_rise"
    assert issue["params"] == {"ms": 400, "usual_ms": 100, "n": 12}


def test_missed_commands_issue():
    summary = {"cmd": {"missed_share": {"missed": 3, "n": 10, "percent": 30}}}
    issue = health.device_issues(plug_record(), summary, {}, START, [])[0]
    assert issue["kind"] == "missed_commands"
    assert issue["severity"] == health.WARNING
    assert issue["params"] == {"missed": 3, "n": 10, "percent": 30}
    assert issue["detail"].startswith("3 of 10 commands in the last 24 h got no reply within 10 s (30%).")


def test_command_checks_show_only_and_can_be_turned_off():
    for kind in ("slow_commands", "missed_commands"):
        assert kind not in health.ALERT_KINDS
        assert kind not in health.DEFAULT_NOTIFY
        assert kind in {c[0] for c in health.CHECKS}
        assert kind in health.KIND_LABELS
    summary = {
        "cmd": {
            "slow": {"median_ms": 1500, "usual_ms": None, "n": 12},
            "missed_share": {"missed": 3, "n": 10, "percent": 30},
        }
    }
    options = {"disabled_checks": ["slow_commands", "missed_commands"]}
    assert health.device_issues(plug_record(), summary, options, START, []) == []


def test_end_to_end_missed_commands_raise_issue(tracker, clock):
    setup(tracker)
    for i in range(10):
        send(tracker, "Lamp/set", {"state": "ON"})
        if i < 7:
            clock.advance(0.2)
            send(tracker, "Lamp", {"state": "ON"})
        else:
            clock.advance(11)
    issues, _ = tracker.evaluate_health()
    kinds = {i["kind"] for i in issues if i["ieee"] == PLUG}
    assert "missed_commands" in kinds
    assert "slow_commands" not in kinds


def test_tracker_module_timeout_is_ten_seconds():
    assert tracker_module.COMMAND_TIMEOUT == 10.0


# ------------------------------------------------------------------ replies must echo the command


def test_regular_report_with_old_state_is_not_a_reply(tracker, clock):
    setup(tracker)
    send(tracker, "Lamp/set", {"state": "OFF"})
    clock.advance(2)
    send(tracker, "Lamp", {"state": "ON", "power": 40})  # a scheduled power report, still ON
    assert cmd(tracker) == []
    clock.advance(11)
    tracker.expire_commands()
    assert cmd(tracker)[0][1:3] == [1, 1]


def test_burst_is_answered_by_the_last_asked_state(tracker, clock):
    setup(tracker)
    send(tracker, "Lamp/set", {"state": "ON"})
    clock.advance(0.1)
    send(tracker, "Lamp/set", {"state": "OFF"})
    clock.advance(0.2)
    send(tracker, "Lamp", {"state": "ON"})
    assert cmd(tracker) == []
    clock.advance(0.2)
    send(tracker, "Lamp", {"state": "off"})
    assert cmd(tracker) == [[HOUR_NOW, 1, 0, [500]]]


def test_toggle_brightness_and_attribute_topic(tracker, clock):
    setup(tracker)
    send(tracker, "Lamp/set", {"state": "TOGGLE"})
    send(tracker, "Lamp", {"state": "ON"})
    send(tracker, "Lamp/set", {"brightness": 200})
    send(tracker, "Lamp", {"state": "ON", "brightness": 150})
    send(tracker, "Lamp", {"state": "ON", "brightness": 198})
    send(tracker, "Lamp/set/state", "OFF")
    send(tracker, "Lamp", {"state": "ON"})
    send(tracker, "Lamp", {"state": "OFF"})
    assert cmd(tracker)[0][1:3] == [3, 0]


def test_unknown_payload_accepts_any_state(tracker, clock):
    setup(tracker)
    send(tracker, "Lamp/set", "garbage")
    send(tracker, "Lamp", {"linkquality": 90})
    assert cmd(tracker)[0][1:3] == [1, 0]


def test_unanswered_command_before_a_later_one_is_missed(tracker, clock):
    setup(tracker)
    send(tracker, "Lamp/set", {"state": "ON"})
    clock.advance(4)
    send(tracker, "Lamp/set", {"state": "OFF"})
    clock.advance(0.3)
    send(tracker, "Lamp", {"state": "OFF"})
    assert cmd(tracker)[0][1:4] == [2, 1, [300]]
