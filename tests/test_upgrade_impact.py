"""Effect of an upgrade: recording Zigbee2MQTT / firmware changes, before and after numbers, and the upgrade_worse check."""

from __future__ import annotations

import json

import pytest
import upgrade_impact as ui
from conftest import ROUTER_A, SENSOR, coordinator, events_of, send, z2m_device
from health import ALERT_KINDS, CHECKS, DEFAULT_NOTIFY, KIND_LABELS

PLUG = "0x00158d0000600001"
INFO = {"version": "2.5.0", "coordinator": {"type": "zStack3x0", "meta": {"revision": 20230507}}}


def setup_network(tracker):
    send(
        tracker,
        "bridge/devices",
        [coordinator(), z2m_device(ROUTER_A, "Router", "Router"), z2m_device(SENSOR, "Sensor"), z2m_device(PLUG, "Plug")],
    )
    for name in ("Router", "Sensor", "Plug"):
        send(tracker, f"{name}/availability", "online")
    send(tracker, "bridge/info", INFO)


def run_hours(tracker, clock, hours, lqi=130, msgs=10, flaps_per_day=0):
    """Advance the clock hour by hour, recording link quality and messages for every device."""
    for hour in range(hours):
        for ieee in (ROUTER_A, SENSOR, PLUG):
            tracker.metrics.add_lqi(ieee, lqi, now=clock.now)
            for _ in range(msgs):
                tracker.metrics.add_message(ieee, now=clock.now)
        if flaps_per_day and hour % (24 // flaps_per_day) == 0:
            send(tracker, "Sensor/availability", "offline")
            clock.advance(minutes=5)
            send(tracker, "Sensor/availability", "online")
            clock.advance(minutes=55)
        else:
            clock.advance(hours=1)


def upgrade(tracker, version="2.6.0", firmware=20230507):
    send(tracker, "bridge/info", {**INFO, "version": version, "coordinator": {**INFO["coordinator"], "meta": {"revision": firmware}}})


# ------------------------------------------------------------------ pure comparison


@pytest.mark.parametrize(
    ("key", "before", "after", "status"),
    [
        ("offline_per_day", 0.3, 2.1, "worse"),
        ("offline_per_day", 0.3, 1.2, "same"),  # up by less than 1 a day
        ("offline_per_day", 3.0, 4.5, "same"),  # up by 1.5 but not doubled
        ("offline_per_day", 2.1, 0.3, "better"),
        ("lqi", 132, 118, "worse"),
        ("lqi", 132, 125, "same"),
        ("lqi", 110, 125, "better"),
        ("msg_per_hour", 410, 405, "same"),
        ("msg_per_hour", 410, 250, "worse"),
        ("msg_per_hour", 20, 13, "same"),  # 35 % down but only 7 an hour
        ("msg_per_hour", 410, 600, "changed"),
        ("leaves_per_day", 0, 0.6, "worse"),
        ("leaves_per_day", 0, 0.4, "same"),
        ("parent_changes_per_scan", 1, 6, "worse"),
        ("parent_changes_per_scan", 4, 5.5, "same"),
        ("parent_changes_per_scan", 6, 1, "better"),
    ],
)
def test_compare_metric_thresholds(key, before, after, status):
    assert ui.compare_metric(key, before, after) == status


def test_window_stats_leaves_out_what_is_not_recorded():
    start, end = 0.0, 2 * 86400.0
    records = {
        SENSOR: {"instance": "zigbee2mqtt", "type": "EndDevice", "availability": None, "outages": []},
    }
    series = {SENSOR: {"msg": [[3600, 5], [7200, 5]], "lqi": [[3600, 100.0, 1]]}}
    stats = ui.window_stats("zigbee2mqtt", start, end, records, series, [], [])
    assert stats["lqi"] == 100.0 and "msg_per_hour" in stats
    # No availability reporting, no routing scans: no offline count, no parent changes.
    assert "offline_per_day" not in stats and "parent_changes_per_scan" not in stats
    # The event list was trimmed after the window began: leaves are unknown.
    assert "leaves_per_day" not in ui.window_stats("zigbee2mqtt", start, end, records, series, [], [], events_from=7200)
    assert ui.window_stats("zigbee2mqtt", start, end, records, {}, [], []) is None


def test_window_stats_narrows_to_recorded_data_and_counts_parent_changes():
    records = {SENSOR: {"instance": "zigbee2mqtt", "type": "EndDevice", "availability": "online", "outages": []}}
    day = 86400
    series = {SENSOR: {"msg": [[h * 3600, 24] for h in range(48, 72)]}}
    scans = [
        {"at": "1970-01-01T12:00:00+00:00", "parents": {SENSOR: [ROUTER_A, 100]}},
        {"at": "1970-01-03T06:00:00+00:00", "parents": {SENSOR: [PLUG, 90]}},
        {"at": "1970-01-03T12:00:00+00:00", "parents": {SENSOR: [ROUTER_A, 90]}},
    ]
    stats = ui.window_stats("zigbee2mqtt", 0, 3 * day, records, series, [], scans)
    # Data only on the third day: one day is compared, not three.
    assert stats["days"] == 1 and stats["msg_per_hour"] == 24
    assert stats["parent_changes_per_scan"] == 1 and stats["scans"] == 2


def test_clean_upgrades_drops_bad_entries():
    clean = ui.clean_upgrades(
        {
            "zigbee2mqtt": [
                {"at": "2026-06-01T12:00:00+00:00", "changes": [{"what": "zigbee2mqtt", "from": "1", "to": "2"}]},
                {"at": "not a time", "changes": [{"what": "zigbee2mqtt", "from": "1", "to": "2"}]},
                {"at": "2026-06-02T12:00:00+00:00", "changes": [{"what": "<script>", "from": "1", "to": "2"}]},
                "junk",
            ],
            "other": "junk",
        }
    )
    assert list(clean) == ["zigbee2mqtt"] and len(clean["zigbee2mqtt"]) == 1
    assert clean["zigbee2mqtt"][0]["before"] is None


# ------------------------------------------------------------------ tracker


def test_upgrade_recorded_with_week_before(tracker, clock):
    setup_network(tracker)
    run_hours(tracker, clock, 7 * 24)
    upgrade(tracker)
    [item] = tracker.data["upgrades"]["zigbee2mqtt"]
    assert item["changes"] == [{"what": "zigbee2mqtt", "from": "2.5.0", "to": "2.6.0"}]
    assert item["after"] is None
    before = item["before"]
    assert before["days"] == pytest.approx(7, abs=0.05)
    assert before["lqi"] == 130 and before["msg_per_hour"] == 30
    assert before["offline_per_day"] == 0 and before["leaves_per_day"] == 0

    # Numbers wait for a day after the upgrade.
    clock.advance(hours=12)
    latest = tracker.upgrade_impact(clock.now)["zigbee2mqtt"]["latest"]
    assert latest["ready"] is False and latest["metrics"] == []


def test_firmware_change_within_an_hour_joins_the_upgrade(tracker, clock):
    setup_network(tracker)
    run_hours(tracker, clock, 48)
    upgrade(tracker, version="2.5.0", firmware=20240710)
    assert [e["detail"] for e in events_of(tracker, "coordinator firmware updated")] == ["20230507 → 20240710"]
    clock.advance(minutes=20)
    upgrade(tracker, version="2.6.1", firmware=20240710)
    [item] = tracker.data["upgrades"]["zigbee2mqtt"]
    assert item["changes"] == [
        {"what": "firmware", "from": "20230507", "to": "20240710"},
        {"what": "zigbee2mqtt", "from": "2.5.0", "to": "2.6.1"},
    ]
    clock.advance(hours=2)
    upgrade(tracker, version="2.6.2", firmware=20240710)
    assert len(tracker.data["upgrades"]["zigbee2mqtt"]) == 2


def test_worse_upgrade_shows_table_and_issue(tracker, clock):
    setup_network(tracker)
    run_hours(tracker, clock, 7 * 24, lqi=132, msgs=10)
    upgrade(tracker)
    run_hours(tracker, clock, 24, lqi=118, msgs=10, flaps_per_day=4)
    latest = tracker.upgrade_impact(clock.now)["zigbee2mqtt"]["latest"]
    assert latest["ready"] and not latest["complete"] and latest["elapsed_days"] == 1
    rows = {m["key"]: (m["before"], m["after"], m["status"]) for m in latest["metrics"]}
    assert rows["lqi"] == (132, 118, "worse")
    assert rows["offline_per_day"][2] == "worse"
    assert rows["msg_per_hour"][2] == "same"
    assert latest["verdict"] == "worse"
    # The check waits for 2 days of the week after.
    assert [i for i in tracker.evaluate_health()[0] if i["kind"] == "upgrade_worse"] == []

    run_hours(tracker, clock, 24, lqi=118, msgs=10, flaps_per_day=4)
    [issue] = [i for i in tracker.evaluate_health()[0] if i["kind"] == "upgrade_worse"]
    assert issue["key"] == "upgrade_worse:zigbee2mqtt" and issue["severity"] == "info"
    assert issue["detail"].startswith("Since Zigbee2MQTT 2.6.0 (8 Jun): devices going offline per day 0 → ")
    assert "average link quality 132 → 118" in issue["detail"]
    assert {m["key"] for m in issue["params"]["metrics"]} == {"offline_per_day", "lqi"}

    snapshot = json.loads(tracker.snapshot_json())
    assert snapshot["upgrade_impact"]["zigbee2mqtt"]["latest"]["verdict"] == "worse"


def test_upgrade_worse_is_show_only_and_can_be_disabled(make_tracker, clock):
    assert "upgrade_worse" not in ALERT_KINDS and "upgrade_worse" not in DEFAULT_NOTIFY
    assert "upgrade_worse" in KIND_LABELS and any(c[0] == "upgrade_worse" for c in CHECKS)
    tracker = make_tracker(options={"disabled_checks": ["upgrade_worse"]})
    setup_network(tracker)
    run_hours(tracker, clock, 7 * 24, lqi=132)
    upgrade(tracker)
    run_hours(tracker, clock, 72, lqi=100)
    assert tracker.upgrade_impact(clock.now)["zigbee2mqtt"]["latest"]["verdict"] == "worse"
    assert [i for i in tracker.evaluate_health()[0] if i["kind"] == "upgrade_worse"] == []


def test_after_week_is_stored_and_next_upgrade_lists_earlier(tracker, clock):
    setup_network(tracker)
    run_hours(tracker, clock, 7 * 24)
    upgrade(tracker)
    run_hours(tracker, clock, 7 * 24 + 2)
    latest = tracker.upgrade_impact(clock.now)["zigbee2mqtt"]["latest"]
    assert latest["complete"] and latest["verdict"] == "same"
    stored = tracker.data["upgrades"]["zigbee2mqtt"][0]["after"]
    assert stored["days"] == pytest.approx(7, abs=0.05) and stored["lqi"] == 130

    run_hours(tracker, clock, 24)
    upgrade(tracker, version="2.7.0")
    result = tracker.upgrade_impact(clock.now)["zigbee2mqtt"]
    assert result["latest"]["changes"][0]["to"] == "2.7.0"
    assert [(e["changes"][0]["to"], e["verdict"]) for e in result["earlier"]] == [("2.6.0", "same")]
    # The second upgrade's "before" starts after the first one settled.
    assert tracker.data["upgrades"]["zigbee2mqtt"][1]["before"]["days"] == pytest.approx(7, abs=0.05)


def test_next_upgrade_cuts_the_previous_after_week_short(tracker, clock):
    setup_network(tracker)
    run_hours(tracker, clock, 7 * 24)
    upgrade(tracker)
    run_hours(tracker, clock, 3 * 24)
    upgrade(tracker, version="2.6.1")
    first = tracker.data["upgrades"]["zigbee2mqtt"][0]["after"]
    assert first["days"] == pytest.approx(3 - 1 / 24, abs=0.05)


def test_upgrades_capped(tracker, clock):
    setup_network(tracker)
    for n in range(ui.MAX_UPGRADES + 3):
        run_hours(tracker, clock, 2)
        upgrade(tracker, version=f"3.0.{n}")
    upgrades = tracker.data["upgrades"]["zigbee2mqtt"]
    assert len(upgrades) == ui.MAX_UPGRADES and upgrades[-1]["changes"][0]["to"] == f"3.0.{ui.MAX_UPGRADES + 2}"


def test_backup_and_restore_keep_upgrades(tracker, clock, make_tracker):
    setup_network(tracker)
    run_hours(tracker, clock, 48)
    upgrade(tracker)
    payload = json.loads(tracker.backup())
    other = make_tracker()
    assert other.restore(payload)["ok"]
    assert other.data["upgrades"] == tracker.data["upgrades"]

    payload["history"]["upgrades"] = ["not", "a", "dict"]
    third = make_tracker()
    assert third.restore(payload)["ok"]
    assert "upgrades" not in third.data

    payload["history"]["upgrades"] = {"zigbee2mqtt": [{"at": 5, "changes": "x"}]}
    assert third.restore(payload)["ok"]
    assert third.data["upgrades"] == {}


def test_upgrades_survive_save_and_load(tracker, clock, make_tracker):
    setup_network(tracker)
    run_hours(tracker, clock, 48)
    upgrade(tracker)
    tracker.save(force=True)
    loaded = make_tracker()
    loaded.load()
    assert loaded.data["upgrades"] == tracker.data["upgrades"]
