"""Running costs: what the devices cost to buy and what their batteries cost to keep going.

Device prices are the free text "price" in each device's asset details ("£14.99", "14,99 €").
Battery prices per type are kept by the app (history.json "battery_prices"), set in Settings.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date, timedelta
from typing import Any

MAX_PRICE = 100000.0
MAX_BATTERY_PRICE = 1000.0
MAX_BATTERY_TYPES = 50
YEAR_DAYS = 365
SAME_CHANGE_DAYS = 3  # entries this close are one change seen twice (as tracker.STOCK_WINDOW_DAYS)
NUMBER_RE = re.compile(r"\d[\d.,']*")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_price(text: Any) -> tuple[float | None, str]:
    """A price typed as free text: (amount, currency mark). Accepts "£14.99", "14,99 €", "EUR 1.234,50",
    "$1,234.50". The mark is whatever surrounds the number ("£", "€", "EUR"), "" when there is none."""
    text = str(text or "").strip()
    match = NUMBER_RE.search(text)
    if not match:
        return None, ""
    raw = match.group(0).rstrip(".,'").replace("'", "")
    if "," in raw and "." in raw:
        decimal = "," if raw.rfind(",") > raw.rfind(".") else "."
        raw = raw.replace("." if decimal == "," else ",", "").replace(",", ".")
    elif "," in raw:
        whole, _, tail = raw.rpartition(",")
        # One comma with 1 or 2 digits after it is a decimal comma; otherwise commas group thousands.
        raw = f"{whole.replace(',', '')}.{tail}" if raw.count(",") == 1 and len(tail) <= 2 else raw.replace(",", "")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    try:
        amount = float(raw)
    except ValueError:
        return None, ""
    if not math.isfinite(amount) or amount < 0 or amount > MAX_PRICE:
        return None, ""
    mark = (text[: match.start()] + text[match.end() :]).strip()
    mark = re.sub(r"[\s\d.,']+", "", mark)[:4]
    return amount, mark


def clean_battery_prices(raw: Any) -> dict[str, float]:
    """Battery prices per type from untrusted input: {type: price per battery}. Bad entries are dropped."""
    out: dict[str, float] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        name = str(key or "").strip()[:40]
        if not name or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not math.isfinite(value) or not 0 <= value <= MAX_BATTERY_PRICE:
            continue
        out[name] = round(float(value), 2)
        if len(out) >= MAX_BATTERY_TYPES:
            break
    return out


def price_of(prices: dict[str, float], btype: str) -> float | None:
    """The price for a battery type, matched without regard to case."""
    wanted = (btype or "").strip().casefold()
    return next((v for k, v in prices.items() if k.casefold() == wanted), None) if wanted else None


def _day(value: Any) -> date | None:
    value = str(value or "")[:10]
    if not DATE_RE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def change_days(dates: list[str]) -> list[date]:
    """One day per real battery change, oldest first. Entries within SAME_CHANGE_DAYS of the change
    before them are the same change logged twice (detected, then ticked), so they merge into it."""
    out: list[date] = []
    for day in sorted({d for d in (_day(x) for x in dates) if d}):
        if not out or (day - out[-1]).days > SAME_CHANGE_DAYS:
            out.append(day)
    return out


def intervals(dates: list[str]) -> list[int]:
    """Days between consecutive battery changes (the install date counts as the first)."""
    points = change_days(dates)
    return [(b - a).days for a, b in zip(points, points[1:], strict=False)]


def running_costs(devices: list[dict], prices: dict[str, float], today: date) -> dict:
    """Totals and a per-model table.

    Each device: {"model", "price" (asset text), "active", "battery" (bool), "type", "quantity",
    "changes" (replacement dates), "installed", "estimate_days" (days until the battery is low, or None)}.

    Battery life per device is its own mean time between changes, else the mean for its model.
    The next 12 months: a device whose battery is predicted to run low within the year needs one
    change then, plus one per battery life after; without a prediction, one change per battery life.
    """
    marks: Counter = Counter()
    spent = 0.0
    priced = 0
    for item in devices:
        amount, mark = parse_price(item.get("price"))
        if amount is None:
            continue
        spent += amount
        priced += 1
        if mark:
            marks[mark] += 1

    model_lives: dict[str, list[int]] = {}
    for item in devices:
        if item.get("battery"):
            lives = intervals([*(item.get("changes") or []), item.get("installed") or ""])
            item["_lives"] = lives
            model_lives.setdefault(item.get("model") or "", []).extend(lives)

    year_ago = today - timedelta(days=YEAR_DAYS)
    unpriced: set[str] = set()
    last_cost = 0.0
    last_count = 0
    next_cost = 0.0
    next_count = 0.0
    unknown = 0
    groups: dict[tuple[str, str], dict] = {}
    for item in devices:
        if not item.get("battery"):
            continue
        btype = item.get("type") or ""
        quantity = int(item.get("quantity") or 1)
        price = price_of(prices, btype)
        recent = sum(1 for day in change_days(item.get("changes") or []) if year_ago < day <= today)
        if recent:
            last_count += recent * quantity
            if price is None:
                unpriced.add(btype or "?")
            else:
                last_cost += recent * quantity * price
        if not item.get("active"):
            continue
        own = item.get("_lives") or []
        pooled = model_lives.get(item.get("model") or "") or []
        life = sum(own) / len(own) if own else sum(pooled) / len(pooled) if pooled else None
        est = item.get("estimate_days")
        if est is not None:
            changes = 0 if est > YEAR_DAYS else 1 + (math.floor((YEAR_DAYS - est) / life) if life else 0)
        elif life:
            changes = YEAR_DAYS / life
        else:
            unknown += 1
            continue
        next_count += changes * quantity
        if price is None:
            if changes:
                unpriced.add(btype or "?")
        else:
            next_cost += changes * quantity * price
        if life:
            key = (item.get("model") or "", btype)
            group = groups.setdefault(key, {"model": key[0], "type": btype, "devices": 0, "rates": []})
            group["devices"] += 1
            group["rates"].append(YEAR_DAYS / life * quantity)

    models = []
    for group in groups.values():
        per_year = sum(group["rates"]) / len(group["rates"])
        price = price_of(prices, group["type"])
        models.append(
            {
                "model": group["model"],
                "type": group["type"],
                "devices": group["devices"],
                "per_year": round(per_year, 1),
                "cost_year": round(per_year * price, 2) if price is not None else None,
            }
        )
    models.sort(key=lambda m: (-(m["cost_year"] if m["cost_year"] is not None else -1), -m["per_year"], m["model"]))
    return {
        "currency": marks.most_common(1)[0][0] if marks else "",
        "devices_total": round(spent, 2),
        "devices_priced": priced,
        "battery_last_12m": round(last_cost, 2),
        "battery_last_count": last_count,
        "battery_next_12m": round(next_cost, 2),
        "battery_next_count": round(next_count, 1),
        "unestimated": unknown,
        "unpriced": sorted(unpriced),
        "models": models,
    }
