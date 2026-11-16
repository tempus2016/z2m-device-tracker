"""Reporting profiles: how long a device may stay silent before "Stopped reporting" flags it.

Each device can pick a profile on its Health tab:

  change   Reports on change   silent after 3 h  (door, leak, smoke, buttons: events plus a check-in)
  regular  Regular             silent after 1 h  (sensors that report every few minutes)
  chatty   Chatty              silent after 15 min (plugs and bulbs that report all the time)
  custom   Custom              the device's own interval (record["report_every"], 30 s to 7 days)
  off      Off                 never flagged

A device without a profile uses the default for its type (Settings > Checks), stored by the app in
history.json under "report_profile_defaults": {group: profile}. A default only applies once the
device has shown it reports often enough for it: its usual gap over the last week, times
DEFAULT_MARGIN, must fit inside the profile's limit. That keeps a bulb that never reports, or a
remote that only sends when pressed, from being flagged by a default nobody chose for it.

Devices from before profiles existed have "report_every" and no "report_profile": they are Custom.
"""

from __future__ import annotations

from typing import Any

PROFILES = {"change": 3 * 3600, "regular": 3600, "chatty": 15 * 60}
CHOICES = (*PROFILES, "custom", "off")
DEFAULT_CHOICES = (*PROFILES, "off")
DEFAULT_MARGIN = 3

# Device type groups for the defaults, by naming.kind_of().
GROUP_OF_KIND = {
    "Door": "contact",
    "Leak": "leak",
    "Smoke": "smoke",
    "Gas": "smoke",
    "CO": "smoke",
    "Button": "button",
    "Switch": "button",
    "Motion": "motion",
    "Presence": "motion",
    "Vibration": "motion",
    "Temp": "sensor",
    "Air": "sensor",
    "Light sensor": "sensor",
    "Plug": "plug",
    "Bulb": "bulb",
}
GROUPS = ("contact", "leak", "smoke", "button", "motion", "sensor", "plug", "bulb", "other")
DEFAULTS = {
    "contact": "change",
    "leak": "change",
    "smoke": "change",
    "button": "change",
    "motion": "change",
    "sensor": "regular",
    "plug": "chatty",
    "bulb": "chatty",
    "other": "off",
}


def group_of(kind: str | None) -> str:
    return GROUP_OF_KIND.get(kind or "", "other")


def defaults(stored: Any) -> dict[str, str]:
    """The built-in defaults with the user's choices on top; unknown groups or profiles are ignored."""
    out = dict(DEFAULTS)
    if isinstance(stored, dict):
        out.update({g: p for g, p in stored.items() if g in GROUPS and p in DEFAULT_CHOICES})
    return out


def clean_defaults(changes: Any) -> dict[str, str]:
    """Validate a change to the defaults; raises ValueError on an unknown group or profile."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("expected {group: profile}")
    for group, profile in changes.items():
        if group not in GROUPS:
            raise ValueError(f"unknown device type {group!r}")
        if profile not in DEFAULT_CHOICES:
            raise ValueError(f"unknown profile {profile!r}")
    return dict(changes)


def usual_gap(summary: dict) -> float | None:
    """Average seconds between messages over the last week (Metrics.summary msg_rate_prev), if known."""
    rate = summary.get("msg_rate_prev")
    return 3600 / rate if isinstance(rate, (int, float)) and rate > 0 else None


def effective(record: dict, kind: str | None, summary: dict, stored_defaults: Any) -> dict[str, Any]:
    """The device's profile and the silent limit it gives.

    {"profile": chosen profile or None, "group", "default": the type default, "every": seconds or None,
    "source": "device" | "default", "gap": usual gap in seconds or None, "waiting": True when the type
    default would apply but the device hasn't shown it reports that often}"""
    chosen = record.get("report_profile")
    if chosen not in CHOICES:
        chosen = "custom" if record.get("report_every") else None
    group = group_of(kind)
    default = defaults(stored_defaults)[group]
    gap = usual_gap(summary)
    out: dict[str, Any] = {"profile": chosen, "group": group, "default": default, "gap": gap, "waiting": False}
    if chosen is not None:
        every = PROFILES.get(chosen) or (record.get("report_every") if chosen == "custom" else None)
        return {**out, "every": int(every) if every else None, "source": "device"}
    limit = PROFILES.get(default)
    fits = limit is not None and gap is not None and gap * DEFAULT_MARGIN <= limit
    return {**out, "every": limit if fits else None, "source": "default", "waiting": limit is not None and not fits}
