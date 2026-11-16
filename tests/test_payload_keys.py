"""Payload changes: new or gone keys in a device's messages, the logbook entry and the Health check."""

from __future__ import annotations

import json

import health
import payload_keys
from conftest import PLUG, START, coordinator, events_of, send, z2m_device
from payload_keys import GONE_AFTER, LEARN, NEW_AFTER, NEW_WITHIN, observe

OLD = {"state": "ON", "power": 10, "consumption": 1.5, "linkquality": 100}
NEW = {"state": "ON", "power": 10, "energy": 1.5, "power_on_behavior": "on", "linkquality": 100}
ENTITY = "sensor.kitchen_plug_consumption"


def feed(state: dict, keys, times: int) -> tuple[list, list]:
    added, removed = [], []
    for _ in range(times):
        a, r = observe(state, keys)
        added += a
        removed += r
    return added, removed


def test_nothing_reported_while_learning():
    state: dict = {}
    assert feed(state, OLD, LEARN) == ([], [])
    assert state["keys"] == {"state": LEARN, "power": LEARN, "consumption": LEARN}


def test_new_and_gone_keys_after_learning():
    state: dict = {}
    feed(state, OLD, LEARN)
    added, removed = feed(state, NEW, NEW_AFTER)
    assert added == ["energy", "power_on_behavior"]
    assert removed == []
    added, removed = feed(state, NEW, GONE_AFTER - NEW_AFTER)
    assert (added, removed) == ([], ["consumption"])
    # Reported once only.
    assert feed(state, NEW, 50) == ([], [])


def test_keys_that_come_and_go_are_never_reported():
    state: dict = {}
    for n in range(LEARN):
        observe(state, {**OLD, "contact": True} if n % 2 else OLD)
    added, removed = [], []
    for n in range(200):
        a, r = observe(state, {**OLD, "contact": True} if n % 3 == 0 else OLD)
        added += a
        removed += r
    assert (added, removed) == ([], [])


def test_one_off_key_is_never_reported():
    state: dict = {}
    feed(state, OLD, LEARN)
    observe(state, {**OLD, "calibration": 1})
    assert feed(state, OLD, NEW_WITHIN + 5) == ([], [])
    assert state["keys"]["calibration"] == 1
    assert "calibration" not in state.get("fresh", {})
    assert feed(state, {**OLD, "calibration": 1}, NEW_AFTER) == ([], [])


def test_ignored_keys():
    state: dict = {}
    feed(state, {**OLD, "action": "single", "update": {}}, LEARN)
    assert "action" not in state["keys"]
    assert feed(state, OLD, GONE_AFTER) == ([], [])


def test_stored_state_stays_small():
    state: dict = {}
    feed(state, OLD, LEARN)
    for n in range(500):
        observe(state, {**OLD, f"junk_{n}": 1})
    assert len(state["keys"]) <= payload_keys.MAX_KEYS
    assert len(state.get("fresh", {})) <= payload_keys.MAX_FRESH


def test_merge_cancels_a_key_that_came_back():
    assert payload_keys.merge({"added": ["energy"], "removed": ["consumption"]}, ["consumption"], []) == {
        "added": ["energy"],
        "removed": [],
    }


def test_clean_drops_bad_data():
    assert payload_keys.clean("junk") == {}
    assert payload_keys.clean({"n": "x", "keys": {"a": 3, "b": "y"}, "fresh": {"c": [1]}}) == {
        "n": 0,
        "keys": {"a": 3},
        "fresh": {},
    }


# ------------------------------------------------------------------ tracker


def setup(tracker) -> None:
    send(tracker, "bridge/devices", [coordinator(), z2m_device(PLUG, "Kitchen plug", "Router")])
    tracker.ha_entities[PLUG] = {"consumption_zigbee2mqtt": ENTITY}


def change_device(tracker, clock) -> None:
    setup(tracker)
    for _ in range(LEARN):
        send(tracker, "Kitchen plug", OLD)
        clock.advance(60)
    send(tracker, "Kitchen plug", {"update": {"state": "updating", "installed_version": 1}})
    send(tracker, "Kitchen plug", {"update": {"state": "idle", "installed_version": "2.1.4"}})
    for _ in range(GONE_AFTER):
        send(tracker, "Kitchen plug", NEW)
        clock.advance(60)


def test_change_is_logged_once_with_keys_and_firmware(tracker, clock):
    change_device(tracker, clock)
    [event] = events_of(tracker, "payload changed")
    assert event["added"] == ["energy", "power_on_behavior"]
    assert event["removed"] == ["consumption"]
    assert event["firmware"] == "2.1.4"
    assert event["detail"] == "New: energy, power_on_behavior. Gone: consumption"
    change = tracker.data["devices"][PLUG]["payload_change"]
    assert change["entities"] == {"consumption": ENTITY}
    assert change["at"] == event["time"]


def test_health_row_only_when_the_entity_is_used(tracker, clock):
    change_device(tracker, clock)
    issues, _ = tracker.evaluate_health()
    assert not [i for i in issues if i["kind"] == "payload_changed"]
    assert tracker.entity_usage_wanted() == [ENTITY]
    tracker.set_entity_usage(
        {ENTITY: {"automations": [{"entity_id": "automation.cost", "name": "Kitchen plug cost", "id": "1"}]}},
        [ENTITY],
    )
    assert tracker.entity_usage_wanted() == []
    issues, _ = tracker.evaluate_health()
    [issue] = [i for i in issues if i["kind"] == "payload_changed"]
    assert issue["severity"] == "info"
    assert issue["detail"] == (
        "Since firmware 2.1.4: consumption gone, energy, power_on_behavior new. "
        f"Still used by Kitchen plug cost ({ENTITY}), which may stop working."
    )
    clock.advance(days=31)
    issues, _ = tracker.evaluate_health()
    assert not [i for i in issues if i["kind"] == "payload_changed"]


def test_failed_lookup_keeps_what_was_known(tracker, clock):
    change_device(tracker, clock)
    used = {ENTITY: {"automations": [{"entity_id": "automation.cost", "name": "Cost", "id": None}], "scripts": []}}
    tracker.set_entity_usage(used, [ENTITY])
    tracker.set_entity_usage({}, [ENTITY])
    assert tracker.entity_usage[ENTITY]["automations"]


def test_baseline_survives_a_restart_and_stays_out_of_the_snapshot(tracker, make_tracker, clock):
    setup(tracker)
    for _ in range(LEARN):
        send(tracker, "Kitchen plug", OLD)
    tracker.save(force=True)
    again = make_tracker()
    again.load()
    assert again.data["devices"][PLUG]["payload_keys"]["n"] == LEARN
    device = next(d for d in json.loads(again.snapshot_json())["devices"] if d["ieee"] == PLUG)
    assert "payload_keys" not in device


def test_health_check_is_pure():
    record = {
        "ieee": PLUG,
        "status": "active",
        "payload_change": {"at": "2026-06-01T12:00:00+00:00", "added": [], "removed": ["consumption"]},
    }
    used = [{"key": "consumption", "entity_id": ENTITY, "automations": [], "scripts": [{"name": "Night"}]}]
    issues = health.device_issues(record, {"payload_usage": used}, {}, START + 3600, [])
    [issue] = [i for i in issues if i["kind"] == "payload_changed"]
    assert issue["params"]["names"] == "Night"
    assert issue["params"]["new"] == ""
