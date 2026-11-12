"""Battery type/quantity lookup using the Battery Notes community device library.

Source: https://github.com/andrew-codechimp/HA-Battery-Notes (library/library.json), MIT licence,
see battery_library.LICENSE. Bundled so it works offline and without Battery Notes installed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


class BatteryLibrary:
    def __init__(self, path: Path) -> None:
        self.by_vendor_model: dict[tuple[str, str], tuple[str, int]] = {}
        self.by_model: dict[str, set[tuple[str, int]]] = {}
        self.by_vendor_desc: dict[tuple[str, str], tuple[str, int]] = {}
        self.count = 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for manufacturer, model, model_id, btype, quantity in data.get("devices", []):
            value = (btype, int(quantity or 1))
            vendor = _norm(manufacturer)
            codes = {_norm(model_id)} if model_id else set()
            codes |= {_norm(code) for code in re.findall(r"\(([^()]+)\)\s*$", model or "")}
            for code in codes - {""}:
                self.by_vendor_model.setdefault((vendor, code), value)
                self.by_model.setdefault(code, set()).add(value)
            if model:
                self.by_vendor_desc.setdefault((vendor, _norm(model)), value)
                self.by_model.setdefault(_norm(model), set()).add(value)
            self.count += 1

    def lookup(self, record: dict) -> tuple[str, int] | None:
        vendor = _norm(record.get("vendor"))
        manufacturer = _norm(record.get("manufacturer"))
        model = _norm(record.get("model"))
        model_id = _norm(record.get("model_id"))
        description = _norm(record.get("description"))
        for key in ((vendor, model), (manufacturer, model_id), (manufacturer, model), (vendor, model_id)):
            if all(key) and key in self.by_vendor_model:
                return self.by_vendor_model[key]
        for key in ((vendor, description), (vendor, f"{description}{model}")):
            if all(key) and key in self.by_vendor_desc:
                return self.by_vendor_desc[key]
        for code in (model, model_id):
            values = self.by_model.get(code) if code else None
            if values and len(values) == 1:
                return next(iter(values))
        return None
