"""Warranty end dates: the Upkeep list and the warranty_ending check."""

from __future__ import annotations

import pytest
from conftest import SENSOR, send, z2m_device
from health import ALERT_KINDS, CHECKS, DEFAULT_NOTIFY

# conftest START is 2026-06-01


@pytest.fixture
def dev(tracker):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall smoke")])
    return tracker


def warranty_issues(tracker) -> list[dict]:
    return [i for i in tracker.evaluate_health()[0] if i["kind"] == "warranty_ending"]


def test_check_is_listed_but_does_not_notify():
    assert "warranty_ending" in {c[0] for c in CHECKS}
    assert "warranty_ending" not in ALERT_KINDS
    assert "warranty_ending" not in DEFAULT_NOTIFY


def test_no_date_no_issue(dev):
    dev.set_asset(SENSOR, {"shop": "Hardware store"})
    assert warranty_issues(dev) == []
    assert dev.warranty_list() == []


@pytest.mark.parametrize("value", ["soon", "2026-13-01", "01/06/2026"])
def test_bad_date_is_ignored(dev, value):
    dev.set_asset(SENSOR, {"warranty_until": value})
    assert warranty_issues(dev) == []
    assert dev.warranty_list() == []


def test_ending_within_30_days(dev):
    dev.set_asset(SENSOR, {"warranty_until": "2026-06-19", "purchase_date": "2024-06-19"})
    (issue,) = warranty_issues(dev)
    assert issue["severity"] == "info"
    assert issue["msg"] == "warranty_ending"
    assert issue["params"] == {"n": 18, "date": "2026-06-19"}
    assert issue["detail"].startswith("Warranty ends in 18 days (2026-06-19).")
    (row,) = dev.warranty_list()
    assert row == {
        "ieee": SENSOR,
        "name": "Hall smoke",
        "until": "2026-06-19",
        "days": 18,
        "purchase_date": "2024-06-19",
    }


def test_ends_today(dev):
    dev.set_asset(SENSOR, {"warranty_until": "2026-06-01"})
    (issue,) = warranty_issues(dev)
    assert issue["msg"] == "warranty_ending_today"
    assert issue["detail"].startswith("Warranty ends today")


def test_further_out_is_listed_but_not_flagged(dev):
    dev.set_asset(SENSOR, {"warranty_until": "2026-07-20"})  # 49 days
    assert warranty_issues(dev) == []
    assert [w["days"] for w in dev.warranty_list()] == [49]
    dev.set_asset(SENSOR, {"warranty_until": "2026-08-15"})  # 75 days: not listed yet
    assert dev.warranty_list() == []


def test_ended(dev, clock):
    dev.set_asset(SENSOR, {"warranty_until": "2026-05-01"})
    assert warranty_issues(dev) == []
    assert [w["days"] for w in dev.warranty_list()] == [-31]
    clock.advance(days=60)  # 91 days ago: dropped from the list
    assert dev.warranty_list() == []


def test_flag_clears_once_ended(dev, clock):
    dev.set_asset(SENSOR, {"warranty_until": "2026-06-10"})
    assert warranty_issues(dev)
    clock.advance(days=10)
    assert warranty_issues(dev) == []


def test_list_order(tracker):
    from conftest import PLUG

    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall smoke"), z2m_device(PLUG, "Kitchen plug")])
    tracker.set_asset(SENSOR, {"warranty_until": "2026-05-20"})  # ended
    tracker.set_asset(PLUG, {"warranty_until": "2026-07-01"})
    assert [w["name"] for w in tracker.warranty_list()] == ["Kitchen plug", "Hall smoke"]


def test_mute_and_disable(dev):
    dev.set_asset(SENSOR, {"warranty_until": "2026-06-10"})
    dev.set_muted(SENSOR, "warranty_ending", True)
    assert warranty_issues(dev) == []
    dev.set_muted(SENSOR, "warranty_ending", False)
    dev.options["disabled_checks"] = ["warranty_ending"]
    assert warranty_issues(dev) == []


def test_does_not_alert_unless_turned_on(dev):
    dev.plan_alerts(dev.evaluate_health()[0], set(DEFAULT_NOTIFY))
    dev.set_asset(SENSOR, {"warranty_until": "2026-06-10"})
    issues, _ = dev.evaluate_health()
    assert dev.plan_alerts(issues, set(DEFAULT_NOTIFY))["send"] == []
    sent = dev.plan_alerts(issues, set(DEFAULT_NOTIFY) | {"warranty_ending"})["send"]
    assert [i["kind"] for i in sent] == ["warranty_ending"]


def test_snapshot_has_warranties(dev):
    import json

    dev.set_asset(SENSOR, {"warranty_until": "2026-06-10"})
    assert json.loads(dev.snapshot_json())["warranties"][0]["days"] == 9
