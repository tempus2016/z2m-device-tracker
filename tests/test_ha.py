"""ha.HomeAssistant: registry refresh (live and from files), live device lookup, registry watch."""

from __future__ import annotations

import json
import threading
import types

import ha
import pytest
from ha import HomeAssistant

PLUG = "0x00158d0000600001"
SENSOR = "0x00158d0000500001"

DEVICES = [
    {
        "id": "dev-plug",
        "identifiers": [["mqtt", f"zigbee2mqtt_{PLUG}"]],
        "area_id": "living_room",
        "name": PLUG,
        "name_by_user": "Fish Tank Filter Plug",
    },
    {"id": "dev-sensor", "identifiers": [["mqtt", f"zigbee2mqtt_{SENSOR}"]], "area_id": None, "name": "Hall Sensor"},
    {"id": "dev-other", "identifiers": [["hue", "abc"]], "area_id": None, "name": "Not Zigbee2MQTT"},
]
AREAS = [{"area_id": "living_room", "name": "Living Room"}]


@pytest.fixture
def home(tmp_path) -> HomeAssistant:
    h = HomeAssistant(str(tmp_path))
    h.token = "token"
    return h


def test_refresh_maps_links_areas_and_names_live(home):
    home._ws_call = lambda messages: [DEVICES, AREAS, []]
    home.refresh()
    assert home.device_ids == {PLUG: "dev-plug", SENSOR: "dev-sensor"}
    assert home.areas == {PLUG: "Living Room", SENSOR: None}
    # name_by_user wins; a name that is just the IEEE address is not a name.
    assert home.names == {PLUG: "Fish Tank Filter Plug", SENSOR: "Hall Sensor"}


def test_refresh_falls_back_to_the_registry_files(home, tmp_path):
    def broken(messages):
        raise RuntimeError("no websocket")

    home._ws_call = broken
    storage = tmp_path / ".storage"
    storage.mkdir()
    (storage / "core.device_registry").write_text(json.dumps({"data": {"devices": DEVICES}}))
    (storage / "core.area_registry").write_text(json.dumps({"data": {"areas": AREAS}}))
    home.refresh()
    assert home.device_ids[PLUG] == "dev-plug"
    assert home.areas[PLUG] == "Living Room"


def test_find_device_asks_home_assistant_live(home):
    home._ws_call = lambda messages: [DEVICES]
    assert home.find_device(SENSOR) == "dev-sensor"
    assert home.find_device("0x00158d00009fffff") is None


class FakeSocket:
    """Replays messages, then stops the watch loop."""

    def __init__(self, messages, stop):
        self.messages = [json.dumps(m) for m in messages]
        self.sent = []
        self.stop = stop

    def send(self, message):
        self.sent.append(json.loads(message))

    def settimeout(self, timeout):
        pass

    def recv(self):
        if self.messages:
            return self.messages.pop(0)
        self.stop.set()
        raise TimeoutError

    def close(self):
        pass


def test_watch_subscribes_and_reports_each_change(home, monkeypatch):
    stop = threading.Event()
    socket = FakeSocket(
        [{"id": 1, "type": "result", "success": True}, {"id": 1, "type": "event", "event": {"data": {}}}], stop
    )
    monkeypatch.setitem(
        __import__("sys").modules, "websocket", types.SimpleNamespace(WebSocketTimeoutException=TimeoutError)
    )
    home._ws_connect = lambda timeout=15: socket
    calls = []
    home.watch(lambda: calls.append(1), stop)
    assert [m["event_type"] for m in socket.sent] == list(ha.REGISTRY_EVENTS)
    # Once on connect (to catch up), once for the event.
    assert len(calls) == 2


def test_refresh_maps_zigbee2mqtt_entities(home):
    entities = [
        {
            "entity_id": "sensor.plug_power",
            "device_id": "dev-plug",
            "platform": "mqtt",
            "unique_id": f"{PLUG}_power_zigbee2mqtt",
        },
        {"entity_id": "sensor.plug_other", "device_id": "dev-plug", "platform": "template", "unique_id": f"{PLUG}_x"},
        {"entity_id": "sensor.hue", "device_id": "dev-other", "platform": "mqtt", "unique_id": "abc_power"},
    ]
    home._ws_call = lambda messages: [DEVICES, AREAS, entities]
    home.refresh()
    assert home.entities == {PLUG: {"power_zigbee2mqtt": "sensor.plug_power"}}


def test_entity_usage_searches_each_entity(home):
    asked = []

    def ws(messages):
        asked.extend(messages)
        return [{"automation": ["automation.cost"]}, {}]

    home._ws_call = ws
    home._request = lambda method, path, body=None: [
        {"entity_id": "automation.cost", "attributes": {"friendly_name": "Cost", "id": "17"}}
    ]
    found = home.entity_usage(["sensor.a", "sensor.b"])
    assert [m["item_id"] for m in asked] == ["sensor.a", "sensor.b"]
    assert all(m["item_type"] == "entity" for m in asked)
    assert found["sensor.a"]["automations"] == [{"entity_id": "automation.cost", "name": "Cost", "id": "17"}]
    assert found["sensor.b"]["automations"] == []


# ------------------------------------------------------------------ ZHA identifiers


def test_identifier_ieee_z2m():
    assert ha.identifier_ieee(["mqtt", f"zigbee2mqtt_{PLUG}"]) == PLUG


def test_identifier_ieee_zha():
    assert ha.identifier_ieee(["zha", "AA:BB:CC:00:00:00:00:03"]) == "0xaabbcc0000000003"


def test_identifier_ieee_bridge_none():
    assert ha.identifier_ieee(["mqtt", f"zigbee2mqtt_bridge_{PLUG}"]) is None
    assert ha.identifier_ieee(["hue", "abc"]) is None
    assert ha.identifier_ieee(["zha"]) is None


def test_leftovers_ignore_zha_devices_in_tracker():
    zha_device = {"id": "dev-zha", "identifiers": [["zha", "aa:bb:cc:00:00:00:00:03"]], "config_entry_id": "zha-entry"}
    assert ha.find_leftovers([zha_device], [], {}, {"mqtt-entry"}, {}) == []


ZHA_DEVICE = {
    "id": "dev-zha",
    "identifiers": [["zha", "aa:bb:cc:00:00:00:00:03"]],
    "area_id": "living_room",
    "name": "FakeCo BTN-1",
    "name_by_user": "Test Button",
}


def test_refresh_maps_zha_devices(home):
    home._ws_call = lambda messages: [[ZHA_DEVICE], AREAS, []]
    home.refresh()
    assert home.device_ids == {"0xaabbcc0000000003": "dev-zha"}
    assert home.areas == {"0xaabbcc0000000003": "Living Room"}
    assert home.names == {"0xaabbcc0000000003": "Test Button"}


def test_find_device_zha(home):
    home._ws_call = lambda messages: [[ZHA_DEVICE]]
    assert home.find_device("0xaabbcc0000000003") == "dev-zha"


SAME_IEEE_MQTT = {
    "id": "dev-old-mqtt",
    "identifiers": [["mqtt", "zigbee2mqtt_0xaabbcc0000000003"]],
    "area_id": None,
    "name": "Old MQTT button",
}


def test_refresh_prefers_mqtt_device_in_z2m_mode(home):
    home._ws_call = lambda messages: [[SAME_IEEE_MQTT, ZHA_DEVICE], AREAS, []]
    home.refresh()
    assert home.device_ids["0xaabbcc0000000003"] == "dev-old-mqtt"
    home._ws_call = lambda messages: [[ZHA_DEVICE, SAME_IEEE_MQTT], AREAS, []]
    home.refresh()
    assert home.device_ids["0xaabbcc0000000003"] == "dev-old-mqtt"


def test_refresh_prefers_zha_device_in_zha_mode(home):
    home.stack = "zha"
    home._ws_call = lambda messages: [[ZHA_DEVICE, SAME_IEEE_MQTT], AREAS, []]
    home.refresh()
    assert home.device_ids["0xaabbcc0000000003"] == "dev-zha"
    assert home.areas["0xaabbcc0000000003"] == "Living Room"
    home._ws_call = lambda messages: [[SAME_IEEE_MQTT, ZHA_DEVICE]]
    assert home.find_device("0xaabbcc0000000003") == "dev-zha"


def test_find_device_prefers_mqtt_in_z2m_mode(home):
    home._ws_call = lambda messages: [[ZHA_DEVICE, SAME_IEEE_MQTT]]
    assert home.find_device("0xaabbcc0000000003") == "dev-old-mqtt"


def test_z2m_entity_ids_keys_by_object_id():
    entities = [
        {
            "entity_id": "light.lamp",
            "device_id": "dev-plug",
            "platform": "mqtt",
            "unique_id": f"{PLUG}_light_zigbee2mqtt",
        },
        {
            "entity_id": "sensor.lamp_power",
            "device_id": "dev-plug",
            "platform": "mqtt",
            "unique_id": f"{PLUG}_power_zigbee2mqtt",
        },
        {
            "entity_id": "sensor.lamp_battery_type",
            "device_id": "dev-plug",
            "platform": "battery_notes",
            "unique_id": "x",
        },
        {
            "entity_id": "sensor.hall",
            "device_id": "dev-sensor",
            "platform": "mqtt",
            "unique_id": f"{SENSOR}_temperature",
        },
    ]
    assert ha.z2m_entity_ids(DEVICES, entities, PLUG, "zigbee2mqtt") == {
        "light": "light.lamp",
        "power": "sensor.lamp_power",
    }
    assert ha.z2m_entity_ids(DEVICES, entities, "0x0000000000000000", "zigbee2mqtt") == {}


def test_plan_entity_ids_gives_the_new_device_the_old_ids():
    old = {"light": "light.living_room_lamp_plug", "power": "sensor.living_room_lamp_plug_power", "lqi": "sensor.a"}
    new = {
        "light": "light.living_room_living_room_lamp_plug",
        "power": "sensor.living_room_living_room_lamp_plug_power",
        "lqi": "sensor.a",
        "energy": "sensor.living_room_living_room_lamp_plug_energy",
    }
    assert ha.plan_entity_ids(old, new) == [
        ("light.living_room_living_room_lamp_plug", "light.living_room_lamp_plug"),
        ("sensor.living_room_living_room_lamp_plug_power", "sensor.living_room_lamp_plug_power"),
    ]


def test_plan_entity_ids_never_changes_the_domain():
    assert ha.plan_entity_ids({"state": "switch.plug"}, {"state": "light.plug_new"}) == []
