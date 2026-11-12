"""Health checks: turns a device record + metrics into a list of issues."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

WARNING = "warning"
INFO = "info"

# Issue kinds that notify by default (info-level kinds only show in the UI unless enabled).
ALERT_KINDS = {"offline", "frozen", "battery_low", "weak_signal", "silent", "unidentified", "network_offline"}

# Everything the Settings page can switch: (key, label, description, is_event)
CHECKS = [
    ("offline", "Offline", "Z2M reports the device unavailable for longer than the offline threshold.", False),
    ("silent", "Not heard from", "No messages for 2 days from a device without availability reporting.", False),
    (
        "frozen",
        "Frozen reading",
        "Temperature, humidity, pressure or air quality hasn't changed while the device keeps reporting.",
        False,
    ),
    (
        "battery_low",
        "Low battery",
        "Battery at or below the battery threshold, or the device reports low battery.",
        False,
    ),
    ("battery_soon", "Battery running down", "Battery expected to reach 10% within 45 days.", False),
    ("weak_signal", "Weak signal", "Average link quality below 30 over the last 24 hours.", False),
    ("signal_drop", "Signal dropping", "Link quality today is well below the previous week.", False),
    ("unidentified", "Unidentified device", "A device joined but its interview never completed.", False),
    ("update", "Firmware update", "Zigbee2MQTT reports a firmware update is available.", False),
    ("replacement", "Possible replacement", "An active device has the same name as a removed one.", False),
    ("network_offline", "Network offline", "Zigbee2MQTT or its coordinator stops responding for 2 minutes.", False),
    ("restart_required", "Restart needed", "Zigbee2MQTT says it needs a restart to apply settings.", False),
    ("joined", "Device joined", "A device joins one of your networks.", True),
    ("left", "Device left", "A device leaves one of your networks.", True),
    ("interview_failed", "Interview failed", "A newly joined device couldn't be identified.", True),
]
DEFAULT_NOTIFY = sorted(ALERT_KINDS | {"joined", "left", "interview_failed"})

KIND_LABELS = {
    "offline": "Offline",
    "silent": "Not heard from",
    "frozen": "Frozen reading",
    "weak_signal": "Weak signal",
    "signal_drop": "Signal dropping",
    "battery_low": "Low battery",
    "battery_soon": "Battery running down",
    "update": "Firmware update",
    "unidentified": "Unidentified",
    "replacement": "Possible replacement",
}


def _age(iso: str | None, now: float) -> float | None:
    if not iso:
        return None
    try:
        return now - datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def _span(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    if seconds < 172800:
        return f"{int(seconds // 3600)} h"
    return f"{int(seconds // 86400)} days"


def mutes_of(record: dict) -> dict[str, str | None]:
    """Muted kinds as {kind: until_iso or None (forever)}; older records stored a plain list."""
    muted = record.get("muted") or {}
    if isinstance(muted, list):
        return {kind: None for kind in muted}
    return dict(muted)


def active_mutes(record: dict, now: float) -> list[str]:
    out = []
    for kind, until in mutes_of(record).items():
        if until is None:
            out.append(kind)
            continue
        try:
            if datetime.fromisoformat(until).timestamp() > now:
                out.append(kind)
        except ValueError:
            continue
    return out


def device_issues(
    record: dict, summary: dict, options: dict, now: float, suggestions: list[dict]
) -> list[dict[str, Any]]:
    ieee = record["ieee"]
    issues: list[dict[str, Any]] = []
    if record.get("type") == "Coordinator" or record.get("status") != "active":
        return issues
    muted = set(active_mutes(record, now)) | set(options.get("disabled_checks") or [])

    def add(kind: str, severity: str, title: str, detail: str) -> None:
        if kind in muted:
            return
        issues.append(
            {
                "key": f"{kind}:{ieee}",
                "kind": kind,
                "label": KIND_LABELS[kind],
                "severity": severity,
                "ieee": ieee,
                "title": title,
                "detail": detail,
            }
        )

    offline_minutes = max(0, int(options.get("offline_alert_minutes", 60)))
    if record.get("availability") == "offline":
        age = _age(record.get("availability_since"), now) or 0
        if age >= offline_minutes * 60:
            add("offline", WARNING, "Offline", f"Unavailable for {_span(age)}.")
    elif record.get("availability") is None:
        age = _age(record.get("last_seen"), now)
        if age is not None and age > 2 * 86400:
            add("silent", WARNING, "Not heard from", f"No messages for {_span(age)}.")

    if record.get("interview_state") and record.get("interview_state") != "SUCCESSFUL":
        add(
            "unidentified",
            WARNING,
            "Never identified",
            "Interview didn't complete, so the model is unknown. Try re-pairing it.",
        )

    frozen_hours = max(0, int(options.get("frozen_alert_hours", 24)))
    if frozen_hours:
        for key, reading in (record.get("readings") or {}).items():
            age = _age(reading.get("changed_at"), now)
            if age is not None and age >= frozen_hours * 3600 and reading.get("reports", 0) >= 20:
                add(
                    "frozen",
                    WARNING,
                    "Reading not changing",
                    f"{key.replace('_', ' ')} stuck at {reading.get('value')} for {_span(age)} across {reading['reports']} reports. Try Reconfigure.",
                )
                break

    lqi_24h = summary.get("lqi_24h")
    lqi_prev = summary.get("lqi_prev_7d")
    if lqi_24h is not None and lqi_24h < 30:
        add("weak_signal", WARNING, "Weak signal", f"Average link quality {lqi_24h:.0f} over 24 h.")
    elif lqi_24h is not None and lqi_prev and lqi_24h < lqi_prev * 0.6 and lqi_24h < 80:
        add(
            "signal_drop",
            INFO,
            "Signal dropping",
            f"Link quality {lqi_24h:.0f} today vs {lqi_prev:.0f} over the previous week.",
        )

    battery = record.get("battery")
    threshold = int(options.get("battery_alert_percent", 15))
    if record.get("battery_low") is True or (isinstance(battery, (int, float)) and battery <= threshold):
        add(
            "battery_low",
            WARNING,
            "Low battery",
            f"Battery at {battery:.0f}%." if isinstance(battery, (int, float)) else "Device reports low battery.",
        )
    else:
        days = summary.get("battery_estimate_days")
        if days is not None and days <= 45:
            add("battery_soon", INFO, "Battery running down", f"Expected to reach 10% in about {int(days)} days.")

    update = record.get("update") or {}
    if update.get("state") == "available":
        add(
            "update",
            INFO,
            "Firmware update available",
            f"{update.get('installed_version')} → {update.get('latest_version')}",
        )

    for suggestion in suggestions:
        add(
            "replacement",
            INFO,
            "Possible replacement",
            f"Has the same name as removed device {suggestion['name']} ({suggestion['ieee']}). Link them to carry over history.",
        )
        break

    return issues


def uptime(record: dict, now: float, days: int = 30) -> float | None:
    outages = record.get("outages") or []
    window_start = now - days * 86400
    first = _age(record.get("first_seen"), now)
    if first is not None:
        window_start = max(window_start, now - first)
    window = now - window_start
    if window <= 3600:
        return None
    down = 0.0
    for outage in outages:
        try:
            start = datetime.fromisoformat(outage["start"]).timestamp()
            end = datetime.fromisoformat(outage["end"]).timestamp() if outage.get("end") else now
        except (KeyError, ValueError, TypeError):
            continue
        down += max(0.0, min(end, now) - max(start, window_start))
    return round(max(0.0, 100.0 * (1 - down / window)), 2)


def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()
