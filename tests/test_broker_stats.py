"""Mosquitto $SYS statistics: latest values, the 24 h message history and dropped messages."""

from __future__ import annotations

from broker_stats import WAIT_SECONDS, BrokerStats

T0 = 1_780_000_000.0 // 3600 * 3600  # on the hour


def sys(stats: BrokerStats, key: str, value, now: float) -> None:
    stats.handle(f"$SYS/broker/{key}", str(value).encode(), now)


def test_states_waiting_none_ok_stale_off():
    stats = BrokerStats()
    assert stats.snapshot(now=T0)["state"] == "waiting"
    stats.subscribed(T0)
    assert stats.snapshot(now=T0 + 30)["state"] == "waiting"
    # Not Mosquitto, or the login can't read $SYS.
    assert stats.snapshot(now=T0 + WAIT_SECONDS)["state"] == "none"
    sys(stats, "clients/connected", 6, T0 + 70)
    assert stats.snapshot(now=T0 + 80)["state"] == "ok"
    assert stats.snapshot(now=T0 + 600)["state"] == "stale"
    assert stats.snapshot(enabled=False, now=T0 + 80)["state"] == "off"


def test_values_are_parsed():
    stats = BrokerStats()
    sys(stats, "version", "mosquitto version 2.0.18", T0)
    sys(stats, "uptime", "954321 seconds", T0)
    sys(stats, "clients/connected", "6", T0)
    sys(stats, "retained messages/count", "1284", T0)
    sys(stats, "load/messages/received/1min", "41.37", T0)
    sys(stats, "subscriptions/count", "not a number", T0)
    sys(stats, "log/N", "lots of log lines", T0)
    stats.handle("zigbee2mqtt/bridge/state", b"online", T0)
    snap = stats.snapshot(now=T0)
    assert snap["version"] == "mosquitto version 2.0.18"
    assert snap["uptime"] == 954321
    assert snap["clients"] == 6 and snap["retained"] == 1284
    assert isinstance(snap["clients"], int)
    assert snap["received_1min"] == 41.37
    assert "subscriptions" not in snap


def test_hourly_messages_per_minute_from_the_counter():
    stats = BrokerStats()
    total = 10_000
    sys(stats, "messages/received", total, T0)
    for i in range(1, 6):  # 400 messages every 10 minutes: 40 a minute
        total += 400
        sys(stats, "messages/received", total, T0 + i * 600)
    snap = stats.snapshot(now=T0 + 3600)
    [hour] = snap["hours"]
    assert hour["hour"] == T0
    assert hour["per_min"] == 40.0
    # Next hour, a broker restart resets the counter: everything since counts (300 in 15 min).
    sys(stats, "messages/received", 300, T0 + 3600 + 300)
    hours = stats.snapshot(now=T0 + 3900)["hours"]
    assert [h["hour"] for h in hours] == [T0, T0 + 3600]
    assert hours[1]["per_min"] == 20.0


def test_long_gap_is_not_spread_into_one_hour():
    stats = BrokerStats()
    sys(stats, "messages/received", 1000, T0)
    sys(stats, "messages/received", 900_000, T0 + 5 * 3600)
    assert stats.snapshot(now=T0 + 5 * 3600)["hours"] == []


def test_dropped_messages():
    stats = BrokerStats()
    sys(stats, "publish/messages/dropped", 100, T0)  # first reading: a baseline, not a drop
    assert stats.snapshot(now=T0)["dropped_24h"] == 0
    sys(stats, "publish/messages/dropped", 137, T0 + 600)
    sys(stats, "publish/messages/dropped", 137, T0 + 610)
    snap = stats.snapshot(now=T0 + 700)
    assert snap["dropped_24h"] == 37
    assert snap["last_drop"]["n"] == 37
    assert snap["last_drop"]["at"].endswith("+00:00")
    # A day later they have aged out.
    snap = stats.snapshot(now=T0 + 26 * 3600)
    assert snap["dropped_24h"] == 0 and snap["last_drop"] is None


def test_history_is_bounded_to_24_hours():
    stats = BrokerStats()
    total = 0
    for i in range(0, 48 * 6):
        total += 100
        sys(stats, "messages/received", total, T0 + i * 600)
    assert len(stats.hours) == 24
    assert len(stats.snapshot(now=T0 + 48 * 3600)["hours"]) <= 25


def test_reset_clears_everything():
    stats = BrokerStats()
    sys(stats, "clients/connected", 6, T0)
    sys(stats, "publish/messages/dropped", 1, T0)
    stats.reset()
    snap = stats.snapshot(now=T0)
    assert snap["state"] == "waiting" and "clients" not in snap and snap["hours"] == []
