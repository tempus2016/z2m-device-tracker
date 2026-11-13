"""Router outages: devices behind an offline router fold into one issue and one alert."""

from __future__ import annotations

import health
from conftest import COORD, ROUTER_A, ROUTER_B, coordinator, send, z2m_device

S1 = "0x00158d0000700001"
S2 = "0x00158d0000700002"
S3 = "0x00158d0000700003"
S4 = "0x00158d0000700004"
LONER = "0x00158d0000700005"

NAMES = {
    ROUTER_A: "Landing plug",
    ROUTER_B: "Attic plug",
    S1: "Back door",
    S2: "Bathroom switch",
    S3: "Landing motion",
    S4: "Attic sensor",
    LONER: "Garage sensor",
}


def link(source, target, lqi=100):
    return {"source": {"ieeeAddr": source}, "target": {"ieeeAddr": target}, "lqi": lqi, "relationship": 1}


def build(tracker):
    """Coordinator <- Landing plug <- (Back door, Bathroom switch, Landing motion, Attic plug <- Attic sensor).
    Garage sensor has never been in a routing scan."""
    send(
        tracker,
        "bridge/devices",
        [coordinator()]
        + [
            z2m_device(ieee, name, "Router" if ieee in (ROUTER_A, ROUTER_B) else "EndDevice")
            for ieee, name in NAMES.items()
        ],
    )
    nodes = [{"ieeeAddr": i} for i in (COORD, ROUTER_A, ROUTER_B, S1, S2, S3, S4)]
    links = [
        link(ROUTER_A, COORD),
        link(ROUTER_B, ROUTER_A),
        link(S1, ROUTER_A),
        link(S2, ROUTER_A),
        link(S3, ROUTER_A),
        link(S4, ROUTER_B),
    ]
    send(tracker, "bridge/response/networkmap", {"status": "ok", "data": {"value": {"nodes": nodes, "links": links}}})
    for name in NAMES.values():
        send(tracker, f"{name}/availability", "online")


def offline(tracker, *ieees):
    for ieee in ieees:
        send(tracker, f"{NAMES[ieee]}/availability", "offline")


def online(tracker, *ieees):
    for ieee in ieees:
        send(tracker, f"{NAMES[ieee]}/availability", "online")


def by_ieee(issues):
    return {i["ieee"]: i for i in issues if i["kind"] == "offline"}


def test_router_and_devices_behind_it_group(make_tracker, clock):
    t = make_tracker(options={"offline_alert_minutes": 10})
    build(t)
    offline(t, ROUTER_A, ROUTER_B, S1, S2, S3, S4, LONER)
    clock.advance(minutes=11)
    issues, _ = t.evaluate_health()
    offline_issues = by_ieee(issues)
    router = offline_issues[ROUTER_A]
    # Deeper devices (behind Attic plug, itself behind Landing plug) group under the top router.
    assert router["devices"] == [ROUTER_B, S4, S1, S2, S3]
    assert "behind" not in router
    assert all(offline_issues[i]["behind"] == ROUTER_A for i in (ROUTER_B, S1, S2, S3, S4))
    # Never in a scan: stays a normal issue.
    assert "behind" not in offline_issues[LONER]
    assert router["title"] == "Router offline"
    assert router["detail"] == (
        "5 devices that route through it are offline too: Attic plug, Attic sensor, Back door and 2 more. "
        "Check its power first."
    )
    assert router["msg"] == "router_offline"
    assert router["params"] == {
        "seconds": 660,
        "n": 5,
        "names": ["Attic plug", "Attic sensor", "Back door"],
        "more": 2,
    }
    assert health.alert_title(router) == "Router offline: Landing plug"
    # The child issues keep their own key, kind and text.
    assert offline_issues[S1]["key"] == f"offline:{S1}"
    assert offline_issues[S1]["title"] == "Offline"


def test_only_one_alert_for_the_router(make_tracker, clock):
    t = make_tracker(options={"offline_alert_minutes": 10})
    build(t)
    offline(t, ROUTER_A, S1, S2)
    clock.advance(minutes=11)
    issues, _ = t.evaluate_health()
    to_send, alerted, dismiss = health.alert_plan(issues, {"offline"}, set())
    assert [i["key"] for i in to_send] == [f"offline:{ROUTER_A}"]
    assert alerted == [f"offline:{ROUTER_A}"]
    assert dismiss == []
    assert to_send[0]["detail"].startswith(
        "2 devices that route through it are offline too: Back door and Bathroom switch."
    )


def test_device_offline_before_router_delay_passes_does_not_alert(make_tracker, clock):
    # The device's own delay passes first; its router is offline but hasn't reached the delay yet.
    t = make_tracker(options={"offline_alert_minutes": 10})
    build(t)
    offline(t, S1)
    clock.advance(minutes=5)
    offline(t, ROUTER_A)
    clock.advance(minutes=6)
    issues, _ = t.evaluate_health()
    assert by_ieee(issues)[S1]["behind"] == ROUTER_A
    assert ROUTER_A not in by_ieee(issues)
    to_send, alerted, _ = health.alert_plan(issues, {"offline"}, set())
    assert to_send == [] and alerted == []
    clock.advance(minutes=5)
    issues, _ = t.evaluate_health()
    to_send, _, _ = health.alert_plan(issues, {"offline"}, set(alerted))
    assert [i["ieee"] for i in to_send] == [ROUTER_A]
    assert by_ieee(issues)[ROUTER_A]["devices"] == [S1]


def test_already_notified_device_stays_notified(make_tracker, clock):
    t = make_tracker(options={"offline_alert_minutes": 10})
    build(t)
    offline(t, S1)
    clock.advance(minutes=11)
    issues, _ = t.evaluate_health()
    to_send, sent, _ = health.alert_plan(issues, {"offline"}, set())
    assert [i["ieee"] for i in to_send] == [S1]
    offline(t, ROUTER_A)
    clock.advance(minutes=11)
    issues, _ = t.evaluate_health()
    to_send, sent, dismiss = health.alert_plan(issues, {"offline"}, set(sent))
    assert [i["ieee"] for i in to_send] == [ROUTER_A]
    assert sorted(sent) == sorted([f"offline:{S1}", f"offline:{ROUTER_A}"])
    assert dismiss == []


def test_router_back_devices_still_offline_alert_after_grace(make_tracker, clock):
    t = make_tracker(options={"offline_alert_minutes": 10})
    build(t)
    offline(t, ROUTER_A, S1, S2)
    clock.advance(minutes=11)
    issues, _ = t.evaluate_health()
    _, sent, _ = health.alert_plan(issues, {"offline"}, set())

    online(t, ROUTER_A, S2)
    clock.advance(minutes=1)
    issues, _ = t.evaluate_health()
    # Just back: Back door gets a few minutes to rejoin before it alerts on its own.
    assert by_ieee(issues)[S1]["behind"] == ROUTER_A
    to_send, sent, dismiss = health.alert_plan(issues, {"offline"}, set(sent))
    assert to_send == [] and dismiss == [f"offline:{ROUTER_A}"]

    clock.advance(seconds=health.ROUTER_BACK_GRACE)
    issues, _ = t.evaluate_health()
    assert "behind" not in by_ieee(issues)[S1]
    to_send, sent, _ = health.alert_plan(issues, {"offline"}, set(sent))
    assert [i["ieee"] for i in to_send] == [S1]
    assert health.alert_title(to_send[0]) == "Zigbee: Back door - Offline"


def test_muted_router_does_not_hold_devices(make_tracker, clock):
    t = make_tracker(options={"offline_alert_minutes": 10})
    build(t)
    t.set_muted(ROUTER_A, "offline", True)
    offline(t, ROUTER_A, S1)
    clock.advance(minutes=11)
    issues, _ = t.evaluate_health()
    assert ROUTER_A not in by_ieee(issues)
    assert "behind" not in by_ieee(issues)[S1]


def test_router_without_devices_behind_keeps_plain_text(make_tracker, clock):
    t = make_tracker(options={"offline_alert_minutes": 10})
    build(t)
    offline(t, ROUTER_A)
    clock.advance(minutes=11)
    router = by_ieee(t.evaluate_health()[0])[ROUTER_A]
    assert router["title"] == "Offline" and "devices" not in router
    assert health.alert_title(router) == "Zigbee: Landing plug - Offline"


def test_grouping_change_counts_as_changed(make_tracker, clock):
    t = make_tracker(options={"offline_alert_minutes": 10})
    build(t)
    offline(t, ROUTER_A, S1)
    clock.advance(minutes=11)
    t.evaluate_health()
    online(t, ROUTER_A)
    assert t.evaluate_health()[1] is True
    # Only Back door's `behind` changes now, with the same issue keys: still a change for the sidebar.
    clock.advance(seconds=health.ROUTER_BACK_GRACE)
    issues, changed = t.evaluate_health()
    assert [i["key"] for i in issues if i["kind"] == "offline"] == [f"offline:{S1}"]
    assert changed is True


def test_nested():
    router = {"key": "offline:r", "kind": "offline"}
    child = {"key": "offline:c", "kind": "offline", "behind": "r"}
    assert health.nested(child, {"offline:r", "offline:c"})
    assert not health.nested(child, {"offline:c"})
    assert not health.nested(router, {"offline:r"})


def test_names():
    assert health._names(["A"]) == "A"
    assert health._names(["A", "B"]) == "A and B"
    assert health._names(["A", "B", "C"]) == "A, B and C"
    assert health._names(["A", "B", "C", "D"]) == "A, B, C and 1 more"
