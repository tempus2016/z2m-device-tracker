"""Power loss in one area: mains devices there drop together while its battery devices stay online."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import health
from conftest import COORD, coordinator, send, z2m_device
from incidents import build_incidents

P1 = "0x00124b0000d00001"
P2 = "0x00124b0000d00002"
P3 = "0x00124b0000d00003"
P4 = "0x00124b0000d00004"
BAT = "0x00158d0000d00005"
HALL = "0x00124b0000d00006"

NAMES = {
    P1: "Garage plug",
    P2: "Garage bulb",
    P3: "Garage relay",
    P4: "Garage router",
    BAT: "Garage leak",
    HALL: "Hall plug",
}
AREAS = {P1: "Garage", P2: "Garage", P3: "Garage", P4: "Garage", BAT: "Garage", HALL: "Hall"}

T0 = datetime(2026, 6, 1, 7, 12, 0, tzinfo=timezone.utc)
NET = "zigbee2mqtt"


# ---------------------------------------------------------------- incidents (pure)


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def ev(seconds: float, ieee: str, kind: str) -> dict:
    return {"time": at(seconds), "instance": NET, "ieee": ieee, "name": NAMES.get(ieee, ieee), "kind": kind}


def dev(ieee: str, dtype: str = "Router", parent: str | None = None, area: str | None = None) -> dict:
    record = {
        "ieee": ieee,
        "type": dtype,
        "status": "active",
        "availability": "online",
        "friendly_name": NAMES.get(ieee, ieee),
        "power_source": "Battery" if dtype == "EndDevice" else "Mains (single phase)",
        "area": area if area is not None else AREAS.get(ieee),
    }
    if parent:
        record["routing"] = {"parent": {"ieee": parent, "lqi": 100}}
    return record


def garage(**parents) -> dict:
    return {
        P1: dev(P1, parent=parents.get(P1, COORD)),
        P2: dev(P2, parent=parents.get(P2, COORD)),
        P3: dev(P3, parent=parents.get(P3, COORD)),
        P4: dev(P4, parent=parents.get(P4, COORD)),
        BAT: dev(BAT, "EndDevice", parent=P4),
        HALL: dev(HALL, parent=COORD),
    }


def drop_and_return(back: float = 1140) -> list[dict]:
    return [
        ev(4, P1, "offline"),
        ev(6, P2, "offline"),
        ev(6, P4, "offline"),
        ev(10, P3, "offline"),
        ev(back, P1, "online"),
        ev(back + 5, P2, "online"),
        ev(back + 8, P3, "online"),
        ev(back + 9, P4, "online"),
    ]


def test_power_loss_named_with_area():
    [inc] = build_incidents(drop_and_return(), garage())
    assert inc["cause"] == "power_loss"
    assert inc["router"] is None
    assert inc["power"] == {"area": "Garage", "n": 4, "seconds": 6, "back_together": True}
    assert inc["affected"] == 4 and inc["recovered"] == 4 and inc["ongoing"] is False


def test_coming_back_apart_is_still_power_loss_but_not_together():
    events = drop_and_return()[:4] + [ev(600, P1, "online"), ev(605, P2, "online"), ev(3000, P3, "online")]
    [inc] = build_incidents(events, garage())
    assert inc["cause"] == "power_loss"
    assert inc["power"]["back_together"] is False


def test_battery_device_dropping_too_is_not_power_loss():
    events = drop_and_return()[:4] + [ev(12, BAT, "offline")]
    [inc] = build_incidents(events, garage())
    assert inc["cause"] != "power_loss"
    assert inc["power"] is None


def test_area_without_battery_devices_is_not_power_loss():
    devices = garage()
    devices[BAT]["area"] = "Hall"
    [inc] = build_incidents(drop_and_return(), devices)
    assert inc["cause"] != "power_loss"


def test_drops_spread_by_availability_pings_still_count():
    # Zigbee2MQTT marks each mains device offline when its own ping fails, minutes apart.
    events = [ev(0, P1, "offline"), ev(240, P2, "offline"), ev(560, P3, "offline")]
    [inc] = build_incidents(events, garage())
    assert inc["cause"] == "power_loss"


def test_spread_over_more_than_eleven_minutes_is_not_power_loss():
    events = [ev(0, P1, "offline"), ev(400, P2, "offline"), ev(800, P3, "offline")]
    incs = build_incidents(events, garage())
    assert all(inc["cause"] != "power_loss" for inc in incs)


def test_devices_in_different_areas_are_not_power_loss():
    devices = garage()
    devices[P2]["area"] = "Hall"
    devices[P3]["area"] = "Hall"
    events = [ev(0, P1, "offline"), ev(2, P2, "offline"), ev(3, P3, "offline")]
    [inc] = build_incidents(events, devices)
    assert inc["cause"] != "power_loss"


def test_router_outage_still_wins_when_the_devices_sit_behind_it():
    # Garage router went first; the other three route through it.
    devices = garage(**{P1: P4, P2: P4, P3: P4})
    events = [ev(0, P4, "offline"), ev(3, P1, "offline"), ev(4, P2, "offline"), ev(5, P3, "offline")]
    [inc] = build_incidents(events, devices)
    assert inc["cause"] == "router_offline"
    assert inc["router"]["ieee"] == P4
    assert inc["power"] is None


def test_power_loss_wins_when_a_router_explains_only_some():
    # P1 sits behind P4, but P2 and P3 route straight to the coordinator.
    devices = garage(**{P1: P4})
    events = [ev(0, P4, "offline"), ev(3, P1, "offline"), ev(4, P2, "offline"), ev(5, P3, "offline")]
    [inc] = build_incidents(events, devices)
    assert inc["cause"] == "power_loss"
    assert inc["power"]["n"] == 4


# ---------------------------------------------------------------- one notification (tracker + health)


def build(tracker, links=()):
    send(
        tracker,
        "bridge/devices",
        [coordinator()]
        + [z2m_device(ieee, name, "EndDevice" if ieee == BAT else "Router") for ieee, name in NAMES.items()],
    )
    if links:
        nodes = [{"ieeeAddr": i} for i in (COORD, *NAMES)]
        data = [{"source": {"ieeeAddr": s}, "target": {"ieeeAddr": t}, "lqi": 100, "relationship": 1} for s, t in links]
        send(
            tracker, "bridge/response/networkmap", {"status": "ok", "data": {"value": {"nodes": nodes, "links": data}}}
        )
    for ieee, name in NAMES.items():
        tracker.data["devices"][ieee]["area"] = AREAS[ieee]
        send(tracker, f"{name}/availability", "online")


def offline(tracker, clock, *ieees, gap: float = 2):
    for ieee in ieees:
        send(tracker, f"{NAMES[ieee]}/availability", "offline")
        clock.advance(gap)


def offline_issues(issues):
    return {i["ieee"]: i for i in issues if i["kind"] == "offline"}


def test_power_loss_folds_into_one_alert(make_tracker, clock):
    t = make_tracker(options={"offline_alert_minutes": 10})
    build(t)
    offline(t, clock, P1, P2, P3)
    clock.advance(minutes=11)
    issues, _ = t.evaluate_health()
    found = offline_issues(issues)
    lead = found[P1]
    assert lead["power_area"] == "Garage"
    assert lead["power_devices"] == [P2, P3]
    assert lead["msg"] == "power_loss"
    assert lead["notify_title"] == "Power loss in Garage?"
    assert lead["detail"].startswith(
        "3 mains devices in Garage went offline within 4 s of each other: Garage bulb, Garage plug and Garage relay."
    )
    assert found[P2]["behind"] == P1 and found[P3]["behind"] == P1
    assert health.alert_title(lead) == "Power loss in Garage?"
    to_send, alerted, _ = health.alert_plan(issues, {"offline"}, set())
    assert [i["key"] for i in to_send] == [f"offline:{P1}"]
    assert alerted == [f"offline:{P1}"]
    plan = t.plan_alerts(issues, {"offline"})
    assert plan["first"]  # first check only records


def test_no_power_loss_when_the_battery_device_is_offline_too(make_tracker, clock):
    t = make_tracker(options={"offline_alert_minutes": 10})
    build(t)
    offline(t, clock, BAT, P1, P2, P3)
    clock.advance(minutes=11)
    issues, _ = t.evaluate_health()
    assert not any(i.get("power_area") for i in issues)
    assert not any(i.get("behind") for i in issues)


def test_no_power_loss_when_drops_are_far_apart(make_tracker, clock):
    t = make_tracker(options={"offline_alert_minutes": 10})
    build(t)
    offline(t, clock, P1, P2, P3, gap=360)
    clock.advance(minutes=11)
    issues, _ = t.evaluate_health()
    assert not any(i.get("power_area") for i in issues)


def test_router_grouping_wins_over_power_loss(make_tracker, clock):
    t = make_tracker(options={"offline_alert_minutes": 10})
    # P4 is the parent of P1, P2 and P3 in the routing scan.
    build(t, links=[(P4, COORD), (P1, P4), (P2, P4), (P3, P4), (BAT, P4), (HALL, COORD)])
    t.data["devices"][BAT]["routing"] = {"parent": {"ieee": COORD, "lqi": 100}}
    offline(t, clock, P4, P1, P2, P3)
    clock.advance(minutes=11)
    issues, _ = t.evaluate_health()
    found = offline_issues(issues)
    assert not any(i.get("power_area") for i in issues)
    assert sorted(found[P4]["devices"]) == sorted([P1, P2, P3])
    assert all(found[i]["behind"] == P4 for i in (P1, P2, P3))


def test_muted_device_is_left_out(make_tracker, clock):
    t = make_tracker(options={"offline_alert_minutes": 10})
    build(t)
    t.set_muted(P3, "offline", True)
    offline(t, clock, P1, P2, P3)
    clock.advance(minutes=11)
    issues, _ = t.evaluate_health()
    assert not any(i.get("power_area") for i in issues)
