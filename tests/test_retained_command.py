"""Retained /set commands: found when the broker replays them, flagged per device, cleared on demand."""

from __future__ import annotations

from conftest import PLUG, SENSOR, coordinator, send, z2m_device
from health import device_issues


def retained(tracker, topic: str, payload: str, base: str = "zigbee2mqtt") -> None:
    tracker.handle_message(base, f"{base}/{topic}", payload.encode(), True)


def network(tracker) -> None:
    send(
        tracker,
        "bridge/devices",
        [coordinator(), z2m_device(PLUG, "Bulb Lounge", "Router"), z2m_device(SENSOR, "Hall")],
    )


def issues_of(tracker, kind="retained_command"):
    issues, _ = tracker.evaluate_health()
    return [i for i in issues if i["kind"] == kind]


def test_retained_set_flags_the_device(tracker):
    network(tracker)
    retained(tracker, "Bulb Lounge/set", '{"state":"ON","brightness":254}')
    [issue] = issues_of(tracker)
    assert issue["ieee"] == PLUG
    assert issue["severity"] == "warning"
    assert issue["params"]["topic"] == "zigbee2mqtt/Bulb Lounge/set"
    assert issue["params"]["payload"] == '{"state":"ON","brightness":254}'
    assert issue["params"]["topics"] == ["zigbee2mqtt/Bulb Lounge/set"]
    # A replayed command isn't timed as a command.
    assert tracker._commands == {}


def test_attribute_endpoint_and_ieee_forms(tracker):
    network(tracker)
    retained(tracker, "Bulb Lounge/set/state", "OFF")
    retained(tracker, "Bulb Lounge/l1/set", '{"state":"ON"}')
    retained(tracker, f"{SENSOR}/set", '{"sensitivity":"high"}')
    by_device = {i["ieee"]: i for i in issues_of(tracker)}
    assert by_device[PLUG]["params"]["n"] == 2
    assert sorted(by_device[PLUG]["params"]["topics"]) == [
        "zigbee2mqtt/Bulb Lounge/l1/set",
        "zigbee2mqtt/Bulb Lounge/set/state",
    ]
    assert by_device[SENSOR]["params"]["topics"] == [f"zigbee2mqtt/{SENSOR}/set"]


def test_replay_before_device_list_is_matched_later(tracker):
    retained(tracker, "Bulb Lounge/set", '{"state":"ON"}')
    assert issues_of(tracker) == []
    network(tracker)
    assert [i["ieee"] for i in issues_of(tracker)] == [PLUG]


def test_unknown_and_live_commands_are_not_flagged(tracker):
    network(tracker)
    retained(tracker, "Somebody Else/set", '{"state":"ON"}')
    send(tracker, "Bulb Lounge/set", {"state": "ON"})  # live command, not retained
    assert issues_of(tracker) == []
    assert tracker.retained_commands_of(PLUG) == []


def test_empty_set_clears_and_forget(tracker):
    network(tracker)
    retained(tracker, "Bulb Lounge/set", '{"state":"ON"}')
    retained(tracker, "Bulb Lounge/set/state", "ON")
    assert len(tracker.retained_commands_of(PLUG)) == 2
    # Someone else cleared one (the broker delivers the empty publish live).
    tracker.handle_message("zigbee2mqtt", "zigbee2mqtt/Bulb Lounge/set/state", b"", False)
    assert tracker.retained_commands_of(PLUG) == ["zigbee2mqtt/Bulb Lounge/set"]
    assert tracker._commands == {}  # an empty /set isn't a command
    tracker.forget_retained_commands(["zigbee2mqtt/Bulb Lounge/set"])
    assert issues_of(tracker) == []


def test_renamed_device_drops_the_issue(tracker):
    network(tracker)
    retained(tracker, "Bulb Lounge/set", '{"state":"ON"}')
    send(
        tracker,
        "bridge/devices",
        [coordinator(), z2m_device(PLUG, "Lounge Bulb", "Router"), z2m_device(SENSOR, "Hall")],
    )
    # The topic now sits under an old name: not a current device's command any more.
    assert issues_of(tracker) == []


def test_mute_and_disable(make_tracker):
    tracker = make_tracker(options={"disabled_checks": ["retained_command"]})
    network(tracker)
    retained(tracker, "Bulb Lounge/set", '{"state":"ON"}')
    assert issues_of(tracker) == []
    tracker = make_tracker()
    network(tracker)
    retained(tracker, "Bulb Lounge/set", '{"state":"ON"}')
    assert tracker.set_muted(PLUG, "retained_command", True)
    assert issues_of(tracker) == []


def test_long_payload_is_trimmed_and_list_is_bounded(tracker, monkeypatch):
    import tracker as tracker_module

    network(tracker)
    retained(tracker, "Bulb Lounge/set", '{"x":"' + "a" * 500 + '"}')
    entry = tracker.retained_commands["zigbee2mqtt/Bulb Lounge/set"]
    assert len(entry["payload"]) == 120 and entry["payload"].endswith("…")
    monkeypatch.setattr(tracker_module, "MAX_RETAINED_COMMANDS", 1)
    retained(tracker, "Hall/set", '{"a":1}')
    assert list(tracker.retained_commands) == ["zigbee2mqtt/Bulb Lounge/set"]


def test_health_text():
    record = {"ieee": PLUG, "type": "Router", "status": "active"}
    summary = {
        "retained_commands": [
            {"topic": "zigbee2mqtt/A/set", "payload": "ON"},
            {"topic": "zigbee2mqtt/A/set/x", "payload": "1"},
        ]
    }
    [issue] = device_issues(record, summary, {}, 0.0, [])
    assert issue["detail"].startswith("zigbee2mqtt/A/set holds ON (and 1 more).")
