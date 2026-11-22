"""ZHA support: the zigbee_stack option, mapping ZHA data, and the ZHA source."""

from __future__ import annotations

import json
import threading

from conftest import FIXTURES, send, z2m_device
from zha import (
    ZhaSource,
    battery_entities,
    device_entries,
    device_names,
    networkmap_value,
    sources,
    state_payload,
    zha_checks,
    zha_ieee,
    zigbee_stack,
)


def test_stack_defaults_to_z2m():
    assert zigbee_stack({}) == "z2m"


def test_stack_zha():
    assert zigbee_stack({"zigbee_stack": " ZHA "}) == "zha"


def test_stack_unknown_is_z2m():
    for value in ("deconz", None, 5, ""):
        assert zigbee_stack({"zigbee_stack": value}) == "z2m"


# ------------------------------------------------------------------ mapping

AIR = "0xaabbcc0000000002"
BUTTON = "0xaabbcc0000000003"


def _devices():
    return json.loads((FIXTURES / "zha_devices.json").read_text())


def test_zha_ieee_converts():
    assert zha_ieee("AA:BB:CC:dd:ee:ff:00:11") == "0xaabbccddeeff0011"


def test_zha_ieee_rejects_bad():
    for value in ("", "88:1a", "zz:bb:cc:dd:ee:ff:00:11", None, 5, "0xaabbccddeeff0011"):
        assert zha_ieee(value) is None


def test_device_entries_types_and_power():
    entries = {e["ieee_address"]: e for e in device_entries(_devices(), {BUTTON})}
    assert entries[BUTTON]["friendly_name"] == "Test Button"
    assert entries[BUTTON]["type"] == "EndDevice"
    assert entries[BUTTON]["power_source"] == "Battery"
    assert entries[AIR]["power_source"] == "Mains (single phase)"
    assert entries[AIR]["definition"] == {"model": "AIR-1", "vendor": "FakeCo"}
    assert entries[AIR]["interview_state"] == "SUCCESSFUL"
    assert entries["0xaabbcc0000000001"]["type"] == "Coordinator"


def test_battery_or_unknown_without_battery_entity_kept():
    entries = {e["ieee_address"]: e for e in device_entries(_devices(), set())}
    assert entries[BUTTON]["power_source"] == "Battery or Unknown"


def test_duplicate_names_get_suffix():
    names = {e["ieee_address"]: e["friendly_name"] for e in device_entries(_devices(), set())}
    assert names["0xaabbcc0000000004"] == "FakeCo Plug (0004)"
    assert names["0xaabbcc0000000005"] == "FakeCo Plug (0005)"
    assert names[AIR] == "Test Air Sensor"


def test_state_payload_only_when_newer():
    button = _devices()[2]
    assert state_payload(button, None) == {"last_seen": "2026-06-01T11:58:00", "linkquality": 160, "rssi": -70}
    assert state_payload(button, "2026-06-01T11:58:00") is None
    assert state_payload({**button, "last_seen": None}, None) is None
    assert state_payload(_devices()[1], None) == {"last_seen": "2026-06-01T11:59:30", "linkquality": 210}


def test_state_payload_keeps_naive_last_seen(make_tracker):
    tracker = make_tracker(["zha"])
    tracker.apply_device_list("zha", device_entries(_devices(), set()))
    tracker.apply_state("zha", "Test Button", state_payload(_devices()[2], None))
    assert tracker.data["devices"][BUTTON]["last_seen"] == "2026-06-01T11:58:00+00:00"
    assert tracker.data["devices"][BUTTON]["lqi"] == 160


def test_networkmap_parent(make_tracker):
    tracker = make_tracker(["zha"])
    tracker.apply_device_list("zha", device_entries(_devices(), set()))
    tracker.apply_networkmap("zha", networkmap_value(_devices()))
    routing = tracker.data["devices"][BUTTON]["routing"]
    assert routing["parent"]["ieee"] == AIR
    assert routing["parent"]["lqi"] == 150
    assert tracker.data["devices"][AIR]["routing"]["parent"]["ieee"] == "0xaabbcc0000000001"


def test_battery_entities_from_registry():
    # HA's entity registry list has no device class; the states carry it.
    registry = [
        {"entity_id": "sensor.test_button_battery", "device_id": "reg-3", "platform": "zha"},
        {"entity_id": "sensor.test_air_sensor_temperature", "device_id": "reg-2", "platform": "zha"},
        {"entity_id": "sensor.other_battery", "device_id": "reg-99", "platform": "zha"},
        {"entity_id": "binary_sensor.test_button_low", "device_id": "reg-3", "platform": "zha"},
        {"entity_id": "sensor.mqtt_battery", "device_id": "reg-3", "platform": "mqtt"},
    ]
    states = [
        {"entity_id": "sensor.test_button_battery", "state": "73", "attributes": {"device_class": "battery"}},
        {
            "entity_id": "sensor.test_air_sensor_temperature",
            "state": "20",
            "attributes": {"device_class": "temperature"},
        },
        {"entity_id": "sensor.other_battery", "state": "50", "attributes": {"device_class": "battery"}},
        {"entity_id": "binary_sensor.test_button_low", "state": "off", "attributes": {"device_class": "battery"}},
        {"entity_id": "sensor.mqtt_battery", "state": "10", "attributes": {"device_class": "battery"}},
    ]
    assert battery_entities(_devices(), registry, states) == {
        "sensor.test_button_battery": BUTTON,
        "binary_sensor.test_button_low": BUTTON,
    }


# ------------------------------------------------------------------ ZHA source

PLUG_5 = "0xaabbcc0000000005"


def _source(tracker, connect=lambda: None):
    return ZhaSource(tracker, connect, threading.Event())


def _msg_count(tracker, ieee):
    return sum(p[1] for p in tracker.metrics.series.get(ieee, {}).get("msg", []))


def test_apply_devices_lists_and_online(make_tracker):
    tracker = make_tracker(["zha"])
    _source(tracker).apply_devices(_devices())
    records = tracker.data["devices"]
    assert len(records) == 5
    assert records[AIR]["availability"] == "online"
    assert records[AIR]["lqi"] == 210
    assert tracker.data["bridges"]["zha"]["state"] == "online"


def test_unavailable_device_goes_offline(make_tracker):
    tracker = make_tracker(["zha"])
    _source(tracker).apply_devices(_devices())
    assert tracker.data["devices"][PLUG_5]["availability"] == "offline"


def test_failed_poll_removes_nothing(make_tracker, clock):
    tracker = make_tracker(["zha"])
    source = _source(tracker)
    source.apply_devices(_devices())
    source.apply_devices(None)
    source.apply_devices([])
    assert all(r["status"] == "active" for r in tracker.data["devices"].values())
    assert tracker.data["bridges"]["zha"]["state"] == "offline"
    clock.advance(minutes=3)
    issues, _ = tracker.evaluate_health()
    assert [i for i in issues if i["kind"] == "network_offline"]


def test_battery_updates_record(make_tracker):
    tracker = make_tracker(["zha"])
    source = _source(tracker)
    source.set_battery_entities({"sensor.test_button_battery": BUTTON, "binary_sensor.test_button_low": BUTTON})
    source.apply_devices(_devices())
    source.apply_battery("sensor.test_button_battery", "87")
    source.apply_battery("binary_sensor.test_button_low", "on")
    record = tracker.data["devices"][BUTTON]
    assert record["battery"] == 87
    assert record["battery_low"] is True
    assert record["power_source"] == "Battery"


def test_battery_ignores_dead_states(make_tracker):
    tracker = make_tracker(["zha"])
    source = _source(tracker)
    source.set_battery_entities({"sensor.test_button_battery": BUTTON})
    source.apply_devices(_devices())
    source.apply_battery("sensor.test_button_battery", "60")
    for state in ("unavailable", "unknown", "abc", None):
        source.apply_battery("sensor.test_button_battery", state)
    assert tracker.data["devices"][BUTTON]["battery"] == 60


def test_last_seen_counts_once(make_tracker):
    tracker = make_tracker(["zha"])
    source = _source(tracker)
    source.apply_devices(_devices())
    source.apply_devices(_devices())
    assert _msg_count(tracker, BUTTON) == 1


class FakeSocket:
    """Answers the commands ZhaSource sends, like Home Assistant's websocket."""

    def __init__(self, stop: threading.Event, devices: list[dict], stop_on_poll: int | None = None) -> None:
        self.stop, self.devices, self.queue, self.sent = stop, devices, [], []
        # Stop the app while its n-th zha/devices request is waiting for an answer.
        self.stop_on_poll = stop_on_poll

    def settimeout(self, _):
        pass

    def send(self, text):
        message = json.loads(text)
        self.sent.append(message)
        polls = sum(1 for m in self.sent if m["type"] == "zha/devices")
        if message["type"] == "zha/devices" and polls == self.stop_on_poll:
            self.stop.set()
            return
        results = {
            "config/entity_registry/list": [
                {"entity_id": "sensor.test_button_battery", "device_id": "reg-3", "platform": "zha"}
            ],
            "zha/devices": self.devices,
            "zha/network/settings": {"settings": {"network_info": {"channel": 15, "pan_id": "1A2B"}}},
            "get_config": {"time_zone": "UTC"},
            "get_states": [
                {"entity_id": "sensor.test_button_battery", "state": "42", "attributes": {"device_class": "battery"}}
            ],
        }
        self.queue.append(
            {"id": message["id"], "type": "result", "success": True, "result": results.get(message["type"])}
        )

    def recv(self):
        if not self.queue:
            self.stop.set()
            raise TimeoutError
        return json.dumps(self.queue.pop(0))

    def close(self):
        pass


def test_run_loop_with_fake_socket(make_tracker):
    tracker = make_tracker(["zha"])
    stop = threading.Event()
    sock = FakeSocket(stop, _devices())
    ZhaSource(tracker, lambda: sock, stop).run()
    assert len(tracker.data["devices"]) == 5
    assert tracker.data["devices"][BUTTON]["battery"] == 42
    sent = {m["type"] for m in sock.sent}
    assert {"zha/devices", "get_states", "subscribe_events", "subscribe_trigger"} <= sent
    # Read-only: nothing that changes ZHA or Home Assistant.
    allowed = {"zha/devices", "zha/network/settings", "get_states", "get_config", "config/entity_registry/list"}
    assert not sent - allowed - {"subscribe_events", "subscribe_trigger", "unsubscribe_events"}


def test_tracker_snapshot_has_stack(make_tracker):
    zha_tracker = make_tracker(["zha"], options={"zigbee_stack": "zha"})
    assert json.loads(zha_tracker.snapshot_json())["stack"] == "zha"
    assert json.loads(make_tracker().snapshot_json())["stack"] == "z2m"


def test_zha_blocked_paths_cover_mqtt_actions():
    from zha import ZHA_BLOCKED_PATHS

    for path in ("/api/rename", "/api/ota", "/api/pairing", "/api/networkmap", "/api/ping", "/api/import"):
        assert path in ZHA_BLOCKED_PATHS
    for path in ("/api/note", "/api/area", "/api/settings", "/api/forget", "/api/battery"):
        assert path not in ZHA_BLOCKED_PATHS


def test_battery_keeps_reported_time(make_tracker):
    # Replaying the current battery value on connect must not make the device look heard from just now.
    tracker = make_tracker(["zha"])
    source = _source(tracker)
    source.set_battery_entities({"sensor.test_button_battery": BUTTON})
    source.apply_devices(_devices())
    source.apply_battery("sensor.test_button_battery", "70", "2026-06-01T11:58:00+00:00")
    record = tracker.data["devices"][BUTTON]
    assert record["battery"] == 70
    assert record["last_seen"] == "2026-06-01T11:58:00+00:00"


def test_networkmap_child_entry_gives_parent(make_tracker):
    # End devices have no neighbour table: their parent only lists them as a Child.
    devices = _devices()
    devices[2]["neighbors"] = []
    devices[0]["neighbors"] = [{"ieee": "aa:bb:cc:00:00:00:00:03", "relationship": "Child", "lqi": "170", "depth": "1"}]
    tracker = make_tracker(["zha"])
    tracker.apply_device_list("zha", device_entries(devices, set()))
    tracker.apply_networkmap("zha", networkmap_value(devices))
    parent = tracker.data["devices"][BUTTON]["routing"]["parent"]
    assert parent == {"ieee": "0xaabbcc0000000001", "lqi": 170}
    # The air sensor names the coordinator as its Parent, the coordinator lists the button as Child.
    assert tracker.data["devices"]["0xaabbcc0000000001"]["routing"]["children"] == 2


def test_networkmap_link_seen_from_both_sides_counts_once(make_tracker):
    devices = _devices()
    devices[0]["neighbors"] = [{"ieee": "aa:bb:cc:00:00:00:00:02", "relationship": "Child", "lqi": "190", "depth": "1"}]
    value = networkmap_value(devices)
    pairs = [(link["sourceIeeeAddr"], link["targetIeeeAddr"]) for link in value["links"] if link["relationship"] == 1]
    assert pairs.count((AIR, "0xaabbcc0000000001")) == 1
    tracker = make_tracker(["zha"])
    tracker.apply_device_list("zha", device_entries(devices, set()))
    tracker.apply_networkmap("zha", value)
    assert tracker.data["devices"][AIR]["routing"]["parent"] == {"ieee": "0xaabbcc0000000001", "lqi": 200}
    # Only the air sensor hangs off the coordinator here (the button's parent is the air sensor).
    assert tracker.data["devices"]["0xaabbcc0000000001"]["routing"]["children"] == 1


# ------------------------------------------------------------------ final review fixes


def test_battery_replay_after_replacement_stays_replaced(make_tracker, clock):
    # A replayed old level (HA restart) must not count as a reading newer than a marked replacement.
    tracker = make_tracker(["zha"])
    source = _source(tracker)
    source.set_battery_entities({"sensor.test_button_battery": BUTTON})
    source.apply_devices(_devices())
    source.apply_battery("sensor.test_button_battery", "5", replay=True)
    tracker.add_battery_replacement(BUTTON, "2026-06-01", "Manual", "", None)
    clock.advance(hours=1)
    source.apply_battery("sensor.test_button_battery", "5", "2026-06-01T12:59:00+00:00", replay=True)
    assert not any(d["ieee"] == BUTTON for g in tracker.battery_shopping(60) for d in g["devices"])
    assert tracker.data["devices"][BUTTON]["last_seen"] == "2026-06-01T11:58:00+00:00"


def test_live_battery_report_after_replacement_counts(make_tracker, clock):
    tracker = make_tracker(["zha"])
    source = _source(tracker)
    source.set_battery_entities({"sensor.test_button_battery": BUTTON})
    source.apply_devices(_devices())
    tracker.add_battery_replacement(BUTTON, "2026-06-01", "Manual", "", None)
    clock.advance(hours=1)
    source.apply_battery("sensor.test_button_battery", "4", "2026-06-01T12:59:00+00:00")
    assert any(d["ieee"] == BUTTON for g in tracker.battery_shopping(60) for d in g["devices"])


def test_state_payload_after_clocks_go_back():
    # Naive local times jump back an hour at the end of summer time; updates must still go through.
    button = _devices()[2]
    assert state_payload(button, "2026-06-01T12:30:00") is not None


class SilentSocket:
    """Accepts commands and never answers, like a half-open connection."""

    def settimeout(self, _):
        pass

    def send(self, _):
        pass

    def recv(self):
        raise TimeoutError

    def close(self):
        pass


def test_call_gives_up_on_a_silent_socket(make_tracker):
    import pytest

    source = _source(make_tracker(["zha"]))
    source.call_timeout = 0.05
    with pytest.raises(TimeoutError):
        source._call(SilentSocket(), {"type": "zha/devices"})


def test_stopping_records_no_outage(make_tracker):
    tracker = make_tracker(["zha"])
    stop = threading.Event()
    sock = FakeSocket(stop, _devices(), stop_on_poll=2)
    ZhaSource(tracker, lambda: sock, stop, poll_seconds=0).run()
    assert tracker.data["bridges"]["zha"]["state"] == "online"
    assert not [e for e in tracker.data["events"] if e["kind"] == "network offline"]


def test_link_and_unlink_allowed_with_zha():
    from zha import ZHA_BLOCKED_PATHS

    assert "/api/link" not in ZHA_BLOCKED_PATHS
    assert "/api/unlink" not in ZHA_BLOCKED_PATHS
    assert "/api/replace" in ZHA_BLOCKED_PATHS


# ------------------------------------------------------------------ second review


def _routing_events(tracker):
    return [e for e in tracker.data["events"] if e["kind"] == "routing scanned"]


def test_never_heard_device_is_not_seen_now(make_tracker):
    # ZHA reports the current time as "last seen" for a device it has never heard, new on every read.
    tracker = make_tracker(["zha"])
    source = _source(tracker)
    devices = _devices()
    for minute in ("12:00:00", "12:01:00", "12:02:00"):
        devices[4]["last_seen"] = f"2026-06-01T{minute}"
        source.apply_devices(devices)
    assert tracker.data["devices"][PLUG_5]["last_seen"] is None
    assert _msg_count(tracker, PLUG_5) == 0


def test_unavailable_device_keeps_its_real_last_seen(make_tracker):
    tracker = make_tracker(["zha"])
    source = _source(tracker)
    source.apply_devices(_devices())
    source.apply_devices(_devices())
    assert tracker.data["devices"][PLUG_5]["last_seen"] == "2026-06-01T11:59:10+00:00"


def test_network_map_waits_until_scan_settles(make_tracker):
    # ZHA fills neighbour tables one device at a time during a scan: no map per partial read.
    tracker = make_tracker(["zha"])
    source = _source(tracker)
    partial = _devices()
    partial[2]["neighbors"] = []
    source.apply_devices(partial)
    source.apply_devices(_devices())
    assert _routing_events(tracker) == []
    source.apply_devices(_devices())
    assert len(_routing_events(tracker)) == 1
    assert tracker.data["devices"][BUTTON]["routing"]["parent"]["ieee"] == AIR


def test_network_map_ignores_lqi_only_changes(make_tracker):
    tracker = make_tracker(["zha"])
    source = _source(tracker)
    source.apply_devices(_devices())
    source.apply_devices(_devices())
    assert len(_routing_events(tracker)) == 1
    changed = _devices()
    changed[2]["neighbors"][0]["lqi"] = "90"
    source.apply_devices(changed)
    source.apply_devices(changed)
    assert len(_routing_events(tracker)) == 1
    # Once a day the map is refreshed anyway, so link quality stays current.
    source._map_applied_at -= 25 * 3600
    source.apply_devices(changed)
    assert len(_routing_events(tracker)) == 2
    assert tracker.data["devices"][BUTTON]["routing"]["parent"]["lqi"] == 90


def test_new_battery_entity_gets_its_current_value(make_tracker):
    # A battery sensor found later (a newly paired device) shows its level without waiting for a report.
    tracker = make_tracker(["zha"])
    stop = threading.Event()
    sock = FakeSocket(stop, _devices())
    source = ZhaSource(tracker, lambda: sock, stop)
    source.apply_devices(_devices())
    assert tracker.data["devices"][BUTTON]["battery"] is None
    source._refresh_batteries(sock, _devices())
    assert tracker.data["devices"][BUTTON]["battery"] == 42
    assert tracker.data["devices"][BUTTON]["power_source"] == "Battery"


def _battery_event(old, new, when="2026-06-01T12:30:00+00:00"):
    return {
        "event_type": "state_changed",
        "data": {
            "entity_id": "sensor.test_button_battery",
            "old_state": None if old is None else {"state": old},
            "new_state": {"state": new, "last_reported": when},
        },
    }


def test_battery_back_from_unavailable_is_not_a_new_report(make_tracker):
    # ZHA reloading (or the coordinator reconnecting) flips every sensor to unavailable and back
    # to its old value. That says nothing about when the device was last heard.
    tracker = make_tracker(["zha"])
    source = _source(tracker)
    source.set_battery_entities({"sensor.test_button_battery": BUTTON})
    source.apply_devices(_devices())
    source._event(_battery_event("unavailable", "70"))
    record = tracker.data["devices"][BUTTON]
    assert record["battery"] == 70
    assert record["last_seen"] == "2026-06-01T11:58:00+00:00"
    source._event(_battery_event(None, "70"))
    assert tracker.data["devices"][BUTTON]["last_seen"] == "2026-06-01T11:58:00+00:00"


def test_battery_change_is_a_new_report(make_tracker):
    tracker = make_tracker(["zha"])
    source = _source(tracker)
    source.set_battery_entities({"sensor.test_button_battery": BUTTON})
    source.apply_devices(_devices())
    source._event(_battery_event("70", "69"))
    record = tracker.data["devices"][BUTTON]
    assert record["battery"] == 69
    assert record["last_seen"] == "2026-06-01T12:30:00+00:00"


def test_no_payload_change_reports_with_zha(make_tracker):
    # Signal and battery arrive as separate updates with ZHA; that mix is not a device changing
    # what it sends (payload tracking is a Zigbee2MQTT thing).
    tracker = make_tracker(["zha"])
    tracker.apply_device_list("zha", device_entries(_devices(), {BUTTON}))
    for n in range(40):
        tracker.apply_state(
            "zha", "Test Button", {"last_seen": f"2026-06-01T10:{n:02d}:00", "linkquality": 150, "rssi": -60}
        )
    for n in range(60):
        tracker.apply_state("zha", "Test Button", {"battery": 70 - n % 2})
        tracker.apply_state(
            "zha", "Test Button", {"last_seen": f"2026-06-01T11:{n:02d}:00", "linkquality": 150, "rssi": -60}
        )
    for n in range(40):
        tracker.apply_state(
            "zha", "Test Button", {"last_seen": f"2026-06-01T12:{n:02d}:00", "linkquality": 150, "rssi": -60}
        )
    record = tracker.data["devices"][BUTTON]
    assert not record.get("payload_change")
    assert not [e for e in tracker.data["events"] if "payload" in e["kind"]]


# ------------------------------------------------------------------ open items


def test_last_seen_uses_home_assistants_time_zone(make_tracker):
    # ZHA's last seen is local time without a zone: read it in HA's zone, whatever the app's own is.
    tracker = make_tracker(["zha"])  # the tracker's zone is UTC in tests
    source = _source(tracker)
    source.set_time_zone("Australia/Sydney")
    source.set_battery_entities({"sensor.test_button_battery": BUTTON})
    source.apply_devices(_devices())
    assert tracker.data["devices"][BUTTON]["last_seen"] == "2026-06-01T01:58:00+00:00"
    source.apply_battery("sensor.test_button_battery", "70", replay=True)
    assert tracker.data["devices"][BUTTON]["last_seen"] == "2026-06-01T01:58:00+00:00"


def test_unknown_time_zone_falls_back(make_tracker):
    tracker = make_tracker(["zha"])
    source = _source(tracker)
    source.set_time_zone("Not/AZone")
    source.apply_devices(_devices())
    assert tracker.data["devices"][BUTTON]["last_seen"] == "2026-06-01T11:58:00+00:00"


def test_same_name_and_same_address_ending_stay_apart():
    devices = _devices()
    devices[4]["ieee"] = "aa:bb:cc:00:11:00:00:04"
    names = device_names(devices)
    assert len(set(names.values())) == len(names) == 5
    assert names["0xaabbcc0000000004"] != names["0xaabbcc0011000004"]


def test_users_own_name_matching_a_suffixed_one_stays_apart():
    devices = _devices()
    devices[1]["user_given_name"] = "FakeCo Plug (0004)"
    names = device_names(devices)
    assert len(set(names.values())) == 5


def test_zha_join_and_leave_notify(make_tracker):
    tracker = make_tracker(["zha"])
    notified = []
    tracker.on_event = lambda event: notified.append((event["kind"], event["name"]))
    source = _source(tracker)
    source.apply_devices(_devices()[:3])
    assert notified == []  # the first list is the network as found, not devices joining
    source.apply_devices(_devices()[:4])
    assert notified == [("joined", "FakeCo Plug")]
    source.apply_devices(_devices()[:3])
    assert notified[-1] == ("left", "FakeCo Plug")


def test_z2m_list_changes_do_not_notify(tracker):
    notified = []
    tracker.on_event = lambda event: notified.append(event["kind"])
    send(tracker, "bridge/devices", [z2m_device(AIR, "Air", "Router")])
    send(tracker, "bridge/devices", [z2m_device(AIR, "Air", "Router"), z2m_device(BUTTON, "Button")])
    send(tracker, "bridge/devices", [z2m_device(AIR, "Air", "Router")])
    assert notified == []


def test_zha_checks_leave_out_zigbee2mqtt_only_ones():
    from health import CHECKS

    keys = {c[0] for c in zha_checks(CHECKS)}
    assert {"offline", "late", "battery_low", "weak_signal", "network_offline", "joined", "left"} <= keys
    assert not keys & {"update", "frozen", "pairing_open", "restart_required", "stale_retained", "interview_failed"}
    assert len(zha_checks(CHECKS)) < len(CHECKS)


def test_network_settings_give_the_radio_channel(make_tracker):
    tracker = make_tracker(["zha"])
    source = _source(tracker)
    source.apply_devices(_devices())
    source.apply_network({"radio_type": "znp", "settings": {"network_info": {"channel": 11, "pan_id": "1A2B"}}})
    networks = tracker.radio()["networks"]
    assert [(n["topic"], n["channel"], n["pan_id"]) for n in networks] == [("zha", 11, 0x1A2B)]
    source.apply_network({"settings": {"network_info": {"channel": "x"}}})
    assert tracker.radio()["networks"][0]["channel"] == 11


def test_battery_trigger_event(make_tracker):
    tracker = make_tracker(["zha"])
    source = _source(tracker)
    source.set_battery_entities({"sensor.test_button_battery": BUTTON})
    source.apply_devices(_devices())
    trigger = {
        "entity_id": "sensor.test_button_battery",
        "from_state": {"state": "70"},
        "to_state": {"state": "68", "last_reported": "2026-06-01T12:30:00+00:00"},
    }
    source._event({"variables": {"trigger": trigger}})
    assert tracker.data["devices"][BUTTON]["battery"] == 68
    assert tracker.data["devices"][BUTTON]["last_seen"] == "2026-06-01T12:30:00+00:00"


def test_listens_only_to_battery_entities(make_tracker):
    tracker = make_tracker(["zha"])
    stop = threading.Event()
    sock = FakeSocket(stop, _devices())
    ZhaSource(tracker, lambda: sock, stop).run()
    triggers = [m for m in sock.sent if m["type"] == "subscribe_trigger"]
    assert [m["trigger"] for m in triggers] == [{"platform": "state", "entity_id": ["sensor.test_button_battery"]}]
    assert not [m for m in sock.sent if m.get("event_type") == "state_changed"]


def test_falls_back_to_all_state_changes_when_trigger_is_refused(make_tracker):
    tracker = make_tracker(["zha"])
    stop = threading.Event()
    sock = FakeSocket(stop, _devices())
    send = sock.send

    def refusing(text):
        message = json.loads(text)
        if message["type"] == "subscribe_trigger":
            sock.sent.append(message)
            sock.queue.append({"id": message["id"], "type": "result", "success": False, "error": {"message": "no"}})
        else:
            send(text)

    sock.send = refusing
    ZhaSource(tracker, lambda: sock, stop).run()
    assert [m for m in sock.sent if m.get("event_type") == "state_changed"]
    assert tracker.data["bridges"]["zha"]["state"] == "online"


def test_first_poll_after_start_files_no_rhythm_gap(make_tracker, clock):
    tracker = make_tracker(["zha"])
    source = _source(tracker)
    source.apply_devices(_devices())
    assert "rhythm_last" not in tracker.metrics.series[BUTTON]
    devices = _devices()
    clock.advance(minutes=5)
    devices[2]["last_seen"] = "2026-06-01T12:30:00"
    source.apply_devices(devices)
    assert tracker.metrics.series[BUTTON]["rhythm_last"]
    assert tracker.metrics.series[BUTTON].get("gaps", {}) == {}
    clock.advance(minutes=5)
    devices[2]["last_seen"] = "2026-06-01T12:35:00"
    source.apply_devices(devices)
    assert sum(tracker.metrics.series[BUTTON]["gaps"].values()) == 1.0


# ------------------------------------------------------------------ several sources at once
def test_sources_default_is_zigbee2mqtt_only():
    assert sources({}) == {"z2m"}
    assert sources({"zigbee2mqtt": True, "zha": False}) == {"z2m"}


def test_sources_both_at_once():
    assert sources({"zigbee2mqtt": True, "zha": True}) == {"z2m", "zha"}


def test_sources_zha_only_with_the_switches():
    assert sources({"zigbee2mqtt": False, "zha": True}) == {"zha"}


def test_sources_old_zigbee_stack_zha_keeps_reading_zha_only():
    assert sources({"zigbee_stack": "zha"}) == {"zha"}
    # The switches as they come after the update don't change that...
    assert sources({"zigbee_stack": "zha", "zigbee2mqtt": True, "zha": False}) == {"zha"}
    # ...but changing one does.
    assert sources({"zigbee_stack": "zha", "zigbee2mqtt": True, "zha": True}) == {"z2m", "zha"}
    assert sources({"zigbee_stack": "zha", "zigbee2mqtt": False, "zha": False}) == set()


def test_sources_both_off_reads_nothing():
    assert sources({"zigbee2mqtt": False, "zha": False}) == set()


def test_tracker_with_both_networks_reports_z2m_stack_and_finds_each_device(make_tracker):
    from conftest import SENSOR, send, z2m_device

    tracker = make_tracker(["zigbee2mqtt", "zha"])
    send(tracker, "bridge/devices", [z2m_device(SENSOR, "Kitchen sensor")])
    tracker.apply_state("zha", "Hall plug", {"last_seen": "2026-06-01T12:00:00+00:00"})
    assert tracker.instance_of(SENSOR) == "zigbee2mqtt"
    assert tracker.instance_of("0xdoesnotexist") is None
    import json

    assert json.loads(tracker.snapshot_json())["stack"] == "z2m"
    zha_only = make_tracker(["zha"])
    assert json.loads(zha_only.snapshot_json())["stack"] == "zha"


def test_zha_network_is_never_an_mqtt_topic(make_tracker):
    tracker = make_tracker(["zigbee2mqtt", "zha"])
    assert tracker.mqtt_topics == ["zigbee2mqtt"]
    assert make_tracker(["zha"]).mqtt_topics == []


def test_sources_with_matter():
    assert sources({"matter": True}) == {"z2m", "matter"}
    assert sources({"zigbee2mqtt": False, "matter": True}) == {"matter"}
    # Turning Matter on counts as changing a switch: the old zigbee_stack no longer decides alone.
    assert sources({"zigbee_stack": "zha", "matter": True}) == {"z2m", "matter"}
