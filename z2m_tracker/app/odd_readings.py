"""Impossible and stuck readings: a temperature outside the set range, humidity or battery outside
0-100 %, and humidity stuck at exactly 0 or 100 % for hours.

The tracker notes every checked reading in record["odd_readings"] (note()): for each key the
lowest and highest believable value today and on the previous day it reported, plus the times of
impossible values in the last 24 hours. The health check (issue()) reads only that and the
record's readings, so it is a pure function.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

TEMP_KEYS = ("temperature", "local_temperature")
CHECKED = (*TEMP_KEYS, "humidity", "battery")
DEFAULT_TEMP_MIN = -30
DEFAULT_TEMP_MAX = 60
DEFAULT_HUMIDITY_HOURS = 6
# Impossible values are counted over this long, and the issue shows until the last one is this old.
BAD_WINDOW = 86400
MAX_BAD = 50
UNITS = {"temperature": " °C", "local_temperature": " °C", "humidity": "%", "battery": "%"}


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def temp_range(options: dict) -> tuple[float, float]:
    low = options.get("impossible_temp_min", DEFAULT_TEMP_MIN)
    high = options.get("impossible_temp_max", DEFAULT_TEMP_MAX)
    if not _number(low) or not _number(high) or low >= high:
        return DEFAULT_TEMP_MIN, DEFAULT_TEMP_MAX
    return low, high


def believable(key: str, value: float, options: dict) -> bool:
    """False for a value the sensor can't really have measured."""
    if key in TEMP_KEYS:
        low, high = temp_range(options)
    else:
        low, high = 0, 100
    return low <= value <= high


def note(store: dict, key: str, value: float, options: dict, now: float, day: str) -> bool:
    """Record one reading. Returns True when it was impossible (the record changed in a way that matters)."""
    entry = store.get(key)
    if not isinstance(entry, dict):
        entry = store[key] = {}
    if entry.get("day") != day:
        # A new day: today's range becomes the previous day's.
        if entry.get("lo") is not None:
            entry["plo"], entry["phi"] = entry.get("lo"), entry.get("hi")
        entry["day"], entry["lo"], entry["hi"] = day, None, None
    if believable(key, value, options):
        if entry["lo"] is None or value < entry["lo"]:
            entry["lo"] = value
        if entry["hi"] is None or value > entry["hi"]:
            entry["hi"] = value
        return False
    bad = [t for t in entry.get("bad") or [] if _number(t) and now - t < BAD_WINDOW]
    bad.append(int(now))
    entry["bad"] = bad[-MAX_BAD:]
    entry["value"] = value
    return True


def clean(store: Any) -> dict:
    """Keep only well-formed entries (stored or restored data can hold anything)."""
    if not isinstance(store, dict):
        return {}
    out = {}
    for key, entry in store.items():
        if key not in CHECKED or not isinstance(entry, dict):
            continue
        tidy = {k: v for k, v in entry.items() if k in ("lo", "hi", "plo", "phi", "value") and _number(v)}
        if isinstance(entry.get("day"), str):
            tidy["day"] = entry["day"][:10]
        if isinstance(entry.get("bad"), list):
            tidy["bad"] = [int(t) for t in entry["bad"] if _number(t)][-MAX_BAD:]
        out[key] = tidy
    return out


def _fmt(value: float) -> str:
    return f"{value:.1f}" if isinstance(value, float) and not value.is_integer() else f"{value:.0f}"


def _usual(entry: dict) -> tuple[float, float] | None:
    """The believable range today and on the previous day it reported."""
    lows = [v for v in (entry.get("lo"), entry.get("plo")) if _number(v)]
    highs = [v for v in (entry.get("hi"), entry.get("phi")) if _number(v)]
    return (min(lows), max(highs)) if lows and highs else None


def issue(record: dict, options: dict, now: float) -> dict | None:
    """The worst impossible or stuck reading on the device, as {msg, title, detail, params}, or None."""
    store = record.get("odd_readings") if isinstance(record.get("odd_readings"), dict) else {}
    for key in CHECKED:
        entry = store.get(key)
        if not isinstance(entry, dict):
            continue
        bad = [t for t in entry.get("bad") or [] if _number(t) and now - t < BAD_WINDOW]
        if not bad or not _number(entry.get("value")):
            continue
        value, n, unit = entry["value"], len(bad), UNITS[key]
        name = key.replace("_", " ")
        times = "once" if n == 1 else f"{n} times"
        if key == "battery":
            return {
                "msg": "impossible_battery",
                "title": "Impossible battery level",
                "detail": f"Battery {_fmt(value)}% is not a real value ({times} in 24 h). Battery estimates ignore it.",
                "params": {"reading": key, "value": value, "n": n},
            }
        usual = _usual(entry)
        params: dict[str, Any] = {"reading": key, "value": value, "n": n, "unit": unit.strip()}
        if usual:
            params["lo"], params["hi"] = usual
            rest = f" Its other readings: {_fmt(usual[0])} to {_fmt(usual[1])}{unit}."
        else:
            rest = ""
        return {
            "msg": "impossible_reading" if usual else "impossible_reading_no_range",
            "title": "Impossible reading",
            "detail": f"{name.capitalize()} {_fmt(value)}{unit}, {times} in 24 h.{rest} Often a failing battery or a bad contact.",
            "params": params,
        }
    hours = options.get("humidity_stuck_hours", DEFAULT_HUMIDITY_HOURS)
    if not _number(hours) or hours <= 0:
        return None
    reading = (record.get("readings") or {}).get("humidity")
    if not isinstance(reading, dict) or reading.get("value") not in (0, 100):
        return None
    try:
        age = now - datetime.fromisoformat(reading["changed_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None
    if age < hours * 3600:
        return None
    value = reading["value"]
    return {
        "msg": "humidity_stuck",
        "title": "Humidity stuck",
        "detail": f"Humidity stuck at {value:.0f}% for {int(age // 3600)} h. "
        + (
            "Condensation on the sensor, or the sensor has failed."
            if value == 100
            else "The sensor has probably failed."
        ),
        "params": {"reading": "humidity", "value": value, "seconds": int(age)},
    }
