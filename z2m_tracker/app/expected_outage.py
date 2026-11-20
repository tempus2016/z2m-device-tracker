"""Expected outages: a network that goes offline at about the same time most days.

Pure functions over a network's stored outages (bridge["outages"]: [{"start": ISO, "end": ISO or None}]),
no I/O and no clock.

Some setups take the network down on a schedule: a nightly Zigbee2MQTT restart, a backup that stops
the container, a router that reboots at 03:00. An alert every night for something that always comes
back teaches people to ignore the alert.

Rule
----
- Looked at: outages that began in the last DAYS days, have ended, and lasted under MAX_SECONDS.
- A pattern exists when outages began inside the same WINDOW_MINUTES of the local day on at least
  MIN_DAYS different days. The window is the one covering the most days; ties go to the earliest.
- An outage happening now is expected when it began inside that window, until GRACE_MINUTES after
  the window's end. One that runs on past that is a normal outage again.

Nothing is marked by hand. The pattern is found from the outages themselves, and stops applying
within a week of the schedule stopping.
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from statistics import median
from typing import Any

DAYS = 7
MIN_DAYS = 4
WINDOW_MINUTES = 90
MAX_SECONDS = 2 * 3600
GRACE_MINUTES = 30
DAY_MINUTES = 1440


def _ts(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def _minute_of_day(ts: float, tz: tzinfo) -> int:
    local = datetime.fromtimestamp(ts, tz=tz)
    return local.hour * 60 + local.minute


def pattern(outages: Any, now: float, tz: tzinfo) -> dict[str, int] | None:
    """{"start": window start (minute of the local day), "at": usual start (minute of the day),
    "minutes": usual length, "days": days it happened on} or None when there's no daily pattern."""
    starts: list[tuple[int, str, float]] = []  # (minute of day, local date, length in seconds)
    for outage in outages if isinstance(outages, list) else []:
        if not isinstance(outage, dict):
            continue
        began, ended = _ts(outage.get("start")), _ts(outage.get("end"))
        if began is None or ended is None or not now - DAYS * 86400 <= began < now:
            continue
        if not 0 <= ended - began < MAX_SECONDS:
            continue
        day = datetime.fromtimestamp(began, tz=tz).date().isoformat()
        starts.append((_minute_of_day(began, tz), day, ended - began))
    best: tuple[int, int] | None = None  # (days covered, window start)
    for window in sorted({minute for minute, _, _ in starts}):
        days = {day for minute, day, _ in starts if (minute - window) % DAY_MINUTES < WINDOW_MINUTES}
        if len(days) >= MIN_DAYS and (best is None or len(days) > best[0]):
            best = (len(days), window)
    if best is None:
        return None
    inside = [(m, length) for m, _, length in starts if (m - best[1]) % DAY_MINUTES < WINDOW_MINUTES]
    offset = int(median((m - best[1]) % DAY_MINUTES for m, _ in inside))
    return {
        "start": best[1],
        "at": (best[1] + offset) % DAY_MINUTES,
        "minutes": max(1, round(median(length for _, length in inside) / 60)),
        "days": best[0],
    }


def expected(found: dict[str, int] | None, began: float | None, now: float, tz: tzinfo) -> bool:
    """True while an outage that began at `began` is still inside the pattern's window plus the grace."""
    if not found or began is None or now < began:
        return False
    into_window = (_minute_of_day(began, tz) - found["start"]) % DAY_MINUTES
    if into_window >= WINDOW_MINUTES:
        return False
    return (now - began) / 60 <= WINDOW_MINUTES - into_window + GRACE_MINUTES


def clock(minute: int) -> str:
    """A minute of the day as HH:MM, rounded to the nearest quarter hour ("around 03:00")."""
    rounded = round(minute / 15) * 15 % DAY_MINUTES
    return f"{rounded // 60:02d}:{rounded % 60:02d}"
