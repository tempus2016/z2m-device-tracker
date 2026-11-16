"""History of Zigbee group membership, from Zigbee2MQTT's bridge/groups.

The last list seen is kept per network in history.json ("group_state": {base topic: {group id:
{"name", "members", "since"}}}) so changes made while the app was stopped are still noticed when
it starts again. Each change goes into "group_history": {base topic: {group id: [event, ...]}},
oldest first, at most MAX_EVENTS per group. Events:

  {"at", "kind": "created", "members": n}        a group that appeared after the app first saw the network
  {"at", "kind": "removed_group", "name"}         the group is gone from Zigbee2MQTT
  {"at", "kind": "renamed", "from", "to"}
  {"at", "kind": "added" | "removed", "ieee", "name"}   a member; name as it was then

History of a removed group is dropped DELETED_DAYS after it was removed.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

MAX_EVENTS = 50
DELETED_DAYS = 90


def apply(
    state: dict[str, dict] | None,
    history: dict[str, list],
    names: dict[str, str],
    members: dict[str, list[str]],
    now: str,
    name_of: Callable[[str], str],
) -> tuple[dict[str, dict], bool]:
    """Compare one network's new group list with its stored state; add events to history in place.

    `state` is None the first time a network's groups are seen: everything is taken as it is, with no
    events, since the app can't know what happened before. Returns the new state and whether
    anything (state or history) changed."""
    first = state is None
    state = state or {}
    new_state: dict[str, dict] = {}
    changed = False

    def add(gid: str, event: dict) -> None:
        nonlocal changed
        events = history.setdefault(gid, [])
        events.append({"at": now, **event})
        del events[: max(0, len(events) - MAX_EVENTS)]
        changed = True

    for gid, ieees in members.items():
        old = state.get(gid)
        name = names.get(gid) or f"Group {gid}"
        if old is None:
            new_state[gid] = {"name": name, "members": list(ieees), "since": now}
            if not first:
                add(gid, {"kind": "created", "members": len(ieees)})
                for ieee in ieees:
                    add(gid, {"kind": "added", "ieee": ieee, "name": name_of(ieee)})
            changed = True
            continue
        new_state[gid] = {"name": name, "members": list(ieees), "since": old.get("since") or now}
        if old.get("name") and old["name"] != name:
            add(gid, {"kind": "renamed", "from": old["name"], "to": name})
        before = list(old.get("members") or [])
        for ieee in before:
            if ieee not in ieees:
                add(gid, {"kind": "removed", "ieee": ieee, "name": name_of(ieee)})
        for ieee in ieees:
            if ieee not in before:
                add(gid, {"kind": "added", "ieee": ieee, "name": name_of(ieee)})
        if new_state[gid] != old:
            changed = True
    for gid, old in state.items():
        if gid not in members:
            add(gid, {"kind": "removed_group", "name": old.get("name") or f"Group {gid}"})
            changed = True
    return new_state, changed


def prune(history: dict[str, list], current: set[str], now: datetime) -> bool:
    """Drop the history of groups removed more than DELETED_DAYS ago. True if anything went."""
    cutoff = (now - timedelta(days=DELETED_DAYS)).isoformat(timespec="seconds")
    gone = [
        gid
        for gid, events in history.items()
        if gid not in current and (not events or (events[-1].get("at") or "") < cutoff)
    ]
    for gid in gone:
        del history[gid]
    return bool(gone)


def clean(raw: Any) -> dict[str, dict[str, list]]:
    """Stored group_history as written by apply(); anything malformed is dropped."""
    out: dict[str, dict[str, list]] = {}
    if not isinstance(raw, dict):
        return out
    for bt, groups in raw.items():
        if not isinstance(groups, dict):
            continue
        out[str(bt)] = {
            str(gid): [e for e in events if isinstance(e, dict) and isinstance(e.get("at"), str) and e.get("kind")][
                -MAX_EVENTS:
            ]
            for gid, events in groups.items()
            if isinstance(events, list)
        }
    return out


def clean_state(raw: Any) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    if not isinstance(raw, dict):
        return out
    for bt, groups in raw.items():
        if not isinstance(groups, dict):
            continue
        out[str(bt)] = {
            str(gid): {
                "name": str(g.get("name") or f"Group {gid}"),
                "members": [str(m) for m in g.get("members") or [] if isinstance(m, str)],
                "since": g.get("since") if isinstance(g.get("since"), str) else None,
            }
            for gid, g in groups.items()
            if isinstance(g, dict)
        }
    return out
