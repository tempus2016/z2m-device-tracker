"""Expected outages: a network that goes offline at about the same time most days isn't alerted on."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import expected_outage
from conftest import send

UTC = timezone.utc
NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC).timestamp()


def outage(day: int, hhmm: str, minutes: float = 10, tz=UTC, month: int = 6) -> dict:
    hour, minute = map(int, hhmm.split(":"))
    start = datetime(2026, month, day, hour, minute, tzinfo=tz)
    return {"start": start.isoformat(), "end": (start + timedelta(minutes=minutes)).isoformat()}


NIGHTLY = [outage(day, "03:00") for day in (6, 7, 8, 9)]


def test_four_nights_at_the_same_time_is_a_pattern():
    assert expected_outage.pattern(NIGHTLY, NOW, UTC) == {"start": 180, "at": 180, "minutes": 10, "days": 4}


def test_three_nights_is_not_enough():
    assert expected_outage.pattern(NIGHTLY[:3], NOW, UTC) is None


def test_times_spread_over_the_day_are_not_a_pattern():
    spread = [outage(6, "03:00"), outage(7, "09:00"), outage(8, "15:00"), outage(9, "21:00")]
    assert expected_outage.pattern(spread, NOW, UTC) is None


def test_two_outages_on_one_day_count_as_one_day():
    same_day = [outage(9, "03:00"), outage(9, "03:20"), outage(9, "03:40"), outage(9, "04:00")]
    assert expected_outage.pattern(same_day, NOW, UTC) is None


def test_long_unfinished_and_old_outages_are_left_out():
    long = [outage(day, "03:00", minutes=180) for day in (6, 7, 8, 9)]
    assert expected_outage.pattern(long, NOW, UTC) is None
    unfinished = [{"start": o["start"], "end": None} for o in NIGHTLY]
    assert expected_outage.pattern(unfinished, NOW, UTC) is None
    old = [outage(day, "03:00", month=5) for day in (6, 7, 8, 9)]
    assert expected_outage.pattern(old, NOW, UTC) is None


def test_window_can_cross_midnight():
    late = [outage(6, "23:40"), outage(7, "23:50"), outage(9, "00:10"), outage(10, "00:20")]
    found = expected_outage.pattern(late, NOW, UTC)
    assert found["start"] == 23 * 60 + 40 and found["days"] == 4


def test_usual_time_and_length_are_the_middle_values():
    drifting = [outage(6, "03:00", 8), outage(7, "03:10", 12), outage(8, "03:20", 10), outage(9, "03:30", 30)]
    assert expected_outage.pattern(drifting, NOW, UTC) == {"start": 180, "at": 195, "minutes": 11, "days": 4}


def test_pattern_follows_local_time_across_a_clock_change():
    london = ZoneInfo("Europe/London")  # clocks go back on 25 October 2026
    nights = [outage(day, "03:00", tz=london, month=10) for day in (23, 24, 26, 27)]
    now = datetime(2026, 10, 28, 12, 0, tzinfo=london).timestamp()
    assert expected_outage.pattern(nights, now, london)["start"] == 180
    assert expected_outage.pattern(nights, now, UTC) is None or expected_outage.pattern(nights, now, UTC)["days"] == 4


def test_rubbish_is_ignored():
    for bad in (None, "x", [None, 5, {"start": "nope", "end": "nope"}, {"start": 3}]):
        assert expected_outage.pattern(bad, NOW, UTC) is None


def test_expected_inside_the_window_and_the_grace_after_it():
    found = expected_outage.pattern(NIGHTLY, NOW, UTC)
    began = datetime(2026, 6, 10, 3, 5, tzinfo=UTC).timestamp()
    assert expected_outage.expected(found, began, began + 60, UTC)
    assert expected_outage.expected(found, began, began + 115 * 60, UTC)  # window ends 04:30, grace to 05:00
    assert not expected_outage.expected(found, began, began + 116 * 60, UTC)


def test_not_expected_outside_the_window_or_without_a_pattern():
    found = expected_outage.pattern(NIGHTLY, NOW, UTC)
    afternoon = datetime(2026, 6, 10, 15, 0, tzinfo=UTC).timestamp()
    assert not expected_outage.expected(found, afternoon, afternoon + 60, UTC)
    assert not expected_outage.expected(None, afternoon, afternoon + 60, UTC)
    assert not expected_outage.expected(found, None, afternoon, UTC)


def test_clock_rounds_to_a_quarter_hour():
    assert [expected_outage.clock(m) for m in (180, 187, 188, 1439)] == ["03:00", "03:00", "03:15", "00:00"]


# ------------------------------------------------------------------ in the tracker
def _nightly(tracker, clock, nights: int) -> None:
    """Take the network down at 03:00 for 10 minutes on `nights` nights in a row, ending the morning after."""
    send(tracker, "bridge/state", "online")
    tracker.plan_alerts([], {"network_offline"})  # the first check only records what's already there
    for _ in range(nights):
        clock.now = (
            (datetime.fromtimestamp(clock.now, tz=UTC) + timedelta(days=1)).replace(hour=3, minute=0).timestamp()
        )
        send(tracker, "bridge/state", "offline")
        clock.advance(minutes=10)
        send(tracker, "bridge/state", "online")


def _down_at_three(tracker, clock) -> None:
    clock.now = (datetime.fromtimestamp(clock.now, tz=UTC) + timedelta(days=1)).replace(hour=3, minute=0).timestamp()
    send(tracker, "bridge/state", "offline")
    clock.advance(minutes=5)


def test_the_fifth_night_is_listed_as_expected_and_does_not_notify(make_tracker, clock):
    tracker = make_tracker()
    _nightly(tracker, clock, 4)
    _down_at_three(tracker, clock)
    issues, _ = tracker.evaluate_health()
    issue = next(i for i in issues if i["kind"] == "network_offline")
    assert (issue["severity"], issue["msg"], issue["expected"]) == ("info", "network_expected", True)
    assert issue["title"] == "Expected outage"
    assert issue["detail"].startswith("Goes offline most days around 03:00 for about 10 min.")
    assert tracker.plan_alerts(issues, {"network_offline"})["send"] == []


def test_an_outage_that_outlasts_the_window_alerts(make_tracker, clock):
    tracker = make_tracker()
    _nightly(tracker, clock, 4)
    _down_at_three(tracker, clock)
    issues, _ = tracker.evaluate_health()
    assert tracker.plan_alerts(issues, {"network_offline"})["send"] == []
    clock.advance(hours=2)
    issues, _ = tracker.evaluate_health()
    issue = next(i for i in issues if i["kind"] == "network_offline")
    assert (issue["severity"], issue["msg"]) == ("warning", "network_offline") and "expected" not in issue
    assert [i["key"] for i in tracker.plan_alerts(issues, {"network_offline"})["send"]] == [
        "network_offline:zigbee2mqtt"
    ]


def test_an_outage_at_another_time_alerts(make_tracker, clock):
    tracker = make_tracker()
    _nightly(tracker, clock, 4)
    clock.advance(hours=8)
    send(tracker, "bridge/state", "offline")
    clock.advance(minutes=5)
    issues, _ = tracker.evaluate_health()
    issue = next(i for i in issues if i["kind"] == "network_offline")
    assert issue["severity"] == "warning"
    assert len(tracker.plan_alerts(issues, {"network_offline"})["send"]) == 1


def test_three_nights_still_alert(make_tracker, clock):
    tracker = make_tracker()
    _nightly(tracker, clock, 3)
    _down_at_three(tracker, clock)
    issues, _ = tracker.evaluate_health()
    assert next(i for i in issues if i["kind"] == "network_offline")["severity"] == "warning"


def test_option_off_alerts_every_night(make_tracker, clock):
    tracker = make_tracker(options={"expected_outages": False})
    _nightly(tracker, clock, 4)
    _down_at_three(tracker, clock)
    issues, _ = tracker.evaluate_health()
    assert next(i for i in issues if i["kind"] == "network_offline")["severity"] == "warning"
