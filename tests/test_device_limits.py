"""Per-device battery and frozen limits: record["limits"] overrides the global options."""

from __future__ import annotations

import health
import pytest
from conftest import send, z2m_device
from test_health import frozen_reading, kinds, record

SENSOR = "0x00158d0000500001"


# ------------------------------------------------------------------ checks


def test_battery_limit_overrides_global():
    rec = record(battery=20, limits={"battery_percent": 25})
    assert kinds(rec) == ["battery_low"]
    assert kinds(rec, options={"battery_alert_percent": 10}) == ["battery_low"]


def test_battery_limit_can_be_lower_than_global():
    rec = record(battery=12, limits={"battery_percent": 5})
    assert kinds(rec, options={"battery_alert_percent": 15}) == []
    assert kinds(record(battery=12), options={"battery_alert_percent": 15}) == ["battery_low"]


def test_battery_limit_zero_still_honours_reported_low():
    assert kinds(record(battery=3, limits={"battery_percent": 0})) == []
    assert kinds(record(battery=None, battery_low=True, limits={"battery_percent": 0})) == ["battery_low"]


def test_frozen_limit_overrides_global():
    rec = record(readings=frozen_reading(30, 50), limits={"frozen_hours": 96})
    assert kinds(rec) == []
    assert kinds(record(readings=frozen_reading(100, 50), limits={"frozen_hours": 96})) == ["frozen"]


def test_frozen_limit_shorter_than_global():
    rec = record(readings=frozen_reading(3, 30), limits={"frozen_hours": 2})
    assert kinds(rec, options={"frozen_alert_hours": 24}) == ["frozen"]


def test_frozen_limit_applies_when_global_is_off():
    rec = record(readings=frozen_reading(10, 30), limits={"frozen_hours": 6})
    assert kinds(rec, options={"frozen_alert_hours": 0}) == ["frozen"]
    assert kinds(record(readings=frozen_reading(10, 30)), options={"frozen_alert_hours": 0}) == []


def test_disabled_check_still_wins_over_limit():
    rec = record(battery=20, limits={"battery_percent": 25})
    assert kinds(rec, options={"disabled_checks": ["battery_low"]}) == []


@pytest.mark.parametrize("bad", [None, "25", 25.0, True, -1, 101])
def test_invalid_stored_battery_limit_falls_back(bad):
    assert health.limit_of({"limits": {"battery_percent": bad}}, {"battery_alert_percent": 15}, "battery_percent") == 15


def test_limit_of_uses_defaults_without_options():
    assert health.limit_of({}, {}, "battery_percent") == 15
    assert health.limit_of({}, {}, "frozen_hours") == 24
    assert health.limit_of({"limits": {"frozen_hours": 0}}, {}, "frozen_hours") == 24


# ------------------------------------------------------------------ validation


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"battery_percent": 0}, {"battery_percent": 0}),
        ({"battery_percent": 100}, {"battery_percent": 100}),
        ({"frozen_hours": 1}, {"frozen_hours": 1}),
        ({"frozen_hours": 720}, {"frozen_hours": 720}),
        ({"frozen_hours": 48.0}, {"frozen_hours": 48}),
        ({"battery_percent": 20, "frozen_hours": 96}, {"battery_percent": 20, "frozen_hours": 96}),
        ({"unknown": 5}, {}),
    ],
)
def test_clean_limits_accepts(changes, expected):
    assert health.clean_limits({}, changes) == (expected, None)


@pytest.mark.parametrize(
    "changes",
    [
        {"battery_percent": -1},
        {"battery_percent": 101},
        {"frozen_hours": 0},
        {"frozen_hours": 721},
        {"frozen_hours": 1.5},
        {"battery_percent": "20"},
        {"battery_percent": True},
        {"battery_percent": float("nan")},
        {"frozen_hours": float("inf")},
        {"battery_percent": [20]},
    ],
)
def test_clean_limits_rejects(changes):
    current = {"battery_percent": 30}
    limits, error = health.clean_limits(current, changes)
    assert error and "must be a whole number" in error
    assert limits == current


def test_clean_limits_clears_with_none_or_blank():
    current = {"battery_percent": 30, "frozen_hours": 96}
    assert health.clean_limits(current, {"battery_percent": None}) == ({"frozen_hours": 96}, None)
    assert health.clean_limits(current, {"frozen_hours": ""}) == ({"battery_percent": 30}, None)
    assert current == {"battery_percent": 30, "frozen_hours": 96}


# ------------------------------------------------------------------ tracker


@pytest.fixture
def one(tracker):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Freezer probe")])
    return tracker


def test_set_limits_stores_and_removes(one):
    assert one.set_limits(SENSOR, {"battery_percent": 25, "frozen_hours": 96})["ok"]
    assert one.data["devices"][SENSOR]["limits"] == {"battery_percent": 25, "frozen_hours": 96}
    assert one.set_limits(SENSOR, {"frozen_hours": None})["limits"] == {"battery_percent": 25}
    assert one.set_limits(SENSOR, {"battery_percent": None, "frozen_hours": None})["ok"]
    assert "limits" not in one.data["devices"][SENSOR]


def test_set_limits_only_touches_given_keys(one):
    one.set_limits(SENSOR, {"battery_percent": 25, "frozen_hours": 96})
    one.set_limits(SENSOR, {"battery_percent": 30})
    assert one.data["devices"][SENSOR]["limits"] == {"battery_percent": 30, "frozen_hours": 96}


def test_set_limits_rejects_and_keeps_old_value(one):
    one.set_limits(SENSOR, {"battery_percent": 25})
    result = one.set_limits(SENSOR, {"battery_percent": 25, "frozen_hours": 0})
    assert not result["ok"]
    assert "Frozen reading limit" in result["error"]
    assert one.data["devices"][SENSOR]["limits"] == {"battery_percent": 25}


def test_set_limits_unknown_device(one):
    assert one.set_limits("0xdeadbeef", {"battery_percent": 20}) == {"ok": False, "error": "Unknown device"}


def test_set_limits_reevaluates_health(one):
    send(one, "Freezer probe", {"battery": 20})
    one.evaluate_health()
    assert not [i for i in one.issues if i["kind"] == "battery_low"]
    one.set_limits(SENSOR, {"battery_percent": 25})
    assert [i["kind"] for i in one.issues if i["ieee"] == SENSOR] == ["battery_low"]


def test_shopping_list_uses_device_limit(one, clock):
    send(one, "Freezer probe", {"battery": 30})
    listed = lambda: {d["ieee"] for g in one.battery_shopping(60) for d in g["devices"]}  # noqa: E731
    assert SENSOR not in listed()
    one.set_limits(SENSOR, {"battery_percent": 35})
    assert SENSOR in listed()
