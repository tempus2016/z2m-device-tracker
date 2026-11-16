"""Copies of each network's coordinator_backup.json, kept in the app's data folder.

Zigbee2MQTT rewrites coordinator_backup.json in place, so a bad backup (a new network key after a
re-pair, devices missing after an adapter swap) replaces the last good one. This keeps the last
KEEP copies per network, one each time the backup changes in a way that matters, plus one a week
when nothing changed so a fresh copy is always there.

The network key is never logged, stored in the index or sent to the sidebar: only a short hash of
it is kept to tell whether it changed. The copies themselves hold the key (they are the backup)
and are only sent when someone clicks Download.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any

_LOGGER = logging.getLogger(__name__)

KEEP = 10
# An unchanged backup is still copied once this much newer than the last copy.
REFRESH = 7 * 86400
INDEX = "index.json"
ID_RE = re.compile(r"^\d{8}T\d{6}Z(-\d+)?$")


def _ieee(value: Any) -> str | None:
    text = str(value or "").strip().lower().removeprefix("0x")
    return f"0x{text}" if re.fullmatch(r"[0-9a-f]{16}", text) else None


def _hex(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return f"{value:04x}"
    return str(value).strip().lower().removeprefix("0x") or None


def _ts(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except ValueError:
        return None


def fingerprint(backup: dict) -> dict:
    """What matters about a backup for telling copies apart. Holds no secret."""
    key = (backup.get("network_key") or {}).get("key")
    devices = {
        ieee for d in backup.get("devices") or [] if isinstance(d, dict) and (ieee := _ieee(d.get("ieee_address")))
    }
    return {
        "devices": sorted(devices),
        "key": hashlib.sha256(str(key).lower().encode()).hexdigest()[:16] if key else None,
        "pan_id": _hex(backup.get("pan_id")),
        "ext_pan": _hex(backup.get("extended_pan_id")),
        "channel": backup.get("channel"),
    }


def changes(before: dict | None, after: dict, names: dict[str, str] | None = None) -> dict | None:
    """What changed from one copy's fingerprint to the next; None for the first copy."""
    if before is None:
        return None
    names = names or {}
    old, new = set(before.get("devices") or []), set(after.get("devices") or [])
    return {
        "added": [{"ieee": i, "name": names.get(i)} for i in sorted(new - old)],
        "removed": [{"ieee": i, "name": names.get(i)} for i in sorted(old - new)],
        "key_changed": before.get("key") != after.get("key"),
        "pan_changed": before.get("pan_id") != after.get("pan_id") or before.get("ext_pan") != after.get("ext_pan"),
        "channel_changed": before.get("channel") != after.get("channel"),
    }


def _same(a: dict, b: dict) -> bool:
    return all(a.get(k) == b.get(k) for k in ("devices", "key", "pan_id", "ext_pan", "channel"))


def _clean_entry(raw: Any) -> dict | None:
    if not isinstance(raw, dict) or not ID_RE.match(str(raw.get("id") or "")):
        return None
    return raw


class BackupHistory:
    """The kept copies and their index (folder/index.json: base topic -> entries, oldest first)."""

    def __init__(self, folder: str) -> None:
        self.folder = folder
        self.lock = threading.Lock()
        self.index: dict[str, list[dict]] | None = None

    def _load(self) -> dict[str, list[dict]]:
        if self.index is None:
            self.index = {}
            try:
                with open(os.path.join(self.folder, INDEX), encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    for bt, entries in data.items():
                        if isinstance(entries, list):
                            self.index[bt] = [e for e in map(_clean_entry, entries) if e]
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as err:
                _LOGGER.warning("Coordinator backup copies: index unreadable (%s), starting a new one", err)
        return self.index

    def _save(self) -> None:
        path = os.path.join(self.folder, INDEX)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.index, handle, separators=(",", ":"))
        os.replace(tmp, path)

    def _file(self, bt: str, entry_id: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", bt)
        return os.path.join(self.folder, f"{safe}-{entry_id}.json")

    def consider(
        self, bt: str, raw: bytes, backup: dict, date: str | None, now: float, names: dict[str, str] | None = None
    ) -> bool:
        """Keep a copy of this backup if it differs from the last copy, or that one is a week older. True if kept."""
        fp = fingerprint(backup)
        with self.lock:
            entries = self._load().setdefault(bt, [])
            last = entries[-1] if entries else None
            if last is not None and _same(last, fp):
                if last.get("date") == date:
                    return False
                newer = (_ts(date) or now) - (_ts(last.get("date")) or 0)
                if newer < REFRESH:
                    return False
            stamp = datetime.fromtimestamp(now, tz=timezone.utc)
            entry_id = stamp.strftime("%Y%m%dT%H%M%SZ")
            taken = {e["id"] for e in entries}
            n = 1
            while entry_id in taken:
                entry_id = f"{stamp.strftime('%Y%m%dT%H%M%SZ')}-{n}"
                n += 1
            try:
                os.makedirs(self.folder, exist_ok=True)
                path = self._file(bt, entry_id)
                with open(f"{path}.tmp", "wb") as handle:
                    handle.write(raw)
                os.replace(f"{path}.tmp", path)
            except OSError as err:
                _LOGGER.warning("Couldn't keep a copy of the coordinator backup for %s: %s", bt, err)
                return False
            entries.append(
                {
                    "id": entry_id,
                    "saved": stamp.isoformat(timespec="seconds"),
                    "date": date,
                    **fp,
                    "change": changes(last, fp, names),
                }
            )
            for old in entries[: max(0, len(entries) - KEEP)]:
                try:
                    os.remove(self._file(bt, old["id"]))
                except OSError:
                    pass
            del entries[: max(0, len(entries) - KEEP)]
            try:
                self._save()
            except OSError as err:
                _LOGGER.warning("Couldn't save the coordinator backup index: %s", err)
            return True

    def rows(self, bt: str, names: dict[str, str] | None = None) -> list[dict]:
        """Newest first, without the device lists or key hash: date, device count and the change before it."""
        names = names or {}
        with self.lock:
            entries = list(self._load().get(bt) or [])
        out = []
        for e in reversed(entries):
            change = e.get("change")
            if change:
                change = {
                    **change,
                    "added": [{**d, "name": names.get(d["ieee"]) or d.get("name")} for d in change.get("added") or []],
                    "removed": [
                        {**d, "name": names.get(d["ieee"]) or d.get("name")} for d in change.get("removed") or []
                    ],
                }
            out.append(
                {
                    "id": e["id"],
                    "saved": e.get("saved"),
                    "date": e.get("date"),
                    "devices": len(e.get("devices") or []),
                    "change": change,
                }
            )
        return out

    def path(self, bt: str, entry_id: str) -> str | None:
        """The stored copy's file, only for an id the index lists for that network."""
        with self.lock:
            if not any(e["id"] == entry_id for e in self._load().get(bt) or []):
                return None
        path = self._file(bt, entry_id)
        return path if os.path.isfile(path) else None
