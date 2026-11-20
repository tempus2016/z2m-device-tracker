"""Standalone mode: the app in plain Docker beside Home Assistant Container, with no Supervisor.

Under Supervisor the app gets the HA API, its options and its sidebar login from Supervisor. Without
it, Home Assistant is reached at HA_URL with a long-lived access token (HA_TOKEN), options live in
/data/options.json, and the web UI asks for UI_PASSWORD before it shows anything.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit

SESSION_COOKIE = "z2mt_session"
SESSION_DAYS = 30
MAX_SESSIONS = 50
MIN_UI_CHARS = 8
# Wrong passwords from one address before it has to wait, and how long it waits.
LOGIN_TRIES = 5
LOGIN_LOCKOUT = 15 * 60


def is_standalone(env: dict | None = None) -> bool:
    """No Supervisor token means no Supervisor: the app runs in plain Docker."""
    env = os.environ if env is None else env
    return not env.get("SUPERVISOR_TOKEN")


def ha_endpoints(url: str | None) -> tuple[str, str] | None:
    """Home Assistant's REST API and WebSocket URLs for a base URL like http://192.168.1.10:8123.

    None when the URL isn't a plain http(s) address (a user name or password in it is refused too).
    """
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return None
    base = f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"
    ws_scheme = "wss" if parts.scheme == "https" else "ws"
    return f"{base}/api", f"{ws_scheme}://{parts.netloc}{parts.path.rstrip('/')}/api/websocket"


def save_options(path: str, options: dict) -> bool:
    """Write the options file in one step, so a crash never leaves half a file."""
    tmp = f"{path}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(options, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    return True


def password_ok(given: str, expected: str) -> bool:
    """Constant-time comparison of the typed password with UI_PASSWORD (compare_digest reveals at most the length)."""
    if not expected:
        return False
    return hmac.compare_digest(str(given or "").encode("utf-8"), expected.encode("utf-8"))


def cookie_value(header: str | None, name: str = SESSION_COOKIE) -> str | None:
    if not header:
        return None
    jar = SimpleCookie()
    try:
        jar.load(header)
    except CookieError:
        return None
    morsel = jar.get(name)
    return morsel.value if morsel is not None else None


def session_cookie(token: str, secure: bool) -> str:
    parts = [
        f"{SESSION_COOKIE}={token}",
        "Path=/",
        f"Max-Age={SESSION_DAYS * 86400}",
        "HttpOnly",
        "SameSite=Strict",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


class Sessions:
    """Signed-in browsers, kept in memory: a restart asks for the password again."""

    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._expires: dict[str, float] = {}

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._expires = {k: v for k, v in self._expires.items() if v > now}
            while len(self._expires) >= MAX_SESSIONS:
                self._expires.pop(min(self._expires, key=self._expires.__getitem__))
            self._expires[token] = now + SESSION_DAYS * 86400
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            expires = self._expires.get(token)
            if expires is None:
                return False
            if expires <= self._clock():
                del self._expires[token]
                return False
            return True


class LoginThrottle:
    """Slows down password guessing: after LOGIN_TRIES misses an address waits LOGIN_LOCKOUT seconds."""

    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._misses: dict[str, list[float]] = {}

    def blocked(self, client: str) -> bool:
        now = self._clock()
        with self._lock:
            recent = [t for t in self._misses.get(client, []) if now - t < LOGIN_LOCKOUT]
            if recent:
                self._misses[client] = recent
            else:
                self._misses.pop(client, None)
            return len(recent) >= LOGIN_TRIES

    def failed(self, client: str) -> None:
        with self._lock:
            self._misses.setdefault(client, []).append(self._clock())
            if len(self._misses) > 1000:
                # Never grows without bound: forget the oldest addresses.
                for key in sorted(self._misses, key=lambda k: self._misses[k][-1])[:500]:
                    del self._misses[key]

    def succeeded(self, client: str) -> None:
        with self._lock:
            self._misses.pop(client, None)
