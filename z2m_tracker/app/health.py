"""Health checks: turns a device record + metrics into a list of issues."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

WARNING = "warning"
INFO = "info"

# Issue kinds that notify by default; every other kind only shows in the UI until enabled.
# Keep in step with the notify_checks default in config.yaml.
ALERT_KINDS = {
    "offline",
    "frozen",
    "battery_low",
    "weak_signal",
    "silent",
    "late",
    "unidentified",
    "network_offline",
}

FLAP_OUTAGES = 4
FLAP_LEAVES = 2
BINDING_GRACE_SECONDS = 600
DEFAULT_CHATTY_PER_HOUR = 1200
# Shortest per-device "should report at least every" interval, and the longest (7 days).
REPORT_EVERY_MIN = 30
REPORT_EVERY_MAX = 7 * 86400

# Everything the Settings page can switch: (key, label, description, is_event)
CHECKS = [
    (
        "offline",
        "Offline",
        "Zigbee2MQTT has marked the device unavailable, and it stays that way for longer than the offline alert delay.",
        False,
    ),
    ("silent", "Not heard from", "No messages for 2 days from a device without availability reporting.", False),
    (
        "late",
        "Stopped reporting",
        "No messages for longer than the device's own report interval, set on its Health tab.",
        False,
    ),
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
    (
        "flapping",
        "Flapping",
        "Drops offline 4 or more times in 24 hours, or leaves the network twice, even if each drop is short.",
        False,
    ),
    (
        "chatty",
        "Chatty device",
        "Sends more messages an hour than the chatty threshold, or 5 times its usual rate. Floods the mesh for everyone.",
        False,
    ),
    (
        "battery_drain",
        "Battery draining fast",
        "Battery fell at least 3 times faster over the last 14 days than its usual rate. Often a weak link to its router.",
        False,
    ),
    (
        "binding_lost",
        "Binding lost",
        "A binding (device to device, group or coordinator reporting) disappeared and hasn't come back after 10 minutes.",
        False,
    ),
    (
        "single_point",
        "Single point of failure",
        "A router that is the only path to the coordinator for other routers in the last routing scan.",
        False,
    ),
    (
        "coordinator_backup",
        "Coordinator backup",
        "Zigbee2MQTT's coordinator_backup.json is missing, more than 3 days old, or doesn't match the running network.",
        False,
    ),
    ("joined", "Device joined", "A device joins one of your networks.", True),
    ("left", "Device left", "A device leaves one of your networks.", True),
    ("interview_failed", "Interview failed", "A newly joined device couldn't be identified.", True),
]
DEFAULT_NOTIFY = sorted(ALERT_KINDS | {"joined", "left", "interview_failed"})

KIND_LABELS = {
    "offline": "Offline",
    "silent": "Not heard from",
    "late": "Stopped reporting",
    "frozen": "Frozen reading",
    "weak_signal": "Weak signal",
    "signal_drop": "Signal dropping",
    "battery_low": "Low battery",
    "battery_soon": "Battery running down",
    "update": "Firmware update",
    "unidentified": "Unidentified",
    "replacement": "Possible replacement",
    "flapping": "Flapping",
    "chatty": "Chatty device",
    "battery_drain": "Battery draining fast",
    "binding_lost": "Binding lost",
    "single_point": "Single point of failure",
    "coordinator_backup": "Coordinator backup",
}


def _age(iso: str | None, now: float) -> float | None:
    if not iso:
        return None
    try:
        return now - datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def _span(seconds: float) -> str:
    if seconds < 120:
        return f"{int(seconds)} s"
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    if seconds < 172800:
        return f"{int(seconds // 3600)} h"
    return f"{int(seconds // 86400)} days"


def late_deadline(record: dict, started: float) -> float | None:
    """When an active device with a report interval counts as late, or None if it has no interval.

    The clock starts no earlier than the app's own start, so a restart doesn't flag
    devices whose messages arrived while the app was down.
    """
    every = record.get("report_every")
    if not every or record.get("status") != "active" or record.get("type") == "Coordinator":
        return None
    try:
        seen = datetime.fromisoformat(record["last_seen"]).timestamp()
    except (KeyError, TypeError, ValueError):
        seen = started
    return max(seen, started) + every


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

    def add(kind: str, severity: str, title: str, detail: str, msg: str = "", **params: Any) -> None:
        # title/detail stay English for notifications; msg + params let the sidebar translate them
        # (keys issue.<msg>.title and issue.<msg>.detail in www/i18n/*.json).
        if kind in muted:
            return
        issue = {
            "key": f"{kind}:{ieee}",
            "kind": kind,
            "label": KIND_LABELS[kind],
            "severity": severity,
            "ieee": ieee,
            "title": title,
            "detail": detail,
        }
        if msg:
            issue["msg"] = msg
            issue["params"] = params
        issues.append(issue)

    offline_minutes = max(0, int(options.get("offline_alert_minutes", 60)))
    if record.get("availability") == "offline":
        age = _age(record.get("availability_since"), now) or 0
        if age >= offline_minutes * 60:
            add("offline", WARNING, "Offline", f"Unavailable for {_span(age)}.", "offline", seconds=int(age))
    elif record.get("availability") is None:
        age = _age(record.get("last_seen"), now)
        if age is not None and age > 2 * 86400:
            add("silent", WARNING, "Not heard from", f"No messages for {_span(age)}.", "silent", seconds=int(age))

    deadline = late_deadline(record, summary.get("started", 0.0))
    if deadline is not None and now >= deadline and not summary.get("network_down"):
        every = int(record["report_every"])
        age = _age(record.get("last_seen"), now)
        quiet = int(age) if age is not None else every
        add(
            "late",
            WARNING,
            "Stopped reporting",
            f"No messages for {_span(quiet)}. It should report at least every {_span(every)}.",
            "late",
            seconds=quiet,
            every=every,
        )

    if record.get("interview_state") and record.get("interview_state") != "SUCCESSFUL":
        add(
            "unidentified",
            WARNING,
            "Never identified",
            "Interview didn't complete, so the model is unknown. Try re-pairing it.",
            "unidentified",
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
                    "frozen",
                    reading=key,
                    value=reading.get("value"),
                    seconds=int(age),
                    n=reading["reports"],
                )
                break

    lqi_24h = summary.get("lqi_24h")
    lqi_prev = summary.get("lqi_prev_7d")
    if lqi_24h is not None and lqi_24h < 30:
        add(
            "weak_signal",
            WARNING,
            "Weak signal",
            f"Average link quality {lqi_24h:.0f} over 24 h.",
            "weak_signal",
            lqi=round(lqi_24h),
        )
    elif lqi_24h is not None and lqi_prev and lqi_24h < lqi_prev * 0.6 and lqi_24h < 80:
        add(
            "signal_drop",
            INFO,
            "Signal dropping",
            f"Link quality {lqi_24h:.0f} today vs {lqi_prev:.0f} over the previous week.",
            "signal_drop",
            lqi=round(lqi_24h),
            prev=round(lqi_prev),
        )

    battery = record.get("battery")
    threshold = int(options.get("battery_alert_percent", 15))
    if record.get("battery_low") is True or (isinstance(battery, (int, float)) and battery <= threshold):
        add(
            "battery_low",
            WARNING,
            "Low battery",
            f"Battery at {battery:.0f}%." if isinstance(battery, (int, float)) else "Device reports low battery.",
            "battery_low" if isinstance(battery, (int, float)) else "battery_low_reported",
            percent=round(battery) if isinstance(battery, (int, float)) else None,
        )
    else:
        days = summary.get("battery_estimate_days")
        if days is not None and days <= 45:
            add(
                "battery_soon",
                INFO,
                "Battery running down",
                f"Expected to reach 10% in about {int(days)} days.",
                "battery_soon",
                n=int(days),
            )

    update = record.get("update") or {}
    if update.get("state") == "available":
        add(
            "update",
            INFO,
            "Firmware update available",
            f"{update.get('installed_version')} → {update.get('latest_version')}",
            "update",
            installed=update.get("installed_version"),
            latest=update.get("latest_version"),
        )

    if summary.get("outages_24h", 0) >= FLAP_OUTAGES or summary.get("leaves_24h", 0) >= FLAP_LEAVES:
        parts = []
        if summary.get("outages_24h"):
            parts.append(f"went offline {summary['outages_24h']} times")
        if summary.get("leaves_24h"):
            parts.append(f"left the network {summary['leaves_24h']} times")
        add(
            "flapping",
            WARNING,
            "Keeps dropping out",
            f"In the last 24 h it {' and '.join(parts)}. Usually a weak or unstable link, a failing power supply, or a router nearby that keeps restarting.",
            "flapping",
            outages=summary.get("outages_24h") or 0,
            leaves=summary.get("leaves_24h") or 0,
        )

    chatty_limit = max(1, int(options.get("chatty_per_hour", DEFAULT_CHATTY_PER_HOUR) or DEFAULT_CHATTY_PER_HOUR))
    rate = summary.get("msg_rate_6h")
    usual = summary.get("msg_rate_prev")
    if rate is not None and rate >= chatty_limit:
        add(
            "chatty",
            WARNING,
            "Very chatty",
            f"About {rate:.0f} messages an hour (one every {3600 / rate:.1f} s) over the last 6 h. That much traffic slows the whole mesh. Raise its reporting interval or set debounce/throttle for it in Zigbee2MQTT.",
            "chatty_high",
            rate=round(rate),
            every=round(3600 / rate, 1),
        )
    elif rate is not None and usual and rate >= 5 * usual and rate >= 360:
        add(
            "chatty",
            INFO,
            "Chattier than usual",
            f"About {rate:.0f} messages an hour over the last 6 h, against its usual {usual:.0f}. Something changed: a stuck sensor, a reporting setting, or an automation sending commands in a loop.",
            "chatty_rise",
            rate=round(rate),
            usual=round(usual),
        )

    drain = summary.get("battery_drain")
    if drain:
        parent = summary.get("parent")
        via = f" It connects through {parent['name']} (LQI {parent['lqi']})." if parent and parent.get("name") else ""
        add(
            "battery_drain",
            WARNING,
            "Battery draining fast",
            f"Dropped {drain['dropped']:.0f}% in the last {drain['days']} days, about {drain['factor']:.0f}× its usual rate of {drain['usual_rate']:.2f}% a day.{via} Often a weak link to its router (it keeps retrying), or a sensor stuck reporting.",
            "battery_drain_via" if via else "battery_drain",
            dropped=round(drain["dropped"]),
            n=drain["days"],
            factor=round(drain["factor"]),
            usual_rate=round(drain["usual_rate"], 2),
            parent=parent.get("name") if via else None,
            lqi=parent.get("lqi") if via else None,
        )

    lost = [b for b in record.get("lost_bindings") or [] if (_age(b.get("lost_at"), now) or 0) >= BINDING_GRACE_SECONDS]
    if lost:
        names = "; ".join(b.get("label") or b.get("key", "") for b in lost[:4]) + ("…" if len(lost) > 4 else "")
        to_coord = any(b.get("coordinator") for b in lost)
        fix = (
            "Reconfigure re-creates the reporting bindings to the coordinator."
            if to_coord
            else "Re-create it in Zigbee2MQTT on the device's Bind tab."
        )
        add(
            "binding_lost",
            WARNING,
            "Binding lost",
            f"{len(lost)} binding{'s' if len(lost) != 1 else ''} gone since {lost[0].get('lost_at', '')[:16].replace('T', ' ')} UTC: {names}. {fix}",
            "binding_lost_coordinator" if to_coord else "binding_lost",
            n=len(lost),
            since=lost[0].get("lost_at", ""),
            names=names,
        )

    spof = summary.get("single_point")
    if spof:
        routers = spof["routers"]
        devices = spof["devices"]
        shown = ", ".join(r["name"] for r in routers[:4]) + ("…" if len(routers) > 4 else "")
        extra = f", plus {devices} battery device{'s' if devices != 1 else ''} behind them" if devices else ""
        add(
            "single_point",
            INFO,
            "Single point of failure",
            f"In the last routing scan it was the only path to the coordinator for {len(routers)} router{'s' if len(routers) != 1 else ''} ({shown}){extra}. If it loses power they have to find another way in. Another router between them and the rest of the mesh removes the risk.",
            "single_point",
            n=len(routers),
            names=shown,
            devices=devices,
        )

    for suggestion in suggestions:
        add(
            "replacement",
            INFO,
            "Possible replacement",
            f"Has the same name as removed device {suggestion['name']} ({suggestion['ieee']}). Link them to carry over history.",
            "replacement",
            name=suggestion["name"],
            ieee=suggestion["ieee"],
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
