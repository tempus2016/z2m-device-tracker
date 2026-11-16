"""Maintenance tasks (test a smoke alarm monthly, ...) from the app's /api/devices payload.

Plain functions with no Home Assistant imports, so the todo and calendar platforms share them.
The app works out each task's next due date; this only picks and words them.
"""

from __future__ import annotations

from datetime import date
from typing import Any

# Open to-do items: overdue tasks and those due within this many days.
SOON_DAYS = 14
# Completed to-do items: tasks done within this many days.
RECENT_DAYS = 30
UNITS = {"days": ("day", "days"), "weeks": ("week", "weeks"), "months": ("month", "months"), "years": ("year", "years")}


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except (TypeError, ValueError):
        return None


def _tasks(data: dict) -> list[dict]:
    out = []
    for raw in data.get("maintenance") or []:
        if not isinstance(raw, dict) or not raw.get("ieee") or not raw.get("id"):
            continue
        due = _parse_date(raw.get("due"))
        if due is None:
            continue
        out.append(
            {
                **raw,
                "uid": f"{raw['ieee']}_{raw['id']}",
                "due": due,
                "last_done": _parse_date(raw.get("last_done")),
                "name": str(raw.get("name") or raw["ieee"]),
                "title": str(raw.get("title") or ""),
            }
        )
    return sorted(out, key=lambda x: (x["due"], x["name"].lower(), x["title"].lower()))


def due_tasks(data: dict, today: date, hidden: set[str] | frozenset[str] = frozenset()) -> list[dict]:
    """Overdue tasks and those due in the next SOON_DAYS days, soonest first, leaving out the uids in `hidden`."""
    return [x for x in _tasks(data) if (x["due"] - today).days <= SOON_DAYS and x["uid"] not in hidden]


def recently_done(data: dict, today: date, days: int = RECENT_DAYS) -> list[dict]:
    """Tasks done in the last `days` days that aren't due again soon, newest first."""
    out = [
        x
        for x in _tasks(data)
        if x["last_done"] is not None
        and 0 <= (today - x["last_done"]).days <= days
        and (x["due"] - today).days > SOON_DAYS
    ]
    return sorted(out, key=lambda x: x["last_done"], reverse=True)


def interval_text(task: dict) -> str:
    one, many = UNITS.get(str(task.get("unit")), ("day", "days"))
    every = task.get("every")
    return f"every {one}" if every == 1 else f"every {every} {many}"


def calendar_items(data: dict) -> list[dict]:
    """One all-day item per task on its next due date, plus one on the day it was last done."""
    out = []
    for task in _tasks(data):
        note = f" {task['note']}" if task.get("note") else ""
        out.append(
            {
                "uid": f"{task['uid']}_due",
                "date": task["due"],
                "summary": f"{task['title']}: {task['name']}",
                "description": f"Maintenance, {interval_text(task)}.{note}",
            }
        )
        if task["last_done"] is not None:
            out.append(
                {
                    "uid": f"{task['uid']}_done_{task['last_done'].isoformat()}",
                    "date": task["last_done"],
                    "summary": f"Done: {task['title']}: {task['name']}",
                    "description": f"Maintenance, {interval_text(task)}.",
                }
            )
    return sorted(out, key=lambda e: (e["date"], e["summary"].lower()))
