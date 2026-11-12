"""Collect historic Zigbee2MQTT device info from files on disk.

Pure functions with no Home Assistant imports so they can run in an executor
(or standalone for testing). Sources:
  - <config>/<base_topic>/configuration*.yaml   friendly names, descriptions
  - <config>/<base_topic>/database.db*           model id, manufacturer, last seen
  - <config>/<base_topic>/state.json             last seen
  - <config>/<base_topic>/log/*/log*.log         joins, interviews, renames
  - <config>/.storage/core.device_registry       HA names, areas, deleted devices
  - <config>/.storage/core.entity_registry       former names from deleted entities
"""

from __future__ import annotations

import glob
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone, tzinfo
from typing import Any

import yaml

IEEE_RE = re.compile(r"^0x[0-9a-f]{16}$")
HA_IDENT_RE = re.compile(r"zigbee2mqtt_(0x[0-9a-f]{16})$")
UNIQUE_ID_RE = re.compile(r"^(0x[0-9a-f]{16})_(.+?)_zigbee2mqtt$")
LOG_LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*?topic '([^']+)', payload '(.*)'\s*$")

EVENT_KINDS = {
    "device_joined": "joined",
    "device_leave": "left",
    "device_interview": "interview",
}


class _LenientLoader(yaml.SafeLoader):
    """SafeLoader that ignores custom tags such as !secret."""


_LenientLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


def _iso_from_epoch(value: Any) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    seconds = number / 1000 if number > 1e11 else number
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    if moment.year < 2000:
        return None
    return moment.isoformat(timespec="seconds")


def _iso_from_string(value: Any, local_tz: tzinfo) -> str | None:
    if not value or not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=local_tz)
    if moment.year < 2000:
        return None
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _local_log_time(stamp: str, local_tz: tzinfo) -> str | None:
    try:
        moment = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=local_tz)
    except ValueError:
        return None
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _file_time(path: str) -> str | None:
    try:
        return _iso_from_epoch(os.path.getmtime(path))
    except OSError:
        return None


def _load_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _load_yaml(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return yaml.load(handle, Loader=_LenientLoader)  # noqa: S506 - SafeLoader subclass
    except (OSError, yaml.YAMLError):
        return None


def _devices_from_config(path: str, data_dir: str) -> dict:
    config = _load_yaml(path)
    if not isinstance(config, dict):
        return {}
    devices = config.get("devices")
    if isinstance(devices, str):
        devices = _load_yaml(os.path.join(data_dir, devices))
    elif isinstance(devices, list):
        merged: dict = {}
        for item in devices:
            if isinstance(item, str):
                loaded = _load_yaml(os.path.join(data_dir, item))
                if isinstance(loaded, dict):
                    merged.update(loaded)
        devices = merged
    return devices if isinstance(devices, dict) else {}


def _features(exposes: Any) -> list[str]:
    found: list[str] = []
    for item in exposes or []:
        if not isinstance(item, dict):
            continue
        name = item.get("property") or (
            item.get("type") if item.get("type") in ("light", "switch", "climate", "lock", "cover", "fan") else None
        )
        if name and name not in ("linkquality",) and name not in found:
            found.append(name)
    return found[:30]


def _name_from_slug(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.split("_") if part)


def collect_history(config_dir: str, base_topics: list[str], local_tz: tzinfo) -> dict:
    """Return {"devices": {ieee: info}, "events": [event, ...]}."""
    devices: dict[str, dict] = {}
    events: list[dict] = []

    def device(ieee: str) -> dict:
        return devices.setdefault(
            ieee,
            {
                "instance": None,
                "names": [],
                "fields": {},
                "times": [],
                "last": [],
                "ha_name": None,
                "area": None,
                "sources": set(),
                "removed_hint": None,
            },
        )

    def fill(info: dict, key: str, value: Any) -> None:
        if value not in (None, "", []) and not info["fields"].get(key):
            info["fields"][key] = value

    for base_topic in base_topics:
        data_dir = os.path.join(config_dir, base_topic)
        if not os.path.isdir(data_dir):
            continue

        current_names: dict[str, str] = {}
        config_files = sorted(glob.glob(os.path.join(data_dir, "configuration_backup*.yaml")))
        config_files.append(os.path.join(data_dir, "configuration.yaml"))
        for path in config_files:
            is_current = os.path.basename(path) == "configuration.yaml"
            for raw_ieee, conf in _devices_from_config(path, data_dir).items():
                ieee = str(raw_ieee).lower()
                if not IEEE_RE.match(ieee):
                    continue
                info = device(ieee)
                if is_current or not info["instance"]:
                    info["instance"] = base_topic
                info["sources"].add("Z2M configuration")
                if not isinstance(conf, dict):
                    continue
                name = conf.get("friendly_name")
                if name is not None:
                    name = str(name)
                    if name.lower() != ieee:
                        info["names"].append((name, None, "Z2M configuration"))
                    if is_current:
                        current_names[name] = ieee
                fill(info, "z2m_description", conf.get("description"))

        for path in sorted(glob.glob(os.path.join(data_dir, "database.db*"))):
            if path.endswith(("-wal", "-shm")):
                continue
            try:
                handle = open(path, encoding="utf-8", errors="ignore")
            except OSError:
                continue
            with handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    ieee = str(record.get("ieeeAddr", "")).lower()
                    if not IEEE_RE.match(ieee):
                        continue
                    info = device(ieee)
                    info["instance"] = info["instance"] or base_topic
                    info["sources"].add("Z2M database")
                    fill(info, "type", record.get("type"))
                    fill(info, "manufacturer", record.get("manufName"))
                    fill(info, "model_id", record.get("modelId"))
                    fill(info, "power_source", record.get("powerSource"))
                    fill(info, "date_code", record.get("dateCode"))
                    fill(info, "software_build_id", record.get("softwareBuildID"))
                    if record.get("interviewCompleted") is False:
                        fill(info, "interview_state", "FAILED")
                    seen = _iso_from_epoch(record.get("lastSeen"))
                    if seen:
                        info["last"].append(seen)

        state = _load_json(os.path.join(data_dir, "state.json"))
        if isinstance(state, dict):
            for raw_ieee, values in state.items():
                ieee = str(raw_ieee).lower()
                if not IEEE_RE.match(ieee) or not isinstance(values, dict):
                    continue
                seen = _iso_from_string(values.get("last_seen"), local_tz) or _iso_from_epoch(values.get("last_seen"))
                if seen:
                    info = device(ieee)
                    info["instance"] = info["instance"] or base_topic
                    info["last"].append(seen)

        renames: list[tuple[str, str, str]] = []
        for path in glob.glob(os.path.join(data_dir, "log", "*", "log*.log")):
            try:
                handle = open(path, encoding="utf-8", errors="ignore")
            except OSError:
                continue
            with handle:
                for line in handle:
                    if "/bridge/" not in line or "payload '" not in line:
                        continue
                    match = LOG_LINE_RE.match(line)
                    if not match:
                        continue
                    stamp, topic, payload_text = match.groups()
                    if not topic.startswith(f"{base_topic}/bridge/"):
                        continue
                    when = _local_log_time(stamp, local_tz)
                    try:
                        payload = json.loads(payload_text)
                    except ValueError:
                        continue
                    if not isinstance(payload, dict) or not when:
                        continue
                    if topic.endswith("/bridge/response/device/rename"):
                        data = payload.get("data") or {}
                        if payload.get("status") == "ok" and data.get("from") and data.get("to"):
                            renames.append((when, str(data["from"]), str(data["to"])))
                        continue
                    if not topic.endswith("/bridge/event"):
                        continue
                    kind = EVENT_KINDS.get(payload.get("type"))
                    data = payload.get("data") or {}
                    ieee = str(data.get("ieee_address", "")).lower()
                    if not kind or not IEEE_RE.match(ieee):
                        continue
                    info = device(ieee)
                    info["instance"] = info["instance"] or base_topic
                    info["sources"].add("Z2M logs")
                    info["times"].append(when)
                    info["last"].append(when)
                    name = data.get("friendly_name")
                    if name and str(name).lower() != ieee:
                        info["names"].append((str(name), when, "Z2M logs"))
                    detail = ""
                    if kind == "interview":
                        status = data.get("status") or "?"
                        kind = f"interview {status}"
                        definition = data.get("definition") or {}
                        if status == "successful":
                            fill(info, "model", definition.get("model"))
                            fill(info, "vendor", definition.get("vendor"))
                            fill(info, "description", definition.get("description"))
                            fill(info, "features", _features(definition.get("exposes")))
                            if definition.get("model"):
                                detail = f"{definition.get('vendor') or ''} {definition.get('model')}".strip()
                    events.append(
                        {
                            "time": when,
                            "instance": base_topic,
                            "ieee": ieee,
                            "name": str(name) if name else None,
                            "kind": kind,
                            "detail": detail,
                        }
                    )

        name_to_ieee = dict(current_names)
        for when, old_name, new_name in sorted(renames, reverse=True):
            ieee = name_to_ieee.get(new_name) or (new_name.lower() if IEEE_RE.match(new_name.lower()) else None)
            if not ieee:
                continue
            info = device(ieee)
            info["instance"] = info["instance"] or base_topic
            info["names"].append((new_name, when, "Z2M logs (rename)"))
            info["names"].append((old_name, None, "Z2M logs (rename)"))
            name_to_ieee[old_name] = ieee
            events.append(
                {
                    "time": when,
                    "instance": base_topic,
                    "ieee": ieee,
                    "name": new_name,
                    "kind": "renamed",
                    "detail": f"{old_name} → {new_name}",
                }
            )

    storage_dir = os.path.join(config_dir, ".storage")
    areas: dict[str, str] = {}
    area_data = _load_json(os.path.join(storage_dir, "core.area_registry"))
    if isinstance(area_data, dict):
        for area in (area_data.get("data") or {}).get("areas", []):
            areas[area.get("id")] = area.get("name")

    def ieee_from_identifiers(entry: dict) -> str | None:
        for identifier in entry.get("identifiers") or []:
            if len(identifier) > 1:
                match = HA_IDENT_RE.search(str(identifier[1]))
                if match:
                    return match.group(1)
        return None

    device_data = _load_json(os.path.join(storage_dir, "core.device_registry"))
    if isinstance(device_data, dict):
        registry = device_data.get("data") or {}
        for entry in registry.get("devices", []):
            ieee = ieee_from_identifiers(entry)
            if not ieee:
                continue
            info = device(ieee)
            info["sources"].add("HA device registry")
            ha_name = entry.get("name_by_user") or entry.get("name")
            if ha_name and str(ha_name).lower() != ieee:
                info["ha_name"] = str(ha_name)
                info["names"].append((str(ha_name), None, "HA device registry"))
            info["area"] = areas.get(entry.get("area_id")) or info["area"]
            fill(info, "description", entry.get("model"))
            fill(info, "model", entry.get("model_id"))
            fill(info, "vendor", entry.get("manufacturer"))
            created = _iso_from_string(entry.get("created_at"), timezone.utc)
            if created:
                info["times"].append(created)
        for entry in registry.get("deleted_devices", []):
            ieee = ieee_from_identifiers(entry)
            if not ieee:
                continue
            info = device(ieee)
            info["sources"].add("HA deleted devices")
            created = _iso_from_string(entry.get("created_at"), timezone.utc)
            if created:
                info["times"].append(created)
            removed = _iso_from_epoch(entry.get("orphaned_timestamp")) or _iso_from_string(
                entry.get("modified_at"), timezone.utc
            )
            if removed:
                info["removed_hint"] = removed

    entity_data = _load_json(os.path.join(storage_dir, "core.entity_registry"))
    if isinstance(entity_data, dict):
        registry = entity_data.get("data") or {}
        bases: dict[str, Counter] = {}
        for entry in registry.get("deleted_entities", []):
            if entry.get("platform") != "mqtt":
                continue
            match = UNIQUE_ID_RE.match(str(entry.get("unique_id") or ""))
            if not match:
                continue
            ieee, suffix = match.groups()
            object_id = str(entry.get("entity_id", "")).split(".", 1)[-1]
            base = object_id[: -len(suffix) - 1] if object_id.endswith(f"_{suffix}") else object_id
            base = re.sub(r"_\d+$", "", base)
            if not base or base.startswith("0x") or ieee[2:] in base:
                continue
            bases.setdefault(ieee, Counter())[base] += 1
        for ieee, counter in bases.items():
            info = device(ieee)
            info["sources"].add("HA deleted entities")
            top = counter.most_common(1)[0][1]
            for base, count in counter.items():
                if count >= 2 or count == top:
                    info["names"].append((_name_from_slug(base), None, "HA old entity names"))

    for info in devices.values():
        info["sources"] = sorted(info["sources"])
    return {"devices": devices, "events": events}
