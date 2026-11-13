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
    ("joined", "Device joined", "A device joins one of your networks.", True),
    ("left", "Device left", "A device leaves one of your networks.", True),
    ("interview_failed", "Interview failed", "A newly joined device couldn't be identified.", True),
]
DEFAULT_NOTIFY = sorted(ALERT_KINDS | {"joined", "left", "interview_failed"})


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
    "coordinator_backup": "Coordinator backup",
    "z2m_memory": "Memory climbing",
}


def offer_new_kinds(notify_checks: list[str], offered: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Switch each NEW_NOTIFY_KINDS entry on once. Returns (notify_checks, offered, added)."""
    new = [kind for kind in NEW_NOTIFY_KINDS if kind not in offered]
    added = [kind for kind in new if kind not in notify_checks]
    return sorted(set(notify_checks) | set(added)), sorted(set(offered) | set(new)), added


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


def nested(issue: dict, keys: set[str]) -> bool:
    """True when the issue is shown inside its router's grouped offline issue rather than on its own."""
    return bool(issue.get("behind")) and f"offline:{issue['behind']}" in keys


def alert_title(issue: dict) -> str:
    prefix = "Critical" if issue.get("critical") else "Zigbee"
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
