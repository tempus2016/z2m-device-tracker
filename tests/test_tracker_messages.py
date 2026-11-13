"""Tracker.handle_message: device list diffs, events, availability, state and bindings."""

from __future__ import annotations

from conftest import COORD, PLUG, SENSOR, coordinator, events_of, send, z2m_device

# ------------------------------------------------------------------ bridge/devices


def test_first_list_records_devices_without_events(tracker, clock):
    send(tracker, "bridge/devices", [coordinator(), z2m_device(SENSOR, "Hall sensor")])
    rec = tracker.data["devices"][SENSOR]
    assert rec["friendly_name"] == "Hall sensor"
    assert rec["first_seen"] == clock.iso()
    assert rec["model"] == "FK-1"
    assert rec["vendor"] == "FakeCo"
    assert rec["features"] == ["temperature"]
    assert rec["sources"] == ["Z2M live"]
    assert rec["status"] == "active"
    # The first list after start-up is the baseline, not a burst of "added" events.
    assert events_of(tracker) == []
    assert tracker.lists_ready.is_set()
    assert tracker.names["zigbee2mqtt"] == {"Coordinator": COORD, "Hall sensor": SENSOR}


def test_uppercase_ieee_is_normalised(tracker):
    send(tracker, "bridge/devices", [z2m_device(SENSOR.upper().replace("0X", "0x"), "Hall sensor")])
    assert SENSOR in tracker.data["devices"]


def test_join_rename_leave_and_rejoin(tracker, clock):
    send(tracker, "bridge/devices", [coordinator(), z2m_device(SENSOR, "Hall sensor")])
    revision = tracker.revision

    clock.advance(minutes=5)
    send(tracker, "bridge/devices", [coordinator(), z2m_device(SENSOR, "Hall sensor"), z2m_device(PLUG, "Lamp")])
    assert [(e["ieee"], e["detail"]) for e in events_of(tracker, "added")] == [
        (PLUG, "Appeared in the Z2M device list")
    ]
    assert tracker.revision > revision

    clock.advance(minutes=5)
    send(tracker, "bridge/devices", [coordinator(), z2m_device(SENSOR, "Landing sensor"), z2m_device(PLUG, "Lamp")])
    renamed = events_of(tracker, "renamed")
    assert [e["detail"] for e in renamed] == ["Hall sensor → Landing sensor"]
    names = tracker.data["devices"][SENSOR]["names"]
    assert [(n["name"], n["until"]) for n in names] == [("Hall sensor", clock.iso()), ("Landing sensor", None)]
    assert tracker.names["zigbee2mqtt"]["Landing sensor"] == SENSOR
    assert "Hall sensor" not in tracker.names["zigbee2mqtt"]

    clock.advance(minutes=5)
    send(tracker, "bridge/devices", [coordinator(), z2m_device(SENSOR, "Landing sensor")])
    plug = tracker.data["devices"][PLUG]
    assert plug["status"] == "removed"
    assert plug["removed_at"] == clock.iso()
    assert [e["ieee"] for e in events_of(tracker, "removed")] == [PLUG]

    clock.advance(minutes=5)
    send(tracker, "bridge/devices", [coordinator(), z2m_device(SENSOR, "Landing sensor"), z2m_device(PLUG, "Lamp")])
    assert plug["status"] == "active"
    assert plug["removed_at"] is None
    assert [e["ieee"] for e in events_of(tracker, "rejoined")] == [PLUG]


def test_rename_back_reopens_old_name(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "A")])
    clock.advance(60)
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "B")])
    clock.advance(60)
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "A")])
    names = tracker.data["devices"][SENSOR]["names"]
    assert [(n["name"], n["until"] is None) for n in names] == [("A", True), ("B", False)]


def test_model_change_event(tracker):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "S")])
    other = z2m_device(SENSOR, "S")
    other["definition"] = {**other["definition"], "model": "FK-2"}
    send(tracker, "bridge/devices", [other])
    assert [e["detail"] for e in events_of(tracker, "model changed")] == ["FK-1 → FK-2"]


def test_device_moving_between_networks(make_tracker):
    t = make_tracker(base_topics=["zigbee2mqtt", "zigbee2mqtt_upstairs"])
    send(t, "bridge/devices", [z2m_device(SENSOR, "S")])
    assert not t.lists_ready.is_set()
    send(t, "bridge/devices", [], base="zigbee2mqtt_upstairs")
    assert t.lists_ready.is_set()
    send(t, "bridge/devices", [z2m_device(SENSOR, "S")], base="zigbee2mqtt_upstairs")
    assert t.data["devices"][SENSOR]["instance"] == "zigbee2mqtt_upstairs"
    assert [e["detail"] for e in events_of(t, "moved")] == ["From zigbee2mqtt"]
    # It moved, so the old network's next list must not mark it removed.
    send(t, "bridge/devices", [])
    assert t.data["devices"][SENSOR]["status"] == "active"


def test_legacy_interview_flags(tracker):
    send(
        tracker,
        "bridge/devices",
        [
            {"ieee_address": SENSOR, "friendly_name": "S", "interview_completed": False},
            {"ieee_address": PLUG, "friendly_name": "P", "interviewing": True},
        ],
    )
    assert tracker.data["devices"][SENSOR]["interview_state"] == "FAILED"
    assert tracker.data["devices"][PLUG]["interview_state"] == "IN_PROGRESS"


def test_bad_payloads_are_ignored(tracker):
    send(tracker, "bridge/devices", "not json")
    send(tracker, "bridge/devices", {"not": "a list"})
    send(tracker, "bridge/devices", [{"friendly_name": "no ieee"}])
    assert tracker.data["devices"] == {}


# ------------------------------------------------------------------ bridge/event


def test_join_leave_and_interview_events_notify(tracker):
    notified = []
    tracker.on_event = notified.append
    send(tracker, "bridge/event", {"type": "device_joined", "data": {"ieee_address": SENSOR, "friendly_name": SENSOR}})
    send(
        tracker,
        "bridge/event",
        {
            "type": "device_interview",
            "data": {
                "ieee_address": SENSOR,
                "status": "successful",
                "definition": {"model": "FK-1", "vendor": "FakeCo", "exposes": [{"property": "humidity"}]},
            },
        },
    )
    send(tracker, "bridge/event", {"type": "device_interview", "data": {"ieee_address": PLUG, "status": "failed"}})
    send(tracker, "bridge/event", {"type": "device_leave", "data": {"ieee_address": SENSOR}})
    send(tracker, "bridge/event", {"type": "device_announce", "data": {"ieee_address": SENSOR}})
    send(tracker, "bridge/event", {"type": "something_else", "data": {"ieee_address": SENSOR}})

    assert [e["kind"] for e in events_of(tracker)] == ["joined", "interview successful", "interview failed", "left"]
    assert [e["kind"] for e in notified] == ["joined", "interview failed", "left"]
    rec = tracker.data["devices"][SENSOR]
    assert rec["model"] == "FK-1"
    assert rec["features"] == ["humidity"]
    assert events_of(tracker, "interview successful")[0]["detail"] == "FakeCo FK-1"


def test_rejoin_during_targeted_pairing(tracker):
    tracker.pairing_sessions["zigbee2mqtt"] = {"target": SENSOR, "active": True, "joined": [], "via_name": "Hall plug"}
    send(tracker, "bridge/event", {"type": "device_joined", "data": {"ieee_address": SENSOR}})
    session = tracker.pairing_sessions["zigbee2mqtt"]
    assert session["rejoined"]
    assert session["joined"] == [SENSOR]
    assert events_of(tracker, "re-paired")[0]["detail"] == "Rejoined while pairing was open only through Hall plug"


# ------------------------------------------------------------------ availability


def test_availability_outages(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    send(tracker, "Hall sensor/availability", "online")
    rec = tracker.data["devices"][SENSOR]
    assert rec["availability"] == "online"
    assert events_of(tracker) == []  # unknown -> online is not news

    clock.advance(hours=1)
    went_down = clock.iso()
    send(tracker, "Hall sensor/availability", '{"state": "offline"}')
    assert rec["availability"] == "offline"
    assert rec["availability_since"] == went_down
    assert rec["outages"] == [{"start": went_down, "end": None}]

    send(tracker, "Hall sensor/availability", "offline")  # repeat: no new outage
    clock.advance(minutes=30)
    send(tracker, "Hall sensor/availability", "online")
    assert rec["outages"] == [{"start": went_down, "end": clock.iso()}]
    assert [e["kind"] for e in events_of(tracker)] == ["offline", "online"]


def test_availability_ignores_unknown_devices_and_states(tracker):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Hall sensor")])
    send(tracker, "Nobody/availability", "offline")
    send(tracker, "Hall sensor/availability", "maybe")
    send(tracker, "Hall sensor/availability", "{broken")
    assert tracker.data["devices"][SENSOR]["availability"] is None


def test_outages_are_capped(tracker, clock, monkeypatch):
    import tracker as tracker_module

    monkeypatch.setattr(tracker_module, "MAX_OUTAGES", 3)
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "S")])
    for _ in range(5):
        clock.advance(60)
        send(tracker, "S/availability", "offline")
        clock.advance(60)
        send(tracker, "S/availability", "online")
    assert len(tracker.data["devices"][SENSOR]["outages"]) == 3


# ------------------------------------------------------------------ device state


def test_state_updates_readings_metrics_and_battery(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "S")])
    send(tracker, "S", {"linkquality": 120, "battery": 80, "battery_low": False, "temperature": 20.5})
    rec = tracker.data["devices"][SENSOR]
    assert rec["lqi"] == 120
    assert rec["battery"] == 80
    assert rec["battery_low"] is False
    assert rec["last_seen"] == clock.iso()
    assert rec["readings"]["temperature"] == {"value": 20.5, "changed_at": clock.iso(), "reports": 0}
    series = tracker.metrics.series[SENSOR]
    assert series["msg"][0][1] == 1
    assert series["lqi"][0][1:] == [120.0, 1]

    clock.advance(minutes=10)
    send(tracker, "S", {"temperature": 20.5})
    assert rec["readings"]["temperature"]["reports"] == 1
    send(tracker, "S", {"temperature": 21})
    assert rec["readings"]["temperature"] == {"value": 21, "changed_at": clock.iso(), "reports": 0}


def test_state_last_seen_from_payload(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "S")])
    later_ms = int((clock.now + 120) * 1000)
    send(tracker, "S", {"last_seen": later_ms})
    clock.advance(120)
    assert tracker.data["devices"][SENSOR]["last_seen"] == clock.iso()


def test_battery_jump_records_replacement(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "S")])
    send(tracker, "S", {"battery": 20})
    clock.advance(days=1)
    send(tracker, "S", {"battery": 100})
    replacements = tracker.data["devices"][SENSOR]["battery_info"]["replacements"]
    assert [(r["date"], r["source"]) for r in replacements] == [(clock.iso()[:10], "Detected")]
    assert events_of(tracker, "battery replaced")[0]["detail"].endswith("Battery went from 20% to 100% (detected)")


def test_firmware_update_events(tracker):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "S")])
    send(tracker, "S", {"update": {"state": "available", "installed_version": 1, "latest_version": 2}})
    send(tracker, "S", {"update": {"state": "updating", "installed_version": 1, "latest_version": 2, "progress": 50}})
    assert tracker.data["devices"][SENSOR]["update"]["progress"] == 50
    send(tracker, "S", {"update": {"state": "idle", "installed_version": 2, "latest_version": 2}})
    assert [e["kind"] for e in events_of(tracker)] == ["update available", "firmware updated"]
    assert events_of(tracker, "firmware updated")[0]["detail"] == "Now on 2"


def test_non_json_state_only_touches_last_seen(tracker, clock):
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "S")])
    send(tracker, "S", "ON")
    assert tracker.data["devices"][SENSOR]["last_seen"] == clock.iso()
    assert SENSOR not in tracker.metrics.series


# ------------------------------------------------------------------ bindings


def with_bindings(device: dict, *targets: tuple[str, str]) -> dict:
    """targets: (cluster, ieee_or_group) pairs on endpoint 1."""
    bindings = []
    for cluster, target in targets:
        if target.startswith("group:"):
            bindings.append({"cluster": cluster, "target": {"type": "group", "id": int(target[6:])}})
        else:
            bindings.append({"cluster": cluster, "target": {"type": "endpoint", "ieee_address": target, "endpoint": 1}})
    device["endpoints"] = {"1": {"bindings": bindings}}
    return device


def bound_list(*targets):
    return [coordinator(), z2m_device(PLUG, "Lamp", "Router"), with_bindings(z2m_device(SENSOR, "Switch"), *targets)]


def test_bindings_baseline_then_loss(tracker, clock):
    send(tracker, "bridge/groups", [{"id": 5, "friendly_name": "Downstairs"}])
    send(tracker, "bridge/devices", bound_list(("genOnOff", PLUG), ("genPowerCfg", COORD), ("genLevelCtrl", "group:5")))
    rec = tracker.data["devices"][SENSOR]
    assert [b["key"] for b in rec["bindings"]] == [
        "1:genLevelCtrl>group:5",
        f"1:genOnOff>{PLUG}/1",
        f"1:genPowerCfg>{COORD}/1",
    ]
    assert events_of(tracker) == []

    clock.advance(minutes=1)
    send(tracker, "bridge/devices", bound_list(("genLevelCtrl", "group:5")))
    lost = rec["lost_bindings"]
    assert [(b["label"], b["coordinator"], b["lost_at"]) for b in lost] == [
        ("genOnOff → Lamp", False, clock.iso()),
        ("genPowerCfg → Coordinator", True, clock.iso()),
    ]
    assert events_of(tracker, "binding removed")[0]["detail"] == "genOnOff → Lamp; genPowerCfg → Coordinator"
    assert tracker.binding_label("zigbee2mqtt", {"cluster": "genScenes", "target": "group:5"}) == (
        "genScenes → group Downstairs"
    )


def test_lost_binding_restored(tracker, clock):
    send(tracker, "bridge/devices", bound_list(("genOnOff", PLUG)))
    send(tracker, "bridge/devices", bound_list())
    assert len(tracker.data["devices"][SENSOR]["lost_bindings"]) == 1
    send(tracker, "bridge/devices", bound_list(("genOnOff", PLUG)))
    assert tracker.data["devices"][SENSOR]["lost_bindings"] == []
    assert [e["kind"] for e in events_of(tracker)] == ["binding removed", "binding restored"]


def test_new_binding_is_just_added(tracker):
    send(tracker, "bridge/devices", bound_list())
    send(tracker, "bridge/devices", bound_list(("genOnOff", PLUG)))
    assert [e["kind"] for e in events_of(tracker)] == ["binding added"]


def test_unbind_requested_through_z2m_is_not_lost(tracker, clock):
    send(tracker, "bridge/devices", bound_list(("genOnOff", PLUG)))
    send(tracker, "bridge/response/device/unbind", {"status": "ok", "data": {"from": "Switch/1", "to": "Lamp"}})
    clock.advance(60)
    send(tracker, "bridge/devices", bound_list())
    assert tracker.data["devices"][SENSOR]["lost_bindings"] == []
    assert [e["kind"] for e in events_of(tracker)] == ["unbound"]


def test_unbind_long_ago_counts_as_loss(tracker, clock):
    send(tracker, "bridge/devices", bound_list(("genOnOff", PLUG)))
    send(tracker, "bridge/response/device/unbind", {"status": "ok", "data": {"from": "Switch"}})
    clock.advance(minutes=4)
    send(tracker, "bridge/devices", bound_list())
    assert len(tracker.data["devices"][SENSOR]["lost_bindings"]) == 1


def test_bindings_ignored_without_endpoint_data_or_interview(tracker):
    send(tracker, "bridge/devices", bound_list(("genOnOff", PLUG)))
    plain = [coordinator(), z2m_device(PLUG, "Lamp", "Router"), z2m_device(SENSOR, "Switch")]
    send(tracker, "bridge/devices", plain)
    rec = tracker.data["devices"][SENSOR]
    assert len(rec["bindings"]) == 1
    assert rec["lost_bindings"] == []


def test_acknowledge_bindings(tracker, clock):
    send(tracker, "bridge/devices", bound_list(("genOnOff", PLUG)))
    send(tracker, "bridge/devices", bound_list())
    assert tracker.acknowledge_bindings(SENSOR)
    assert tracker.data["devices"][SENSOR]["lost_bindings"] == []
    assert events_of(tracker, "binding change accepted")[0]["detail"] == "1 lost binding(s) dismissed"
    assert not tracker.acknowledge_bindings("0xmissing")
