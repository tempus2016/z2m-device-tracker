"""Zigbee2MQTT's own health (bridge/health): parsing, the daily memory history and the z2m_memory check."""

from __future__ import annotations

from datetime import date

from conftest import SENSOR, send, z2m_device
from tracker import MEMORY_DAYS, memory_growth, record_memory

# A bridge/health payload as Zigbee2MQTT 2.5+ publishes it (lib/extension/health.ts).
HEALTH = {
    "response_time": 1780315200000,
    "os": {"load_average": [0.42, 0.3, 0.25], "memory_used_mb": 3120.5, "memory_percent": 39.1},
    "process": {"uptime_sec": 3600, "memory_used_mb": 212.34, "memory_percent": 2.66},
    "mqtt": {"connected": True, "queued": 0, "received": 2100, "published": 184000},
    "devices": {SENSOR: {"messages": 40, "messages_per_sec": 0.011, "leave_count": 1, "network_address_changes": 2}},
}


def health(memory: float, uptime: int = 3600) -> dict:
    return {**HEALTH, "process": {**HEALTH["process"], "memory_used_mb": memory, "uptime_sec": uptime}}


def test_parses_bridge_health(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "S")])
    send(tracker, "bridge/health", HEALTH)
    bridge = tracker.data["bridges"]["zigbee2mqtt"]
    h = bridge["health"]
    assert (h["uptime_sec"], h["memory_mb"], h["mqtt_published"], h["mqtt_received"], h["os_load"]) == (
        3600,
        212.34,
        184000,
        2100,
        0.42,
    )
    assert h["mqtt_connected"] is True
    assert bridge["memory_days"] == [["2026-06-01", 212.3]]
    stats = tracker.data["devices"][SENSOR]["z2m_stats"]
    assert (stats["leave_count"], stats["nwk_changes"], stats["messages"]) == (1, 2, 40)


def test_ignores_health_without_memory(tracker):
    send(tracker, "bridge/health", {"process": {"uptime_sec": 10}})
    send(tracker, "bridge/health", "not json")
    bridge = tracker.data["bridges"]["zigbee2mqtt"]
    assert bridge["health"]["uptime_sec"] == 10
    assert "memory_days" not in bridge


def test_daily_history_keeps_max_per_day(tracker, clock):
    send(tracker, "bridge/health", health(200))
    clock.advance(minutes=10)
    send(tracker, "bridge/health", health(250))
    clock.advance(minutes=10)
    send(tracker, "bridge/health", health(220))
    assert tracker.data["bridges"]["zigbee2mqtt"]["memory_days"] == [["2026-06-01", 250]]
    clock.advance(days=1)
    send(tracker, "bridge/health", health(180))
    assert tracker.data["bridges"]["zigbee2mqtt"]["memory_days"] == [["2026-06-01", 250], ["2026-06-02", 180]]


def test_daily_history_trimmed_to_14_days(tracker, clock):
    for _ in range(20):
        send(tracker, "bridge/health", health(200))
        clock.advance(days=1)
    days = tracker.data["bridges"]["zigbee2mqtt"]["memory_days"]
    assert len(days) == MEMORY_DAYS == 14
    assert days[0][0] == "2026-06-07" and days[-1][0] == "2026-06-20"


def test_record_memory_drops_old_days_after_a_gap():
    days = [["2026-05-01", 100], ["2026-05-20", 120]]
    assert record_memory(days, date(2026, 6, 1), 130) == [["2026-05-20", 120], ["2026-06-01", 130]]


def _days(values: list[float], end: date = date(2026, 6, 14)) -> list:
    n = len(values)
    return [[date.fromordinal(end.toordinal() - n + 1 + i).isoformat(), v] for i, v in enumerate(values)]


def test_growth_needs_five_days_in_the_week():
    today = date(2026, 6, 14)
    assert memory_growth(_days([100, 120, 140, 200]), today) is None
    assert memory_growth(_days([100, 120, 140, 170, 200]), today) == {"percent": 100, "days": 4}


def test_growth_threshold_and_window():
    today = date(2026, 6, 14)
    # Only the last 7 days count: the low readings 10+ days ago are outside the window.
    assert memory_growth(_days([50, 50, 50, 100, 105, 110, 115, 120, 125, 130, 140, 145, 148, 150]), today) is None
    assert memory_growth(_days([100, 110, 120, 130, 140, 150, 150]), today) is None  # exactly 50 %
    assert memory_growth(_days([100, 110, 120, 130, 140, 150, 151]), today) == {"percent": 51, "days": 6}


def test_growth_clears_after_restart():
    today = date(2026, 6, 14)
    days = _days([100, 120, 140, 170, 200, 230, 260])
    assert memory_growth(days, today)["percent"] == 160
    assert memory_growth(days, today, current=110) is None


def test_z2m_memory_issue(tracker, clock):
    for mb in (200, 230, 260, 290, 320, 350, 370):
        send(tracker, "bridge/state", "online")
        send(tracker, "bridge/health", health(mb))
        clock.advance(days=1)
    clock.advance(days=-1)
    issues, _ = tracker.evaluate_health()
    assert [(i["kind"], i["severity"], i["name"], i["params"]) for i in issues] == [
        ("z2m_memory", "info", "Main network", {"percent": 85, "days": 6})
    ]
    assert issues[0]["detail"].startswith("Memory up 85 % in 6 days. Restarting Zigbee2MQTT frees it")

    send(tracker, "bridge/health", health(150, uptime=60))
    assert tracker.evaluate_health()[0] == []


def test_z2m_memory_can_be_disabled_and_does_not_notify(make_tracker, clock):
    from health import ALERT_KINDS, DEFAULT_NOTIFY

    assert "z2m_memory" not in ALERT_KINDS and "z2m_memory" not in DEFAULT_NOTIFY
    t = make_tracker(options={"disabled_checks": ["z2m_memory"]})
    for mb in (100, 150, 200, 250, 300):
        send(t, "bridge/health", health(mb))
        clock.advance(days=1)
    assert t.evaluate_health()[0] == []
