"""Prometheus text format for the tracker's device and network state (the optional /metrics port)."""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime
from typing import Any

# (name, type, help) for every family, in output order.
FAMILIES: list[tuple[str, str, str]] = [
    ("z2m_tracker_info", "gauge", "App version, always 1."),
    ("z2m_tracker_alerts_paused", "gauge", "1 while alerts are paused."),
    ("z2m_tracker_network_online", "gauge", "1 when the Zigbee2MQTT bridge reports online."),
    ("z2m_tracker_network_devices", "gauge", "Tracked devices per network and status (coordinator excluded)."),
    ("z2m_tracker_issues", "gauge", "Open health issues per check and severity."),
    ("z2m_tracker_device_info", "gauge", "Device details as labels, always 1."),
    ("z2m_tracker_device_online", "gauge", "1 online, 0 offline. Missing when Zigbee2MQTT availability is off."),
    ("z2m_tracker_device_linkquality", "gauge", "Last reported link quality (0-255)."),
    ("z2m_tracker_device_linkquality_24h", "gauge", "Average link quality over the last 24 hours."),
    ("z2m_tracker_device_battery_percent", "gauge", "Last reported battery level."),
    ("z2m_tracker_device_battery_days_left", "gauge", "Estimated days until the battery runs low."),
    ("z2m_tracker_device_last_seen_timestamp_seconds", "gauge", "When the device last sent anything (Unix time)."),
    ("z2m_tracker_device_messages_24h", "gauge", "Messages received in the last 24 hours."),
    ("z2m_tracker_device_uptime_30d_percent", "gauge", "Share of the last 30 days the device was online."),
    ("z2m_tracker_device_issues", "gauge", "Open health issues for the device."),
]


def escape(value: Any) -> str:
    """A label value: backslash, double quote and newline escaped as the text format requires."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(labels: dict[str, Any]) -> str:
    parts = [f'{key}="{escape(value)}"' for key, value in labels.items() if value is not None]
    return "{" + ",".join(parts) + "}" if parts else ""


def _number(value: Any) -> str | None:
    if isinstance(value, bool):
        return "1" if value else "0"
    if not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)):
        return None
    return repr(float(value)) if isinstance(value, float) else str(value)


def _timestamp(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def render(samples: Iterable[tuple[str, dict[str, Any], Any]]) -> str:
    """Group (family, labels, value) samples under their HELP/TYPE lines. Samples without a number are dropped."""
    by_family: dict[str, list[str]] = {name: [] for name, _, _ in FAMILIES}
    for family, labels, value in samples:
        number = _number(value)
        if number is not None and family in by_family:
            by_family[family].append(f"{family}{_labels(labels)} {number}")
    lines: list[str] = []
    for name, kind, help_text in FAMILIES:
        if by_family[name]:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")
            lines.extend(by_family[name])
    return "\n".join(lines) + "\n"


def collect(snapshot: dict[str, Any], app_version: str) -> list[tuple[str, dict[str, Any], Any]]:
    """Samples from the data Tracker.prometheus_snapshot returns."""
    out: list[tuple[str, dict[str, Any], Any]] = [
        ("z2m_tracker_info", {"version": app_version}, 1),
        ("z2m_tracker_alerts_paused", {}, bool(snapshot.get("alerts_paused"))),
    ]
    for network, state in sorted((snapshot.get("bridges") or {}).items()):
        if state in ("online", "offline"):
            out.append(("z2m_tracker_network_online", {"network": network}, state == "online"))

    counts: dict[tuple[str, str], int] = {}
    issue_counts: dict[tuple[str, str], int] = {}
    issues_by_device: dict[str, int] = {}
    for issue in snapshot.get("issues") or []:
        key = (issue.get("kind") or "", issue.get("severity") or "")
        issue_counts[key] = issue_counts.get(key, 0) + 1
        issues_by_device[issue.get("ieee") or ""] = issues_by_device.get(issue.get("ieee") or "", 0) + 1
    for (kind, severity), count in sorted(issue_counts.items()):
        out.append(("z2m_tracker_issues", {"check": kind, "severity": severity}, count))

    for device in snapshot.get("devices") or []:
        network = device.get("instance") or ""
        counts[(network, device.get("status") or "")] = counts.get((network, device.get("status") or ""), 0) + 1
        if device.get("status") != "active":
            continue
        ident = {"ieee": device["ieee"], "name": device.get("name") or device["ieee"], "network": network}
        out.append(
            (
                "z2m_tracker_device_info",
                {
                    **ident,
                    "type": device.get("type") or "",
                    "power_source": device.get("power_source") or "",
                    "vendor": device.get("vendor") or "",
                    "model": device.get("model") or "",
                    "area": device.get("area") or "",
                },
                1,
            )
        )
        availability = device.get("availability")
        if availability in ("online", "offline"):
            out.append(("z2m_tracker_device_online", ident, availability == "online"))
        out.append(("z2m_tracker_device_linkquality", ident, device.get("lqi")))
        out.append(("z2m_tracker_device_linkquality_24h", ident, device.get("lqi_24h")))
        out.append(("z2m_tracker_device_battery_percent", ident, device.get("battery")))
        out.append(("z2m_tracker_device_battery_days_left", ident, device.get("battery_days")))
        out.append(("z2m_tracker_device_last_seen_timestamp_seconds", ident, _timestamp(device.get("last_seen"))))
        out.append(("z2m_tracker_device_messages_24h", ident, device.get("msg_24h")))
        out.append(("z2m_tracker_device_uptime_30d_percent", ident, device.get("uptime_30d")))
        out.append(("z2m_tracker_device_issues", ident, issues_by_device.get(device["ieee"], 0)))

    for (network, status), count in sorted(counts.items()):
        out.append(("z2m_tracker_network_devices", {"network": network, "status": status}, count))
    return out
