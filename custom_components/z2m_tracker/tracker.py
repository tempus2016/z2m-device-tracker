"""Client for the Z2M Device Tracker app (the app does the recording)."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, SIGNAL_UPDATED, STATUS_ACTIVE, STATUS_REMOVED

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(seconds=15)
FULL_REFRESH_EVERY = 8
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)


def _wifi_overlap(mhz: int | None, wifi: list[int]) -> str | None:
    """Same thresholds as the app's radio check: within 12 MHz overlaps, within 16 MHz is close."""
    if not mhz or not wifi:
        return None
    gap = min(abs(mhz - (2484 if w == 14 else 2407 + 5 * w)) for w in wifi)
    return "overlap" if gap <= 12 else "near" if gap <= 16 else "clear"


def _network_summary(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-network facts for the network card, trimmed from the app's snapshot."""
    radio = payload.get("radio") or {}
    wifi = [int(w) for w in radio.get("wifi") or [] if isinstance(w, int)]
    channels = {n.get("topic"): n for n in radio.get("networks") or []}
    out = []
    for topic in payload.get("instances") or []:
        bridge = (payload.get("bridges") or {}).get(topic) or {}
        net = channels.get(topic) or {}
        scan = (payload.get("networkmap") or {}).get(topic) or {}
        backup = (payload.get("backups") or {}).get(topic) or {}
        out.append(
            {
                "topic": topic,
                "state": bridge.get("state"),
                "channel": net.get("channel"),
                "wifi_channels": wifi,
                "wifi": _wifi_overlap(net.get("mhz"), wifi),
                "scanned_at": scan.get("scanned_at"),
                "scanning": bool(scan.get("running_since")),
                "single_points": [
                    {
                        "ieee": p.get("ieee"),
                        "name": p.get("name"),
                        "routers": len(p.get("routers") or []),
                        "devices": p.get("devices") or 0,
                    }
                    for p in (payload.get("single_points") or {}).get(topic) or []
                ],
                "backup": {
                    "exists": bool(backup.get("exists")),
                    "date": backup.get("date"),
                    "has_key": bool(backup.get("has_key")),
                    "error": bool(backup.get("error")),
                }
                if backup.get("found")
                else None,
            }
        )
    return out


class TrackerAppError(Exception):
    """Raised when the app cannot be reached or returns an error."""


class Z2MTracker:
    """Mirrors the app's device history and forwards edits to it."""

    def __init__(self, hass: HomeAssistant, app_url: str, repairs: bool = True) -> None:
        self.hass = hass
        self.repairs = repairs
        self.app_url = app_url.rstrip("/")
        host = self.app_url.split("://", 1)[-1].split(":", 1)[0].split("/", 1)[0]
        self.panel = f"/app/{host.replace('-', '_')}"
        self._session = async_get_clientsession(hass)
        self.data: dict[str, Any] = {"devices": {}, "events": [], "imported": None, "issues": []}
        self.base_topics: list[str] = []
        self.available = False
        self._version: str | None = None
        self._polls = 0
        self._unsub_poll: Callable[[], None] | None = None
        self._listeners: set[Callable[[], None]] = set()
        self._lock = asyncio.Lock()

    async def async_start(self) -> None:
        await self._async_poll(None)
        self._unsub_poll = async_track_time_interval(self.hass, self._async_poll, POLL_INTERVAL)

    async def async_stop(self) -> None:
        if self._unsub_poll:
            self._unsub_poll()
            self._unsub_poll = None
        self._clear_repairs()

    async def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        try:
            async with self._session.request(
                method, f"{self.app_url}{path}", json=body, timeout=REQUEST_TIMEOUT
            ) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    message = payload.get("error") if isinstance(payload, dict) else None
                    raise TrackerAppError(message or f"HTTP {response.status}")
                return payload
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise TrackerAppError(str(err) or err.__class__.__name__) from err

    async def _async_poll(self, _now: Any) -> None:
        async with self._lock:
            self._polls += 1
            try:
                version = (await self._request("GET", "/api/version")).get("version")
                if version != self._version or not self.available or self._polls % FULL_REFRESH_EVERY == 0:
                    await self._async_load()
            except TrackerAppError as err:
                if self.available:
                    _LOGGER.warning("Z2M Device Tracker app unreachable at %s: %s", self.app_url, err)
                    self.available = False
                    self._notify()

    async def _async_load(self) -> None:
        payload = await self._request("GET", "/api/devices")
        self.data = {
            "devices": {device["ieee"]: device for device in payload.get("devices") or []},
            "events": payload.get("events") or [],
            "imported": payload.get("imported"),
            "issues": payload.get("issues") or [],
            "options": payload.get("options") or {},
            "networks": _network_summary(payload),
        }
        if self.repairs:
            self._sync_repairs()
        self.base_topics = payload.get("instances") or []
        changed = payload.get("version") != self._version or not self.available
        self._version = payload.get("version")
        self.available = True
        if changed:
            self._notify()

    async def async_refresh_now(self) -> None:
        async with self._lock:
            try:
                await self._async_load()
            except TrackerAppError as err:
                _LOGGER.debug("Refresh after edit failed: %s", err)

    # ---------------------------------------------------------------- repairs

    @callback
    def _sync_repairs(self) -> None:
        """Mirror the app's alerting issues into Settings > Repairs; they clear when fixed."""
        options = self.data.get("options") or {}
        notify = set(options.get("notify_checks") or []) - set(options.get("disabled_checks") or [])
        wanted: dict[str, dict] = {}
        for issue in self.data.get("issues") or []:
            if issue.get("severity") == "warning" or issue.get("kind") in notify:
                wanted["issue_" + re.sub(r"[^a-z0-9_]+", "_", str(issue.get("key", "")).lower())] = issue
        registry = ir.async_get(self.hass)
        for domain, issue_id in list(registry.issues):
            if domain == DOMAIN and issue_id not in wanted:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        for issue_id, issue in wanted.items():
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="device_issue",
                translation_placeholders={
                    "name": str(issue.get("name") or issue.get("ieee") or ""),
                    "title": str(issue.get("label") or issue.get("title") or ""),
                    "detail": str(issue.get("detail") or ""),
                    "panel": self.panel,
                },
            )

    @callback
    def _clear_repairs(self) -> None:
        registry = ir.async_get(self.hass)
        for domain, issue_id in list(registry.issues):
            if domain == DOMAIN:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATED)
        for listener in list(self._listeners):
            listener()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)

        @callback
        def _remove() -> None:
            self._listeners.discard(listener)

        return _remove

    # ---------------------------------------------------------------- summary

    @callback
    def counts(self) -> dict[str, int]:
        active = removed = offline = 0
        for record in self.data["devices"].values():
            if record.get("type") == "Coordinator":
                continue
            if record.get("status") == STATUS_REMOVED:
                removed += 1
            else:
                active += 1
                if record.get("availability") == "offline":
                    offline += 1
        return {"active": active, "removed": removed, "offline": offline}

    @callback
    def offline_names(self) -> list[str]:
        return sorted(
            record.get("friendly_name") or record["ieee"]
            for record in self.data["devices"].values()
            if record.get("status") == STATUS_ACTIVE
            and record.get("availability") == "offline"
            and record.get("type") != "Coordinator"
        )

    @callback
    def issues(self) -> list[dict]:
        return self.data.get("issues") or []

    @callback
    def battery_alert_percent(self) -> int:
        try:
            return int((self.data.get("options") or {}).get("battery_alert_percent", 15))
        except (TypeError, ValueError):
            return 15

    @callback
    def last_event(self) -> dict | None:
        events = self.data["events"]
        return events[-1] if events else None

    # ------------------------------------------------------------------ edits

    async def async_set_note(self, ieee: str, note: str) -> None:
        await self._request("POST", "/api/note", {"ieee": ieee, "note": note})
        await self.async_refresh_now()

    async def async_forget(self, ieee: str) -> None:
        await self._request("POST", "/api/forget", {"ieee": ieee})
        await self.async_refresh_now()

    async def async_import_history(self) -> dict[str, Any]:
        result = await self._request("POST", "/api/import")
        await self.async_refresh_now()
        return result
