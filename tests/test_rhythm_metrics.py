"""Metrics: the per-device gap table (rhythm), how messages feed it and what it skips."""

from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta, timezone

import pytest
import rhythm
from conftest import SENSOR, send, z2m_device
from metrics import Metrics

IEEE = "0x00158d0000500001"
OTHER = "0x00158d0000500002"
HOUR = 3600
DAY = 86400
MON_14 = datetime(2026, 6, 1, 14, 0, 0, tzinfo=timezone.utc).timestamp()  # a Monday: slot 0 * 8 + 14 // 3 = 4


@pytest.fixture
def metrics(tmp_path) -> Metrics:
    return Metrics(str(tmp_path / "metrics.json"))


def gaps_of(m: Metrics, ieee: str = IEEE) -> dict:
    return m.series.get(ieee, {}).get("gaps", {})


def test_gap_is_filed_under_the_slot_it_started(metrics):
    metrics.add_message(IEEE, MON_14)
    metrics.add_message(IEEE, MON_14 + 300)
    assert gaps_of(metrics) == {f"4:{rhythm.band_of(300)}": 1.0}


def test_slot_follows_local_time(metrics):
    metrics.local_tz = timezone(timedelta(hours=5))  # Monday 14:00 UTC is 19:00 local: slot 6
    metrics.add_message(IEEE, MON_14)
    metrics.add_message(IEEE, MON_14 + 300)
    assert list(gaps_of(metrics)) == [f"6:{rhythm.band_of(300)}"]


def test_first_message_files_nothing_and_starts_the_clock(metrics):
    metrics.add_message(IEEE, MON_14)
    device = metrics.series[IEEE]
    assert device.get("gaps", {}) == {}
    assert device["rhythm_last"] == MON_14
    assert device["rhythm_since"] == MON_14


def test_rhythm_since_is_set_once(metrics):
    metrics.add_message(IEEE, MON_14)
    metrics.add_message(IEEE, MON_14 + 300)
    assert metrics.series[IEEE]["rhythm_since"] == MON_14


def test_retained_replay_files_nothing(metrics):
    metrics.add_message(IEEE, MON_14, real=True)
    metrics.add_message(IEEE, MON_14 + 3600, real=False)
    metrics.add_message(IEEE, MON_14 + 7200, real=True)
    assert gaps_of(metrics) == {f"4:{rhythm.band_of(7200)}": 1.0}
    assert sum(count for _, count in metrics.series[IEEE]["msg"]) == 3


def test_replay_as_first_message_does_not_start_the_clock(metrics):
    metrics.add_message(IEEE, MON_14, real=False)
    assert "rhythm_last" not in metrics.series[IEEE]
    assert "rhythm_since" not in metrics.series[IEEE]


def test_deaf_drops_the_next_gap(metrics):
    metrics.add_message(IEEE, MON_14)
    metrics.deaf()
    metrics.add_message(IEEE, MON_14 + 9 * HOUR)
    assert gaps_of(metrics) == {}
    metrics.add_message(IEEE, MON_14 + 9 * HOUR + 300)
    assert gaps_of(metrics) == {f"{rhythm.slot_of(MON_14 + 9 * HOUR, timezone.utc)}:{rhythm.band_of(300)}": 1.0}


def test_deaf_for_some_devices_only(metrics):
    for ieee in (IEEE, OTHER):
        metrics.add_message(ieee, MON_14)
    metrics.deaf([IEEE])
    for ieee in (IEEE, OTHER):
        metrics.add_message(ieee, MON_14 + 300)
    assert gaps_of(metrics, IEEE) == {}
    assert gaps_of(metrics, OTHER) == {f"4:{rhythm.band_of(300)}": 1.0}


def test_deaf_ignores_unknown_device(metrics):
    metrics.deaf(["0xnope"])
    assert "0xnope" not in metrics.series


def test_clock_going_back_files_nothing(metrics):
    metrics.add_message(IEEE, MON_14)
    metrics.add_message(IEEE, MON_14 - 60)
    assert gaps_of(metrics) == {}
    assert metrics.series[IEEE]["rhythm_last"] == MON_14 - 60


def learned_table(m: Metrics, ieee: str = IEEE) -> None:
    """Ten days of history with 40 five-minute gaps: learned, and 5 minutes is the recurring band."""
    m.series[ieee] = {"gaps": {f"4:{rhythm.band_of(300)}": 40.0}, "rhythm_since": MON_14 - 10 * DAY, "rhythm_count": 40}


def test_over_limit_gap_counts_half_once_learned(metrics):
    learned_table(metrics)
    metrics.add_message(IEEE, MON_14, limit=900)
    metrics.add_message(IEEE, MON_14 + 3 * HOUR, limit=900)
    assert gaps_of(metrics)[f"4:{rhythm.band_of(3 * HOUR)}"] == 0.5


def test_within_limit_gap_counts_full_once_learned(metrics):
    learned_table(metrics)
    metrics.add_message(IEEE, MON_14, limit=900)
    metrics.add_message(IEEE, MON_14 + 600, limit=900)
    assert gaps_of(metrics)[f"4:{rhythm.band_of(600)}"] == 1.0


def test_over_limit_gap_counts_full_while_learning(metrics):
    metrics.add_message(IEEE, MON_14, limit=900)
    metrics.add_message(IEEE, MON_14 + 3 * HOUR, limit=900)
    assert gaps_of(metrics)[f"4:{rhythm.band_of(3 * HOUR)}"] == 1.0


def test_no_limit_counts_full_once_learned(metrics):
    learned_table(metrics)
    metrics.add_message(IEEE, MON_14, limit=None)
    metrics.add_message(IEEE, MON_14 + 3 * HOUR, limit=None)
    assert gaps_of(metrics)[f"4:{rhythm.band_of(3 * HOUR)}"] == 1.0


def test_load_clears_clock_and_cleans_table(tmp_path):
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps({IEEE: {"gaps": ["bad"], "rhythm_last": 1.0, "msg": [[0, 1]]}}))
    m = Metrics(str(path))
    m.load()
    assert m.series[IEEE]["gaps"] == {}
    assert "rhythm_last" not in m.series[IEEE]
    assert m.series[IEEE]["msg"] == [[0, 1]]


def test_load_keeps_a_good_table(tmp_path):
    first = Metrics(str(tmp_path / "metrics.json"))
    first.add_message(IEEE, MON_14)
    first.add_message(IEEE, MON_14 + 300)
    first.save()
    second = Metrics(str(tmp_path / "metrics.json"))
    second.load()
    assert gaps_of(second) == {f"4:{rhythm.band_of(300)}": 1.0}
    assert second.series[IEEE]["rhythm_since"] == MON_14
    assert "rhythm_last" not in second.series[IEEE]


def test_old_file_without_rhythm_keys_loads(tmp_path):
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps({IEEE: {"msg": [[0, 1]], "bat": [[0, 90.0]]}, "junk": 5}))
    m = Metrics(str(path))
    m.load()
    assert m.series[IEEE] == {"msg": [[0, 1]], "bat": [[0, 90.0]]}
    assert m.rhythm(IEEE, now=MON_14)["learned"] is False


def test_irregular_device_never_gets_a_limit(metrics):
    """A button: gaps spread log-uniformly from 1 minute to 20 hours (about 8 a day), for 12 weeks."""
    rng = random.Random(11)
    now = MON_14
    end = MON_14 + 12 * 7 * DAY
    while now < end:
        metrics.add_message(IEEE, now)
        info = metrics.rhythm(IEEE, now=now, last_seen=now)
        assert info["limit"] is None
        metrics.decay_rhythm(now)
        now += math.exp(rng.uniform(math.log(60), math.log(20 * HOUR)))
    info = metrics.rhythm(IEEE, now=now, last_seen=now)
    assert info["learned"] is True and info["steady"] is False and info["limit"] is None


def test_day_tight_night_long_sensor_stays_steady(metrics):
    """Every 5 minutes from 08:00 to 23:00, asleep overnight, for 3 weeks."""
    for day in range(21):
        start = MON_14 - 6 * HOUR + day * DAY  # 08:00
        for i in range(15 * 12 + 1):
            metrics.add_message(IEEE, start + i * 300)
        metrics.decay_rhythm(start)
    now = MON_14 + 21 * DAY
    info = metrics.rhythm(IEEE, now=now, last_seen=now)
    assert info["learned"] and info["steady"] and info["limit"] == 900
    assert info["slot_longest"] == rhythm.BANDS[rhythm.band_of(300)]
    night = metrics.rhythm(IEEE, now=now, last_seen=MON_14 + 9 * HOUR + 20 * DAY)  # Sunday 23:00
    assert night["limit"] >= 9 * HOUR


def test_weekly_reporter_stays_learning(metrics):
    for week in range(10):
        metrics.add_message(IEEE, MON_14 + week * 7 * DAY)
    info = metrics.rhythm(IEEE, now=MON_14 + 9 * 7 * DAY, last_seen=MON_14 + 9 * 7 * DAY)
    assert info["learned"] is False
    assert info["limit"] is None


def test_rhythm_limit_for_a_learned_device(metrics):
    learned_table(metrics)
    info = metrics.rhythm(IEEE, now=MON_14, last_seen=MON_14)
    assert info["learned"] is True and info["excused"] is False
    assert info["limit"] == rhythm.limit(gaps_of(metrics), 4)
    assert info["slot_longest"] == rhythm.BANDS[rhythm.band_of(300)]


def test_slot_longest_is_the_band_edge_not_the_clamped_limit(metrics):
    learned_table(metrics)  # 5-minute gaps: the limit is the 15-minute floor
    info = metrics.rhythm(IEEE, now=MON_14, last_seen=MON_14)
    assert info["limit"] == 900
    assert info["slot_longest"] == rhythm.BANDS[rhythm.band_of(300)] == 448
    metrics.series[IEEE]["rhythm_excused"] = MON_14
    assert metrics.rhythm(IEEE, now=MON_14, last_seen=MON_14)["slot_longest"] is None
    assert metrics.rhythm(IEEE, now=MON_14)["limit"] is None
    assert metrics.rhythm("0xunknown", now=MON_14, last_seen=MON_14)["learned"] is False


def test_teach_excuses_the_silence_and_files_it_at_weight_4_on_return(metrics):
    metrics.add_message(IEEE, MON_14)
    assert metrics.teach(IEEE) is True
    assert metrics.series[IEEE]["rhythm_excused"] == MON_14
    assert metrics.dirty is True
    metrics.add_message(IEEE, MON_14 + 3 * HOUR)
    assert gaps_of(metrics) == {f"4:{rhythm.band_of(3 * HOUR)}": 4.0}
    assert "rhythm_excused" not in metrics.series[IEEE]
    assert metrics.series[IEEE]["rhythm_count"] == 1
    metrics.forget_rhythm(IEEE)
    for key in ("gaps", "rhythm_since", "rhythm_last", "rhythm_excused", "rhythm_day", "rhythm_count"):
        assert key not in metrics.series[IEEE]


def test_teach_uses_last_seen_when_the_clock_was_cleared(metrics):
    metrics.add_message(IEEE, MON_14)
    metrics.deaf()
    assert metrics.teach(IEEE, MON_14 - 60) is True
    assert metrics.series[IEEE]["rhythm_excused"] == MON_14 - 60
    metrics.add_message(IEEE, MON_14 + 3 * HOUR)
    assert gaps_of(metrics) == {f"4:{rhythm.band_of(3 * HOUR + 60)}": 4.0}


def test_teach_needs_a_time_to_excuse(metrics):
    assert metrics.teach(IEEE) is False
    assert IEEE not in metrics.series


def file_one(m: Metrics, now: float) -> None:
    """File one 5-minute gap ending at now."""
    m.deaf([IEEE])
    m.add_message(IEEE, now - 300)
    m.add_message(IEEE, now)


def test_decay_runs_once_per_local_day(metrics):
    metrics.series[IEEE] = {"gaps": {"4:15": 1.0, "4:16": 0.05}}
    file_one(metrics, MON_14)
    metrics.decay_rhythm(MON_14)
    assert gaps_of(metrics)["4:15"] == pytest.approx(0.98)
    assert "4:16" not in gaps_of(metrics)
    file_one(metrics, MON_14 + HOUR)
    metrics.decay_rhythm(MON_14 + HOUR)
    assert gaps_of(metrics)["4:15"] == pytest.approx(0.98)
    metrics.decay_rhythm(MON_14 + DAY)
    assert gaps_of(metrics)["4:15"] == pytest.approx(0.98**2)
    assert metrics.series[IEEE]["rhythm_day"] == datetime.fromtimestamp(MON_14 + DAY, timezone.utc).toordinal()


def test_decay_day_is_the_local_day(metrics):
    metrics.local_tz = timezone(timedelta(hours=5))
    metrics.series[IEEE] = {"gaps": {"4:5": 1.0}}
    file_one(metrics, MON_14 + 4 * HOUR)
    metrics.decay_rhythm(MON_14 + 4 * HOUR)  # 23:00 local Monday
    file_one(metrics, MON_14 + 5 * HOUR)
    metrics.decay_rhythm(MON_14 + 5 * HOUR)  # 00:00 local Tuesday: a new day
    assert gaps_of(metrics)["4:5"] == pytest.approx(0.98**2)


def test_decay_skips_a_device_that_filed_nothing(metrics):
    metrics.series[IEEE] = {"gaps": {"4:5": 1.0}}
    for day in range(5):
        metrics.decay_rhythm(MON_14 + day * DAY)
    assert gaps_of(metrics) == {"4:5": 1.0}


def test_ten_restarts_in_a_day_decay_at_most_once(tmp_path):
    path = str(tmp_path / "metrics.json")
    first = Metrics(path)
    first.series[IEEE] = {"gaps": {"4:20": 10.0}, "rhythm_since": MON_14 - 10 * DAY, "rhythm_count": 10}
    first.save(force=True)
    for start in range(10):
        m = Metrics(path)
        m.load()
        now = MON_14 + start * HOUR
        m.add_message(IEEE, now)
        m.add_message(IEEE, now + 300)
        m.decay_rhythm(now + 300)
        m.save(force=True)
    final = Metrics(path)
    final.load()
    assert gaps_of(final)["4:20"] == pytest.approx(10.0 * 0.98)


def test_silent_device_keeps_its_limit_and_stays_learned(metrics):
    learned_table(metrics)
    before = metrics.rhythm(IEEE, now=MON_14, last_seen=MON_14)
    for day in range(1, 61):
        metrics.decay_rhythm(MON_14 + day * DAY)
    after = metrics.rhythm(IEEE, now=MON_14 + 60 * DAY, last_seen=MON_14)
    assert after["learned"] is True
    assert after["limit"] == before["limit"] == 900
    assert gaps_of(metrics) == {f"4:{rhythm.band_of(300)}": 40.0}


def test_reports_counts_filed_gaps_not_weight(metrics):
    metrics.series[IEEE] = {}
    for i in range(24):
        now = MON_14 + i * DAY
        file_one(metrics, now)
        metrics.decay_rhythm(now)
    info = metrics.rhythm(IEEE, now=MON_14 + 24 * DAY)
    assert info["gaps"] == 24
    assert rhythm.total(gaps_of(metrics)) < 24


def test_load_upgrades_and_checks_the_count_and_day(tmp_path):
    path = tmp_path / "metrics.json"
    path.write_text(
        json.dumps(
            {
                "old": {"gaps": {"4:5": 20.4, "4:6": 12.0}},
                "bad": {"gaps": {"4:5": 3.0}, "rhythm_count": "x", "rhythm_day": True, "rhythm_excused": "y"},
                "neg": {"rhythm_count": -2, "rhythm_day": 1.5, "rhythm_excused": float("inf")},
                "ok": {"gaps": {"4:5": 3.0}, "rhythm_count": 50, "rhythm_day": 739000, "rhythm_excused": MON_14},
            }
        )
    )
    m = Metrics(str(path))
    m.load()
    assert m.series["old"]["rhythm_count"] == 32
    assert m.series["bad"]["rhythm_count"] == 3
    assert "rhythm_day" not in m.series["bad"] and "rhythm_excused" not in m.series["bad"]
    assert m.series["neg"] == {}
    assert (m.series["ok"]["rhythm_count"], m.series["ok"]["rhythm_day"]) == (50, 739000)
    assert m.series["ok"]["rhythm_excused"] == MON_14


def test_decay_round_trips_through_save_and_load(metrics, tmp_path):
    metrics.series[IEEE] = {"gaps": {"4:5": 1.0}}
    file_one(metrics, MON_14)
    del metrics.series[IEEE]["gaps"][f"4:{rhythm.band_of(300)}"]
    metrics.decay_rhythm(MON_14)
    metrics.save()
    again = Metrics(str(tmp_path / "metrics.json"))
    again.load()
    assert gaps_of(again) == {"4:5": pytest.approx(0.98)}
    assert set(again.series) == {IEEE}


def test_forget_drops_rhythm(metrics):
    metrics.add_message(IEEE, MON_14)
    metrics.add_message(IEEE, MON_14 + 300)
    metrics.forget(IEEE)
    assert IEEE not in metrics.series


def test_replacement_carries_the_table_not_the_clock(tracker):
    new = "0x00158d0000700001"
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Leak"), z2m_device(new, "New leak")])
    tracker.metrics.add_message(SENSOR, MON_14)
    tracker.metrics.add_message(SENSOR, MON_14 + 300)
    table = dict(gaps_of(tracker.metrics, SENSOR))
    tracker.carry_over(SENSOR, new, notes=False, battery=False)
    series = tracker.metrics.series[new]
    assert series["gaps"] == table
    assert series["rhythm_since"] == MON_14
    assert "rhythm_last" not in series
    series["gaps"]["0:0"] = 1.0
    assert "0:0" not in gaps_of(tracker.metrics, SENSOR)


def test_replacement_merges_into_a_table_the_new_device_already_has(tracker):
    new = "0x00158d0000700001"
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Leak"), z2m_device(new, "New leak")])
    five = f"4:{rhythm.band_of(300)}"
    tracker.metrics.series.setdefault(SENSOR, {}).update(
        {"gaps": {five: 30.0, "7:16": 3.0}, "rhythm_since": MON_14 - 40 * DAY, "rhythm_count": 45}
    )
    tracker.metrics.series[SENSOR].update({"rhythm_excused": MON_14, "rhythm_day": 5, "rhythm_last": MON_14})
    for i in range(4):  # the new device joined and sent a few messages already
        tracker.metrics.add_message(new, MON_14 + i * 300)
    tracker.metrics.series[new]["rhythm_day"] = 9
    tracker.carry_over(SENSOR, new, notes=False, battery=False)
    series = tracker.metrics.series[new]
    assert series["gaps"] == {five: 33.0, "7:16": 3.0}
    assert series["rhythm_since"] == MON_14 - 40 * DAY
    assert series["rhythm_count"] == 48
    assert series["rhythm_day"] == 9 and series["rhythm_last"] == MON_14 + 900
    assert "rhythm_excused" not in series


def test_state_message_feeds_the_table(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Leak")])
    send(tracker, "Leak", {"battery": 90})
    clock.advance(minutes=5)
    send(tracker, "Leak", {"battery": 90})
    assert list(gaps_of(tracker.metrics, SENSOR).values()) == [1.0]


def test_retained_state_does_not_feed_the_table(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Leak")])
    send(tracker, "Leak", {"battery": 90})
    clock.advance(minutes=5)
    tracker.handle_message("zigbee2mqtt", "zigbee2mqtt/Leak", b'{"battery": 90}', retained=True)
    assert gaps_of(tracker.metrics, SENSOR) == {}


def test_bridge_offline_makes_that_network_deaf(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Leak")])
    send(tracker, "Leak", {"battery": 90})
    send(tracker, "bridge/state", "offline")
    clock.advance(hours=9)
    send(tracker, "Leak", {"battery": 90})
    assert gaps_of(tracker.metrics, SENSOR) == {}


def test_zha_battery_event_is_not_a_message(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Leak")])
    send(tracker, "Leak", {"battery": 90})
    clock.advance(minutes=5)
    tracker.apply_state("zigbee2mqtt", "Leak", {"battery": 80}, rhythm=False)
    assert gaps_of(tracker.metrics, SENSOR) == {}
    assert tracker.metrics.series[SENSOR]["msg"][-1][1] == 2


def test_load_drops_bad_rhythm_since(tmp_path):
    path = tmp_path / "metrics.json"
    bad = {"a": "x", "b": True, "c": None, "d": [1]}
    path.write_text(json.dumps({k: {"rhythm_since": v} for k, v in bad.items()} | {"ok": {"rhythm_since": 5}}))
    m = Metrics(str(path))
    m.load()
    assert all("rhythm_since" not in m.series[k] for k in bad)
    assert m.series["ok"]["rhythm_since"] == 5


def test_backup_import_gets_the_load_treatment(tracker):
    backup = {"gaps": ["bad"], "rhythm_last": MON_14, "rhythm_since": "soon", "msg": [[0, 1]]}
    tracker.metrics.adopt({IEEE: backup})
    assert tracker.metrics.series[IEEE] == {"gaps": {}, "msg": [[0, 1]]}
    tracker.metrics.add_message(IEEE, MON_14 + 300)
    assert gaps_of(tracker.metrics) == {}


def test_restore_runs_adopt(tracker):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Leak")])
    payload = json.loads(tracker.backup())
    payload["metrics"] = {SENSOR: {"gaps": ["bad"], "rhythm_last": MON_14, "rhythm_since": "soon"}}
    assert tracker.restore(payload)["ok"] is True
    assert tracker.metrics.series[SENSOR] == {"gaps": {}}


def test_merge_twice_is_the_same_as_once(metrics):
    metrics.series[IEEE] = {"gaps": {"4:6": 30.0}, "rhythm_since": MON_14 - 40 * DAY, "rhythm_count": 30, "msg": []}
    metrics.series[OTHER] = {"gaps": {"4:6": 2.0}, "rhythm_since": MON_14, "rhythm_count": 2}
    metrics.merge_rhythm(IEEE, OTHER)
    once = json.loads(json.dumps(metrics.series[OTHER]))
    metrics.merge_rhythm(IEEE, OTHER)
    assert (
        metrics.series[OTHER] == once == {"gaps": {"4:6": 32.0}, "rhythm_since": MON_14 - 40 * DAY, "rhythm_count": 32}
    )
    assert metrics.series[IEEE] == {"msg": []}
