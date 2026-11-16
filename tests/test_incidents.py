"""Incidents: related problem events on one network grouped into one story with a likely cause."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from conftest import COORD, ROUTER_A, send
from incidents import build_incidents
from test_router_outage import LONER, NAMES, S1, S2, S3, build, offline, online

T0 = datetime(2026, 6, 1, 23, 10, 0, tzinfo=timezone.utc)
NET = "zigbee2mqtt"


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def ev(seconds: float, ieee: str, kind: str, name: str | None = None, net: str = NET) -> dict:
    return {"time": at(seconds), "instance": net, "ieee": ieee, "name": name or ieee, "kind": kind, "detail": ""}


def dev(ieee: str, dtype: str = "EndDevice", parent: str | None = None, availability: str = "online") -> dict:
    record = {"ieee": ieee, "type": dtype, "status": "active", "availability": availability, "friendly_name": ieee}
    if parent:
        record["routing"] = {"parent": {"ieee": parent, "lqi": 100}}
    return record


def test_single_offline_device_is_not_an_incident():
    events = [ev(0, "0xs1", "offline"), ev(3600, "0xs1", "online")]
    assert build_incidents(events, {"0xs1": dev("0xs1")}) == []


def test_two_devices_are_not_enough():
    events = [ev(0, "0xa", "offline"), ev(30, "0xb", "offline")]
    assert build_incidents(events, {}) == []


def test_router_first_then_followers_names_the_router():
    devices = {
        "0xr": dev("0xr", "Router", parent="0xc"),
        "0xa": dev("0xa", parent="0xr"),
        "0xb": dev("0xb", parent="0xr"),
        "0xd": dev("0xd", parent="0xr"),
    }
    events = [
        ev(0, "0xr", "offline", "Garage plug"),
        ev(120, "0xa", "offline"),
        ev(400, "0xb", "offline"),
        ev(900, "0xd", "offline"),  # 500 s after the previous one: still inside the 10 minute gap
        ev(1800, "0xr", "online"),
        ev(1830, "0xa", "online"),
        ev(1840, "0xb", "online"),
        ev(1850, "0xd", "online"),
    ]
    [inc] = build_incidents(events, devices)
    assert inc["cause"] == "router_offline"
    assert inc["router"] == {"ieee": "0xr", "name": "Garage plug"}
    assert inc["affected"] == 4 and inc["recovered"] == 4
    assert inc["ongoing"] is False
    assert inc["start"] == at(0) and inc["end"] == at(1850)
    assert [d["ieee"] for d in inc["devices"]] == ["0xr", "0xa", "0xb", "0xd"]
    assert len(inc["events"]) == inc["events_total"] == 8


def test_gap_splits_incidents():
    events = [ev(0, f"0x{i}", "offline") for i in "abc"] + [
        ev(3000 + n, f"0x{i}", "offline") for n, i in enumerate("def")
    ]
    incidents = build_incidents(events, {})
    assert [i["start"] for i in incidents] == [at(3000), at(0)]  # newest first


def test_network_offline_is_an_incident_on_its_own():
    events = [ev(0, "network", "network offline", "Network"), ev(300, "network", "network online", "Network")]
    [inc] = build_incidents(events, {}, {NET: {"state": "online"}})
    assert inc["cause"] == "z2m_down"
    assert inc["affected"] == 0
    assert inc["ongoing"] is False and inc["end"] == at(300)


def test_network_still_offline_is_ongoing():
    events = [ev(0, "network", "network offline", "Network")]
    [inc] = build_incidents(events, {}, {NET: {"state": "offline"}})
    assert inc["ongoing"] is True and inc["end"] is None


def test_z2m_restart_beats_router_cause():
    devices = {"0xr": dev("0xr", "Router"), "0xa": dev("0xa", parent="0xr"), "0xb": dev("0xb", parent="0xr")}
    events = [
        ev(0, "network", "zigbee2mqtt restarted", "Network"),
        ev(10, "0xr", "offline"),
        ev(20, "0xa", "offline"),
        ev(30, "0xb", "offline"),
    ]
    assert build_incidents(events, devices)[0]["cause"] == "z2m_down"


def test_many_devices_across_routers_within_a_minute_is_network_wide():
    devices = {
        "0xa": dev("0xa", parent="0xr1"),
        "0xb": dev("0xb", parent="0xr2"),
        "0xc": dev("0xc", parent="0xr3"),
    }
    events = [ev(0, "0xa", "offline"), ev(20, "0xb", "offline"), ev(50, "0xc", "offline")]
    [inc] = build_incidents(events, devices)
    assert inc["cause"] == "network_wide"


def test_spread_out_drops_are_unknown():
    devices = {"0xa": dev("0xa", parent="0xr1"), "0xb": dev("0xb", parent="0xr2"), "0xc": dev("0xc", parent="0xr3")}
    events = [ev(0, "0xa", "offline"), ev(300, "0xb", "offline"), ev(600, "0xc", "offline")]
    assert build_incidents(events, devices)[0]["cause"] == "unknown"


def test_device_still_offline_keeps_incident_ongoing():
    devices = {"0xa": dev("0xa"), "0xb": dev("0xb"), "0xc": dev("0xc", availability="offline")}
    events = [ev(0, "0xa", "offline"), ev(5, "0xb", "offline"), ev(9, "0xc", "offline")]
    events += [ev(100, "0xa", "online"), ev(110, "0xb", "online")]
    [inc] = build_incidents(events, devices)
    assert inc["ongoing"] is True and inc["end"] is None
    assert inc["recovered"] == 2 and inc["affected"] == 3


def test_live_state_counts_a_device_back_without_its_online_event():
    devices = {"0xa": dev("0xa"), "0xb": dev("0xb"), "0xc": dev("0xc")}
    events = [ev(0, "0xa", "offline"), ev(5, "0xb", "offline"), ev(9, "0xc", "offline")]
    [inc] = build_incidents(events, devices)
    assert inc["ongoing"] is False and inc["recovered"] == 3


def test_networks_are_grouped_separately():
    events = [ev(0, "0xa", "offline"), ev(1, "0xb", "offline", net="other"), ev(2, "0xc", "offline")]
    assert build_incidents(events, {}) == []


def test_other_event_kinds_are_ignored_and_bad_times_skipped():
    events = [ev(0, "0xa", "renamed"), ev(1, "0xa", "moved"), {"time": "nonsense", "kind": "offline"}, "junk"]
    assert build_incidents(events, {}) == []


def test_keeps_the_newest_fifty():
    events = []
    for n in range(60):
        base = n * 3600
        events += [ev(base, "0xa", "offline"), ev(base + 1, "0xb", "offline"), ev(base + 2, "0xc", "offline")]
    incidents = build_incidents(events, {})
    assert len(incidents) == 50
    assert incidents[0]["start"] == at(59 * 3600)


def test_tracker_snapshot_has_router_incident(make_tracker, clock):
    t = make_tracker(options={"offline_alert_minutes": 10})
    build(t)
    offline(t, ROUTER_A)
    clock.advance(seconds=30)
    offline(t, S1, S2, S3)
    clock.advance(minutes=30)
    online(t, ROUTER_A, S1, S2, S3)
    snap = json.loads(t.snapshot_json())
    [inc] = snap["incidents"]
    assert inc["cause"] == "router_offline"
    assert inc["router"]["ieee"] == ROUTER_A
    assert inc["router"]["name"] == NAMES[ROUTER_A]
    assert inc["affected"] == 4 and inc["recovered"] == 4 and inc["ongoing"] is False


def test_tracker_snapshot_ongoing_and_cache_refreshes(make_tracker, clock):
    t = make_tracker()
    build(t)
    offline(t, S1, S2, LONER)
    with t.lock:
        first = t.incidents()
    assert first[0]["ongoing"] is True and first[0]["affected"] == 3
    with t.lock:
        assert t.incidents() is first  # cached while nothing changed
    online(t, S1, S2, LONER)
    with t.lock:
        again = t.incidents()
    assert again[0]["ongoing"] is False and again[0]["recovered"] == 3


def test_tracker_network_offline_incident(make_tracker, clock):
    t = make_tracker()
    send(t, "bridge/devices", [{"ieee_address": COORD, "friendly_name": "Coordinator", "type": "Coordinator"}])
    send(t, "bridge/state", {"state": "online"})
    send(t, "bridge/state", {"state": "offline"})
    snap = json.loads(t.snapshot_json())
    [inc] = snap["incidents"]
    assert inc["cause"] == "z2m_down" and inc["ongoing"] is True
