"""Retained MQTT messages that belong to no current device.

The broker keeps the last retained message on a topic until someone publishes an empty retained
message there. After a device is renamed or removed, its old topics stay behind: an availability
topic that still says offline, a state from years ago, or Home Assistant discovery configs that
bring back ghost entities on every restart. A scan collects the retained messages under each
Zigbee2MQTT base topic and Home Assistant's discovery prefix; stale_entries() picks out the ones
that belong to no current device. Everything here is pure, so it can be tested without MQTT.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

# Topics kept from one scan. A big install has a few thousand; past this the scan stops collecting
# and says so, rather than growing without limit.
MAX_TOPICS = 20000
# Characters of each payload kept for display.
PREVIEW = 160
# Zigbee2MQTT's discovery topics: <prefix>/<component>/<ieee>/<object_id>/config, and the
# device-based form <prefix>/device/<ieee>/config. This app's own use the node id z2m_tracker.
DISCOVERY_RE = re.compile(r"^[^/]+/[^/]+/(0x[0-9a-f]{16})(?:/[^/]+)?/config$")

OLD_NAME = "old_name"
REMOVED = "removed"
DISCOVERY = "discovery"
REASONS = (OLD_NAME, REMOVED, DISCOVERY)


def _preview(payload: bytes) -> str:
    text = payload[: PREVIEW * 4].decode("utf-8", errors="replace")
    return text if len(text) <= PREVIEW else text[: PREVIEW - 1] + "…"


def collect(found: dict[str, dict], topic: str, payload: bytes, base_topics: list[str], prefix: str) -> bool:
    """Keep one retained message from a scan in `found`. Returns False once the scan is full.

    Bridge topics and empty payloads (a cleared topic) are never kept. Discovery configs keep the
    IEEE in the topic and which tracked base topics the full payload points at, since only a
    preview of the payload is stored.
    """
    if not payload:
        return True
    entry: dict[str, Any] | None = None
    for bt in base_topics:
        if topic.startswith(f"{bt}/"):
            if topic.startswith(f"{bt}/bridge/") or topic == f"{bt}/bridge":
                return True
            entry = {"base": bt}
            break
    if entry is None and topic.startswith(f"{prefix}/"):
        match = DISCOVERY_RE.match(topic)
        if not match:
            return True
        text = payload.decode("utf-8", errors="ignore")
        refs = [bt for bt in base_topics if f'"{bt}/' in text]
        entry = {"ieee": match.group(1), "refs": refs}
    if entry is None:
        return True
    if topic not in found and len(found) >= MAX_TOPICS:
        return False
    entry["payload"] = _preview(payload)
    entry["size"] = len(payload)
    found[topic] = entry
    return True


def _current_names(bt: str, devices: dict[str, dict], names: dict, groups: dict) -> set[str]:
    current = set((names.get(bt) or {}).keys())
    current.update((groups.get(bt) or {}).values())
    # Zigbee2MQTT also accepts a device's IEEE address in place of its name (for /set and /get).
    current.update(ieee for ieee, r in devices.items() if r.get("status") == "active" and r.get("instance") == bt)
    return current


def _known_names(bt: str, devices: dict[str, dict]) -> dict[str, tuple[str, str, str | None]]:
    """Names on this network that no longer belong to their device: name -> (reason, ieee, since).

    An old name of an active device is OLD_NAME, since the time it stopped being used. Every name
    of a removed device, and its IEEE address, is REMOVED, since its removal. When two devices
    once had the same name, the most recent use wins.
    """
    out: dict[str, tuple[str, str, str | None]] = {}
    for ieee, record in devices.items():
        if record.get("instance") != bt:
            continue
        active = record.get("status") == "active"
        if active:
            for entry in record.get("names") or []:
                name, until = entry.get("name"), entry.get("until")
                # Names imported from history have no end date: still old, just of unknown age.
                if not name or name == record.get("friendly_name"):
                    continue
                if name not in out or (out[name][2] or "") < (until or ""):
                    out[name] = (OLD_NAME, ieee, until)
        else:
            since = record.get("removed_at")
            for name in [*(e.get("name") for e in record.get("names") or []), record.get("friendly_name"), ieee]:
                if name and (name not in out or (out[name][2] or "") < (since or "")):
                    out[name] = (REMOVED, ieee, since)
    return out


def _display(record: dict | None) -> str | None:
    if not record:
        return None
    return record.get("friendly_name") or next(
        (e["name"] for e in reversed(record.get("names") or []) if e.get("name")), None
    )


def stale_entries(
    topics: dict[str, dict], devices: dict[str, dict], names: dict, groups: dict, base_topics: list[str]
) -> list[dict]:
    """The retained topics from a scan that belong to no current device, oldest problem first.

    Under a base topic, the longest leading part of the topic that is a known name decides: a
    current device or group name (or an active device's IEEE) means the topic is in use and is
    never listed. A name the device no longer has, or one of a removed device, is stale. Names
    the tracker has never seen are left alone: they may be something else's.

    A discovery config is stale when its IEEE is not an active device and its payload points at a
    tracked base topic, so a device on a Zigbee2MQTT network the app doesn't track is never listed.
    """
    per_net = {bt: (_current_names(bt, devices, names, groups), _known_names(bt, devices)) for bt in base_topics}
    out = []
    for topic, entry in topics.items():
        bt = entry.get("base")
        if bt is not None:
            if bt not in per_net:
                continue
            current, known = per_net[bt]
            parts = topic[len(bt) + 1 :].split("/")
            hit = None
            for k in range(len(parts), 0, -1):
                name = "/".join(parts[:k])
                if name in current:
                    break
                if name in known:
                    hit = (name, *known[name])
                    break
            if hit is None:
                continue
            name, reason, ieee, since = hit
            record = devices.get(ieee) or {}
            item = {
                "topic": topic,
                "payload": entry.get("payload", ""),
                "reason": reason,
                "ieee": ieee,
                "old_name": name,
                "name": _display(record),
                "since": since,
                "instance": bt,
            }
            if reason == OLD_NAME:
                item["availability"] = record.get("availability")
            out.append(item)
            continue
        ieee = entry.get("ieee")
        if not ieee or not entry.get("refs"):
            continue
        record = devices.get(ieee)
        if record and record.get("status") == "active":
            continue
        out.append(
            {
                "topic": topic,
                "payload": entry.get("payload", ""),
                "reason": DISCOVERY,
                "ieee": ieee,
                "old_name": None,
                "name": _display(record),
                "since": (record or {}).get("removed_at"),
                "instance": entry["refs"][0],
            }
        )
    out.sort(key=lambda x: (REASONS.index(x["reason"]), x["since"] or "", x["topic"]))
    return out


def counts(entries: list[dict]) -> dict[str, int]:
    out = dict.fromkeys(REASONS, 0)
    for entry in entries:
        out[entry["reason"]] += 1
    return out


class RetainedStore:
    """The retained messages found by the last scan. Replaced whole by each scan."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.topics: dict[str, dict] = {}
        self.scanned_at: float | None = None
        self.error: str | None = None
        self.truncated = False
        self.running = False

    def replace(self, found: dict[str, dict], truncated: bool, now: float | None = None) -> None:
        with self.lock:
            self.topics = found
            self.truncated = truncated
            self.scanned_at = now or time.time()
            self.error = None

    def failed(self, error: str) -> None:
        with self.lock:
            self.error = error

    def forget(self, topics: list[str]) -> None:
        with self.lock:
            for topic in topics:
                self.topics.pop(topic, None)

    def items(self) -> dict[str, dict]:
        with self.lock:
            return dict(self.topics)

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "scanned_at": self.scanned_at,
                "error": self.error,
                "truncated": self.truncated,
                "running": self.running,
            }
