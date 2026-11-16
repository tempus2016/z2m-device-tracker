"""The last messages to and from each device, for the device page's Messages tab.

Memory only: nothing here is saved, so a restart starts every list empty. Each device keeps at
most MAX_MESSAGES, and each payload is cut to MAX_TEXT characters, so the whole log stays small
however chatty a device is.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Callable

MAX_MESSAGES = 50
MAX_TEXT = 2000
KINDS = ("state", "availability", "command")


class MessageLog:
    def __init__(self, size: int = MAX_MESSAGES, clock: Callable[[], float] = time.time) -> None:
        self.size = size
        self.clock = clock
        self._log: dict[str, deque] = {}

    def add(self, ieee: str, kind: str, payload: str, topic: str = "") -> dict:
        """Keep one message. kind is state, availability or command; topic is the part after the
        device's name (such as "set" or "l1/set"), only for commands."""
        text = payload if len(payload) <= MAX_TEXT else payload[: MAX_TEXT - 1] + "…"
        entry: dict = {"at": round(self.clock(), 3), "kind": kind, "payload": text}
        if topic:
            entry["topic"] = topic
        log = self._log.get(ieee)
        if log is None:
            log = self._log[ieee] = deque(maxlen=self.size)
        log.append(entry)
        return entry

    def settle(self, ieee: str, since: float, answered: bool, now: float | None = None) -> None:
        """Mark the commands sent since `since` that have no outcome yet: answered (with the reply
        time of each) or missed."""
        now = self.clock() if now is None else now
        for entry in reversed(self._log.get(ieee) or ()):
            if entry["at"] < since - 0.001:
                break
            if entry["kind"] != "command" or "reply_ms" in entry or entry.get("missed"):
                continue
            if answered:
                entry["reply_ms"] = max(0, round((now - entry["at"]) * 1000))
            else:
                entry["missed"] = True

    def recent(self, ieee: str) -> list[dict]:
        """The device's messages, newest first (copies, safe to hand to another thread)."""
        return [dict(entry) for entry in reversed(self._log.get(ieee) or ())]

    def forget(self, ieee: str) -> None:
        self._log.pop(ieee, None)
