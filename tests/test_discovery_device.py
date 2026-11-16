"""MQTT discovery names the device, so HA doesn't name it after the broker when Z2M's own discovery is off."""

from __future__ import annotations

import types

from discovery import Discovery

IEEE = "0x00158d0000700001"


def entities(record: dict) -> dict[str, dict]:
    tracker = types.SimpleNamespace(is_battery_device=lambda record: False)
    return Discovery(tracker, "1.2.0", True)._entities(record, {}, [])


def test_every_entity_names_the_device():
    configs = entities({"ieee": IEEE, "friendly_name": "Hall plug", "type": "Router"})
    assert configs
    for config in configs.values():
        assert config["device"] == {"identifiers": [f"zigbee2mqtt_{IEEE}"], "name": "Hall plug"}


def test_address_is_the_name_when_unknown():
    # Seen only in a join event: no friendly name yet. Zigbee2MQTT calls it by its address; a nameless
    # device would make HA name it after the MQTT broker.
    configs = entities({"ieee": IEEE, "friendly_name": None, "type": "Router"})
    for config in configs.values():
        assert config["device"] == {"identifiers": [f"zigbee2mqtt_{IEEE}"], "name": IEEE}
