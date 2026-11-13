"""Battery to-buy list and replacement dates, worked out from the app's /api/devices payload.

Plain functions with no Home Assistant imports, so the todo and calendar platforms share them.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .const import STATUS_ACTIVE

RECENT_DAYS = 30
MAX_ESTIMATE_DAYS = 3650


def device_name(record: dict) -> str:
    return str(record.get("friendly_name") or record.get("ha_name") or record.get("ieee") or "")


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def shopping_groups(data: dict, today: date, hidden: set[str] | frozenset[str] = frozenset()) -> list[dict]:
    """The app's shopping list with a due date per group, leaving out the devices in `hidden`.

    The app drops a device from its list once it's marked replaced. `hidden` covers the
    moment between ticking an item and the integration's next read of the app.
    """
    groups = []
    for group in data.get("battery_shopping") or []:
        members = [d for d in group.get("devices") or [] if d.get("ieee") not in hidden]
        if not members:
            continue
        days = [d["days"] for d in members if isinstance(d.get("days"), (int, float))]
        count = sum(int(d.get("quantity") or 1) for d in members)
        stock = group.get("stock")
        stock = int(stock) if isinstance(stock, (int, float)) and not isinstance(stock, bool) else None
        groups.append(
            {
                "type": str(group.get("type") or "Unknown type"),
                "count": count,
                "stock": stock,
                "to_buy": max(0, count - stock) if stock is not None else None,
                "devices": members,
                "due": today + timedelta(days=max(0, round(min(days)))) if days else today,
            }
        )
    return groups


def group_summary(group: dict) -> str:
    """To-do item title: what to buy when the stock of this type is known, else how many are needed."""
    if group.get("stock") is None:
        return f"{group['type']} × {group['count']}"
    if group["to_buy"]:
        return f"{group['type']} × {group['to_buy']} to buy"
    return f"{group['type']} × {group['count']}, in stock"


def stock_line(group: dict) -> str:
    """Start of the to-do item description, comparing need with stock. Empty when stock is unknown."""
    if group.get("stock") is None:
        return ""
    return f"{group['count']} needed, {group['stock']} in stock. "


def describe_member(member: dict) -> str:
    parts = []
    battery = member.get("battery")
    parts.append(f"{round(battery)}%" if isinstance(battery, (int, float)) else "low")
    days = member.get("days")
    if isinstance(days, (int, float)):
        parts.append("due now" if days <= 0 else f"about {round(days)} days")
    if int(member.get("quantity") or 1) > 1:
        parts.append(f"takes {member['quantity']}")
    return f"{member.get('name') or member.get('ieee')} ({', '.join(parts)})"


def _battery_type(record: dict) -> tuple[str, int]:
    info = record.get("battery_record") or {}
    return str(info.get("type") or "Unknown type"), int(info.get("quantity") or 1)


def _is_battery_record(record: dict) -> bool:
    return record.get("status") == STATUS_ACTIVE and record.get("battery_record") is not None


def recent_replacements(data: dict, today: date, days: int = RECENT_DAYS) -> list[dict]:
    """Replacements recorded in the last `days` days, grouped by date and battery type, newest first."""
    groups: dict[tuple[date, str], dict] = {}
    for record in (data.get("devices") or {}).values():
        if not _is_battery_record(record):
            continue
        btype, quantity = _battery_type(record)
        seen: set[date] = set()
        for entry in (record.get("battery_record") or {}).get("replacements") or []:
            when = _parse_date(entry.get("date"))
            if when is None or when in seen or not 0 <= (today - when).days <= days:
                continue
            seen.add(when)
            group = groups.setdefault((when, btype), {"date": when, "type": btype, "count": 0, "names": []})
            group["count"] += quantity
            group["names"].append(device_name(record))
    return sorted(groups.values(), key=lambda g: (g["date"], g["type"]), reverse=True)


def predicted_replacements(data: dict, today: date) -> list[dict]:
    """One entry per active battery device that has a battery estimate, on its predicted date."""
    out = []
    for record in (data.get("devices") or {}).values():
        if not _is_battery_record(record):
            continue
        est = (record.get("metrics") or {}).get("battery_estimate_days")
        if not isinstance(est, (int, float)) or isinstance(est, bool):
            continue
        info = record.get("battery_record") or {}
        out.append(
            {
                "ieee": record["ieee"],
                "name": device_name(record),
                "date": today + timedelta(days=min(max(0, round(est)), MAX_ESTIMATE_DAYS)),
                "label": str(info.get("label") or info.get("type") or ""),
                "battery": record.get("battery"),
            }
        )
    return sorted(out, key=lambda e: (e["date"], e["name"].lower()))


def past_replacements(data: dict) -> list[dict]:
    """Every recorded replacement, plus a newer Battery Notes date the app merged in."""
    out = []
    for record in (data.get("devices") or {}).values():
        if record.get("battery_record") is None:
            continue
        info = record["battery_record"]
        dates: set[date] = set()
        for entry in info.get("replacements") or []:
            when = _parse_date(entry.get("date"))
            if when is None or when in dates:
                continue
            dates.add(when)
            out.append(
                {
                    "ieee": record["ieee"],
                    "name": device_name(record),
                    "date": when,
                    "label": str(info.get("label") or info.get("type") or ""),
                    "source": str(entry.get("source") or ""),
                    "note": str(entry.get("note") or ""),
                }
            )
        last = _parse_date(info.get("last_replaced"))
        if last is not None and last not in dates:
            out.append(
                {
                    "ieee": record["ieee"],
                    "name": device_name(record),
                    "date": last,
                    "label": str(info.get("label") or info.get("type") or ""),
                    "source": "Battery Notes",
                    "note": "",
                }
            )
    return sorted(out, key=lambda e: (e["date"], e["name"].lower()))
