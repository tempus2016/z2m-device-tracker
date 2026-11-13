"""Pairing left open: who opened permit join, how long it has been open, and closing it."""

from __future__ import annotations

import health
import pytest
from conftest import send

V2 = {"version": "2.1.0"}


def info(clock, on: bool, seconds_left: int | None = None, **extra) -> dict:
    payload = {**V2, "permit_join": on, **extra}
    if on and seconds_left is not None:
        payload["permit_join_end"] = int((clock.now + seconds_left) * 1000)
    return payload


def pairing_issues(tracker) -> list[dict]:
    return [i for i in tracker.evaluate_health()[0] if i["kind"] == "pairing_open"]


@pytest.fixture
def z2m(tracker, clock):
    """A fake Zigbee2MQTT: echoes requests like the broker does and answers permit_join."""
    published = []

    def publish(topic: str, payload: dict) -> None:
        published.append((topic, payload))
        send(tracker, "bridge/request/permit_join", payload)
        seconds = payload.get("time") or 0
        send(tracker, "bridge/info", info(clock, bool(seconds), seconds))
        send(
            tracker,
            "bridge/response/permit_join",
            {"status": "ok", "data": {"time": seconds}, "transaction": payload["transaction"]},
        )

    tracker.publish = publish
    send(tracker, "bridge/info", info(clock, False))
    return published


def test_own_pairing_only_alerts_after_ten_minutes(tracker, clock, z2m):
    assert tracker.start_pairing("zigbee2mqtt", 254)["ok"]
    assert tracker.permit_meta["zigbee2mqtt"]["origin"] == "app"
    assert pairing_issues(tracker) == []
    clock.advance(minutes=4)
    send(tracker, "bridge/info", info(clock, True, 254))
    clock.advance(seconds=359)
    assert pairing_issues(tracker) == []
    clock.advance(seconds=1)
    send(tracker, "bridge/info", info(clock, True, 200))
    [issue] = pairing_issues(tracker)
    assert issue["key"] == "pairing_open:zigbee2mqtt"
    assert issue["severity"] == "warning"
    assert issue["ieee"] == "network:zigbee2mqtt"
    assert issue["detail"] == "Open for 10 min · closes by itself in 3 min"
    assert issue["notify_title"] == "Zigbee pairing still open"
    assert issue["notify_message"] == (
        "Main network has accepted new devices for 10 minutes. Close it if you're not pairing anything."
    )


def test_reopening_from_app_restarts_the_clock(tracker, clock, z2m):
    send(tracker, "bridge/info", info(clock, True, 254))
    assert pairing_issues(tracker)
    assert tracker.start_pairing("zigbee2mqtt", 180)["ok"]
    assert pairing_issues(tracker) == []


def test_opened_in_frontend_alerts_straight_away(tracker, clock):
    send(tracker, "bridge/info", info(clock, False))
    send(tracker, "bridge/info", info(clock, True, 254))
    [issue] = pairing_issues(tracker)
    assert issue["detail"] == "Open for 1 min · opened in the Zigbee2MQTT frontend · closes by itself in 4 min"
    assert issue["params"]["origin"] == "frontend"
    assert issue["notify_message"] == (
        "Main network is accepting new devices. Close it if you're not pairing anything."
    )
    clock.advance(minutes=14)
    [issue] = pairing_issues(tracker)
    # The end time has passed without Z2M saying it closed: don't claim it will close by itself.
    assert issue["detail"] == "Open for 14 min · opened in the Zigbee2MQTT frontend"
    assert "for 14 minutes" in issue["notify_message"]


def test_opened_by_another_mqtt_client_names_the_device(tracker, clock):
    send(tracker, "bridge/info", info(clock, False))
    send(tracker, "bridge/request/permit_join", {"time": 254, "device": "Hall plug"})
    clock.advance(seconds=2)
    send(tracker, "bridge/info", info(clock, True, 254))
    [issue] = pairing_issues(tracker)
    assert issue["params"]["origin"] == "mqtt"
    assert issue["detail"] == (
        "Open for 1 min · opened from Home Assistant or an automation · only through Hall plug · closes by itself in 4 min"
    )


def test_device_from_permit_join_response(tracker, clock):
    send(tracker, "bridge/info", info(clock, False))
    send(tracker, "bridge/info", info(clock, True, 254))
    send(tracker, "bridge/response/permit_join", {"status": "ok", "data": {"time": 254, "device": "Router 2"}})
    [issue] = pairing_issues(tracker)
    assert "only through Router 2" in issue["detail"]


def test_stale_request_doesnt_count(tracker, clock):
    send(tracker, "bridge/info", info(clock, False))
    send(tracker, "bridge/request/permit_join", '{"time": 254}')
    clock.advance(seconds=61)
    send(tracker, "bridge/info", info(clock, True, 254))
    assert tracker.permit_meta["zigbee2mqtt"]["origin"] == "frontend"


def test_closing_requests_are_ignored(tracker, clock):
    send(tracker, "bridge/request/permit_join", {"time": 0})
    send(tracker, "bridge/request/permit_join", {"value": False})
    send(tracker, "bridge/request/permit_join", "false")
    assert tracker._permit_hint == {}


def test_open_at_startup_waits_ten_minutes(tracker, clock):
    send(tracker, "bridge/info", info(clock, True))
    assert tracker.permit_meta["zigbee2mqtt"]["origin"] is None
    clock.advance(minutes=9)
    assert pairing_issues(tracker) == []
    clock.advance(minutes=1)
    [issue] = pairing_issues(tracker)
    assert issue["detail"] == "Open for 10 min"


def test_closing_clears_the_issue(tracker, clock, z2m):
    send(tracker, "bridge/info", info(clock, True, 254))
    assert pairing_issues(tracker)
    assert tracker.start_pairing("zigbee2mqtt", 0)["ok"]
    assert z2m[-1][1]["time"] == 0
    assert pairing_issues(tracker) == []
    assert tracker.permit_meta == {}


def test_z2m_closing_by_itself_clears_the_issue(tracker, clock):
    send(tracker, "bridge/info", info(clock, False))
    send(tracker, "bridge/info", info(clock, True, 254))
    assert pairing_issues(tracker)
    send(tracker, "bridge/info", info(clock, False))
    assert pairing_issues(tracker) == []


def test_check_can_be_disabled(make_tracker, clock):
    t = make_tracker(options={"disabled_checks": ["pairing_open"]})
    send(t, "bridge/info", info(clock, False))
    send(t, "bridge/info", info(clock, True, 254))
    assert pairing_issues(t) == []


def test_other_network_label(make_tracker, clock):
    t = make_tracker(["zigbee2mqtt", "zigbee2mqtt_garage"])
    send(t, "bridge/info", info(clock, False), base="zigbee2mqtt_garage")
    send(t, "bridge/info", info(clock, True), base="zigbee2mqtt_garage")
    clock.advance(minutes=3)
    [issue] = pairing_issues(t)
    assert issue["name"] == "Garage network"
    assert issue["notify_message"].startswith("Garage network has accepted new devices for 3 minutes.")


def test_permit_payload_by_z2m_version(tracker):
    send(tracker, "bridge/info", {"version": "2.1.0", "permit_join": False})
    assert tracker.permit_payload("zigbee2mqtt", 0) == {"time": 0}
    assert tracker.permit_payload("zigbee2mqtt", 120) == {"time": 120}
    send(tracker, "bridge/info", {"version": "1.42.0", "permit_join": False})
    assert tracker.permit_payload("zigbee2mqtt", 0) == {"value": False}
    assert tracker.permit_payload("zigbee2mqtt", 120) == {"value": True, "time": 120}


def test_z2m_1x_timeout_is_seconds_left(tracker, clock):
    send(tracker, "bridge/info", {"version": "1.42.0", "permit_join": True, "permit_join_timeout": 120})
    clock.advance(120)
    assert tracker.permit["zigbee2mqtt"] == {"on": True, "end": clock.iso()}


def test_pairing_open_notifies_by_default():
    assert "pairing_open" in health.ALERT_KINDS
    assert "pairing_open" in health.DEFAULT_NOTIFY


def test_new_notify_kind_is_offered_once():
    checks, offered, added = health.offer_new_kinds(["offline"], [])
    assert checks == ["late", "offline", "pairing_open"]
    assert offered == ["late", "pairing_open"]
    assert added == ["late", "pairing_open"]
    # Turned off afterwards in Settings: stays off.
    assert health.offer_new_kinds(["offline"], offered) == (["offline"], ["late", "pairing_open"], [])
    # An install that was already offered "late" gets only the new one.
    assert health.offer_new_kinds(["offline"], ["late"]) == (
        ["offline", "pairing_open"],
        ["late", "pairing_open"],
        ["pairing_open"],
    )
    # Fresh install already has both.
    assert health.offer_new_kinds(["late", "offline", "pairing_open"], []) == (
        ["late", "offline", "pairing_open"],
        ["late", "pairing_open"],
        [],
    )
