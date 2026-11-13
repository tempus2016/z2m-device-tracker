"""Download diagnostics: app version, settings, network and device summaries, with personal details redacted."""

from __future__ import annotations

from collections import Counter
from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_APP_URL, VERSION
from .tracker import TrackerAppError, Z2MTracker

TO_REDACT = {
    CONF_APP_URL,
    "asset",
    "coordinator_ieee",
    "detail",
    "friendly_name",
    "ha_name",
    "ieee",
    "name",
    "names",
    "note",
    "notes",
    "notify_service",
    "pan_id",
    "z2m_panels",
}

DEVICE_FIELDS = (
    "type",
    "power_source",
    "vendor",
    "model",
    "model_id",
    "manufacturer",
    "software_build_id",
    "date_code",
    "interview_state",
    "status",
    "availability",
    "lqi",
    "battery",
    "battery_low",
)


def _redact_url(url: str) -> str:
    """Keep the scheme and port, which help with connection problems, and hide the host."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return REDACTED
    hostport = rest.split("/", 1)[0]
    port = hostport.rsplit(":", 1)[1] if ":" in hostport else ""
    return f"{scheme}://{REDACTED}{':' + port if port else ''}"


def _networks(payload: dict) -> list[dict[str, Any]]:
    bridges = payload.get("bridges") or {}
    topics = list(payload.get("instances") or []) or sorted(bridges)
    out = []
    for topic in topics:
        devices = [d for d in payload.get("devices") or [] if d.get("instance") == topic]
        active = [d for d in devices if d.get("status") == "active" and d.get("type") != "Coordinator"]
        bridge = bridges.get(topic) or {}
        info = bridge.get("info") or {}
        health = bridge.get("health") or {}
        out.append(
            {
                "base_topic": topic,
                "state": bridge.get("state"),
                "z2m_version": info.get("version"),
                "coordinator_type": info.get("coordinator_type"),
                "coordinator_firmware": info.get("firmware"),
                "channel": info.get("channel"),
                "pan_id": info.get("pan_id"),
                "coordinator_ieee": info.get("coordinator_ieee"),
                "restart_required": info.get("restart_required"),
                "z2m_uptime_sec": health.get("uptime_sec"),
                "mqtt_connected": health.get("mqtt_connected"),
                "outages": len(bridge.get("outages") or []),
                "restarts": len(bridge.get("restarts") or []),
                "devices": len(active),
                "routers": sum(1 for d in active if d.get("type") == "Router"),
                "end_devices": sum(1 for d in active if d.get("type") == "EndDevice"),
                "battery": sum(1 for d in active if d.get("battery_record") is not None),
                "offline": sum(1 for d in active if d.get("availability") == "offline"),
                "removed": sum(1 for d in devices if d.get("status") == "removed"),
            }
        )
    return out


def _device(record: dict) -> dict[str, Any]:
    metrics = record.get("metrics") or {}
    battery = record.get("battery_record") or {}
    return {
        "ieee": record.get("ieee"),
        "friendly_name": record.get("friendly_name"),
        "network": record.get("instance"),
        **{key: record.get(key) for key in DEVICE_FIELDS},
        "lqi_24h": metrics.get("lqi_24h"),
        "battery_estimate_days": metrics.get("battery_estimate_days"),
        "battery_type": battery.get("label") or None,
        "uptime_30d": record.get("uptime_30d"),
        "update_available": bool((record.get("update") or {}).get("state") == "available"),
        "issues": sorted({i.get("kind") for i in record.get("issues") or [] if i.get("kind")}),
        "has_note": bool(record.get("note")),
        "has_asset": bool(record.get("asset")),
    }


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry) -> dict[str, Any]:
    tracker: Z2MTracker = entry.runtime_data
    errors: list[str] = []
    try:
        version = await tracker._request("GET", "/api/version")
    except TrackerAppError as err:
        version = {}
        errors.append(f"/api/version: {err}")
    try:
        payload = await tracker._request("GET", "/api/devices")
    except TrackerAppError as err:
        errors.append(f"/api/devices: {err}")
        payload = {
            "devices": list(tracker.data.get("devices", {}).values()),
            "issues": tracker.data.get("issues") or [],
            "options": tracker.data.get("options") or {},
            "instances": tracker.base_topics,
        }

    issues = payload.get("issues") or []
    devices = payload.get("devices") or []
    events = payload.get("events") or []
    data = {
        "integration": {
            "version": VERSION,
            "app_address": _redact_url(tracker.app_url),
            "repairs": tracker.repairs,
            "app_reachable": tracker.available,
            "errors": errors,
        },
        "entry": {"data": dict(entry.data), "options": dict(entry.options)},
        "app": {"version": version.get("app"), "data_version": version.get("version") or payload.get("version")},
        "settings": payload.get("options") or {},
        "networks": _networks(payload),
        "issues": {
            "total": len(issues),
            "warnings": sum(1 for i in issues if i.get("severity") == "warning"),
            "by_kind": dict(sorted(Counter(i.get("kind") or "unknown" for i in issues).items())),
        },
        "recent_events": [
            {"time": e.get("time"), "network": e.get("instance"), "kind": e.get("kind")} for e in events[-25:]
        ],
        "devices": [_device(d) for d in devices],
    }
    return async_redact_data(data, TO_REDACT)
