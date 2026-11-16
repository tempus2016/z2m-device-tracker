"""Buttons on phone alerts (Companion app actionable notifications).

A button's action id names what to do, the device, the check and a one-time code:
Z2MT_MUTE24|0x00158d0000500001|battery_low|3f9a0c51d2e7. The press comes back as Home Assistant's
mobile_app_notification_action event, which any phone registered to any HA user (and anything that
may fire events) can send with any action text. So a press only counts when its code is one this app
handed out for that device and check, hasn't expired, and that button hasn't been used yet.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

PREFIX = "Z2MT_"
# Verb -> button title. Titles are English, like the alert text itself.
VERBS = {"MUTE24": "Mute 24 h", "PING": "Ping", "BATTERY": "Battery replaced"}
# Alerts that may offer "Battery replaced".
BATTERY_KINDS = ("battery_low", "battery_soon")
# A button keeps working this long after its alert was sent.
VALID_SECONDS = 7 * 86400
# Codes kept at most; the oldest go first.
MAX_CODES = 300
ACTION_RE = re.compile(r"Z2MT_(MUTE24|PING|BATTERY)\|(0x[0-9a-f]{16})\|([a-z_]{2,30})\|([0-9a-f]{12})")


def action_id(verb: str, ieee: str, kind: str, code: str) -> str:
    return f"{PREFIX}{verb}|{ieee}|{kind}|{code}"


def parse(action: Any) -> tuple[str, str, str, str] | None:
    """(verb, ieee, kind, code) from an action id, or None if it isn't one of ours or is malformed."""
    if not isinstance(action, str) or len(action) > 100:
        return None
    match = ACTION_RE.fullmatch(action)
    if match is None:
        return None
    verb, ieee, kind, code = match.groups()
    return verb, ieee, kind, code


def issue(store: dict, ieee: str, kind: str, verbs: list[str], now: float) -> list[dict]:
    """Hand out a code for one alert's buttons and return the Companion app actions list.

    store is the persisted {code: {...}} map; the caller holds whatever lock guards it.
    """
    verbs = [verb for verb in verbs if verb in VERBS]
    if not verbs:
        return []
    prune(store, now)
    code = secrets.token_hex(6)
    store[code] = {"ieee": ieee, "kind": kind, "verbs": verbs, "until": now + VALID_SECONDS, "used": []}
    while len(store) > MAX_CODES:
        oldest = min(store, key=lambda c: store[c].get("until") or 0)
        store.pop(oldest)
    return [{"action": action_id(verb, ieee, kind, code), "title": VERBS[verb]} for verb in verbs]


def claim(store: dict, action: Any, now: float) -> tuple[str, str, str] | None:
    """(verb, ieee, kind) when the press is valid, marking that button used; None otherwise."""
    parsed = parse(action)
    if parsed is None:
        return None
    verb, ieee, kind, code = parsed
    entry = store.get(code)
    if not isinstance(entry, dict):
        return None
    if entry.get("ieee") != ieee or entry.get("kind") != kind or verb not in (entry.get("verbs") or []):
        return None
    if not isinstance(entry.get("until"), (int, float)) or entry["until"] < now:
        store.pop(code, None)
        return None
    used = entry.setdefault("used", [])
    if verb in used:
        return None
    used.append(verb)
    return verb, ieee, kind


def prune(store: dict, now: float) -> None:
    for code in [c for c, e in store.items() if not isinstance(e, dict) or (e.get("until") or 0) < now]:
        store.pop(code)
