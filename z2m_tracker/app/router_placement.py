"""Which Home Assistant area an extra plug-in router would help most.

Pure functions, no tracker state. Each device row is a dict with:
  ieee, name, area   area is the HA area name, or None
  type               "Router" or "EndDevice" (the coordinator is left out by the caller)
  link_lqi           end device: LQI to its parent; router: its best link to another router (None: not scanned)
  hops               times its parent router changed in recent scans (end devices only)
  offline            offline right now
  flapping           dropped out repeatedly in the last 24 hours
  no_backup          router only: in the what-if for some router it had no other router in range
                     (the scan never shows what an end device hears, so end devices can't be judged)
"""

from __future__ import annotations

from typing import Any

from health import HOP_CHANGES

WEAK_LQI = 50
# How much each problem counts towards an area's score. A device with no backup route is the
# strongest case for a new router, a single dropout the weakest.
WEIGHTS = {"no_backup": 3, "weak": 2, "hopping": 2, "unstable": 1}
MIN_SCORE = 2
TOP = 3


def device_reasons(row: dict) -> list[str]:
    """The problems a router nearby would help with, strongest first."""
    out = []
    if row.get("no_backup"):
        out.append("no_backup")
    lqi = row.get("link_lqi")
    if isinstance(lqi, (int, float)) and lqi < WEAK_LQI:
        out.append("weak")
    if row.get("type") == "EndDevice" and (row.get("hops") or 0) >= HOP_CHANGES:
        out.append("hopping")
    if row.get("offline") or row.get("flapping"):
        out.append("unstable")
    return out


def rank_areas(rows: list[dict], top: int = TOP) -> dict[str, Any]:
    """The `top` areas where a plug-in router would help most, with the counts behind each score."""
    areas: dict[str, dict] = {}
    no_area = 0
    for row in rows:
        area = row.get("area")
        if area and area not in areas:
            areas[area] = {"area": area, "score": 0, "routers": 0, "devices": [], **dict.fromkeys(WEIGHTS, 0)}
        if area and row.get("type") == "Router" and not row.get("offline"):
            areas[area]["routers"] += 1
        reasons = device_reasons(row)
        if not reasons:
            continue
        if not area:
            no_area += 1
            continue
        entry = areas[area]
        for reason in reasons:
            entry[reason] += 1
            entry["score"] += WEIGHTS[reason]
        entry["devices"].append({"ieee": row["ieee"], "name": row.get("name") or row["ieee"], "reasons": reasons})
    ranked = sorted(
        (a for a in areas.values() if a["score"] >= MIN_SCORE),
        key=lambda a: (-a["score"], -len(a["devices"]), a["area"].lower()),
    )
    for entry in ranked:
        entry["devices"].sort(key=lambda d: (-sum(WEIGHTS[r] for r in d["reasons"]), d["name"].lower()))
    return {"areas": ranked[:top], "no_area": no_area}
