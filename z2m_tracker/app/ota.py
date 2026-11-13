"""Firmware (OTA) updates: one-at-a-time queue, optional overnight window and update history."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from tracker import STATUS_ACTIVE, Tracker, _display_name, _now, friendly_z2m_error

_LOGGER = logging.getLogger("z2m_tracker")

MAX_HISTORY = 200
CHECK_TIMEOUT = 45
UPDATE_TIMEOUT = 3600
GAP_SECONDS = 20
AUTO_RETRIES = 1
WINDOW_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


def parse_window(text: str | None) -> tuple[int, int] | None:
    """'02:00-05:00' -> (120, 300) minutes after midnight, or None when off or invalid."""
    match = WINDOW_RE.match(str(text or "").replace(" ", ""))
    if not match:
        return None
    start = int(match.group(1)) * 60 + int(match.group(2))
    end = int(match.group(3)) * 60 + int(match.group(4))
    return None if start == end else (start, end)


def window_state(window: tuple[int, int] | None, now: datetime) -> tuple[bool, str | None, datetime | None]:
    """(open now, key of the current or last window, start of the next window)."""
    if window is None:
        return False, None, None
    start, end = window
    minute = now.hour * 60 + now.minute
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if start < end:
        is_open = start <= minute < end
        key_day = midnight
    else:
        is_open = minute >= start or minute < end
        key_day = midnight - timedelta(days=1) if minute < end else midnight
    next_start = midnight + timedelta(minutes=start)
    if next_start <= now:
        next_start += timedelta(days=1)
    return is_open, key_day.date().isoformat() if is_open else None, next_start


def _version(value: Any) -> Any:
    return None if value in (None, -1, "") else value


class OtaManager:
    """Runs firmware updates through Zigbee2MQTT one device at a time."""

    def __init__(
        self,
        tracker: Tracker,
        options: dict,
        logbook: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self.tracker = tracker
        self.options = options
        self.logbook = logbook
        self.lock = threading.RLock()
        self.queue: list[dict] = []
        self.current: dict | None = None
        self.status: dict[str, dict] = {}
        self.check: dict[str, Any] = {"running": False, "done": 0, "total": 0}
        self.window_done: dict[str, set[str]] = {}
        self.external: dict[str, dict] = {}
        self.last_finished = 0.0
        self.wake = threading.Event()

    # ----------------------------------------------------------------- helpers

    def _changed(self) -> None:
        with self.tracker.lock:
            self.tracker.revision += 1

    def min_battery(self) -> int:
        try:
            return max(0, min(100, int(self.options.get("ota_min_battery", 30))))
        except (TypeError, ValueError):
            return 30

    def window(self) -> tuple[int, int] | None:
        return parse_window(self.options.get("ota_window"))

    def _record(self, ieee: str) -> dict | None:
        return self.tracker.data["devices"].get(ieee)

    def _battery_block(self, record: dict) -> str | None:
        """Reason a battery device shouldn't update now, or None."""
        if not self.tracker.is_battery_device(record):
            return None
        level = record.get("battery")
        limit = self.min_battery()
        if isinstance(level, (int, float)) and level < limit:
            return f"Battery below {limit}%"
        if record.get("battery_low") is True:
            return "Battery is low"
        return None

    def _blocked(self, record: dict | None, force: bool) -> str | None:
        if record is None or record["status"] != STATUS_ACTIVE:
            return "Device is not active in Zigbee2MQTT"
        if record.get("availability") == "offline":
            return "Device is offline"
        if (record.get("update") or {}).get("state") != "available":
            return "No update available"
        return None if force else self._battery_block(record)

    def _history(self) -> list[dict]:
        history = self.tracker.data.get("ota_history")
        if not isinstance(history, list):
            history = []
            self.tracker.data["ota_history"] = history
        return history

    def _failures(self) -> dict[str, dict]:
        """Failed updates in a row per device, kept in history.json: {ieee: {"count": n, "to": version}}."""
        failures = self.tracker.data.get("ota_failures")
        if not isinstance(failures, dict):
            failures = {}
            self.tracker.data["ota_failures"] = failures
        return failures

    def _fail_count(self, record: dict) -> int:
        """Failures in a row for the version on offer now. A newer version starts again at 0."""
        entry = self._failures().get(record["ieee"])
        if not isinstance(entry, dict):
            return 0
        if entry.get("to") != _version((record.get("update") or {}).get("latest_version")):
            return 0
        count = entry.get("count")
        return count if isinstance(count, int) else 0

    def _add_history(self, entry: dict) -> None:
        with self.tracker.lock:
            history = self._history()
            history.append(entry)
            del history[: max(0, len(history) - MAX_HISTORY)]
            self.tracker._changed(structural=True)
        if self.logbook:
            device_id = self.tracker.ha_device_ids.get(entry["ieee"])
            if device_id:
                minutes = max(1, round((entry.get("duration") or 0) / 60))
                if entry["result"] == "ok":
                    old = entry.get("from_build") or entry.get("from")
                    new = entry.get("to_build") or entry.get("to")
                    message = f"firmware updated from {old} to {new} in {minutes} min"
                else:
                    message = f"firmware update failed: {entry.get('error') or 'unknown error'}"
                try:
                    self.logbook(device_id, entry.get("name") or entry["ieee"], message)
                except Exception:  # noqa: BLE001 - the logbook is optional
                    _LOGGER.exception("Writing OTA result to the logbook failed")

    # ------------------------------------------------------------------ public

    def enqueue(self, ieees: list[str], force: bool = False) -> dict:
        added, skipped = [], []
        with self.lock, self.tracker.lock:
            busy = {item["ieee"] for item in self.queue} | ({self.current["ieee"]} if self.current else set())
            for ieee in ieees:
                if ieee in busy:
                    if force:
                        for item in self.queue:
                            if item["ieee"] == ieee:
                                item["force"] = True
                    continue
                record = self._record(ieee)
                reason = self._blocked(record, force)
                if reason:
                    self.status[ieee] = {"state": "skipped", "reason": reason, "at": _now()}
                    skipped.append({"ieee": ieee, "reason": reason})
                    continue
                self.queue.append({"ieee": ieee, "force": force, "queued": _now()})
                self.status[ieee] = {"state": "queued", "at": _now()}
                added.append(ieee)
        self._changed()
        self.wake.set()
        return {"ok": True, "queued": added, "skipped": skipped}

    def enqueue_all(self, force: bool = False) -> dict:
        with self.tracker.lock:
            ieees = [
                r["ieee"]
                for r in self.tracker.data["devices"].values()
                if r["status"] == STATUS_ACTIVE and (r.get("update") or {}).get("state") == "available"
            ]
        return self.enqueue(ieees, force)

    def cancel(self, ieee: str) -> dict:
        with self.lock:
            before = len(self.queue)
            self.queue = [item for item in self.queue if item["ieee"] != ieee]
            removed = len(self.queue) != before
            if removed:
                self.status.pop(ieee, None)
        self._changed()
        return {"ok": removed, "error": None if removed else "Not in the queue"}

    def check_all(self) -> dict:
        with self.lock:
            if self.check["running"]:
                return {"ok": False, "error": "A check is already running"}
            with self.tracker.lock:
                targets = [
                    (r["ieee"], r["instance"])
                    for r in self.tracker.data["devices"].values()
                    if r["status"] == STATUS_ACTIVE
                    and r.get("type") != "Coordinator"
                    and isinstance(r.get("update"), dict)
                    and r.get("availability") != "offline"
                ]
            self.check = {"running": True, "done": 0, "total": len(targets), "started": _now(), "errors": 0}
        self._changed()

        def run() -> None:
            errors = 0
            for index, (ieee, base_topic) in enumerate(targets, 1):
                try:
                    result = self.tracker.request(
                        base_topic, "device/ota_update/check", {"id": ieee}, timeout=CHECK_TIMEOUT
                    )
                    if not result or result.get("status") != "ok":
                        errors += 1
                except Exception:  # noqa: BLE001 - keep checking the rest
                    errors += 1
                    _LOGGER.exception("OTA check failed for %s", ieee)
                with self.lock:
                    self.check["done"] = index
                    self.check["errors"] = errors
                self._changed()
            with self.lock:
                self.check["running"] = False
                self.check["finished"] = _now()
            with self.tracker.lock:
                self.tracker.data["ota_last_check"] = _now()
                self.tracker._changed(structural=True)
            _LOGGER.info("Firmware check finished: %d devices, %d without an answer", len(targets), errors)

        threading.Thread(target=run, name="ota-check", daemon=True).start()
        return {"ok": True, "total": len(targets)}

    def snapshot(self) -> dict:
        now_local = datetime.now(self.tracker.local_tz)
        window = self.window()
        is_open, _, next_start = window_state(window, now_local)
        with self.lock, self.tracker.lock:
            status = {k: dict(v) for k, v in self.status.items()}
            for item in self.queue:
                status.setdefault(item["ieee"], {"state": "queued"})
            if self.current:
                status[self.current["ieee"]] = {"state": "updating", "at": self.current["start"]}
            return {
                "queue": [item["ieee"] for item in self.queue],
                "current": self.current["ieee"] if self.current else None,
                "status": status,
                "check": dict(self.check),
                "last_check": self.tracker.data.get("ota_last_check"),
                "history": list(reversed(self._history()))[:50],
                "window": self.options.get("ota_window") or "",
                "window_valid": window is not None or not self.options.get("ota_window"),
                "window_open": is_open,
                "next_window": next_start.isoformat(timespec="minutes") if next_start else None,
                "min_battery": self.min_battery(),
                "auto_retries": AUTO_RETRIES,
                "failures": {
                    r["ieee"]: count
                    for r in self.tracker.data["devices"].values()
                    if (r.get("update") or {}).get("state") == "available" and (count := self._fail_count(r))
                },
                "battery_blocked": {
                    r["ieee"]: reason
                    for r in self.tracker.data["devices"].values()
                    if r["status"] == STATUS_ACTIVE
                    and (r.get("update") or {}).get("state") == "available"
                    and (reason := self._battery_block(r))
                },
            }

    # ------------------------------------------------------------------ worker

    def _watch_external(self) -> None:
        """Record updates started outside the tracker (e.g. from the Zigbee2MQTT frontend)."""
        now = time.time()
        finished = []
        with self.lock, self.tracker.lock:
            ours = self.current["ieee"] if self.current else None
            for record in self.tracker.data["devices"].values():
                ieee = record["ieee"]
                update = record.get("update") or {}
                if update.get("state") == "updating" and ieee != ours:
                    self.external.setdefault(
                        ieee,
                        {
                            "start": now,
                            "from": _version(update.get("installed_version")),
                            "to": _version(update.get("latest_version")),
                        },
                    )
                elif ieee in self.external:
                    seen = self.external.pop(ieee)
                    installed = _version(update.get("installed_version"))
                    ok = update.get("state") == "idle" or (installed is not None and installed != seen["from"])
                    if ok:
                        self._failures().pop(ieee, None)
                    finished.append(
                        {
                            "ieee": ieee,
                            "name": _display_name(record) or ieee,
                            "from": seen["from"],
                            "to": installed if ok and installed is not None else seen["to"],
                            "start": datetime.fromtimestamp(seen["start"], tz=timezone.utc).isoformat(
                                timespec="seconds"
                            ),
                            "duration": round(now - seen["start"]),
                            "result": "ok" if ok else "failed",
                            "error": None if ok else "Zigbee2MQTT reported the update didn't finish",
                            "source": "Zigbee2MQTT",
                        }
                    )
        for entry in finished:
            self._add_history(entry)

    def _next(self) -> dict | None:
        """Pick the next device to update, or None."""
        now_local = datetime.now(self.tracker.local_tz)
        is_open, key, _ = window_state(self.window(), now_local)
        with self.lock, self.tracker.lock:
            if any((r.get("update") or {}).get("state") == "updating" for r in self.tracker.data["devices"].values()):
                return None
            while self.queue:
                item = self.queue.pop(0)
                reason = self._blocked(self._record(item["ieee"]), item["force"])
                if reason:
                    self.status[item["ieee"]] = {"state": "skipped", "reason": reason, "at": _now()}
                    continue
                return item
            if not is_open or key is None:
                return None
            done = self.window_done.setdefault(key, set())
            for stale in [k for k in self.window_done if k != key]:
                del self.window_done[stale]
            for record in sorted(self.tracker.data["devices"].values(), key=lambda r: r["ieee"]):
                ieee = record["ieee"]
                if ieee in done or (record.get("update") or {}).get("state") != "available":
                    continue
                if self._fail_count(record) > AUTO_RETRIES:
                    continue
                done.add(ieee)
                reason = self._blocked(record, False)
                if reason:
                    self.status[ieee] = {"state": "skipped", "reason": reason, "at": _now()}
                    continue
                return {"ieee": ieee, "force": False, "queued": _now(), "scheduled": True}
        return None

    def _run(self, item: dict) -> None:
        ieee = item["ieee"]
        with self.tracker.lock:
            record = self._record(ieee)
            if record is None:
                return
            base_topic = record["instance"]
            name = _display_name(record) or ieee
            update = record.get("update") or {}
            from_v, to_v = _version(update.get("installed_version")), _version(update.get("latest_version"))
            from_build = record.get("software_build_id")
            self.tracker._add_event(
                _now(), base_topic, ieee, record.get("friendly_name"), "firmware update started", ""
            )
            record["update"] = {**update, "state": "updating", "progress": 0}
            self.tracker._changed(structural=True)
        start = time.time()
        start_iso = _now()
        with self.lock:
            self.current = {"ieee": ieee, "start": start_iso}
            self.status[ieee] = {"state": "updating", "at": start_iso}
        _LOGGER.info("Firmware update started: %s (%s -> %s)", name, from_v, to_v)
        try:
            result = self.tracker.request(base_topic, "device/ota_update/update", {"id": ieee}, timeout=UPDATE_TIMEOUT)
        except Exception as err:  # noqa: BLE001 - report as a failed update
            result = {"status": "error", "error": str(err)}
        ok = bool(result and result.get("status") == "ok")
        error = None if ok else friendly_z2m_error((result or {}).get("error") or "No response")
        builds: dict[str, Any] = {}
        with self.tracker.lock:
            record = self._record(ieee)
            if record is not None:
                data = (result or {}).get("data") or {}
                builds = {
                    side: (data.get(side) or {}).get("software_build_id") if isinstance(data.get(side), dict) else None
                    for side in ("from", "to")
                }
                failures = self._failures()
                if ok:
                    failures.pop(ieee, None)
                else:
                    # A manual update or Retry starts the count again; the overnight run adds to it.
                    count = self._fail_count(record) + 1 if item.get("scheduled") else 1
                    failures[ieee] = {"count": count, "to": to_v}
                if not ok:
                    self.tracker._add_event(
                        _now(), base_topic, ieee, record.get("friendly_name"), "firmware update failed", error
                    )
                    if (record.get("update") or {}).get("state") == "updating":
                        record["update"]["state"] = "available"
                self.tracker._changed(structural=True)
        with self.lock:
            self.current = None
            self.external.pop(ieee, None)
            if ok:
                self.status.pop(ieee, None)
            else:
                self.status[ieee] = {"state": "failed", "reason": error, "at": _now()}
            self.last_finished = time.time()
        _LOGGER.info("Firmware update %s: %s%s", "finished" if ok else "failed", name, "" if ok else f" ({error})")
        self._add_history(
            {
                "ieee": ieee,
                "name": name,
                "from": from_v,
                "to": to_v,
                "from_build": builds.get("from") or from_build,
                "to_build": builds.get("to"),
                "start": start_iso,
                "duration": round(time.time() - start),
                "result": "ok" if ok else "failed",
                "error": error,
                "source": "scheduled" if item.get("scheduled") else "manual",
            }
        )

    def loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.wake.wait(5)
            self.wake.clear()
            try:
                self._watch_external()
                if time.time() - self.last_finished < GAP_SECONDS:
                    continue
                item = self._next()
                if item:
                    self._changed()
                    self._run(item)
            except Exception:  # noqa: BLE001 - keep the worker alive
                _LOGGER.exception("OTA worker step failed")

    def start(self, stop: threading.Event) -> None:
        threading.Thread(target=self.loop, args=(stop,), name="ota-worker", daemon=True).start()
