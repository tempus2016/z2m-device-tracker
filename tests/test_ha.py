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
