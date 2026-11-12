"""Client for the Z2M Device Tracker app (the app does the recording)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .const import SIGNAL_UPDATED, STATUS_ACTIVE, STATUS_REMOVED

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(seconds=15)
FULL_REFRESH_EVERY = 8
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)


class TrackerAppError(Exception):
    """Raised when the app cannot be reached or returns an error."""


class Z2MTracker:
    """Mirrors the app's device history and forwards edits to it."""

    def __init__(self, hass: HomeAssistant, app_url: str) -> None:
        self.hass = hass
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
        }
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
