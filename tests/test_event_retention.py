"""A full event list makes room from the noisiest devices first, not from the oldest events of all."""

from __future__ import annotations

from tracker_base import EVENTS_PER_KIND, MAX_EVENTS, trim_events


def event(n: int, ieee: str, kind: str) -> dict:
    return {"time": f"t{n:06d}", "instance": "zigbee2mqtt", "ieee": ieee, "kind": kind}


def test_a_flapping_device_gives_way_before_the_others():
    events = [event(n, f"quiet{n}", "joined") for n in range(400)]
    for n in range(400, MAX_EVENTS + 1):
        events.append(event(n, "flapper", "offline" if n % 2 else "online"))
    trim_events(events)
    assert len(events) <= MAX_EVENTS - MAX_EVENTS // 10 + 2
    assert sum(1 for e in events if e["ieee"].startswith("quiet")) == 400
    # The flapper keeps its newest events, in order.
    flaps = [e for e in events if e["ieee"] == "flapper"]
    assert flaps[-1]["time"] == f"t{MAX_EVENTS:06d}"
    assert [e["time"] for e in events] == sorted(e["time"] for e in events)


def test_a_noisy_device_keeps_its_last_fifty_of_each_kind():
    events = [event(n, f"quiet{n}", "joined") for n in range(MAX_EVENTS - 40)]
    events += [event(MAX_EVENTS + n, "flapper", "offline") for n in range(61)]
    trim_events(events)
    assert sum(1 for e in events if e["ieee"] == "flapper") == EVENTS_PER_KIND
    # That wasn't enough room, so the rest comes from the oldest events, as before.
    assert len(events) == MAX_EVENTS
    assert events[0]["time"] == "t000010"


def test_no_noisy_device_drops_the_oldest():
    events = [event(n, f"dev{n % 500}", "joined") for n in range(MAX_EVENTS + 5)]
    trim_events(events)
    assert len(events) == MAX_EVENTS and events[0]["time"] == "t000005"


def test_a_list_with_room_is_left_alone():
    events = [event(n, "flapper", "offline") for n in range(MAX_EVENTS)]
    trim_events(events)
    assert len(events) == MAX_EVENTS


def test_tracker_events_go_through_it(tracker):
    for n in range(MAX_EVENTS + 1):
        tracker._add_event(f"t{n:06d}", "zigbee2mqtt", "quiet" if n < 5 else "flapper", "x", "offline", "")
    assert sum(1 for e in tracker.data["events"] if e["ieee"] == "quiet") == 5
    assert len(tracker.data["events"]) < MAX_EVENTS
