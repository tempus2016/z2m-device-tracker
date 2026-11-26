"""Reliability by model: the user's own devices grouped by manufacturer and model.

Built from local records only (outages, battery changes, events), for the sidebar's
"Reliability by model" card. Cheap enough to run on every snapshot: one pass over the
devices and one over the event log.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from health import uptime
from storage import is_stored

WINDOW_DAYS = 30
# A device seen for less than a day says nothing useful about dropouts per month.
MIN_DROPOUT_DAYS = 1
# Two re-pair records closer than this are the same re-pair (a re-paired event and its joined event).
REPAIR_GAP = 600
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _ts(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def _model_key(record: dict) -> tuple[str, str] | None:
    model = record.get("model") or record.get("model_id")
    if not model:
        return None
    return (str(record.get("vendor") or record.get("manufacturer") or ""), str(model))


def _day(value: str) -> date | None:
    """A YYYY-MM-DD string as a date; None for anything else, a date that doesn't exist included."""
    if not DATE_RE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _battery_intervals(record: dict) -> list[int]:
    """Days between consecutive battery changes, counting the install date as the first one."""
    info = record.get("battery_info") or {}
    days = {str(r.get("date") or "") for r in info.get("replacements") or [] if isinstance(r, dict)}
    days.add(str(info.get("installed") or ""))
    points = sorted(p for p in (_day(d) for d in days) if p)
    return [(b - a).days for a, b in zip(points, points[1:], strict=False) if (b - a).days > 0]


def _dropouts(record: dict, now: float) -> tuple[int, float] | None:
    """Outages that started in the window, and the days the device was watched in it."""
    start = now - WINDOW_DAYS * 86400
    first = _ts(record.get("first_seen"))
    if first is not None:
        start = max(start, first)
    days = (now - start) / 86400
    if days < MIN_DROPOUT_DAYS:
        return None
    count = sum(1 for o in record.get("outages") or [] if (_ts(o.get("start")) or 0) >= start)
    return count, days


def _repairs(times: list[float], first_seen: float | None) -> int:
    """Separate re-pairs: joins after the first one, with near-simultaneous records merged."""
    count, last = 0, None
    for at in sorted(times):
        if first_seen is not None and at <= first_seen + REPAIR_GAP:
            continue
        if last is None or at - last > REPAIR_GAP:
            count += 1
        last = at
    return count


def model_reliability(records: list[dict], events: list[dict], now: float) -> list[dict]:
    """One row per vendor + model, worst first.

    Covers active devices that aren't the coordinator. Each row has:
    `vendor`, `model`, `devices` (count), `ieees`, `uptime_30d` (mean % over devices with
    enough history, or None), `dropouts_30d` (outages started in the window),
    `dropouts_per_month` (per device, or None with under a day of history),
    `battery_life_days` (mean days between battery changes, or None) with `battery_samples`
    (how many intervals that mean is from), `interview_failures` and `repairs`.
    """
    failures: dict[str, int] = {}
    joins: dict[str, list[float]] = {}
    for event in events:
        ieee, kind = event.get("ieee"), event.get("kind")
        if kind == "interview failed":
            failures[ieee] = failures.get(ieee, 0) + 1
        elif kind in ("joined", "re-paired"):
            at = _ts(event.get("time"))
            if at is not None:
                joins.setdefault(ieee, []).append(at)

    groups: dict[tuple[str, str], dict] = {}
    for record in records:
        # A stored device is offline on purpose: its outages say nothing about the model.
        if record.get("status") != "active" or record.get("type") == "Coordinator" or is_stored(record):
            continue
        key = _model_key(record)
        if key is None:
            continue
        ieee = record["ieee"]
        g = groups.setdefault(
            key,
            {
                "ieees": [],
                "uptimes": [],
                "drops": 0,
                "days": 0.0,
                "watched": 0,
                "battery": [],
                "fails": 0,
                "repairs": 0,
            },
        )
        g["ieees"].append(ieee)
        up = uptime(record, now)
        if up is not None:
            g["uptimes"].append(up)
        drops = _dropouts(record, now)
        if drops is not None:
            g["drops"] += drops[0]
            g["days"] += drops[1]
            g["watched"] += 1
        g["battery"].extend(_battery_intervals(record))
        g["fails"] += failures.get(ieee, 0)
        g["repairs"] += _repairs(joins.get(ieee, []), _ts(record.get("first_seen")))

    rows = []
    for (vendor, model), g in groups.items():
        rows.append(
            {
                "vendor": vendor,
                "model": model,
                "devices": len(g["ieees"]),
                "ieees": sorted(g["ieees"]),
                "uptime_30d": round(sum(g["uptimes"]) / len(g["uptimes"]), 2) if g["uptimes"] else None,
                "dropouts_30d": g["drops"],
                "dropouts_per_month": round(g["drops"] / g["days"] * WINDOW_DAYS, 1) if g["watched"] else None,
                "battery_life_days": round(sum(g["battery"]) / len(g["battery"])) if g["battery"] else None,
                "battery_samples": len(g["battery"]),
                "interview_failures": g["fails"],
                "repairs": g["repairs"],
            }
        )
    rows.sort(
        key=lambda r: (
            r["uptime_30d"] if r["uptime_30d"] is not None else 101.0,
            -(r["dropouts_per_month"] or 0),
            -(r["interview_failures"] + r["repairs"]) / r["devices"],
            -r["devices"],
            r["vendor"].lower(),
            r["model"].lower(),
        )
    )
    return rows


# ------------------------------------------------------------ device age
# How old each device is and when its firmware last changed (Upkeep → Models → By device age).
# Age counts from the purchase date in the asset details, else the first time the tracker saw the device.
# The firmware change date is record["firmware_changed"] (set when Zigbee2MQTT reports a different
# software_build_id, or an update finishes), else the newest "firmware updated" event in the log.

STALE_DAYS = 730  # firmware unchanged for 2 years or more, on a device at least that old
AGE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _age_day(value: Any) -> date | None:
    text = str(value or "")
    if not AGE_DATE_RE.match(text):
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _age_when(value: Any) -> str | None:
    """An ISO timestamp kept as given when it parses, else None."""
    try:
        datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return str(value)


def device_ages(records: list[dict], events: list[dict], today: date) -> list[dict]:
    """Active devices, oldest first: since (date), source ("bought" or "first_seen"), age_days,
    firmware, firmware_changed (ISO time or None) and stale (firmware unchanged for 2+ years)."""
    updated: dict[str, str] = {}
    for event in events:
        if event.get("kind") == "firmware updated" and event.get("ieee") and _age_when(event.get("time")):
            updated[event["ieee"]] = max(updated.get(event["ieee"], ""), event["time"])
    out = []
    for record in records:
        if record.get("status") != "active" or record.get("type") == "Coordinator":
            continue
        bought = _age_day((record.get("asset") or {}).get("purchase_date"))
        seen = _age_day(record.get("first_seen"))
        since = bought if bought and bought <= today else seen
        if since is None:
            continue
        changed = max(
            filter(None, (_age_when(record.get("firmware_changed")), updated.get(record["ieee"]))), default=None
        )
        age = (today - since).days
        quiet = (today - _age_day(changed)).days if changed and _age_day(changed) else age
        update = record.get("update") if isinstance(record.get("update"), dict) else {}
        out.append(
            {
                "ieee": record["ieee"],
                "name": record.get("friendly_name") or record["ieee"],
                "model": " ".join(
                    x for x in (record.get("vendor"), record.get("model") or record.get("model_id")) if x
                ),
                "since": since.isoformat(),
                "source": "bought" if since == bought else "first_seen",
                "age_days": age,
                "firmware": record.get("software_build_id") or update.get("installed_version"),
                "firmware_changed": changed,
                "stale": age >= STALE_DAYS and quiet >= STALE_DAYS,
            }
        )
    return sorted(out, key=lambda x: (-x["age_days"], x["name"].lower()))
