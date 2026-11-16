"""Naming pattern, the friendly device kind, the naming check, and pairing progress."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from conftest import COORD, PLUG, ROUTER_A, SENSOR, coordinator, send, z2m_device
from health import CHECKS, DEFAULT_NOTIFY, KIND_LABELS
from naming import clean_pattern, kind_of, matches, render, values_of

V2 = {"version": "2.1.0"}
NEW = "0x00158d0000700001"
NEW2 = "0x00158d0000700002"


def naming_issues(tracker) -> list[dict]:
    return [i for i in tracker.evaluate_health()[0] if i["kind"] == "naming"]


def motion(ieee: str, name: str, **extra) -> dict:
    return z2m_device(
        ieee,
        name,
        definition={
            "model": "RTCGQ11LM",
            "vendor": "Aqara",
            "description": "Motion sensor",
            "exposes": [{"property": "occupancy"}, {"property": "illuminance"}, {"property": "battery"}],
        },
        **extra,
    )


# ------------------------------------------------------------------ pattern setting


@pytest.mark.parametrize(
    ("value", "clean"),
    [
        (None, ""),
        ("", ""),
        ("   ", ""),
        ("{area}  {type}   {n}", "{area} {type} {n}"),
        ("{model}", "{model}"),
        ("Zigbee {type} - {area}", "Zigbee {type} - {area}"),
    ],
)
def test_clean_pattern_accepts(value, clean):
    assert clean_pattern(value) == clean


@pytest.mark.parametrize(
    "value",
    ["{room} {type}", "{n}", "plain text", "{area} #", "{area}/", "{area {type}", "x" * 81 + "{area}", 5, ["{area}"]],
)
def test_clean_pattern_rejects(value):
    with pytest.raises(ValueError):
        clean_pattern(value)


def test_option_is_in_config_and_docs():
    root = Path(__file__).resolve().parent.parent / "z2m_tracker"
    config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    assert config["options"]["naming_pattern"] == ""
    assert config["schema"]["naming_pattern"] == "str?"
    assert "`naming_pattern`" in (root / "DOCS.md").read_text(encoding="utf-8")
    labels = yaml.safe_load((root / "translations/en.yaml").read_text(encoding="utf-8"))
    assert labels["configuration"]["naming_pattern"]["name"] == "Naming pattern"


# ------------------------------------------------------------------ kind


@pytest.mark.parametrize(
    ("record", "kind"),
    [
        ({"features": ["occupancy", "illuminance", "battery"]}, "Motion"),
        ({"features": ["contact", "battery", "temperature"]}, "Door"),
        ({"features": ["water_leak", "battery"]}, "Leak"),
        ({"features": ["state", "power", "energy"]}, "Plug"),
        ({"features": ["state"]}, "Switch"),
        ({"features": ["light"]}, "Bulb"),
        ({"features": ["action", "battery"]}, "Button"),
        ({"features": ["temperature", "humidity", "battery"]}, "Temp"),
        ({"features": ["climate", "battery"]}, "Thermostat"),
        ({"features": ["pm25"]}, "Air"),
        ({"features": [], "description": "Smart plug (with power monitoring)"}, "Plug"),
        ({"features": [], "type": "Router", "description": "Zigbee repeater"}, "Repeater"),
        ({"features": [], "type": "Router"}, "Repeater"),
        ({"features": [], "type": "EndDevice"}, None),
        ({"type": "Coordinator"}, None),
    ],
)
def test_kind_of(record, kind):
    assert kind_of(record) == kind


# ------------------------------------------------------------------ render and match


def test_render_numbers_from_one_and_skips_taken():
    values = {"area": "Kitchen", "type": "Motion", "model": "X"}
    assert render("{area} {type} {n}", values, set()) == "Kitchen Motion 1"
    assert render("{area} {type} {n}", values, {"kitchen motion 1", "kitchen motion 2"}) == "Kitchen Motion 3"


def test_render_without_n_adds_a_number_only_on_a_clash():
    values = {"area": "Hall", "type": "Door", "model": "X"}
    assert render("{area} {type}", values, set()) == "Hall Door"
    assert render("{area} {type}", values, {"hall door"}) == "Hall Door 2"


def test_render_leaves_out_empty_tokens_and_their_separators():
    values = {"area": "", "type": "Plug", "model": "TS011F"}
    assert render("{area} - {type} - {n}", values, set()) == "Plug - 1"
    assert render("{area} {type}", values, set()) == "Plug"
    assert render("{area}", values, set()) is None
    assert render("", values, set()) is None


def test_values_are_safe_for_zigbee2mqtt():
    values = values_of({"area": " Kitchen/Diner ", "model": "A+B#", "features": ["contact"]})
    assert values == {"area": "Kitchen-Diner", "type": "Door", "model": "AB"}


def test_matches():
    values = {"area": "Kitchen", "type": "Motion", "model": "RTCGQ11LM"}
    assert matches("{area} {type} {n}", "Kitchen Motion 2", values)
    assert matches("{area} {type} {n}", "kitchen  motion 12", values)
    assert not matches("{area} {type} {n}", "Kitchen Motion", values)
    assert not matches("{area} {type} {n}", "Hall Motion 1", values)
    assert matches("{area} {type}", "Kitchen Motion", values)
    assert matches("{area} {type}", "Kitchen Motion 2", values)
    assert not matches("{area} {type}", "Kitchen Motion sensor", values)
    # Unknown area: can't judge.
    assert matches("{area} {type}", "Whatever", {**values, "area": ""}) is None


def test_render_always_matches_its_own_pattern():
    values = {"area": "Living room", "type": "Light sensor", "model": "GZCGQ01LM"}
    for pattern in ("{area} {type} {n}", "{type} ({area})", "{model}-{n}", "Z {area}: {type}"):
        assert matches(pattern, render(pattern, values, {"living room light sensor 1"}), values)


# ------------------------------------------------------------------ the check


def test_naming_check_is_show_only():
    assert "naming" in KIND_LABELS
    assert any(c[0] == "naming" and not c[3] for c in CHECKS)
    assert "naming" not in DEFAULT_NOTIFY


def test_ieee_named_device_without_a_pattern(tracker, clock):
    send(tracker, "bridge/devices", [coordinator(), z2m_device(SENSOR, SENSOR), z2m_device(PLUG, "Hall plug")])
    [issue] = naming_issues(tracker)
    assert issue["key"] == f"naming:{SENSOR}"
    assert issue["severity"] == "info"
    assert issue["msg"] == "naming_ieee"
    assert issue["params"] == {"pattern": "", "suggested": None}
    assert issue["detail"] == "Still named by its IEEE address."


def test_device_still_interviewing_is_left_alone(tracker, clock):
    send(tracker, "bridge/devices", [coordinator(), z2m_device(SENSOR, SENSOR, interview_state="IN_PROGRESS")])
    assert naming_issues(tracker) == []


def test_pattern_mismatch_and_unique_suggestions(make_tracker, clock):
    tracker = make_tracker(options={"naming_pattern": "{area} {type} {n}"})
    send(
        tracker,
        "bridge/devices",
        [
            coordinator(),
            motion(SENSOR, "Kitchen Motion 1"),
            motion(NEW, NEW),
            motion(NEW2, "kitchen pir"),
            motion(PLUG, "Hall thing"),
        ],
    )
    for ieee in (SENSOR, NEW, NEW2):
        tracker.data["devices"][ieee]["area"] = "Kitchen"
    issues = {i["ieee"]: i for i in naming_issues(tracker)}
    # PLUG has no area: the pattern can't be judged, and its name isn't an address.
    assert set(issues) == {NEW, NEW2}
    assert issues[NEW]["msg"] == "naming_ieee"
    assert issues[NEW2]["msg"] == "naming_pattern"
    suggestions = {issues[NEW]["params"]["suggested"], issues[NEW2]["params"]["suggested"]}
    assert suggestions == {"Kitchen Motion 2", "Kitchen Motion 3"}
    assert issues[NEW2]["detail"].startswith("Doesn't follow {area} {type} {n}. Suggested: Kitchen Motion ")


def test_naming_check_can_be_disabled_and_muted(make_tracker, clock):
    tracker = make_tracker(options={"disabled_checks": ["naming"]})
    send(tracker, "bridge/devices", [coordinator(), z2m_device(SENSOR, SENSOR)])
    assert naming_issues(tracker) == []
    tracker.options["disabled_checks"] = []
    assert len(naming_issues(tracker)) == 1
    assert tracker.set_muted(SENSOR, "naming", True)
    assert naming_issues(tracker) == []


def test_snapshot_carries_kind(tracker, clock):
    send(tracker, "bridge/devices", [coordinator(), motion(SENSOR, "Hall motion")])
    devices = {d["ieee"]: d for d in json.loads(tracker.snapshot_json())["devices"]}
    assert devices[SENSOR]["kind"] == "Motion"
    assert devices[COORD]["kind"] is None


# ------------------------------------------------------------------ pairing progress


@pytest.fixture
def pairing(tracker, clock):
    def publish(topic: str, payload: dict) -> None:
        send(tracker, "bridge/info", {**V2, "permit_join": bool(payload.get("time"))})
        send(
            tracker,
            "bridge/response/permit_join",
            {"status": "ok", "data": {"time": payload.get("time")}, "transaction": payload["transaction"]},
        )

    tracker.publish = publish
    send(tracker, "bridge/info", {**V2, "permit_join": False})
    send(tracker, "bridge/devices", [coordinator(), z2m_device(ROUTER_A, "Hall plug", "Router")])
    assert tracker.start_pairing("zigbee2mqtt", 180)["ok"]
    return tracker


def progress(tracker) -> dict:
    return json.loads(tracker.snapshot_json())["pairing"]["zigbee2mqtt"]["progress"]


def event(tracker, kind: str, ieee: str, **data) -> None:
    send(tracker, "bridge/event", {"type": kind, "data": {"ieee_address": ieee, "friendly_name": ieee, **data}})


def test_pairing_progress_follows_the_interview(pairing):
    event(pairing, "device_joined", NEW)
    assert progress(pairing) == {NEW: "joined"}
    event(pairing, "device_announce", NEW)
    assert progress(pairing) == {NEW: "announced"}
    event(pairing, "device_interview", NEW, status="started")
    assert progress(pairing) == {NEW: "interviewing"}
    event(
        pairing,
        "device_interview",
        NEW,
        status="successful",
        definition={"model": "RTCGQ11LM", "vendor": "Aqara", "description": "Motion sensor", "exposes": []},
    )
    assert progress(pairing) == {NEW: "successful"}
    assert pairing.data["devices"][NEW]["interview_state"] == "SUCCESSFUL"
    # A later announce (the device waking up) doesn't undo the result.
    event(pairing, "device_announce", NEW)
    assert progress(pairing) == {NEW: "successful"}


def test_pairing_progress_shows_a_failed_interview(pairing):
    event(pairing, "device_joined", NEW)
    event(pairing, "device_interview", NEW, status="started")
    event(pairing, "device_interview", NEW, status="failed")
    assert progress(pairing) == {NEW: "failed"}
    assert pairing.data["devices"][NEW]["interview_state"] == "FAILED"


def test_announce_from_an_existing_device_is_not_listed(pairing):
    event(pairing, "device_announce", ROUTER_A)
    assert progress(pairing) == {}
    assert json.loads(pairing.snapshot_json())["pairing"]["zigbee2mqtt"]["joined"] == []


def test_new_pairing_session_starts_empty(pairing):
    event(pairing, "device_joined", NEW)
    assert pairing.start_pairing("zigbee2mqtt", 120)["ok"]
    assert progress(pairing) == {}


def test_empty_token_leaves_no_space_before_colon():
    import naming

    assert naming.render("Z {area}: {type}", {"area": "", "type": "Bulb"}, set()) == "Z: Bulb"
