"""Client for the Z2M Device Tracker app (the app does the recording)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
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
# Requests the app answers only after Zigbee2MQTT replies (reconfigure waits up to 40 s).
SLOW_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)
# Sidebar translation keys the cards reuse, and how long a fetched language is kept.
CARD_STRING_PREFIXES = ("card.", "issue.", "unit.", "reading.", "net.")
STRINGS_TTL = 3600


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
        # Repairs issue ids fixed from HA, hidden until the deadline (monotonic) or until the app clears them.
        self._snoozed: dict[str, float] = {}
        # Card translations per requested language: (monotonic time fetched, result).
        self._strings: dict[str, tuple[float, dict[str, Any]]] = {}

    async def async_start(self) -> None:
        await self._async_poll(None)
        self._unsub_poll = async_track_time_interval(self.hass, self._async_poll, POLL_INTERVAL)

    async def async_stop(self) -> None:
        if self._unsub_poll:
            self._unsub_poll()
            self._unsub_poll = None
        self._clear_repairs()

    async def _request(
        self, method: str, path: str, body: dict | None = None, timeout: aiohttp.ClientTimeout = REQUEST_TIMEOUT
    ) -> Any:
        try:
            async with self._session.request(method, f"{self.app_url}{path}", json=body, timeout=timeout) as response:
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
            "battery_shopping": payload.get("battery_shopping") or [],
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
        # The app's master alerts switch covers Repairs too.
        if options.get("alerts_enabled") is False:
            self._snoozed.clear()
            self._clear_repairs()
            return
        notify = set(options.get("notify_checks") or []) - set(options.get("disabled_checks") or [])
        wanted: dict[str, dict] = {}
        issues = self.data.get("issues") or []
        keys = {str(issue.get("key", "")) for issue in issues}
        for issue in issues:
            # A device behind an offline router is covered by the router's repair, which lists it.
            if issue.get("behind") and f"offline:{issue['behind']}" in keys:
                continue
            if issue.get("severity") == "warning" or issue.get("kind") in notify:
                wanted["issue_" + re.sub(r"[^a-z0-9_]+", "_", str(issue.get("key", "")).lower())] = issue
        self._prune_snoozed(wanted)
        registry = ir.async_get(self.hass)
        for domain, issue_id in list(registry.issues):
            if domain == DOMAIN and issue_id not in wanted:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        for issue_id, issue in wanted.items():
            device = str(issue.get("ieee") or "")
            # Network and group issues have no single device to fix or mute.
            network = device.startswith(("network:", "group:"))
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                data={"ieee": device, "kind": str(issue.get("kind") or "")},
                is_fixable=not network,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="network_issue" if network else "device_issue",
                translation_placeholders={
                    "name": str(issue.get("name") or issue.get("ieee") or ""),
                    "title": str(issue.get("label") or issue.get("title") or ""),
                    "detail": str(issue.get("detail") or ""),
                    "panel": self.panel,
                },
            )

    @callback
    def snooze_repair(self, issue_id: str, seconds: float) -> None:
        """Keep a fixed issue out of Repairs while the fix takes effect (e.g. a new battery reports)."""
        self._snoozed[issue_id] = time.monotonic() + seconds

    @callback
    def _prune_snoozed(self, wanted: dict[str, dict]) -> None:
        """Drop snoozes that ran out or whose problem cleared, and leave snoozed issues out of `wanted`."""
        now = time.monotonic()
        for issue_id, until in list(self._snoozed.items()):
            if issue_id not in wanted or until <= now:
                del self._snoozed[issue_id]
            else:
                del wanted[issue_id]

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

    async def async_card_strings(self, language: str) -> dict[str, Any]:
        """The app's translations the cards use, in the closest language the app has.

        Cached for an hour per requested language. An older app without a language,
        or without the card keys, leaves the cards on their built-in English.
        """
        cached = self._strings.get(language)
        if cached and time.monotonic() - cached[0] < STRINGS_TTL:
            return cached[1]
        index = await self._request("GET", "/i18n/index.json")
        codes = [str(lang.get("code")) for lang in (index or {}).get("languages") or [] if isinstance(lang, dict)]
        low = language.lower()
        base = low.split("-")[0]
        code = (
            next((c for c in codes if c.lower() == low), None)
            or next((c for c in codes if c.lower() == base), None)
            or next((c for c in codes if c.split("-")[0].lower() == base), None)
        )
        strings: dict[str, Any] = {}
        if code and code != "en":
            payload = await self._request("GET", f"/i18n/{code}.json")
            if isinstance(payload, dict):
                strings = {k: v for k, v in payload.items() if k.startswith(CARD_STRING_PREFIXES)}
        result = {"language": code or "en", "strings": strings}
        self._strings[language] = (time.monotonic(), result)
        return result

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

    async def async_mute(self, ieee: str, kinds: list[str], days: float | None = None) -> None:
        """Mute checks for one device; days=None mutes until unmuted in the app."""
        for kind in kinds:
            await self._request("POST", "/api/mute", {"ieee": ieee, "kind": kind, "muted": True, "days": days})
        await self.async_refresh_now()

    async def async_battery_replaced(
        self, ieee: str | list[str], date: str | None = None, note: str = ""
    ) -> dict[str, Any]:
        """Record a battery change for one device or several; no date means today in the app's time zone."""
        ieees = [ieee] if isinstance(ieee, str) else ieee
        extra = {**({"date": date} if date else {}), **({"note": note} if note else {})}
        result: dict[str, Any] = {}
        try:
            for one in ieees:
                body = {"ieee": one, **extra}
                result = await self._request("POST", "/api/battery/replaced", body, timeout=SLOW_REQUEST_TIMEOUT)
        finally:
            await self.async_refresh_now()
        return result

    async def async_reconfigure(self, ieee: str) -> None:
        await self._request("POST", "/api/reconfigure", {"ieee": ieee}, timeout=SLOW_REQUEST_TIMEOUT)
        await self.async_refresh_now()

    async def async_ota_update(self, ieee: str) -> None:
        await self._request("POST", "/api/ota", {"ieee": ieee, "action": "update"})
        await self.async_refresh_now()

    # --------------------------------------------------------------- services

    async def async_set_battery_type(self, ieee: str, battery_type: str) -> None:
        """Set the battery type, keeping the quantity and install date already recorded."""
        info = (self.data["devices"].get(ieee) or {}).get("battery_info") or {}
        body = {
            "ieee": ieee,
            "info": {"type": battery_type, "quantity": info.get("quantity"), "installed": info.get("installed")},
        }
        await self._request("POST", "/api/battery", body)
        await self.async_refresh_now()

    async def async_network_scan(self, base_topic: str | None = None) -> None:
        await self._request("POST", "/api/networkmap", {"instance": base_topic} if base_topic else {})

    async def async_pause_alerts(self, minutes: int | None = None) -> None:
        """Pause every alert for some minutes; None pauses until resumed."""
        await self._request("POST", "/api/alerts/pause", {"minutes": minutes})
        await self.async_refresh_now()

    async def async_resume_alerts(self) -> None:
        await self._request("POST", "/api/alerts/resume", {})
        await self.async_refresh_now()

    # ---------------------------------------------------------- repair fixes

    async def async_acknowledge_bindings(self, ieee: str) -> None:
        await self._request("POST", "/api/bindings/ack", {"ieee": ieee})
        await self.async_refresh_now()

    async def async_restore_bindings(self, ieee: str) -> dict[str, Any]:
        # One bind request per lost binding, each waiting up to 30 s for the device.
        result = await self._request(
            "POST", "/api/bindings/restore", {"ieee": ieee}, timeout=aiohttp.ClientTimeout(total=300)
        )
        await self.async_refresh_now()
        return result
