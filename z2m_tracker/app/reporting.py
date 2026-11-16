"""Attribute reporting settings: a slim copy from bridge/devices, and a short list of rules that flag bad ones.

Zigbee2MQTT lists what each device was told to report under endpoints[ep].configured_reportings.
Three rules, kept few on purpose (DOCS.md "Reporting settings" explains them):

- battery_drain: a battery device reporting anything on a timer under 5 minutes, or a fast-changing
  measurement with no minimum interval. The radio wakes for every report.
- mesh_flood: a mains device reporting power, energy, current or voltage at least every 5 seconds,
  or on every change (reportable change 0) without a minimum interval of a minute.
- never: a measurement whose maximum interval is 65535 (reporting off), or a measurement the device
  has a cluster for but no reporting set at all, on a device that does use configured reporting.

A finding carries recommended values that change only what's wrong and keep the rest.
"""

from __future__ import annotations

import math
from typing import Any

MAX_REPORTINGS = 40
MAX_NAME = 64
NEVER = 0xFFFF
BATTERY_MAX_MIN = 300
FLOOD_MAX = 5
FLOOD_MIN_GUARD = 60
# Message rates (per hour, last 6 h) that back up a finding: more than one a minute on battery,
# one every 5 seconds on mains.
BATTERY_RATE = 60
FLOOD_RATE = 720

ENV = "env"
ELECTRIC = "electric"
BATTERY = "battery"

# (cluster, attribute) -> key, English label, kind, display scale, unit,
# recommended (min, max, change) on battery, on mains. Changes are in the attribute's raw units.
ATTRS: dict[tuple[str, str], dict[str, Any]] = {}


def _attr(cluster, attribute, key, label, kind, scale=1, unit="", battery=None, mains=None):
    ATTRS[(cluster, attribute)] = {
        "key": key,
        "label": label,
        "kind": kind,
        "scale": scale,
        "unit": unit,
        "battery": battery,
        "mains": mains or battery,
    }


_attr(
    "msTemperatureMeasurement",
    "measuredValue",
    "temperature",
    "Temperature",
    ENV,
    0.01,
    "°C",
    (30, 3600, 20),
    (10, 3600, 10),
)
_attr(
    "hvacThermostat",
    "localTemperature",
    "local_temperature",
    "Room temperature",
    ENV,
    0.01,
    "°C",
    (30, 3600, 20),
    (10, 3600, 10),
)
_attr("msRelativeHumidity", "measuredValue", "humidity", "Humidity", ENV, 0.01, "%", (30, 3600, 100), (10, 3600, 50))
_attr("msPressureMeasurement", "measuredValue", "pressure", "Pressure", ENV, 1, "hPa", (30, 3600, 1), (10, 3600, 1))
_attr(
    "msIlluminanceMeasurement",
    "measuredValue",
    "illuminance",
    "Illuminance",
    ENV,
    1,
    "",
    (30, 3600, 500),
    (10, 3600, 500),
)
_attr("msCO2", "measuredValue", "co2", "CO2", ENV, 1e6, "ppm", (30, 3600, 0.00005), (10, 3600, 0.00005))
_attr("pm25Measurement", "measuredValue", "pm25", "PM2.5", ENV, 1, "µg/m³", (30, 3600, 1), (10, 3600, 1))
_attr(
    "msSoilMoisture",
    "measuredValue",
    "soil_moisture",
    "Soil moisture",
    ENV,
    0.01,
    "%",
    (30, 3600, 100),
    (10, 3600, 100),
)
_attr("genPowerCfg", "batteryPercentageRemaining", "battery", "Battery", BATTERY, 0.5, "%", (3600, 43200, 2))
_attr("genPowerCfg", "batteryVoltage", "battery_voltage", "Battery voltage", BATTERY, 0.1, "V", (3600, 43200, 1))
_attr("haElectricalMeasurement", "activePower", "power", "Power", ELECTRIC, mains=(10, 3600, 5))
_attr("haElectricalMeasurement", "apparentPower", "apparent_power", "Apparent power", ELECTRIC, mains=(10, 3600, 5))
_attr("haElectricalMeasurement", "rmsCurrent", "current", "Current", ELECTRIC, mains=(10, 3600, 10))
_attr("haElectricalMeasurement", "rmsVoltage", "voltage", "Voltage", ELECTRIC, mains=(10, 3600, 5))
_attr("seMetering", "instantaneousDemand", "power", "Power", ELECTRIC, mains=(10, 3600, 5))
_attr("seMetering", "currentSummDelivered", "energy", "Energy", ELECTRIC, mains=(10, 3600, 10))
_attr("genOnOff", "onOff", "state", "On/off", "other")
_attr("genLevelCtrl", "currentLevel", "brightness", "Brightness", "other")
_attr("msOccupancySensing", "occupancy", "occupancy", "Occupancy", "other")

# Exposed property -> the attributes that report it (any one of them counts).
EXPOSES = {
    "temperature": [("msTemperatureMeasurement", "measuredValue")],
    "local_temperature": [("hvacThermostat", "localTemperature")],
    "humidity": [("msRelativeHumidity", "measuredValue")],
    "pressure": [("msPressureMeasurement", "measuredValue")],
    "illuminance": [("msIlluminanceMeasurement", "measuredValue")],
    "illuminance_lux": [("msIlluminanceMeasurement", "measuredValue")],
    "co2": [("msCO2", "measuredValue")],
    "pm25": [("pm25Measurement", "measuredValue")],
    "soil_moisture": [("msSoilMoisture", "measuredValue")],
    "battery": [("genPowerCfg", "batteryPercentageRemaining")],
    "power": [("haElectricalMeasurement", "activePower"), ("seMetering", "instantaneousDemand")],
    "current": [("haElectricalMeasurement", "rmsCurrent")],
    "voltage": [("haElectricalMeasurement", "rmsVoltage")],
    "energy": [("seMetering", "currentSummDelivered")],
}
# Input clusters worth remembering (for the "no reporting at all" rule); the rest aren't kept.
MEASURE_CLUSTERS = sorted({cluster for pairs in EXPOSES.values() for cluster, _ in pairs})


def _name(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()[:MAX_NAME]
    return None


def _interval(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    value = int(value)
    return value if 0 <= value <= NEVER else None


def _change(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value if value >= 0 else None


def slim(device: dict) -> tuple[list[dict], dict[str, list[str]]] | None:
    """(configured reportings, measurement input clusters per endpoint), or None without endpoint data."""
    endpoints = device.get("endpoints")
    if not isinstance(endpoints, dict):
        return None
    items: list[dict] = []
    inputs: dict[str, list[str]] = {}
    for ep, info in endpoints.items():
        if not isinstance(info, dict):
            continue
        ep = _name(ep)
        if ep is None:
            continue
        for entry in info.get("configured_reportings") or []:
            if not isinstance(entry, dict):
                continue
            cluster, attribute = _name(entry.get("cluster")), _name(entry.get("attribute"))
            if not cluster or not attribute:
                continue
            items.append(
                {
                    "ep": ep,
                    "cluster": cluster,
                    "attribute": attribute,
                    "min": _interval(entry.get("minimum_report_interval")),
                    "max": _interval(entry.get("maximum_report_interval")),
                    "change": _change(entry.get("reportable_change")),
                }
            )
        clusters = info.get("clusters") if isinstance(info.get("clusters"), dict) else {}
        found = [c for c in clusters.get("input") or [] if isinstance(c, str) and c in MEASURE_CLUSTERS]
        if found:
            inputs[ep] = sorted(set(found))
    items.sort(key=lambda r: (r["ep"], r["cluster"], r["attribute"]))
    return items[:MAX_REPORTINGS], inputs


def clean(items: Any) -> list[dict] | None:
    """Stored reportings with the right types (old or restored files are not trusted)."""
    if not isinstance(items, list):
        return None
    out = []
    for item in items[:MAX_REPORTINGS]:
        if not isinstance(item, dict):
            continue
        ep, cluster, attribute = _name(item.get("ep")), _name(item.get("cluster")), _name(item.get("attribute"))
        if ep and cluster and attribute:
            out.append(
                {
                    "ep": ep,
                    "cluster": cluster,
                    "attribute": attribute,
                    "min": _interval(item.get("min")),
                    "max": _interval(item.get("max")),
                    "change": _change(item.get("change")),
                }
            )
    return out


def _check(item: dict, battery: bool) -> tuple[str, dict] | None:
    """(rule, recommended values) when this reporting breaks a rule, else None."""
    info = ATTRS.get((item["cluster"], item["attribute"])) or {}
    kind = info.get("kind")
    low, high, change = item["min"], item["max"], item["change"]
    rec = info.get("battery" if battery else "mains") or (60 if battery else 10, 3600, None)
    fix = {"min": low, "max": high, "change": change}
    if kind in (ENV, ELECTRIC, BATTERY) and high == NEVER:
        fix["max"] = rec[1]
        if low is None or low > rec[1]:
            fix["min"] = rec[0]
        return "never", fix
    if battery:
        timer = high is not None and 0 < high < BATTERY_MAX_MIN
        eager = kind == ENV and low == 0
        if timer or eager:
            if timer:
                fix["max"] = rec[1]
            if eager or low is None:
                fix["min"] = rec[0]
            if kind == ENV and change == 0 and rec[2] is not None:
                fix["change"] = rec[2]
            return "battery_drain", fix
        return None
    if kind == ELECTRIC:
        fast = high is not None and 0 < high <= FLOOD_MAX
        every = change == 0 and (low or 0) < FLOOD_MIN_GUARD
        if fast or every:
            if fast:
                fix["max"] = rec[1]
            if every:
                fix["change"] = rec[2]
            if low is None or low < rec[0]:
                fix["min"] = rec[0]
            return "mesh_flood", fix
    return None


def audit(
    items: list[dict] | None, inputs: dict | None, features: list | None, battery: bool, rate: float | None
) -> dict | None:
    """Each reporting with its label and any finding, plus measurements with no reporting at all."""
    if items is None:
        return None
    out = []
    for item in items:
        info = ATTRS.get((item["cluster"], item["attribute"])) or {}
        row = {
            **item,
            "key": info.get("key"),
            "label": info.get("label") or f"{item['cluster']} {item['attribute']}",
            "scale": info.get("scale", 1),
            "unit": info.get("unit", ""),
            "rule": None,
            "recommended": None,
        }
        found = _check(item, battery)
        if found:
            row["rule"], row["recommended"] = found
        out.append(row)
    missing = []
    if items:
        have = {(i["cluster"], i["attribute"]) for i in items}
        clusters = {c: ep for ep, cs in sorted((inputs or {}).items()) for c in cs}
        seen = set()
        for prop in features or []:
            pairs = [p for p in EXPOSES.get(prop, []) if p[0] in clusters]
            if not pairs or any(p in have for p in pairs) or pairs[0] in seen:
                continue
            seen.add(pairs[0])
            info = ATTRS[pairs[0]]
            missing.append(
                {
                    "ep": clusters[pairs[0][0]],
                    "cluster": pairs[0][0],
                    "attribute": pairs[0][1],
                    "key": info["key"],
                    "label": info["label"],
                    "rule": "missing",
                }
            )
    confirmed = False
    if rate is not None:
        rules = {r["rule"] for r in out}
        confirmed = ("battery_drain" in rules and rate >= BATTERY_RATE) or (
            "mesh_flood" in rules and rate >= FLOOD_RATE
        )
    return {
        "items": out,
        "missing": missing,
        "battery": battery,
        "rate": round(rate) if rate is not None else None,
        "confirmed": confirmed,
    }


def findings(result: dict | None) -> list[dict]:
    if not result:
        return []
    return [r for r in result["items"] if r["rule"]] + list(result["missing"])


def _dur(seconds: int) -> str:
    """Exact when it divides evenly, otherwise rounded (62000 s reads "17 h")."""
    if seconds < 120:
        return f"{seconds} s"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} h"
    if seconds % 60 == 0 and seconds < 7200:
        return f"{seconds // 60} min"
    return f"{round(seconds / 3600)} h" if seconds >= 3600 else f"{round(seconds / 60)} min"


def _amount(change: int | float, scale: float, unit: str) -> str:
    value = round(change * scale, 6)
    text = f"{value:g}"
    return f"{text} {unit}".strip() if unit else text


def describe(item: dict) -> str:
    """'Temperature: at least every 10 s, at most every 1 h, on a 0.1 °C change' (English, for events)."""
    info = ATTRS.get((item["cluster"], item["attribute"])) or {}
    label = info.get("label") or f"{item['cluster']} {item['attribute']}"
    parts = []
    low, high, change = item.get("min"), item.get("max"), item.get("change")
    if low is not None:
        parts.append("as often as it changes" if low == 0 else f"at least every {_dur(low)}")
    if high is not None:
        parts.append(
            "never on a timer" if high == NEVER else "only on change" if high == 0 else f"at most every {_dur(high)}"
        )
    if change is not None:
        parts.append(
            "on any change"
            if change == 0
            else f"on a {_amount(change, info.get('scale', 1), info.get('unit', ''))} change"
        )
    return f"{label}: {', '.join(parts) or 'no details'}"


RULE_TEXT = {
    "battery_drain": "drains the battery",
    "mesh_flood": "floods the mesh",
    "never": "may go silent",
    "missing": "no reporting set, may go silent",
}


def summary_text(found: list[dict]) -> str:
    """'Temperature (drains the battery), Power (floods the mesh)…'"""
    shown = [f"{f['label']} ({RULE_TEXT[f['rule']]})" for f in found[:4]]
    return "; ".join(shown) + ("…" if len(found) > 4 else "")


def clean_fix(body: dict) -> tuple[dict | None, str | None]:
    """The configure_reporting request from the sidebar, checked. Returns (fix, error)."""
    ep, cluster, attribute = _name(body.get("ep")), _name(body.get("cluster")), _name(body.get("attribute"))
    if not ep or not cluster or not attribute:
        return None, "Endpoint, cluster and attribute are required"
    values = {}
    for key in ("min", "max"):
        value = body.get(key)
        whole = isinstance(value, int) or (isinstance(value, float) and math.isfinite(value) and value.is_integer())
        if isinstance(value, bool) or not whole or not 0 <= int(value) <= NEVER:
            return None, f"{'Minimum' if key == 'min' else 'Maximum'} interval must be a whole number from 0 to 65535"
        values[key] = int(value)
    change = body.get("change")
    if isinstance(change, bool) or not isinstance(change, (int, float)) or not math.isfinite(change):
        return None, "Reportable change must be a number from 0 to 65535"
    if not 0 <= change <= NEVER:
        return None, "Reportable change must be a number from 0 to 65535"
    if values["max"] not in (0, NEVER) and values["min"] > values["max"]:
        return None, "The minimum interval can't be longer than the maximum"
    return {"ep": ep, "cluster": cluster, "attribute": attribute, **values, "change": change}, None
