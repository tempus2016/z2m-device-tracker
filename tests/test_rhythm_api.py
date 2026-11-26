"""Learned rhythm: the learn_rhythm option, "Normal for this device" and "Forget"."""

from __future__ import annotations

import ast
import math
import random
from pathlib import Path
from zoneinfo import ZoneInfo

import rhythm
import yaml
from conftest import SENSOR, send, z2m_device
from metrics import Metrics
from reporting_profiles import effective
from tracker import Tracker

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "z2m_tracker/config.yaml").read_text(encoding="utf-8"))
DAY = 86400


def learned_device(tracker, clock):
    """A sensor that has reported every 5 minutes at this time of week for 10 days, set to Auto."""
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Bathroom temp")])
    send(tracker, "Bathroom temp", {"temperature": 20.5})
    slot = rhythm.slot_of(clock.time(), tracker.metrics.local_tz)
    gaps = {}
    rhythm.add(gaps, slot, rhythm.band_of(300), 40.0)
    tracker.metrics.series[SENSOR].update({"gaps": gaps, "rhythm_since": clock.time() - 10 * DAY, "rhythm_count": 40})
    assert tracker.set_report_profile(SENSOR, "auto")["ok"]


def late(tracker) -> list[str]:
    issues, _ = tracker.evaluate_health()
    return [i["ieee"] for i in issues if i["kind"] == "late"]


def test_option_is_declared_with_default_true():
    assert CONFIG["options"]["learn_rhythm"] is True
    assert CONFIG["schema"]["learn_rhythm"] == "bool?"


def test_unknown_device_is_an_error(tracker):
    for action in (tracker.rhythm_normal, tracker.rhythm_forget):
        result = action("0xdeadbeef")
        assert result["ok"] is False and "error" in result


def test_normal_needs_a_last_seen(tracker):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Bathroom temp")])
    tracker.data["devices"][SENSOR].pop("last_seen", None)
    assert "error" in tracker.rhythm_normal(SENSOR)


def test_normal_needs_a_silence(tracker, clock):
    learned_device(tracker, clock)
    assert "error" in tracker.rhythm_normal(SENSOR)


def test_normal_makes_the_current_silence_acceptable(tracker, clock):
    learned_device(tracker, clock)
    clock.advance(hours=2)
    assert late(tracker) == [SENSOR]
    assert tracker.rhythm_normal(SENSOR) == {"ok": True}
    assert late(tracker) == []
    info = tracker._rhythm(tracker.data["devices"][SENSOR], clock.time())
    assert info["excused"] is True and info["limit"] is None


def test_normal_covers_the_whole_silence_and_teaches_it(tracker, clock):
    learned_device(tracker, clock)
    started = clock.time()
    clock.advance(hours=2)
    assert tracker.rhythm_normal(SENSOR)["ok"]
    for hours in (4, 30, 59):
        clock.now = started + hours * 3600
        assert late(tracker) == []
        assert not tracker.late_due(clock.time())
    clock.now = started + 60 * 3600
    send(tracker, "Bathroom temp", {"temperature": 20.5})
    series = tracker.metrics.series[SENSOR]
    slot = rhythm.slot_of(started, tracker.metrics.local_tz)
    assert series["gaps"][f"{slot}:{rhythm.band_of(60 * 3600)}"] == rhythm.WEIGHT_TAUGHT
    assert "rhythm_excused" not in series
    assert late(tracker) == []


def test_normal_runs_out_after_7_days(tracker, clock):
    learned_device(tracker, clock)
    started = clock.time()
    clock.advance(hours=2)
    assert tracker.rhythm_normal(SENSOR)["ok"]
    clock.now = started + 7 * DAY - 60
    assert late(tracker) == []
    clock.now = started + 7 * DAY + 60
    assert late(tracker) == [SENSOR]


def test_normal_works_after_a_restart_cleared_the_clock(tracker, clock):
    learned_device(tracker, clock)
    tracker.metrics.deaf()
    clock.advance(hours=2)
    assert tracker.rhythm_normal(SENSOR)["ok"]
    assert late(tracker) == []


def test_excused_silence_raises_nothing_from_a_fitting_type_default(tracker, clock):
    learned_device(tracker, clock)
    tracker.set_report_profile(SENSOR, "default")
    rhythm.add(tracker.metrics.series[SENSOR]["gaps"], rhythm.slot_of(clock.time(), tracker.metrics.local_tz), 13, 3)
    record = tracker.data["devices"][SENSOR]
    assert tracker.report_profile(record, {"msg_rate_prev": 12})["source"] == "learned"
    clock.advance(hours=2)
    assert tracker.rhythm_normal(SENSOR)["ok"]
    profile = tracker.report_profile(record, {"msg_rate_prev": 12})
    assert profile["every"] is None


def irregular(tracker, clock) -> None:
    """A button pressed at random: gaps log-uniform from 1 minute to 20 hours, filed for 12 weeks."""
    rng = random.Random(3)
    now = clock.time() - 12 * 7 * DAY
    while now < clock.time():
        tracker.metrics.add_message(SENSOR, now)
        tracker.metrics.decay_rhythm(now)
        now += math.exp(rng.uniform(math.log(60), math.log(20 * 3600)))


def test_irregular_device_raises_no_late(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall button")])
    irregular(tracker, clock)
    send(tracker, "Hall button", {"action": "single"})
    for profile in ("default", "auto"):
        tracker.set_report_profile(SENSOR, profile)
        start = clock.time()
        for hours in (0.5, 2, 8, 30, 100):
            clock.now = start + hours * 3600
            assert late(tracker) == []
        clock.now = start
    info = tracker._rhythm(tracker.data["devices"][SENSOR], clock.time())
    assert info["learned"] and not info["steady"] and info["limit"] is None


def test_unsteady_contact_sensor_keeps_its_type_default():
    # An hourly check-in plus door events at all hours: learned, not steady, so no learned limit.
    m = Metrics(":memory:")
    rng = random.Random(5)
    now = 1_800_000_000.0
    for _ in range(600):
        m.add_message(SENSOR, now)
        now += rng.choice([3600, 3600, 20, 300, 1500, 40, 900, 2400, 90])
    info = m.rhythm(SENSOR, now=now, last_seen=now)
    assert info["learned"] and not info["steady"] and info["limit"] is None
    out = effective({}, "Door", {"msg_rate_prev": 3}, None, learned=info["limit"], excused=info["excused"])
    assert (out["every"], out["source"]) == (3 * 3600, "default")


def test_late_alert_cites_the_slot_silence(tracker, clock):
    """5-minute sensor asleep 23:00-08:00 for 3 weeks: an afternoon alert cites about 7 min, not 11 h."""
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Bathroom temp")])
    today = clock.time() - 4 * 3600  # 08:00
    for day in range(21, 0, -1):
        start = today - day * DAY
        for i in range(15 * 12 + 1):
            tracker.metrics.add_message(SENSOR, start + i * 300)
    clock.now = today + 6 * 3600
    send(tracker, "Bathroom temp", {"temperature": 20.5})
    assert tracker.set_report_profile(SENSOR, "auto")["ok"]
    clock.advance(minutes=16)
    issue = next(i for i in tracker.evaluate_health()[0] if i["kind"] == "late")
    assert issue["detail"] == "No messages for 16 min. Its longest normal silence is about 7 min."
    assert issue["params"]["longest"] == rhythm.BANDS[rhythm.band_of(300)]
    detail = tracker._rhythm(tracker.data["devices"][SENSOR], clock.time())
    assert detail["longest"] >= 9 * 3600  # the device page's Sleeps row keeps the week's longest


def test_network_return_restarts_the_late_clock(tracker, clock):
    learned_device(tracker, clock)
    clock.advance(minutes=5)
    send(tracker, "bridge/state", "offline")
    clock.advance(minutes=30)
    send(tracker, "bridge/state", "online")
    back = clock.time()
    clock.now = back + 14 * 60
    assert late(tracker) == []
    assert not tracker.late_due(clock.time())
    clock.now = back + 16 * 60
    assert tracker.late_due(clock.time())
    assert late(tracker) == [SENSOR]


def test_mqtt_reconnect_restarts_the_late_clock(tracker, clock):
    learned_device(tracker, clock)
    clock.advance(minutes=5)
    tracker.mqtt_lost()
    assert "rhythm_last" not in tracker.metrics.series[SENSOR]
    clock.advance(minutes=30)
    tracker.mqtt_connected()
    back = clock.time()
    clock.now = back + 14 * 60
    assert late(tracker) == []
    assert not tracker.late_due(clock.time())
    clock.now = back + 16 * 60
    assert late(tracker) == [SENSOR]


def test_rhythm_is_worked_out_once_per_device_per_pass(make_tracker, clock, monkeypatch):
    tracker = make_tracker(options={"learn_rhythm": True})
    learned_device(tracker, clock)
    calls = []
    real = tracker.metrics.rhythm
    monkeypatch.setattr(tracker.metrics, "rhythm", lambda *a, **k: calls.append(a) or real(*a, **k))
    clock.advance(minutes=20)
    tracker.evaluate_health()
    assert len(calls) == 1
    calls.clear()
    tracker.set_report_profile(SENSOR, "regular")
    calls.clear()
    tracker.evaluate_health()
    assert calls == []
    tracker.set_report_profile(SENSOR, "auto")
    tracker.options["learn_rhythm"] = False
    calls.clear()
    tracker.evaluate_health()
    assert calls == []


def test_main_wires_mqtt_decay_and_routes():
    """main.py starts threads and writes /data on import, so its wiring is checked in the source."""
    tree = ast.parse((ROOT / "z2m_tracker/app/main.py").read_text(encoding="utf-8"))
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    def calls(func: str) -> set[str]:
        return {ast.unparse(n.func) for n in ast.walk(funcs[func]) if isinstance(n, ast.Call)}

    assert "tracker.mqtt_lost" in calls("on_disconnect")
    assert "tracker.mqtt_connected" in calls("on_connect")
    assert "tracker.metrics.decay_rhythm" in calls("scheduler")
    for name in ("rhythm_normal", "rhythm_forget"):
        route = funcs[f"_post_{name}"]
        assert [ast.unparse(d) for d in route.decorator_list] == [f"post('/api/{name}')"]
        assert f"tracker.{name}" in calls(route.name)
        assert "return (200 if result['ok'] else 400, result)" in ast.unparse(route)
    assert "POST_ROUTES.get" in calls("do_POST")


def test_rhythm_actions_always_say_ok(tracker, clock):
    learned_device(tracker, clock)
    clock.advance(hours=1)
    for action in (tracker.rhythm_normal, tracker.rhythm_forget):
        assert action(SENSOR) == {"ok": True}
        assert action("0xnope")["ok"] is False


def test_tracker_sets_the_metrics_time_zone(data_dir, config_dir):
    london = ZoneInfo("Europe/London")
    metrics = Metrics(str(data_dir / "metrics.json"))
    Tracker(str(data_dir / "history.json"), ["zigbee2mqtt"], str(config_dir), london, metrics, {}, {})
    assert metrics.local_tz is london


def test_forget_returns_the_device_to_learning(tracker, clock):
    learned_device(tracker, clock)
    before = tracker.revision
    assert tracker.rhythm_forget(SENSOR) == {"ok": True}
    assert tracker.revision > before
    out = tracker._rhythm(tracker.data["devices"][SENSOR], clock.time())
    assert (out["learned"], out["limit"]) == (False, None)
    assert tracker.metrics.series[SENSOR].get("gaps", {}) == {}


def test_learn_rhythm_off_and_on_keeps_gaps_and_raises_nothing_new(make_tracker, clock):
    tracker = make_tracker(options={"learn_rhythm": True})
    learned_device(tracker, clock)
    clock.advance(minutes=20)
    gaps = dict(tracker.metrics.series[SENSOR]["gaps"])
    assert late(tracker) == [SENSOR]
    tracker.options["learn_rhythm"] = False
    assert late(tracker) == []
    tracker.options["learn_rhythm"] = True
    assert tracker.metrics.series[SENSOR]["gaps"] == gaps
    assert late(tracker) == [SENSOR]
