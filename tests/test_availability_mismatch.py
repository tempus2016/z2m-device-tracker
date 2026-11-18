"""Availability mismatch: Zigbee2MQTT availability that contradicts the device's own messages."""

from __future__ import annotations

from datetime import datetime, timezone

from conftest import PLUG, SENSOR, coordinator, send, z2m_device
from health import availability_mismatch, device_issues, z2m_availability

NOW = 1_780_000_000.0
V2_ON = {"enabled": True, "active": {"timeout": 10}, "passive": {"timeout": 1500}}


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def avail(enabled=True, devices=None):
    config = {"availability": {**V2_ON, "enabled": enabled}, "devices": devices or {}}
    return z2m_availability({"version": "2.6.0", "config": config})


def device(availability="offline", since=NOW - 3600, last_seen=NOW - 60, power="Battery", dtype="EndDevice", **extra):
    return {
        "ieee": SENSOR,
        "instance": "zigbee2mqtt",
        "friendly_name": "Sim Motion Hall",
        "status": "active",
        "type": dtype,
        "power_source": power,
        "availability": availability,
        "availability_since": iso(since),
        "last_seen": iso(last_seen),
        **extra,
    }


def summary(recent=(), z2m=None, rate=None, **extra):
    out = {"recent_messages": list(recent), "z2m_availability": z2m, "started": NOW - 30 * 86400, **extra}
    if rate is not None:
        out["msg_rate_prev"] = rate
    return out


def kinds(record, summ, options=None):
    return [i["kind"] for i in device_issues(record, summ, options or {}, NOW, [])]


# ------------------------------------------------------------ offline but talking


def test_offline_with_three_recent_messages():
    found = availability_mismatch(device(), summary([NOW - 500, NOW - 200, NOW - 30]), {}, NOW)
    assert found["msg"] == "availability_mismatch_offline"
    assert found["params"] == {
        "seconds": 3600,
        "n": 3,
        "ago": 30,
        "topic": "zigbee2mqtt/Sim Motion Hall/availability",
    }


def test_offline_alert_replaced_by_the_mismatch():
    issues = device_issues(device(), summary([NOW - 500, NOW - 200, NOW - 30]), {}, NOW, [])
    assert [i["kind"] for i in issues] == ["availability_mismatch"]
    assert issues[0]["severity"] == "info"
    assert "3 messages came in the last 10 min" in issues[0]["detail"]
    assert "zigbee2mqtt/Sim Motion Hall/availability" in issues[0]["detail"]


def test_two_messages_old_messages_or_before_going_offline_dont_count():
    for recent in ([NOW - 200, NOW - 30], [NOW - 900, NOW - 700, NOW - 30], [NOW - 4000, NOW - 3700, NOW - 30]):
        assert availability_mismatch(device(), summary(recent), {}, NOW) is None
        assert kinds(device(), summary(recent)) == ["offline"]


def test_just_gone_offline_is_not_a_mismatch():
    record = device(since=NOW - 120)
    assert availability_mismatch(record, summary([NOW - 100, NOW - 60, NOW - 10]), {}, NOW) is None


def test_disabled_or_muted_mismatch_keeps_the_offline_alert():
    recent = [NOW - 500, NOW - 200, NOW - 30]
    assert kinds(device(), summary(recent), {"disabled_checks": ["availability_mismatch"]}) == ["offline"]
    assert kinds(device(muted={"availability_mismatch": None}), summary(recent)) == ["offline"]


def test_network_down_or_removed_is_never_a_mismatch():
    recent = [NOW - 500, NOW - 200, NOW - 30]
    assert availability_mismatch(device(), summary(recent, network_down=True), {}, NOW) is None
    assert availability_mismatch(device(status="removed"), summary(recent), {}, NOW) is None


# ------------------------------------------------------------ online but silent


def test_online_untracked_device_silent_for_days():
    record = device("online", last_seen=NOW - 3 * 86400)
    found = availability_mismatch(record, summary(z2m=avail(enabled=False)), {}, NOW)
    assert found["msg"] == "availability_mismatch_untracked"
    assert found["params"]["seconds"] == 3 * 86400
    # Without a week of history it waits 2 days.
    record = device("online", last_seen=NOW - 86400)
    assert availability_mismatch(record, summary(z2m=avail(enabled=False)), {}, NOW) is None


def test_online_untracked_uses_the_usual_gap():
    # Usually 6 messages an hour (10 min gap): 6 h of silence is enough (10 gaps = 100 min, floor 6 h).
    record = device("online", last_seen=NOW - 7 * 3600)
    found = availability_mismatch(record, summary(z2m=avail(enabled=False), rate=6), {}, NOW)
    assert found["msg"] == "availability_mismatch_untracked" and found["params"]["gap"] == 600
    record = device("online", last_seen=NOW - 5 * 3600)
    assert availability_mismatch(record, summary(z2m=avail(enabled=False), rate=6), {}, NOW) is None
    # A device that usually reports every 2 h needs 20 h.
    record = device("online", last_seen=NOW - 10 * 3600)
    assert availability_mismatch(record, summary(z2m=avail(enabled=False), rate=0.5), {}, NOW) is None


def test_online_battery_device_past_the_passive_timeout():
    record = device("online", last_seen=NOW - 30 * 3600)
    found = availability_mismatch(record, summary(z2m=avail(), rate=2), {}, NOW)
    assert found["msg"] == "availability_mismatch_passive"
    # Within Zigbee2MQTT's 25 h (plus an hour): Zigbee2MQTT may still flip it.
    record = device("online", last_seen=NOW - 25.5 * 3600)
    assert availability_mismatch(record, summary(z2m=avail(), rate=2), {}, NOW) is None


def test_online_mains_device_zigbee2mqtt_pings_is_never_flagged():
    record = device("online", last_seen=NOW - 5 * 86400, power="Mains (single phase)", dtype="Router")
    assert availability_mismatch(record, summary(z2m=avail()), {}, NOW) is None


def test_online_skipped_without_z2m_config_or_with_report_interval():
    record = device("online", last_seen=NOW - 5 * 86400)
    assert availability_mismatch(record, summary(z2m=None), {}, NOW) is None
    record = device("online", last_seen=NOW - 5 * 86400, report_every=3600)
    assert availability_mismatch(record, summary(z2m=avail(enabled=False)), {}, NOW) is None


def test_quiet_counts_from_app_start():
    record = device("online", last_seen=NOW - 5 * 86400)
    assert availability_mismatch(record, summary(z2m=avail(enabled=False), started=NOW - 3600), {}, NOW) is None


# ------------------------------------------------------------ through the tracker


def test_tracker_counts_live_messages_only(tracker, clock):
    send(tracker, "bridge/devices", [coordinator(), z2m_device(SENSOR, "Hall"), z2m_device(PLUG, "Plug", "Router")])
    send(tracker, "Hall/availability", {"state": "offline"})
    clock.advance(minutes=30)
    for _ in range(3):
        # Retained states replayed by the broker don't count.
        tracker.handle_message("zigbee2mqtt", "zigbee2mqtt/Hall", b'{"occupancy": false}', True)
    issues, _ = tracker.evaluate_health()
    assert [i["kind"] for i in issues if i["ieee"] == SENSOR] == []
    for _ in range(3):
        clock.advance(minutes=2)
        send(tracker, "Hall", {"occupancy": True})
    issues, _ = tracker.evaluate_health()
    mine = [i for i in issues if i["ieee"] == SENSOR]
    assert [i["kind"] for i in mine] == ["availability_mismatch"]
    assert mine[0]["params"]["topic"] == "zigbee2mqtt/Hall/availability"
    # Zigbee2MQTT catches up: back online, no mismatch.
    send(tracker, "Hall/availability", {"state": "online"})
    issues, _ = tracker.evaluate_health()
    assert [i for i in issues if i["ieee"] == SENSOR] == []
    assert tracker.forget(SENSOR)
    assert SENSOR not in tracker._recent


def test_cleared_retained_availability_resets_state(tracker, clock):
    """Deleting the retained availability topic (an empty message) drops the stale offline state."""
    send(tracker, "bridge/devices", [coordinator(), z2m_device(SENSOR, "Hall")])
    send(tracker, "Hall/availability", {"state": "offline"})
    record = tracker.data["devices"][SENSOR]
    assert record["availability"] == "offline"
    clock.advance(minutes=30)
    send(tracker, "Hall/availability", "")
    assert record["availability"] is None
    assert record["availability_since"] is None
    assert record["outages"][-1]["end"]
    issues, _ = tracker.evaluate_health()
    assert [i for i in issues if i["ieee"] == SENSOR and i["kind"] == "offline"] == []
    # A second clear changes nothing.
    revision = tracker.revision
    send(tracker, "Hall/availability", "")
    assert tracker.revision == revision
