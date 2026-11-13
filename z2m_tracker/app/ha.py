"""Home Assistant access: Core API and WebSocket (via Supervisor), registry files as a fallback."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from typing import Any, Callable

_LOGGER = logging.getLogger("z2m_tracker")

API = "http://supervisor/core/api"
WEBSOCKET = "ws://supervisor/core/websocket"
# Registry changes that can alter a device's link, name, area or Battery Notes entities.
REGISTRY_EVENTS = ("device_registry_updated", "area_registry_updated", "entity_registry_updated")
IDENT_RE = re.compile(r"zigbee2mqtt_(0x[0-9a-f]{16})$")


class HomeAssistant:
    def __init__(self, config_dir: str) -> None:
        self.config_dir = config_dir
        self.token = os.environ.get("SUPERVISOR_TOKEN") or ""
        self.device_ids: dict[str, str] = {}
        self.areas: dict[str, str | None] = {}
        self.names: dict[str, str | None] = {}
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

    def _ws_connect(self, timeout: float = 15):
        import websocket  # websocket-client, installed in the Dockerfile

        if not self.token:
            raise RuntimeError("No Home Assistant access")
        ws = websocket.create_connection(WEBSOCKET, timeout=timeout)
        try:
            json.loads(ws.recv())
            ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            if json.loads(ws.recv()).get("type") != "auth_ok":
                raise RuntimeError("Home Assistant rejected the app's token")
        except Exception:
            ws.close()
            raise
        return ws

    def _ws_call(self, messages: list[dict]) -> list:
        """Run HA WebSocket commands (area registry etc. have no REST API)."""
        ws = self._ws_connect()
        try:
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

    def find_device(self, ieee: str) -> str | None:
        """Look one device up live; the registry file on disk can lag behind HA."""
        devices = self._ws_call([{"type": "config/device_registry/list"}])[0] or []
        for device in devices:
            for identifier in device.get("identifiers") or []:
                if len(identifier) > 1:
                    match = IDENT_RE.search(str(identifier[1]))
                    if match and match.group(1) == ieee:
                        return device["id"]
        return None

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

    def notify(self, service: str, title: str, message: str, data: dict | None = None) -> None:
        service = service.strip()
        if not service:
            return
        if "." in service:
            domain, name = service.split(".", 1)
        else:
            domain, name = "notify", service
        payload: dict = {"title": title, "message": message}
        if data:
            payload["data"] = data
        self._request("POST", f"/services/{domain}/{name}", payload)

    def logbook(self, device_id: str, name: str, message: str) -> None:
        """Add a logbook entry tied to one of the device's entities, so it shows on the device page."""
        entities = [
            e
            for e in self._load_storage("core.entity_registry").get("entities", [])
            if e.get("device_id") == device_id and not e.get("disabled_by")
        ]
        if not entities:
            return
        entities.sort(key=lambda e: (not str(e.get("entity_id", "")).startswith("update."), e.get("entity_id", "")))
        self._request(
            "POST",
            "/services/logbook/log",
            {"name": name, "message": message, "entity_id": entities[0]["entity_id"], "domain": "z2m_tracker"},
        )

    # ---------------------------------------------------------------- inbound

    def _load_storage(self, name: str) -> dict:
        try:
            with open(os.path.join(self.config_dir, ".storage", name), encoding="utf-8") as handle:
                return (json.load(handle) or {}).get("data") or {}
        except (OSError, ValueError):
            return {}

    def _registries(self) -> tuple[list, list, list]:
        """Device, area and entity registries. Asked live, because HA writes the files
        to disk a few seconds after a change; the files are the fallback."""
        if self.token:
            try:
                devices, areas, entities = self._ws_call(
                    [
                        {"type": "config/device_registry/list"},
                        {"type": "config/area_registry/list"},
                        {"type": "config/entity_registry/list"},
                    ]
                )
                return devices or [], areas or [], entities or []
            except Exception as err:  # noqa: BLE001 - fall back to the files
                _LOGGER.warning("Reading HA registries over the WebSocket failed, using the files: %s", err)
        return (
            self._load_storage("core.device_registry").get("devices", []),
            self._load_storage("core.area_registry").get("areas", []),
            self._load_storage("core.entity_registry").get("entities", []),
        )

    def refresh(self) -> None:
        """Map IEEE → HA device id, name and area, and collect Battery Notes info."""
        devices, area_list, entities = self._registries()
        area_names = {area.get("area_id"): area.get("name") for area in area_list}
        device_to_ieee: dict[str, str] = {}
        ieee_to_device: dict[str, str] = {}
        areas: dict[str, str | None] = {}
        names: dict[str, str | None] = {}
        for device in devices:
            for identifier in device.get("identifiers") or []:
                if len(identifier) > 1:
                    match = IDENT_RE.search(str(identifier[1]))
                    if match:
                        ieee = match.group(1)
                        ieee_to_device[ieee] = device["id"]
                        device_to_ieee[device["id"]] = ieee
                        areas[ieee] = area_names.get(device.get("area_id"))
                        name = device.get("name_by_user") or device.get("name")
                        names[ieee] = str(name) if name and str(name).lower() != ieee else None
        self.device_ids = ieee_to_device
        self.areas = areas
        self.names = names

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

    def watch(self, on_change: Callable[[], None], stop_event: threading.Event) -> None:
        """Call on_change whenever HA's device, area or entity registry changes.
        Runs until stop_event is set, reconnecting after a dropped connection."""
        import websocket  # websocket-client, installed in the Dockerfile

        while not stop_event.is_set():
            ws = None
            try:
                ws = self._ws_connect()
                for index, event_type in enumerate(REGISTRY_EVENTS, 1):
                    ws.send(json.dumps({"id": index, "type": "subscribe_events", "event_type": event_type}))
                ws.settimeout(60)
                _LOGGER.info("Watching Home Assistant's device registry for changes")
                on_change()  # catch anything missed while disconnected
                while not stop_event.is_set():
                    try:
                        message = json.loads(ws.recv())
                    except websocket.WebSocketTimeoutException:
                        continue
                    if message.get("type") == "event":
                        on_change()
            except Exception as err:  # noqa: BLE001 - reconnect below
                _LOGGER.warning("Home Assistant registry watch lost, retrying in 30 s: %s", err)
            finally:
                if ws is not None:
                    ws.close()
            stop_event.wait(30)
