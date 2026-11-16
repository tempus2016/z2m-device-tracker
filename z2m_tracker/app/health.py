"""Health checks: turns a device record + metrics into a list of issues."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

from incidents import POWER_MIN_DEVICES, POWER_WINDOW, is_battery, is_mains
from odd_readings import issue as odd_reading_issue
from reporting import audit as reporting_audit
from reporting import findings as reporting_findings
from reporting import summary_text as reporting_summary

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
    "pairing_open",
}
# Checks added to notify_checks after release. Existing installs get each one switched on once at
# upgrade (their saved list predates it); removing it in Settings afterwards sticks.
NEW_NOTIFY_KINDS = ("late", "pairing_open")

FLAP_OUTAGES = 4
FLAP_LEAVES = 2
BINDING_GRACE_SECONDS = 600
DEFAULT_CHATTY_PER_HOUR = 1200
# Shortest per-device "should report at least every" interval, and the longest (7 days).
REPORT_EVERY_MIN = 30
REPORT_EVERY_MAX = 7 * 86400
# After a router comes back, the devices behind it get this long to rejoin before they alert on their own.
ROUTER_BACK_GRACE = 300
# Router hopping: an end device whose parent changes this many times across the routing scans of the last HOP_DAYS.
HOP_CHANGES = 3
HOP_DAYS = 7
DEFAULT_CRITICAL_OFFLINE_MINUTES = 5
# Availability mismatch: "offline" while this many live messages arrived within MISMATCH_WINDOW, after
# being offline for at least MISMATCH_SETTLE (Zigbee2MQTT itself flips a device back online within
# seconds of a message, so a short overlap is just the change arriving).
MISMATCH_MESSAGES = 3
MISMATCH_WINDOW = 600
MISMATCH_SETTLE = 300
# "online" with no message for at least this long, and at least MISMATCH_GAPS of the device's usual gap.
MISMATCH_QUIET = 6 * 3600
MISMATCH_GAPS = 10

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
        "impossible_reading",
        "Impossible readings",
        "A temperature outside the limits set in Settings, humidity or battery outside 0-100%, or humidity stuck at 0 or 100% for longer than the set hours.",
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
        "pairing_open",
        "Pairing left open",
        "Pairing (permit join) stays open for 10 minutes, or something other than the Zigbee Tracker opened it.",
        False,
    ),
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
        "router_hopping",
        "Changes router often",
        "An end device whose parent router changed 3 or more times in the routing scans of the last 7 days. Usually a weak link or a router that keeps dropping.",
        False,
    ),
    (
        "slow_router",
        "Router getting slower",
        "A router's nightly reply time over the last 3 days is more than 3 times its usual and above 150 ms.",
        False,
    ),
    (
        "slow_commands",
        "Slow to respond",
        "Commands to the device (from Home Assistant or automations) take more than 1 s to answer over the last 24 hours, or 3 times longer than its usual. Needs at least 10 commands.",
        False,
    ),
    (
        "missed_commands",
        "Misses commands",
        "At least 20% of the commands sent to the device over the last 24 hours got no reply within 10 s. Needs at least 10 commands.",
        False,
    ),
    (
        "coordinator_backup",
        "Coordinator backup",
        "Zigbee2MQTT's coordinator_backup.json is missing, more than 3 days old, or doesn't match the running network.",
        False,
    ),
    (
        "z2m_memory",
        "Zigbee2MQTT memory",
        "Zigbee2MQTT's own memory use grew more than 50% over the last 7 days. Needs Zigbee2MQTT 2.5 or newer.",
        False,
    ),
    (
        "signal_dip",
        "Daily signal dip",
        "Link quality drops across a network at the same 1 to 4 hours on at least 4 of the last 7 days.",
        False,
    ),
    (
        "upgrade_worse",
        "Worse since an upgrade",
        "After a Zigbee2MQTT or coordinator firmware update, at least 2 days of the week after look clearly worse than the week before: more devices going offline or leaving, lower link quality, fewer messages or more parent router changes.",
        False,
    ),
    (
        "group_member_offline",
        "Group members offline",
        "A Zigbee2MQTT group has members that have been offline for longer than the offline threshold.",
        False,
    ),
    (
        "maintenance_due",
        "Maintenance overdue",
        "A maintenance task set on the device (such as testing a smoke alarm) is past its due date.",
        False,
    ),
    (
        "reporting_config",
        "Reporting settings",
        "A device's attribute reporting is set to drain its battery, flood the mesh, or never report a measurement.",
        False,
    ),
    (
        "naming",
        "Naming",
        "A device still named by its IEEE address (0x…), or, when a naming pattern is set, a name that doesn't follow it.",
        False,
    ),
    (
        "firmware_note",
        "Firmware you noted",
        "A device runs a firmware version you wrote a firmware note about on the Updates tab.",
        False,
    ),
    (
        "stale_retained",
        "Stale retained messages",
        "Retained MQTT messages under an old device name, of a removed device, or Home Assistant discovery for a device no longer in Zigbee2MQTT.",
        False,
    ),
    (
        "retained_command",
        "Retained command",
        "A command (<name>/set) is retained on the broker, so the device repeats it every time the broker or Zigbee2MQTT restarts.",
        False,
    ),
    (
        "availability_mismatch",
        "Availability disagreement",
        "Zigbee2MQTT says the device is offline while it keeps sending messages (3 in 10 minutes), or online while it has sent nothing for far longer than usual. Usually a stale retained availability topic.",
        False,
    ),
    ("joined", "Device joined", "A device joins one of your networks.", True),
    ("left", "Device left", "A device leaves one of your networks.", True),
    ("interview_failed", "Interview failed", "A newly joined device couldn't be identified.", True),
]
DEFAULT_NOTIFY = sorted(ALERT_KINDS | {"joined", "left", "interview_failed"})


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def notify_kinds(options: dict) -> set[str]:
    """Issue and event kinds that send a notification with these settings."""
    if not options.get("alerts_enabled", True):
        return set()
    return set(options.get("notify_checks") or []) - set(options.get("disabled_checks") or [])


KIND_LABELS = {
    "offline": "Offline",
    "silent": "Not heard from",
    "late": "Stopped reporting",
    "frozen": "Frozen reading",
    "impossible_reading": "Impossible reading",
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
    "router_hopping": "Changes router often",
    "slow_router": "Router getting slower",
    "slow_commands": "Slow to respond",
    "missed_commands": "Misses commands",
    "coordinator_backup": "Coordinator backup",
    "z2m_memory": "Memory climbing",
    "signal_dip": "Daily signal dip",
    "group_member_offline": "Group members offline",
    "maintenance_due": "Maintenance overdue",
    "upgrade_worse": "Worse since upgrade",
    "reporting_config": "Reporting settings",
    "naming": "Naming",
    "firmware_note": "Firmware you noted",
    "stale_retained": "Stale retained messages",
    "retained_command": "Retained command",
    "availability_mismatch": "Availability disagreement",
}


def offer_new_kinds(notify_checks: list[str], offered: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Switch each NEW_NOTIFY_KINDS entry on once. Returns (notify_checks, offered, added)."""
    new = [kind for kind in NEW_NOTIFY_KINDS if kind not in offered]
    added = [kind for kind in new if kind not in notify_checks]
    return sorted(set(notify_checks) | set(added)), sorted(set(offered) | set(new)), added


# Per-device overrides in record["limits"]: key -> (global option, min, max, default).
# Ranges match the Settings page; frozen 0 (off) is global-only.
LIMITS = {
    "battery_percent": ("battery_alert_percent", 0, 100, 15),
    "frozen_hours": ("frozen_alert_hours", 1, 720, 24),
}
LIMIT_NAMES = {"battery_percent": "Low battery limit", "frozen_hours": "Frozen reading limit"}


def limit_of(record: dict, options: dict, key: str) -> int:
    """The device's own limit when set, otherwise the global option."""
    option, low, high, default = LIMITS[key]
    value = (record.get("limits") or {}).get(key)
    if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
        return value
    return int(options.get(option, default))


def clean_limits(current: dict, changes: dict) -> tuple[dict, str | None]:
    """Apply {key: int | None} changes to a limits dict. Returns (new limits, error)."""
    out = dict(current or {})
    for key, value in changes.items():
        if key not in LIMITS:
            continue
        if value is None or value == "":
            out.pop(key, None)
            continue
        _, low, high, _ = LIMITS[key]
        whole = isinstance(value, int) or (isinstance(value, float) and value.is_integer())
        if isinstance(value, bool) or not whole or not low <= int(value) <= high:
            return dict(current or {}), f"{LIMIT_NAMES[key]} must be a whole number from {low} to {high}"
        out[key] = int(value)
    return out, None


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


# Zigbee2MQTT's own availability defaults, in minutes: active = mains/router devices it pings,
# passive = battery devices it only listens for.
Z2M_ACTIVE_MINUTES = 10
Z2M_PASSIVE_MINUTES = 1500
SILENT_SECONDS = 2 * 86400


def _minutes(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return value


def _device_availability(value: Any) -> bool | float | None:
    """A device's `availability` option as False, True or a timeout in minutes (None = not set)."""
    if value is None:
        return None
    if isinstance(value, dict):
        return _minutes(value.get("timeout")) or True
    return bool(value)


def z2m_availability(info: dict) -> dict | None:
    """The availability bits of Zigbee2MQTT's bridge/info config, or None if it sent no config.

    2.x always sends `availability: {enabled, active: {timeout}, passive: {timeout}}`.
    1.x sends nothing (off), `{}` / an object (on), or turns it on with the deprecated
    `advanced.availability_timeout`, and can limit it with pass/block lists.
    Per-device options: `false`, `true` or `{timeout}`, merged over `device_options`.
    """
    config = info.get("config")
    if not isinstance(config, dict):
        return None
    try:
        major = int(str(info.get("version") or "").split(".")[0])
    except ValueError:
        major = 0
    raw = config.get("availability")
    if not major:
        major = 2 if isinstance(raw, dict) and "enabled" in raw else 1
    advanced = config.get("advanced") if isinstance(config.get("advanced"), dict) else {}
    if major >= 2:
        enabled = bool(raw.get("enabled")) if isinstance(raw, dict) else bool(raw)
    else:
        enabled = raw is True or isinstance(raw, dict) or bool(advanced.get("availability_timeout"))
    block = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {
        "major": major,
        "enabled": enabled,
        "active": _minutes((block.get("active") or {}).get("timeout")) or Z2M_ACTIVE_MINUTES,
        "passive": _minutes((block.get("passive") or {}).get("timeout")) or Z2M_PASSIVE_MINUTES,
    }
    default = _device_availability((config.get("device_options") or {}).get("availability"))
    if default is not None:
        out["default"] = default
    devices = {}
    for ieee, opts in (config.get("devices") or {}).items():
        if not isinstance(opts, dict):
            continue
        if opts.get("disabled"):
            devices[str(ieee).lower()] = False
        elif "availability" in opts:
            devices[str(ieee).lower()] = _device_availability(opts["availability"])
    if devices:
        out["devices"] = devices
    if major < 2:
        for key, names in (
            ("passlist", ("availability_passlist", "availability_whitelist")),
            ("blocklist", ("availability_blocklist", "availability_blacklist")),
        ):
            items = [str(x) for name in names for x in advanced.get(name) or []]
            if items:
                out[key] = items
    return out


# Coordinator type from zigbee-herdsman -> adapter driver, for setups that leave serial.adapter to auto-detect.
_ADAPTER_BY_TYPE = (
    ("zstack", "zstack"),
    ("emberznet", "ember"),
    ("ezsp", "ezsp"),
    ("conbee", "deconz"),
    ("raspbee", "deconz"),
    ("zigate", "zigate"),
    ("zboss", "zboss"),
    ("zigbee on host", "zoh"),
)


def strip_credentials(url: Any) -> str | None:
    """A URL without any user:password@ part, or None if there's no URL."""
    if not isinstance(url, str) or not url.strip():
        return None
    scheme, sep, rest = url.strip().partition("://")
    if not sep:
        scheme, rest = "", scheme
    # Everything up to the last "@" is credentials: a raw password can itself hold "/", ":" or "@".
    return f"{scheme}{sep}{rest.rpartition('@')[2]}"


def z2m_connection(port: Any) -> dict | None:
    """How Zigbee2MQTT reaches its coordinator, from serial.port.

    `{mode, target, name}`: mode is tcp, mdns, usb or serial. `name` is the /dev/serial/by-id name
    shortened to the adapter's model (USB serial number and interface suffix dropped).
    """
    if not isinstance(port, str) or not port.strip():
        return None
    port = port.strip()
    scheme, sep, rest = port.partition("://")
    if sep and scheme.lower() == "tcp":
        return {"mode": "tcp", "target": strip_credentials(rest) or rest, "name": None}
    if sep and scheme.lower() == "mdns":
        return {"mode": "mdns", "target": rest, "name": None}
    name = None
    if "/serial/by-id/" in port:
        raw = port.rsplit("/", 1)[-1]
        raw = re.sub(r"^usb-", "", raw)
        raw = re.sub(r"-if\d+(-port\d+)?$", "", raw)
        parts = raw.split("_")
        # Drop the trailing USB serial number (long, has a digit, no spaces).
        if len(parts) > 1 and len(parts[-1]) >= 6 and re.search(r"\d", parts[-1]) and parts[-1].isalnum():
            parts = parts[:-1]
        name = " ".join(p for p in parts if p) or None
    usb = name is not None or re.search(r"tty(USB|ACM)\d*$", port) is not None
    return {"mode": "usb" if usb else "serial", "target": port, "name": name}


def z2m_setup(info: dict) -> dict:
    """The setup details the Coordinators card shows, from bridge/info. Never secrets.

    Zigbee2MQTT already drops the network key, MQTT password and frontend token from bridge/info;
    this keeps only a few display fields so history.json stays small, and strips credentials from URLs.
    Missing fields come back as None (1.x sends less).
    """
    config = info.get("config") if isinstance(info.get("config"), dict) else {}
    serial = config.get("serial") if isinstance(config.get("serial"), dict) else {}
    advanced = config.get("advanced") if isinstance(config.get("advanced"), dict) else {}
    mqtt = config.get("mqtt") if isinstance(config.get("mqtt"), dict) else {}
    coordinator = info.get("coordinator") if isinstance(info.get("coordinator"), dict) else {}
    network = info.get("network") if isinstance(info.get("network"), dict) else {}

    adapter = serial.get("adapter") if isinstance(serial.get("adapter"), str) else None
    if not adapter:
        ctype = str(coordinator.get("type") or "").lower()
        adapter = next((a for key, a in _ADAPTER_BY_TYPE if key in ctype), None)

    raw_frontend = config.get("frontend")
    frontend = None
    if raw_frontend is True:
        frontend = {"url": None, "port": 8080}
    elif isinstance(raw_frontend, dict) and raw_frontend.get("enabled", True):
        port = raw_frontend.get("port")
        frontend = {
            "url": strip_credentials(raw_frontend.get("url")),
            "port": port if isinstance(port, int) and not isinstance(port, bool) else 8080,
        }

    rtscts = serial.get("rtscts")
    baudrate = serial.get("baudrate")

    def version(key: str) -> str | None:
        value = info.get(key)
        value = value.get("version") if isinstance(value, dict) else value
        return str(value) if value else None

    top_mqtt = info.get("mqtt") if isinstance(info.get("mqtt"), dict) else {}
    log_level = info.get("log_level") or advanced.get("log_level")
    return {
        "connection": z2m_connection(serial.get("port")),
        "adapter": adapter,
        "baudrate": baudrate if isinstance(baudrate, int) and not isinstance(baudrate, bool) else None,
        "rtscts": rtscts if isinstance(rtscts, bool) else None,
        "extended_pan_id": str(network["extended_pan_id"]) if network.get("extended_pan_id") else None,
        "herdsman": version("zigbee_herdsman"),
        "converters": version("zigbee_herdsman_converters"),
        "log_level": str(log_level) if isinstance(log_level, str) and log_level else None,
        "mqtt_server": strip_credentials(mqtt.get("server")) or strip_credentials(top_mqtt.get("server")),
        "frontend": frontend,
    }


def _z2m_active(record: dict, major: int) -> bool:
    """Zigbee2MQTT pings "active" devices (shorter timeout); the rest are "passive" (battery)."""
    power = record.get("power_source")
    if record.get("type") == "Router" and power != "Battery":
        return True
    if major >= 2:
        return power not in (None, "Unknown", "Battery")
    return power == "Mains (single phase)"


def offline_timing(record: dict, availability: dict | None, options: dict, now: float) -> dict | None:
    """When each alert fires for a device, counted from its last message.

    Steps are in seconds after the last message. Zigbee2MQTT marks a device offline after its own
    timeout; the Offline alert then waits offline_alert_minutes more. Without Z2M availability the
    only check is Not heard from, after 2 days. None when Z2M hasn't sent its config yet.
    """
    if availability is None or record.get("type") == "Coordinator" or record.get("status") != "active":
        return None
    off = set(active_mutes(record, now)) | set(options.get("disabled_checks") or [])
    major = availability.get("major") or 2
    active = _z2m_active(record, major)
    ieee = record["ieee"].lower()
    devices = availability.get("devices") or {}
    if ieee in devices:
        setting, where = devices[ieee], "device"
    else:
        setting, where = availability.get("default"), "all"
    names = {ieee, record.get("friendly_name") or ""}
    if setting is False:
        tracked, why = False, "device" if where == "device" else "network"
    elif setting is not None:
        tracked, why = True, None
    elif not availability.get("enabled"):
        tracked, why = False, "network"
    elif availability.get("passlist"):
        tracked = bool(names & set(availability["passlist"]))
        why = None if tracked else "device"
    else:
        tracked = not names & set(availability.get("blocklist") or [])
        why = None if tracked else "device"

    out: dict[str, Any] = {"tracked": tracked, "why": why, "mode": "active" if active else "passive"}
    steps = [{"kind": "last", "at": 0}]
    if tracked:
        if _minutes(setting):
            minutes, source = setting, "device" if where == "device" else "custom"
        else:
            minutes = availability.get("active" if active else "passive")
            default = Z2M_ACTIVE_MINUTES if active else Z2M_PASSIVE_MINUTES
            minutes = _minutes(minutes) or default
            source = "default" if minutes == default else "custom"
        z2m = int(minutes * 60)
        delay = max(0, int(options.get("offline_alert_minutes", 60))) * 60
        out.update(z2m=z2m, source=source, delay=delay, total=z2m + delay)
        steps.append({"kind": "z2m", "at": z2m})
        steps.append({"kind": "offline", "at": z2m + delay, "off": "offline" in off})
    else:
        steps.append({"kind": "silent", "at": SILENT_SECONDS, "off": "silent" in off})
    every = record.get("report_every")
    if every:
        steps.append({"kind": "late", "at": int(every), "off": "late" in off})
    out["steps"] = sorted(steps, key=lambda s: s["at"])
    return out


def availability_mismatch(record: dict, summary: dict, options: dict, now: float) -> dict | None:
    """When the device's Zigbee2MQTT availability can't be right, given the messages it sends.

    Offline, yet MISMATCH_MESSAGES live messages came within the last MISMATCH_WINDOW, all after it
    went offline: the device is talking, so the availability topic is stale (typically a retained
    "offline" left from when availability was on). Online, yet silent for far longer than its usual
    gap, while Zigbee2MQTT either doesn't track its availability (so nothing updates the topic) or
    should have marked it offline by now (a battery device past the passive timeout). Mains devices
    Zigbee2MQTT pings are left alone: a ping keeps them online without any message.

    Devices with their own report interval are left to the Stopped reporting check. Returns
    {msg, params} or None.
    """
    if summary.get("network_down") or record.get("type") == "Coordinator" or record.get("status") != "active":
        return None
    state = record.get("availability")
    name = record.get("friendly_name") or record.get("ieee")
    topic = f"{record.get('instance')}/{name}/availability"
    if state == "offline":
        since = _age(record.get("availability_since"), now)
        if since is None or since < MISMATCH_SETTLE:
            return None
        went = now - since
        recent = [t for t in summary.get("recent_messages") or [] if t > went and now - t <= MISMATCH_WINDOW]
        if len(recent) < MISMATCH_MESSAGES:
            return None
        return {
            "msg": "availability_mismatch_offline",
            "params": {"seconds": int(since), "n": len(recent), "ago": int(now - max(recent)), "topic": topic},
        }
    if state != "online" or record.get("report_every"):
        return None
    timing = offline_timing(record, summary.get("z2m_availability"), options, now)
    if timing is None:
        return None
    quiet = _age(record.get("last_seen"), now)
    # Messages that arrived while the app was down don't count against the device.
    quiet = min(quiet, now - summary.get("started", 0.0)) if quiet is not None else None
    if quiet is None:
        return None
    rate = summary.get("msg_rate_prev")
    gap = 3600 / rate if _finite(rate) and rate > 0 else None
    if timing["tracked"]:
        if timing["mode"] == "active" or quiet < timing["z2m"] + 3600:
            return None
        why = "passive"
    else:
        why = "untracked"
    need = max(MISMATCH_QUIET, MISMATCH_GAPS * gap) if gap else SILENT_SECONDS
    if quiet < need:
        return None
    params: dict[str, Any] = {"seconds": int(quiet), "topic": topic}
    if gap:
        params["gap"] = int(gap)
    return {"msg": f"availability_mismatch_{why}", "params": params}


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
    critical = bool(record.get("critical"))

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
        if critical:
            issue["critical"] = True
        issues.append(issue)

    offline_minutes = max(0, int(options.get("offline_alert_minutes", 60)))
    if critical:
        # Critical devices use their own, usually shorter, delay.
        offline_minutes = max(0, int(options.get("critical_offline_minutes", DEFAULT_CRITICAL_OFFLINE_MINUTES)))
    mismatch = None if "availability_mismatch" in muted else availability_mismatch(record, summary, options, now)
    if mismatch:
        p = mismatch["params"]
        if mismatch["msg"] == "availability_mismatch_offline":
            detail = (
                f"Availability says offline for {_span(p['seconds'])}, but {p['n']} messages came in the last "
                f"{_span(MISMATCH_WINDOW)}. Likely a stale retained {p['topic']}."
            )
        elif mismatch["msg"] == "availability_mismatch_untracked":
            detail = (
                f"Availability says online, but no message for {_span(p['seconds'])}. Zigbee2MQTT doesn't track "
                f"this device's availability, so the online is likely a stale retained {p['topic']}."
            )
        else:
            detail = (
                f"Availability says online, but no message for {_span(p['seconds'])}, past Zigbee2MQTT's own "
                f"timeout for battery devices. Likely a stale retained {p['topic']}."
            )
        add("availability_mismatch", INFO, "Availability and messages disagree", detail, mismatch["msg"], **p)
    if record.get("availability") == "offline" and not (mismatch and mismatch["msg"].endswith("_offline")):
        # A device that keeps sending messages isn't offline: the mismatch above says so instead.
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

    frozen_hours = max(0, limit_of(record, options, "frozen_hours"))
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

    odd = odd_reading_issue(record, options, now)
    if odd:
        add("impossible_reading", WARNING, odd["title"], odd["detail"], odd["msg"], **odd["params"])

    # Saved metrics from before input was checked can hold NaN or infinity: treat those as no data.
    lqi_24h = summary.get("lqi_24h") if _finite(summary.get("lqi_24h")) else None
    lqi_prev = summary.get("lqi_prev_7d") if _finite(summary.get("lqi_prev_7d")) else None
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

    battery = record.get("battery") if _finite(record.get("battery")) else None
    threshold = limit_of(record, options, "battery_percent")
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

    hops = summary.get("router_hops") or {}
    if hops.get("changes", 0) >= HOP_CHANGES:
        lo, hi = hops.get("lqi_min"), hops.get("lqi_max")
        quality = "" if lo is None else f" · link quality {lo}" if lo == hi else f" · link quality {lo} to {hi}"
        add(
            "router_hopping",
            INFO,
            "Changes router often",
            f"Changed router {hops['changes']} times in {HOP_DAYS} days{quality}.",
            "router_hopping",
            n=hops["changes"],
            days=HOP_DAYS,
            lqi_min=lo,
            lqi_max=hi,
        )
    slow = summary.get("ping_slow")
    if slow:
        add(
            "slow_router",
            INFO,
            "Router getting slower",
            f"Getting slower: {slow['from']} ms to {slow['to']} ms in 2 weeks. Often a failing plug or too many devices on one router.",
            "slow_router",
            from_ms=slow["from"],
            to_ms=slow["to"],
        )

    overdue = summary.get("maintenance_overdue") or []
    if overdue:
        shown = ", ".join(task["title"] for task in overdue[:3]) + ("…" if len(overdue) > 3 else "")
        days = overdue[0]["days"]
        add(
            "maintenance_due",
            INFO,
            "Maintenance overdue",
            f"Overdue: {shown}. Due {days} day{'s' if days != 1 else ''} ago.",
            "maintenance_due",
            titles=shown,
            n=days,
        )
    cmd = summary.get("cmd") or {}
    slow_cmd = cmd.get("slow")
    if slow_cmd and slow_cmd.get("rise"):
        add(
            "slow_commands",
            INFO,
            "Slower to respond",
            f"Typical reply to commands {slow_cmd['median_ms']} ms over the last 24 h, against its usual {slow_cmd['usual_ms']} ms ({slow_cmd['n']} commands). Often a weaker link or a busy router on the way.",
            "slow_commands_rise",
            ms=slow_cmd["median_ms"],
            usual_ms=slow_cmd["usual_ms"],
            n=slow_cmd["n"],
        )
    elif slow_cmd:
        add(
            "slow_commands",
            INFO,
            "Slow to respond",
            f"Typical reply to commands {slow_cmd['median_ms']} ms over the last 24 h ({slow_cmd['n']} commands). Often a weak link or a busy router on the way.",
            "slow_commands",
            ms=slow_cmd["median_ms"],
            n=slow_cmd["n"],
        )
    missed_cmd = cmd.get("missed_share")
    if missed_cmd:
        add(
            "missed_commands",
            WARNING,
            "Misses commands",
            f"{missed_cmd['missed']} of {missed_cmd['n']} commands in the last 24 h got no reply within 10 s ({missed_cmd['percent']}%). Usually a weak link or a router that drops messages.",
            "missed_commands",
            missed=missed_cmd["missed"],
            n=missed_cmd["n"],
            percent=missed_cmd["percent"],
        )
    battery = summary.get("battery_device")
    if battery is None:
        battery = record.get("power_source") == "Battery" or _finite(record.get("battery"))
    result = reporting_audit(
        record.get("reportings"),
        record.get("reporting_inputs"),
        record.get("features"),
        bool(battery),
        summary.get("msg_rate_6h") if _finite(summary.get("msg_rate_6h")) else None,
    )
    found = reporting_findings(result)
    if found:
        names = reporting_summary(found)
        n = len(found)
        backed = f" It sent about {result['rate']} messages an hour over the last 6 h." if result["confirmed"] else ""
        add(
            "reporting_config",
            INFO,
            "Reporting settings",
            f"{n} reporting setting{'s' if n != 1 else ''} to check: {names}.{backed} The device's Health tab has recommended values.",
            "reporting_config_rate" if backed else "reporting_config",
            n=n,
            names=names,
            rate=result["rate"],
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

    retained = summary.get("retained_commands") or []
    if retained:
        first = retained[0]
        more = len(retained) - 1
        add(
            "retained_command",
            WARNING,
            "Retained command",
            f"{first['topic']} holds {first['payload']}"
            + (f" (and {more} more)" if more else "")
            + ". The broker sends it again after every restart, so the device repeats it."
            " Usually an automation or Node-RED flow that publishes with retain on.",
            "retained_command",
            topic=first["topic"],
            payload=first["payload"],
            n=len(retained),
            topics=[r["topic"] for r in retained],
        )

    return issues


def _parent_of(record: dict | None) -> str | None:
    return (((record or {}).get("routing") or {}).get("parent") or {}).get("ieee")


def _names(names: list[str]) -> str:
    """'A', 'A and B', 'A, B and C', 'A, B, C and 3 more'."""
    shown = names[:3]
    more = len(names) - len(shown)
    if more:
        return f"{', '.join(shown)} and {more} more"
    return " and ".join([", ".join(shown[:-1]), shown[-1]]) if len(shown) > 1 else "".join(shown)


def group_router_outages(issues: list[dict], devices: dict[str, dict], options: dict, now: float) -> None:
    """Fold the offline issues of devices behind an offline router into that router's issue.

    A device is behind a router when the router is its parent in the routing scan that last saw it,
    or the parent's parent and so on while each of them is offline too. Its offline issue stays (HA
    entities read it) but gets `behind` = the highest offline router in that chain, so alerts skip it
    and the sidebar nests it. It counts from the moment the router drops, before the router's own
    offline delay passes, so a device never alerts first and the router after it. The router's issue,
    once it has one, lists the devices and gets the grouped text. Changes the issues in place.
    """
    if "offline" in set(options.get("disabled_checks") or []):
        return

    def down(ieee: str) -> bool:
        record = devices.get(ieee) or {}
        if record.get("type") != "Router" or record.get("status") != "active":
            return False
        if "offline" in active_mutes(record, now):
            return False
        if record.get("availability") == "offline":
            return True
        age = _age(record.get("availability_since"), now)
        return record.get("availability") == "online" and age is not None and age < ROUTER_BACK_GRACE

    def top(ieee: str) -> str | None:
        seen = {ieee}
        found = None
        parent = _parent_of(devices.get(ieee))
        while parent and parent not in seen and down(parent):
            seen.add(parent)
            found = parent
            parent = _parent_of(devices.get(parent))
        return found

    groups: dict[str, list[dict]] = {}
    for issue in issues:
        if issue["kind"] != "offline":
            continue
        router = top(issue["ieee"])
        if router:
            issue["behind"] = router
            groups.setdefault(router, []).append(issue)
    by_key = {issue["key"]: issue for issue in issues}
    for router, behind in groups.items():
        issue = by_key.get(f"offline:{router}")
        if issue is None or issue.get("behind"):
            continue
        behind.sort(key=lambda i: str(i.get("name") or i["ieee"]).lower())
        names = [str(i.get("name") or i["ieee"]) for i in behind]
        n = len(names)
        issue["title"] = "Router offline"
        issue["detail"] = (
            f"{n} device{'s' if n != 1 else ''} that route{'' if n != 1 else 's'} through it "
            f"{'are' if n != 1 else 'is'} offline too: {_names(names)}. Check its power first."
        )
        issue["msg"] = "router_offline"
        issue["params"] = {**(issue.get("params") or {}), "n": n, "names": names[:3], "more": max(0, n - 3)}
        issue["devices"] = [i["ieee"] for i in behind]


def group_power_loss(issues: list[dict], devices: dict[str, dict], options: dict, now: float) -> None:
    """Fold the offline issues of mains devices in one area that dropped together into one issue.

    POWER_MIN_DEVICES or more mains devices in the same Home Assistant area, offline now, that went
    offline within POWER_WINDOW seconds of each other while at least one battery device in that area is
    still online: most likely the power there went off, not the mesh. Runs after group_router_outages,
    so devices already folded into a router's issue (and the router issues themselves) are left out:
    the router grouping wins. Counts from the moment the devices drop, before their offline delay
    passes, like the router grouping. The device that dropped first leads: its issue gets
    `power_area`, `power_devices` and the "Power loss in <area>?" text; the others get `behind` = it,
    so they don't alert on their own and the sidebar nests them. Changes the issues in place.
    """
    if "offline" in set(options.get("disabled_checks") or []):
        return
    offline_issue = {i["ieee"]: i for i in issues if i["kind"] == "offline"}
    by_area: dict[str, list[tuple[float, str]]] = {}
    batteries_up: dict[str, int] = {}
    for ieee, record in devices.items():
        area = record.get("area")
        if not area or record.get("status") != "active" or record.get("type") == "Coordinator":
            continue
        if record.get("availability") != "offline":
            if is_battery(record) and record.get("availability") is not None:
                batteries_up[area] = batteries_up.get(area, 0) + 1
            continue
        if not is_mains(record) or "offline" in active_mutes(record, now):
            continue
        issue = offline_issue.get(ieee)
        if issue is not None and (issue.get("behind") or issue.get("devices")):
            continue
        age = _age(record.get("availability_since"), now)
        if age is None:
            continue
        by_area.setdefault(area, []).append((now - age, ieee))
    for area, drops in by_area.items():
        if len(drops) < POWER_MIN_DEVICES or not batteries_up.get(area):
            continue
        drops.sort()
        cluster: list[tuple[float, str]] = []
        start = 0
        for end in range(len(drops)):
            while drops[end][0] - drops[start][0] > POWER_WINDOW:
                start += 1
            if end - start + 1 > len(cluster):
                cluster = drops[start : end + 1]
        if len(cluster) < POWER_MIN_DEVICES:
            continue
        lead = cluster[0][1]
        others = [ieee for _ts, ieee in cluster[1:]]
        for ieee in others:
            if ieee in offline_issue:
                offline_issue[ieee]["behind"] = lead
        issue = offline_issue.get(lead)
        if issue is None:
            continue
        names = sorted(
            (_display(devices.get(ieee) or {}) or ieee for _ts, ieee in cluster),
            key=str.lower,
        )
        n = len(cluster)
        spread = round(cluster[-1][0] - cluster[0][0])
        issue["title"] = "Power loss?"
        issue["detail"] = (
            f"{n} mains devices in {area} went offline within {spread} s of each other: {_names(names)}. "
            "Battery devices there are still online, so the Zigbee mesh is fine. Check the power in that area first."
        )
        issue["msg"] = "power_loss"
        issue["params"] = {
            **(issue.get("params") or {}),
            "area": area,
            "n": n,
            "spread": spread,
            "names": names[:3],
            "more": max(0, n - 3),
        }
        issue["power_area"] = area
        issue["power_devices"] = others
        issue["notify_title"] = f"{'Critical: ' if issue.get('critical') else ''}Power loss in {area}?"
        issue["notify_message"] = issue["detail"]


def _display(record: dict) -> str | None:
    return record.get("friendly_name") or record.get("ha_name")


def nested(issue: dict, keys: set[str]) -> bool:
    """True when the issue is shown inside its router's grouped offline issue rather than on its own."""
    return bool(issue.get("behind")) and f"offline:{issue['behind']}" in keys


def alert_title(issue: dict) -> str:
    prefix = "Critical" if issue.get("critical") else "Zigbee"
    if issue.get("power_area"):
        return f"{'Critical: ' if issue.get('critical') else ''}Power loss in {issue['power_area']}?"
    if issue.get("devices"):
        return f"{'Critical: ' if issue.get('critical') else ''}Router offline: {issue['name']}"
    return f"{prefix}: {issue['name']} - {issue['label']}"


def alert_plan(issues: list[dict], enabled: set[str], sent: set[str]) -> tuple[list[dict], list[str], list[str]]:
    """Which issues to notify now, which keys count as notified afterwards, and which to dismiss.

    Devices behind an offline router never notify on their own: the router's alert covers them.
    One that already notified before its router went down stays notified, so it doesn't repeat.
    Once the router is back, a device still offline is a normal issue and notifies as usual.
    """
    current = {issue["key"]: issue for issue in issues if issue["kind"] in enabled}
    quiet = {key for key, issue in current.items() if issue.get("behind")}
    to_send = [current[key] for key in sorted(set(current) - quiet - sent)]
    alerted = sorted((set(current) - quiet) | (quiet & sent))
    dismiss = sorted(sent - set(current))
    return to_send, alerted, dismiss


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
