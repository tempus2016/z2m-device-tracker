"""When a network map read from Home Assistant is worth giving to the tracker.

ZHA and Matter are read every minute, and each map the tracker gets is a routing scan in its history
(40 kept per network) and a write to scans.json. A map passes once two reads in a row show the same
links and those differ from the last map passed, or once a day so link quality stays current. That
keeps the scan history as long as Zigbee2MQTT's nightly scan does.
"""

from __future__ import annotations

import time

# The map is taken again after this long even when no link changed.
MAP_REFRESH_SECONDS = 24 * 3600


class MapGate:
    def __init__(self, refresh_seconds: float = MAP_REFRESH_SECONDS) -> None:
        self.refresh_seconds = refresh_seconds
        # The links of the last read, of the last map passed, and when that was.
        self.seen: frozenset | None = None
        self.applied: frozenset | None = None
        self.applied_at = 0.0

    def due(self, value: dict) -> bool:
        """True when this map (a Zigbee2MQTT-style {"links": [...]}) should go to the tracker now."""
        shape = frozenset(
            (link.get("sourceIeeeAddr"), link.get("targetIeeeAddr"), link.get("relationship"))
            for link in value.get("links") or []
            if isinstance(link, dict)
        )
        settled = shape == self.seen
        self.seen = shape
        if not settled:
            return False
        stale = time.monotonic() - self.applied_at >= self.refresh_seconds
        if shape == self.applied and not stale:
            return False
        self.applied = shape
        self.applied_at = time.monotonic()
        return True
