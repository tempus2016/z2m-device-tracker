"""Input from the broker and from backup files is untrusted: it must never crash the tracker or poison its data."""

from __future__ import annotations

import json

import pytest
from conftest import COORD, PLUG, ROUTER_A, ROUTER_B, SENSOR, coordinator, events_of, send, z2m_device
from ha import HomeAssistant, markdown_safe

XSS = '<img src=x onerror="alert(1)">'


@pytest.fixture
def net(tracker):
    send(
        tracker,
        "bridge/devices",
        [
            coordinator(),
            z2m_device(SENSOR, "Hall sensor"),
            z2m_device(PLUG, "Landing plug", "Router"),
            z2m_device(ROUTER_A, "Router A", "Router"),
            z2m_device(ROUTER_B, "Router B", "Router"),
        ],
    )
    return tracker


@pytest.mark.parametrize(
    ("topic", "payload"),
    [
        ("bridge/devices", b"[1]"),
        ("bridge/devices", b'[{"ieee_address": "0x1", "friendly_name": []}]'),
        ("bridge/event", b'{"data": [1]}'),
        ("bridge/info", b'{"coordinator": "x"}'),
        ("bridge/info", b'{"permit_join": true, "permit_join_end": 1e300}'),
        ("bridge/health", b'{"os": {"load_average": {"a": 1}}}'),
        ("bridge/health", b'{"devices": [1]}'),
        ("bridge/response/networkmap", b'{"status": "ok", "data": {"value": {"links": [1]}}}'),
        ("Hall sensor", b'{"last_seen": 1e300}'),
        ("Hall sensor", b'{"last_seen": 1e17}'),
        ("Hall sensor", b'{"linkquality": NaN}'),
        ("Hall sensor", b"[" * 200000),
    ],
)
def test_malformed_messages_never_raise(net, topic, payload):
    net.handle_message("zigbee2mqtt", f"zigbee2mqtt/{topic}", payload)
    net.evaluate_health()
    json.loads(net.snapshot_json())


def test_non_text_names_are_dropped(net):
    listed = [coordinator(), z2m_device(SENSOR, "Hall sensor"), z2m_device(PLUG, "Landing plug", "Router")]
    listed += [z2m_device(ROUTER_A, "Router A", "Router"), z2m_device(ROUTER_B, "Router B", "Router")]
    send(net, "bridge/devices", [*listed, {"ieee_address": "0x00158d0000700001", "friendly_name": 5}])
    record = net.data["devices"]["0x00158d0000700001"]
    assert record["friendly_name"] is None
    net.evaluate_health()
    json.loads(net.snapshot_json())


def test_non_finite_readings_are_ignored(net):
    net.handle_message("zigbee2mqtt", "zigbee2mqtt/Hall sensor", b'{"linkquality": -Infinity, "battery": NaN}')
    record = net.data["devices"][SENSOR]
    assert record["lqi"] is None
    assert record["battery"] is None
    net.evaluate_health()
    assert "Infinity" not in net.snapshot_json().decode()


def test_events_need_a_known_type_and_a_real_ieee(net):
    before = len(net.data["devices"])
    send(net, "bridge/event", {"type": "x", "data": {"ieee_address": "0x00158d0000800001"}})
    send(net, "bridge/event", {"type": "device_joined", "data": {"ieee_address": "A" * 100000}})
    assert len(net.data["devices"]) == before


def test_text_is_capped_and_control_characters_removed(net):
    long_name = "Bad\nname " + "x" * 5000
    send(net, "bridge/event", {"type": "device_joined", "data": {"ieee_address": SENSOR, "friendly_name": long_name}})
    event = events_of(net, "joined")[-1]
    assert "\n" not in event["name"]
    assert len(event["name"]) <= 256


def test_a_list_emptying_the_network_waits_for_a_later_copy(net, clock):
    send(net, "bridge/devices", [])
    assert net.data["devices"][SENSOR]["status"] == "active"
    assert len(events_of(net, "device list held")) == 1
    # A quick second copy (a burst, or [1] then []) is still held, without another event.
    net.handle_message("zigbee2mqtt", "zigbee2mqtt/bridge/devices", b"[1]")
    clock.advance(minutes=4)
    send(net, "bridge/devices", [])
    assert net.data["devices"][SENSOR]["status"] == "active"
    assert len(events_of(net, "device list held")) == 1
    clock.advance(minutes=2)
    send(net, "bridge/devices", [])
    assert net.data["devices"][SENSOR]["status"] == "removed"


def test_a_full_list_cancels_a_held_one(net, clock):
    send(net, "bridge/devices", [])
    send(
        net,
        "bridge/devices",
        [
            coordinator(),
            z2m_device(SENSOR, "Hall sensor"),
            z2m_device(PLUG, "Landing plug", "Router"),
            z2m_device(ROUTER_A, "Router A", "Router"),
            z2m_device(ROUTER_B, "Router B", "Router"),
        ],
    )
    clock.advance(minutes=10)
    send(net, "bridge/devices", [])
    assert net.data["devices"][SENSOR]["status"] == "active"


def test_removing_one_device_applies_at_once(net):
    send(
        net,
        "bridge/devices",
        [
            coordinator(),
            z2m_device(PLUG, "Landing plug", "Router"),
            z2m_device(ROUTER_A, "Router A", "Router"),
            z2m_device(ROUTER_B, "Router B", "Router"),
        ],
    )
    assert net.data["devices"][SENSOR]["status"] == "removed"


def test_text_lqi_never_reaches_the_saved_scan(net, data_dir):
    net.load_scans(str(data_dir / "scans.json"))
    link = {"source": {"ieeeAddr": SENSOR}, "target": {"ieeeAddr": COORD}, "relationship": 1, "lqi": XSS}
    send(net, "bridge/response/networkmap", {"status": "ok", "data": {"value": {"nodes": [], "links": [link]}}})
    assert XSS not in json.dumps(net.scans)
    assert XSS not in (data_dir / "scans.json").read_text()


def test_old_scans_with_text_lqi_are_cleaned_on_load(tracker, data_dir):
    path = data_dir / "scans.json"
    path.write_text(
        json.dumps(
            {
                "zigbee2mqtt": [
                    {
                        "at": "2026-01-01T00:00:00+00:00",
                        "parents": {SENSOR: [COORD, XSS]},
                        "links": [[ROUTER_A, COORD, XSS]],
                    }
                ]
            }
        )
    )
    tracker.load_scans(str(path))
    scan = tracker.scans["zigbee2mqtt"][0]
    assert scan["parents"][SENSOR] == [COORD, None]
    assert scan["links"][0][2] is None


def test_bad_restore_keeps_the_current_history(net):
    before = len(net.data["devices"])
    result = net.restore({"format": "z2m-tracker-backup", "history": {"devices": {"x": 1}}})
    assert result["ok"] is False
    assert len(net.data["devices"]) == before


def test_restore_never_takes_discovery_topics_or_unknown_keys(net):
    backup = json.loads(net.backup())
    backup["history"]["discovery_topics"] = ["homeassistant/lock/front_door/config"]
    backup["history"]["anything"] = "else"
    backup["history"]["devices"][SENSOR]["friendly_name"] = 5
    assert net.restore(backup)["ok"] is True
    assert "homeassistant/lock/front_door/config" not in (net.data.get("discovery_topics") or [])
    assert "anything" not in net.data
    assert net.data["devices"][SENSOR]["friendly_name"] is None


def test_corrupt_history_file_starts_empty_and_keeps_the_file(make_tracker, data_dir):
    (data_dir / "history.json").write_text("{not json")
    tracker = make_tracker()
    assert tracker.load() == "corrupt"
    assert list(data_dir.glob("history.json.corrupt-*"))


def test_csv_quotes_formulas(net):
    send(
        net,
        "bridge/devices",
        [
            coordinator(),
            z2m_device(SENSOR, '=HYPERLINK("https://evil","x")'),
            z2m_device(PLUG, "Landing plug", "Router"),
            z2m_device(ROUTER_A, "Router A", "Router"),
            z2m_device(ROUTER_B, "Router B", "Router"),
        ],
    )
    csv_text = net.export_csv().decode("utf-8-sig")
    assert "'=HYPERLINK" in csv_text
    assert ",=HYPERLINK" not in csv_text


def test_markdown_safe_blocks_links_images_and_html():
    text = markdown_safe("Hall [Re-pair](https://evil.example) ![](https://e/p.gif) <img src=x> www.evil.com")
    for raw in ("[Re-pair]", "](https://", " <img", "https://", "www.evil"):
        assert raw not in text
    assert "\\<img" in text


def test_notify_only_calls_notify_services(monkeypatch):
    calls = []
    ha = HomeAssistant("/nonexistent")
    monkeypatch.setattr(ha, "_request", lambda method, path, body=None: calls.append(path))
    ha.notify("script.wipe_everything", "t", "m")
    ha.notify("shell_command.x", "t", "m")
    ha.notify("notify.mobile_app_phone", "t", "m")
    ha.notify("mobile_app_phone", "t", "m")
    assert calls == ["/services/notify/mobile_app_phone", "/services/notify/mobile_app_phone"]
