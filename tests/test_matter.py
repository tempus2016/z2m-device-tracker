"""Matter support: mapping Home Assistant's Matter devices and states into the tracker's network."""

from __future__ import annotations

from matter import (
    KEY_PREFIX,
    MATTER_INSTANCE,
    MatterSource,
    device_entries,
    device_key,
    device_readings,
    matter_devices,
    networkmap_value,
    node_id,
    topology_nodes,
)

SENSOR = {
    "id": "dev-sensor",
    "name": "Hall temperature",
    "name_by_user": None,
    "manufacturer": "matter-node.js",
    "model": "Temperature sensor",
    "config_entries": ["entry-matter"],
    "identifiers": [["matter", "deviceid_174044377A109B9A-0000000000000004-MatterNodeDevice"], ["matter", "serial_x"]],
    "disabled_by": None,
}
PLUG = {
    **SENSOR,
    "id": "dev-plug",
    "name": "Desk plug",
    "model": "Plug",
    "identifiers": [["matter", "deviceid_174044377A109B9A-0000000000000007-MatterNodeDevice"]],
}
OTHER = {**SENSOR, "id": "dev-zha", "identifiers": [["zha", "00:11"]], "config_entries": ["entry-zha"]}

ENTITIES = [
    {"entity_id": "sensor.hall_temperature", "device_id": "dev-sensor", "disabled_by": None},
    {"entity_id": "sensor.hall_battery", "device_id": "dev-sensor", "disabled_by": None},
    {"entity_id": "switch.desk_plug", "device_id": "dev-plug", "disabled_by": None},
]


def states(plug="on", reported="2026-06-01T12:00:00+00:00"):
    return [
        {
            "entity_id": "sensor.hall_temperature",
            "state": "17.0",
            "last_reported": reported,
            "attributes": {"device_class": "temperature"},
        },
        {
            "entity_id": "sensor.hall_battery",
            "state": "64",
            "last_reported": "2026-06-01T11:00:00+00:00",
            "attributes": {"device_class": "battery", "unit_of_measurement": "%"},
        },
        {
            "entity_id": "switch.desk_plug",
            "state": plug,
            "last_reported": "2026-06-01T10:00:00+00:00",
            "attributes": {},
        },
    ]


def test_keys_are_stable_reserved_and_never_zigbee():
    key = device_key(SENSOR)
    assert key.startswith(KEY_PREFIX) and len(key) == 18
    assert device_key(SENSOR) == key and device_key(PLUG) != key
    assert device_key(OTHER) is None
    assert node_id(SENSOR) == 4 and node_id(PLUG) == 7


def test_only_matter_devices_of_loaded_entries():
    assert matter_devices([SENSOR, PLUG, OTHER, {**PLUG, "disabled_by": "user"}], {"entry-matter"}) == [SENSOR, PLUG]
    assert matter_devices([SENSOR], {"another-entry"}) == []


def test_readings_take_the_newest_report_battery_and_availability():
    r = device_readings([SENSOR, PLUG], ENTITIES, states())
    sensor = r[device_key(SENSOR)]
    assert sensor["battery"] == 64.0 and sensor["available"] is True
    assert sensor["last_reported"] == 1780315200.0  # 12:00, the temperature's report, not the battery's 11:00
    assert r[device_key(PLUG)]["battery"] is None
    off = device_readings([PLUG], ENTITIES, states(plug="unavailable"))[device_key(PLUG)]
    assert off["available"] is False and off["last_reported"] is None


def test_entries_use_topology_roles_and_battery_power():
    nodes = topology_nodes(
        {"nodes": [{"id": "4", "node_id": 4, "role": "sleepy_end_device"}, {"id": "7", "node_id": 7, "role": "router"}]}
    )
    entries = device_entries([SENSOR, PLUG], device_readings([SENSOR, PLUG], ENTITIES, states()), nodes)
    assert [(e["friendly_name"], e["type"], e["power_source"]) for e in entries] == [
        ("Hall temperature", "EndDevice", "Battery"),
        ("Desk plug", "Router", None),
    ]


def test_same_names_get_told_apart():
    twin = {**PLUG, "name": "Hall temperature"}
    names = [e["friendly_name"] for e in device_entries([SENSOR, twin], {}, {})]
    assert len(set(names)) == 2


def test_thread_links_become_a_network_map():
    topo = {
        "nodes": [{"id": "4", "node_id": 4}, {"id": "7", "node_id": 7}, {"id": "br", "kind": "border_router"}],
        "connections": [
            {
                "source": "4",
                "target": "7",
                "network": "thread",
                "source_to_target": {"lqi": 120},
                "target_to_source": {"lqi": 200},
            },
            {"source": "7", "target": "br", "network": "thread"},
            {"source": "4", "target": "7", "network": "wifi"},
        ],
    }
    value = networkmap_value([SENSOR, PLUG], topo)
    assert value["links"] == [
        {
            "sourceIeeeAddr": device_key(SENSOR),
            "targetIeeeAddr": device_key(PLUG),
            "lqi": 200,
            "relationship": 2,
            "depth": 1,
        }
    ]
    assert networkmap_value([SENSOR], None)["links"] == []


def test_source_feeds_the_tracker_as_its_own_network(make_tracker, clock):
    import threading

    tracker = make_tracker(["zigbee2mqtt", "matter"])
    source = MatterSource(tracker, connect=lambda: None, stop_event=threading.Event())
    nodes = topology_nodes({"nodes": [{"id": "4", "node_id": 4, "available": True}]})
    source.apply([SENSOR, PLUG], device_readings([SENSOR, PLUG], ENTITIES, states()), nodes)
    record = tracker.data["devices"][device_key(SENSOR)]
    assert record["instance"] == MATTER_INSTANCE and record["status"] == "active"
    assert tracker.instance_of(device_key(PLUG)) == MATTER_INSTANCE
    # A later report moves last seen on; the plug going unavailable marks it offline.
    source.apply(
        [SENSOR, PLUG],
        device_readings([SENSOR, PLUG], ENTITIES, states(plug="unavailable", reported="2026-06-01T12:05:00+00:00")),
        nodes,
    )
    assert tracker.data["devices"][device_key(SENSOR)]["last_seen"].startswith("2026-06-01T12:05")
    assert tracker.data["devices"][device_key(PLUG)]["availability"] == "offline"
    # A failed read marks the Matter network offline, not each device.
    source.apply(None, {}, {})
    assert tracker.data["bridges"][MATTER_INSTANCE]["state"] == "offline"
    assert tracker.mqtt_topics == ["zigbee2mqtt"]


def test_idle_keeps_reading_so_pings_are_answered(make_tracker):
    """Home Assistant closes a websocket that doesn't answer its pings; the client answers only in recv()."""
    import threading

    class Timeout(Exception):
        pass

    Timeout.__name__ = "WebSocketTimeoutException"

    class FakeWs:
        reads = 0

        def recv(self):
            self.reads += 1
            raise Timeout("timed out")

    ws = FakeWs()
    source = MatterSource(make_tracker(["matter"]), connect=lambda: ws, stop_event=threading.Event())
    assert source._idle(ws, 0.05) is False
    assert ws.reads >= 1

    class Broken:
        def recv(self):
            raise BrokenPipeError(32, "Broken pipe")

    import pytest

    with pytest.raises(BrokenPipeError):
        source._idle(Broken(), 1)
    stop = threading.Event()
    stop.set()
    assert MatterSource(make_tracker(["matter"]), connect=lambda: ws, stop_event=stop)._idle(ws, 1) is True


class _ScriptedWs:
    """Answers each request the way Home Assistant's websocket would, from a table by request type."""

    def __init__(self, answers):
        self.answers = answers
        self.sent = []
        self.queue = []

    def send(self, text):
        import json

        message = json.loads(text)
        self.sent.append(message)
        for reply in self.answers[message["type"]](message):
            self.queue.append(json.dumps({"id": message["id"], **reply}))

    def recv(self):
        return self.queue.pop(0)


def _matter_source(make_tracker):
    import threading

    return MatterSource(make_tracker(["matter"]), connect=lambda: None, stop_event=threading.Event())


MATTER_DEVICES = [{"id": "dev1", "identifiers": [["matter", "deviceid_1-0000000000000004-MatterNodeDevice"]]}]
MATTER_ENTITIES = [
    {"entity_id": "sensor.m_temp", "device_id": "dev1"},
    {"entity_id": "sensor.m_off", "device_id": "dev1", "disabled_by": "user"},
    {"entity_id": "sensor.other", "device_id": "dev2"},
]
ROW = {"entity_id": "sensor.m_temp", "state": "21", "last_reported": "2026-06-01T12:00:00+00:00", "attributes": {}}


def test_states_asks_for_the_matter_entities_only(make_tracker, monkeypatch):
    monkeypatch.setattr("matter.device_key", lambda d: "0xff4d000000000004")
    ok = {"type": "result", "success": True, "result": None}
    ws = _ScriptedWs(
        {
            "render_template": lambda m: [ok, {"type": "event", "event": {"result": [ROW]}}],
            "unsubscribe_events": lambda m: [ok],
        }
    )
    source = _matter_source(make_tracker)
    assert source.states(ws, MATTER_DEVICES, MATTER_ENTITIES) == [ROW]
    assert ws.sent[0]["variables"] == {"ids": ["sensor.m_temp"]}
    assert [m["type"] for m in ws.sent] == ["render_template", "unsubscribe_events"]
    assert source.template_states is True


def test_states_falls_back_to_every_state_for_good(make_tracker, monkeypatch):
    monkeypatch.setattr("matter.device_key", lambda d: "0xff4d000000000004")
    ok = {"type": "result", "success": True, "result": None}
    everything = [{"entity_id": "sensor.m_temp", "state": "21"}, {"entity_id": "light.kitchen", "state": "on"}]
    ws = _ScriptedWs(
        {
            # A Home Assistant whose templates give no report time: the short read can't be trusted.
            "render_template": lambda m: [ok, {"type": "event", "event": {"result": [{**ROW, "last_reported": ""}]}}],
            "unsubscribe_events": lambda m: [ok],
            "get_states": lambda m: [{"type": "result", "success": True, "result": everything}],
        }
    )
    source = _matter_source(make_tracker)
    assert source.states(ws, MATTER_DEVICES, MATTER_ENTITIES) == everything
    assert source.template_states is False
    assert source.states(ws, MATTER_DEVICES, MATTER_ENTITIES) == everything
    assert [m["type"] for m in ws.sent].count("render_template") == 1


def test_states_template_error_falls_back(make_tracker, monkeypatch):
    monkeypatch.setattr("matter.device_key", lambda d: "0xff4d000000000004")
    ws = _ScriptedWs(
        {
            "render_template": lambda m: [{"type": "result", "success": False, "error": {"message": "bad template"}}],
            "get_states": lambda m: [{"type": "result", "success": True, "result": []}],
        }
    )
    source = _matter_source(make_tracker)
    assert source.states(ws, MATTER_DEVICES, MATTER_ENTITIES) == []
    assert source.template_states is False


def test_topology_error_is_tried_again_but_an_unknown_command_is_not(make_tracker, monkeypatch):
    import matter as matter_module

    clock = [1000.0]
    monkeypatch.setattr(matter_module.time, "monotonic", lambda: clock[0])
    ok = lambda result: [{"type": "result", "success": True, "result": result}]  # noqa: E731
    error = ["Matter server is starting"]
    calls = []

    def topology(message):
        calls.append(1)
        return [{"type": "result", "success": False, "error": {"message": error[0]}}]

    ws = _ScriptedWs(
        {
            "config_entries/get": lambda m: ok([{"entry_id": "e1", "state": "loaded"}]),
            "config/device_registry/list": lambda m: ok([{"id": "x", "config_entries": []}]),
            "config/entity_registry/list": lambda m: ok([]),
            "get_states": lambda m: ok([]),
            "matter/network_topology": topology,
        }
    )
    source = _matter_source(make_tracker)
    source.read(ws)
    assert source.topology_supported is None and len(calls) == 1
    source.read(ws)
    assert len(calls) == 1  # not again straight away
    clock[0] += matter_module.REGISTRY_REFRESH_SECONDS + 1
    error[0] = "Unknown command."
    source.read(ws)
    assert source.topology_supported is False and len(calls) == 2
    clock[0] += matter_module.REGISTRY_REFRESH_SECONDS + 1
    source.read(ws)
    assert len(calls) == 2
