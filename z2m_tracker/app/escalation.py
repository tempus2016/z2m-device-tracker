"""Escalation: an alert nobody answered goes again, once, to a second notify service.

Pure functions over a store dict kept in the history file (`escalation`), keyed by issue key:
{"at": when the alert was sent (epoch seconds), "ieee": ..., "kind": ..., "critical": bool,
"acked": bool, "escalated": bool}.

- `track` records each issue alert as it is sent.
- `acknowledge` marks a device's alerts as answered: a ping, or a button pressed on a phone alert.
- `due` drops entries whose problem is gone (fixed, muted or ignored: muted checks raise no issue)
  and returns the ones still open, unanswered and older than the delay, marking them escalated so
  each problem escalates at most once. A problem that clears and comes back is a new problem.
"""

from __future__ import annotations

from typing import Any

DEFAULT_HOURS = 2
MAX_HOURS = 168
# Entries kept at most; the oldest go first.
MAX_ENTRIES = 500


def settings(options: dict) -> dict | None:
    """The escalation settings when escalation is on and has a service to send to, else None."""
    if not options.get("escalate_enabled") or not options.get("escalate_service"):
        return None
    try:
        hours = float(options.get("escalate_hours", DEFAULT_HOURS))
    except (TypeError, ValueError):
        hours = DEFAULT_HOURS
    return {
        "seconds": max(0.0, min(hours, MAX_HOURS)) * 3600,
        "service": str(options["escalate_service"]),
        "critical_only": options.get("escalate_critical_only", True) is not False,
    }


def track(store: dict, issues: list[dict], now: float, critical_only: bool = True) -> None:
    """Record issue alerts just sent. With critical_only, only critical devices' alerts count."""
    for issue in issues:
        if critical_only and not issue.get("critical"):
            continue
        key = str(issue.get("key") or "")
        if not key or key in store:
            continue
        store[key] = {
            "at": now,
            "ieee": issue.get("ieee"),
            "kind": issue.get("kind"),
            "critical": bool(issue.get("critical")),
            "acked": False,
            "escalated": False,
        }
    if len(store) > MAX_ENTRIES:
        for key in sorted(store, key=lambda k: store[k].get("at") or 0)[: len(store) - MAX_ENTRIES]:
            del store[key]


def acknowledge(store: dict, ieee: str) -> bool:
    """Mark every open alert for the device as answered. True when one was waiting."""
    found = False
    for entry in store.values():
        if entry.get("ieee") == ieee and not entry.get("acked") and not entry.get("escalated"):
            entry["acked"] = True
            found = True
    return found


def due(
    store: dict, current: dict[str, dict], now: float, seconds: float, hold_routine: bool = False
) -> list[tuple[dict, float]]:
    """Issues to escalate now, with how long their alert has gone unanswered (seconds).

    `current` maps issue key -> issue for the issues open now. Entries whose issue is gone are dropped.
    With hold_routine (quiet hours), only critical devices escalate; the rest wait until it ends.
    """
    out: list[tuple[dict, float]] = []
    for key in list(store):
        entry = store[key]
        issue = current.get(key)
        if issue is None:
            del store[key]
            continue
        if entry.get("acked") or entry.get("escalated"):
            continue
        at = entry.get("at")
        waited = now - (float(at) if isinstance(at, (int, float)) else now)
        if waited < seconds:
            continue
        if hold_routine and not issue.get("critical"):
            continue
        entry["escalated"] = True
        out.append((issue, waited))
    return out


def message(issue: dict, base: str, waited: float) -> tuple[str, str]:
    """Title and text for the escalated alert."""
    hours = waited / 3600
    span = f"{hours:.0f} h" if hours >= 1 else f"{max(1, round(waited / 60))} min"
    title = f"Unanswered: {issue.get('notify_title') or base}"
    text = (
        f"{issue.get('notify_message') or issue.get('detail') or ''}\n\nNobody muted, pinged or fixed this in {span}."
    )
    return title, text.strip()


def clean(value: Any) -> dict:
    """The stored dict, or an empty one for anything else (older files have none)."""
    return value if isinstance(value, dict) else {}
