"""Ping: timed genBasic read through bridge/request/action, nightly reply-time series and the slow_router check."""

from __future__ import annotations

import json

import health
import metrics as metrics_module
import pytest
import tracker as tracker_module
from conftest import PLUG, ROUTER_A, ROUTER_B, SENSOR, START, coordinator, send, z2m_device
from metrics import Metrics

DAY = 86400
TODAY = int(START // DAY * DAY)


@pytest.fixture
def m(tmp_path) -> Metrics:
    return Metrics(str(tmp_path / "metrics.json"))


def series(values: list[float | None], end: int = TODAY) -> list[list]:
    """Daily points, the last one on `end`."""
    return [[end - (len(values) - 1 - i) * DAY, v] for i, v in enumerate(values)]


# ------------------------------------------------------------------ storage


def test_add_ping_one_value_per_day(m):
    m.add_ping(ROUTER_A, 40.4, now=START)
    m.add_ping(ROUTER_A, 55, now=START + 60)
    m.add_ping(ROUTER_A, None, now=START + DAY)
    assert m.series[ROUTER_A]["ping"] == [[TODAY, 55], [TODAY + DAY, None]]
    assert m.dirty


@pytest.mark.parametrize("value", ["12", True, -5, [1]])
def test_add_ping_ignores_bad_values(m, value):
    m.add_ping(ROUTER_A, value, now=START)
    assert m.series == {}


def test_add_ping_keeps_30_days(m):
    for day in range(40):
        m.add_ping(ROUTER_A, 50, now=START + day * DAY)
    points = m.series[ROUTER_A]["ping"]
    assert len(points) == metrics_module.PING_KEEP_DAYS
    assert points[-1][0] == TODAY + 39 * DAY
    assert points[0][0] == TODAY + 10 * DAY


def test_ping_survives_save_and_load(m, tmp_path):
    m.add_ping(ROUTER_A, 42, now=START)
    m.save()
    again = Metrics(str(tmp_path / "metrics.json"))
    again.load()
    assert again.series[ROUTER_A]["ping"] == [[TODAY, 42]]


def test_summary_chart_is_last_14_days(m):
    for day in range(20):
        m.add_ping(ROUTER_A, 40 + day, now=START - (19 - day) * DAY)
    summary = m.summary(ROUTER_A, now=START)
    daily = summary["ping_daily"]
    assert len(daily) == 14
    assert daily[-1] == [TODAY, 59]
    assert daily[0][0] == TODAY - 13 * DAY
    assert "ping_slow" not in summary


def test_summary_without_pings_has_no_ping_keys(m):
    m.add_lqi(SENSOR, 100, now=START)
    assert "ping_daily" not in m.summary(SENSOR, now=START)


# ------------------------------------------------------------------ slow_router maths


def test_slowdown_flags_router_3x_slower_and_above_150():
    points = series([42] * 14 + [180, 190, 200])
    assert Metrics.ping_slowdown(points, START) == {"from": 42, "to": 190}


def test_slowdown_needs_more_than_150_ms():
    # 6x slower but still fast in absolute terms.
    points = series([20] * 14 + [120, 130, 140])
    assert Metrics.ping_slowdown(points, START) is None


def test_slowdown_needs_more_than_3x():
    points = series([80] * 14 + [230, 240, 235])
    assert Metrics.ping_slowdown(points, START) is None


def test_slowdown_uses_medians_so_one_slow_night_is_ignored():
    points = series([42] * 14 + [40, 900, 45])
    assert Metrics.ping_slowdown(points, START) is None


def test_slowdown_ignores_nights_without_reply():
    points = series([42] * 14 + [None, 190, 200])
    assert Metrics.ping_slowdown(points, START) == {"from": 42, "to": 195}


def test_slowdown_needs_enough_history():
    assert Metrics.ping_slowdown(series([42] * 4 + [190, 200, 210]), START) is None
    assert Metrics.ping_slowdown(series([42] * 14 + [None, None, 200]), START) is None


def test_slowdown_baseline_is_the_14_days_before_the_recent_3():
    # Days older than 17 days don't count: an old slow spell doesn't hide a new one.
    points = series([500] * 10 + [42] * 14 + [190, 190, 190])
    assert Metrics.ping_slowdown(points, START) == {"from": 42, "to": 190}


def test_slowdown_reaches_the_summary(m):
    for i, value in enumerate([42] * 14 + [180, 190, 200]):
        m.add_ping(ROUTER_A, value, now=START - (16 - i) * DAY)
    assert m.summary(ROUTER_A, now=START)["ping_slow"] == {"from": 42, "to": 190}


# ------------------------------------------------------------------ check


def router_record() -> dict:
    return {"ieee": ROUTER_A, "type": "Router", "status": "active", "availability": "online", "readings": {}}


def test_slow_router_issue():
    issues = health.device_issues(router_record(), {"ping_slow": {"from": 42, "to": 190}}, {}, START, [])
    assert [i["kind"] for i in issues] == ["slow_router"]
    issue = issues[0]
    assert issue["severity"] == health.INFO
    assert issue["detail"] == (
        "Getting slower: 42 ms to 190 ms in 2 weeks. Often a failing plug or too many devices on one router."
    )
    assert issue["msg"] == "slow_router"
    assert issue["params"] == {"from_ms": 42, "to_ms": 190}


def test_slow_router_does_not_notify_by_default_and_can_be_turned_off():
    assert "slow_router" not in health.ALERT_KINDS
    assert "slow_router" in {c[0] for c in health.CHECKS}
    assert "slow_router" in health.KIND_LABELS
    issues = health.device_issues(
        router_record(), {"ping_slow": {"from": 42, "to": 190}}, {"disabled_checks": ["slow_router"]}, START, []
    )
    assert issues == []


# ------------------------------------------------------------------ ping over MQTT


class FakeZ2M:
    """Stands in for MQTT + Zigbee2MQTT: records publishes and answers bridge requests."""

    def __init__(self, tracker, reply=None) -> None:
        self.tracker = tracker
        self.sent: list[tuple[str, dict]] = []
        # reply(params) -> response dict (status/error) or None for silence
        self.reply = reply or (lambda params: {"status": "ok", "data": {}})
        tracker.publish = self.publish

    def publish(self, topic: str, payload: dict) -> None:
        self.sent.append((topic, payload))
        response = self.reply(payload.get("params") or {})
        if response is not None:
            kind = topic.split("/bridge/request/", 1)[1]
            send(self.tracker, f"bridge/response/{kind}", {**response, "transaction": payload["transaction"]})


def plug_device(**extra) -> dict:
    fields = {
        "network_address": 0x1A2B,
        "endpoints": {
            "1": {"clusters": {"input": ["genOnOff"], "output": []}},
            "2": {"clusters": {"input": ["genBasic", "genOnOff"], "output": []}},
        },
        **extra,
    }
    return z2m_device(PLUG, "Landing plug", "Router", **fields)


@pytest.fixture
def net(tracker):
    send(tracker, "bridge/info", {"version": "2.7.1"})
    send(
        tracker,
        "bridge/devices",
        [
            coordinator(),
            plug_device(),
            z2m_device(SENSOR, "Hall sensor", network_address=0x0042),
            z2m_device(ROUTER_A, "Router A", "Router", network_address=0x0101),
            z2m_device(ROUTER_B, "Router B", "Router", network_address=0x0102),
        ],
    )
    return tracker


@pytest.fixture
def ticks(monkeypatch):
    """Fake monotonic clock: each call moves on by `step` seconds."""
    state = {"now": 1000.0, "step": 0.184}

    def fake() -> float:
        state["now"] += state["step"]
        return state["now"]

    monkeypatch.setattr(tracker_module, "_monotonic", fake)
    return state


def test_ping_sends_a_genbasic_read_and_times_the_reply(net, ticks):
    z2m = FakeZ2M(net)
    assert net.ping(PLUG) == {"ok": True, "ms": 184}
    topic, payload = z2m.sent[0]
    assert topic == "zigbee2mqtt/bridge/request/action"
    assert payload["action"] == "raw"
    params = payload["params"]
    assert params["ieee_address"] == PLUG
    assert params["network_address"] == 0x1A2B
    assert params["dst_endpoint"] == 2  # the endpoint that has genBasic
    assert params["cluster_key"] == "genBasic"
    assert params["zcl"] == {"frame_type": 0, "direction": 0, "command_key": "read", "payload": [{"attrId": 0}]}
    assert params["timeout"] == 10000
    # Read only: the request carries nothing that writes.
    assert "write" not in json.dumps(payload).lower()


def test_ping_endpoint_falls_back_to_first_then_1(net):
    assert net.addresses[ROUTER_A] == {"nwk": 0x0101, "ep": 1}
    send(
        net,
        "bridge/devices",
        [plug_device(endpoints={"11": {"clusters": {"input": ["genOnOff"]}}, "12": {"clusters": {"input": []}}})],
    )
    assert net.addresses[PLUG]["ep"] == 11


def test_ping_no_response_is_a_timeout(net, monkeypatch):
    monkeypatch.setattr(tracker_module, "PING_TIMEOUT", 0.01)
    monkeypatch.setattr(tracker_module, "PING_GRACE", 0.01)
    FakeZ2M(net, reply=lambda params: None)
    result = net.ping(PLUG)
    assert result["ok"] is False
    assert result["timeout"] is True


def test_ping_z2m_timeout_error_is_no_reply(net):
    FakeZ2M(net, reply=lambda params: {"status": "error", "error": "Timeout - 6699 - 2 - 12 - 0 - 1 after 10000ms"})
    result = net.ping(PLUG)
    assert result["ok"] is False
    assert result["timeout"] is True
    assert result["seconds"] == 10
    assert "Timeout" in result["detail"]


def test_ping_invalid_action_is_an_error_not_a_timeout(net):
    FakeZ2M(net, reply=lambda params: {"status": "error", "error": "Invalid action"})
    result = net.ping(PLUG)
    assert result["ok"] is False
    assert "timeout" not in result
    assert "Invalid action" in result["error"]


@pytest.mark.parametrize(
    ("ieee", "error"),
    [
        (SENSOR, "Battery devices sleep"),
        ("0x00124b0000c00001", "coordinator"),
        ("0xdeadbeef00000000", "Unknown device"),
    ],
)
def test_ping_refuses_devices_it_cannot_ping(net, ieee, error):
    z2m = FakeZ2M(net)
    result = net.ping(ieee)
    assert result["ok"] is False
    assert error in result["error"]
    assert z2m.sent == []


def test_ping_needs_z2m_2_7(net):
    send(net, "bridge/info", {"version": "2.6.3"})
    z2m = FakeZ2M(net)
    assert "2.7" in net.ping(PLUG)["error"]
    assert z2m.sent == []


def test_snapshot_marks_pingable_devices(net):
    devices = {d["ieee"]: d for d in json.loads(net.snapshot_json())["devices"]}
    assert devices[PLUG]["can_ping"] is True
    assert devices[SENSOR]["can_ping"] is False


# ------------------------------------------------------------------ nightly


def test_nightly_pings_each_online_router_once_spaced_out(net, ticks):
    send(net, "Router B/availability", "offline")
    answered = []

    def reply(params):
        answered.append(params["ieee_address"])
        return {"status": "ok"} if params["ieee_address"] == PLUG else {"status": "error", "error": "Timeout"}

    FakeZ2M(net, reply=reply)
    waits = []
    counts = net.ping_routers(lambda seconds: waits.append(seconds) and False)
    # The plug (router, mains) and Router A; not Router B (offline), the sensor or the coordinator.
    assert sorted(answered) == sorted([PLUG, ROUTER_A])
    assert waits == [tracker_module.PING_SPACING]
    assert counts == {"replied": 1, "no_reply": 1, "skipped": 0}
    assert net.metrics.series[PLUG]["ping"] == [[TODAY, 184]]
    assert net.metrics.series[ROUTER_A]["ping"] == [[TODAY, None]]


def test_nightly_stops_when_asked(net):
    FakeZ2M(net)
    counts = net.ping_routers(lambda seconds: True)
    assert counts["replied"] == 1
