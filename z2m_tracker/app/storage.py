"""Stored devices: devices put away for part of the year, such as Christmas lights or summer lights.

Pure functions over the stored mark on a device record and the saved storage groups, no I/O and no clock.

A stored device keeps its history and its Home Assistant entities, but no health check runs on it, so
it raises no issue and no alert, and its outages don't count against its model or its network.

Device record
-------------
record["stored"] = {"since": ISO, "group": group id or None, "away": ISO or None}
  since  when it was put in storage
  group  the storage group that stored it, if any (only shown in the sidebar)
  away   when it was first seen offline while stored, or None while it is still online

With the storage_auto_resume option on, a stored device comes back by itself when it is seen online
again after being away. A device stored while still online must go offline first, so storing a light
before unplugging it doesn't undo itself straight away.

Storage groups (data["storage_groups"])
---------------------------------------
[{"id": str, "name": str, "devices": [ieee, ...]}]: a named set, stored or brought back in one go.
"""

from __future__ import annotations

import re
from typing import Any

MAX_GROUPS = 50
MAX_NAME = 40
MAX_MEMBERS = 500
GROUP_ID = re.compile(r"^[a-z0-9]{1,32}$")
IEEE = re.compile(r"^[0-9A-Za-z_:.-]{1,64}$")


def _iso(value: Any) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 40 else None


def clean_stored(value: Any) -> dict | None:
    """The stored mark from saved data, or None when it's missing or malformed."""
    if not isinstance(value, dict) or _iso(value.get("since")) is None:
        return None
    group = value.get("group")
    return {
        "since": value["since"],
        "group": group if isinstance(group, str) and GROUP_ID.match(group) else None,
        "away": _iso(value.get("away")),
    }


def is_stored(record: dict | None) -> bool:
    return bool(record) and isinstance(record.get("stored"), dict)


def clean_name(value: Any) -> str:
    return " ".join(str(value or "").split())[:MAX_NAME]


def clean_groups(value: Any) -> list[dict]:
    """Saved storage groups with valid ids, names and member lists; duplicates dropped."""
    out: list[dict] = []
    seen: set[str] = set()
    for group in value if isinstance(value, list) else []:
        if not isinstance(group, dict):
            continue
        gid, name = group.get("id"), clean_name(group.get("name"))
        if not isinstance(gid, str) or not GROUP_ID.match(gid) or gid in seen or not name:
            continue
        members = group.get("devices") if isinstance(group.get("devices"), list) else []
        devices = list(dict.fromkeys(m for m in members if isinstance(m, str) and IEEE.match(m)))[:MAX_MEMBERS]
        seen.add(gid)
        out.append({"id": gid, "name": name, "devices": devices})
        if len(out) >= MAX_GROUPS:
            break
    return out


def check_group(raw: Any, known: set[str], groups: list[dict]) -> tuple[dict | None, str | None]:
    """A group to save from untrusted input: ({"id" or None, "name", "devices"}, None) or (None, error).

    Members must be known devices. The name must be unique (ignoring case) among the other groups."""
    if not isinstance(raw, dict):
        return None, "Not a group"
    name = clean_name(raw.get("name"))
    if not name:
        return None, "Give the group a name"
    gid = raw.get("id")
    if gid is not None and (not isinstance(gid, str) or not any(g["id"] == gid for g in groups)):
        return None, "Unknown group"
    if gid is None and len(groups) >= MAX_GROUPS:
        return None, f"At most {MAX_GROUPS} groups"
    if any(g["name"].casefold() == name.casefold() and g["id"] != gid for g in groups):
        return None, "A group with that name already exists"
    members = raw.get("devices")
    if not isinstance(members, list):
        return None, "devices must be a list"
    devices = list(dict.fromkeys(m for m in members if isinstance(m, str) and m in known))
    if len(devices) > MAX_MEMBERS:
        return None, f"At most {MAX_MEMBERS} devices in a group"
    return {"id": gid, "name": name, "devices": devices}, None
