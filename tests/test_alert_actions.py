"""Buttons on phone alerts: action ids, one-time codes, and the press coming back over the websocket."""

from __future__ import annotations

import threading
import types

import alert_actions
import ha
from test_ha import FakeSocket, home  # noqa: F401 - home is a fixture

PLUG = "0x00158d0000600001"
NOW = 1_800_000_000.0


def test_issue_builds_companion_actions_with_one_code():
    store: dict = {}
    actions = alert_actions.issue(store, PLUG, "offline", ["MUTE24", "PING", "BOGUS"], NOW)
    assert [a["title"] for a in actions] == ["Mute 24 h", "Ping"]
    (code,) = store
    assert actions[0]["action"] == f"Z2MT_MUTE24|{PLUG}|offline|{code}"
    assert all(len(a["action"]) <= 100 for a in actions)
    assert alert_actions.parse(actions[1]["action"]) == ("PING", PLUG, "offline", code)


def test_no_buttons_no_code():
    store: dict = {}
    assert alert_actions.issue(store, PLUG, "offline", [], NOW) == []
    assert store == {}


def test_claim_accepts_each_button_once():
    store: dict = {}
    actions = alert_actions.issue(store, PLUG, "battery_low", ["MUTE24", "BATTERY"], NOW)
    battery = actions[1]["action"]
    assert alert_actions.claim(store, battery, NOW + 60) == ("BATTERY", PLUG, "battery_low")
    assert alert_actions.claim(store, battery, NOW + 61) is None
    assert alert_actions.claim(store, actions[0]["action"], NOW + 62) == ("MUTE24", PLUG, "battery_low")


def test_claim_refuses_forged_and_expired_presses():
    store: dict = {}
    actions = alert_actions.issue(store, PLUG, "offline", ["MUTE24"], NOW)
    (code,) = store
    other = "0x00158d0000500001"
    for action in (
        f"Z2MT_MUTE24|{other}|offline|{code}",  # another device with a real code
        f"Z2MT_MUTE24|{PLUG}|weak_signal|{code}",  # another check
        f"Z2MT_PING|{PLUG}|offline|{code}",  # a button this alert didn't have
        f"Z2MT_MUTE24|{PLUG}|offline|000000000000",  # made-up code
        f"Z2MT_MUTE24|{PLUG.upper()}|offline|{code}",
        f"Z2MT_MUTE24|{PLUG}|offline|{code}|x",
        "Z2MT_MUTE24",
        None,
        ["list"],
        "x" * 5000,
    ):
        assert alert_actions.claim(store, action, NOW) is None, action
    assert alert_actions.claim(store, actions[0]["action"], NOW + alert_actions.VALID_SECONDS + 1) is None
    assert store == {}


def test_store_is_capped_and_pruned():
    store: dict = {}
    for i in range(alert_actions.MAX_CODES + 20):
        alert_actions.issue(store, PLUG, "offline", ["MUTE24"], NOW + i)
    assert len(store) == alert_actions.MAX_CODES
    alert_actions.issue(store, PLUG, "offline", ["MUTE24"], NOW + alert_actions.VALID_SECONDS + 10_000)
    assert len(store) == 1


def _run_watch(home, monkeypatch, messages, on_action):  # noqa: F811 - home is the fixture
    stop = threading.Event()
    socket = FakeSocket(messages, stop)
    monkeypatch.setitem(
        __import__("sys").modules, "websocket", types.SimpleNamespace(WebSocketTimeoutException=TimeoutError)
    )
    home._ws_connect = lambda timeout=15: socket
    changes: list = []
    home.watch(lambda: changes.append(1), stop, on_action)
    return socket, changes


def test_watch_passes_button_presses_on(home, monkeypatch):  # noqa: F811
    pressed: list = []
    press = {"event_type": ha.ACTION_EVENT, "data": {"action": "Z2MT_PING|x"}}
    socket, changes = _run_watch(
        home,
        monkeypatch,
        [{"id": 4, "type": "event", "event": press}, {"id": 1, "type": "event", "event": {"data": {}}}],
        pressed.append,
    )
    assert [m["event_type"] for m in socket.sent] == [*ha.REGISTRY_EVENTS, ha.ACTION_EVENT]
    assert pressed == [{"action": "Z2MT_PING|x"}]
    # Once on connect, once for the registry event; the press is not a registry change.
    assert len(changes) == 2


def test_watch_survives_a_failing_press_handler(home, monkeypatch):  # noqa: F811
    def boom(data):
        raise RuntimeError("bad")

    press = {"event_type": ha.ACTION_EVENT, "data": {"action": "x"}}
    _, changes = _run_watch(
        home,
        monkeypatch,
        [{"id": 4, "type": "event", "event": press}, {"id": 1, "type": "event", "event": {}}],
        boom,
    )
    assert len(changes) == 2


def test_watch_without_handler_does_not_subscribe(home, monkeypatch):  # noqa: F811
    socket, _ = _run_watch(home, monkeypatch, [], None)
    assert [m["event_type"] for m in socket.sent] == list(ha.REGISTRY_EVENTS)
