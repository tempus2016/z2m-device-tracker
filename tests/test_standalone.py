"""Standalone mode (plain Docker, no Supervisor): HA URLs, options file, web UI sign-in."""

from __future__ import annotations

import json
import os

import standalone
from standalone import LOGIN_LOCKOUT, LOGIN_TRIES, LoginThrottle, Sessions


def test_standalone_only_without_supervisor_token():
    assert standalone.is_standalone({})
    assert standalone.is_standalone({"SUPERVISOR_TOKEN": ""})
    assert not standalone.is_standalone({"SUPERVISOR_TOKEN": "abc"})


def test_ha_endpoints_http_and_https():
    assert standalone.ha_endpoints("http://192.168.1.10:8123") == (
        "http://192.168.1.10:8123/api",
        "ws://192.168.1.10:8123/api/websocket",
    )
    assert standalone.ha_endpoints(" https://ha.example.com/ ") == (
        "https://ha.example.com/api",
        "wss://ha.example.com/api/websocket",
    )


def test_ha_endpoints_refuses_bad_urls():
    for url in (None, "", "192.168.1.10:8123", "ftp://ha", "http://user:pw@ha:8123", "http://"):
        assert standalone.ha_endpoints(url) is None, url


def test_save_options_writes_whole_file(tmp_path):
    path = tmp_path / "options.json"
    assert standalone.save_options(str(path), {"base_topics": ["zigbee2mqtt"], "quiet_hours": True})
    assert json.loads(path.read_text()) == {"base_topics": ["zigbee2mqtt"], "quiet_hours": True}
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert not (tmp_path / "options.json.tmp").exists()


def test_save_options_reports_failure(tmp_path):
    assert not standalone.save_options(str(tmp_path / "missing" / "options.json"), {})
    # Not JSON: nothing half-written is left behind.
    assert not standalone.save_options(str(tmp_path / "options.json"), {"x": object()})
    assert list(tmp_path.iterdir()) == []


def test_password_ok():
    assert standalone.password_ok("correct horse", "correct horse")
    assert not standalone.password_ok("correct hors", "correct horse")
    assert not standalone.password_ok("", "")
    assert not standalone.password_ok("anything", "")


def test_cookie_value():
    assert standalone.cookie_value("a=1; z2mt_session=tok-123; b=2") == "tok-123"
    assert standalone.cookie_value("a=1") is None
    assert standalone.cookie_value(None) is None


def test_session_cookie_flags():
    cookie = standalone.session_cookie("tok", secure=False)
    assert cookie.startswith("z2mt_session=tok;")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Secure" not in cookie
    assert "Secure" in standalone.session_cookie("tok", secure=True)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def test_sessions_expire():
    clock = Clock()
    sessions = Sessions(clock)
    token = sessions.create()
    assert sessions.valid(token)
    assert not sessions.valid("other")
    assert not sessions.valid(None)
    clock.now += standalone.SESSION_DAYS * 86400 + 1
    assert not sessions.valid(token)


def test_sessions_are_capped():
    sessions = Sessions(Clock())
    tokens = [sessions.create() for _ in range(standalone.MAX_SESSIONS + 5)]
    assert sum(sessions.valid(t) for t in tokens) == standalone.MAX_SESSIONS
    assert sessions.valid(tokens[-1])


def test_login_throttle_locks_out_then_forgives():
    clock = Clock()
    throttle = LoginThrottle(clock)
    for _ in range(LOGIN_TRIES - 1):
        throttle.failed("10.0.0.5")
    assert not throttle.blocked("10.0.0.5")
    throttle.failed("10.0.0.5")
    assert throttle.blocked("10.0.0.5")
    assert not throttle.blocked("10.0.0.6")
    clock.now += LOGIN_LOCKOUT + 1
    assert not throttle.blocked("10.0.0.5")


def test_login_success_clears_misses():
    throttle = LoginThrottle(Clock())
    for _ in range(LOGIN_TRIES - 1):
        throttle.failed("10.0.0.5")
    throttle.succeeded("10.0.0.5")
    throttle.failed("10.0.0.5")
    assert not throttle.blocked("10.0.0.5")
