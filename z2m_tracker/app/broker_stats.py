"""Mosquitto's own statistics, from the $SYS/broker/# topics it publishes every sys_interval (10 s).

Keeps the latest values and an hourly history for the last 24 hours (messages received and
dropped), in memory only. Brokers that don't publish $SYS (or a login that may not read it)
simply never send anything, which snapshot() reports as "none" after a minute.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

PREFIX = "$SYS/broker/"
FILTER = "$SYS/broker/#"
HOURS = 24
# Without any $SYS message this long after subscribing, the broker doesn't publish them.
WAIT_SECONDS = 60
# Values older than this are stale (the broker stopped publishing them, or the connection dropped).
STALE_SECONDS = 180
# A gap between two counter readings longer than this isn't spread over one hour's bucket.
MAX_GAP = 3600
DROP_EVENTS = 50

# $SYS/broker/<key> -> the name kept, for plain numbers.
NUMBERS = {
    "clients/connected": "clients",
    "clients/total": "clients_total",
    "retained messages/count": "retained",
    "subscriptions/count": "subscriptions",
    "load/messages/received/1min": "received_1min",
    "load/messages/sent/1min": "sent_1min",
}


def _number(text: str) -> float | None:
    try:
        value = float(text.split()[0])
    except (ValueError, IndexError):
        return None
    return value if value == value and value not in (float("inf"), float("-inf")) and value >= 0 else None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


class BrokerStats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self, subscribed_at: float | None = None) -> None:
        with self.lock:
            self.values: dict[str, Any] = {}
            self.seen_at: float | None = None
            self.subscribed_at = subscribed_at
            self._total: tuple[float, float] | None = None  # (counter, when)
            self._dropped: float | None = None
            # [hour start, messages received, seconds covered, messages dropped]
            self.hours: deque[list] = deque(maxlen=HOURS)
            self.drops: deque[list] = deque(maxlen=DROP_EVENTS)

    def subscribed(self, now: float | None = None) -> None:
        with self.lock:
            self.subscribed_at = now or time.time()

    def _bucket(self, now: float) -> list:
        hour = int(now // 3600 * 3600)
        if not self.hours or self.hours[-1][0] != hour:
            self.hours.append([hour, 0, 0.0, 0])
        return self.hours[-1]

    def handle(self, topic: str, payload: bytes, now: float | None = None) -> None:
        """One $SYS message. Unknown topics are ignored; nothing here can raise on bad input."""
        if not topic.startswith(PREFIX):
            return
        key = topic[len(PREFIX) :]
        if key.startswith("log/"):
            return
        now = now or time.time()
        text = payload[:200].decode("utf-8", errors="ignore").strip()
        with self.lock:
            self.seen_at = now
            if key == "version":
                self.values["version"] = text[:80]
                return
            value = _number(text)
            if value is None:
                return
            if key == "uptime":
                self.values["uptime"] = int(value)
            elif key in NUMBERS:
                self.values[NUMBERS[key]] = int(value) if value.is_integer() else value
            elif key == "messages/received":
                self._received(value, now)
            elif key == "publish/messages/dropped":
                self._drop(value, now)

    def _received(self, total: float, now: float) -> None:
        if self._total is not None:
            last, at = self._total
            # A smaller counter means the broker restarted: everything since counts.
            delta = total - last if total >= last else total
            gap = now - at
            if 0 < gap <= MAX_GAP:
                bucket = self._bucket(now)
                bucket[1] += int(delta)
                bucket[2] += gap
        self._total = (total, now)

    def _drop(self, total: float, now: float) -> None:
        if self._dropped is not None:
            delta = total - self._dropped if total >= self._dropped else total
            if delta > 0:
                self._bucket(now)[3] += int(delta)
                self.drops.append([now, int(delta)])
        self._dropped = total

    def snapshot(self, enabled: bool = True, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        with self.lock:
            fresh = self.seen_at is not None and now - self.seen_at <= STALE_SECONDS
            if not enabled:
                state = "off"
            elif fresh:
                state = "ok"
            elif self.seen_at is not None:
                state = "stale"
            elif self.subscribed_at is not None and now - self.subscribed_at >= WAIT_SECONDS:
                state = "none"
            else:
                state = "waiting"
            day_ago = now - HOURS * 3600
            hours = [
                {
                    "hour": hour,
                    "per_min": round(received / (seconds / 60), 1) if seconds >= 60 else None,
                    "dropped": dropped,
                }
                for hour, received, seconds, dropped in self.hours
                if hour > day_ago - 3600
            ]
            drops = [d for d in self.drops if d[0] >= day_ago]
            return {
                "state": state,
                "seen_at": _iso(self.seen_at) if self.seen_at else None,
                **self.values,
                "hours": hours,
                "dropped_24h": sum(h["dropped"] for h in hours),
                "last_drop": {"at": _iso(drops[-1][0]), "n": drops[-1][1]} if drops else None,
            }
