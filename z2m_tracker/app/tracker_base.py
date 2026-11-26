"""Constants and helpers of the history store (tracker.py): cleaning input, records, dates, files."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from datetime import date as date_cls
from typing import Any

from health import (
    mutes_of,
)
from history_import import IEEE_RE
from odd_readings import clean as clean_odd_readings
from payload_keys import clean as clean_payload_keys
from reporting import clean as clean_reportings
from reporting import clean_sent as clean_reporting_sent

_LOGGER = logging.getLogger("z2m_tracker")

MAX_EVENTS = 3000
# When the event list is full, a device's events of one kind beyond this many go first (trim_events).
EVENTS_PER_KIND = 50
# Checks that compare a reading with a limit: once one has cleared, the same alert stays quiet for
# alert_repeat_hours (plan_alerts).
THRESHOLD_KINDS = frozenset({"battery_low", "weak_signal", "signal_drop"})
# Changes that only move on what a device last said (last seen, readings, report counts) are written
# at most this often; everything else is written at the next save. The whole file is rewritten each
# time, so on a network where something reports every few seconds this is what spares the SD card.
# A power cut loses at most this much of those values; a normal stop saves everything.
SOFT_SAVE_SECONDS = 600
MAX_OUTAGES = 200
# "Left the group" facts kept in history.json, and how long the Groups card shows one.
MAX_GROUP_LEFT = 100
# Retained /set topics remembered (memory only), and how much of each payload is kept for display.
MAX_RETAINED_COMMANDS = 500
RETAINED_COMMAND_PREVIEW = 120
GROUP_LEFT_DAYS = 30
STATUS_ACTIVE = "active"
STATUS_REMOVED = "removed"
# Availability messages kept per network for names not in its device list yet.
MAX_UNLISTED_AVAILABILITY = 200
# App options the sidebar never needs and must not receive.
SECRET_OPTIONS = frozenset({"mqtt_password", "metrics_token"})

# Longest pause for alerts given in minutes (30 days).
MAX_PAUSE_MINUTES = 43200

# Measurements that should drift on their own; used for frozen-reading detection.
MEASURE_KEYS = ("temperature", "local_temperature", "humidity", "pressure", "co2", "voc", "pm25", "soil_moisture")

# Quiet hours: most lines per section of the morning digest, and most held event alerts kept.
DIGEST_LINES = 10
MAX_HELD_EVENTS = 50
CLOCK_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
# Zigbee2MQTT process memory from bridge/health: one point per local day (that day's highest reading).
MEMORY_DAYS = 14
MEMORY_GROWTH = 0.5  # flag growth above 50 %
MEMORY_WINDOW_DAYS = 7  # over the last 7 days
MEMORY_MIN_DAYS = 5  # with at least 5 days of data in that window

ASSET_FIELDS = ("purchase_date", "shop", "price", "warranty_until", "location", "serial")
UNKNOWN_BATTERY_TYPE = "Unknown type"
# Maintenance tasks (record["maintenance"]): recurring physical jobs such as testing a smoke alarm.
# Longest interval per unit, all about 10 years (3650 days).
MAINTENANCE_UNITS = {"days": 3650, "weeks": 521, "months": 120, "years": 10}
MAX_TASKS = 20
MAX_TASK_TITLE = 80
MAX_TASK_NOTE = 500
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TASK_ID_RE = re.compile(r"^[0-9a-f]{8}$")
MAX_STOCK = 9999
# Warranties (asset "warranty_until"): Upkeep lists those ending in the next WARRANTY_AHEAD_DAYS and
# those that ended in the last WARRANTY_ENDED_DAYS. Health flags one ending within WARRANTY_SOON_DAYS.
WARRANTY_AHEAD_DAYS = 60
WARRANTY_ENDED_DAYS = 90
WARRANTY_SOON_DAYS = 30
# What uses a device in HA (automations, scripts, scenes): looked up again when a new issue
# opens and the last answer is older than this; at most this many devices per lookup.
USAGE_MAX_AGE = 600
USAGE_LIMIT = 50
USAGE_KEYS = ("automations", "scripts", "scenes")
# Names in a notification's "Used by" line before it says "+N more", and their longest length.
USAGE_LINE_NAMES = 3
USAGE_NAME_MAX = 40
# A battery change recorded within this many days of one that already took from stock is
# the same change seen twice (the app detecting the jump, then someone ticking it), so it
# doesn't take again. Changes dated further back than this are history being filled in
# and leave today's stock alone.
STOCK_WINDOW_DAYS = 3
# Battery brand: short free text on each replacement ("Panasonic", "IKEA Ladda").
MAX_BRAND = 40
# A brand's average life is only shown once it has this many completed intervals.
BRAND_MIN_INTERVALS = 2
# Saved brands offered in the sidebar's brand list.
MAX_BRANDS = 50

# Other parts of history.json a backup may bring back, with the type each must have.
RESTORE_KEYS = {
    "alerts_initialised": bool,
    "battery_brand_list": list,
    "battery_prices": dict,
    "battery_stock": dict,
    "bridges": dict,
    "group_left": list,
    "notify_offered": list,
    "ota_failures": dict,
    "ota_history": list,
    "ota_last_check": (str, int, float),
    "summary_sent": (str, dict),
    "daily_summary": dict,
    "z2m_config": dict,
    "dismissed_advice": list,
    "upgrades": dict,
    "firmware_notes": list,
    "report_profile_defaults": dict,
    "mesh_scores": dict,
    "group_history": dict,
    "group_state": dict,
}
# Alert state that is about now, not about when a backup was made: a restore keeps what is loaded.
RESTORE_KEEPS = ("alerts_paused", "quiet", "discovery_topics", "escalation", "alert_actions", "alert_cleared")

# Settings advice on the Health tab: one-off notices, each dismissible for good (data["dismissed_advice"]).
ADVICE_KINDS = frozenset({"last_seen_off"})
LAST_SEEN_TEXT = (
    "Silent checks for battery devices are weaker without it: messages Zigbee2MQTT repeats from its "
    "cache look new, so a device that has gone quiet can still look alive."
)

# Anything on the broker can publish under the base topic, so incoming text and numbers are checked
# before they are kept: text must be a string (control characters removed, length capped), numbers
# must be finite, and IEEE addresses must look like one.
MAX_TEXT = 256
MAX_DETAIL = 1000
MAX_NAMES = 50
# Biggest MQTT payload read; a large network's bridge/devices list is a few MB.
MAX_PAYLOAD = 16 * 1024 * 1024
# A command (<name>/set) with no state message from the device within this many seconds counts as missed.
COMMAND_TIMEOUT = 10.0
# Commands closer together than this (a dimmer slider, a double tap) are one burst with one reply.
COMMAND_BURST = 2.0
# A device list that drops more than half of a network's devices (with at least this many) is only
# applied when the same list arrives again at least SHRINK_HOLD_SECONDS later, so a stray or spoofed
# message (or a quick burst of them) can't empty the inventory and remove the HA entities.
SHRINK_MIN_DEVICES = 4
SHRINK_HOLD_SECONDS = 300
# Event types that may add a device the tracker hasn't seen yet.
EVENT_TYPES = ("device_joined", "device_interview", "device_announce", "device_leave")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
TEXT_FIELDS = (
    "friendly_name",
    "type",
    "model",
    "vendor",
    "description",
    "model_id",
    "manufacturer",
    "power_source",
    "software_build_id",
    "date_code",
    "interview_state",
    "ha_name",
    "area",
)


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    """A clean string from untrusted input, or None when it isn't text."""
    if not isinstance(value, str):
        return None
    return _CONTROL_RE.sub(" ", value).strip()[:limit] or None


def _brand_list(values: Any) -> list[str]:
    """Brand names cleaned, without repeats (ignoring case), at most MAX_BRANDS (the first ones), sorted."""
    seen: dict[str, str] = {}
    for value in values if isinstance(values, list) else []:
        brand = _text(value, MAX_BRAND)
        if brand and brand.casefold() not in seen and len(seen) < MAX_BRANDS:
            seen[brand.casefold()] = brand
    return sorted(seen.values(), key=str.casefold)


def base_topic_list(values: Any) -> list[str]:
    """Base topics from the app options, kept exactly as written apart from outer slashes.

    MQTT topics may start or end with spaces, and Zigbee2MQTT uses its base_topic as given, so a
    base topic like "Z2MQTT " (trailing space) must stay as it is. Blank entries and repeats are dropped.
    """
    topics: list[str] = []
    for value in values if isinstance(values, list) else []:
        topic = str(value).strip("/") if isinstance(value, str) else ""
        if topic.strip() and topic not in topics:
            topics.append(topic)
    return topics or ["zigbee2mqtt"]


def _ieee(value: Any) -> str | None:
    ieee = value.strip().lower() if isinstance(value, str) else ""
    return ieee if IEEE_RE.match(ieee) else None


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not valid JSON")


def _loads(text: str | bytes) -> Any:
    """json.loads without NaN and Infinity, which Python accepts but browsers and HA don't."""
    return json.loads(text, parse_constant=_reject_constant)


def _json_safe(value: Any) -> Any:
    """A copy with every non-finite float replaced by None (for data saved before input was checked)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _dumps(value: Any, **kwargs: Any) -> str:
    try:
        return json.dumps(value, allow_nan=False, **kwargs)
    except ValueError:
        return json.dumps(_json_safe(value), allow_nan=False, **kwargs)


def _iso_date(value: Any) -> date | None:
    """A YYYY-MM-DD string as a date, or None."""
    if not isinstance(value, str) or not DATE_RE.match(value):
        return None
    try:
        return date_cls.fromisoformat(value)
    except ValueError:
        return None


def add_interval(day: date, every: int, unit: str) -> date:
    """`day` plus `every` days, weeks, months or years. Months and years end on the last day of a short month."""
    if unit == "days":
        return day + timedelta(days=every)
    if unit == "weeks":
        return day + timedelta(weeks=every)
    months = every * 12 if unit == "years" else every
    year, month = divmod(day.month - 1 + months, 12)
    year += day.year
    month += 1
    last = (date_cls(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
    return date_cls(year, month, min(day.day, last))


def clean_task(raw: Any, today: date | None = None) -> tuple[dict | None, str | None]:
    """A maintenance task from untrusted input: (task, None) or (None, error).

    With `today`, a last-done date after today is refused (input from the UI); stored tasks skip that check.
    """
    if not isinstance(raw, dict):
        return None, "A task must be an object"
    title = _text(raw.get("title"), MAX_TASK_TITLE)
    if not title:
        return None, "Give the task a title"
    unit = raw.get("unit")
    if unit not in MAINTENANCE_UNITS:
        return None, "Unit must be days, weeks, months or years"
    every = raw.get("every")
    if isinstance(every, float) and every.is_integer():
        every = int(every)
    if isinstance(every, bool) or not isinstance(every, int) or not 1 <= every <= MAINTENANCE_UNITS[unit]:
        return None, f"Repeat every 1 to {MAINTENANCE_UNITS[unit]} {unit}"
    last_done = raw.get("last_done") or ""
    if last_done:
        done = _iso_date(last_done)
        if done is None:
            return None, "Last done must be a date like 2026-01-31"
        if today is not None and done > today:
            return None, "Last done can't be in the future"
    created = raw.get("created") if _iso_date(raw.get("created")) else None
    task_id = raw.get("id") if isinstance(raw.get("id"), str) and TASK_ID_RE.match(raw["id"]) else None
    note = raw.get("note")
    task = {
        "id": task_id or uuid.uuid4().hex[:8],
        "title": title,
        "every": every,
        "unit": unit,
        "last_done": last_done,
        "created": created or (today or datetime.now(timezone.utc).date()).isoformat(),
        "note": (_text(note, MAX_TASK_NOTE) or "") if note is not None else "",
    }
    return task, None


def warranty_end(record: dict) -> date | None:
    """The day a device's warranty ends, from its asset details. None when unset or not a date."""
    return _iso_date(((record.get("asset") or {}).get("warranty_until") or "").strip()[:10])


def task_due(task: dict) -> date:
    """Next due date: last done (or the day the task was added) plus its interval."""
    start = _iso_date(task.get("last_done")) or _iso_date(task.get("created")) or datetime.now(timezone.utc).date()
    return add_interval(start, task["every"], task["unit"])


def _clean_tasks(tasks: Any) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for raw in tasks if isinstance(tasks, list) else []:
        task, _ = clean_task(raw)
        if task and task["id"] not in seen:
            seen.add(task["id"])
            out.append(task)
    return out[:MAX_TASKS]


def _clean_record(record: dict) -> dict:
    """Fix the types in a stored or restored device record, so bad data can't break health or the UI."""
    for key in TEXT_FIELDS:
        if record.get(key) is not None:
            record[key] = _text(record[key])
    if record.get("z2m_description") is not None:
        record["z2m_description"] = _text(record["z2m_description"], MAX_DETAIL)
    names = record.get("names")
    record["names"] = [
        {**entry, "name": _text(entry["name"])}
        for entry in (names if isinstance(names, list) else [])
        if isinstance(entry, dict) and _text(entry.get("name"))
    ][-MAX_NAMES:]
    if not isinstance(record.get("note"), str):
        record["note"] = ""
    for key in ("lqi", "battery"):
        if record.get(key) is not None and not _finite(record[key]):
            record[key] = None
    for key, default in (("readings", {}), ("asset", {}), ("battery_info", {})):
        if not isinstance(record.get(key), dict):
            record[key] = default
    replacements = record["battery_info"].get("replacements")
    if replacements is not None:
        if not isinstance(replacements, list):
            replacements = []
        replacements = [entry for entry in replacements if isinstance(entry, dict)]
        for entry in replacements:
            if "brand" in entry:
                brand = _text(entry["brand"], MAX_BRAND)
                if brand:
                    entry["brand"] = brand
                else:
                    del entry["brand"]
        record["battery_info"]["replacements"] = replacements
    for key in ("sources", "outages", "predecessors", "lost_bindings", "features"):
        if not isinstance(record.get(key), list):
            record[key] = []
    # Mutes are {kind: until_iso or None}; older files stored a plain list of kinds.
    record["muted"] = {
        kind: until
        for kind, until in mutes_of(record if isinstance(record.get("muted"), (dict, list)) else {}).items()
        if isinstance(kind, str) and re.fullmatch(r"[a-z_]{2,30}", kind) and (until is None or isinstance(until, str))
    }
    record["maintenance"] = _clean_tasks(record.get("maintenance"))
    if "odd_readings" in record:
        record["odd_readings"] = clean_odd_readings(record["odd_readings"])
    if "payload_keys" in record:
        record["payload_keys"] = clean_payload_keys(record["payload_keys"])
    change = record.get("payload_change")
    if change is not None and not (
        isinstance(change, dict)
        and isinstance(change.get("at"), str)
        and all(isinstance(change.get(k), list) for k in ("added", "removed"))
    ):
        record["payload_change"] = None
    if "reportings" in record:
        record["reportings"] = clean_reportings(record["reportings"])
    if "reporting_sent" in record:
        record["reporting_sent"] = clean_reporting_sent(record["reporting_sent"])
    inputs = record.get("reporting_inputs")
    record["reporting_inputs"] = (
        {
            str(ep)[:64]: [c for c in clusters if isinstance(c, str)][:20]
            for ep, clusters in inputs.items()
            if isinstance(clusters, list)
        }
        if isinstance(inputs, dict)
        else {}
    )
    if record.get("instance") is not None and not isinstance(record["instance"], str):
        record["instance"] = None
    if record.get("status") not in (STATUS_ACTIVE, STATUS_REMOVED):
        record["status"] = STATUS_REMOVED
    return record


def _clean_scans(scans: list) -> list:
    """Saved routing scans with only IEEE addresses and numeric LQIs (older files were not checked)."""
    out = []
    for scan in scans:
        if not isinstance(scan, dict) or not isinstance(scan.get("at"), str):
            continue
        parents = {
            ieee: [p[0], p[1] if _finite(p[1]) else None]
            for ieee, p in (scan.get("parents") if isinstance(scan.get("parents"), dict) else {}).items()
            if _ieee(ieee) and isinstance(p, list) and len(p) >= 2 and _ieee(p[0])
        }
        links = [
            [link[0], link[1], link[2] if _finite(link[2]) else None]
            for link in (scan.get("links") if isinstance(scan.get("links"), list) else [])
            if isinstance(link, list) and len(link) >= 3 and _ieee(link[0]) and _ieee(link[1])
        ]
        out.append({"at": scan["at"], "parents": parents, "links": links})
    return out


def _csv_safe(row: list) -> list:
    """Quote text a spreadsheet would run as a formula (=, +, -, @, tab, CR), so device names can't."""
    return [
        f"'{cell}" if isinstance(cell, str) and cell[:1] in ("=", "+", "-", "@", "\t", "\r") else cell for cell in row
    ]


def trim_events(events: list[dict], limit: int = MAX_EVENTS, per_kind: int = EVENTS_PER_KIND) -> None:
    """Make room in a full event list (oldest first), in place.

    One device that keeps going offline and online would otherwise push every other device's
    history out. So the room comes first from the devices with more than per_kind events of one
    kind: their oldest of that kind go, never leaving fewer than per_kind. About a tenth of the list
    is freed that way at a time, so this doesn't run on every event. Only when no device has that
    many do the oldest events of all go, as before.
    """
    if len(events) <= limit:
        return

    def key(event: dict) -> tuple:
        return (event.get("instance"), event.get("ieee"), event.get("kind"))

    counts: dict[tuple, int] = {}
    for event in events:
        counts[key(event)] = counts.get(key(event), 0) + 1
    excess = {k: n - per_kind for k, n in counts.items() if n > per_kind}
    if excess:
        need = len(events) - (limit - limit // 10)
        total = sum(excess.values())
        # Each noisy device gives in proportion to how far over it is.
        drop = {k: min(n, -(-need * n // total)) for k, n in excess.items()}
        kept = []
        for event in events:
            k = key(event)
            if drop.get(k, 0) > 0:
                drop[k] -= 1
            else:
                kept.append(event)
        events[:] = kept
    if len(events) > limit:
        del events[: len(events) - limit]


def _write_atomic(path: str, payload: str, keep_previous: bool = False) -> None:
    """Write a file so a crash or power cut leaves the old one or the new one, never half of each.

    keep_previous keeps the file being replaced as <path>.bak: a rename, so nothing more is written."""
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    kept = False
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if keep_previous and os.path.exists(path):
            os.replace(path, f"{path}.bak")
            kept = True
        os.replace(tmp_path, path)
    except BaseException:
        if kept:
            # The new file never arrived: put the one just set aside back.
            try:
                os.replace(f"{path}.bak", path)
            except OSError:
                pass
        # A failed write (often a full disk) must not leave its half-written copy behind.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stamp_offline_since(record: dict) -> bool:
    """Give an offline device with no start time one, counting from now.

    Old data files can hold a device that is offline with no
    availability_since, so the offline alert never fired for it. Counting from
    when the tracker first saw it offline lets the alert threshold apply.
    """
    if record.get("availability") == "offline" and not record.get("availability_since"):
        record["availability_since"] = _now()
        return True
    return False


def _clock_minutes(value: Any) -> int | None:
    """Minutes after midnight for "HH:MM", or None."""
    match = CLOCK_RE.match(str(value or "").strip())
    return int(match[1]) * 60 + int(match[2]) if match else None


def _first_sentence(text: str) -> str:
    text = str(text or "").strip()
    cut = text.find(". ")
    return text[: cut + 1] if cut >= 0 else text


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _min_time(*values: str | None) -> str | None:
    present = [value for value in values if value]
    return min(present) if present else None


def _max_time(*values: str | None) -> str | None:
    present = [value for value in values if value]
    return max(present) if present else None


def _new_record(ieee: str, instance: str | None) -> dict[str, Any]:
    return {
        "ieee": ieee,
        "instance": instance,
        "friendly_name": None,
        "names": [],
        "type": None,
        "model": None,
        "vendor": None,
        "description": None,
        "model_id": None,
        "manufacturer": None,
        "power_source": None,
        "software_build_id": None,
        "date_code": None,
        "supported": None,
        "interview_state": None,
        "z2m_description": None,
        "features": [],
        "ha_name": None,
        "area": None,
        "status": STATUS_ACTIVE,
        "availability": None,
        "availability_since": None,
        "first_seen": None,
        "last_seen": None,
        "last_listed": None,
        "removed_at": None,
        "note": "",
        "sources": [],
        "lqi": None,
        "battery": None,
        "battery_low": None,
        "update": None,
        "readings": {},
        "outages": [],
        "routing": None,
        "asset": {},
        "predecessors": [],
        "successor": None,
        "muted": {},
        "battery_info": {},
        "bindings": None,
        "lost_bindings": [],
        "reportings": None,
        "reporting_inputs": {},
        "z2m_stats": None,
        "maintenance": [],
    }


# Data only Zigbee2MQTT provides. Cleared when a device moves between Zigbee2MQTT and ZHA, so no
# Zigbee2MQTT-only check runs on what the other stack left behind. History (names, outages, metrics) stays.
Z2M_ONLY_FIELDS = (
    "features",
    "software_build_id",
    "date_code",
    "supported",
    "interview_state",
    "z2m_description",
    "update",
    "routing",
    "bindings",
    "lost_bindings",
    "reportings",
    "reporting_inputs",
    "z2m_stats",
    "payload_keys",
    "payload_change",
    "reporting_sent",
)


def _list_label(instance: Any) -> str:
    """Short name of the stack whose device list a network comes from, for event text."""
    return {"zha": "ZHA", "matter": "Matter"}.get(_stack_of(instance), "Z2M")


def _stack_of(instance: Any) -> str:
    """Where a network is read from: "zha" and "matter" through Home Assistant, every base topic is
    Zigbee2MQTT ("z2m")."""
    return instance if instance in ("zha", "matter") else "z2m"


# Command values a reply has to echo before it counts as the answer; other keys only need to appear.
_ECHOED = ("state", "brightness", "color_temp", "position")
# Command options and relative moves: Zigbee2MQTT acts on them but never publishes them in the device
# state, so a reply can't show them. Expecting them made every such command count as missed (#37).
_NOT_ECHOED = frozenset(
    {
        "transition",
        "brightness_step",
        "brightness_step_onoff",
        "brightness_move",
        "brightness_move_onoff",
        "color_temp_step",
        "color_temp_move",
        "hue_step",
        "hue_move",
        "saturation_step",
        "saturation_move",
        "effect",
        "alert",
        "flash",
        "on_time",
        "off_wait_time",
    }
)


def _expected_reply(rest: str, text: str) -> dict:
    """What a device's reply to this /set must show: {key: value} or {key: None} (any value)."""
    if "/set/" in rest:
        key = rest.rsplit("/set/", 1)[1]
        try:
            value = json.loads(text)
        except ValueError:
            value = text.strip()
        if key in _NOT_ECHOED:
            return {}
        return {key: value if key in _ECHOED else None}
    parsed = _loads(text) if text[:1] == "{" else None
    if not isinstance(parsed, dict):
        return {}
    out = {}
    for key, value in list(parsed.items())[:10]:
        if key in _NOT_ECHOED:
            continue
        if key in _ECHOED and isinstance(value, (str, int, float)) and not isinstance(value, bool):
            # TOGGLE flips whatever the state was, so any state answers it.
            out[key] = None if isinstance(value, str) and value.upper() == "TOGGLE" else value
        elif isinstance(key, str):
            out[key] = None
    return out


def _reply_matches(expect: dict, state: Any) -> bool:
    if not expect:
        return True
    if not isinstance(state, dict):
        return False
    for key, value in expect.items():
        if key not in state:
            return False
        if value is None:
            continue
        got = state[key]
        if isinstance(value, str) and isinstance(got, str):
            if value.upper() != got.upper():
                return False
        elif isinstance(value, (int, float)) and isinstance(got, (int, float)):
            # Brightness and colour land near, not always on, the asked value.
            if abs(got - value) > max(2, abs(value) * 0.05):
                return False
        elif got != value:
            return False
    return True


def _set_current_name(record: dict, name: str | None, when: str) -> str | None:
    """Set the live friendly name. Returns the previous name when it changed."""
    if not name or record["friendly_name"] == name:
        if name and not any(entry["name"] == name for entry in record["names"]):
            record["names"].append({"name": name, "from": when, "until": None, "source": "Z2M"})
        return None
    previous = record["friendly_name"]
    for entry in record["names"]:
        if entry["name"] == previous and entry.get("until") is None:
            entry["until"] = when
    existing = next((entry for entry in record["names"] if entry["name"] == name), None)
    if existing:
        existing["until"] = None
    else:
        record["names"].append({"name": name, "from": when, "until": None, "source": "Z2M"})
    record["friendly_name"] = name
    if len(record["names"]) > MAX_NAMES:
        current = [entry for entry in record["names"] if entry["name"] == name]
        older = [entry for entry in record["names"] if entry["name"] != name]
        record["names"] = older[-(MAX_NAMES - len(current)) :] + current
    return previous or ""


def _add_historic_name(record: dict, name: str | None, when: str | None, source: str) -> bool:
    if not name or IEEE_RE.match(name.lower()):
        return False
    slug = _slug(name)
    for entry in record["names"]:
        if _slug(entry["name"]) == slug:
            if when and (not entry.get("from") or when < entry["from"]):
                entry["from"] = when
                return True
            return False
    record["names"].append({"name": name, "from": when, "until": None, "source": source, "historic": True})
    record["names"].sort(key=lambda entry: entry.get("from") or "")
    return True


def _bindings_of(device: dict) -> list[dict] | None:
    """Bindings from a bridge/devices entry, or None when Z2M didn't include endpoint data."""
    endpoints = device.get("endpoints")
    if not isinstance(endpoints, dict):
        return None
    out = []
    for ep, info in endpoints.items():
        for binding in (info or {}).get("bindings") or []:
            target = binding.get("target") or {}
            cluster = binding.get("cluster") or "?"
            if target.get("type") == "group":
                ident = f"group:{target.get('id')}"
            else:
                ident = f"{str(target.get('ieee_address') or '').lower()}/{target.get('endpoint')}"
            out.append({"key": f"{ep}:{cluster}>{ident}", "ep": str(ep), "cluster": cluster, "target": ident})
    return sorted(out, key=lambda b: b["key"])


BACKUP_MAX_AGE = 3 * 86400
BACKUP_REFRESH = 600
# Pairing (permit join) open longer than this raises pairing_open even when this app opened it.
PAIRING_OPEN_ALERT = 600
# A permit_join request seen this long before Z2M reports pairing open is taken as what opened it.
PERMIT_HINT_SECONDS = 60
PERMIT_ORIGIN_TEXT = {
    "frontend": "opened in the Zigbee2MQTT frontend",
    "mqtt": "opened from Home Assistant or an automation",
}


def _permit_opening(payload: Any) -> bool:
    """Whether a bridge/request/permit_join payload opens pairing (Z2M 2.x "time", 1.x "value")."""
    if isinstance(payload, dict):
        if "value" in payload:
            value = payload["value"]
            return value.strip().lower() == "true" if isinstance(value, str) else bool(value)
        payload = payload.get("time")
    if isinstance(payload, bool):
        return payload
    if isinstance(payload, str):
        if payload.strip().lower() in ("true", "false"):
            return payload.strip().lower() == "true"
    try:
        return int(payload) > 0
    except (TypeError, ValueError):
        return False


# Ping: the same read Z2M's availability check uses (genBasic zclVersion), sent through the
# bridge/request/action "raw" API (Zigbee2MQTT 2.7+) so the reply comes back with our transaction id.
PING_TIMEOUT = 10
PING_MIN_Z2M = (2, 7)
PING_SPACING = 2.0
PING_GRACE = 5


def _ping_address(device: dict) -> dict | None:
    """Network address and the endpoint to read genBasic from (first endpoint listing it, as herdsman does)."""
    nwk = device.get("network_address")
    if not isinstance(nwk, int) or isinstance(nwk, bool):
        return None
    endpoints = device.get("endpoints")
    ids: list[int] = []
    if isinstance(endpoints, dict):
        for key, info in endpoints.items():
            try:
                ep = int(key)
            except (TypeError, ValueError):
                continue
            inputs = ((info or {}).get("clusters") or {}).get("input") or []
            ids.append(ep)
            if "genBasic" in inputs:
                return {"nwk": nwk, "ep": ep}
    return {"nwk": nwk, "ep": ids[0] if ids else 1}


def _endpoint_clusters(device: dict) -> dict[str, set[str]]:
    """Each endpoint's clusters (input and output) from a bridge/devices entry, for fitting bindings to another model."""
    endpoints = device.get("endpoints")
    if not isinstance(endpoints, dict):
        return {}
    out = {}
    for key, info in endpoints.items():
        clusters = (info or {}).get("clusters") or {}
        out[str(key)] = {str(c) for c in (clusters.get("input") or []) + (clusters.get("output") or [])}
    return out


def _z2m_version(text: Any) -> tuple[int, int] | None:
    match = re.match(r"\s*(\d+)\.(\d+)", str(text or ""))
    return (int(match.group(1)), int(match.group(2))) if match else None


def friendly_z2m_error(raw: str | None) -> str:
    text = str(raw or "")
    low = text.lower()
    if "timeout" in low or "timed out" in low or "no response" in low:
        return "The device didn't respond. It's probably offline, out of range, or asleep if it runs on battery."
    if "not found" in low or "doesn't exist" in low or "does not exist" in low:
        return "Zigbee2MQTT doesn't know this device any more."
    if "delivery failed" in low or "mac_no_ack" in low or "no ack" in low:
        return "The message couldn't be delivered to the device. Check that it has power and is in range."
    return text[:300] or "Zigbee2MQTT reported an error."


def parent_rows(scans: list, ieee: str, since: str) -> list[dict]:
    """The device's parent in each stored scan from `since` on, oldest first.

    Scans where the device has no parent are skipped, not counted as a change. `changed` compares
    with the previous scan that had one, even when that scan is older than `since`.
    """
    rows: list[dict] = []
    previous = None
    for scan in scans:
        entry = (scan.get("parents") or {}).get(ieee)
        if not entry:
            continue
        parent = entry[0]
        lqi = entry[1] if len(entry) > 1 else None
        if (scan.get("at") or "") >= since:
            rows.append({"at": scan.get("at"), "ieee": parent, "lqi": lqi, "changed": previous not in (None, parent)})
        previous = parent
    return rows


def hop_summary(rows: list[dict]) -> dict:
    """Changes, link-quality range and best-link parent for parent_rows() output."""
    lqis = [r["lqi"] for r in rows if isinstance(r.get("lqi"), (int, float))]
    best = max((r for r in rows if isinstance(r.get("lqi"), (int, float))), key=lambda r: r["lqi"], default=None)
    return {
        "changes": sum(1 for r in rows if r["changed"]),
        "parents": len({r["ieee"] for r in rows}),
        "lqi_min": min(lqis) if lqis else None,
        "lqi_max": max(lqis) if lqis else None,
        "best": {"ieee": best["ieee"], "lqi": best["lqi"]} if best else None,
    }


def record_memory(days: list, today: date, mb: float) -> list:
    """Add a reading to the daily memory history ([date, max MB] pairs) and drop days older than MEMORY_DAYS."""
    key = today.isoformat()
    if days and days[-1][0] == key:
        days[-1][1] = max(days[-1][1], mb)
    else:
        days.append([key, mb])
    oldest = (today - timedelta(days=MEMORY_DAYS - 1)).isoformat()
    return [d for d in days if d[0] >= oldest][-MEMORY_DAYS:]


def memory_growth(days: list, today: date, current: float | None = None) -> dict | None:
    """{percent, days} when Zigbee2MQTT's memory grew more than MEMORY_GROWTH over the last week, else None.

    Compares the oldest day in the window with the current reading (or the latest day), so a restart that
    frees the memory clears the flag straight away.
    """
    start = (today - timedelta(days=MEMORY_WINDOW_DAYS - 1)).isoformat()
    window = [d for d in days if isinstance(d, list) and len(d) >= 2 and d[0] >= start and _finite(d[1])]
    if len(window) < MEMORY_MIN_DAYS or window[0][1] <= 0:
        return None
    first = window[0][1]
    last = current if _finite(current) and current > 0 else window[-1][1]
    growth = (last - first) / first
    if growth <= MEMORY_GROWTH:
        return None
    span_days = (today - date.fromisoformat(window[0][0])).days
    return {"percent": round(growth * 100), "days": max(1, span_days)}


def _pairing_step(event_type: str, status: str | None) -> str | None:
    """Where a device that joined during pairing has got to: joined, announced, interviewing, successful, failed."""
    if event_type == "device_joined":
        return "joined"
    if event_type == "device_announce":
        return "announced"
    if event_type == "device_interview":
        return {"started": "interviewing", "successful": "successful", "failed": "failed"}.get(status or "")
    return None


def _display_name(record: dict) -> str | None:
    if record.get("friendly_name") and not IEEE_RE.match(record["friendly_name"].lower()):
        return record["friendly_name"]
    named = [n["name"] for n in record.get("names") or [] if n.get("name") and not IEEE_RE.match(n["name"].lower())]
    if named:
        return named[-1]
    return record.get("ha_name")


def shopping_text(groups: list[dict], days: int = 60) -> str:
    """One line for the weekly summary. Says "to buy" for types with stock entered, "needed" otherwise."""
    if not groups:
        return ""
    if all(g.get("stock") is None for g in groups):
        return f"Batteries for the next {days} days: " + ", ".join(f"{g['count']}× {g['type']}" for g in groups) + "."
    parts = []
    for g in groups:
        if g.get("stock") is None:
            parts.append(f"{g['count']}× {g['type']} needed")
        elif g["to_buy"]:
            parts.append(f"{g['to_buy']}× {g['type']} to buy ({g['stock']} in stock)")
        else:
            parts.append(f"{g['type']} covered by stock")
    return f"Batteries for the next {days} days: " + ", ".join(parts) + "."
