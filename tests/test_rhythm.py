"""Rhythm maths: bands, weekly slots, decay and the learned report-gap limit."""

from __future__ import annotations

import math
import random
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import rhythm

LONDON = ZoneInfo("Europe/London")


def day_night_table():
    # every 5 min all week, plus a 9 h sleep starting 23:00 on three nights
    gaps = {}
    for slot in range(56):
        rhythm.add(gaps, slot, rhythm.band_of(300), 30)
    night = rhythm.band_of(9 * 3600)
    for day in (0, 1, 2):
        rhythm.add(gaps, day * 8 + 7, night, 1)
    return gaps


def test_bands_span_30s_to_7d():
    assert len(rhythm.BANDS) == 23 and rhythm.BANDS[0] == 30 and rhythm.BANDS[-1] == 604800
    assert list(rhythm.BANDS) == sorted(rhythm.BANDS)
    assert rhythm.band_of(10) == 0 and rhythm.band_of(30) == 0 and rhythm.band_of(31) == 1
    assert rhythm.band_of(8 * 86400) == 23


def test_bad_gaps_have_no_band():
    for bad in (0, -5, float("nan"), float("inf")):
        assert rhythm.band_of(bad) is None


def test_slot_is_local_week():
    mon_0130 = datetime(2026, 11, 16, 1, 30, tzinfo=LONDON).timestamp()
    sun_2259 = datetime(2026, 11, 22, 22, 59, tzinfo=LONDON).timestamp()
    assert rhythm.slot_of(mon_0130, LONDON) == 0 and rhythm.slot_of(sun_2259, LONDON) == 55


def test_add_accumulates():
    gaps = {}
    rhythm.add(gaps, 5, 3, 1.0)
    rhythm.add(gaps, 5, 3, 0.5)
    assert gaps == {"5:3": 1.5}
    assert rhythm.total(gaps) == 1.5


def test_day_tight_night_long():
    gaps = day_night_table()
    assert rhythm.limit(gaps, 4) == 900  # 12:00-15:00: clamped to the 15 min floor
    assert rhythm.limit(gaps, 3 * 8 + 7) >= 9 * 3600 * 1.5 - 1  # Thursday night, known from the other nights


def test_single_outage_changes_nothing():
    gaps = {}
    for slot in range(56):
        rhythm.add(gaps, slot, rhythm.band_of(300), 30)
    rhythm.add(gaps, 4, rhythm.band_of(2 * 86400), rhythm.WEIGHT_OVER)
    assert rhythm.limit(gaps, 4) == 900


def test_taught_gap_is_accepted_at_once_for_that_slot_only():
    gaps = {}
    for slot in range(56):
        rhythm.add(gaps, slot, rhythm.band_of(300), 30)
    rhythm.add(gaps, 38, rhythm.band_of(60 * 3600), rhythm.WEIGHT_TAUGHT)  # Friday 18:00-21:00
    assert rhythm.limit(gaps, 38) >= 60 * 3600
    assert rhythm.limit(gaps, 4) == 900


def test_thin_slot_falls_back_to_whole_device():
    gaps = {}
    rhythm.add(gaps, 2, rhythm.band_of(3600), 40)
    assert rhythm.limit(gaps, 30) == rhythm.limit(gaps, 2)


def test_limit_none_when_nothing_recurs():
    assert rhythm.limit({}, 0) is None
    assert rhythm.limit({"0:3": 2.0}, 0) is None


def test_learned_needs_7_days_and_30_gaps():
    now = 1_800_000_000.0
    assert not rhythm.learned(40, now - 6 * 86400, now)
    assert not rhythm.learned(29, now - 30 * 86400, now)
    assert rhythm.learned(30, now - 7 * 86400, now)
    assert not rhythm.learned(40, None, now)


def test_decay_and_drop():
    gaps = {"0:3": 1.0, "0:4": 0.051}
    rhythm.decay(gaps)
    assert gaps == {"0:3": pytest.approx(0.98)}


def test_clean_drops_rubbish():
    assert rhythm.clean(
        {"0:3": 2, "99:1": 1, "0:40": 1, "x": 1, "1:1": float("nan"), "2:2": -1, "3:3": "5", "4:4": 10**400}
    ) == {"0:3": 2.0}
    assert rhythm.clean([1, 2]) == {} and rhythm.clean(None) == {}


def test_limit_only_reads_own_table():
    gaps = {f"{s}:{b}": 5.0 for s in range(56) for b in range(24)}
    assert rhythm.limit(gaps, 0) is None  # weight spread over every band: not steady
    for s in range(56):
        rhythm.add(gaps, s, 22, 2000)
    assert rhythm.limit(gaps, 0) == rhythm.MAX_LIMIT


def test_describe():
    now = 1_800_000_000.0
    d = rhythm.describe(day_night_table(), now - 41 * 86400, now, 1683)
    assert (
        d["learned"] and d["steady"] and d["gaps"] == 1683 and d["days"] == 41 and d["usual"][0] <= 300 <= d["usual"][1]
    )
    assert d["longest"] >= 9 * 3600 and set(d["sleep_slots"]) == {7, 15, 23}
    assert d["need_days"] == 7 and d["need_gaps"] == 30


def test_describe_empty():
    d = rhythm.describe({}, None, 1_800_000_000.0)
    assert d["learned"] is False and d["days"] == 0 and d["gaps"] == 0
    assert d["usual"] is None and d["longest"] is None and d["sleep_slots"] == []
    assert d["steady"] is False


def log_uniform_table(seed: int = 7, n: int = 420) -> dict:
    """An event-driven device: gaps spread log-uniformly from 1 minute to 20 hours."""
    rng = random.Random(seed)
    gaps = {}
    for _ in range(n):
        gap = math.exp(rng.uniform(math.log(60), math.log(20 * 3600)))
        rhythm.add(gaps, rng.randrange(56), rhythm.band_of(gap), 1.0)
    return gaps


def test_steady_periodic_reporter():
    assert rhythm.steady(day_night_table())
    gaps = {}
    for slot in range(56):  # jitter between 4 and 6 minutes spans neighbouring bands
        for seconds in (240, 300, 360):
            rhythm.add(gaps, slot, rhythm.band_of(seconds), 10)
    assert rhythm.steady(gaps)


def test_irregular_device_is_not_steady_and_has_no_limit():
    gaps = log_uniform_table()
    assert not rhythm.steady(gaps)
    assert all(rhythm.limit(gaps, slot) is None for slot in range(56))
    now = 1_800_000_000.0
    d = rhythm.describe(gaps, now - 84 * 86400, now, 420)
    assert d["learned"] and not d["steady"]


def test_steady_needs_80_percent_within_one_band_of_the_median():
    assert rhythm.STEADY_SHARE == 0.8
    assert rhythm.steady({"0:10": 8.0, "0:11": 1.0, "0:20": 1.0})  # 90 % in bands 9..11
    assert not rhythm.steady({"0:10": 7.0, "0:13": 2.0, "0:20": 1.0})  # 70 %
    assert not rhythm.steady({})


def test_describe_and_limit_agree_on_recurring_bands():
    gaps = {}
    rhythm.add(gaps, 2, rhythm.band_of(3600), 40)
    for slot in (2, 20, 30):  # one each in three groups: only the whole table recurs at 6000 s
        rhythm.add(gaps, slot, rhythm.band_of(6000), 1)
    now = 1_800_000_000.0
    d = rhythm.describe(gaps, now - 10 * 86400, now, 43)
    assert d["longest"] == rhythm.BANDS[rhythm.band_of(6000)]
    assert rhythm.limit(gaps, 21) == int(d["longest"] * rhythm.MARGIN)


def test_sleep_slots_need_weight_1():
    gaps = day_night_table()
    rhythm.add(gaps, 40, rhythm.band_of(9 * 3600), 0.3)  # a decayed leftover
    now = 1_800_000_000.0
    assert set(rhythm.describe(gaps, now - 41 * 86400, now, 1683)["sleep_slots"]) == {7, 15, 23}


def test_slot_of_across_dst_changes():
    # Europe/London: clocks go forward 01:00 -> 02:00 on Sun 29 Mar 2026 and back 02:00 -> 01:00 on Sun 25 Oct 2026.
    for day in (datetime(2026, 3, 29), datetime(2026, 10, 25)):
        sunday = 6 * 8
        for hour in (0, 3, 4, 12, 23):
            ts = datetime(day.year, day.month, day.day, hour, 30, tzinfo=LONDON).timestamp()
            assert rhythm.slot_of(ts, LONDON) == sunday + hour // 3
    before = datetime(2026, 3, 29, 0, 59, tzinfo=LONDON).timestamp()
    assert rhythm.slot_of(before + 3600, LONDON) == 48  # 02:59 BST: still Sunday 00:00-03:00
    assert rhythm.slot_of(before + 2 * 3600, LONDON) == 49  # 03:59 BST
    back = datetime(2026, 10, 25, 0, 30, tzinfo=LONDON).timestamp()
    assert [rhythm.slot_of(back + h * 3600, LONDON) for h in range(5)] == [48, 48, 48, 48, 49]  # 01:30 twice


def periodic_table(per_report: int, every: float = 300, days: int = 21) -> dict:
    """A sensor reporting every `every` seconds, each report sent as `per_report` messages about 1 s apart."""
    gaps = {}
    start = datetime(2026, 6, 1, tzinfo=LONDON).timestamp()
    t = start
    while t < start + days * 86400:
        slot = rhythm.slot_of(t, LONDON)
        for _ in range(per_report - 1):
            rhythm.add(gaps, slot, rhythm.band_of(1), 1)
        rhythm.add(gaps, slot, rhythm.band_of(every - (per_report - 1)), 1)
        t += every
    return gaps


@pytest.mark.parametrize("per_report", [1, 2, 3, 5])
def test_several_messages_per_report_stay_steady(per_report):
    gaps = periodic_table(per_report)
    assert rhythm.steady(gaps)
    assert all(rhythm.limit(gaps, slot) == 900 for slot in range(56))


def test_bursts_do_not_make_an_irregular_device_steady():
    gaps = log_uniform_table()
    rhythm.add(gaps, 0, 0, 400)  # many messages under 30 s apart
    assert not rhythm.steady(gaps)


def test_device_reporting_every_10_s_is_steady():
    assert rhythm.steady(periodic_table(1, every=10, days=2))
