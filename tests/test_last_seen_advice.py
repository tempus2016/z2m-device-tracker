"""Settings advice: last_seen off in Zigbee2MQTT, the Turn on request and Dismiss."""

from __future__ import annotations

from conftest import coordinator, send


def info(tracker, last_seen) -> None:
    advanced = {"channel": 15} if last_seen is None else {"channel": 15, "last_seen": last_seen}
    send(tracker, "bridge/info", {"version": "2.6.0", "config": {"advanced": advanced}})


def advice(tracker) -> list[dict]:
    issues, _ = tracker.evaluate_health()
    return [i for i in issues if i["kind"] == "last_seen_off"]


def test_advice_when_last_seen_is_disabled(tracker, clock):
    send(tracker, "bridge/devices", [coordinator()])
    assert advice(tracker) == []
    info(tracker, "disable")
    [issue] = advice(tracker)
    assert issue["key"] == "last_seen_off:zigbee2mqtt"
    assert issue["severity"] == "info"
    assert issue["ieee"] == "network:zigbee2mqtt"
    info(tracker, "ISO_8601")
    assert advice(tracker) == []


def test_no_advice_when_the_setting_is_unknown(tracker, clock):
    info(tracker, None)
    assert advice(tracker) == []


def test_dismiss_is_for_good(tracker, make_tracker, clock):
    info(tracker, "disable")
    advice(tracker)
    assert not tracker.dismiss_advice("offline:0x1")
    assert tracker.dismiss_advice("last_seen_off:zigbee2mqtt")
    assert advice(tracker) == []
    tracker.save(force=True)
    again = make_tracker()
    again.load()
    assert advice(again) == []


def test_turn_on_sends_the_options_request(tracker, clock):
    sent = []

    def publish(topic, payload):
        sent.append((topic, payload))
        tracker.handle_message(
            "zigbee2mqtt",
            "zigbee2mqtt/bridge/response/options",
            ('{"status":"ok","data":{"restart_required":false},"transaction":"%s"}' % payload["transaction"]).encode(),
        )

    tracker.publish = publish
    assert tracker.turn_on_last_seen("zigbee2mqtt") == {"ok": True, "restart_required": False}
    topic, payload = sent[0]
    assert topic == "zigbee2mqtt/bridge/request/options"
    assert payload["options"] == {"advanced": {"last_seen": "ISO_8601"}}


def test_turn_on_reports_errors(tracker, clock):
    assert tracker.turn_on_last_seen("elsewhere") == {"ok": False, "error": "Unknown network"}
    assert tracker.turn_on_last_seen("zigbee2mqtt")["error"] == "MQTT is not connected"

    def publish(topic, payload):
        tracker.handle_message(
            "zigbee2mqtt",
            "zigbee2mqtt/bridge/response/options",
            ('{"status":"error","error":"bad option","transaction":"%s"}' % payload["transaction"]).encode(),
        )

    tracker.publish = publish
    assert tracker.turn_on_last_seen("zigbee2mqtt") == {"ok": False, "error": "bad option"}
