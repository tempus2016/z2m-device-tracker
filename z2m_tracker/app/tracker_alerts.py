"""Alerts: pause, critical devices, quiet hours, what one check sends.

Part of Tracker (tracker.py): these methods use its lock, data and metrics.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from typing import Any

from health import (
    alert_plan,
    notify_kinds,
)
from tracker_base import (
    DIGEST_LINES,
    MAX_HELD_EVENTS,
    MAX_PAUSE_MINUTES,
    RESEND_COOLDOWN,
    THRESHOLD_KINDS,
    _clock_minutes,
    _dumps,
    _finite,
    _first_sentence,
)

_LOGGER = logging.getLogger("z2m_tracker")


class AlertsMixin:
    # ------------------------------------------------------------ alert pause

    def pause_alerts(self, minutes: Any = None, until: Any = None, at: Any = None) -> dict:
        """Hold every alert for `minutes`, until an ISO time, or until the next HH:MM local time.

        With none of them the pause lasts until resume_alerts(). Raises ValueError for bad input.
        """
        now = time.time()
        if minutes is not None:
            if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
                raise ValueError("minutes must be a number")
            if not 1 <= minutes <= MAX_PAUSE_MINUTES:
                raise ValueError(f"minutes must be from 1 to {MAX_PAUSE_MINUTES}")
            end: float | None = now + float(minutes) * 60
        elif until not in (None, ""):
            iso = self._to_utc_iso(str(until))
            if iso is None:
                raise ValueError("until must be a date and time like 2026-11-14T08:00")
            end = datetime.fromisoformat(iso).timestamp()
            if end <= now:
                raise ValueError("until must be in the future")
        elif at not in (None, ""):
            match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(at).strip())
            if not match:
                raise ValueError("at must be a time like 08:00")
            day = datetime.fromtimestamp(now, tz=self.local_tz).date()
            wall = dtime(int(match[1]), int(match[2]))
            end = datetime.combine(day, wall, tzinfo=self.local_tz).timestamp()
            if end <= now:
                end = datetime.combine(day + timedelta(days=1), wall, tzinfo=self.local_tz).timestamp()
        else:
            end = None
        with self.lock:
            since = (self.data.get("alerts_paused") or {}).get("since") if self.alerts_paused() else None
            self.data["alerts_paused"] = {
                "since": since or datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="seconds"),
                "until": datetime.fromtimestamp(end, tz=timezone.utc).isoformat(timespec="seconds") if end else None,
            }
            self._changed(structural=True)
            state = self.alert_pause_state()
        if end is None:
            _LOGGER.info("Alerts paused until resumed")
        else:
            _LOGGER.info(
                "Alerts paused until %s", datetime.fromtimestamp(end, tz=self.local_tz).strftime("%Y-%m-%d %H:%M")
            )
        return state

    def resume_alerts(self) -> bool:
        """End a pause now. Returns False when alerts weren't paused."""
        with self.lock:
            if not self.alerts_paused():
                return False
            self.data["alerts_paused"] = None
            self._changed(structural=True)
        _LOGGER.info("Alerts resumed")
        return True

    def alerts_paused(self) -> bool:
        """True while a pause is running. A pause whose time has passed is cleared here."""
        with self.lock:
            pause = self.data.get("alerts_paused")
            if not pause:
                return False
            until = pause.get("until")
            try:
                if until is None or datetime.fromisoformat(until).timestamp() > time.time():
                    return True
            except (TypeError, ValueError):
                pass
            self.data["alerts_paused"] = None
            self._changed(structural=True)
        _LOGGER.info("Alert pause ended")
        return False

    def alert_pause_state(self) -> dict | None:
        """The running pause for the UI, with how many issues will be sent when it ends."""
        with self.lock:
            if not self.alerts_paused():
                return None
            pause = self.data["alerts_paused"]
            pending = 0
            if self.data.get("alerts_initialised"):
                enabled = notify_kinds(self.options)
                sent = set(self.data.get("alerted") or [])
                pending = sum(1 for i in self.issues if i["kind"] in enabled and i["key"] not in sent)
            return {"since": pause.get("since"), "until": pause.get("until"), "pending": pending}

    def set_critical(self, ieee: str, critical: bool) -> bool:
        """Mark a device as critical (shorter offline delay, high-priority alerts) or clear the mark."""
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None:
                return False
            if critical:
                record["critical"] = True
            else:
                record.pop("critical", None)
            self._changed(structural=True)
        self.evaluate_health()
        return True

    def is_critical(self, ieee: str) -> bool:
        with self.lock:
            return bool((self.data["devices"].get(ieee) or {}).get("critical"))

    # ---------------------------------------------------------------- alerts

    def in_quiet_hours(self, now: float | None = None) -> bool:
        """True inside the quiet_start-quiet_end window (local time, may cross midnight)."""
        if not self.options.get("quiet_hours"):
            return False
        start = _clock_minutes(self.options.get("quiet_start", "23:00"))
        end = _clock_minutes(self.options.get("quiet_end", "07:00"))
        if start is None or end is None or start == end:
            return False
        local = datetime.fromtimestamp(time.time() if now is None else now, tz=self.local_tz)
        minute = local.hour * 60 + local.minute
        return start <= minute < end if start < end else minute >= start or minute < end

    def _quiet_state(self, now: float) -> dict:
        quiet = self.data.get("quiet")
        if not isinstance(quiet, dict):
            quiet = {"since": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="seconds")}
        quiet.setdefault("issues", {})
        quiet.setdefault("events", [])
        self.data["quiet"] = quiet
        return quiet

    def hold_event(self, title: str, message: str) -> bool:
        """Keep a join/leave/interview alert for the digest when it's quiet hours. False: send it now."""
        now = time.time()
        with self.lock:
            if not self.in_quiet_hours(now):
                return False
            quiet = self._quiet_state(now)
            quiet["events"] = [
                *quiet["events"],
                {
                    "time": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="seconds"),
                    "title": title,
                    "message": message,
                },
            ][-MAX_HELD_EVENTS:]
            self.dirty = True
        return True

    def plan_alerts(self, issues: list[dict], enabled: set[str]) -> dict:
        """Decide what one alert check sends and updates the record of what was sent.

        Returns {"send": [issues to alert now], "dismiss": [keys that cleared], "digest": (title, message) | None,
        "first": True on the very first check, when existing issues are only recorded}.
        During quiet hours new issues are held (critical devices excepted); the first check after quiet hours
        sends one digest instead of the held alerts.
        """
        now = time.time()
        stamp = datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="seconds")
        current = {issue["key"]: issue for issue in issues if issue["kind"] in enabled}
        with self.lock:
            kept = ("alerts_initialised", "alerted", "alert_cleared", "quiet")
            before = _dumps({key: self.data.get(key) for key in kept})
            try:
                return self._plan_alerts(current, enabled, now, stamp)
            finally:
                # This runs every minute: only a real change is worth a write.
                if _dumps({key: self.data.get(key) for key in kept}) != before:
                    self.dirty = True

    def _plan_alerts(self, current: dict[str, dict], enabled: set[str], now: float, stamp: str) -> dict:
        initialised = self.data.get("alerts_initialised")
        sent = set(self.data.get("alerted") or [])
        self.data["alerts_initialised"] = True
        if not enabled:
            # Alerts are off: nothing to hold, nothing to report later.
            self.data["quiet"] = None
        if not initialised:
            self.data["alerted"] = sorted(current)
            return {"send": [], "dismiss": [], "digest": None, "first": True}
        # Devices behind an offline router never alert on their own: the router's alert covers them.
        # Nor does a network outage that happens at this time most days (expected_outage.py); it
        # alerts as usual once it runs on past its usual window.
        new, alerted, dismiss = alert_plan(list(current.values()), enabled, sent)
        # A reading that hovers at its limit (battery 15 %, 16 %, 15 %) clears and comes back over
        # and over: once such an alert has cleared, the same one is not sent again for a day.
        cleared = {
            key: at
            for key, at in (
                self.data.get("alert_cleared") if isinstance(self.data.get("alert_cleared"), dict) else {}
            ).items()
            if _finite(at) and 0 <= now - at < RESEND_COOLDOWN
        }
        for key in dismiss:
            if key.split(":", 1)[0] in THRESHOLD_KINDS:
                cleared[key] = now
        if cleared or self.data.get("alert_cleared"):
            self.data["alert_cleared"] = cleared
        new = [issue for issue in new if issue["key"] not in cleared]
        urgent = [issue for issue in new if issue.get("critical")]
        if enabled and self.in_quiet_hours(now):
            held = self._quiet_state(now)["issues"]
            for issue in new:
                if issue.get("critical"):
                    held.pop(issue["key"], None)
                    continue
                entry = held.setdefault(issue["key"], {"first": stamp})
                entry.pop("cleared", None)
                entry.update(name=issue["name"], label=issue["label"], detail=issue["detail"])
            for key, entry in held.items():
                if key not in current and "cleared" not in entry:
                    entry["cleared"] = stamp
            self.data["alerted"] = sorted((sent & set(current)) | {issue["key"] for issue in urgent})
            return {"send": urgent, "dismiss": dismiss, "digest": None, "first": False}
        digest = None
        send = new
        if self.data.get("quiet"):
            # Quiet hours are over: one digest, and what's still open counts as alerted.
            digest = self._digest(self.data["quiet"], [i for i in new if not i.get("critical")], current)
            self.data["quiet"] = None
            send = urgent
        self.data["alerted"] = alerted
        return {"send": send, "dismiss": dismiss, "digest": digest, "first": False}

    def _digest(self, quiet: dict, still_open: list[dict], current: dict) -> tuple[str, str] | None:
        held = quiet.get("issues") or {}
        resolved = sorted(
            (entry for key, entry in held.items() if key not in current and entry.get("cleared")),
            key=lambda entry: entry["first"],
        )
        events = quiet.get("events") or []
        if not still_open and not resolved and not events:
            return None

        def clock(iso: str) -> str:
            try:
                return datetime.fromisoformat(iso).astimezone(self.local_tz).strftime("%H:%M")
            except (TypeError, ValueError):
                return "?"

        def section(heading: str, lines: list[str]) -> list[str]:
            if not lines:
                return []
            more = len(lines) - DIGEST_LINES
            return [heading, *(f"- {line}" for line in lines[:DIGEST_LINES])] + (
                [f"- and {more} more"] if more > 0 else []
            )

        open_lines = []
        for issue in still_open:
            since = held.get(issue["key"], {}).get("first")
            when = f" (since {clock(since)})" if since else ""
            open_lines.append(f"{issue['name']} - {issue['label']}{when}: {_first_sentence(issue['detail'])}")
        resolved_lines = [
            f"{entry['name']} - {entry['label']} ({clock(entry['first'])}-{clock(entry['cleared'])})"
            for entry in resolved
        ]
        event_lines = [f"{clock(e.get('time'))} {e.get('title')}: {_first_sentence(e.get('message'))}" for e in events]
        n, m = len(still_open), len(resolved)
        title = f"Zigbee overnight: {n} issue{'s' if n != 1 else ''}, {m} resolved"
        body = [
            *section("Still open:", open_lines),
            *section("Resolved:", resolved_lines),
            *section("Held alerts:", event_lines),
        ]
        return title, "\n".join(body)
