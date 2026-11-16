"""Group history: membership, rename and created/removed events from bridge/groups, kept across restarts."""

from __future__ import annotations

from datetime import datetime, timezone

from conftest import PLUG, ROUTER_A, ROUTER_B, send
from group_history import DELETED_DAYS, MAX_EVENTS, apply, clean, clean_state, prune
from test_tracker_groups import _devices, _groups


def kinds(events):
    return [(e["kind"], e.get("ieee") or e.get("to") or e.get("members")) for e in events]


def test_first_list_is_the_baseline():
    hist: dict = {}
    state, changed = apply(None, hist, {"1": "Lounge"}, {"1": ["a", "b"]}, "t0", str)
    assert changed and hist == {}
    assert state == {"1": {"name": "Lounge", "members": ["a", "b"], "since": "t0"}}


def test_members_added_removed_renamed_created_and_removed():
    hist: dict = {}
    state, _ = apply(None, hist, {"1": "Lounge"}, {"1": ["a", "b"]}, "t0", str)
    state, changed = apply(state, hist, {"1": "Lounge lights"}, {"1": ["b", "c"]}, "t1", str.upper)
    assert changed
    assert kinds(hist["1"]) == [("renamed", "Lounge lights"), ("removed", "a"), ("added", "c")]
    assert hist["1"][1]["name"] == "A" and hist["1"][0]["from"] == "Lounge"
    state, _ = apply(state, hist, {"1": "Lounge lights", "2": "Kitchen"}, {"1": ["b", "c"], "2": ["d"]}, "t2", str)
    assert kinds(hist["2"]) == [("created", 1), ("added", "d")]
    state, _ = apply(state, hist, {"1": "Lounge lights"}, {"1": ["b", "c"]}, "t3", str)
    assert hist["2"][-1] == {"at": "t3", "kind": "removed_group", "name": "Kitchen"}
    assert "2" not in state
    # Nothing new: nothing changes.
    _, changed = apply(state, hist, {"1": "Lounge lights"}, {"1": ["b", "c"]}, "t4", str)
    assert not changed


def test_history_is_bounded():
    hist: dict = {}
    state, _ = apply(None, hist, {"1": "G"}, {"1": []}, "t", str)
    for n in range(MAX_EVENTS):
        state, _ = apply(state, hist, {"1": "G"}, {"1": [f"x{n}"]}, f"t{n:03d}", str)
    assert len(hist["1"]) == MAX_EVENTS


def test_prune_drops_long_removed_groups():
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    hist = {
        "1": [{"at": "2026-01-01T00:00:00+00:00", "kind": "removed_group"}],
        "2": [{"at": "2026-01-01T00:00:00+00:00", "kind": "added"}],
    }
    assert prune(hist, {"2"}, now)
    assert list(hist) == ["2"]
    assert DELETED_DAYS == 90


def test_clean_drops_malformed():
    assert clean({"z": {"1": [{"at": "t", "kind": "added"}, {"kind": "x"}, "bad"], "2": "x"}, "y": 1}) == {
        "z": {"1": [{"at": "t", "kind": "added"}]}
    }
    assert clean_state({"z": {"1": {"name": "G", "members": ["a", 3]}, "2": 1}}) == {
        "z": {"1": {"name": "G", "members": ["a"], "since": None}}
    }


def test_tracker_records_changes_and_survives_restart(make_tracker, clock):
    tracker = make_tracker()
    tracker.load()
    _devices(tracker)
    _groups(tracker, {1: [ROUTER_A, ROUTER_B]}, {1: "Lounge"})
    assert tracker.data["group_history"]["zigbee2mqtt"] == {}
    clock.advance(hours=1)
    _groups(tracker, {1: [ROUTER_A, PLUG]}, {1: "Lounge"})
    health = tracker.group_health(clock.time())
    assert [(e["kind"], e["name"]) for e in health[0]["history"]] == [("added", "Spot 3"), ("removed", "Spot 2")]
    assert health[0]["since"]
    tracker.save(force=True)
    # Changed while the app was stopped: noticed when the retained list arrives again.
    again = make_tracker()
    again.load()
    _devices(again)
    _groups(again, {1: [ROUTER_A]}, {1: "Lounge"})
    events = again.data["group_history"]["zigbee2mqtt"]["1"]
    assert [e["kind"] for e in events] == ["removed", "added", "removed"]


def test_new_empty_group_is_listed_with_its_history(tracker, clock):
    _devices(tracker)
    _groups(tracker, {1: [ROUTER_A]})
    _groups(tracker, {1: [ROUTER_A], 2: []})
    groups = {g["id"]: g for g in tracker.group_health(clock.time())}
    assert [e["kind"] for e in groups["2"]["history"]] == ["created"]
    send(tracker, "bridge/groups", [])
    # Removed groups leave the list; their history stays in history.json for a while.
    assert tracker.group_health(clock.time()) == []
    assert tracker.data["group_history"]["zigbee2mqtt"]["2"][-1]["kind"] == "removed_group"
