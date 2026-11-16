"""What uses a device in Home Assistant: lookup, cache, snapshot and the notification line.

Home Assistant is never contacted: every WebSocket and REST call is mocked.
"""

from __future__ import annotations

import json
import logging

import pytest
from conftest import PLUG, SENSOR, coordinator, send, z2m_device
from ha import HomeAssistant, build_usage
from usage import RETRY_DELAYS, UsageRefresher

STATES = [
    {
        "entity_id": "automation.hall_lights",
        "attributes": {"friendly_name": "Hall lights at night", "id": "1700000000001"},
    },
    {"entity_id": "automation.yaml_only", "attributes": {"friendly_name": "Old YAML one"}},
    {"entity_id": "script.leak_shut_off", "attributes": {"friendly_name": "Leak shut-off"}},
    {"entity_id": "scene.movie", "attributes": {"friendly_name": "Movie", "id": "1700000000002"}},
]


def by_id(states):
    return {s["entity_id"]: s for s in states}


def test_build_usage_names_and_edit_ids():
    related = {
        "automation": ["automation.yaml_only", "automation.hall_lights"],
        "script": ["script.leak_shut_off"],
        "scene": ["scene.movie"],
        "entity": ["switch.plug"],
    }
    usage = build_usage(related, by_id(STATES))
    assert usage["automations"] == [
        {"entity_id": "automation.hall_lights", "name": "Hall lights at night", "id": "1700000000001"},
        {"entity_id": "automation.yaml_only", "name": "Old YAML one", "id": None},
    ]
    assert usage["scripts"] == [{"entity_id": "script.leak_shut_off", "name": "Leak shut-off", "id": "leak_shut_off"}]
    assert usage["scenes"] == [{"entity_id": "scene.movie", "name": "Movie", "id": "1700000000002"}]


def test_build_usage_without_states_or_hits():
    usage = build_usage({"automation": ["automation.gone"], "script": ["script.wake"]}, {})
    assert usage["automations"] == [{"entity_id": "automation.gone", "name": "gone", "id": None}]
    assert usage["scripts"][0]["id"] == "wake"
    assert build_usage(None, {}) == {"automations": [], "scripts": [], "scenes": []}


class FakeHA:
    def __init__(self, home: HomeAssistant, search: dict) -> None:
        self.calls: list[list[dict]] = []
        self.state_reads = 0
        self.search = search
        home._ws_call = self.ws_call
        home._request = self.request

    def ws_call(self, messages):
        self.calls.append(messages)
        assert all(m["type"] == "search/related" and m["item_type"] == "device" for m in messages)
        return [self.search.get(m["item_id"], {}) for m in messages]

    def request(self, method, path, body=None):
        assert (method, path) == ("GET", "/states")
        self.state_reads += 1
        return STATES


@pytest.fixture
def home(tmp_path) -> HomeAssistant:
    h = HomeAssistant(str(tmp_path))
    h.token = "token"
    return h


def test_device_usage_one_connection_and_states_only_when_needed(home):
    fake = FakeHA(home, {"dev-a": {"automation": ["automation.hall_lights"]}})
    found = home.device_usage(["dev-a", "dev-b", "dev-a"])
    assert len(fake.calls) == 1 and [m["item_id"] for m in fake.calls[0]] == ["dev-a", "dev-b"]
    assert fake.state_reads == 1
    assert found["dev-a"]["automations"][0]["name"] == "Hall lights at night"
    assert found["dev-b"] == {"automations": [], "scripts": [], "scenes": []}

    fake = FakeHA(home, {})
    home.device_usage(["dev-b"])
    assert fake.state_reads == 0
    assert home.device_usage([]) == {}


def usage(*names: str) -> dict:
    return {
        "automations": [{"entity_id": f"automation.a{n}", "name": name, "id": str(n)} for n, name in enumerate(names)],
        "scripts": [],
        "scenes": [],
    }


@pytest.fixture
def linked(tracker):
    send(
        tracker,
        "bridge/devices",
        [coordinator(), z2m_device(SENSOR, "Leak sensor"), z2m_device(PLUG, "Plug", "Router")],
    )
    tracker.ha_device_ids = {SENSOR: "dev-sensor", PLUG: "dev-plug"}
    tracker.issues = [{"key": f"offline:{SENSOR}", "ieee": SENSOR, "kind": "offline"}]
    return tracker


def test_wanted_is_issue_devices_then_critical_ones_linked_to_ha(linked):
    assert linked.usage_wanted() == [SENSOR]
    linked.data["devices"][PLUG]["critical"] = True
    assert linked.usage_wanted() == [SENSOR, PLUG]
    del linked.ha_device_ids[SENSOR]
    assert linked.usage_wanted() == [PLUG]


def test_due_only_for_newly_opened_issues_with_stale_lookups(linked, clock):
    assert linked.usage_due() == [SENSOR]
    assert linked.usage_due() == []  # same issue, not new
    linked.set_ha_usage({PLUG: usage("Night light")})
    linked.issues.append({"key": f"battery_low:{PLUG}", "ieee": PLUG, "kind": "battery_low"})
    assert linked.usage_due() == []  # looked up just now
    linked.issues = [i for i in linked.issues if i["ieee"] != PLUG]
    linked.usage_due()
    clock.advance(minutes=11)
    linked.issues.append({"key": f"battery_low:{PLUG}", "ieee": PLUG, "kind": "battery_low"})
    assert linked.usage_due() == [PLUG]


def test_set_usage_bumps_revision_only_on_change_and_prunes(linked):
    before = linked.version
    linked.set_ha_usage({SENSOR: usage("Leak shut-off")})
    after = linked.version
    assert after != before
    linked.set_ha_usage({SENSOR: usage("Leak shut-off")})
    assert linked.version == after
    linked.set_ha_usage({PLUG: usage("Night light")}, keep={PLUG})
    assert set(linked.ha_usage) == {PLUG}


def test_snapshot_carries_used_by_only_when_something_uses_the_device(linked):
    linked.set_ha_usage({SENSOR: usage("Leak shut-off"), PLUG: usage()})
    devices = {d["ieee"]: d for d in json.loads(linked.snapshot_json())["devices"]}
    assert devices[SENSOR]["used_by"] == {
        "automations": [{"entity_id": "automation.a0", "name": "Leak shut-off", "id": "0"}],
        "scripts": [],
        "scenes": [],
    }
    assert "used_by" not in devices[PLUG]


def test_usage_line_keeps_notifications_short(linked):
    assert linked.usage_line(SENSOR) is None
    linked.set_ha_usage({SENSOR: usage("Hall lights at night", "Leak shut-off")})
    assert linked.usage_line(SENSOR) == "Used by: Hall lights at night, Leak shut-off"
    long = "x" * 80
    linked.set_ha_usage({SENSOR: usage("A", "B", long, "D", "E")})
    line = linked.usage_line(SENSOR)
    assert line == f"Used by: A, B, {'x' * 39}… (+2 more)"


class FlakyHA:
    """Stands in for HomeAssistant: device_usage fails while `down` is set."""

    available = True

    def __init__(self) -> None:
        self.down = False
        self.calls: list[list[str]] = []

    def device_usage(self, device_ids):
        self.calls.append(list(device_ids))
        if self.down:
            raise RuntimeError("Handshake status 502 Bad Gateway")
        return {d: usage("Leak shut-off") for d in device_ids}


@pytest.fixture
def refresher(linked, clock):
    ha_fake = FlakyHA()
    return UsageRefresher(linked, ha_fake, clock.time), ha_fake


def run_due(r: UsageRefresher):
    """What start_due does, on this thread."""
    job = r.due()
    if job is None:
        return None
    r._run(*job)
    return job


def test_failed_lookup_retries_with_backoff_then_waits_for_normal_triggers(refresher, linked, clock, caplog):
    r, fake = refresher
    fake.down = True
    r.request_full()
    assert run_due(r) == (None,)
    assert len(fake.calls) == 1 and linked.ha_usage == {}
    for delay in RETRY_DELAYS:
        assert r.retry_at == clock.time() + delay
        clock.advance(delay - 1)
        assert run_due(r) is None  # not yet, and nothing stacked
        clock.advance(1)
        assert run_due(r) == (None,)
    assert len(fake.calls) == 1 + len(RETRY_DELAYS)
    assert r.retry_at is None
    clock.advance(hours=5)
    assert run_due(r) is None  # backoff used up: only a registry refresh or new issue now

    fake.down = False
    caplog.set_level(logging.INFO, logger="z2m_tracker")
    r.request_full()
    assert run_due(r) == (None,)
    assert linked.used_by(SENSOR)
    assert (r.failures, r.retry_at) == (0, None)
    assert "Found what uses 1 devices in Home Assistant" in caplog.text


def test_new_issue_lookup_that_fails_is_retried_for_every_wanted_device(refresher, linked, clock):
    r, fake = refresher
    fake.down = True
    assert run_due(r) == ([SENSOR],)  # the issue just opened
    fake.down = False
    clock.advance(RETRY_DELAYS[0])
    assert run_due(r) == (None,)
    assert linked.used_by(SENSOR)


def test_nothing_is_dropped_while_a_lookup_runs(refresher, linked):
    r, fake = refresher
    r.busy = True  # a lookup is running on another thread
    r.request_full()
    assert r.due() is None
    assert r.due() is None
    r.busy = False
    assert r.due() == (None,)  # the registry refresh's lookup still comes
    assert r.busy


def test_success_after_registry_refresh_even_with_a_failure_before(refresher, linked, clock):
    r, fake = refresher
    fake.down = True
    r.request_full()
    run_due(r)
    fake.down = False
    r.request_full()  # HA came back, registry refreshed: don't wait for the retry
    assert run_due(r) == (None,)
    assert linked.used_by(SENSOR) and r.retry_at is None
