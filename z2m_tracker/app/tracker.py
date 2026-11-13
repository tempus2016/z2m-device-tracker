"""Device tracking core for the Z2M Device Tracker app (thread-safe, no HA deps)."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone, tzinfo
from datetime import time as dtime
from typing import Any

from health import (
    HOP_CHANGES,
    HOP_DAYS,
    REPORT_EVERY_MAX,
    REPORT_EVERY_MIN,
    clean_limits,
    device_issues,
    group_router_outages,
    late_deadline,
    limit_of,
    mutes_of,
    notify_kinds,
    offline_timing,
    uptime,
    z2m_availability,
)
from history_import import IEEE_RE, _features, collect_history
from metrics import Metrics

_LOGGER = logging.getLogger("z2m_tracker")

MAX_EVENTS = 3000
MAX_OUTAGES = 200
# "Left the group" facts kept in history.json, and how long the Groups card shows one.
MAX_GROUP_LEFT = 100
GROUP_LEFT_DAYS = 30
STATUS_ACTIVE = "active"
STATUS_REMOVED = "removed"

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
        "muted": [],
        "battery_info": {},
        "bindings": None,
        "lost_bindings": [],
        "z2m_stats": None,
    }


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
    window = [d for d in days if d[0] >= start and isinstance(d[1], (int, float))]
    if len(window) < MEMORY_MIN_DAYS or window[0][1] <= 0:
        return None
    first = window[0][1]
    last = current if isinstance(current, (int, float)) and current > 0 else window[-1][1]
    growth = (last - first) / first
    if growth <= MEMORY_GROWTH:
        return None
    span_days = (today - date.fromisoformat(window[0][0])).days
    return {"percent": round(growth * 100), "days": max(1, span_days)}


def _display_name(record: dict) -> str | None:
    if record.get("friendly_name") and not IEEE_RE.match(record["friendly_name"].lower()):
        return record["friendly_name"]
    named = [n["name"] for n in record.get("names") or [] if n.get("name") and not IEEE_RE.match(n["name"].lower())]
    if named:
        return named[-1]
    return record.get("ha_name")


class Tracker:
    """Keeps a persistent history of every device seen on Zigbee2MQTT."""

    def __init__(
        self,
        data_path: str,
        base_topics: list[str],
        config_dir: str,
        local_tz: tzinfo,
        metrics: Metrics,
        options: dict,
        oui: dict[str, str],
    ) -> None:
        self.data_path = data_path
        self.base_topics = base_topics
        self.config_dir = config_dir
        self.local_tz = local_tz
        self.metrics = metrics
        self.options = options
        self.oui = oui
        self.lock = threading.RLock()
        self.data: dict[str, Any] = {"devices": {}, "events": [], "imported": None, "networkmap": {}, "alerted": []}
        self.names: dict[str, dict[str, str]] = {}
        self.lists_received: set[str] = set()
        self.lists_ready = threading.Event()
        self.dirty = False
        self.boot_id = uuid.uuid4().hex[:8]
        self.revision = 0
        self.publish: Callable[[str, dict], None] | None = None
        self.on_event: Callable[[dict], None] | None = None
        self.issues: list[dict] = []
        self.ha_device_ids: dict[str, str] = {}
        self.ha_battery: dict[str, dict] = {}
        self.ha_leftovers: list[dict] = []
        self._pending: dict[str, dict] = {}
        self.battery_lib = None
        self.permit: dict[str, dict] = {}
        self.scans: dict[str, list] = {}
        self.scans_path: str | None = None
        self.pairing_sessions: dict[str, dict] = {}
        # Per base topic while pairing is open: {"since", "origin" (app/mqtt/frontend/None), "device"}.
        self.permit_meta: dict[str, dict] = {}
        self._permit_hint: dict[str, dict] = {}
        self.groups: dict[str, dict[str, str]] = {}
        # Current member IEEEs per group per base topic (memory only; bridge/groups is retained).
        self.group_members: dict[str, dict[str, list[str]]] = {}
        self._unbinds: dict[str, float] = {}
        self._spof_cache: dict[str, tuple] = {}
        self._backups: dict[str, dict] = {}
        self._backups_at = 0.0
        self.started = time.time()
        self._late_checked: dict[str, float] = {}
        self._hours: dict[str, dict] = {}
        self._hours_at: int | None = None

    # ------------------------------------------------------------ persistence

    def load(self, migrate_from: str | None = None) -> str:
        source = "new"
        stored = None
        if os.path.exists(self.data_path):
            with open(self.data_path, encoding="utf-8") as handle:
                stored = json.load(handle)
            source = "saved"
        elif migrate_from and os.path.exists(migrate_from):
            with open(migrate_from, encoding="utf-8") as handle:
                wrapper = json.load(handle)
            stored = wrapper.get("data") if isinstance(wrapper, dict) and "data" in wrapper else wrapper
            source = "migrated"
        with self.lock:
            if isinstance(stored, dict):
                self.data["events"] = list(stored.get("events") or [])
                self.data["imported"] = stored.get("imported")
                self.data["networkmap"] = stored.get("networkmap") or {}
                self.data["alerted"] = list(stored.get("alerted") or [])
                for ieee, record in (stored.get("devices") or {}).items():
                    self.data["devices"][ieee] = {**_new_record(ieee, record.get("instance")), **record}
                for key, value in stored.items():
                    if key not in ("devices", "events", "imported", "networkmap", "alerted"):
                        self.data[key] = value
            for record in self.data["devices"].values():
                if _stamp_offline_since(record):
                    self.dirty = True
                if record["status"] == STATUS_ACTIVE and record["friendly_name"] and record["instance"]:
                    self.names.setdefault(record["instance"], {})[record["friendly_name"]] = record["ieee"]
            if source == "migrated":
                self.dirty = True
        return source

    def save(self, force: bool = False) -> bool:
        with self.lock:
            if not self.dirty and not force:
                return False
            payload = json.dumps(self.data, ensure_ascii=False)
            self.dirty = False
        tmp_path = f"{self.data_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_path, self.data_path)
        return True

    def _changed(self, structural: bool) -> None:
        self.dirty = True
        if structural:
            self.revision += 1

    @property
    def version(self) -> str:
        return f"{self.boot_id}-{self.revision}"

    # ------------------------------------------------------------------- MQTT

    def handle_message(self, base_topic: str, topic: str, payload: bytes) -> None:
        rest = topic[len(base_topic) + 1 :]
        text = payload.decode("utf-8", errors="ignore")
        try:
            with self.lock:
                if rest.startswith("bridge/"):
                    if rest == "bridge/devices":
                        parsed = json.loads(text)
                        if isinstance(parsed, list):
                            self._handle_devices(base_topic, parsed)
                    elif rest == "bridge/event":
                        parsed = json.loads(text)
                        if isinstance(parsed, dict):
                            self._handle_event(base_topic, parsed)
                    elif rest.startswith("bridge/response/"):
                        parsed = json.loads(text)
                        if isinstance(parsed, dict):
                            self._handle_response(base_topic, rest[len("bridge/response/") :], parsed)
                    elif rest == "bridge/info":
                        parsed = json.loads(text)
                        if isinstance(parsed, dict):
                            self._handle_info(base_topic, parsed)
                    elif rest == "bridge/state":
                        self._handle_bridge_state(base_topic, text)
                    elif rest == "bridge/request/permit_join":
                        self._handle_permit_request(base_topic, text)
                    elif rest == "bridge/groups":
                        parsed = json.loads(text)
                        if isinstance(parsed, list):
                            self._handle_groups(base_topic, parsed)
                    elif rest == "bridge/health":
                        parsed = json.loads(text)
                        if isinstance(parsed, dict):
                            self._handle_bridge_health(base_topic, parsed)
                    return
                if rest.endswith("/availability"):
                    self._handle_availability(base_topic, rest[: -len("/availability")], text)
                    return
                self._handle_state(base_topic, rest, text)
        except ValueError:
            return

    def _record_for(self, base_topic: str, name: str) -> dict | None:
        ieee = self.names.get(base_topic, {}).get(name)
        return self.data["devices"].get(ieee) if ieee else None

    def _handle_devices(self, base_topic: str, devices: list[dict]) -> None:
        now = _now()
        records = self.data["devices"]
        tracking_already = any(record["instance"] == base_topic for record in records.values())
        listed: set[str] = set()
        names: dict[str, str] = {}
        changed = False

        for device in devices:
            ieee = str(device.get("ieee_address") or "").lower()
            if not ieee:
                continue
            listed.add(ieee)
            name = device.get("friendly_name")
            if name:
                names[name] = ieee

            record = records.get(ieee)
            if record is None:
                record = _new_record(ieee, base_topic)
                record["first_seen"] = now
                records[ieee] = record
                changed = True
                if tracking_already:
                    self._add_event(now, base_topic, ieee, name, "added", "Appeared in the Z2M device list")

            if record["instance"] != base_topic:
                if record["instance"] not in (None, "unknown"):
                    self._add_event(now, base_topic, ieee, name, "moved", f"From {record['instance']}")
                record["instance"] = base_topic
                changed = True

            if record["status"] != STATUS_ACTIVE:
                if record["status"] == STATUS_REMOVED and tracking_already:
                    self._add_event(now, base_topic, ieee, name, "rejoined", "Back in the Z2M device list")
                record["status"] = STATUS_ACTIVE
                record["removed_at"] = None
                changed = True

            previous = _set_current_name(record, name, now)
            if previous is not None:
                changed = True
                if previous:
                    self._add_event(now, base_topic, ieee, name, "renamed", f"{previous} → {name}")

            definition = device.get("definition") or {}
            interview_state = device.get("interview_state")
            if not interview_state:
                if device.get("interviewing"):
                    interview_state = "IN_PROGRESS"
                elif device.get("interview_completed"):
                    interview_state = "SUCCESSFUL"
                elif device.get("interview_completed") is False:
                    interview_state = "FAILED"
            updates = {
                "type": device.get("type"),
                "model": definition.get("model"),
                "vendor": definition.get("vendor"),
                "description": definition.get("description"),
                "model_id": device.get("model_id"),
                "manufacturer": device.get("manufacturer"),
                "power_source": device.get("power_source"),
                "software_build_id": device.get("software_build_id"),
                "date_code": device.get("date_code"),
                "supported": device.get("supported"),
                "interview_state": interview_state,
                "z2m_description": device.get("description") or None,
                "features": _features(definition.get("exposes")),
            }
            for key, value in updates.items():
                if value is None or value == []:
                    continue
                if record.get(key) != value:
                    if key == "model" and record.get(key):
                        self._add_event(now, base_topic, ieee, name, "model changed", f"{record[key]} → {value}")
                    record[key] = value
                    changed = True
            if self._update_bindings(base_topic, record, device, name, now, tracking_already):
                changed = True
            record["last_listed"] = now
            if "Z2M live" not in record["sources"]:
                record["sources"].append("Z2M live")

        for ieee, record in records.items():
            if record["instance"] == base_topic and record["status"] == STATUS_ACTIVE and ieee not in listed:
                record["status"] = STATUS_REMOVED
                record["removed_at"] = now
                self._add_event(
                    now, base_topic, ieee, record["friendly_name"], "removed", "Gone from the Z2M device list"
                )
                changed = True

        self.names[base_topic] = names
        self.lists_received.add(base_topic)
        if self.lists_received >= set(self.base_topics):
            self.lists_ready.set()
        self._changed(structural=changed)

    # ----------------------------------------------------------------- groups

    def _handle_groups(self, base_topic: str, groups: list) -> None:
        """bridge/groups: [{id, friendly_name, members: [{ieee_address, endpoint}], scenes}]."""
        names: dict[str, str] = {}
        members: dict[str, list[str]] = {}
        for group in groups:
            if not isinstance(group, dict) or group.get("id") is None:
                continue
            gid = str(group["id"])
            names[gid] = group.get("friendly_name") or f"Group {gid}"
            ieees: list[str] = []
            for member in group.get("members") or []:
                ieee = str((member or {}).get("ieee_address") or "").lower() if isinstance(member, dict) else ""
                if ieee and ieee not in ieees:
                    ieees.append(ieee)
            members[gid] = ieees
        previous = self.group_members.get(base_topic)
        facts = list(self.data.get("group_left") or [])
        # Forget facts for groups that are gone, and for members that are back in their group.
        kept = [
            f
            for f in facts
            if f.get("instance") != base_topic
            or (f.get("group") in members and f.get("ieee") not in members[f.get("group")])
        ]
        if previous is not None:
            now = _now()
            for gid, current in members.items():
                for ieee in previous.get(gid) or []:
                    if ieee in current:
                        continue
                    kept = [
                        f
                        for f in kept
                        if not (f.get("instance") == base_topic and f.get("group") == gid and f.get("ieee") == ieee)
                    ]
                    kept.append({"instance": base_topic, "group": gid, "ieee": ieee, "at": now})
        kept = kept[-MAX_GROUP_LEFT:]
        if kept != facts:
            self.data["group_left"] = kept
            self._changed(structural=True)
        if names != self.groups.get(base_topic) or members != previous:
            self.revision += 1
        self.groups[base_topic] = names
        self.group_members[base_topic] = members

    def group_health(self, now: float) -> list[dict]:
        """Each group with its members' state and members that left it recently (still on the network)."""
        devices = self.data["devices"]
        cutoff = datetime.fromtimestamp(now - GROUP_LEFT_DAYS * 86400, tz=timezone.utc).isoformat(timespec="seconds")
        left_by: dict[tuple[str, str], list[dict]] = {}
        for fact in self.data.get("group_left") or []:
            record = devices.get(fact.get("ieee"))
            # A device that left the network drops out of its groups first; only flag it once it's back.
            if (fact.get("at") or "") < cutoff or record is None or record["status"] != STATUS_ACTIVE:
                continue
            left_by.setdefault((fact.get("instance"), fact.get("group")), []).append(
                {"ieee": fact["ieee"], "name": _display_name(record) or fact["ieee"], "at": fact["at"]}
            )
        out = []
        for bt in self.base_topics:
            names = self.groups.get(bt) or {}
            for gid, ieees in (self.group_members.get(bt) or {}).items():
                left = left_by.get((bt, gid), [])
                if not ieees and not left:
                    continue
                members = []
                for ieee in ieees:
                    record = devices.get(ieee) or {}
                    offline = record.get("status") == STATUS_ACTIVE and record.get("availability") == "offline"
                    seconds = None
                    if offline and record.get("availability_since"):
                        try:
                            seconds = max(
                                0, int(now - datetime.fromisoformat(record["availability_since"]).timestamp())
                            )
                        except ValueError:
                            seconds = None
                    members.append(
                        {
                            "ieee": ieee,
                            "name": _display_name(record) or ieee,
                            "offline": offline,
                            "offline_seconds": seconds,
                        }
                    )
                out.append(
                    {
                        "instance": bt,
                        "id": gid,
                        "name": names.get(gid) or f"Group {gid}",
                        "members": members,
                        "total": len(members),
                        "offline": sum(1 for m in members if m["offline"]),
                        "left": left,
                    }
                )
        out.sort(key=lambda g: (-g["offline"], not g["left"], g["name"].lower()))
        return out

    def _group_issues(self, now: float) -> list[dict]:
        if "group_member_offline" in set(self.options.get("disabled_checks") or []):
            return []
        limit = max(0, int(self.options.get("offline_alert_minutes", 60))) * 60
        out = []
        for group in self.group_health(now):
            late = [m for m in group["members"] if m["offline"] and (m["offline_seconds"] or 0) >= limit]
            if not late:
                continue
            names = ", ".join(m["name"] for m in late)
            n, total = len(late), group["total"]
            out.append(
                {
                    "key": f"group_member_offline:{group['instance']}:{group['id']}",
                    "kind": "group_member_offline",
                    "label": "Group members offline",
                    "severity": "info",
                    "ieee": f"group:{group['instance']}:{group['id']}",
                    "title": "Group members offline",
                    "detail": f"{n} of {total} member{'' if total == 1 else 's'} offline: {names}.",
                    "msg": "group_member_offline",
                    "params": {"n": n, "total": total, "names": names},
                    "name": group["name"],
                    "instance": group["instance"],
                }
            )
        return out

    # --------------------------------------------------------------- bindings

    def binding_label(self, base_topic: str, binding: dict) -> str:
        target = binding.get("target") or ""
        if target.startswith("group:"):
            gid = target.split(":", 1)[1]
            dest = f"group {self.groups.get(base_topic, {}).get(gid) or gid}"
        else:
            ieee, _, ep = target.partition("/")
            rec = self.data["devices"].get(ieee)
            if rec and rec.get("type") == "Coordinator":
                dest = "Coordinator"
            else:
                dest = (_display_name(rec) if rec else None) or ieee
                if ep and ep not in ("None", "1"):
                    dest += f" (endpoint {ep})"
        return f"{binding.get('cluster')} → {dest}"

    def _is_coordinator_target(self, binding: dict) -> bool:
        ieee = (binding.get("target") or "").partition("/")[0]
        return (self.data["devices"].get(ieee) or {}).get("type") == "Coordinator"

    def _update_bindings(
        self, base_topic: str, record: dict, device: dict, name: str | None, now: str, tracking_already: bool
    ) -> bool:
        current = _bindings_of(device)
        if current is None or record.get("type") == "Coordinator":
            return False
        interview = record.get("interview_state")
        if interview and interview != "SUCCESSFUL":
            return False
        previous = record.get("bindings")
        record["bindings"] = current
        if previous is None:
            return True
        old = {b["key"]: b for b in previous}
        new = {b["key"]: b for b in current}
        if old.keys() == new.keys():
            return False
        removed = [old[k] for k in sorted(old.keys() - new.keys())]
        added = [new[k] for k in sorted(new.keys() - old.keys())]
        lost = record.setdefault("lost_bindings", [])
        if added:
            back = {b["key"] for b in added}
            restored = [b for b in lost if b["key"] in back]
            record["lost_bindings"] = lost = [b for b in lost if b["key"] not in back]
            text = "; ".join(self.binding_label(base_topic, b) for b in added[:6]) + ("…" if len(added) > 6 else "")
            self._add_event(
                now,
                base_topic,
                record["ieee"],
                name,
                "binding restored" if restored and len(restored) == len(added) else "binding added",
                text,
            )
        if removed:
            unbound_at = self._unbinds.pop(record["ieee"], 0)
            intended = time.time() - unbound_at < 180
            text = "; ".join(self.binding_label(base_topic, b) for b in removed[:6]) + ("…" if len(removed) > 6 else "")
            self._add_event(now, base_topic, record["ieee"], name, "unbound" if intended else "binding removed", text)
            if not intended and tracking_already:
                known = {b["key"] for b in lost}
                for b in removed:
                    if b["key"] not in known:
                        lost.append(
                            {
                                **b,
                                "label": self.binding_label(base_topic, b),
                                "coordinator": self._is_coordinator_target(b),
                                "lost_at": now,
                            }
                        )
                del lost[: max(0, len(lost) - 50)]
        return True

    def acknowledge_bindings(self, ieee: str) -> bool:
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None:
                return False
            if record.get("lost_bindings"):
                self._add_event(
                    _now(),
                    record.get("instance") or "unknown",
                    ieee,
                    record.get("friendly_name"),
                    "binding change accepted",
                    f"{len(record['lost_bindings'])} lost binding(s) dismissed",
                )
            record["lost_bindings"] = []
            self._changed(structural=True)
        self.evaluate_health()
        return True

    def restore_bindings(self, ieee: str, keys: list[str] | None = None) -> dict:
        """Re-create lost bindings: a bind request for device/group targets, reconfigure for the coordinator.

        keys limits it to those lost bindings; None restores all of them.
        """
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None or record["status"] != STATUS_ACTIVE:
                return {"ok": False, "error": "Device is not active in Zigbee2MQTT"}
            base_topic = record["instance"]
            lost = [dict(b) for b in record.get("lost_bindings") or []]
            if keys is not None:
                lost = [b for b in lost if b.get("key") in keys]
        if not lost:
            return {"ok": True, "restored": 0, "failed": []}
        failed: list[str] = []
        errors: list[str] = []
        restored = 0
        if any(b.get("coordinator") for b in lost):
            result = self.reconfigure(ieee)
            if result["ok"]:
                restored += sum(1 for b in lost if b.get("coordinator"))
            else:
                failed += [b.get("label") or b["key"] for b in lost if b.get("coordinator")]
                errors.append(str(result.get("error")))
        for binding in lost:
            if binding.get("coordinator"):
                continue
            target = binding.get("target") or ""
            payload: dict[str, Any] = {"from": ieee, "clusters": [binding.get("cluster")]}
            ep = str(binding.get("ep") or "")
            if ep.isdigit():
                payload["from_endpoint"] = int(ep)
            if target.startswith("group:"):
                # Zigbee2MQTT only accepts a group's friendly name as a bind target, not its ID.
                gid = target.split(":", 1)[1]
                group_name = self.groups.get(base_topic, {}).get(gid)
                if not group_name:
                    failed.append(binding.get("label") or binding["key"])
                    errors.append(f"Group {gid} no longer exists in Zigbee2MQTT")
                    continue
                payload["to"] = group_name
            else:
                to_ieee, _, to_ep = target.partition("/")
                payload["to"] = to_ieee
                if to_ep.isdigit():
                    payload["to_endpoint"] = int(to_ep)
            result = self.request(base_topic, "device/bind", payload, timeout=30)
            if result and result.get("status") == "ok" and not (result.get("data") or {}).get("failed"):
                restored += 1
            else:
                failed.append(binding.get("label") or binding["key"])
                errors.append(friendly_z2m_error((result or {}).get("error") or "No response from Zigbee2MQTT"))
        with self.lock:
            rec = self.data["devices"].get(ieee)
            if rec is not None:
                self._add_event(
                    _now(),
                    base_topic,
                    ieee,
                    rec.get("friendly_name"),
                    "binding restore requested" if not failed else "binding restore failed",
                    f"{restored} of {len(lost)} re-created" + (f": {errors[0]}" if errors else ""),
                )
                self._changed(structural=True)
        ok = not failed
        return {"ok": ok, "restored": restored, "failed": failed, "error": None if ok else errors[0]}

    def _handle_event(self, base_topic: str, payload: dict) -> None:
        event_type = payload.get("type")
        data = payload.get("data") or {}
        ieee = str(data.get("ieee_address") or "").lower()
        if not ieee:
            return
        now = _now()
        name = data.get("friendly_name")
        record = self.data["devices"].get(ieee)
        if record is None:
            record = _new_record(ieee, base_topic)
            record["first_seen"] = now
            self.data["devices"][ieee] = record
        record["last_seen"] = _max_time(record["last_seen"], now)

        if (
            event_type in ("device_joined", "device_interview", "device_announce")
            and base_topic in self.pairing_sessions
        ):
            session = self.pairing_sessions[base_topic]
            if session.get("target") == ieee and session.get("active") and not session.get("rejoined"):
                session["rejoined"] = now
                via = session.get("via_name") or "the chosen router"
                self._add_event(
                    now, base_topic, ieee, name, "re-paired", f"Rejoined while pairing was open only through {via}"
                )
                self._changed(structural=True)
            if event_type != "device_announce" and ieee not in session["joined"]:
                session["joined"].append(ieee)
                self.revision += 1
        if event_type == "device_announce":
            self._changed(structural=False)
            return
        if event_type == "device_joined":
            self._add_event(now, base_topic, ieee, name, "joined", "", notify=True)
        elif event_type == "device_leave":
            self._add_event(now, base_topic, ieee, name, "left", "Device left the network", notify=True)
        elif event_type == "device_interview":
            status = data.get("status") or "?"
            detail = ""
            definition = data.get("definition") or {}
            if status == "successful" and definition:
                for key in ("model", "vendor", "description"):
                    if definition.get(key):
                        record[key] = definition[key]
                features = _features(definition.get("exposes"))
                if features:
                    record["features"] = features
                detail = f"{definition.get('vendor') or ''} {definition.get('model') or ''}".strip()
            self._add_event(now, base_topic, ieee, name, f"interview {status}", detail, notify=status == "failed")
        else:
            return
        self._changed(structural=True)

    def _handle_response(self, base_topic: str, kind: str, payload: dict) -> None:
        transaction = payload.get("transaction")
        if transaction and transaction in self._pending:
            pending = self._pending[transaction]
            pending["result"] = payload
            pending["event"].set()
        if kind == "networkmap":
            self._ingest_networkmap(base_topic, payload)
        elif kind == "permit_join" and payload.get("status") == "ok":
            data = payload.get("data") or {}
            device = data.get("device")
            if device and _permit_opening(data):
                for holder in (self.permit_meta.get(base_topic), self._permit_hint.get(base_topic)):
                    if holder is not None:
                        holder["device"] = device
                self.revision += 1
        elif kind == "device/unbind" and payload.get("status") == "ok":
            source = str((payload.get("data") or {}).get("from") or "")
            ieee = (
                self.names.get(base_topic, {}).get(source)
                or self.names.get(base_topic, {}).get(source.rsplit("/", 1)[0])
                or source.lower()
            )
            if ieee in self.data["devices"]:
                self._unbinds[ieee] = time.time()

    def _handle_availability(self, base_topic: str, name: str, payload: str) -> None:
        record = self._record_for(base_topic, name)
        if record is None:
            return
        state = payload.strip()
        if state.startswith("{"):
            state = (json.loads(state) or {}).get("state", "")
        if state not in ("online", "offline"):
            return
        previous = record["availability"]
        if previous == state:
            if _stamp_offline_since(record):
                self._changed(structural=True)
            return
        now = _now()
        record["availability"] = state
        record["availability_since"] = now
        outages = record.setdefault("outages", [])
        if state == "offline":
            if not outages or outages[-1].get("end"):
                outages.append({"start": now, "end": None})
                del outages[: max(0, len(outages) - MAX_OUTAGES)]
        elif outages and not outages[-1].get("end"):
            outages[-1]["end"] = now
        if previous == "online" and state == "offline":
            self._add_event(now, base_topic, record["ieee"], name, "offline", "")
        elif previous == "offline" and state == "online":
            self._add_event(now, base_topic, record["ieee"], name, "online", "")
        self._changed(structural=True)

    def _handle_state(self, base_topic: str, name: str, payload: str) -> None:
        record = self._record_for(base_topic, name)
        if record is None:
            return
        parsed = None
        if payload[:1] == "{":
            parsed = json.loads(payload)
        if not isinstance(parsed, dict):
            record["last_seen"] = _max_time(record["last_seen"], _now())
            self._changed(structural=False)
            return
        now = _now()
        ieee = record["ieee"]
        seen = self._to_utc_iso(parsed.get("last_seen")) or now
        record["last_seen"] = _max_time(record["last_seen"], seen)
        structural = False
        self.metrics.add_message(ieee)

        lqi = parsed.get("linkquality")
        if isinstance(lqi, (int, float)) and not isinstance(lqi, bool):
            record["lqi"] = lqi
            self.metrics.add_lqi(ieee, lqi)

        battery = parsed.get("battery")
        if isinstance(battery, (int, float)) and not isinstance(battery, bool):
            if record.get("battery") != battery:
                structural = True
            record["battery"] = battery
            if self._replaced_since_reading(record):
                structural = True
            record["battery_reported"] = now
            replaced_from = self.metrics.add_battery(ieee, battery)
            if replaced_from is not None:
                self._record_replacement(
                    record, now[:10], "Detected", f"Battery went from {replaced_from:.0f}% to {battery:.0f}%"
                )
                structural = True
        if isinstance(parsed.get("battery_low"), bool):
            if record.get("battery_low") != parsed["battery_low"]:
                structural = True
            record["battery_low"] = parsed["battery_low"]
            if self._replaced_since_reading(record):
                structural = True
            record["battery_reported"] = now

        update = parsed.get("update")
        if isinstance(update, dict):
            slim = {
                "state": update.get("state"),
                "installed_version": update.get("installed_version"),
                "latest_version": update.get("latest_version"),
            }
            if update.get("state") == "updating":
                slim["progress"] = update.get("progress")
                slim["remaining"] = update.get("remaining")
            previous = record.get("update") or {}
            if previous.get("state") == "updating" and slim["state"] == "idle":
                self._add_event(now, base_topic, ieee, name, "firmware updated", f"Now on {slim['installed_version']}")
            if slim != previous:
                if slim["state"] == "available" and previous.get("state") != "available":
                    self._add_event(
                        now,
                        base_topic,
                        ieee,
                        name,
                        "update available",
                        f"{slim['installed_version']} → {slim['latest_version']}",
                    )
                record["update"] = slim
                structural = True

        readings = record.setdefault("readings", {})
        for key in MEASURE_KEYS:
            value = parsed.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            reading = readings.get(key)
            if reading is None or reading.get("value") != value:
                readings[key] = {"value": value, "changed_at": seen, "reports": 0}
            else:
                reading["reports"] = int(reading.get("reports", 0)) + 1

        self._changed(structural=structural)

    def _ingest_networkmap(self, base_topic: str, payload: dict) -> None:
        now = _now()
        status = payload.get("status")
        info = {"scanned_at": now, "status": status, "error": payload.get("error")}
        value = ((payload.get("data") or {}).get("value")) if status == "ok" else None
        if not isinstance(value, dict):
            self.data["networkmap"][base_topic] = info
            self._changed(structural=True)
            return
        links = value.get("links") or []
        neighbours: dict[str, list[dict]] = {}
        parents: dict[str, dict] = {}
        children: dict[str, int] = {}
        depth: dict[str, Any] = {}

        def addr(side: Any, fallback: Any) -> str:
            if isinstance(side, dict):
                return str(side.get("ieeeAddr") or "").lower()
            return str(fallback or "").lower()

        for link in links:
            source = addr(link.get("source"), link.get("sourceIeeeAddr"))
            target = addr(link.get("target"), link.get("targetIeeeAddr"))
            if not source or not target:
                continue
            lqi = link.get("lqi", link.get("linkquality"))
            relationship = link.get("relationship")
            neighbours.setdefault(source, []).append({"ieee": target, "lqi": lqi, "rel": relationship})
            neighbours.setdefault(target, []).append({"ieee": source, "lqi": lqi, "rel": relationship})
            if relationship == 1:
                current = parents.get(source)
                if current is None or (lqi or 0) > (current.get("lqi") or 0):
                    parents[source] = {"ieee": target, "lqi": lqi}
                children[target] = children.get(target, 0) + 1
            if link.get("depth") is not None:
                depth[source] = link.get("depth")

        for node in value.get("nodes") or []:
            ieee = str(node.get("ieeeAddr") or "").lower()
            record = self.data["devices"].get(ieee)
            if record is None:
                continue
            nbrs = {}
            for entry in neighbours.get(ieee, []):
                existing = nbrs.get(entry["ieee"])
                if existing is None or (entry["lqi"] or 0) > (existing["lqi"] or 0):
                    nbrs[entry["ieee"]] = entry
            record["routing"] = {
                "scanned_at": now,
                "parent": parents.get(ieee),
                "children": children.get(ieee, 0),
                "depth": depth.get(ieee),
                "neighbours": sorted(nbrs.values(), key=lambda e: -(e["lqi"] or 0))[:12],
            }
        info["nodes"] = len(value.get("nodes") or [])
        info["links"] = len(links)
        self._record_scan(base_topic, now, parents, neighbours)
        self.data["networkmap"][base_topic] = info
        self._add_event(
            now, base_topic, "network", "Network", "routing scanned", f"{info['nodes']} nodes, {info['links']} links"
        )
        self._changed(structural=True)

    # ------------------------------------------------------------ scan history

    def load_scans(self, path: str) -> None:
        self.scans_path = path
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self.scans = data
        except (OSError, ValueError):
            self.scans = {}

    def _save_scans(self) -> None:
        if not self.scans_path:
            return
        tmp = f"{self.scans_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.scans, handle, separators=(",", ":"))
        os.replace(tmp, self.scans_path)

    def _record_scan(self, base_topic: str, when: str, parents: dict, neighbours: dict) -> None:
        record_parents = {ieee: [p["ieee"], p.get("lqi")] for ieee, p in parents.items()}
        links: dict[str, list] = {}
        for ieee, nbrs in neighbours.items():
            dev = self.data["devices"].get(ieee)
            if not dev or dev.get("type") not in ("Router", "Coordinator"):
                continue
            routers = [
                n for n in nbrs if (self.data["devices"].get(n["ieee"]) or {}).get("type") in ("Router", "Coordinator")
            ]
            for n in sorted(routers, key=lambda x: -(x.get("lqi") or 0))[:4]:
                key = "|".join(sorted([ieee, n["ieee"]]))
                if key not in links or (n.get("lqi") or 0) > (links[key][2] or 0):
                    links[key] = [ieee, n["ieee"], n.get("lqi")]
        scans = self.scans.setdefault(base_topic, [])
        scans.append({"at": when, "parents": record_parents, "links": list(links.values())})
        del scans[: max(0, len(scans) - 40)]
        try:
            self._save_scans()
        except OSError as err:
            _LOGGER.warning("Saving scan history failed: %s", err)

    def scan_history(self, base_topic: str) -> list:
        with self.lock:
            return list(self.scans.get(base_topic) or [])

    def _hop_rows(self, record: dict, now: float, days: int = HOP_DAYS) -> list[dict]:
        since = datetime.fromtimestamp(now - days * 86400, tz=timezone.utc).isoformat(timespec="seconds")
        return parent_rows(self.scans.get(record.get("instance") or "") or [], record["ieee"], since)

    def parent_history(self, ieee: str, days: int = HOP_DAYS) -> dict | None:
        """Parent per routing scan over the last `days`, newest first, for the device panel's History tab."""
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None:
                return None
            rows = self._hop_rows(record, time.time(), days)

            def name(parent: str) -> str:
                return _display_name(self.data["devices"].get(parent) or {}) or parent

            summary = hop_summary(rows)
            if summary["best"]:
                summary["best"]["name"] = name(summary["best"]["ieee"])
            return {
                "days": days,
                "threshold": HOP_CHANGES,
                "scans": [{**r, "name": name(r["ieee"])} for r in reversed(rows)],
                **summary,
            }

    def _to_utc_iso(self, value: Any) -> str | None:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            if value <= 0:
                return None
            seconds = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds")
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=self.local_tz)
        return moment.astimezone(timezone.utc).isoformat(timespec="seconds")

    def _add_event(
        self, when: str, base_topic: str, ieee: str, name: str | None, kind: str, detail: str, notify: bool = False
    ) -> None:
        event = {"time": when, "instance": base_topic, "ieee": ieee, "name": name, "kind": kind, "detail": detail}
        events = self.data["events"]
        events.append(event)
        if len(events) > MAX_EVENTS:
            del events[: len(events) - MAX_EVENTS]
        if notify and self.on_event:
            self.on_event(dict(event))

    # --------------------------------------------------------- MQTT requests

    def request(self, base_topic: str, path: str, payload: dict, timeout: float = 30.0) -> dict | None:
        if not self.publish:
            raise RuntimeError("MQTT is not connected")
        transaction = f"z2mt-{uuid.uuid4().hex[:10]}"
        waiter = {"event": threading.Event(), "result": None}
        with self.lock:
            self._pending[transaction] = waiter
        try:
            self.publish(f"{base_topic}/bridge/request/{path}", {**payload, "transaction": transaction})
            waiter["event"].wait(timeout)
            return waiter["result"]
        finally:
            with self.lock:
                self._pending.pop(transaction, None)

    def reconfigure(self, ieee: str) -> dict:
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None or record["status"] != STATUS_ACTIVE:
                return {"ok": False, "error": "Device is not active in Zigbee2MQTT"}
            base_topic = record["instance"]
            name = record["friendly_name"]
        result = self.request(base_topic, "device/configure", {"id": ieee}, timeout=40)
        ok = bool(result and result.get("status") == "ok")
        raw = None if ok else ((result or {}).get("error") or "No response from Zigbee2MQTT")
        error = None if ok else friendly_z2m_error(raw)
        with self.lock:
            self._add_event(
                _now(), base_topic, ieee, name, "reconfigured" if ok else "reconfigure failed", "" if ok else str(error)
            )
            if ok:
                for reading in (self.data["devices"][ieee].get("readings") or {}).values():
                    reading["reports"] = 0
            self._changed(structural=True)
        return {"ok": ok, "error": error, "detail": raw}

    def scan_networkmap(self, base_topic: str | None = None) -> dict:
        topics = [base_topic] if base_topic else list(self.base_topics)
        results = {}
        for topic in topics:
            with self.lock:
                self.data["networkmap"].setdefault(topic, {})["running_since"] = _now()
                self._changed(structural=True)
            result = self.request(topic, "networkmap", {"type": "raw", "routes": False}, timeout=600)
            with self.lock:
                self.data["networkmap"].setdefault(topic, {}).pop("running_since", None)
                if result is None:
                    self.data["networkmap"][topic].update({"scanned_at": _now(), "status": "timeout"})
                self._changed(structural=True)
            results[topic] = (result or {}).get("status", "timeout")
        return results

    # ----------------------------------------------------------------- health

    def _suggestions(self) -> dict[str, list[dict]]:
        removed_by_name: dict[str, list[dict]] = {}
        for record in self.data["devices"].values():
            if record["status"] != STATUS_REMOVED or record.get("successor"):
                continue
            for entry in record.get("names") or []:
                if entry.get("name") and not IEEE_RE.match(entry["name"].lower()):
                    removed_by_name.setdefault(_slug(entry["name"]), []).append(record)
        out: dict[str, list[dict]] = {}
        for record in self.data["devices"].values():
            if record["status"] != STATUS_ACTIVE or not record.get("friendly_name"):
                continue
            seen = set(record.get("predecessors") or [])
            for old in removed_by_name.get(_slug(record["friendly_name"]), []):
                if old["ieee"] != record["ieee"] and old["ieee"] not in seen:
                    seen.add(old["ieee"])
                    out.setdefault(record["ieee"], []).append(
                        {"ieee": old["ieee"], "name": _display_name(old), "removed_at": old.get("removed_at")}
                    )
        return out

    # ------------------------------------------------- single points of failure

    def single_points(self, base_topic: str) -> list[dict]:
        """Routers that are the only path to the coordinator for other routers, from the latest scan."""
        scanned_at = (self.data.get("networkmap") or {}).get(base_topic, {}).get("scanned_at")
        if not scanned_at:
            return []
        cached = self._spof_cache.get(base_topic)
        if cached and cached[0] == scanned_at and cached[1] == self.revision:
            return cached[2]
        records = self.data["devices"]
        nodes = {
            ieee
            for ieee, r in records.items()
            if r.get("instance") == base_topic
            and r["status"] == STATUS_ACTIVE
            and r.get("type") in ("Router", "Coordinator")
            and (r.get("routing") or {}).get("scanned_at") == scanned_at
        }
        coordinator = next((i for i in nodes if records[i].get("type") == "Coordinator"), None)
        result: list[dict] = []
        if coordinator:
            edges: dict[str, set[str]] = {i: set() for i in nodes}
            for ieee in nodes:
                for n in (records[ieee].get("routing") or {}).get("neighbours") or []:
                    other = n.get("ieee")
                    if other in nodes and other != ieee and (n.get("lqi") or 0) > 0:
                        edges[ieee].add(other)
                        edges[other].add(ieee)

            def reach(skip: str | None) -> set[str]:
                seen = {coordinator}
                stack = [coordinator]
                while stack:
                    for other in edges[stack.pop()]:
                        if other != skip and other not in seen:
                            seen.add(other)
                            stack.append(other)
                return seen

            connected = reach(None)
            ends_by_parent: dict[str, list[str]] = {}
            for ieee, r in records.items():
                parent = ((r.get("routing") or {}).get("parent") or {}).get("ieee")
                if (
                    r.get("instance") == base_topic
                    and r["status"] == STATUS_ACTIVE
                    and r.get("type") == "EndDevice"
                    and parent
                    and (r.get("routing") or {}).get("scanned_at") == scanned_at
                ):
                    ends_by_parent.setdefault(parent, []).append(ieee)
            for router in sorted(connected - {coordinator}):
                cut = connected - reach(router) - {router}
                if not cut:
                    continue
                behind = [e for p in cut for e in ends_by_parent.get(p, [])]
                result.append(
                    {
                        "ieee": router,
                        "name": _display_name(records[router]) or router,
                        "routers": sorted(
                            ({"ieee": i, "name": _display_name(records[i]) or i} for i in cut),
                            key=lambda x: x["name"].lower(),
                        ),
                        "devices": len(behind),
                        "device_list": sorted(behind),
                        "children": len(ends_by_parent.get(router, [])),
                    }
                )
            result.sort(key=lambda x: -(len(x["routers"]) + x["devices"]))
        self._spof_cache[base_topic] = (scanned_at, self.revision, result)
        return result

    # ------------------------------------------------------ coordinator backup

    def _z2m_dirs(self) -> dict[str, str]:
        """Map base topic -> Zigbee2MQTT data folder, for folders inside the HA config directory."""
        found: dict[str, str] = {}
        try:
            entries = sorted(os.listdir(self.config_dir))
        except OSError:
            return found
        for entry in entries:
            folder = os.path.join(self.config_dir, entry)
            config = os.path.join(folder, "configuration.yaml")
            if not entry.lower().startswith("zigbee2mqtt") or not os.path.isfile(config):
                continue
            topic = "zigbee2mqtt"
            try:
                with open(config, encoding="utf-8") as handle:
                    in_mqtt = False
                    for line in handle:
                        if re.match(r"^\S", line):
                            in_mqtt = line.startswith("mqtt:")
                            continue
                        match = re.match(r"^\s+base_topic:\s*['\"]?([^'\"#\s]+)", line)
                        if in_mqtt and match:
                            topic = match.group(1)
                            break
            except OSError:
                continue
            found.setdefault(topic, folder)
        return found

    def coordinator_backups(self, force: bool = False) -> dict[str, dict]:
        if not force and time.time() - self._backups_at < BACKUP_REFRESH:
            return self._backups
        dirs = self._z2m_dirs()
        out: dict[str, dict] = {}
        for bt in self.base_topics:
            folder = dirs.get(bt)
            if not folder:
                out[bt] = {"found": False}
                continue
            path = os.path.join(folder, "coordinator_backup.json")
            info: dict[str, Any] = {
                "found": True,
                "folder": os.path.relpath(folder, self.config_dir),
                "exists": os.path.isfile(path),
            }
            if info["exists"]:
                try:
                    info["modified"] = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).isoformat(
                        timespec="seconds"
                    )
                    with open(path, encoding="utf-8") as handle:
                        backup = json.load(handle)
                    meta = backup.get("metadata") or {}
                    info.update(
                        {
                            "date": self._to_utc_iso((meta.get("internal") or {}).get("date")) or info["modified"],
                            "source": meta.get("source"),
                            "channel": backup.get("channel"),
                            "pan_id": backup.get("pan_id"),
                            "devices": len(backup.get("devices") or []),
                            "has_key": bool((backup.get("network_key") or {}).get("key")),
                        }
                    )
                except (OSError, ValueError, AttributeError) as err:
                    info["error"] = f"Couldn't read it: {err}"
            out[bt] = info
        self._backups = out
        self._backups_at = time.time()
        return out

    # English text of each backup problem; the sidebar translates the same keys (issue.<msg>.detail).
    BACKUP_TEXT = {
        "backup_missing": "There's no coordinator_backup.json in its data folder. Without it, a replacement coordinator can't take over the network and every device must be re-paired.",
        "backup_unreadable": "coordinator_backup.json exists but is unreadable. {error}",
        "backup_no_key": "coordinator_backup.json has no network key, so it can't restore the network.",
        "backup_channel": "The backup is for channel {backup} but the network runs on channel {network}. It's from an older network and wouldn't restore this one.",
        "backup_pan": "The backup's PAN ID ({backup}) doesn't match the running network ({network}). It's from an older network and wouldn't restore this one.",
        "backup_old": "The last coordinator backup is {n} days old. Zigbee2MQTT normally refreshes it at least daily, so writing it may be failing.",
    }

    def _backup_issue(self, bt: str, now: float) -> str | None:
        problem = self._backup_problem(bt, now)
        return self.BACKUP_TEXT[problem[0]].format(**problem[1]) if problem else None

    def _backup_problem(self, bt: str, now: float) -> tuple[str, dict] | None:
        """(msg, params) for the coordinator backup problem, if any."""
        info = self._backups.get(bt) or {}
        if not info.get("found"):
            return None
        bridge = (self.data.get("bridges") or {}).get(bt) or {}
        if not info.get("exists"):
            return "backup_missing", {}
        if info.get("error"):
            return "backup_unreadable", {"error": info["error"]}
        if not info.get("has_key"):
            return "backup_no_key", {}
        net = bridge.get("info") or {}
        try:
            pan = int(str(info.get("pan_id")), 16) if info.get("pan_id") is not None else None
        except ValueError:
            pan = None
        if net.get("channel") and info.get("channel") and int(net["channel"]) != int(info["channel"]):
            return "backup_channel", {"backup": info["channel"], "network": net["channel"]}
        if net.get("pan_id") is not None and pan is not None and int(net["pan_id"]) != pan:
            return "backup_pan", {"backup": pan, "network": net["pan_id"]}
        try:
            age = now - datetime.fromisoformat(info.get("date") or "").timestamp()
        except ValueError:
            age = 0
        uptime = (bridge.get("health") or {}).get("uptime_sec") or 0
        if age > BACKUP_MAX_AGE and bridge.get("state") == "online" and uptime > 86400:
            return "backup_old", {"n": int(age // 86400)}
        return None

    def _memory_problem(self, bridge: dict) -> dict | None:
        if bridge.get("state") == "offline":
            return None
        return memory_growth(
            bridge.get("memory_days") or [], self._today(), (bridge.get("health") or {}).get("memory_mb")
        )

    def network_hours(self, now: float) -> dict[str, dict]:
        """Link quality by hour of day per network (Metrics.network_hours), recomputed once an hour."""
        hour = int(now // 3600)
        if self._hours_at == hour:
            return self._hours
        with self.lock:
            members: dict[str, list[str]] = {}
            for ieee, record in self.data["devices"].items():
                if record.get("status") == STATUS_ACTIVE and record.get("type") != "Coordinator":
                    members.setdefault(record.get("instance") or "", []).append(ieee)
        out = {}
        for bt in self.base_topics:
            result = self.metrics.network_hours(members.get(bt, []), self.local_tz, now)
            if result:
                out[bt] = result
        self._hours, self._hours_at = out, hour
        return out

    @staticmethod
    def _dip_text(dip: dict) -> tuple[str, dict]:
        """English detail and params for a signal_dip issue."""
        start, end = dip["start"], (dip["start"] + dip["hours"]) % 24
        middle = (start + dip["hours"] / 2) % 24
        period = (
            "mornings"
            if 5 <= middle < 12
            else "afternoons"
            if 12 <= middle < 17
            else "evenings"
            if 17 <= middle < 22
            else "nights"
        )
        params = {"n": dip["devices"], "period": period, "start": f"{start:02d}:00", "end": f"{end:02d}:00"}
        text = (
            f"Signal drops across {params['n']} device{'' if params['n'] == 1 else 's'} most {period}, {params['start']}–{params['end']}. "
            "Something switches on then: Wi-Fi traffic on an overlapping channel, a microwave, "
            "or a USB 3 device near the coordinator."
        )
        return text, params

    def _network_issues(self, now: float) -> list[dict]:
        out = []
        disabled = set(self.options.get("disabled_checks") or [])
        hours = self.network_hours(now) if "signal_dip" not in disabled else {}
        for bt in self.base_topics:
            bridge = (self.data.get("bridges") or {}).get(bt) or {}
            label = "Main" if bt == "zigbee2mqtt" else bt.replace("zigbee2mqtt_", "").replace("_", " ").capitalize()
            if bridge.get("state") == "offline" and "network_offline" not in disabled:
                try:
                    age = now - datetime.fromisoformat(bridge["since"]).timestamp()
                except (TypeError, ValueError):
                    age = 0
                if age >= 120:
                    mins = int(age // 60)
                    out.append(
                        {
                            "key": f"network_offline:{bt}",
                            "kind": "network_offline",
                            "label": "Network offline",
                            "severity": "warning",
                            "ieee": f"network:{bt}",
                            "title": "Zigbee2MQTT offline",
                            "detail": f"Not responding for {mins} min. Every device on this network is unreachable. Check the coordinator's power and network connection.",
                            "msg": "network_offline",
                            "params": {"seconds": int(age)},
                            "name": f"{label} network",
                            "instance": bt,
                        }
                    )
            if (bridge.get("info") or {}).get("restart_required") and "restart_required" not in disabled:
                out.append(
                    {
                        "key": f"restart_required:{bt}",
                        "kind": "restart_required",
                        "label": "Restart needed",
                        "severity": "info",
                        "ieee": f"network:{bt}",
                        "title": "Restart needed",
                        "detail": "Zigbee2MQTT says it needs a restart to apply a settings change.",
                        "msg": "restart_required",
                        "params": {},
                        "name": f"{label} network",
                        "instance": bt,
                    }
                )
            if "pairing_open" not in disabled:
                issue = self.pairing_open_issue(bt, now)
                if issue:
                    out.append(issue)
            growth = None if "z2m_memory" in disabled else self._memory_problem(bridge)
            if growth:
                out.append(
                    {
                        "key": f"z2m_memory:{bt}",
                        "kind": "z2m_memory",
                        "label": "Memory climbing",
                        "severity": "info",
                        "ieee": f"network:{bt}",
                        "title": "Zigbee2MQTT memory climbing",
                        "detail": f"Memory up {growth['percent']} % in {growth['days']} days. Restarting Zigbee2MQTT frees it; if it keeps climbing, check its log for an extension or converter that leaks.",
                        "msg": "z2m_memory",
                        "params": growth,
                        "name": f"{label} network",
                        "instance": bt,
                    }
                )
            if "coordinator_backup" not in disabled:
                problem = self._backup_problem(bt, now)
                if problem:
                    out.append(
                        {
                            "key": f"coordinator_backup:{bt}",
                            "kind": "coordinator_backup",
                            "label": "Coordinator backup",
                            "severity": "warning",
                            "ieee": f"network:{bt}",
                            "title": "Coordinator backup",
                            "detail": self.BACKUP_TEXT[problem[0]].format(**problem[1]),
                            "msg": problem[0],
                            "params": problem[1],
                            "name": f"{label} network",
                            "instance": bt,
                        }
                    )
            dips = (hours.get(bt) or {}).get("dips") or []
            if dips:
                text, params = self._dip_text(dips[0])
                out.append(
                    {
                        "key": f"signal_dip:{bt}",
                        "kind": "signal_dip",
                        "label": "Daily signal dip",
                        "severity": "info",
                        "ieee": f"network:{bt}",
                        "title": "Daily signal dip",
                        "detail": text,
                        "msg": "signal_dip",
                        "params": params,
                        "name": f"{label} network",
                        "instance": bt,
                    }
                )
        return out

    def radio(self) -> dict:
        """Zigbee/Wi-Fi channel overlap analysis."""
        zfreq = lambda c: 2405 + 5 * (int(c) - 11)
        wfreq = lambda n: 2484 if int(n) == 14 else 2407 + 5 * int(n)
        nets = []
        for bt in self.base_topics:
            info = ((self.data.get("bridges") or {}).get(bt) or {}).get("info") or {}
            if info.get("channel"):
                nets.append(
                    {
                        "topic": bt,
                        "channel": int(info["channel"]),
                        "pan_id": info.get("pan_id"),
                        "mhz": zfreq(info["channel"]),
                    }
                )
        wifi = []
        for part in str(self.options.get("wifi_channels") or "").replace(";", ",").split(","):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= 14 and int(part) not in wifi:
                wifi.append(int(part))
        findings = []
        label = lambda bt: (
            "Main" if bt == "zigbee2mqtt" else bt.replace("zigbee2mqtt_", "").replace("_", " ").capitalize()
        )

        def score(channel: int, avoid: list[int]) -> float:
            f = zfreq(channel)
            wifi_gap = min([abs(f - wfreq(w)) for w in wifi] or [50])
            zig_gap = min([abs(channel - a) for a in avoid] or [16])
            return min(wifi_gap, 40) + min(zig_gap, 4) * 6 - (10 if channel == 26 else 0)

        for net in nets:
            for w in wifi:
                gap = abs(net["mhz"] - wfreq(w))
                if gap <= 12:
                    best_w = max((1, 6, 11), key=lambda c: abs(net["mhz"] - wfreq(c)))
                    findings.append(
                        {
                            "sev": 2,
                            "text": f"{label(net['topic'])} network (Zigbee channel {net['channel']}, {net['mhz']} MHz) overlaps Wi-Fi channel {w}. The easiest fix is moving your Wi-Fi to channel {best_w} in the router settings. Changing the Zigbee channel means re-pairing every device.",
                            "msg": "overlap",
                            "params": {
                                "net": net["topic"],
                                "channel": net["channel"],
                                "mhz": net["mhz"],
                                "wifi": w,
                                "best": best_w,
                            },
                        }
                    )
                elif gap <= 16:
                    findings.append(
                        {
                            "sev": 1,
                            "text": f"{label(net['topic'])} network (Zigbee channel {net['channel']}) sits just beside Wi-Fi channel {w}. Usually fine, but busy Wi-Fi can still cause occasional drops.",
                            "msg": "beside",
                            "params": {"net": net["topic"], "channel": net["channel"], "wifi": w},
                        }
                    )
        by_channel: dict[int, list] = {}
        for net in nets:
            by_channel.setdefault(net["channel"], []).append(net)
        for channel, group in by_channel.items():
            if len(group) > 1:
                others = [n["channel"] for n in nets]
                candidates = [c for c in range(11, 27) if c != channel]
                best = max(candidates, key=lambda c: score(c, others))
                names = " and ".join(label(n["topic"]) for n in group)
                findings.append(
                    {
                        "sev": 1,
                        "text": f"{names} both use Zigbee channel {channel}. Separate networks on one channel share its airtime, which matters if their coordinators or devices are within range of each other. If one of them has dropouts, moving the smaller network to channel {best} would separate them. That needs every device on it re-paired, so only do it if there's a real problem.",
                        "msg": "shared",
                        "params": {"nets": [n["topic"] for n in group], "channel": channel, "best": best},
                    }
                )
        if nets and not findings:
            findings.append(
                {
                    "sev": 0,
                    "text": "No overlap: your Zigbee channels are clear of the Wi-Fi channels you've entered"
                    + ("" if wifi else " (add your Wi-Fi channels in Settings to check them too)")
                    + ".",
                    "msg": "clear" if wifi else "clear_no_wifi",
                    "params": {},
                }
            )
        return {"networks": nets, "wifi": wifi, "findings": findings}

    def _context(self, now: float) -> dict[str, dict]:
        """Per-device extras for the health checks: recent outages/leaves, parent, single points of failure."""
        day_ago = datetime.fromtimestamp(now - 86400, tz=timezone.utc).isoformat(timespec="seconds")
        leaves: dict[str, int] = {}
        for event in reversed(self.data["events"]):
            if (event.get("time") or "") < day_ago:
                break
            if event.get("kind") == "left":
                leaves[event["ieee"]] = leaves.get(event["ieee"], 0) + 1
        spof = {item["ieee"]: item for bt in self.base_topics for item in self.single_points(bt)}
        bridges = self.data.get("bridges") or {}
        down = {bt for bt, bridge in bridges.items() if (bridge or {}).get("state") == "offline"}
        out: dict[str, dict] = {}
        for ieee, record in self.data["devices"].items():
            extra: dict[str, Any] = {
                "outages_24h": sum(1 for o in record.get("outages") or [] if (o.get("start") or "") >= day_ago),
                "leaves_24h": leaves.get(ieee, 0),
                "started": self.started,
                "network_down": record.get("instance") in down,
            }
            parent = (record.get("routing") or {}).get("parent") or {}
            if parent.get("ieee"):
                extra["parent"] = {
                    "name": _display_name(self.data["devices"].get(parent["ieee"]) or {}) or parent["ieee"],
                    "lqi": parent.get("lqi"),
                }
            if ieee in spof:
                extra["single_point"] = spof[ieee]
            # Only end devices pick a parent to route through; a router's tree parent is just
            # whoever let it join and says little about how its traffic flows.
            if record.get("type") == "EndDevice":
                extra["router_hops"] = hop_summary(self._hop_rows(record, now))
            out[ieee] = extra
        return out

    def evaluate_health(self) -> tuple[list[dict], bool]:
        now = time.time()
        self.coordinator_backups()
        with self.lock:
            suggestions = self._suggestions()
            context = self._context(now)
            issues = []
            for record in self.data["devices"].values():
                summary = {**self.metrics.summary(record["ieee"], now), **context.get(record["ieee"], {})}
                for issue in device_issues(record, summary, self.options, now, suggestions.get(record["ieee"], [])):
                    issue["name"] = _display_name(record) or record["ieee"]
                    issue["instance"] = record["instance"]
                    issues.append(issue)
            group_router_outages(issues, self.data["devices"], self.options, now)
            issues.extend(self._network_issues(now))
            issues.extend(self._group_issues(now))
            order = {"warning": 0, "info": 1}
            issues.sort(key=lambda i: (order.get(i["severity"], 2), i["name"].lower()))

            def shape(items: list[dict]) -> list:
                return [(i["key"], i.get("behind"), i.get("devices")) for i in items]

            changed = shape(issues) != shape(self.issues)
            self.issues = issues
            if changed:
                self.revision += 1
        return issues, changed

    # ------------------------------------------------------ leftovers in HA

    def leftover_context(self) -> tuple[bool, dict[str, dict]]:
        """What the tracker knows about each IEEE, for ha.find_leftovers.

        Not ready until every tracked network has sent its device list since the
        app started: before that, every device would look unknown."""
        with self.lock:
            ready = self.lists_received >= set(self.base_topics)
            devices = self.data["devices"]
            known = {}
            for ieee, record in devices.items():
                successor = record.get("successor")
                known[ieee] = {
                    "active": record["status"] == STATUS_ACTIVE,
                    "name": _display_name(record),
                    "removed_at": record.get("removed_at"),
                    "successor": {
                        "ieee": successor,
                        "name": _display_name(devices.get(successor) or {}) or successor,
                    }
                    if successor
                    else None,
                }
            return ready, known

    def set_ha_leftovers(self, leftovers: list[dict]) -> None:
        with self.lock:
            if leftovers != self.ha_leftovers:
                self.ha_leftovers = leftovers
                self.revision += 1

    # ----------------------------------------------------------------- public

    def _oui_vendor(self, ieee: str) -> str | None:
        return self.oui.get(ieee[2:8].upper()) if ieee.startswith("0x") else None

    def snapshot_json(self) -> bytes:
        now = time.time()
        with self.lock:
            suggestions = self._suggestions()
            issues_by_device: dict[str, list[dict]] = {}
            for issue in self.issues:
                issues_by_device.setdefault(issue["ieee"], []).append(issue)
            devices = []
            for record in self.data["devices"].values():
                ieee = record["ieee"]
                item = dict(record)
                item["metrics"] = self.metrics.summary(ieee, now)
                item["uptime_30d"] = uptime(record, now)
                item["oui_vendor"] = self._oui_vendor(ieee)
                item["ha_device_id"] = self.ha_device_ids.get(ieee)
                item["battery_notes"] = self.ha_battery.get(ieee)
                item["issues"] = issues_by_device.get(ieee, [])
                item["battery_record"] = self.battery_summary(record) if self.is_battery_device(record) else None
                item["suggestions"] = suggestions.get(ieee, [])
                bridge_info = (self.data.get("bridges") or {}).get(record.get("instance")) or {}
                item["offline_timing"] = offline_timing(
                    record, (bridge_info.get("info") or {}).get("availability"), self.options, now
                )
                devices.append(item)
            payload = {
                "devices": devices,
                "events": self.data["events"],
                "instances": self.base_topics,
                "imported": self.data.get("imported"),
                "issues": self.issues,
                "networkmap": self.data.get("networkmap") or {},
                "permit": self.permit,
                "battery_shopping": self.battery_shopping(60),
                "ha_leftovers": self.ha_leftovers,
                "bridges": self.data.get("bridges") or {},
                "radio": self.radio(),
                "network_hours": self.network_hours(now),
                "single_points": {bt: self.single_points(bt) for bt in self.base_topics},
                "backups": self._backups,
                "groups": self.groups,
                "group_health": self.group_health(now),
                "scan_times": {bt: [x["at"] for x in self.scans.get(bt) or []] for bt in self.base_topics},
                "pairing": {bt: {**s, "joined": list(s["joined"])} for bt, s in self.pairing_sessions.items()},
                "panels": self.options.get("_panels", {}),
                "options": {k: v for k, v in self.options.items() if not k.startswith("_")},
                "alerts_paused": self.alert_pause_state(),
                "version": self.version,
            }
            return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def export_csv(self) -> bytes:
        now = time.time()
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        columns = [
            "ieee",
            "name",
            "former_names",
            "status",
            "network",
            "vendor",
            "model",
            "model_id",
            "manufacturer",
            "radio_vendor",
            "description",
            "type",
            "power_source",
            "firmware",
            "first_seen",
            "last_seen",
            "removed_at",
            "lqi_24h",
            "battery",
            "battery_type",
            "battery_quantity",
            "battery_installed",
            "battery_last_replaced",
            "battery_replacements",
            "uptime_30d",
            "area",
            "ha_name",
            *ASSET_FIELDS,
            "critical",
            "note",
        ]
        writer.writerow(columns)
        with self.lock:
            for record in sorted(self.data["devices"].values(), key=lambda r: (_display_name(r) or "~").lower()):
                if record.get("type") == "Coordinator":
                    continue
                name = _display_name(record) or ""
                former = sorted(
                    {
                        n["name"]
                        for n in record.get("names") or []
                        if n.get("name") and n["name"] != name and not IEEE_RE.match(n["name"].lower())
                    }
                )
                summary = self.metrics.summary(record["ieee"], now)
                battery = self.battery_summary(record)
                asset = record.get("asset") or {}
                writer.writerow(
                    [
                        record["ieee"],
                        name,
                        "; ".join(former),
                        record["status"],
                        record.get("instance") or "",
                        record.get("vendor") or "",
                        record.get("model") or "",
                        record.get("model_id") or "",
                        record.get("manufacturer") or "",
                        self._oui_vendor(record["ieee"]) or "",
                        record.get("description") or "",
                        record.get("type") or "",
                        record.get("power_source") or "",
                        " ".join(str(x) for x in (record.get("software_build_id"), record.get("date_code")) if x),
                        record.get("first_seen") or "",
                        record.get("last_seen") or "",
                        record.get("removed_at") or "",
                        summary.get("lqi_24h") if summary.get("lqi_24h") is not None else "",
                        record.get("battery") if record.get("battery") is not None else "",
                        battery["type"],
                        battery["quantity"] or "",
                        battery["installed"],
                        battery["last_replaced"] or "",
                        len(battery["replacements"]),
                        uptime(record, now) or "",
                        record.get("area") or "",
                        record.get("ha_name") or "",
                        *[asset.get(field, "") for field in ASSET_FIELDS],
                        "yes" if record.get("critical") else "",
                        record.get("note") or "",
                    ]
                )
        return buffer.getvalue().encode("utf-8-sig")

    def set_note(self, ieee: str, note: str) -> bool:
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None:
                return False
            record["note"] = note
            self._changed(structural=True)
            return True

    def set_asset(self, ieee: str, asset: dict) -> bool:
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None:
                return False
            clean = {field: str(asset.get(field) or "").strip()[:200] for field in ASSET_FIELDS}
            record["asset"] = {k: v for k, v in clean.items() if v}
            self._changed(structural=True)
            return True

    # ---------------------------------------------------------------- battery

    def _record_replacement(self, record: dict, date: str, source: str, note: str = "") -> dict:
        info = record.setdefault("battery_info", {})
        replacements = info.setdefault("replacements", [])
        entry = {"date": date, "source": source, "note": note[:200], "recorded": _now()}
        if replacements and replacements[-1].get("date") == date and source == "Detected":
            return replacements[-1]
        replacements.append(entry)
        replacements.sort(key=lambda r: r.get("date") or "")
        del replacements[: max(0, len(replacements) - 50)]
        self._add_event(
            _now(),
            record.get("instance") or "unknown",
            record["ieee"],
            record.get("friendly_name"),
            "battery replaced",
            f"{date}{f' · {note}' if note else ''} ({source.lower()})",
        )
        return entry

    def set_battery_info(self, ieee: str, values: dict) -> bool:
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None:
                return False
            info = record.setdefault("battery_info", {})
            info["type"] = str(values.get("type") or "").strip()[:40]
            try:
                quantity = int(values.get("quantity") or 0)
            except (TypeError, ValueError):
                quantity = 0
            info["quantity"] = quantity if 0 < quantity <= 20 else None
            installed = str(values.get("installed") or "").strip()
            info["installed"] = installed if re.fullmatch(r"\d{4}-\d{2}-\d{2}", installed) else ""
            self._changed(structural=True)
            return True

    def add_battery_replacement(self, ieee: str, date: str, source: str, note: str = "") -> dict | None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
            return None
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None:
                return None
            entry = self._record_replacement(record, date, source, note)
            self._changed(structural=True)
            return entry

    def remove_battery_replacement(self, ieee: str, index: int) -> bool:
        with self.lock:
            record = self.data["devices"].get(ieee)
            replacements = ((record or {}).get("battery_info") or {}).get("replacements") or []
            if not 0 <= index < len(replacements):
                return False
            replacements.pop(index)
            self._changed(structural=True)
            return True

    def battery_summary(self, record: dict) -> dict:
        """Merge the tracker's battery record with Battery Notes for display/export."""
        info = record.get("battery_info") or {}
        notes = self.ha_battery.get(record["ieee"]) or {}
        btype, quantity = info.get("type") or "", info.get("quantity")
        source = "you" if btype else None
        if not btype and notes.get("type"):
            match = re.match(r"^\s*(\d+)\s*[x×]\s*(.+)$", notes["type"])
            btype, quantity = (match.group(2).strip(), int(match.group(1))) if match else (notes["type"], quantity or 1)
            source = "battery_notes"
        if not btype and self.battery_lib is not None:
            found = self.battery_lib.lookup(record)
            if found:
                btype, quantity = found[0], quantity or found[1]
                source = "library"
        replacements = info.get("replacements") or []
        last = replacements[-1]["date"] if replacements else None
        notes_date = (notes.get("last_replaced") or "")[:10] or None
        if notes_date and (not last or notes_date > last):
            last = notes_date
        return {
            "type": btype,
            "quantity": quantity,
            "label": f"{quantity}× {btype}" if btype and quantity and quantity > 1 else btype,
            "installed": info.get("installed") or "",
            "last_replaced": last,
            "replacements": replacements,
            "source": source,
            "from_battery_notes": source == "battery_notes",
        }

    def is_battery_device(self, record: dict) -> bool:
        return (
            record.get("power_source") == "Battery"
            or isinstance(record.get("battery"), (int, float))
            or bool((record.get("battery_info") or {}).get("type"))
            or record["ieee"] in self.ha_battery
        )

    def set_report_every(self, ieee: str, seconds: int | None) -> bool:
        """Per-device "should report at least every" interval in seconds; None or 0 turns it off."""
        return not self.set_report_every_many([{"ieee": ieee, "seconds": seconds}])["errors"]

    def set_report_every_many(self, items: list) -> dict:
        """Set or clear the report interval for several devices, with one health run at the end.

        Each item is {"ieee", "seconds"}; seconds None or 0 turns it off. Valid items are applied,
        invalid ones are listed in "errors" as {"ieee", "error"}.
        """
        applied = 0
        errors: list[dict] = []
        with self.lock:
            for item in items:
                item = item if isinstance(item, dict) else {}
                ieee, seconds = str(item.get("ieee") or ""), item.get("seconds")
                record = self.data["devices"].get(ieee)
                if record is None:
                    errors.append({"ieee": ieee, "error": "unknown device"})
                elif seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, (int, float))):
                    errors.append({"ieee": ieee, "error": "seconds must be a number"})
                elif seconds and not REPORT_EVERY_MIN <= seconds <= REPORT_EVERY_MAX:
                    errors.append({"ieee": ieee, "error": f"seconds must be {REPORT_EVERY_MIN} to {REPORT_EVERY_MAX}"})
                else:
                    if seconds:
                        record["report_every"] = int(seconds)
                    else:
                        record.pop("report_every", None)
                    applied += 1
            if applied:
                self._changed(structural=True)
        if applied:
            self.evaluate_health()
        return {"ok": not errors, "applied": applied, "errors": errors}

    def late_due(self, now: float) -> bool:
        """True once for each device that passes its report interval, so health runs then, not up to 60 s later."""
        due = False
        with self.lock:
            for ieee, record in self.data["devices"].items():
                deadline = late_deadline(record, self.started)
                if deadline is not None and now >= deadline and self._late_checked.get(ieee) != deadline:
                    self._late_checked[ieee] = deadline
                    due = True
        return due

    def set_muted(self, ieee: str, kind: str, muted: bool, days: float | None = None) -> bool:
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None or not re.fullmatch(r"[a-z_]{2,30}", kind):
                return False
            current = mutes_of(record)
            if muted:
                until = None
                if days:
                    until = datetime.fromtimestamp(time.time() + float(days) * 86400, tz=timezone.utc).isoformat(
                        timespec="seconds"
                    )
                current[kind] = until
            else:
                current.pop(kind, None)
            record["muted"] = current
            self._changed(structural=True)
        self.evaluate_health()
        return True

    # ------------------------------------------------------------ alert pause

    def pause_alerts(self, minutes: Any = None, until: Any = None, at: Any = None) -> dict:
        """Hold every alert for `minutes`, until an ISO time, or until the next HH:MM local time.

        With none of them the pause lasts until resume_alerts(). Raises ValueError for bad input.
        """
        now = time.time()
        if minutes is not None:
            if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
                raise ValueError("minutes must be a number")
            if not 1 <= minutes <= MAX_PAUSE_MINUTES:
                raise ValueError(f"minutes must be from 1 to {MAX_PAUSE_MINUTES}")
            end: float | None = now + float(minutes) * 60
        elif until not in (None, ""):
            iso = self._to_utc_iso(str(until))
            if iso is None:
                raise ValueError("until must be a date and time like 2026-11-14T08:00")
            end = datetime.fromisoformat(iso).timestamp()
            if end <= now:
                raise ValueError("until must be in the future")
        elif at not in (None, ""):
            match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(at).strip())
            if not match:
                raise ValueError("at must be a time like 08:00")
            day = datetime.fromtimestamp(now, tz=self.local_tz).date()
            wall = dtime(int(match[1]), int(match[2]))
            end = datetime.combine(day, wall, tzinfo=self.local_tz).timestamp()
            if end <= now:
                end = datetime.combine(day + timedelta(days=1), wall, tzinfo=self.local_tz).timestamp()
        else:
            end = None
        with self.lock:
            since = (self.data.get("alerts_paused") or {}).get("since") if self.alerts_paused() else None
            self.data["alerts_paused"] = {
                "since": since or datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="seconds"),
                "until": datetime.fromtimestamp(end, tz=timezone.utc).isoformat(timespec="seconds") if end else None,
            }
            self._changed(structural=True)
            state = self.alert_pause_state()
        if end is None:
            _LOGGER.info("Alerts paused until resumed")
        else:
            _LOGGER.info(
                "Alerts paused until %s", datetime.fromtimestamp(end, tz=self.local_tz).strftime("%Y-%m-%d %H:%M")
            )
        return state

    def resume_alerts(self) -> bool:
        """End a pause now. Returns False when alerts weren't paused."""
        with self.lock:
            if not self.alerts_paused():
                return False
            self.data["alerts_paused"] = None
            self._changed(structural=True)
        _LOGGER.info("Alerts resumed")
        return True

    def alerts_paused(self) -> bool:
        """True while a pause is running. A pause whose time has passed is cleared here."""
        with self.lock:
            pause = self.data.get("alerts_paused")
            if not pause:
                return False
            until = pause.get("until")
            try:
                if until is None or datetime.fromisoformat(until).timestamp() > time.time():
                    return True
            except (TypeError, ValueError):
                pass
            self.data["alerts_paused"] = None
            self._changed(structural=True)
        _LOGGER.info("Alert pause ended")
        return False

    def alert_pause_state(self) -> dict | None:
        """The running pause for the UI, with how many issues will be sent when it ends."""
        with self.lock:
            if not self.alerts_paused():
                return None
            pause = self.data["alerts_paused"]
            pending = 0
            if self.data.get("alerts_initialised"):
                enabled = notify_kinds(self.options)
                sent = set(self.data.get("alerted") or [])
                pending = sum(1 for i in self.issues if i["kind"] in enabled and i["key"] not in sent)
            return {"since": pause.get("since"), "until": pause.get("until"), "pending": pending}

    def set_critical(self, ieee: str, critical: bool) -> bool:
        """Mark a device as critical (shorter offline delay, high-priority alerts) or clear the mark."""
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None:
                return False
            if critical:
                record["critical"] = True
            else:
                record.pop("critical", None)
            self._changed(structural=True)
        self.evaluate_health()
        return True

    def is_critical(self, ieee: str) -> bool:
        with self.lock:
            return bool((self.data["devices"].get(ieee) or {}).get("critical"))

    # ---------------------------------------------------------------- alerts

    def in_quiet_hours(self, now: float | None = None) -> bool:
        """True inside the quiet_start-quiet_end window (local time, may cross midnight)."""
        if not self.options.get("quiet_hours"):
            return False
        start = _clock_minutes(self.options.get("quiet_start", "23:00"))
        end = _clock_minutes(self.options.get("quiet_end", "07:00"))
        if start is None or end is None or start == end:
            return False
        local = datetime.fromtimestamp(time.time() if now is None else now, tz=self.local_tz)
        minute = local.hour * 60 + local.minute
        return start <= minute < end if start < end else minute >= start or minute < end

    def _quiet_state(self, now: float) -> dict:
        quiet = self.data.get("quiet")
        if not isinstance(quiet, dict):
            quiet = {"since": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="seconds")}
        quiet.setdefault("issues", {})
        quiet.setdefault("events", [])
        self.data["quiet"] = quiet
        return quiet

    def hold_event(self, title: str, message: str) -> bool:
        """Keep a join/leave/interview alert for the digest when it's quiet hours. False: send it now."""
        now = time.time()
        with self.lock:
            if not self.in_quiet_hours(now):
                return False
            quiet = self._quiet_state(now)
            quiet["events"] = [
                *quiet["events"],
                {
                    "time": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="seconds"),
                    "title": title,
                    "message": message,
                },
            ][-MAX_HELD_EVENTS:]
            self.dirty = True
        return True

    def plan_alerts(self, issues: list[dict], enabled: set[str]) -> dict:
        """Decide what one alert check sends and updates the record of what was sent.

        Returns {"send": [issues to alert now], "dismiss": [keys that cleared], "digest": (title, message) | None,
        "first": True on the very first check, when existing issues are only recorded}.
        During quiet hours new issues are held (critical devices excepted); the first check after quiet hours
        sends one digest instead of the held alerts.
        """
        now = time.time()
        stamp = datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="seconds")
        current = {issue["key"]: issue for issue in issues if issue["kind"] in enabled}
        with self.lock:
            initialised = self.data.get("alerts_initialised")
            sent = set(self.data.get("alerted") or [])
            self.data["alerts_initialised"] = True
            self.dirty = True
            if not enabled:
                # Alerts are off: nothing to hold, nothing to report later.
                self.data["quiet"] = None
            if not initialised:
                self.data["alerted"] = sorted(current)
                return {"send": [], "dismiss": [], "digest": None, "first": True}
            # Devices behind an offline router never alert on their own: the router's alert covers them.
            behind = {key for key, issue in current.items() if issue.get("behind")}
            new = [current[key] for key in sorted(set(current) - behind - sent)]
            dismiss = sorted(sent - set(current))
            urgent = [issue for issue in new if issue.get("critical")]
            if enabled and self.in_quiet_hours(now):
                held = self._quiet_state(now)["issues"]
                for issue in new:
                    if issue.get("critical"):
                        held.pop(issue["key"], None)
                        continue
                    entry = held.setdefault(issue["key"], {"first": stamp})
                    entry.pop("cleared", None)
                    entry.update(name=issue["name"], label=issue["label"], detail=issue["detail"])
                for key, entry in held.items():
                    if key not in current and "cleared" not in entry:
                        entry["cleared"] = stamp
                self.data["alerted"] = sorted((sent & set(current)) | {issue["key"] for issue in urgent})
                return {"send": urgent, "dismiss": dismiss, "digest": None, "first": False}
            digest = None
            send = new
            if self.data.get("quiet"):
                # Quiet hours are over: one digest, and what's still open counts as alerted.
                digest = self._digest(self.data["quiet"], [i for i in new if not i.get("critical")], current)
                self.data["quiet"] = None
                send = urgent
            self.data["alerted"] = sorted((set(current) - behind) | (behind & sent))
            return {"send": send, "dismiss": dismiss, "digest": digest, "first": False}

    def _digest(self, quiet: dict, still_open: list[dict], current: dict) -> tuple[str, str] | None:
        held = quiet.get("issues") or {}
        resolved = sorted(
            (entry for key, entry in held.items() if key not in current and entry.get("cleared")),
            key=lambda entry: entry["first"],
        )
        events = quiet.get("events") or []
        if not still_open and not resolved and not events:
            return None

        def clock(iso: str) -> str:
            try:
                return datetime.fromisoformat(iso).astimezone(self.local_tz).strftime("%H:%M")
            except (TypeError, ValueError):
                return "?"

        def section(heading: str, lines: list[str]) -> list[str]:
            if not lines:
                return []
            more = len(lines) - DIGEST_LINES
            return [heading, *(f"- {line}" for line in lines[:DIGEST_LINES])] + (
                [f"- and {more} more"] if more > 0 else []
            )

        open_lines = []
        for issue in still_open:
            since = held.get(issue["key"], {}).get("first")
            when = f" (since {clock(since)})" if since else ""
            open_lines.append(f"{issue['name']} - {issue['label']}{when}: {_first_sentence(issue['detail'])}")
        resolved_lines = [
            f"{entry['name']} - {entry['label']} ({clock(entry['first'])}-{clock(entry['cleared'])})"
            for entry in resolved
        ]
        event_lines = [f"{clock(e.get('time'))} {e.get('title')}: {_first_sentence(e.get('message'))}" for e in events]
        n, m = len(still_open), len(resolved)
        title = f"Zigbee overnight: {n} issue{'s' if n != 1 else ''}, {m} resolved"
        body = [
            *section("Still open:", open_lines),
            *section("Resolved:", resolved_lines),
            *section("Held alerts:", event_lines),
        ]
        return title, "\n".join(body)

    def set_limits(self, ieee: str, changes: dict) -> dict:
        """Set or clear the device's own battery/frozen limits. Only keys in `changes` are touched."""
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None:
                return {"ok": False, "error": "Unknown device"}
            limits, error = clean_limits(record.get("limits") or {}, changes)
            if error:
                return {"ok": False, "error": error}
            if limits:
                record["limits"] = limits
            else:
                record.pop("limits", None)
            self._changed(structural=True)
        self.evaluate_health()
        return {"ok": True, "limits": limits}

    # ------------------------------------------------------------ Z2M actions

    def _bridge(self, base_topic: str) -> dict:
        bridges = self.data.setdefault("bridges", {})
        return bridges.setdefault(
            base_topic, {"state": None, "since": None, "outages": [], "restarts": [], "info": {}, "health": {}}
        )

    def _handle_bridge_state(self, base_topic: str, text: str) -> None:
        state = text.strip()
        if state.startswith("{"):
            state = (json.loads(state) or {}).get("state", "")
        if state not in ("online", "offline"):
            return
        bridge = self._bridge(base_topic)
        previous = bridge.get("state")
        if previous == state:
            return
        now = _now()
        bridge["state"] = state
        bridge["since"] = now
        if state == "offline":
            bridge["outages"].append({"start": now, "end": None})
            del bridge["outages"][: max(0, len(bridge["outages"]) - 100)]
            self._add_event(
                now,
                base_topic,
                "network",
                "Network",
                "network offline",
                "Zigbee2MQTT stopped (coordinator or service offline)",
            )
        elif previous == "offline":
            if bridge["outages"] and not bridge["outages"][-1].get("end"):
                bridge["outages"][-1]["end"] = now
            bridge["restarts"].append(now)
            del bridge["restarts"][: max(0, len(bridge["restarts"]) - 100)]
            self._add_event(now, base_topic, "network", "Network", "network online", "Zigbee2MQTT is running again")
        self._changed(structural=True)

    def _handle_bridge_health(self, base_topic: str, health: dict) -> None:
        bridge = self._bridge(base_topic)
        process = health.get("process") or {}
        uptime = process.get("uptime_sec")
        previous = (bridge.get("health") or {}).get("uptime_sec")
        if isinstance(uptime, (int, float)) and isinstance(previous, (int, float)) and uptime + 120 < previous:
            now = _now()
            bridge["restarts"].append(now)
            del bridge["restarts"][: max(0, len(bridge["restarts"]) - 100)]
            self._add_event(
                now, base_topic, "network", "Network", "zigbee2mqtt restarted", f"Was up {int(previous // 3600)} h"
            )
            self.revision += 1
        for ieee, stats in (health.get("devices") or {}).items():
            record = self.data["devices"].get(str(ieee).lower())
            if record is None or not isinstance(stats, dict):
                continue
            record["z2m_stats"] = {
                "messages": stats.get("messages"),
                "per_sec": stats.get("messages_per_sec"),
                "leave_count": stats.get("leave_count"),
                "nwk_changes": stats.get("network_address_changes"),
                "since": _now()
                if not isinstance(uptime, (int, float))
                else datetime.fromtimestamp(time.time() - uptime, tz=timezone.utc).isoformat(timespec="minutes"),
            }
        mqtt = health.get("mqtt") or {}
        memory = process.get("memory_used_mb")
        bridge["health"] = {
            "uptime_sec": uptime,
            "memory_mb": memory,
            "mqtt_connected": mqtt.get("connected"),
            "mqtt_published": mqtt.get("published"),
            "mqtt_received": mqtt.get("received"),
            "os_load": ((health.get("os") or {}).get("load_average") or [None])[0],
            "updated": _now(),
        }
        if isinstance(memory, (int, float)) and not isinstance(memory, bool) and memory > 0:
            bridge["memory_days"] = record_memory(bridge.get("memory_days") or [], self._today(), round(memory, 1))
        self.dirty = True

    def _today(self) -> date:
        return datetime.fromtimestamp(time.time(), tz=self.local_tz).date()

    def _handle_info(self, base_topic: str, info: dict) -> None:
        coordinator = info.get("coordinator") or {}
        network = info.get("network") or {}
        meta = coordinator.get("meta") or {}
        firmware = meta.get("revision") or meta.get("version") or meta.get("firmware")
        slim = {
            "version": info.get("version"),
            "commit": info.get("commit"),
            "coordinator_type": coordinator.get("type"),
            "coordinator_ieee": coordinator.get("ieee_address"),
            "firmware": str(firmware) if firmware is not None else None,
            "channel": network.get("channel"),
            "pan_id": network.get("pan_id"),
            "restart_required": bool(info.get("restart_required")),
            # Only Z2M's availability settings, not its whole config, to keep history.json small.
            "availability": z2m_availability(info),
        }
        bridge = self._bridge(base_topic)
        if bridge.get("info") != slim:
            old = bridge.get("info") or {}
            if old.get("version") and slim["version"] and old["version"] != slim["version"]:
                self._add_event(
                    _now(),
                    base_topic,
                    "network",
                    "Network",
                    "zigbee2mqtt updated",
                    f"{old['version']} → {slim['version']}",
                )
            bridge["info"] = slim
            self._changed(structural=True)
        on = bool(info.get("permit_join"))
        # Z2M 2.x: permit_join_end is an epoch in ms. 1.x: permit_join_timeout is seconds left.
        end = info.get("permit_join_end") or info.get("permit_join_timeout")
        end_iso = None
        if isinstance(end, (int, float)) and not isinstance(end, bool) and end > 0:
            end_ts = end / 1000 if end > 1e11 else time.time() + end
            end_iso = datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat(timespec="seconds")
        state = {"on": on, "end": end_iso}
        previous = self.permit.get(base_topic)
        if on and base_topic not in self.permit_meta:
            self._permit_opened(base_topic, known_closed=previous is not None and not previous.get("on"))
        elif not on:
            self.permit_meta.pop(base_topic, None)
            self._permit_hint.pop(base_topic, None)
        if previous != state:
            self.permit[base_topic] = state
            if not on and base_topic in self.pairing_sessions:
                self.pairing_sessions[base_topic]["active"] = False
            self.revision += 1

    def _handle_permit_request(self, base_topic: str, text: str) -> None:
        """Someone published bridge/request/permit_join. Remember it if it opens pairing and isn't ours."""
        try:
            payload = json.loads(text)
        except ValueError:
            payload = text
        transaction = str(payload.get("transaction") or "") if isinstance(payload, dict) else ""
        if transaction in self._pending or transaction.startswith("z2mt-") or not _permit_opening(payload):
            return
        device = payload.get("device") if isinstance(payload, dict) else None
        self._permit_hint[base_topic] = {"origin": "mqtt", "at": time.time(), "device": device}

    def _permit_opened(self, base_topic: str, known_closed: bool) -> None:
        """Z2M reports pairing open and we hadn't seen it open: work out what opened it."""
        hint = self._permit_hint.pop(base_topic, None)
        if hint and time.time() - hint["at"] <= PERMIT_HINT_SECONDS:
            origin, device = hint["origin"], hint.get("device")
        else:
            # Closed a moment ago and no MQTT request: the frontend (it talks to Z2M over its websocket).
            # Open when we first hear from Z2M: we can't tell who opened it.
            origin, device = ("frontend" if known_closed else None), None
        self.permit_meta[base_topic] = {"since": time.time(), "origin": origin, "device": device}

    def _z2m_major(self, base_topic: str) -> int | None:
        version = str((self._bridge(base_topic).get("info") or {}).get("version") or "")
        match = re.match(r"(\d+)\.", version)
        return int(match.group(1)) if match else None

    def permit_payload(self, base_topic: str, seconds: int) -> dict[str, Any]:
        """bridge/request/permit_join payload: 1.x wants {"value": bool, "time"}, 2.x only {"time"} (0 closes)."""
        if self._z2m_major(base_topic) == 1:
            return {"value": True, "time": seconds} if seconds else {"value": False}
        return {"time": seconds}

    def pairing_open_issue(self, base_topic: str, now: float) -> dict | None:
        """The pairing_open network issue, or None while pairing is closed or only briefly open by us."""
        permit = self.permit.get(base_topic) or {}
        meta = self.permit_meta.get(base_topic)
        if not permit.get("on") or meta is None:
            return None
        age = max(0.0, now - meta["since"])
        origin = meta.get("origin")
        if age < PAIRING_OPEN_ALERT and origin in ("app", None):
            return None
        mins = max(1, round(age / 60))
        left = None
        if permit.get("end"):
            try:
                left = datetime.fromisoformat(permit["end"]).timestamp() - now
            except ValueError:
                left = None
            if left is not None and left <= 0:
                left = None
        label = (
            "Main"
            if base_topic == "zigbee2mqtt"
            else base_topic.replace("zigbee2mqtt_", "").replace("_", " ").capitalize()
        )
        parts = [f"Open for {mins} min"]
        if origin in PERMIT_ORIGIN_TEXT:
            parts.append(PERMIT_ORIGIN_TEXT[origin])
        if meta.get("device"):
            parts.append(f"only through {meta['device']}")
        if left is not None:
            parts.append(f"closes by itself in {max(1, round(left / 60))} min")
        accepted = (
            f"has accepted new devices for {mins} minute{'' if mins == 1 else 's'}"
            if age >= 60
            else "is accepting new devices"
        )
        return {
            "key": f"pairing_open:{base_topic}",
            "kind": "pairing_open",
            "label": "Pairing open",
            "severity": "warning",
            "ieee": f"network:{base_topic}",
            "title": "Pairing open",
            "detail": " · ".join(parts),
            "msg": "pairing_open",
            "params": {
                "seconds": int(age),
                "origin": origin,
                "device": meta.get("device"),
                "left": int(left) if left is not None else None,
            },
            "notify_title": "Zigbee pairing still open",
            "notify_message": f"{label} network {accepted}. Close it if you're not pairing anything.",
            "name": f"{label} network",
            "instance": base_topic,
        }

    def start_pairing(self, base_topic: str, seconds: int, via: str | None = None, target: str | None = None) -> dict:
        seconds = max(0, min(254, int(seconds)))
        with self.lock:
            payload = self.permit_payload(base_topic, seconds)
        via_name = None
        if via and seconds:
            with self.lock:
                router = self.data["devices"].get(via)
                if (
                    router is None
                    or router.get("instance") != base_topic
                    or router.get("type") not in ("Router", "Coordinator")
                    or router["status"] != STATUS_ACTIVE
                ):
                    return {"ok": False, "error": "Choose a router or the coordinator on this network"}
                via_name = router.get("friendly_name") or (
                    "Coordinator" if router.get("type") == "Coordinator" else via
                )
            payload["device"] = "Coordinator" if router.get("type") == "Coordinator" else via_name
        if seconds:
            with self.lock:
                self._permit_hint[base_topic] = {"origin": "app", "at": time.time(), "device": payload.get("device")}
        result = self.request(base_topic, "permit_join", payload, timeout=15)
        ok = bool(result and result.get("status") == "ok")
        with self.lock:
            if ok and seconds:
                self.pairing_sessions[base_topic] = {
                    "started": _now(),
                    "seconds": seconds,
                    "active": True,
                    "joined": [],
                    "via": via,
                    "via_name": via_name,
                    "target": target,
                    "rejoined": None,
                }
                end = datetime.fromtimestamp(time.time() + seconds, tz=timezone.utc).isoformat(timespec="seconds")
                self.permit[base_topic] = {"on": True, "end": end}
                self.permit_meta[base_topic] = {"since": time.time(), "origin": "app", "device": payload.get("device")}
                self._permit_hint.pop(base_topic, None)
            elif ok:
                if base_topic in self.pairing_sessions:
                    self.pairing_sessions[base_topic]["active"] = False
                self.permit[base_topic] = {"on": False, "end": None}
                self.permit_meta.pop(base_topic, None)
                self._permit_hint.pop(base_topic, None)
            self.revision += 1
        return {"ok": ok, "error": None if ok else friendly_z2m_error((result or {}).get("error") or "No response")}

    def rename(self, ieee: str, new_name: str) -> dict:
        new_name = (new_name or "").strip()
        if not new_name or len(new_name) > 120 or new_name.endswith("/") or re.search(r"[+#]", new_name):
            return {"ok": False, "error": "Enter a name without + or # and no trailing slash"}
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None or record["status"] != STATUS_ACTIVE:
                return {"ok": False, "error": "Device is not active in Zigbee2MQTT"}
            base_topic = record["instance"]
        result = self.request(
            base_topic, "device/rename", {"from": ieee, "to": new_name, "homeassistant_rename": True}, timeout=20
        )
        ok = bool(result and result.get("status") == "ok")
        return {"ok": ok, "error": None if ok else friendly_z2m_error((result or {}).get("error") or "No response")}

    def ota(self, ieee: str, action: str) -> dict:
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None or record["status"] != STATUS_ACTIVE:
                return {"ok": False, "error": "Device is not active in Zigbee2MQTT"}
            base_topic = record["instance"]
            name = record.get("friendly_name")
        if action == "check":
            result = self.request(base_topic, "device/ota_update/check", {"id": ieee}, timeout=60)
            ok = bool(result and result.get("status") == "ok")
            available = ((result or {}).get("data") or {}).get(
                "update_available", ((result or {}).get("data") or {}).get("updateAvailable")
            )
            return {
                "ok": ok,
                "available": available,
                "error": None if ok else friendly_z2m_error((result or {}).get("error") or "No response"),
            }
        if action == "update":
            with self.lock:
                self._add_event(_now(), base_topic, ieee, name, "firmware update started", "")
                rec = self.data["devices"][ieee]
                rec["update"] = {**(rec.get("update") or {}), "state": "updating", "progress": 0}
                self._changed(structural=True)

            def run() -> None:
                result = self.request(base_topic, "device/ota_update/update", {"id": ieee}, timeout=3600)
                with self.lock:
                    if not result or result.get("status") != "ok":
                        self._add_event(
                            _now(),
                            base_topic,
                            ieee,
                            name,
                            "firmware update failed",
                            friendly_z2m_error((result or {}).get("error") or "No response"),
                        )
                        rec = self.data["devices"].get(ieee)
                        if rec and (rec.get("update") or {}).get("state") == "updating":
                            rec["update"]["state"] = "available"
                    self._changed(structural=True)

            threading.Thread(target=run, name="ota", daemon=True).start()
            return {"ok": True}
        return {"ok": False, "error": "Unknown action"}

    def _replaced_since_reading(self, record: dict) -> bool:
        """True when the battery was marked replaced after the device last reported its level.

        Keeps a device off the shopping list from the moment it's ticked until a new reading
        arrives. If that reading is still low, the device goes back on the list.
        """
        marked = []
        for entry in (record.get("battery_info") or {}).get("replacements") or []:
            if entry.get("source") == "Detected":
                continue
            day = str(entry.get("date") or "")
            end_of_day = self._to_utc_iso(f"{day}T23:59:59") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else None
            marked.append(_min_time(self._to_utc_iso(entry.get("recorded")), end_of_day))
        marked.append(self._to_utc_iso((self.ha_battery.get(record["ieee"]) or {}).get("last_replaced")))
        last_marked = _max_time(*marked)
        if not last_marked:
            return False
        reading = record.get("battery_reported")
        if not reading:
            with self.metrics.lock:
                points = (self.metrics.series.get(record["ieee"]) or {}).get("bat") or []
                reading = self._to_utc_iso(points[-1][0]) if points else None
        return reading is None or last_marked >= reading

    def battery_shopping(self, days: int = 60) -> list[dict]:
        """Batteries needed soon, grouped by type."""
        now = time.time()
        groups: dict[str, dict] = {}
        with self.lock:
            for record in self.data["devices"].values():
                if record["status"] != STATUS_ACTIVE or not self.is_battery_device(record):
                    continue
                if self._replaced_since_reading(record):
                    continue
                level = record.get("battery")
                est = self.metrics.summary(record["ieee"], now).get("battery_estimate_days")
                due = (
                    (est is not None and est <= days)
                    or record.get("battery_low") is True
                    or (isinstance(level, (int, float)) and level <= 25 and est is None)
                    or (isinstance(level, (int, float)) and level <= limit_of(record, self.options, "battery_percent"))
                )
                if not due:
                    continue
                info = self.battery_summary(record)
                key = info["type"] or "Unknown type"
                group = groups.setdefault(key, {"type": key, "count": 0, "devices": []})
                group["count"] += int(info["quantity"] or 1)
                group["devices"].append(
                    {
                        "ieee": record["ieee"],
                        "name": _display_name(record) or record["ieee"],
                        "battery": level,
                        "days": est,
                        "quantity": info["quantity"] or 1,
                    }
                )
        for group in groups.values():
            group["devices"].sort(
                key=lambda d: (
                    d["days"] if d["days"] is not None else 999,
                    d["battery"] if d["battery"] is not None else 101,
                )
            )
        return sorted(groups.values(), key=lambda g: (g["type"] == "Unknown type", -g["count"]))

    def backup(self) -> bytes:
        with self.lock, self.metrics.lock:
            return json.dumps(
                {
                    "format": "z2m-tracker-backup",
                    "version": 1,
                    "created": _now(),
                    "history": self.data,
                    "metrics": self.metrics.series,
                },
                ensure_ascii=False,
            ).encode("utf-8")

    def restore(self, payload: dict) -> dict:
        if not isinstance(payload, dict) or payload.get("format") != "z2m-tracker-backup":
            return {"ok": False, "error": "This isn't a Zigbee Tracker backup file"}
        history = payload.get("history") or {}
        if not isinstance(history.get("devices"), dict):
            return {"ok": False, "error": "The backup has no device list"}
        with self.lock:
            # A pause is about now, not about when the backup was made.
            paused = self.data.get("alerts_paused")
            # Held quiet-hours alerts are about now, not about when the backup was made.
            quiet = self.data.get("quiet")
            self.data = {
                "devices": {},
                "events": list(history.get("events") or []),
                "imported": history.get("imported"),
                "networkmap": history.get("networkmap") or {},
                "alerted": list(history.get("alerted") or []),
            }
            for key, value in history.items():
                if key not in ("devices", "events", "imported", "networkmap", "alerted"):
                    self.data[key] = value
            self.data.pop("alerts_paused", None)
            if paused:
                self.data["alerts_paused"] = paused
            self.data["quiet"] = quiet
            for ieee, record in history["devices"].items():
                self.data["devices"][ieee] = {**_new_record(ieee, record.get("instance")), **record}
            self.names = {}
            for record in self.data["devices"].values():
                if record["status"] == STATUS_ACTIVE and record["friendly_name"] and record["instance"]:
                    self.names.setdefault(record["instance"], {})[record["friendly_name"]] = record["ieee"]
            self._changed(structural=True)
        if isinstance(payload.get("metrics"), dict):
            with self.metrics.lock:
                self.metrics.series = payload["metrics"]
                self.metrics.dirty = True
        self.save(force=True)
        self.metrics.save(force=True)
        return {"ok": True, "devices": len(self.data["devices"]), "events": len(self.data["events"])}

    def link(self, old_ieee: str, new_ieee: str) -> dict:
        with self.lock:
            old = self.data["devices"].get(old_ieee)
            new = self.data["devices"].get(new_ieee)
            if old is None or new is None or old_ieee == new_ieee:
                return {"ok": False, "error": "Both devices must exist and differ"}
            if old_ieee not in new.setdefault("predecessors", []):
                new["predecessors"].append(old_ieee)
            old["successor"] = new_ieee
            if not new.get("note") and old.get("note"):
                new["note"] = old["note"]
            if not new.get("asset") and old.get("asset"):
                new["asset"] = dict(old["asset"])
            now = _now()
            self._add_event(
                now,
                new["instance"],
                new_ieee,
                new.get("friendly_name"),
                "linked",
                f"Replaces {_display_name(old) or old_ieee} ({old_ieee})",
            )
            self._add_event(
                now,
                old["instance"] or "unknown",
                old_ieee,
                _display_name(old),
                "linked",
                f"Replaced by {_display_name(new) or new_ieee} ({new_ieee})",
            )
            self._changed(structural=True)
        self.evaluate_health()
        return {"ok": True}

    def unlink(self, old_ieee: str, new_ieee: str) -> dict:
        with self.lock:
            old = self.data["devices"].get(old_ieee)
            new = self.data["devices"].get(new_ieee)
            if new and old_ieee in (new.get("predecessors") or []):
                new["predecessors"].remove(old_ieee)
            if old and old.get("successor") == new_ieee:
                old["successor"] = None
            self._changed(structural=True)
        self.evaluate_health()
        return {"ok": True}

    def forget(self, ieee: str) -> bool:
        with self.lock:
            if self.data["devices"].pop(ieee, None) is None:
                return False
            self.data["events"] = [event for event in self.data["events"] if event.get("ieee") != ieee]
            for record in self.data["devices"].values():
                if ieee in (record.get("predecessors") or []):
                    record["predecessors"].remove(ieee)
                if record.get("successor") == ieee:
                    record["successor"] = None
            self.metrics.forget(ieee)
            self._changed(structural=True)
            return True

    def import_history(self) -> dict[str, int]:
        result = collect_history(self.config_dir, list(self.base_topics), self.local_tz)
        added = 0
        new_events = 0
        with self.lock:
            records = self.data["devices"]
            for ieee, info in result["devices"].items():
                record = records.get(ieee)
                if record is None:
                    record = _new_record(ieee, info["instance"] or "unknown")
                    record["status"] = STATUS_REMOVED
                    records[ieee] = record
                    added += 1
                if record["instance"] in (None, "unknown") and info["instance"]:
                    record["instance"] = info["instance"]
                for key, value in info["fields"].items():
                    if value not in (None, "", []) and not record.get(key):
                        record[key] = value
                record["first_seen"] = _min_time(record["first_seen"], *info["times"])
                record["last_seen"] = _max_time(record["last_seen"], *info["last"])
                for name, when, source in info["names"]:
                    _add_historic_name(record, name, when, source)
                if info["ha_name"]:
                    record["ha_name"] = info["ha_name"]
                if info["area"]:
                    record["area"] = info["area"]
                for source in info["sources"]:
                    if source not in record["sources"]:
                        record["sources"].append(source)
                if record["status"] == STATUS_REMOVED and not record["removed_at"] and info["removed_hint"]:
                    record["removed_at"] = info["removed_hint"]

            existing = {(event["time"], event["ieee"], event["kind"]) for event in self.data["events"]}
            for event in result["events"]:
                key = (event["time"], event["ieee"], event["kind"])
                if key not in existing:
                    existing.add(key)
                    self.data["events"].append(event)
                    new_events += 1
            self.data["events"].sort(key=lambda event: event.get("time") or "")
            if len(self.data["events"]) > MAX_EVENTS:
                del self.data["events"][: len(self.data["events"]) - MAX_EVENTS]
            self.data["imported"] = _now()
            self._changed(structural=True)
        return {"devices_found": len(result["devices"]), "new_devices": added, "new_events": new_events}

    # ------------------------------------------------------------ replacement

    def replacement_bindings(self, ieee: str) -> list[dict]:
        """The old device's last known bindings worth re-creating (not the ones Z2M sets up to the coordinator)."""
        with self.lock:
            record = self.data["devices"].get(ieee) or {}
            known = list(record.get("bindings") or [])
            keys = {b["key"] for b in known}
            known += [b for b in record.get("lost_bindings") or [] if b.get("key") not in keys]
            return [b for b in known if not self._is_coordinator_target(b)]

    def replace_problem(self, old_ieee: str, new_ieee: str) -> str | None:
        with self.lock:
            old = self.data["devices"].get(old_ieee)
            new = self.data["devices"].get(new_ieee)
            if old is None or new is None:
                return "Both devices must be known to the tracker"
            if old_ieee == new_ieee:
                return "Choose a different device as the replacement"
            if "Coordinator" in (old.get("type"), new.get("type")):
                return "The coordinator can't be replaced here"
            if new["status"] != STATUS_ACTIVE:
                return "The new device must be in Zigbee2MQTT"
        return None

    def remove_from_z2m(self, ieee: str) -> dict:
        """Force-remove a device from Zigbee2MQTT. The tracker keeps its history."""
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None or record["status"] != STATUS_ACTIVE:
                return {"ok": True, "skipped": True}
            base_topic = record["instance"]
        result = self.request(base_topic, "device/remove", {"id": ieee, "force": True}, timeout=30)
        ok = bool(result and result.get("status") == "ok")
        return {"ok": ok, "error": None if ok else friendly_z2m_error((result or {}).get("error") or "No response")}

    def rebind(self, old_ieee: str, new_ieee: str) -> dict:
        """Re-create the old device's bindings on the new one through bridge/request/device/bind."""
        wanted = self.replacement_bindings(old_ieee)
        with self.lock:
            new = self.data["devices"].get(new_ieee)
            if new is None or new["status"] != STATUS_ACTIVE:
                return {"ok": False, "error": "The new device isn't in Zigbee2MQTT", "done": 0, "failed": []}
            base_topic = new["instance"]
            version = str(((self.data.get("bridges") or {}).get(base_topic) or {}).get("info", {}).get("version") or "")
            groups = dict(self.groups.get(base_topic) or {})
            requests: dict[tuple[str, str], dict] = {}
            for binding in wanted:
                target = binding.get("target") or ""
                if target.startswith("group:"):
                    gid = target.split(":", 1)[1]
                    to, to_ep = groups.get(gid) or (int(gid) if gid.isdigit() else gid), None
                else:
                    ieee, _, ep = target.partition("/")
                    rec = self.data["devices"].get(ieee)
                    if rec is None or rec["status"] != STATUS_ACTIVE or rec.get("instance") != base_topic:
                        continue
                    to, to_ep = ieee, ep if ep.isdigit() else None
                entry = requests.setdefault(
                    (binding.get("ep") or "", target),
                    {"ep": binding.get("ep") or "", "to": to, "to_ep": to_ep, "clusters": [], "labels": []},
                )
                entry["clusters"].append(binding.get("cluster"))
                entry["labels"].append(binding.get("label") or self.binding_label(base_topic, binding))
        skipped = len(wanted) - sum(len(r["clusters"]) for r in requests.values())
        legacy = version[:1].isdigit() and int(version.split(".", 1)[0]) < 2
        done, failed = 0, []
        for entry in list(requests.values())[:12]:
            ep = entry["ep"]
            if legacy:
                payload: dict[str, Any] = {
                    "from": f"{new_ieee}/{ep}" if ep.isdigit() else new_ieee,
                    "to": f"{entry['to']}/{entry['to_ep']}" if entry["to_ep"] else entry["to"],
                }
            else:
                payload = {"from": new_ieee, "to": entry["to"]}
                if ep.isdigit():
                    payload["from_endpoint"] = int(ep)
                if entry["to_ep"]:
                    payload["to_endpoint"] = int(entry["to_ep"])
            payload["clusters"] = entry["clusters"]
            result = self.request(base_topic, "device/bind", payload, timeout=20)
            if result and result.get("status") == "ok":
                done += len(entry["clusters"])
            else:
                failed.append(
                    {
                        "labels": entry["labels"],
                        "error": friendly_z2m_error((result or {}).get("error") or "No response"),
                    }
                )
        return {"ok": not failed, "done": done, "failed": failed, "skipped": skipped}

    def carry_over(self, old_ieee: str, new_ieee: str, notes: bool, battery: bool) -> list[str]:
        """Copy notes, asset details, the battery record and the critical mark. Returns what was copied."""
        copied = []
        with self.lock:
            old = self.data["devices"].get(old_ieee)
            new = self.data["devices"].get(new_ieee)
            if old is None or new is None:
                return copied
            if notes:
                if old.get("note"):
                    mine = (new.get("note") or "").strip()
                    new["note"] = old["note"] if not mine or mine == old["note"] else f"{mine}\n\n{old['note']}"
                    copied.append("note")
                asset = {k: v for k, v in (old.get("asset") or {}).items() if k != "serial"}
                if asset:
                    new["asset"] = {**asset, **(new.get("asset") or {})}
                    copied.append("asset")
            if battery and old.get("battery_info"):
                info = json.loads(json.dumps(old["battery_info"]))
                new["battery_info"] = {**(new.get("battery_info") or {}), **info}
                copied.append("battery")
            if old.get("critical"):
                # The replacement does the same job, so it stays critical.
                new["critical"] = True
                copied.append("critical")
            self._changed(structural=True)
        return copied

    def link_replacement(self, old_ieee: str, new_ieee: str, carried: list[str], new_name: str | None = None) -> bool:
        """Join both histories: "replaced" on the new device, "replaced by" on the old one."""
        with self.lock:
            old = self.data["devices"].get(old_ieee)
            new = self.data["devices"].get(new_ieee)
            if old is None or new is None or old_ieee == new_ieee:
                return False
            if old_ieee not in new.setdefault("predecessors", []):
                new["predecessors"].append(old_ieee)
            old["successor"] = new_ieee
            now = _now()
            old_name = _display_name(old) or old_ieee
            new_name = new_name or _display_name(new) or new_ieee
            extra = f". Carried over: {', '.join(carried)}" if carried else ""
            self._add_event(now, new["instance"], new_ieee, new_name, "replaced", f"{old_name} ({old_ieee}){extra}")
            self.data["events"][-1]["ref"] = old_ieee
            self._add_event(
                now, old["instance"] or "unknown", old_ieee, old_name, "replaced by", f"{new_name} ({new_ieee})"
            )
            self.data["events"][-1]["ref"] = new_ieee
            self._changed(structural=True)
        self.evaluate_health()
        return True
