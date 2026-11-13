"""Actions (services) for automations: thin wrappers around the app's API."""

from __future__ import annotations

import re
from datetime import date as date_cls
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, STATUS_ACTIVE
from .tracker import TrackerAppError, Z2MTracker

ATTR_DEVICE_ID = "device_id"
ATTR_IEEE = "ieee"

# Z2M's MQTT discovery sets the device identifier to "zigbee2mqtt_<ieee>".
IDENT_RE = re.compile(r"zigbee2mqtt_(0x[0-9a-f]{16})$")
IEEE_RE = re.compile(r"0x[0-9a-f]{16}")

# Checks that belong to one device and can be muted for it (network-wide checks can't).
DEVICE_CHECKS = [
    "offline",
    "silent",
    "frozen",
    "battery_low",
    "battery_soon",
    "battery_drain",
    "weak_signal",
    "signal_drop",
    "unidentified",
    "update",
    "replacement",
    "flapping",
    "chatty",
    "binding_lost",
    "single_point",
    "router_hopping",
]

DEVICE_FIELDS = {
    vol.Optional(ATTR_DEVICE_ID): cv.string,
    vol.Optional(ATTR_IEEE): cv.string,
}

SCHEMAS: dict[str, vol.Schema] = {
    "battery_replaced": vol.Schema(
        {
            **DEVICE_FIELDS,
            vol.Optional("date"): cv.date,
            vol.Optional("battery_type"): vol.All(cv.string, vol.Length(max=40)),
        }
    ),
    "mute_device": vol.Schema(
        {
            **DEVICE_FIELDS,
            vol.Optional("checks"): vol.All(cv.ensure_list, [vol.In(DEVICE_CHECKS)]),
            vol.Optional("duration"): cv.positive_time_period_dict,
        }
    ),
    "run_network_scan": vol.Schema({vol.Optional("base_topic"): cv.string}),
    "reconfigure": vol.Schema(DEVICE_FIELDS),
    "ota_update": vol.Schema(DEVICE_FIELDS),
    "add_note": vol.Schema({**DEVICE_FIELDS, vol.Required("text"): vol.All(cv.string, vol.Length(min=1))}),
}

NOTE_LIMIT = 2000


def _tracker(hass: HomeAssistant) -> Z2MTracker:
    tracker: Z2MTracker | None = hass.data.get(DOMAIN)
    if tracker is None:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_loaded")
    if not tracker.available:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="app_unreachable")
    return tracker


def ieee_from_device(hass: HomeAssistant, device_id: str, tracker: Z2MTracker) -> str:
    """Map a Home Assistant device id to the Zigbee IEEE address Z2M gave it."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_device_id",
            translation_placeholders={"device": device_id},
        )
    for _domain, identifier in device.identifiers:
        match = IDENT_RE.search(str(identifier).lower())
        if match:
            return match.group(1)
    for ieee, record in tracker.data["devices"].items():
        if record.get("ha_device_id") == device_id:
            return ieee
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="not_zigbee",
        translation_placeholders={"device": device.name_by_user or device.name or device_id},
    )


def resolve_ieee(hass: HomeAssistant, call: ServiceCall, tracker: Z2MTracker, *, active: bool = False) -> str:
    """The IEEE address for a call's device_id (preferred) or raw ieee field."""
    if call.data.get(ATTR_DEVICE_ID):
        ieee = ieee_from_device(hass, call.data[ATTR_DEVICE_ID], tracker)
    elif call.data.get(ATTR_IEEE):
        ieee = str(call.data[ATTR_IEEE]).strip().lower()
        if not IEEE_RE.fullmatch(ieee):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="bad_ieee", translation_placeholders={"ieee": ieee}
            )
    else:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_device")
    record = tracker.data["devices"].get(ieee)
    if record is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="unknown_ieee", translation_placeholders={"ieee": ieee}
        )
    if active and record.get("status") != STATUS_ACTIVE:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_active",
            translation_placeholders={"name": record.get("friendly_name") or ieee},
        )
    return ieee


def _app_error(err: TrackerAppError) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN, translation_key="app_error", translation_placeholders={"error": str(err)}
    )


async def _battery_replaced(hass: HomeAssistant, call: ServiceCall) -> None:
    tracker = _tracker(hass)
    ieee = resolve_ieee(hass, call, tracker)
    when: date_cls | None = call.data.get("date")
    try:
        await tracker.async_battery_replaced(ieee, when.isoformat() if when else None)
        if call.data.get("battery_type"):
            await tracker.async_set_battery_type(ieee, call.data["battery_type"].strip())
    except TrackerAppError as err:
        raise _app_error(err) from err


async def _mute_device(hass: HomeAssistant, call: ServiceCall) -> None:
    tracker = _tracker(hass)
    ieee = resolve_ieee(hass, call, tracker)
    duration: timedelta | None = call.data.get("duration")
    days = duration.total_seconds() / 86400 if duration and duration.total_seconds() > 0 else None
    try:
        await tracker.async_mute(ieee, call.data.get("checks") or DEVICE_CHECKS, days)
    except TrackerAppError as err:
        raise _app_error(err) from err


async def _run_network_scan(hass: HomeAssistant, call: ServiceCall) -> None:
    tracker = _tracker(hass)
    base_topic = (call.data.get("base_topic") or "").strip() or None
    if base_topic and base_topic not in tracker.base_topics:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_base_topic",
            translation_placeholders={"base_topic": base_topic, "known": ", ".join(tracker.base_topics) or "none"},
        )
    try:
        await tracker.async_network_scan(base_topic)
    except TrackerAppError as err:
        raise _app_error(err) from err


async def _reconfigure(hass: HomeAssistant, call: ServiceCall) -> None:
    tracker = _tracker(hass)
    ieee = resolve_ieee(hass, call, tracker, active=True)
    try:
        await tracker.async_reconfigure(ieee)
    except TrackerAppError as err:
        raise _app_error(err) from err


async def _ota_update(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    tracker = _tracker(hass)
    if call.data.get(ATTR_DEVICE_ID) or call.data.get(ATTR_IEEE):
        ieee = resolve_ieee(hass, call, tracker, active=True)
        record = tracker.data["devices"][ieee]
        if (record.get("update") or {}).get("state") != "available":
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_update",
                translation_placeholders={"name": record.get("friendly_name") or ieee},
            )
        targets = [ieee]
    else:
        targets = [
            ieee
            for ieee, record in tracker.data["devices"].items()
            if record.get("status") == STATUS_ACTIVE and (record.get("update") or {}).get("state") == "available"
        ]
    started: list[str] = []
    failed: dict[str, str] = {}
    for ieee in targets:
        name = tracker.data["devices"].get(ieee, {}).get("friendly_name") or ieee
        try:
            await tracker.async_ota_update(ieee)
            started.append(name)
        except TrackerAppError as err:
            if len(targets) == 1:
                raise _app_error(err) from err
            failed[name] = str(err)
    return {"started": started, "failed": failed}


async def _add_note(hass: HomeAssistant, call: ServiceCall) -> None:
    tracker = _tracker(hass)
    ieee = resolve_ieee(hass, call, tracker)
    current = str(tracker.data["devices"][ieee].get("note") or "").rstrip()
    text = call.data["text"].strip()
    note = f"{current}\n{text}" if current else text
    if len(note) > NOTE_LIMIT:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="note_too_long",
            translation_placeholders={"limit": str(NOTE_LIMIT)},
        )
    try:
        await tracker.async_set_note(ieee, note)
    except TrackerAppError as err:
        raise _app_error(err) from err


HANDLERS: dict[str, Any] = {
    "battery_replaced": (_battery_replaced, SupportsResponse.NONE),
    "mute_device": (_mute_device, SupportsResponse.NONE),
    "run_network_scan": (_run_network_scan, SupportsResponse.NONE),
    "reconfigure": (_reconfigure, SupportsResponse.NONE),
    "ota_update": (_ota_update, SupportsResponse.OPTIONAL),
    "add_note": (_add_note, SupportsResponse.NONE),
}


def async_register_services(hass: HomeAssistant) -> None:
    """Register the automation actions once; they look up the loaded tracker on each call."""
    for name, (handler, supports_response) in HANDLERS.items():
        if hass.services.has_service(DOMAIN, name):
            continue

        async def _call(call: ServiceCall, _handler=handler) -> ServiceResponse:
            return await _handler(hass, call)

        hass.services.async_register(DOMAIN, name, _call, schema=SCHEMAS[name], supports_response=supports_response)
