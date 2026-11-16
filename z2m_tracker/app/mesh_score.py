"""One 0-100 score per Zigbee network, from four parts the app already measures.

  online    40  share of the network's devices that aren't offline
  lqi       20  average 24-hour link quality, full marks at LQI_GOOD or better
  backup    20  share of devices in the last routing scan that don't depend on one router
  problems  20  open warnings, all 20 lost at max(5, a fifth of the devices)

A part without data (no link quality yet, no routing scan) is left out and the others are
scaled up to 100, so a network isn't marked down for something the app can't see.

The history is one entry a day per network (the day's last value), kept HISTORY_DAYS days, in
history.json under "mesh_scores": {base topic: [[date, score, online, total, lqi, backup, backup_of,
problems], ...]}.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

WEIGHTS = {"online": 40, "lqi": 20, "backup": 20, "problems": 20}
LQI_GOOD = 100
HISTORY_DAYS = 90
TREND_DAYS = 30
FIELDS = ("score", "online", "total", "lqi", "backup", "backup_of", "problems")


def score(
    online: int, total: int, lqi: float | None, backup: int | None, backup_of: int | None, problems: int
) -> dict[str, Any] | None:
    """The score and each part's share (0-1). None for a network without devices."""
    if total <= 0:
        return None
    parts: dict[str, float] = {"online": online / total}
    if lqi is not None:
        parts["lqi"] = min(1.0, max(0.0, lqi / LQI_GOOD))
    if backup is not None and backup_of:
        parts["backup"] = backup / backup_of
    parts["problems"] = max(0.0, 1 - problems / max(5, total / 5))
    weight = sum(WEIGHTS[k] for k in parts)
    value = round(100 * sum(WEIGHTS[k] * v for k, v in parts.items()) / weight)
    return {
        "score": value,
        "online": online,
        "total": total,
        "lqi": None if lqi is None else round(lqi),
        "backup": backup if backup_of else None,
        "backup_of": backup_of or None,
        "problems": problems,
        "parts": {k: round(v, 3) for k, v in parts.items()},
    }


def record(history: list, today: date, current: dict) -> bool:
    """Set today's entry from the current score; trim to HISTORY_DAYS. True when the history changed."""
    entry = [today.isoformat(), *(current.get(k) for k in FIELDS)]
    changed = False
    if history and history[-1][0] == entry[0]:
        if history[-1] != entry:
            history[-1] = entry
            changed = True
    else:
        history.append(entry)
        changed = True
    cutoff = (today - timedelta(days=HISTORY_DAYS)).isoformat()
    while history and history[0][0] < cutoff:
        history.pop(0)
        changed = True
    return changed


def clean(raw: Any) -> dict[str, list]:
    """Stored history as written by record(); anything malformed is dropped."""
    out: dict[str, list] = {}
    if not isinstance(raw, dict):
        return out
    for bt, rows in raw.items():
        if isinstance(rows, list):
            good = [r for r in rows if isinstance(r, list) and len(r) == 1 + len(FIELDS) and isinstance(r[0], str)]
            out[str(bt)] = sorted(good, key=lambda r: r[0])
    return out


def _as_dict(row: list) -> dict:
    return {"date": row[0], **dict(zip(FIELDS, row[1:], strict=True))}


def summary(history: list, today: date, current: dict) -> dict:
    """Current score plus the value a week ago (the newest entry 7 to 10 days old) and the 30-day trend."""
    week_day = (today - timedelta(days=7)).isoformat()
    oldest = (today - timedelta(days=10)).isoformat()
    week = next((r for r in reversed(history) if oldest <= r[0] <= week_day), None)
    start = (today - timedelta(days=TREND_DAYS)).isoformat()
    return {
        **current,
        "week": _as_dict(week) if week else None,
        "trend": [[r[0], r[1]] for r in history if r[0] >= start and r[1] is not None],
    }
