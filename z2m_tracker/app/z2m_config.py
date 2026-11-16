"""Zigbee2MQTT's own settings (bridge/info "config"): flatten, hide secrets, and list what changed."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# Keys whose values are never stored or shown, only that they changed. Matched against each path part.
SECRET_PART = re.compile(r"(password|passwd|secret|token|network_key|pan_id|ext_pan_id|api_key|auth|cert|key$)", re.I)
# Covered elsewhere in the tracker (renames, groups) or changes on its own; not a settings change.
IGNORED = re.compile(r"^(groups\.|devices\.[^.]+\.friendly_name$|permit_join$|permit_join_timeout$|map_options\.)")
DEVICE_PATH = re.compile(r"^devices\.(0x[0-9a-fA-F]{16})\.(.+)$")
VALUE_MAX = 120
KEEP_CHANGES = 200


def _value(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text if len(text) <= VALUE_MAX else text[: VALUE_MAX - 1] + "…"


def _secret(path: str) -> bool:
    return any(SECRET_PART.search(part) for part in path.split("."))


def flatten(config: Any) -> dict[str, str]:
    """Dotted path -> JSON text of each leaf. Lists count as one value. Secrets become a short hash."""
    out: dict[str, str] = {}

    def walk(node: Any, prefix: str) -> None:
        if isinstance(node, dict):
            # An empty section is not a setting: {} -> {"0x..": {...}} must not read as "devices removed".
            for key, child in node.items():
                walk(child, f"{prefix}.{key}" if prefix else str(key))
            return
        if not prefix or IGNORED.match(prefix):
            return
        text = _value(node)
        if _secret(prefix):
            # A hash only tells two values apart; it is never shown.
            text = "#" + hashlib.sha256(text.encode()).hexdigest()[:12]
        out[prefix] = text

    if isinstance(config, dict):
        walk(config, "")
    return out


def diff(old: dict[str, str], new: dict[str, str]) -> list[dict[str, Any]]:
    """Changes from old to new, sorted by path. Secret values come back as None, with secret=True."""
    changes = []
    for path in sorted(set(old) | set(new)):
        before, after = old.get(path), new.get(path)
        if before == after:
            continue
        secret = _secret(path)
        change: dict[str, Any] = {
            "path": path,
            "old": None if secret else before,
            "new": None if secret else after,
            "secret": secret,
        }
        if before is None:
            change["change"] = "added"
        elif after is None:
            change["change"] = "removed"
        else:
            change["change"] = "changed"
        match = DEVICE_PATH.match(path)
        if match:
            change["ieee"] = match.group(1).lower()
            change["setting"] = match.group(2)
        changes.append(change)
    return changes


def _plain(text: str | None) -> str:
    """JSON text for people: strings lose their quotes."""
    try:
        value = json.loads(text) if text is not None else None
    except ValueError:
        return str(text)
    return value if isinstance(value, str) else str(text)


def describe(change: dict[str, Any]) -> str:
    """One line in English for the logbook and notifications."""
    name = change.get("setting") or change["path"]
    if change["secret"]:
        return f"{name} changed"
    if change["change"] == "added":
        return f"{name} set to {_plain(change['new'])}"
    if change["change"] == "removed":
        return f"{name} removed (was {_plain(change['old'])})"
    return f"{name}: {_plain(change['old'])} → {_plain(change['new'])}"
