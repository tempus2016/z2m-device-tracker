"""Payload changes: notice when a device starts sending new keys, or stops sending ones it always
sent. Usually a firmware update or a changed Zigbee2MQTT converter, and an automation that used the
old key quietly stops working.

Each device keeps a small baseline in record["payload_keys"]:
  n       messages seen (a JSON object, after the ignored keys are taken out)
  keys    key -> how many of the first LEARN messages had it (LEARN for keys added later,
          1 for keys that only turn up now and then)
  absent  regular key -> messages in a row without it
  fresh   unknown key -> [messages with it, message number it first came in]
Nothing is reported while the first LEARN messages come in. After that a regular key (in at
least REGULAR of the learning messages) counts as gone after GONE_AFTER messages in a row without
it, and an unknown key counts as new once NEW_AFTER messages have it within NEW_WITHIN messages.
A key that doesn't get there is one that comes and goes: it is remembered and never reported.
"""

from __future__ import annotations

from typing import Any

# Keys that come and go by design, or that every device has.
IGNORED = frozenset({"action", "click", "update", "update_available", "last_seen", "linkquality", "device", "elapsed"})
IGNORED_PREFIXES = ("action_",)
LEARN = 30
REGULAR = 0.8
GONE_AFTER = 20
NEW_AFTER = 5
NEW_WITHIN = 50
MAX_KEYS = 80
MAX_FRESH = 20
MAX_KEY = 64
# How long a change shows on the Health tab, and how close two changes must be to count as one.
SHOW_DAYS = 30
MERGE_SECONDS = 86400


def relevant(keys: Any) -> set[str]:
    return {
        key
        for key in keys
        if isinstance(key, str) and len(key) <= MAX_KEY and key not in IGNORED and not key.startswith(IGNORED_PREFIXES)
    }


def observe(state: dict, payload_keys: Any) -> tuple[list[str], list[str]]:
    """Count one message's keys. Returns (added, removed), both empty unless something changed."""
    keys = relevant(payload_keys)
    if not keys:
        return [], []
    n = state["n"] = int(state.get("n") or 0) + 1
    known = state.setdefault("keys", {})
    if n <= LEARN:
        for key in keys:
            if key in known or len(known) < MAX_KEYS:
                known[key] = known.get(key, 0) + 1
        return [], []
    absent = state.setdefault("absent", {})
    removed = []
    for key in [k for k, hits in known.items() if hits >= REGULAR * LEARN]:
        if key in keys:
            absent.pop(key, None)
            continue
        absent[key] = absent.get(key, 0) + 1
        if absent[key] >= GONE_AFTER:
            removed.append(key)
            del known[key]
            del absent[key]
    fresh = state.setdefault("fresh", {})
    added = []
    for key in sorted(keys - set(known)):
        hits, first = fresh.get(key) or [0, n]
        hits += 1
        if hits >= NEW_AFTER:
            added.append(key)
            fresh.pop(key, None)
            if len(known) < MAX_KEYS:
                known[key] = LEARN
        elif key in fresh or len(fresh) < MAX_FRESH:
            fresh[key] = [hits, first]
    for key, (_, first) in list(fresh.items()):
        if n - first > NEW_WITHIN:
            # Seen now and then, never steadily: a key that comes and goes.
            del fresh[key]
            if len(known) < MAX_KEYS:
                known[key] = 1
    return added, sorted(removed)


def clean(state: Any) -> dict:
    """Keep a stored baseline only when it is well formed; anything else starts learning again."""
    if not isinstance(state, dict):
        return {}

    def counts(value: Any) -> dict:
        if not isinstance(value, dict):
            return {}
        return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)}

    out: dict[str, Any] = {"n": state["n"] if isinstance(state.get("n"), int) else 0}
    out["keys"] = dict(list(counts(state.get("keys")).items())[:MAX_KEYS])
    if state.get("absent"):
        out["absent"] = counts(state["absent"])
    fresh = state.get("fresh")
    if isinstance(fresh, dict):
        out["fresh"] = {
            k: [int(v[0]), int(v[1])]
            for k, v in list(fresh.items())[:MAX_FRESH]
            if isinstance(k, str) and isinstance(v, list) and len(v) == 2 and all(isinstance(x, int) for x in v)
        }
    return out


def merge(change: dict | None, added: list[str], removed: list[str]) -> dict:
    """Fold a new change into the last one: a key that came back cancels out."""
    old_added = set((change or {}).get("added") or [])
    old_removed = set((change or {}).get("removed") or [])
    new_added = (old_added - set(removed)) | (set(added) - old_removed)
    new_removed = (old_removed - set(added)) | (set(removed) - old_added)
    return {"added": sorted(new_added), "removed": sorted(new_removed)}


def describe(added: list[str], removed: list[str]) -> str:
    parts = []
    if added:
        parts.append("New: " + ", ".join(added))
    if removed:
        parts.append("Gone: " + ", ".join(removed))
    return ". ".join(parts)
