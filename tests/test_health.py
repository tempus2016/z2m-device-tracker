"""health.device_issues: one test group per check, plus mutes and uptime."""

from __future__ import annotations

from datetime import datetime, timezone

import health
import pytest
from conftest import START

IEEE = "0x00158d0000500001"


def iso(seconds_ago: float) -> str:
    return datetime.fromtimestamp(START - seconds_ago, tz=timezone.utc).isoformat(timespec="seconds")


def record(**overrides) -> dict:
    base = {
        "ieee": IEEE,
        "type": "EndDevice",
        "status": "active",
        "availability": "online",
        "availability_since": iso(86400),
        "last_seen": iso(60),
        "interview_state": "SUCCESSFUL",
        "readings": {},
        "battery": 90,
        "battery_low": False,
        "update": None,
        "lost_bindings": [],
        "muted": {},
    }
    base.update(overrides)
    return base


def kinds(rec: dict, summary: dict | None = None, options: dict | None = None, suggestions=None) -> list[str]:
    issues = health.device_issues(rec, summary or {}, options or {}, START, suggestions or [])
    return [i["kind"] for i in issues]


def test_healthy_device_has_no_issues():
    assert kinds(record()) == []


def test_issue_shape():
    issues = health.device_issues(record(battery=5), {}, {}, START, [])
    assert issues == [
        {
            "key": f"battery_low:{IEEE}",
            "kind": "battery_low",
            "label": "Low battery",
            "severity": health.WARNING,
            "ieee": IEEE,
            "title": "Low battery",
            "detail": "Battery at 5%.",
        }
    ]


@pytest.mark.parametrize("rec", [record(type="Coordinator", battery=1), record(status="removed", battery=1)])
def test_coordinator_and_removed_devices_are_skipped(rec):
    assert kinds(rec) == []


# ------------------------------------------------------------------ offline / silent


def test_offline_after_threshold():
    rec = record(availability="offline", availability_since=iso(61 * 60))
    issues = health.device_issues(rec, {}, {}, START, [])
    assert [i["kind"] for i in issues] == ["offline"]
    assert issues[0]["detail"] == "Unavailable for 1 h."


def test_offline_within_threshold_is_quiet():
    assert kinds(record(availability="offline", availability_since=iso(59 * 60))) == []


def test_offline_threshold_option():
    rec = record(availability="offline", availability_since=iso(10 * 60))
    assert kinds(rec, options={"offline_alert_minutes": 5}) == ["offline"]
    assert kinds(rec, options={"offline_alert_minutes": 0}) == ["offline"]


def test_offline_without_since_never_alerts():
    # No availability_since means age 0, which is below any non-zero threshold.
    assert kinds(record(availability="offline", availability_since=None)) == []


def test_silent_only_without_availability_reporting():
    assert kinds(record(availability=None, last_seen=iso(3 * 86400))) == ["silent"]
    assert kinds(record(availability=None, last_seen=iso(86400))) == []
    assert kinds(record(availability=None, last_seen=None)) == []
    # A device with availability reporting is judged by that, not by last_seen.
    assert kinds(record(availability="online", last_seen=iso(30 * 86400))) == []


# ------------------------------------------------------------------ interview


@pytest.mark.parametrize("state", ["FAILED", "IN_PROGRESS", "PENDING"])
def test_unidentified(state):
    assert kinds(record(interview_state=state)) == ["unidentified"]


def test_missing_interview_state_is_not_unidentified():
    assert kinds(record(interview_state=None)) == []


# ------------------------------------------------------------------ frozen


def frozen_reading(hours: float, reports: int) -> dict:
    return {"temperature": {"value": 21.5, "changed_at": iso(hours * 3600), "reports": reports}}


def test_frozen_reading():
    issues = health.device_issues(record(readings=frozen_reading(25, 30)), {}, {}, START, [])
    assert [i["kind"] for i in issues] == ["frozen"]
    assert "temperature stuck at 21.5" in issues[0]["detail"]
    assert "30 reports" in issues[0]["detail"]


def test_frozen_needs_enough_reports_and_time():
    assert kinds(record(readings=frozen_reading(25, 19))) == []
    assert kinds(record(readings=frozen_reading(23, 50))) == []


def test_frozen_hours_option_and_disable():
    rec = record(readings=frozen_reading(5, 30))
    assert kinds(rec, options={"frozen_alert_hours": 4}) == ["frozen"]
    assert kinds(record(readings=frozen_reading(500, 500)), options={"frozen_alert_hours": 0}) == []


def test_frozen_reports_once_for_several_readings():
    readings = {**frozen_reading(30, 40), "humidity": {"value": 50, "changed_at": iso(30 * 3600), "reports": 40}}
    assert kinds(record(readings=readings)) == ["frozen"]


# ------------------------------------------------------------------ signal


def test_weak_signal():
    issues = health.device_issues(record(), {"lqi_24h": 22.4}, {}, START, [])
    assert [i["kind"] for i in issues] == ["weak_signal"]
    assert issues[0]["detail"] == "Average link quality 22 over 24 h."


def test_signal_drop():
    issues = health.device_issues(record(), {"lqi_24h": 50, "lqi_prev_7d": 120}, {}, START, [])
    assert [(i["kind"], i["severity"]) for i in issues] == [("signal_drop", health.INFO)]


@pytest.mark.parametrize(
    "summary",
    [
        {"lqi_24h": 85, "lqi_prev_7d": 200},  # dropped a lot but still good
        {"lqi_24h": 70, "lqi_prev_7d": 100},  # under 40% drop
        {"lqi_24h": 70},  # no history to compare
        {"lqi_24h": 30},  # exactly on the weak threshold
    ],
)
def test_signal_ok(summary):
    assert kinds(record(), summary) == []


def test_weak_signal_wins_over_drop():
    assert kinds(record(), {"lqi_24h": 10, "lqi_prev_7d": 200}) == ["weak_signal"]


# ------------------------------------------------------------------ battery


def test_low_battery_threshold_and_option():
    assert kinds(record(battery=15)) == ["battery_low"]
    assert kinds(record(battery=16)) == []
    assert kinds(record(battery=25), options={"battery_alert_percent": 30}) == ["battery_low"]


def test_battery_low_flag_without_percentage():
    issues = health.device_issues(record(battery=None, battery_low=True), {}, {}, START, [])
    assert issues[0]["detail"] == "Device reports low battery."


def test_battery_soon():
    assert kinds(record(), {"battery_estimate_days": 30}) == ["battery_soon"]
    assert kinds(record(), {"battery_estimate_days": 46}) == []


def test_battery_low_suppresses_battery_soon():
    assert kinds(record(battery=5), {"battery_estimate_days": 3}) == ["battery_low"]


def test_fast_drain():
    drain = {"days": 14, "dropped": 20.0, "rate": 1.43, "usual_rate": 0.1, "factor": 14.3}
    summary = {"battery_drain": drain, "parent": {"name": "Hall plug", "lqi": 40}}
    issues = health.device_issues(record(), summary, {}, START, [])
    assert [i["kind"] for i in issues] == ["battery_drain"]
    assert "Dropped 20% in the last 14 days" in issues[0]["detail"]
    assert "through Hall plug (LQI 40)" in issues[0]["detail"]


# ------------------------------------------------------------------ firmware


def test_update_available():
    rec = record(update={"state": "available", "installed_version": "1", "latest_version": "2"})
    issues = health.device_issues(rec, {}, {}, START, [])
    assert [(i["kind"], i["detail"]) for i in issues] == [("update", "1 → 2")]
    assert kinds(record(update={"state": "idle"})) == []


# ------------------------------------------------------------------ flapping


def test_flapping_from_outages():
    issues = health.device_issues(record(), {"outages_24h": 4, "leaves_24h": 0}, {}, START, [])
    assert [i["kind"] for i in issues] == ["flapping"]
    assert "went offline 4 times" in issues[0]["detail"]
    assert "left the network" not in issues[0]["detail"]


def test_flapping_from_leaves():
    issues = health.device_issues(record(), {"outages_24h": 1, "leaves_24h": 2}, {}, START, [])
    assert "went offline 1 times and left the network 2 times" in issues[0]["detail"]


def test_not_flapping_below_limits():
    assert kinds(record(), {"outages_24h": 3, "leaves_24h": 1}) == []


# ------------------------------------------------------------------ chatty


def test_chatty_absolute():
    issues = health.device_issues(record(), {"msg_rate_6h": 1500}, {}, START, [])
    assert [(i["kind"], i["severity"]) for i in issues] == [("chatty", health.WARNING)]


def test_chatty_option():
    assert kinds(record(), {"msg_rate_6h": 150}, {"chatty_per_hour": 100}) == ["chatty"]
    # 0 or missing falls back to the default rather than alerting on everything.
    assert kinds(record(), {"msg_rate_6h": 150}, {"chatty_per_hour": 0}) == []


def test_chatty_relative_to_usual():
    issues = health.device_issues(record(), {"msg_rate_6h": 400, "msg_rate_prev": 50}, {}, START, [])
    assert [(i["kind"], i["severity"]) for i in issues] == [("chatty", health.INFO)]


@pytest.mark.parametrize(
    "summary", [{"msg_rate_6h": 300, "msg_rate_prev": 10}, {"msg_rate_6h": 400, "msg_rate_prev": 100}]
)
def test_not_chatty(summary):
    assert kinds(record(), summary) == []


# ------------------------------------------------------------------ bindings


def lost_binding(seconds_ago: float, coordinator: bool = False, n: int = 1) -> dict:
    return {
        "key": f"1:genOnOff>0x00158d0000600001/{n}",
        "label": f"genOnOff → Lamp {n}",
        "coordinator": coordinator,
        "lost_at": iso(seconds_ago),
    }


def test_lost_binding_after_grace():
    issues = health.device_issues(record(lost_bindings=[lost_binding(700)]), {}, {}, START, [])
    assert [i["kind"] for i in issues] == ["binding_lost"]
    assert "1 binding gone" in issues[0]["detail"]
    assert "Bind tab" in issues[0]["detail"]


def test_lost_binding_within_grace_is_quiet():
    assert kinds(record(lost_bindings=[lost_binding(health.BINDING_GRACE_SECONDS - 1)])) == []


def test_lost_coordinator_binding_suggests_reconfigure():
    lost = [lost_binding(700, coordinator=True, n=i) for i in range(6)]
    issues = health.device_issues(record(lost_bindings=lost), {}, {}, START, [])
    assert "6 bindings gone" in issues[0]["detail"]
    assert "Reconfigure" in issues[0]["detail"]
    assert "…" in issues[0]["detail"]


# ------------------------------------------------------------------ single point of failure


def test_single_point():
    spof = {"routers": [{"name": "Shed plug"}, {"name": "Garage plug"}], "devices": 3}
    issues = health.device_issues(record(type="Router"), {"single_point": spof}, {}, START, [])
    assert [(i["kind"], i["severity"]) for i in issues] == [("single_point", health.INFO)]
    assert "2 routers (Shed plug, Garage plug), plus 3 battery devices" in issues[0]["detail"]


# ------------------------------------------------------------------ replacement


def test_replacement_suggestion_only_first():
    suggestions = [{"ieee": "0x1", "name": "Old A"}, {"ieee": "0x2", "name": "Old B"}]
    issues = health.device_issues(record(), {}, {}, START, suggestions)
    assert [i["kind"] for i in issues] == ["replacement"]
    assert "Old A" in issues[0]["detail"]


# ------------------------------------------------------------------ muting / disabling


def test_disabled_checks_option():
    assert kinds(record(battery=5), options={"disabled_checks": ["battery_low"]}) == []


def test_mute_forever_old_list_format():
    assert kinds(record(battery=5, muted=["battery_low"])) == []


def test_mute_until_expires():
    assert kinds(record(battery=5, muted={"battery_low": iso(-3600)})) == []
    assert kinds(record(battery=5, muted={"battery_low": iso(3600)})) == ["battery_low"]


def test_mutes_of_and_active_mutes():
    rec = {"muted": {"a": None, "b": iso(-60), "c": iso(60), "d": "not a date"}}
    assert health.mutes_of(rec) == rec["muted"]
    assert health.mutes_of({"muted": ["x"]}) == {"x": None}
    assert health.mutes_of({}) == {}
    assert sorted(health.active_mutes(rec, START)) == ["a", "b"]


# ------------------------------------------------------------------ catalogue consistency


def test_every_device_kind_has_a_label_and_a_check_entry():
    check_keys = {key for key, *_ in health.CHECKS}
    for kind in health.KIND_LABELS:
        assert kind in check_keys
    assert health.ALERT_KINDS <= check_keys
    assert set(health.DEFAULT_NOTIFY) <= check_keys


def test_span():
    assert health._span(59) == "0 min"
    assert health._span(3599) == "59 min"
    assert health._span(3600) == "1 h"
    assert health._span(172799) == "47 h"
    assert health._span(172800) == "2 days"


# ------------------------------------------------------------------ uptime


def test_uptime_window_and_open_outage():
    rec = {
        "first_seen": iso(10 * 86400),
        "outages": [
            {"start": iso(5 * 86400), "end": iso(5 * 86400 - 8640)},  # 0.1 day
            {"start": iso(8640), "end": None},  # still down, 0.1 day so far
        ],
    }
    # 0.2 days down out of 10.
    assert health.uptime(rec, START) == 98.0


def test_uptime_clips_to_window_and_ignores_bad_rows():
    rec = {
        "first_seen": iso(400 * 86400),
        "outages": [
            {"start": iso(40 * 86400), "end": iso(29 * 86400)},  # only 1 day inside the 30-day window
            {"start": "garbage", "end": None},
            {"end": iso(1)},
        ],
    }
    assert health.uptime(rec, START) == pytest.approx(100 * (1 - 1 / 30), abs=0.01)


def test_uptime_needs_an_hour_of_history():
    assert health.uptime({"first_seen": iso(1800), "outages": []}, START) is None
