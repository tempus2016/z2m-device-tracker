"""The device page's Messages tab: the last messages to and from each device, kept in memory."""

from __future__ import annotations

from conftest import PLUG, SENSOR, coordinator, send, z2m_device
from live_messages import MAX_MESSAGES, MAX_TEXT, MessageLog


def setup(tracker) -> None:
    send(
        tracker,
        "bridge/devices",
        [coordinator(), z2m_device(PLUG, "Lamp", "Router"), z2m_device(SENSOR, "Door")],
    )
    send(tracker, "Lamp/availability", {"state": "online"})


def test_state_availability_and_commands_are_kept_newest_first(tracker, clock):
    setup(tracker)
    clock.advance(1)
    send(tracker, "Door", {"contact": True, "battery": 90})
    clock.advance(1)
    send(tracker, "Lamp/set", {"state": "ON"})
    msgs = tracker.recent_messages(PLUG)
    assert [m["kind"] for m in msgs] == ["command", "availability"]
    assert msgs[0]["topic"] == "set"
    assert msgs[0]["payload"] == '{"state": "ON"}'
    assert msgs[1]["payload"] == '{"state": "online"}'
    door = tracker.recent_messages(SENSOR)
    assert [m["kind"] for m in door] == ["state"]
    assert door[0]["at"] == clock.time() - 1


def test_command_gets_its_reply_time(tracker, clock):
    setup(tracker)
    send(tracker, "Lamp/set", {"state": "ON"})
    clock.advance(0.4)
    send(tracker, "Lamp", {"state": "ON", "linkquality": 120})
    command = tracker.recent_messages(PLUG)[1]
    assert command["kind"] == "command"
    assert command["reply_ms"] == 400


def test_unanswered_command_is_marked_missed(tracker, clock):
    setup(tracker)
    send(tracker, "Lamp/set", {"state": "OFF"})
    clock.advance(11)
    tracker.expire_commands()
    command = tracker.recent_messages(PLUG)[0]
    assert command["missed"] is True
    assert "reply_ms" not in command


def test_next_command_does_not_mark_itself_missed(tracker, clock):
    setup(tracker)
    send(tracker, "Lamp/set", {"state": "ON"})
    clock.advance(5)
    send(tracker, "Lamp/set", {"state": "OFF"})
    newest, older = tracker.recent_messages(PLUG)[:2]
    assert older["missed"] is True
    assert "missed" not in newest
    clock.advance(0.2)
    send(tracker, "Lamp", {"state": "OFF"})
    assert tracker.recent_messages(PLUG)[1]["reply_ms"] == 200


def test_endpoint_commands_and_attribute_topics(tracker, clock):
    setup(tracker)
    send(tracker, "Lamp/l1/set", {"state": "ON"})
    send(tracker, "Lamp/set/brightness", "120")
    topics = [m["topic"] for m in tracker.recent_messages(PLUG) if m["kind"] == "command"]
    assert topics == ["set/brightness", "l1/set"]


def test_retained_commands_are_not_logged(tracker, clock):
    setup(tracker)
    tracker.handle_message("zigbee2mqtt", "zigbee2mqtt/Lamp/set", b'{"state":"ON"}', retained=True)
    assert [m["kind"] for m in tracker.recent_messages(PLUG)] == ["availability"]


def test_unknown_device_and_forget(tracker, clock):
    setup(tracker)
    assert tracker.recent_messages("0x0000000000000bad") is None
    tracker.forget(SENSOR)
    assert tracker.recent_messages(SENSOR) is None
    assert SENSOR not in tracker.messages._log


def test_log_is_bounded_per_device_and_per_message():
    log = MessageLog(clock=lambda: 1000.0)
    for n in range(MAX_MESSAGES + 25):
        log.add("0x1", "state", f'{{"n":{n}}}')
    kept = log.recent("0x1")
    assert len(kept) == MAX_MESSAGES
    assert kept[0]["payload"] == f'{{"n":{MAX_MESSAGES + 24}}}'
    log.add("0x1", "state", "x" * (MAX_TEXT * 3))
    assert len(log.recent("0x1")[0]["payload"]) == MAX_TEXT


def test_messages_are_not_saved(tracker, clock, data_dir):
    setup(tracker)
    send(tracker, "Door", {"contact": False})
    tracker.save(force=True)
    assert "contact" not in (data_dir / "history.json").read_text()
