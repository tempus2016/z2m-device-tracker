"""Reporting profiles: per-device choice, type defaults gated by how often the device reports, and old intervals."""

from __future__ import annotations

import json
from datetime import datetime

import health
import pytest
from conftest import PLUG, SENSOR, send, z2m_device
from reporting_profiles import DEFAULTS, clean_defaults, defaults, effective, group_of


def test_group_of_kinds():
    assert [group_of(k) for k in ("Door", "Leak", "Smoke", "Button", "Temp", "Plug", "Bulb", "Lock", None)] == [
        "contact",
        "leak",
        "smoke",
        "button",
        "sensor",
        "plug",
        "bulb",
        "other",
        "other",
    ]


def test_default_table_matches_the_brief():
    assert (DEFAULTS["contact"], DEFAULTS["sensor"], DEFAULTS["plug"], DEFAULTS["bulb"]) == (
        "change",
        "regular",
        "chatty",
        "chatty",
    )


def test_chosen_profiles():
    assert effective({"report_profile": "regular"}, "Temp", {}, None)["every"] == 3600
    assert effective({"report_profile": "chatty"}, "Door", {}, None)["every"] == 900
    assert effective({"report_profile": "off", "report_every": 60}, "Temp", {}, None)["every"] is None
    assert effective({"report_profile": "custom", "report_every": 120}, "Temp", {}, None)["every"] == 120


def test_old_interval_is_custom():
    out = effective({"report_every": 600}, "Temp", {}, None)
    assert (out["profile"], out["every"], out["source"]) == ("custom", 600, "device")


def test_type_default_waits_until_the_device_reports_often_enough():
    # Temp sensor, default Regular (1 h): needs a usual gap of at most 20 min.
    waiting = effective({}, "Temp", {"msg_rate_prev": 2}, None)
    assert (waiting["every"], waiting["waiting"], waiting["gap"]) == (None, True, 1800)
    fits = effective({}, "Temp", {"msg_rate_prev": 12}, None)
    assert (fits["every"], fits["source"], fits["waiting"]) == (3600, "default", False)
    assert effective({}, "Temp", {}, None)["waiting"] is True
    # "Other" defaults to off: never waiting.
    assert effective({}, "Lock", {"msg_rate_prev": 100}, None) | {"gap": None} == {
        "profile": None,
        "group": "other",
        "default": "off",
        "gap": None,
        "learned": None,
        "waiting": False,
        "every": None,
        "source": "default",
    }


def test_user_defaults_override_and_are_checked():
    assert defaults({"sensor": "chatty", "bogus": "x", "plug": "nope"})["sensor"] == "chatty"
    assert defaults({"plug": "nope"})["plug"] == "chatty"
    assert effective({}, "Temp", {"msg_rate_prev": 60}, {"sensor": "chatty"})["every"] == 900
    with pytest.raises(ValueError):
        clean_defaults({"sensor": "custom"})
    with pytest.raises(ValueError):
        clean_defaults({"fridge": "regular"})


def setup(tracker):
    temp = z2m_device(SENSOR, "Bathroom temp")
    plug = z2m_device(PLUG, "Kitchen plug", "Router")
    plug["definition"]["exposes"] = [{"property": "state"}, {"property": "power"}]
    send(tracker, "bridge/devices", [temp, plug])
    send(tracker, "Bathroom temp", {"temperature": 20.5})
    send(tracker, "Kitchen plug", {"state": "ON", "power": 5})


def late(tracker) -> list[str]:
    issues, _ = tracker.evaluate_health()
    return [i["ieee"] for i in issues if i["kind"] == "late"]


def test_device_profile_drives_the_late_check(tracker, clock):
    setup(tracker)
    assert tracker.set_report_profile(SENSOR, "chatty")["ok"]
    clock.advance(minutes=14)
    assert late(tracker) == []
    clock.advance(minutes=2)
    assert late(tracker) == [SENSOR]
    assert tracker.late_due(clock.time())
    assert tracker.set_report_profile(SENSOR, "off")["ok"]
    assert late(tracker) == []


def test_custom_and_default_choices(tracker, clock):
    setup(tracker)
    assert not tracker.set_report_profile(SENSOR, "custom")["ok"]
    assert tracker.set_report_profile(SENSOR, "custom", 300)["ok"]
    rec = tracker.data["devices"][SENSOR]
    assert (rec["report_profile"], rec["report_every"]) == ("custom", 300)
    assert tracker.set_report_profile(SENSOR, "default")["ok"]
    assert "report_profile" not in rec and "report_every" not in rec
    assert not tracker.set_report_profile(SENSOR, "loud")["ok"]
    assert not tracker.set_report_profile("0x0000000000000bad", "regular")["ok"]


def test_old_interval_keeps_working(tracker, clock):
    setup(tracker)
    tracker.data["devices"][SENSOR]["report_every"] = 60
    clock.advance(61)
    assert late(tracker) == [SENSOR]
    snap = json.loads(tracker.snapshot_json())
    dev = next(d for d in snap["devices"] if d["ieee"] == SENSOR)
    assert dev["reporting_profile"]["profile"] == "custom"
    assert dev["offline_timing"] is None or any(s["kind"] == "late" for s in dev["offline_timing"]["steps"])


def test_bulk_tool_sets_custom_and_off(tracker):
    setup(tracker)
    tracker.set_report_every_many([{"ieee": SENSOR, "seconds": 600}, {"ieee": PLUG, "seconds": None}])
    assert tracker.data["devices"][SENSOR]["report_profile"] == "custom"
    assert tracker.data["devices"][PLUG]["report_profile"] == "off"


def test_set_defaults_saved_and_in_snapshot(tracker):
    setup(tracker)
    assert tracker.set_report_defaults({"plug": "regular"})["ok"]
    assert not tracker.set_report_defaults({"plug": "custom"})["ok"]
    assert not tracker.set_report_defaults("x")["ok"]
    snap = json.loads(tracker.snapshot_json())
    assert snap["report_defaults"]["plug"] == "regular" and snap["report_defaults"]["contact"] == "change"
    plug = next(d for d in snap["devices"] if d["ieee"] == PLUG)
    assert (plug["reporting_profile"]["group"], plug["reporting_profile"]["default"]) == ("plug", "regular")


# ------------------------------------------------------------------ learned limit (Auto profile)

FITS = {"msg_rate_prev": 60}  # one message a minute: fits every built-in default
LONELY: dict = {}  # no usual gap known: no built-in default fits


def test_auto_with_a_learned_limit():
    out = effective({"report_profile": "auto"}, "Temp", {}, None, learned=7200)
    assert (out["every"], out["source"], out["learned"]) == (7200, "learned", 7200)


def test_auto_without_learning_has_no_limit():
    for kwargs in ({"learned": None}, {"learned": 7200, "learn": False}):
        out = effective({"report_profile": "auto"}, "Temp", {}, None, **kwargs)
        assert (out["every"], out["source"]) == (None, "device")


def test_learned_never_tightens_a_fitting_type_default():
    out = effective({}, "Door", FITS, None, learned=900)
    assert (out["every"], out["source"], out["learned"]) == (10800, "default", 900)
    out = effective({}, "Door", FITS, None, learned=20000)
    assert (out["every"], out["source"]) == (20000, "learned")


def test_hand_set_type_default_beats_learning():
    out = effective({}, "Door", FITS, {"contact": "off"}, learned=3600)
    assert (out["every"], out["source"]) == (None, "default")
    out = effective({}, "Door", FITS, {"contact": "chatty"}, learned=3600)
    assert (out["every"], out["source"]) == (900, "default")


def test_type_default_that_does_not_fit_gives_way_to_learning():
    out = effective({}, "Door", LONELY, None, learned=5000)
    assert (out["every"], out["source"]) == (5000, "learned")


def test_other_group_gets_learned_limit():
    out = effective({}, None, {}, None, learned=7200)
    assert (out["every"], out["source"]) == (7200, "learned")


def test_learn_off_behaves_as_before():
    cases = [
        ({"report_profile": "auto"}, "Temp", {}, None),
        ({}, "Door", FITS, None),
        ({}, "Door", FITS, {"contact": "off"}),
        ({}, "Door", LONELY, None),
        ({}, None, {}, None),
    ]
    for record, kind, summary, stored in cases:
        off = effective(record, kind, summary, stored, learned=60, learn=False)
        assert {k: v for k, v in off.items() if k != "learned"} == {
            k: v for k, v in effective(record, kind, summary, stored).items() if k != "learned"
        }


def test_explicit_profiles_ignore_learning():
    for profile, record in (
        ("change", {}),
        ("regular", {}),
        ("chatty", {}),
        ("custom", {"report_every": 120}),
        ("off", {}),
    ):
        rec = {"report_profile": profile, **record}
        assert effective(rec, "Temp", {}, None, learned=60) == {**effective(rec, "Temp", {}, None), "learned": 60}


def test_auto_is_accepted_and_next_deadline_is_last_seen_plus_learned(tracker, clock, monkeypatch):
    setup(tracker)
    seen = []

    def fake_rhythm(ieee, now=None, last_seen=None):
        seen.append(last_seen)
        return {"limit": 1800, "slot_longest": 1200, "longest": 40000, "learned": True, "excused": False}

    monkeypatch.setattr(tracker.metrics, "rhythm", fake_rhythm)
    assert tracker.set_report_profile(SENSOR, "auto")["ok"]
    clock.advance(minutes=29)
    assert late(tracker) == []
    clock.advance(minutes=2)
    assert SENSOR in late(tracker)
    issue = next(i for i in tracker.evaluate_health()[0] if i["kind"] == "late" and i["ieee"] == SENSOR)
    assert issue["msg"] == "late_learned" and issue["params"]["longest"] == 1200
    assert seen and all(isinstance(x, float) for x in seen)
    assert tracker.late_due(clock.time())
    rec = tracker.data["devices"][SENSOR]
    assert tracker._report_limits[SENSOR] == 1800
    assert health.late_deadline(rec, 0, 1800) == datetime.fromisoformat(rec["last_seen"]).timestamp() + 1800


def test_late_keeps_plain_text_when_the_type_default_won(tracker, clock, monkeypatch):
    setup(tracker)
    rec = tracker.data["devices"][SENSOR]
    rec["report_profile"] = None
    monkeypatch.setattr(
        tracker, "report_profile", lambda r, s, now=None, info=None: {"every": 3600, "source": "default"}
    )
    clock.advance(minutes=61)
    issue = next(i for i in tracker.evaluate_health()[0] if i["kind"] == "late" and i["ieee"] == SENSOR)
    assert issue["msg"] == "late"


def test_excused_is_reported_only_when_it_removes_the_limit():
    assert effective({"report_profile": "auto"}, "Temp", {}, None, excused=True)["excused"] is True
    assert effective({}, "Door", FITS, None, excused=True)["excused"] is True
    for record, stored, learn in (
        ({}, None, False),
        ({}, {"contact": "change"}, True),
        ({"report_profile": "regular"}, None, True),
        ({"report_profile": "auto"}, None, False),
    ):
        out = effective(record, "Door", FITS, stored, learned=None, learn=learn, excused=True)
        assert "excused" not in out
