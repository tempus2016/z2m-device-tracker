"""Escalation: an unanswered alert goes again, once, to a second notify service."""

from __future__ import annotations

import escalation

H = 3600


def issue(key: str, critical: bool = True, **extra) -> dict:
    kind, ieee = key.split(":", 1)
    return {"key": key, "kind": kind, "ieee": ieee, "critical": critical, "detail": "Unavailable for 1 h.", **extra}


LEAK = issue("offline:0xleak")
LAMP = issue("offline:0xlamp", critical=False)


def test_settings_need_switch_and_service():
    assert escalation.settings({}) is None
    assert escalation.settings({"escalate_enabled": True}) is None
    assert escalation.settings({"escalate_service": "notify.x"}) is None
    conf = escalation.settings({"escalate_enabled": True, "escalate_service": "notify.x", "escalate_hours": 3})
    assert conf == {"seconds": 3 * H, "service": "notify.x", "critical_only": True}
    conf = escalation.settings(
        {"escalate_enabled": True, "escalate_service": "notify.x", "escalate_critical_only": False}
    )
    assert conf["seconds"] == 2 * H and conf["critical_only"] is False


def test_escalates_once_after_the_delay():
    store: dict = {}
    escalation.track(store, [LEAK], 0)
    current = {LEAK["key"]: LEAK}
    assert escalation.due(store, current, 2 * H - 1, 2 * H) == []
    [(found, waited)] = escalation.due(store, current, 2 * H, 2 * H)
    assert found is LEAK and waited == 2 * H
    assert escalation.due(store, current, 10 * H, 2 * H) == []


def test_critical_only_skips_other_devices():
    store: dict = {}
    escalation.track(store, [LEAK, LAMP], 0, critical_only=True)
    assert list(store) == [LEAK["key"]]
    store = {}
    escalation.track(store, [LEAK, LAMP], 0, critical_only=False)
    assert sorted(store) == sorted([LEAK["key"], LAMP["key"]])


def test_ping_or_button_answers_it():
    store: dict = {}
    escalation.track(store, [LEAK], 0)
    assert escalation.acknowledge(store, "0xleak") is True
    assert escalation.acknowledge(store, "0xleak") is False
    assert escalation.due(store, {LEAK["key"]: LEAK}, 5 * H, 2 * H) == []


def test_fixed_or_muted_problem_is_dropped_and_a_return_is_new():
    store: dict = {}
    escalation.track(store, [LEAK], 0)
    assert escalation.due(store, {}, 1 * H, 2 * H) == []  # fixed (or muted: no issue)
    assert store == {}
    escalation.track(store, [LEAK], 3 * H)
    assert escalation.due(store, {LEAK["key"]: LEAK}, 4 * H, 2 * H) == []
    assert len(escalation.due(store, {LEAK["key"]: LEAK}, 5 * H, 2 * H)) == 1


def test_resent_alert_keeps_its_first_time():
    store: dict = {}
    escalation.track(store, [LEAK], 0)
    escalation.track(store, [LEAK], 1 * H)
    assert store[LEAK["key"]]["at"] == 0


def test_quiet_hours_hold_routine_devices_only():
    store: dict = {}
    escalation.track(store, [LEAK, LAMP], 0, critical_only=False)
    current = {LEAK["key"]: LEAK, LAMP["key"]: LAMP}
    found = escalation.due(store, current, 3 * H, 2 * H, hold_routine=True)
    assert [i["key"] for i, _ in found] == [LEAK["key"]]
    found = escalation.due(store, current, 9 * H, 2 * H, hold_routine=False)
    assert [i["key"] for i, _ in found] == [LAMP["key"]]


def test_message():
    title, text = escalation.message(LEAK, "Critical: Leak - Offline", 2 * H)
    assert title == "Unanswered: Critical: Leak - Offline"
    assert text == "Unavailable for 1 h.\n\nNobody muted, pinged or fixed this in 2 h."
    title, text = escalation.message({**LEAK, "notify_title": "Power loss in Garage?"}, "x", 30 * 60)
    assert title == "Unanswered: Power loss in Garage?"
    assert text.endswith("in 30 min.")


def test_store_is_capped():
    store: dict = {}
    escalation.track(store, [issue(f"offline:0x{n:016x}") for n in range(escalation.MAX_ENTRIES + 5)], 0)
    assert len(store) == escalation.MAX_ENTRIES


def test_older_files_load():
    assert escalation.clean(None) == {}
    assert escalation.clean([]) == {}
    assert escalation.clean({"a": {}}) == {"a": {}}
