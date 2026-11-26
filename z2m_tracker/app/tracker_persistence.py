"""Loading, saving, backup and restore of the history store.

Part of Tracker (tracker.py): these methods use its lock, data and metrics.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import firmware_notes
from group_history import clean as clean_group_history
from group_history import clean_state as clean_group_state
from mesh_score import clean as clean_mesh_scores
from storage import clean_groups
from tracker_base import (
    MAX_EVENTS,
    RESTORE_KEEPS,
    RESTORE_KEYS,
    SOFT_SAVE_SECONDS,
    STATUS_ACTIVE,
    _brand_list,
    _clean_record,
    _dumps,
    _ieee,
    _new_record,
    _now,
    _stamp_offline_since,
    _text,
    _write_atomic,
)
from upgrade_impact import (
    clean_upgrades,
)

_LOGGER = logging.getLogger("z2m_tracker")


def _read_history(path: str) -> tuple[dict | None, str | None]:
    """A stored history file: (its content, None), or (None, what is wrong with it). A file that
    isn't there is (None, None). The content must be an object whose device list is one too: valid
    JSON of another shape (a list, null) is as unusable as half a file."""
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError) as err:
        return None, str(err)
    if not isinstance(stored, dict) or not isinstance(stored.get("devices", {}), dict):
        return None, "not a history file"
    return stored, None


class PersistenceMixin:
    # ------------------------------------------------------------ persistence

    def load(self, migrate_from: str | None = None) -> str:
        source = "new"
        stored = None
        backup_path = f"{self.data_path}.bak"
        if os.path.exists(self.data_path) or os.path.exists(backup_path):
            stored, problem = _read_history(self.data_path)
            source = "saved"
            if stored is None:
                if problem is not None:
                    # Keep the broken file for recovery, rather than crash-looping on it.
                    broken = f"{self.data_path}.corrupt-{int(time.time())}"
                    _LOGGER.error("%s can't be read (%s); moved it to %s", self.data_path, problem, broken)
                    try:
                        os.replace(self.data_path, broken)
                    except OSError:
                        pass
                # The copy the save before last left behind: a few minutes older, but whole.
                stored, backup_problem = _read_history(backup_path)
                if stored is not None:
                    _LOGGER.warning("Loaded the history from the previous save, %s", backup_path)
                    source = "backup"
                else:
                    if backup_problem is not None:
                        _LOGGER.error("%s can't be read either (%s)", backup_path, backup_problem)
                    _LOGGER.error("Starting with an empty history")
                    source = "corrupt"
        elif migrate_from and os.path.exists(migrate_from):
            with open(migrate_from, encoding="utf-8") as handle:
                wrapper = json.load(handle)
            stored = wrapper.get("data") if isinstance(wrapper, dict) and "data" in wrapper else wrapper
            source = "migrated"
        with self.lock:
            if isinstance(stored, dict):
                events = stored.get("events")
                self.data["events"] = [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []
                self.data["imported"] = stored.get("imported")
                networkmap = stored.get("networkmap")
                self.data["networkmap"] = networkmap if isinstance(networkmap, dict) else {}
                # A scan that was running when the app stopped will never answer this run.
                for info in self.data["networkmap"].values():
                    if isinstance(info, dict):
                        info.pop("running_since", None)
                self.data["alerted"] = list(stored.get("alerted") or [])
                for ieee, record in (stored.get("devices") or {}).items():
                    if isinstance(record, dict):
                        self.data["devices"][ieee] = _clean_record(
                            {**_new_record(ieee, record.get("instance")), **record}
                        )
                for key, value in stored.items():
                    if key not in ("devices", "events", "imported", "networkmap", "alerted"):
                        self.data[key] = value
                if isinstance(self.data.get("battery_brand_list"), list):
                    self.data["battery_brand_list"] = _brand_list(self.data["battery_brand_list"])
                if "upgrades" in self.data:
                    self.data["upgrades"] = clean_upgrades(self.data["upgrades"])
                if "mesh_scores" in self.data:
                    self.data["mesh_scores"] = clean_mesh_scores(self.data["mesh_scores"])
                if "group_history" in self.data:
                    self.data["group_history"] = clean_group_history(self.data["group_history"])
                if "group_state" in self.data:
                    self.data["group_state"] = clean_group_state(self.data["group_state"])
                if "storage_groups" in self.data:
                    self.data["storage_groups"] = clean_groups(self.data["storage_groups"])
            for record in self.data["devices"].values():
                if _stamp_offline_since(record):
                    self.dirty = True
                if record["status"] == STATUS_ACTIVE and record["friendly_name"] and record["instance"]:
                    self.names.setdefault(record["instance"], {})[record["friendly_name"]] = record["ieee"]
            if source in ("migrated", "backup"):
                self.dirty = True
        return source

    def save(self, force: bool = False) -> bool:
        # One writer at a time: the scheduler, a restore and shutdown can all save at once.
        with self._save_lock:
            with self.lock:
                if not self._dirty and not force:
                    return False
                if not force and not self._hard and 0 <= time.time() - self._saved_at < SOFT_SAVE_SECONDS:
                    return False
                payload = _dumps(self.data, ensure_ascii=False, separators=(",", ":"))
                self.dirty = False
            try:
                _write_atomic(self.data_path, payload, keep_previous=True)
            except BaseException:
                # Not on disk (full or read-only): still to be saved, so the next save tries again.
                self.dirty = True
                raise
            self._saved_at = time.time()
        return True

    @property
    def dirty(self) -> bool:
        return self._dirty

    @dirty.setter
    def dirty(self, value: bool) -> None:
        """Set from outside it means "save at the next chance"; only _soft_change() can wait."""
        self._dirty = self._hard = value
        if value:
            self._snapshot = None

    def _soft_change(self) -> None:
        self._dirty = True

    def _changed(self, structural: bool) -> None:
        if structural:
            self.dirty = True
            self.revision += 1
        else:
            self._soft_change()

    def backup(self) -> bytes:
        with self.lock, self.metrics.lock:
            return _dumps(
                {
                    "format": "z2m-tracker-backup",
                    "version": 1,
                    "created": _now(),
                    "history": self.data,
                    "metrics": self.metrics.series,
                },
                ensure_ascii=False,
            ).encode("utf-8")

    def restore(self, payload: dict) -> dict:
        if not isinstance(payload, dict) or payload.get("format") != "z2m-tracker-backup":
            return {"ok": False, "error": "This isn't a Zigbee Tracker backup file"}
        history = payload.get("history") or {}
        if not isinstance(history.get("devices"), dict):
            return {"ok": False, "error": "The backup has no device list"}
        # Build and check the new state first: a broken file must not wipe what is loaded now.
        devices: dict[str, dict] = {}
        for ieee, record in history["devices"].items():
            clean = _ieee(ieee)
            if not clean or not isinstance(record, dict):
                return {"ok": False, "error": f"The backup has an invalid device entry ({_text(str(ieee), 40)})"}
            # A network this app doesn't listen to keeps its name: its devices are frozen (history
            # kept, no checks, no requests) and pick up again if the app listens to it later.
            instance = record.get("instance")
            if not (instance is None or isinstance(instance, str)):
                instance = None
            devices[clean] = _clean_record(
                {**_new_record(clean, instance), **record, "ieee": clean, "instance": instance}
            )
        restored: dict[str, Any] = {
            "devices": devices,
            "events": [e for e in history.get("events") or [] if isinstance(e, dict)][-MAX_EVENTS:],
            "imported": history.get("imported") if isinstance(history.get("imported"), (str, dict)) else None,
            "networkmap": history.get("networkmap") if isinstance(history.get("networkmap"), dict) else {},
            "alerted": [str(k) for k in history.get("alerted") or [] if isinstance(k, str)],
        }
        for key, kind in RESTORE_KEYS.items():
            if isinstance(history.get(key), kind):
                restored[key] = history[key]
        if "battery_brand_list" in restored:
            restored["battery_brand_list"] = _brand_list(restored["battery_brand_list"])
        if "upgrades" in restored:
            restored["upgrades"] = clean_upgrades(restored["upgrades"])
        if "mesh_scores" in restored:
            restored["mesh_scores"] = clean_mesh_scores(restored["mesh_scores"])
        if "group_history" in restored:
            restored["group_history"] = clean_group_history(restored["group_history"])
        if "group_state" in restored:
            restored["group_state"] = clean_group_state(restored["group_state"])
        if "firmware_notes" in restored:
            restored["firmware_notes"] = firmware_notes.clean_notes(restored["firmware_notes"])
        with self.lock:
            # A pause, held quiet-hours alerts and running escalations are about now, not about when
            # the backup was made. The published entities are kept so the next sync removes the ones
            # the backup doesn't need.
            kept = {key: self.data.get(key) for key in RESTORE_KEEPS}
            self.data = restored
            for key, value in kept.items():
                if value or key == "quiet":
                    self.data[key] = value
            self.names = {}
            for record in self.data["devices"].values():
                if record["status"] == STATUS_ACTIVE and record["friendly_name"] and record["instance"]:
                    self.names.setdefault(record["instance"], {})[record["friendly_name"]] = record["ieee"]
            self._upgrade_rev += 1
            self._changed(structural=True)
        if isinstance(payload.get("metrics"), dict):
            with self.metrics.lock:
                self.metrics.adopt(payload["metrics"])
                self.metrics.dirty = True
        self.save(force=True)
        self.metrics.save(force=True)
        return {"ok": True, "devices": len(self.data["devices"]), "events": len(self.data["events"])}
