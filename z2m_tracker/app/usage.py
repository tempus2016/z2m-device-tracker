"""When to look up what uses a device in Home Assistant (automations, scripts, scenes).

The lookup itself is HomeAssistant.device_usage; the cache lives in the Tracker
(ha_usage). This decides when to run it: after a registry refresh, when a new
issue opens, and again after a failure (60 s, 5 min, 15 min, then only the
normal triggers).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

_LOGGER = logging.getLogger("z2m_tracker")

# Seconds to wait before retrying after the 1st, 2nd and 3rd failure in a row.
# After that, only a registry refresh or a new issue starts a lookup.
RETRY_DELAYS = (60, 300, 900)


class UsageRefresher:
    def __init__(self, tracker: Any, home_assistant: Any, clock: Callable[[], float] = time.time) -> None:
        self.tracker = tracker
        self.home_assistant = home_assistant
        self.clock = clock
        self.lock = threading.Lock()
        # Set when a lookup has been handed to a thread and not finished yet.
        self.busy = False
        self.full_due = False
        self.pending: set[str] = set()
        self.failures = 0
        self.retry_at: float | None = None

    def request_full(self) -> None:
        """Look every wanted device up again soon (after a registry refresh)."""
        self.full_due = True

    def refresh(self, ieees: list[str] | None = None) -> bool:
        """Run a lookup now, blocking. With ieees, only those that are stale; without,
        every device tracker.usage_wanted lists, forgetting the rest.
        False when Home Assistant couldn't be asked (a retry is then scheduled)."""
        if not self.home_assistant.available:
            return False
        tracker = self.tracker
        with self.lock:
            wanted = None if ieees is not None else tracker.usage_wanted()
            targets = tracker.usage_stale(ieees) if ieees is not None else wanted
            with tracker.lock:
                ids = {ieee: tracker.ha_device_ids[ieee] for ieee in targets if ieee in tracker.ha_device_ids}
            found: dict[str, dict] = {}
            if ids:
                try:
                    usage = self.home_assistant.device_usage(list(ids.values()))
                except Exception as err:  # noqa: BLE001 - retried later, see _failed
                    self._failed(len(ids), err)
                    return False
                found = {ieee: usage[device_id] for ieee, device_id in ids.items() if device_id in usage}
                _LOGGER.info("Found what uses %d devices in Home Assistant", len(found))
                self.failures = 0
                self.retry_at = None
            tracker.set_ha_usage(found, set(wanted) if wanted is not None else None)
            return True

    def _failed(self, count: int, err: Exception) -> None:
        self.failures += 1
        if self.failures <= len(RETRY_DELAYS):
            delay = RETRY_DELAYS[self.failures - 1]
            self.retry_at = self.clock() + delay
            _LOGGER.warning(
                "Looking up what uses %d devices in Home Assistant failed, retrying in %d s: %s", count, delay, err
            )
        else:
            self.retry_at = None
            _LOGGER.warning(
                "Looking up what uses %d devices in Home Assistant failed again, "
                "trying at the next registry refresh or new issue: %s",
                count,
                err,
            )

    def due(self) -> tuple[list[str] | None] | None:
        """What the scheduler should run now: (None,) for every wanted device,
        (ieees,) for just those, or None for nothing. Nothing that is due is dropped
        while a lookup is running; it waits for the next call."""
        self.pending.update(self.tracker.usage_due())
        if self.busy or self.lock.locked() or not self.home_assistant.available:
            return None
        if self.full_due or (self.retry_at is not None and self.clock() >= self.retry_at):
            self.full_due = False
            self.retry_at = None
            self.pending.clear()
            self.busy = True
            return (None,)
        if self.pending:
            ieees = sorted(self.pending)
            self.pending.clear()
            self.busy = True
            return (ieees,)
        return None

    def start_due(self) -> bool:
        """Start what due() returns on its own thread. True when a lookup was started."""
        job = self.due()
        if job is None:
            return False
        threading.Thread(target=self._run, args=job, name="ha-usage", daemon=True).start()
        return True

    def _run(self, ieees: list[str] | None) -> None:
        try:
            self.refresh(ieees)
        except Exception:  # noqa: BLE001 - keep the thread quiet, the next trigger tries again
            _LOGGER.exception("Looking up what uses devices in Home Assistant failed")
        finally:
            self.busy = False
