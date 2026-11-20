"""Daily summary: one short message a day with what needs attention and what changed since yesterday.

Pure functions, no I/O and no clock. main.py decides when to call them and sends the result.

The app keeps no log of when a problem opened or cleared, so "since yesterday" is worked out from a
snapshot: the problems listed in the previous day's summary, stored in history.json under
"daily_summary" as {"sent": "YYYY-MM-DD", "open": {issue key: "Device name (problem)"}}.

- Needs attention: warnings open now (a device behind an offline router is counted with the router).
- New since yesterday: open now, not in the snapshot.
- Cleared since yesterday: in the snapshot, not open now.
- Batteries to buy: added when a message goes out anyway. A battery that is due stays due for weeks,
  so by itself it doesn't send a message every morning; the weekly summary covers it.

Nothing is sent on a day with no open problems and nothing cleared.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from health import WARNING, nested

MAX_LISTED = 8
MAX_SNAPSHOT = 500


def snapshot(issues: list[dict]) -> dict[str, str]:
    """The problems a summary would list now: {issue key: "Device name (problem)"}."""
    keys = {i["key"] for i in issues}
    out: dict[str, str] = {}
    for issue in issues:
        if issue.get("severity") != WARNING or nested(issue, keys):
            continue
        out[issue["key"]] = f"{issue.get('name') or '?'} ({str(issue.get('label') or '').lower()})"
        if len(out) >= MAX_SNAPSHOT:
            break
    return out


def clean_state(state: Any) -> dict[str, Any]:
    """The stored snapshot, or an empty one when it's missing or not in the expected shape."""
    if not isinstance(state, dict):
        return {"sent": None, "open": None}
    sent = state.get("sent")
    stored = state.get("open")
    if not isinstance(stored, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in stored.items()):
        stored = None
    return {"sent": sent if isinstance(sent, str) else None, "open": stored}


def _listed(names: list[str]) -> str:
    return "; ".join(names[:MAX_LISTED]) + ("…" if len(names) > MAX_LISTED else "")


def build(now_open: dict[str, str], before: dict[str, str] | None, batteries: str = "") -> tuple[str, str] | None:
    """(title, message) for today's summary, or None when there's nothing to say.

    `before` is yesterday's snapshot; None (no summary yet) leaves out the new and cleared lines.
    """
    new = [text for key, text in now_open.items() if before is not None and key not in before]
    cleared = [text for key, text in (before or {}).items() if key not in now_open]
    if not now_open and not cleared:
        return None
    lines = []
    if now_open:
        lines.append(f"Needs attention ({len(now_open)}): {_listed(list(now_open.values()))}")
    else:
        lines.append("Nothing needs attention.")
    if new:
        lines.append(f"New since yesterday ({len(new)}): {_listed(new)}")
    if cleared:
        lines.append(f"Cleared since yesterday ({len(cleared)}): {_listed(cleared)}")
    if batteries:
        lines.append(batteries)
    title = f"Zigbee daily summary: {len(now_open)} need attention" if now_open else "Zigbee daily summary: all clear"
    return title, "\n".join(lines)


def due(options: dict, local: datetime, sent: str | None) -> str | None:
    """What the scheduler should do this minute: "send", "skip" (the weekly summary goes out today
    instead, so only the snapshot is refreshed), or None (not time, off, or already done today)."""
    if not options.get("daily_summary"):
        return None
    try:
        hour = int(options.get("summary_hour", 9))
        weekly_day = int(options.get("summary_day", 0))
    except (TypeError, ValueError):
        return None
    if local.hour != hour or sent == local.date().isoformat():
        return None
    if options.get("weekly_summary") and local.weekday() == weekly_day:
        return "skip"
    return "send"
