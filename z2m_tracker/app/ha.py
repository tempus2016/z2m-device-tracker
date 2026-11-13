"""Home Assistant access: Core API (via Supervisor) and read-only registry files."""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any

_LOGGER = logging.getLogger("z2m_tracker")

API = "http://supervisor/core/api"
IDENT_RE = re.compile(r"zigbee2mqtt_(0x[0-9a-f]{16})$")


class HomeAssistant:
    def __init__(self, config_dir: str) -> None:
        self.config_dir = config_dir
        self.token = os.environ.get("SUPERVISOR_TOKEN") or ""
        self.device_ids: dict[str, str] = {}
        self.battery: dict[str, dict[str, Any]] = {}

    @property
    def available(self) -> bool:
        return bool(self.token)

    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        if not self.token:
            return None
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{API}{path}",
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except (urllib.error.URLError, TimeoutError, ValueError) as err:
            _LOGGER.warning("Home Assistant API %s %s failed: %s", method, path, err)
            return None

    def save_app_options(self, app_options: dict) -> bool:
        """Persist options to this app's Supervisor configuration (needs hassio_api)."""
        if not self.token:
            return False
        request = urllib.request.Request(
            "http://supervisor/addons/self/options",
            data=json.dumps({"options": app_options}).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return (json.loads(response.read() or b"{}") or {}).get("result") == "ok"
        except (urllib.error.URLError, TimeoutError, ValueError) as err:
            _LOGGER.warning("Saving app options failed: %s", err)
            return False

    def battery_notes_replaced(self, device_id: str, when_iso: str) -> bool:
        result = self._request(
            "POST",
            "/services/battery_notes/set_battery_replaced",
            {"device_id": device_id, "datetime_replaced": when_iso},
        )
        return result is not None

    def _ws_call(self, messages: list[dict]) -> list:
        """Run HA WebSocket commands (area registry etc. have no REST API)."""
        import websocket  # websocket-client, installed in the Dockerfile

        if not self.token:
            raise RuntimeError("No Home Assistant access")
        ws = websocket.create_connection("ws://supervisor/core/websocket", timeout=15)
        try:
            json.loads(ws.recv())
            ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            if json.loads(ws.recv()).get("type") != "auth_ok":
                raise RuntimeError("Home Assistant rejected the app's token")
            results = []
            for index, message in enumerate(messages, 1):
                ws.send(json.dumps({"id": index, **message}))
                while True:
                    reply = json.loads(ws.recv())
                    if reply.get("id") == index:
                        break
                if not reply.get("success"):
                    raise RuntimeError((reply.get("error") or {}).get("message") or "Home Assistant returned an error")
                results.append(reply.get("result"))
            return results
        finally:
            ws.close()

    def list_areas(self) -> list[dict]:
        areas = self._ws_call([{"type": "config/area_registry/list"}])[0] or []
        return sorted(
            ({"id": a["area_id"], "name": a["name"], "floor_id": a.get("floor_id")} for a in areas),
            key=lambda a: a["name"].lower(),
        )

    def list_floors(self) -> list[dict]:
        """HA floors (2024.4+). Empty on older versions or if the registry can't be read."""
        try:
            floors = self._ws_call([{"type": "config/floor_registry/list"}])[0] or []
        except Exception:  # noqa: BLE001 - floors are optional, areas still work without them
            return []
        return [{"id": f["floor_id"], "name": f["name"], "level": f.get("level")} for f in floors]

    def set_device_area(self, device_id: str, area_id: str | None) -> None:
        self._ws_call([{"type": "config/device_registry/update", "device_id": device_id, "area_id": area_id}])

    # --------------------------------------------------------------- outbound

    def fire_event(self, event_type: str, data: dict) -> None:
        self._request("POST", f"/events/{event_type}", data)

    def persistent_notification(self, title: str, message: str, notification_id: str) -> None:
        self._request(
            "POST",
            "/services/persistent_notification/create",
            {"title": title, "message": message, "notification_id": notification_id},
        )

    def dismiss_notification(self, notification_id: str) -> None:
        self._request("POST", "/services/persistent_notification/dismiss", {"notification_id": notification_id})

    def notify(self, service: str, title: str, message: str) -> None:
        service = service.strip()
        if not service:
            return
        if "." in service:
            domain, name = service.split(".", 1)
        else:
            domain, name = "notify", service
        self._request("POST", f"/services/{domain}/{name}", {"title": title, "message": message})

    # ---------------------------------------------------------------- inbound

    def _load_storage(self, name: str) -> dict:
        try:
            with open(os.path.join(self.config_dir, ".storage", name), encoding="utf-8") as handle:
                return (json.load(handle) or {}).get("data") or {}
        except (OSError, ValueError):
            return {}

    def refresh(self) -> None:
        """Map IEEE → HA device id and collect Battery Notes info."""
        devices = self._load_storage("core.device_registry").get("devices", [])
        device_to_ieee: dict[str, str] = {}
        ieee_to_device: dict[str, str] = {}
        for device in devices:
            for identifier in device.get("identifiers") or []:
                if len(identifier) > 1:
                    match = IDENT_RE.search(str(identifier[1]))
                    if match:
                        ieee_to_device[match.group(1)] = device["id"]
                        device_to_ieee[device["id"]] = match.group(1)
        self.device_ids = ieee_to_device

        entities = self._load_storage("core.entity_registry").get("entities", [])
        wanted: dict[str, tuple[str, str]] = {}
        for entity in entities:
            if entity.get("platform") != "battery_notes":
                continue
            ieee = device_to_ieee.get(entity.get("device_id") or "")
            key = entity.get("translation_key")
            if ieee and key in ("battery_type", "battery_last_replaced"):
                wanted[entity["entity_id"]] = (ieee, key)
        battery: dict[str, dict[str, Any]] = {}
        if wanted and self.token:
            states = self._request("GET", "/states") or []
            for state in states:
                hit = wanted.get(state.get("entity_id"))
                if not hit or state.get("state") in (None, "unknown", "unavailable", ""):
                    continue
                ieee, key = hit
                battery.setdefault(ieee, {})["type" if key == "battery_type" else "last_replaced"] = state["state"]
        self.battery = battery
