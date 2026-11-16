"""Refresh z2m_tracker/app/battery_library.json from the Battery Notes device library.

Usage:
    python scripts/update_battery_library.py              download from GitHub
    python scripts/update_battery_library.py library.json  convert a local copy

Writes the compact form the app loads (see battery_library.py): one row per device,
[manufacturer, model, model_id, battery_type, quantity], in upstream order.
Source: https://github.com/andrew-codechimp/HA-Battery-Notes (MIT, see battery_library.LICENSE).
Exits 0 whether or not anything changed; prints the row counts.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

URL = "https://raw.githubusercontent.com/andrew-codechimp/HA-Battery-Notes/main/library/library.json"
SOURCE = "https://github.com/andrew-codechimp/HA-Battery-Notes (library/library.json, MIT licence)"
TARGET = Path(__file__).resolve().parent.parent / "z2m_tracker" / "app" / "battery_library.json"
MIN_ROWS = 500  # A much smaller library means a broken download, not a real change.


def load_upstream(argv: list[str]) -> dict:
    if len(argv) > 1:
        return json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    with urllib.request.urlopen(URL, timeout=60) as response:  # noqa: S310 (fixed https URL)
        return json.loads(response.read().decode("utf-8"))


def convert(upstream: dict) -> dict:
    rows = []
    for device in upstream.get("devices", []):
        manufacturer = str(device.get("manufacturer") or "").strip()
        model = str(device.get("model") or "").strip()
        battery_type = str(device.get("battery_type") or "").strip()
        if not manufacturer or not model or not battery_type:
            continue
        quantity = device.get("battery_quantity") or 1
        rows.append([manufacturer, model, str(device.get("model_id") or "").strip(), battery_type, int(quantity)])
    return {"source": SOURCE, "version": upstream.get("version", 1), "devices": rows}


def main(argv: list[str]) -> int:
    data = convert(load_upstream(argv))
    count = len(data["devices"])
    if count < MIN_ROWS:
        print(f"Only {count} devices in the upstream library; refusing to overwrite.", file=sys.stderr)
        return 1
    before = len(json.loads(TARGET.read_text(encoding="utf-8")).get("devices", [])) if TARGET.exists() else 0
    TARGET.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"battery_library.json: {before} -> {count} devices")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
