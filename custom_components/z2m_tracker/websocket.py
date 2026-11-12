"""WebSocket API used by the Z2M Tracker card."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .tracker import TrackerAppError


def _tracker(hass: HomeAssistant):
    return hass.data.get(DOMAIN)


@callback
def async_register_websocket_commands(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_devices)
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_set_note)
    websocket_api.async_register_command(hass, ws_forget)


@websocket_api.websocket_command({vol.Required("type"): "z2m_tracker/devices"})
@callback
def ws_devices(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    tracker = _tracker(hass)
    if tracker is None:
        connection.send_error(msg["id"], "not_loaded", "Z2M Device Tracker is not loaded")
        return
    if not tracker.available and not tracker.data["devices"]:
        connection.send_error(msg["id"], "unavailable", "The Z2M Device Tracker app is not reachable")
        return
    connection.send_result(
        msg["id"],
        {
            "devices": list(tracker.data["devices"].values()),
            "events": tracker.data["events"],
            "instances": tracker.base_topics,
            "imported": tracker.data.get("imported"),
            "panel": tracker.panel,
        },
    )


@websocket_api.websocket_command({vol.Required("type"): "z2m_tracker/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    tracker = _tracker(hass)
    if tracker is None:
        connection.send_error(msg["id"], "not_loaded", "Z2M Device Tracker is not loaded")
        return

    @callback
    def _forward() -> None:
        connection.send_message(websocket_api.event_message(msg["id"], {"changed": True}))

    connection.subscriptions[msg["id"]] = tracker.async_add_listener(_forward)
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "z2m_tracker/set_note",
        vol.Required("ieee"): str,
        vol.Required("note"): vol.All(str, vol.Length(max=2000)),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_set_note(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    tracker = _tracker(hass)
    if tracker is None:
        connection.send_error(msg["id"], "not_loaded", "Z2M Device Tracker is not loaded")
        return
    try:
        await tracker.async_set_note(msg["ieee"], msg["note"])
    except TrackerAppError as err:
        connection.send_error(msg["id"], "app_error", str(err))
        return
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command({vol.Required("type"): "z2m_tracker/forget", vol.Required("ieee"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_forget(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    tracker = _tracker(hass)
    if tracker is None:
        connection.send_error(msg["id"], "not_loaded", "Z2M Device Tracker is not loaded")
        return
    try:
        await tracker.async_forget(msg["ieee"])
    except TrackerAppError as err:
        connection.send_error(msg["id"], "app_error", str(err))
        return
    connection.send_result(msg["id"], {"ok": True})
