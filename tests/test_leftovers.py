"""Zigbee2MQTT devices left behind in Home Assistant: detection and guarded removal.

Home Assistant is never contacted: every WebSocket and REST call is mocked.
"""

from __future__ import annotations

import pytest
from conftest import PLUG, SENSOR, coordinator, send, z2m_device
from ha import HomeAssistant, find_leftovers, plan_removal

OLD = "0x00158d0000700001"  # removed from Z2M, replaced by PLUG
STRANGER = "0x00158d0000800001"  # never seen by the tracker
OTHER_NET = "0x00158d0000900001"  # on a Z2M network the tracker doesn't watch
MQTT = "mqtt-entry"


def device(dev_id: str, ident: str, entries=(MQTT,), **extra) -> dict:
    return {"id": dev_id, "identifiers": [["mqtt", ident]], "config_entries": list(entries), "name": None, **extra}


DEVICES = [
    device("dev-plug", f"zigbee2mqtt_{PLUG}", name="Plug"),
    device("dev-old", f"zigbee2mqtt_{OLD}", name="Shed door (old)"),
    device("dev-stranger", f"zigbee2mqtt_{STRANGER}"),
    device("dev-other-net", f"zigbee2mqtt_{OTHER_NET}", name="Neighbour plug"),
    device("dev-bridge", "zigbee2mqtt_bridge_0x00124b0000c00001", name="Zigbee2MQTT Bridge"),
    device("dev-hue", "zigbee2mqtt_0x00158d0000a00001", entries=("hue-entry",), name="Not MQTT owned"),
]
ENTITIES = [
    {"entity_id": "switch.plug", "device_id": "dev-plug", "platform": "mqtt"},
    {"entity_id": "binary_sensor.shed_door", "device_id": "dev-old", "platform": "mqtt"},
    {"entity_id": "sensor.shed_door_battery", "device_id": "dev-old", "platform": "mqtt"},
    # Battery Notes stays live on a gone device; it must not hide the leftover.
    {"entity_id": "sensor.shed_door_battery_type", "device_id": "dev-old", "platform": "battery_notes"},
    {"entity_id": "sensor.stranger_lqi", "device_id": "dev-stranger", "platform": "mqtt"},
    {"entity_id": "switch.neighbour", "device_id": "dev-other-net", "platform": "mqtt"},
]
STATES = {
    "switch.plug": "on",
    "binary_sensor.shed_door": "unavailable",
    "sensor.shed_door_battery": "unavailable",
    "sensor.shed_door_battery_type": "CR2032",
    "sensor.stranger_lqi": "unavailable",
    "switch.neighbour": "off",
}
KNOWN = {
    PLUG: {"active": True, "name": "Plug", "removed_at": None, "successor": None},
    OLD: {
        "active": False,
        "name": "Shed door",
        "removed_at": "2026-10-03T08:00:00+00:00",
        "successor": {"ieee": PLUG, "name": "Plug"},
    },
}


def test_find_leftovers_lists_only_gone_mqtt_devices():
    found = find_leftovers(DEVICES, ENTITIES, STATES, {MQTT}, KNOWN)
    assert [d["device_id"] for d in found] == ["dev-stranger", "dev-old"]  # sorted by name
    old = next(d for d in found if d["device_id"] == "dev-old")
    assert old["ieee"] == OLD
    assert old["name"] == "Shed door (old)"
    assert old["entities"] == 3
    assert old["config_entry_id"] == MQTT
    assert old["known"] is True
    assert old["removed_at"] == "2026-10-03T08:00:00+00:00"
    assert old["successor"] == {"ieee": PLUG, "name": "Plug"}
    stranger = next(d for d in found if d["device_id"] == "dev-stranger")
    assert stranger["known"] is False
    assert stranger["name"] == STRANGER  # no name in HA or the tracker


def test_find_leftovers_new_style_registry_with_one_owner():
    devices = [{"id": "d1", "identifiers": [["mqtt", f"zigbee2mqtt_{STRANGER}"]], "config_entry_id": MQTT}]
    found = find_leftovers(devices, [], {}, {MQTT}, {})
    assert found[0]["config_entry_id"] == MQTT and found[0]["sole_owner"] is True
    assert find_leftovers(devices, [], {}, {"another-mqtt"}, {}) == []


def test_disabled_entities_dont_count_as_live():
    entities = [
        {"entity_id": "switch.neighbour", "device_id": "dev-other-net", "platform": "mqtt", "disabled_by": "user"}
    ]
    found = find_leftovers([DEVICES[3]], entities, {"switch.neighbour": "off"}, {MQTT}, {})
    assert [d["device_id"] for d in found] == ["dev-other-net"]


def test_plan_removal_refuses_ids_that_are_not_leftovers():
    found = find_leftovers(DEVICES, ENTITIES, STATES, {MQTT}, KNOWN)
    go, refused = plan_removal(["dev-old", "dev-plug", "dev-hue", "dev-bridge", "nope", "dev-old"], found)
    assert [d["device_id"] for d in go] == ["dev-old"]
    assert refused == ["dev-plug", "dev-hue", "dev-bridge", "nope"]


class FakeHA:
    """Records WebSocket commands and answers them from registry data."""

    def __init__(self, home: HomeAssistant, states=STATES, unknown_command=False, search=None):
        self.sent: list[dict] = []
        self.unknown_command = unknown_command
        self.search = search or {}
        home._ws_call = self.ws_call
        home._request = lambda method, path, body=None: (
            [{"entity_id": k, "state": v} for k, v in states.items()] if states is not None else None
        )

    def ws_call(self, messages):
        out = []
        for message in messages:
            self.sent.append(message)
            kind = message["type"]
            if kind == "config/device_registry/list":
                out.append(DEVICES)
            elif kind == "config/entity_registry/list":
                out.append(ENTITIES)
            elif kind == "config_entries/get":
                out.append([{"entry_id": MQTT, "domain": "mqtt"}])
            elif kind == "search/related":
                out.append(self.search.get(message["item_id"], {}))
            elif kind == "config/device_registry/remove_config_entry":
                if self.unknown_command:
                    raise RuntimeError("Unknown command.")
                out.append(None)
            elif kind == "config/device_registry/remove":
                out.append(None)
            else:
                raise AssertionError(f"unexpected command {kind}")
        return out

    def removals(self):
        return [m for m in self.sent if m["type"].startswith("config/device_registry/remove")]


@pytest.fixture
def home(tmp_path) -> HomeAssistant:
    h = HomeAssistant(str(tmp_path))
    h.token = "token"
    return h


def test_remove_only_removes_rechecked_leftovers_from_the_mqtt_entry(home):
    fake = FakeHA(home)
    result = home.remove_leftovers(["dev-old", "dev-plug", "dev-other-net"], KNOWN)
    assert fake.removals() == [
        {"type": "config/device_registry/remove_config_entry", "device_id": "dev-old", "config_entry_id": MQTT}
    ]
    assert result == {"ok": False, "removed": ["dev-old"], "refused": ["dev-plug", "dev-other-net"], "failed": []}


def test_remove_refuses_everything_when_states_cant_be_read(home):
    fake = FakeHA(home, states=None)
    with pytest.raises(RuntimeError):
        home.remove_leftovers(["dev-old"], KNOWN)
    assert fake.removals() == []


def test_remove_falls_back_to_plain_remove_only_for_a_sole_mqtt_owner(home):
    fake = FakeHA(home, unknown_command=True)
    result = home.remove_leftovers(["dev-old"], KNOWN)
    # Old-style device (config_entries list, no single owner): no fallback.
    assert result["removed"] == [] and result["failed"][0]["device_id"] == "dev-old"
    assert [m["type"] for m in fake.removals()] == ["config/device_registry/remove_config_entry"]

    DEVICES[1]["config_entry_id"] = MQTT
    try:
        fake.sent.clear()
        result = home.remove_leftovers(["dev-old"], KNOWN)
    finally:
        del DEVICES[1]["config_entry_id"]
    assert result["removed"] == ["dev-old"]
    assert fake.removals()[-1] == {"type": "config/device_registry/remove", "device_id": "dev-old"}


def test_usage_counts_automations_and_scripts(home):
    FakeHA(home, search={"dev-old": {"automation": ["automation.shed_light"], "script": ["script.a", "script.b"]}})
    found = {d["device_id"]: d for d in home.leftovers(KNOWN, usage=True)}
    assert (found["dev-old"]["automations"], found["dev-old"]["scripts"]) == (1, 2)
    assert (found["dev-stranger"]["automations"], found["dev-stranger"]["scripts"]) == (0, 0)


def test_usage_unknown_when_search_is_missing(home):
    fake = FakeHA(home)

    def no_search(messages):
        if messages[0]["type"] == "search/related":
            raise RuntimeError("Unknown command.")
        return FakeHA.ws_call(fake, messages)

    home._ws_call = no_search
    found = home.leftovers(KNOWN, usage=True)
    assert found and all(d["automations"] is None for d in found)


def test_tracker_context_waits_for_every_network(make_tracker):
    t = make_tracker(["zigbee2mqtt", "zigbee2mqtt_garden"])
    send(t, "bridge/devices", [coordinator(), z2m_device(SENSOR, "Shed door"), z2m_device(PLUG, "Plug", "Router")])
    ready, _ = t.leftover_context()
    assert not ready
    send(t, "bridge/devices", [], base="zigbee2mqtt_garden")
    send(t, "bridge/devices", [coordinator(), z2m_device(PLUG, "Plug", "Router")])
    t.link_replacement(SENSOR, PLUG, [])
    ready, known = t.leftover_context()
    assert ready
    assert known[PLUG]["active"] is True
    assert known[SENSOR]["active"] is False
    assert known[SENSOR]["removed_at"]
    assert known[SENSOR]["successor"] == {"ieee": PLUG, "name": "Plug"}


def test_set_ha_leftovers_bumps_revision_only_on_change(tracker):
    before = tracker.version
    tracker.set_ha_leftovers([])
    assert tracker.version == before
    tracker.set_ha_leftovers([{"device_id": "x"}])
    assert tracker.version != before
    assert b'"ha_leftovers": [{"device_id": "x"}]' in tracker.snapshot_json()
