"""Coordinator setup details from bridge/info: connection, adapter, versions, MQTT, frontend. No secrets."""

from __future__ import annotations

import json

from conftest import send
from health import strip_credentials, z2m_connection, z2m_setup

USB_BY_ID = "/dev/serial/by-id/usb-ITead_Sonoff_Zigbee_3.0_USB_Dongle_Plus_e2d4a3c2b8c9ec11b65c2a1e2d4a3c2b-if00-port0"


def info_v2(port="tcp://192.168.0.50:6638", **serial) -> dict:
    """A Zigbee2MQTT 2.x bridge/info payload, trimmed to what matters here."""
    return {
        "version": "2.14.3",
        "commit": "abc1234",
        "zigbee_herdsman": {"version": "7.1.2"},
        "zigbee_herdsman_converters": {"version": "25.80.0"},
        "coordinator": {
            "ieee_address": "0x00124b0000c00001",
            "type": "zStack3x0",
            "meta": {"revision": 20240710, "transportrev": 2, "product": 1, "majorrel": 2, "minorrel": 7},
        },
        "network": {"channel": 15, "pan_id": 6754, "extended_pan_id": "0xdddddddddddddddd"},
        "log_level": "info",
        "permit_join": False,
        "restart_required": False,
        "mqtt": {"version": 4, "server": "mqtt://core-mosquitto:1883"},
        "config": {
            "serial": {"port": port, "adapter": "zstack", **serial},
            "mqtt": {"server": "mqtt://addons:s3cr3t@core-mosquitto:1883", "user": "addons"},
            "frontend": {"enabled": True, "port": 8099, "host": "0.0.0.0"},
            "advanced": {"log_level": "info", "pan_id": 6754},
        },
    }


def test_connection_tcp_mdns_usb_serial():
    assert z2m_connection("tcp://192.168.0.50:6638") == {"mode": "tcp", "target": "192.168.0.50:6638", "name": None}
    assert z2m_connection("mdns://slzb-06") == {"mode": "mdns", "target": "slzb-06", "name": None}
    assert z2m_connection("/dev/ttyUSB0") == {"mode": "usb", "target": "/dev/ttyUSB0", "name": None}
    assert z2m_connection("/dev/ttyACM0")["mode"] == "usb"
    by_id = z2m_connection(USB_BY_ID)
    assert by_id["mode"] == "usb"
    assert by_id["name"] == "ITead Sonoff Zigbee 3.0 USB Dongle Plus"
    conbee = z2m_connection("/dev/serial/by-id/usb-dresden_elektronik_ingenieurtechnik_GmbH_ConBee_II_DE2123456-if00")
    assert conbee["name"] == "dresden elektronik ingenieurtechnik GmbH ConBee II"
    assert z2m_connection("/dev/ttyAMA0")["mode"] == "serial"
    assert z2m_connection(None) is None
    assert z2m_connection("") is None


def test_strip_credentials():
    assert strip_credentials("mqtt://user:pass@core-mosquitto:1883") == "mqtt://core-mosquitto:1883"
    # A raw password holding "/" or "@" must not leak either.
    assert strip_credentials("mqtts://u:p/a@ss@broker.lan:8883") == "mqtts://broker.lan:8883"
    assert strip_credentials("mqtt://core-mosquitto:1883") == "mqtt://core-mosquitto:1883"
    assert strip_credentials("user:pw@host:1883") == "host:1883"
    assert strip_credentials("") is None
    assert strip_credentials(None) is None


def test_setup_from_v2_info_tcp():
    setup = z2m_setup(info_v2(baudrate=115200, rtscts=False))
    assert setup == {
        "connection": {"mode": "tcp", "target": "192.168.0.50:6638", "name": None},
        "adapter": "zstack",
        "baudrate": 115200,
        "rtscts": False,
        "extended_pan_id": "0xdddddddddddddddd",
        "herdsman": "7.1.2",
        "converters": "25.80.0",
        "log_level": "info",
        "mqtt_server": "mqtt://core-mosquitto:1883",
        "frontend": {"url": None, "port": 8099},
    }
    assert "s3cr3t" not in json.dumps(setup)


def test_setup_frontend_url_and_disabled():
    payload = info_v2()
    payload["config"]["frontend"] = {"enabled": True, "port": 8080, "url": "https://me:pw@z2m.example.com"}
    assert z2m_setup(payload)["frontend"] == {"url": "https://z2m.example.com", "port": 8080}
    payload["config"]["frontend"] = {"enabled": False, "port": 8080}
    assert z2m_setup(payload)["frontend"] is None


def test_setup_adapter_from_coordinator_type_when_not_set():
    payload = info_v2(port="/dev/ttyUSB0")
    del payload["config"]["serial"]["adapter"]
    payload["coordinator"]["type"] = "EmberZNet"
    assert z2m_setup(payload)["adapter"] == "ember"
    payload["coordinator"]["type"] = "ConBee2/RaspBee2"
    assert z2m_setup(payload)["adapter"] == "deconz"
    payload["coordinator"]["type"] = "Something new"
    assert z2m_setup(payload)["adapter"] is None


def test_setup_from_v1_info_hides_missing_fields():
    payload = {
        "version": "1.35.0",
        "coordinator": {"ieee_address": "0x00124b0000c00001", "type": "zStack3x0", "meta": {"revision": 20221226}},
        "network": {"channel": 11, "pan_id": 6754},
        "config": {"serial": {"port": "/dev/ttyACM0"}, "frontend": True, "advanced": {"log_level": "warn"}},
    }
    setup = z2m_setup(payload)
    assert setup["connection"]["mode"] == "usb"
    assert setup["adapter"] == "zstack"
    assert setup["frontend"] == {"url": None, "port": 8080}
    assert setup["log_level"] == "warn"
    for key in ("herdsman", "converters", "extended_pan_id", "mqtt_server", "baudrate", "rtscts"):
        assert setup[key] is None
    # No config at all: everything missing, nothing raises.
    bare = z2m_setup({"version": "1.20.0"})
    assert all(value is None for value in bare.values())


def test_stored_bridge_info_is_small_and_has_no_secrets(tracker):
    payload = info_v2(port=USB_BY_ID, baudrate=115200, rtscts=True)
    payload["config"]["advanced"]["network_key"] = [1, 2, 3, 4]
    payload["config"]["mqtt"]["password"] = "s3cr3t"
    payload["config"]["devices"] = {f"0x{i:016x}": {"friendly_name": f"Device {i}"} for i in range(50)}
    send(tracker, "bridge/info", payload)
    stored = tracker.data["bridges"]["zigbee2mqtt"]["info"]
    assert stored["connection"]["name"] == "ITead Sonoff Zigbee 3.0 USB Dongle Plus"
    assert stored["firmware"] == "20240710"
    assert stored["coordinator_ieee"] == "0x00124b0000c00001"
    assert stored["rtscts"] is True
    text = json.dumps(stored)
    assert "s3cr3t" not in text
    assert "network_key" not in text
    assert "Device 1" not in text
    assert "config" not in stored
