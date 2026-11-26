"""Battery records: replacements, stock, prices, brands, shopping.

Part of Tracker (tracker.py): these methods use its lock, data and metrics.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date as date_cls
from datetime import datetime
from typing import Any

from costs import clean_battery_prices, running_costs
from health import (
    limit_of,
)
from tracker_base import (
    BRAND_MIN_INTERVALS,
    MAX_BRAND,
    MAX_BRANDS,
    MAX_STOCK,
    STATUS_ACTIVE,
    STOCK_WINDOW_DAYS,
    UNKNOWN_BATTERY_TYPE,
    _brand_list,
    _display_name,
    _max_time,
    _min_time,
    _now,
    _text,
)

_LOGGER = logging.getLogger("z2m_tracker")


class BatteryMixin:
    # ---------------------------------------------------------------- battery

    def battery_unreliable(self, ieee: str) -> bool:
        """True when the user marked this device's battery level as not to be trusted."""
        record = self.data["devices"].get(ieee) or {}
        return bool((record.get("battery_info") or {}).get("unreliable"))

    def _battery_jump(self, record: dict, date: str, before: float, after: float) -> bool:
        """A battery level that rose sharply: record a replacement, ask first, or ignore it. True if anything changed."""
        info = record.get("battery_info") or {}
        if info.get("unreliable") or not self.options.get("battery_auto_detect", True):
            return False
        note = f"Battery went from {before:.0f}% to {after:.0f}%"
        if not self.options.get("battery_confirm_detected", False):
            self._record_replacement(record, date, "Detected", note)
            return True
        pending = info.get("pending")
        if isinstance(pending, dict) and pending.get("date") == date:
            return False
        record.setdefault("battery_info", {})["pending"] = {"date": date, "note": note}
        self._add_event(
            _now(),
            record.get("instance") or "unknown",
            record["ieee"],
            record.get("friendly_name"),
            "battery change suggested",
            f"{note}. Confirm or dismiss it on the device's page.",
        )
        return True

    def resolve_battery_pending(self, ieee: str, confirm: bool) -> bool:
        """Confirm (record it as a replacement) or dismiss a detected battery change waiting for the user."""
        with self.lock:
            record = self.data["devices"].get(ieee)
            info = (record or {}).get("battery_info") or {}
            pending = info.pop("pending", None)
            if not isinstance(pending, dict):
                return False
            if confirm and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(pending.get("date") or "")):
                self._record_replacement(record, pending["date"], "Detected", str(pending.get("note") or ""))
            self._changed(structural=True)
            return True

    def set_battery_unreliable(self, ieee: str, unreliable: Any) -> bool:
        """Mark a device's battery level as unreliable: no detected replacements and no estimate."""
        if not isinstance(unreliable, bool):
            return False
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None:
                return False
            info = record.setdefault("battery_info", {})
            if unreliable:
                info["unreliable"] = True
                info.pop("pending", None)
            else:
                info.pop("unreliable", None)
            self._changed(structural=True)
            return True

    def _record_replacement(
        self, record: dict, date: str, source: str, note: str = "", brand: str | None = None
    ) -> dict:
        info = record.setdefault("battery_info", {})
        # Recording a change answers any detected change still waiting to be confirmed.
        info.pop("pending", None)
        replacements = info.setdefault("replacements", [])
        entry = {"date": date, "source": source, "note": note[:200], "recorded": _now()}
        brand = _text(brand, MAX_BRAND)
        if brand:
            entry["brand"] = brand
        if replacements and replacements[-1].get("date") == date and source == "Detected":
            return replacements[-1]
        replacements.append(entry)
        self._take_from_stock(record, entry)
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

    def _stock_key(self, btype: str) -> str | None:
        """The stock entry for a battery type, matched without regard to case. None when it isn't stocked."""
        wanted = (btype or "").strip().casefold()
        if not wanted:
            return None
        for key in self.data.get("battery_stock") or {}:
            if key.casefold() == wanted:
                return key
        return None

    def _take_from_stock(self, record: dict, entry: dict) -> None:
        """Take one change's batteries from stock, once per change, never below zero."""
        info = self.battery_summary(record)
        key = self._stock_key(info["type"])
        if key is None:
            return
        try:
            day = date_cls.fromisoformat(str(entry.get("date"))[:10])
        except ValueError:
            return
        today = datetime.fromtimestamp(time.time(), tz=self.local_tz).date()
        if (today - day).days > STOCK_WINDOW_DAYS:
            return
        for other in (record.get("battery_info") or {}).get("replacements") or []:
            if other is entry or "stock_used" not in other:
                continue
            try:
                other_day = date_cls.fromisoformat(str(other.get("date"))[:10])
            except ValueError:
                continue
            if abs((other_day - day).days) <= STOCK_WINDOW_DAYS:
                return
        stock = self.data["battery_stock"]
        used = min(int(stock.get(key) or 0), int(info["quantity"] or 1))
        stock[key] = int(stock.get(key) or 0) - used
        entry["stock_used"] = used
        entry["stock_type"] = key

    def _known_battery_type(self, name: str) -> str | None:
        """A device's spelling of this battery type ("aaa" -> "AAA"), so new stock matches the shopping list."""
        wanted = name.casefold()
        for record in self.data["devices"].values():
            if self.is_battery_device(record):
                btype = self.battery_summary(record)["type"]
                if btype and btype.casefold() == wanted:
                    return btype
        return None

    def set_battery_stock(self, btype: str, count: Any = None, delta: Any = None) -> dict:
        """Set the spare count for a battery type (`count`), or add to it (`delta`, may be negative)."""
        name = str(btype or "").strip()[:40]
        if not name or name.casefold() == UNKNOWN_BATTERY_TYPE.casefold():
            return {"ok": False, "error": "Give a battery type, for example CR2032"}

        def whole(value: Any) -> int | None:
            if isinstance(value, bool):
                return None
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None
            return int(number) if number == int(number) else None

        with self.lock:
            stock = self.data.setdefault("battery_stock", {})
            key = self._stock_key(name) or self._known_battery_type(name) or name
            current = int(stock.get(key) or 0)
            if count is not None:
                value = whole(count)
                if value is None:
                    return {"ok": False, "error": "Count must be a whole number"}
                new = value
            elif delta is not None:
                step = whole(delta)
                if step is None:
                    return {"ok": False, "error": "Change must be a whole number"}
                new = current + step
            else:
                return {"ok": False, "error": "Give a count or a change"}
            stock[key] = max(0, min(MAX_STOCK, new))
            self._changed(structural=True)
            return {"ok": True, "type": key, "count": stock[key]}

    def set_battery_prices(self, prices: Any) -> dict:
        """Replace the price per battery type (Settings). Types left out or set to null are removed."""
        if not isinstance(prices, dict):
            return {"ok": False, "error": "Give prices as {type: price}"}
        with self.lock:
            self.data["battery_prices"] = clean_battery_prices({k: v for k, v in prices.items() if v is not None})
            self._changed(structural=True)
            return {"ok": True, "prices": dict(self.data["battery_prices"])}

    def running_costs(self, now: float | None = None) -> dict:
        """Device and battery spend (see costs.running_costs), plus the battery types in use for Settings."""
        now = time.time() if now is None else now
        items = []
        types: set[str] = set()
        with self.lock:
            for record in self.data["devices"].values():
                if record.get("type") == "Coordinator":
                    continue
                battery = self.is_battery_device(record)
                info = self.battery_summary(record) if battery else {}
                active = record["status"] == STATUS_ACTIVE
                if battery and active and info.get("type"):
                    types.add(info["type"])
                items.append(
                    {
                        "model": " ".join(
                            x for x in (record.get("vendor"), record.get("model") or record.get("model_id")) if x
                        ),
                        "price": (record.get("asset") or {}).get("price"),
                        "active": active,
                        "battery": battery,
                        "type": info.get("type") or "",
                        "quantity": info.get("quantity") or 1,
                        "changes": [r.get("date") for r in info.get("replacements") or []],
                        "installed": info.get("installed") or "",
                        "estimate_days": self.metrics.summary(record["ieee"], now).get("battery_estimate_days")
                        if battery and active
                        else None,
                    }
                )
            prices = clean_battery_prices(self.data.get("battery_prices"))
        out = running_costs(items, prices, self._today())
        out["types"] = sorted(types, key=str.casefold)
        return out

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

    def add_battery_replacement(
        self, ieee: str, date: str, source: str, note: str = "", brand: str | None = None
    ) -> dict | None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
            return None
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None:
                return None
            entry = self._record_replacement(record, date, source, note, brand)
            if entry.get("brand"):
                self._remember_brand(entry["brand"])
            self._changed(structural=True)
            return entry

    def brand_list(self) -> list[str]:
        """The saved brands. The first time, it starts from the brands already on recorded changes."""
        with self.lock:
            brands = self.data.get("battery_brand_list")
            if not isinstance(brands, list):
                used = [
                    entry.get("brand")
                    for record in self.data["devices"].values()
                    for entry in (record.get("battery_info") or {}).get("replacements") or []
                    if isinstance(entry, dict)
                ]
                self.data["battery_brand_list"] = _brand_list(used)
                self._changed(structural=False)
            return self.data["battery_brand_list"]

    def _remember_brand(self, brand: str) -> bool:
        """Add a brand to the saved list. False when the list is full; True when it is there already."""
        brands = self.brand_list()
        if any(b.casefold() == brand.casefold() for b in brands):
            return True
        if len(brands) >= MAX_BRANDS:
            return False
        self.data["battery_brand_list"] = _brand_list([*brands, brand])
        return True

    def add_brand(self, brand: Any) -> dict:
        clean = _text(brand, MAX_BRAND)
        if not clean:
            return {"ok": False, "error": "Give a brand name"}
        with self.lock:
            if not self._remember_brand(clean):
                return {"ok": False, "error": f"The list holds up to {MAX_BRANDS} brands. Remove one first."}
            self._changed(structural=True)
            return {"ok": True, "brands": list(self.brand_list())}

    def remove_brand(self, brand: Any) -> dict:
        """Take a brand off the saved list. Changes already recorded with it keep it."""
        clean = _text(brand, MAX_BRAND)
        with self.lock:
            brands = self.brand_list()
            kept = [b for b in brands if not clean or b.casefold() != clean.casefold()]
            if len(kept) == len(brands):
                return {"ok": False, "error": "That brand isn't in the list"}
            self.data["battery_brand_list"] = kept
            self._changed(structural=True)
            return {"ok": True, "brands": list(kept)}

    def set_battery_brand(self, ieee: str, index: Any, brand: Any) -> bool:
        """Set or clear (empty text) the brand on one recorded battery change."""
        if isinstance(index, bool) or not isinstance(index, int):
            return False
        if brand is not None and not isinstance(brand, str):
            return False
        with self.lock:
            record = self.data["devices"].get(ieee)
            replacements = ((record or {}).get("battery_info") or {}).get("replacements") or []
            if not 0 <= index < len(replacements):
                return False
            clean = _text(brand, MAX_BRAND)
            if clean:
                replacements[index]["brand"] = clean
                self._remember_brand(clean)
            else:
                replacements[index].pop("brand", None)
            self._changed(structural=True)
            return True

    @staticmethod
    def _battery_changes(replacements: list) -> list[tuple[date_cls, str | None]]:
        """One (day, brand) per real battery change, oldest first.

        Entries within STOCK_WINDOW_DAYS of each other are the same change seen twice (detected, then
        ticked), so they merge; the merged change takes the latest brand given for it.
        """
        days = []
        for entry in replacements:
            if not isinstance(entry, dict):
                continue
            try:
                day = date_cls.fromisoformat(str(entry.get("date"))[:10])
            except ValueError:
                continue
            days.append((day, _text(entry.get("brand"), MAX_BRAND)))
        days.sort(key=lambda x: x[0])
        changes: list[tuple[date_cls, str | None]] = []
        for day, brand in days:
            if changes and (day - changes[-1][0]).days <= STOCK_WINDOW_DAYS:
                changes[-1] = (changes[-1][0], brand or changes[-1][1])
            else:
                changes.append((day, brand))
        return changes

    def battery_brands(self) -> dict:
        """Average life per battery brand, per battery type, from completed intervals only.

        An interval runs from one change to the next on the same device and belongs to the brand put in
        at the start of it. The battery in now hasn't run out yet, so it isn't counted. Also returns the
        brand used most recently per type, to prefill the next change.
        """
        stats: dict[str, dict[str, dict]] = {}
        latest: dict[str, tuple[str, str]] = {}
        with self.lock:
            for record in self.data["devices"].values():
                replacements = (record.get("battery_info") or {}).get("replacements") or []
                if not any(isinstance(r, dict) and r.get("brand") for r in replacements):
                    continue
                btype = self.battery_summary(record)["type"] or UNKNOWN_BATTERY_TYPE
                tkey = btype.casefold()
                for entry in replacements:
                    brand = _text(entry.get("brand"), MAX_BRAND) if isinstance(entry, dict) else None
                    when = f"{entry.get('date') or ''} {entry.get('recorded') or ''}" if brand else ""
                    if brand and (tkey not in latest or when >= latest[tkey][0]):
                        latest[tkey] = (when, brand)
                changes = self._battery_changes(replacements)
                per_type = stats.setdefault(tkey, {"type": btype, "brands": {}})
                for i, (day, brand) in enumerate(changes):
                    if not brand:
                        continue
                    item = per_type["brands"].setdefault(
                        brand.casefold(), {"brand": brand, "days": [], "in_use": 0, "seen": day}
                    )
                    if day >= item["seen"]:
                        item["brand"], item["seen"] = brand, day
                    if i + 1 < len(changes):
                        item["days"].append((changes[i + 1][0] - day).days)
                    elif record.get("status") == STATUS_ACTIVE:
                        item["in_use"] += 1
        types = []
        for per_type in stats.values():
            brands = []
            for item in per_type["brands"].values():
                n = len(item["days"])
                enough = n >= BRAND_MIN_INTERVALS
                brands.append(
                    {
                        "brand": item["brand"],
                        "intervals": n,
                        "in_use": item["in_use"],
                        "average_days": round(sum(item["days"]) / n) if enough else None,
                        "shortest_days": min(item["days"]) if enough else None,
                        "longest_days": max(item["days"]) if enough else None,
                    }
                )
            if not brands:
                continue
            brands.sort(key=lambda b: (b["average_days"] is None, -(b["average_days"] or 0), b["brand"].casefold()))
            types.append({"type": per_type["type"], "brands": brands})
        types.sort(key=lambda x: (x["type"] == UNKNOWN_BATTERY_TYPE, x["type"].casefold()))
        last = {stats[k]["type"] if k in stats else k: brand for k, (_, brand) in latest.items()}
        return {"types": types, "last_brand": last}

    def remove_battery_replacement(self, ieee: str, index: int) -> bool:
        with self.lock:
            record = self.data["devices"].get(ieee)
            replacements = ((record or {}).get("battery_info") or {}).get("replacements") or []
            if not 0 <= index < len(replacements):
                return False
            removed = replacements.pop(index)
            used = removed.get("stock_used")
            if isinstance(used, int) and used > 0:
                # Undo a change recorded by mistake: its batteries go back in the drawer.
                stock = self.data.setdefault("battery_stock", {})
                key = self._stock_key(str(removed.get("stock_type") or "")) or str(removed.get("stock_type") or "")
                if key:
                    stock[key] = min(MAX_STOCK, int(stock.get(key) or 0) + used)
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
            "unreliable": bool(info.get("unreliable")),
            "pending": info.get("pending") if isinstance(info.get("pending"), dict) else None,
        }

    def is_battery_device(self, record: dict) -> bool:
        return (
            record.get("power_source") == "Battery"
            or isinstance(record.get("battery"), (int, float))
            or bool((record.get("battery_info") or {}).get("type"))
            or record["ieee"] in self.ha_battery
        )

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
        """Batteries needed soon, grouped by type, against the spares in stock.

        Each group has `count` (needed), `stock` and `to_buy`. `stock` and `to_buy` are None
        for a type whose stock was never entered.
        """
        now = time.time()
        groups: dict[str, dict] = {}
        with self.lock:
            for record in self.data["devices"].values():
                if record["status"] != STATUS_ACTIVE or not self.is_battery_device(record) or self.is_frozen(record):
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
                key = info["type"] or UNKNOWN_BATTERY_TYPE
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
            stock = self.data.get("battery_stock") or {}
            for group in groups.values():
                stock_key = self._stock_key(group["type"]) if group["type"] != UNKNOWN_BATTERY_TYPE else None
                group["stock"] = int(stock[stock_key]) if stock_key else None
                group["to_buy"] = max(0, group["count"] - group["stock"]) if stock_key else None
        for group in groups.values():
            group["devices"].sort(
                key=lambda d: (
                    d["days"] if d["days"] is not None else 999,
                    d["battery"] if d["battery"] is not None else 101,
                )
            )
        return sorted(groups.values(), key=lambda g: (g["type"] == UNKNOWN_BATTERY_TYPE, -g["count"]))
