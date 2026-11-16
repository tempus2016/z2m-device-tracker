"""Effect of an upgrade: the network in the week before a Zigbee2MQTT or coordinator firmware change against the week after.

Pure functions over data the tracker already keeps: device records (availability outages), the event list
(devices leaving), the hourly metrics series (link quality and message counts) and the routing scans
(parent changes). Nothing here reads the clock; callers pass `now`.

The hourly series are kept for 14 days and the event list is capped, so the "before" numbers are worked out
once, when the upgrade is seen, and stored with it. The "after" numbers are stored when the week is over
or when the next upgrade arrives.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

WINDOW_DAYS = 7
# Upgrades on one network kept in history.json.
MAX_UPGRADES = 20
# A change within this long of the previous one (firmware flashed, then Zigbee2MQTT updated) joins that upgrade.
MERGE_SECONDS = 3600
# The first hour after an upgrade is left out of "after": the restart itself knocks devices offline for a moment.
SETTLE_SECONDS = 3600
# Numbers are shown, and compared, once both windows hold at least this much data.
MIN_DAYS = 1.0
# The upgrade_worse check waits for this much of the "after" week, and stops this long after the upgrade.
ISSUE_MIN_DAYS = 2.0
ISSUE_MAX_DAYS = 30
# Earlier upgrades listed under the latest one.
EARLIER_SHOWN = 5
CHANGE_KINDS = ("zigbee2mqtt", "firmware")

# Order the metrics are shown in.
METRICS = ("offline_per_day", "lqi", "msg_per_hour", "leaves_per_day", "parent_changes_per_scan")
# Counts where more is worse: (smallest rise that counts, and the "after" value must also be at least this times "before").
RISE_IS_WORSE = {
    "offline_per_day": (1.0, 2.0),
    "leaves_per_day": (0.5, 2.0),
    "parent_changes_per_scan": (2.0, 1.5),
}
LQI_STEP = 10  # average link quality down (or up) by at least this much
MSG_SHARE = 0.3  # messages per hour down by at least 30 % is worse; up by 30 % is only "changed"
MSG_MIN = 10  # ...and by at least this many messages an hour
MIN_COMPARISONS = 2  # routing scans (each compared with the one before) needed for parent changes


def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def ts(value: Any) -> float | None:
    """Seconds since the epoch for a stored ISO time, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds")


def clean_upgrades(value: Any) -> dict[str, list[dict]]:
    """Stored or restored upgrades with the types this module expects; anything else is dropped."""
    out: dict[str, list[dict]] = {}
    if not isinstance(value, dict):
        return out
    for bt, items in value.items():
        if not isinstance(bt, str) or not isinstance(items, list):
            continue
        kept = []
        for item in items:
            if not isinstance(item, dict) or ts(item.get("at")) is None:
                continue
            changes = [
                {"what": c["what"], "from": str(c.get("from") or "")[:80], "to": str(c.get("to") or "")[:80]}
                for c in item.get("changes") or []
                if isinstance(c, dict) and c.get("what") in CHANGE_KINDS
            ]
            if not changes:
                continue
            kept.append(
                {
                    "at": item["at"],
                    "changes": changes,
                    "before": _clean_stats(item.get("before")),
                    "after": _clean_stats(item.get("after")),
                }
            )
        kept.sort(key=lambda u: ts(u["at"]) or 0)
        if kept:
            out[bt] = kept[-MAX_UPGRADES:]
    return out


def _clean_stats(stats: Any) -> dict | None:
    if not isinstance(stats, dict) or not _num(stats.get("days")):
        return None
    out: dict[str, Any] = {"days": stats["days"]}
    for key in ("from", "to"):
        if isinstance(stats.get(key), str):
            out[key] = stats[key]
    for key in ("devices", "scans", *METRICS):
        if _num(stats.get(key)):
            out[key] = stats[key]
    return out


def window_stats(
    bt: str,
    start: float,
    end: float,
    records: dict[str, dict],
    series: dict[str, dict],
    events: list[dict],
    scans: list[dict],
    events_from: float | None = None,
) -> dict | None:
    """Network-wide numbers for one base topic between `start` and `end` (seconds), or None without data.

    The window is narrowed to where the metrics series have data, so a tracker that started halfway
    through the week compares the days it saw. A metric is left out when its data isn't there:
    offline counts need availability reporting, leaves need the event list to reach back to the start
    (`events_from` is the oldest event when the list has been trimmed), parent changes need routing scans.
    """
    if end <= start:
        return None
    members = [
        ieee
        for ieee, record in records.items()
        if isinstance(record, dict) and record.get("instance") == bt and record.get("type") != "Coordinator"
    ]
    first: float | None = None
    messages = 0.0
    seen_messages = False
    lqi_means: list[float] = []
    for ieee in members:
        data = series.get(ieee) or {}
        for point in data.get("msg") or []:
            if isinstance(point, list) and len(point) >= 2 and _num(point[0]) and _num(point[1]):
                if start <= point[0] < end:
                    messages += point[1]
                    seen_messages = True
                    first = point[0] if first is None else min(first, point[0])
        total = count = 0.0
        for point in data.get("lqi") or []:
            if isinstance(point, list) and len(point) >= 3 and all(_num(p) for p in point[:3]):
                if start <= point[0] < end and point[2] > 0:
                    total += point[1]
                    count += point[2]
                    first = point[0] if first is None else min(first, point[0])
        if count:
            lqi_means.append(total / count)
    if first is None:
        return None
    since = max(start, first)
    days = (end - since) / 86400
    if days <= 0:
        return None
    out: dict[str, Any] = {"from": _iso(since), "to": _iso(end), "days": round(days, 2), "devices": len(members)}
    if seen_messages:
        out["msg_per_hour"] = round(messages / (days * 24), 1)
    if lqi_means:
        out["lqi"] = round(sum(lqi_means) / len(lqi_means), 1)

    member_set = set(members)
    if any(records[ieee].get("availability") is not None for ieee in members):
        outages = 0
        for ieee in members:
            for outage in records[ieee].get("outages") or []:
                began = ts(outage.get("start")) if isinstance(outage, dict) else None
                if began is not None and since <= began < end:
                    outages += 1
        out["offline_per_day"] = round(outages / days, 2)

    if events_from is None or events_from <= since:
        leaves = 0
        for event in events:
            if isinstance(event, dict) and event.get("instance") == bt and event.get("kind") == "left":
                when = ts(event.get("time"))
                if when is not None and since <= when < end:
                    leaves += 1
        out["leaves_per_day"] = round(leaves / days, 2)

    changes = comparisons = 0
    previous: dict[str, str] = {}
    had_scan = False
    for scan in sorted((s for s in scans if isinstance(s, dict)), key=lambda s: str(s.get("at") or "")):
        when = ts(scan.get("at"))
        parents = scan.get("parents") if isinstance(scan.get("parents"), dict) else {}
        inside = when is not None and since <= when < end
        if inside and had_scan:
            comparisons += 1
        for ieee, entry in parents.items():
            if ieee not in member_set or not isinstance(entry, list) or not entry:
                continue
            parent = entry[0]
            if inside and previous.get(ieee) not in (None, parent):
                changes += 1
            previous[ieee] = parent
        if when is not None and when < end:
            had_scan = True
        if when is not None and when >= end:
            break
    if comparisons >= MIN_COMPARISONS:
        out["parent_changes_per_scan"] = round(changes / comparisons, 2)
        out["scans"] = comparisons
    return out


def compare_metric(key: str, before: float, after: float) -> str:
    """'worse', 'better', 'changed' (messages up: not good or bad on its own) or 'same'."""
    if key == "lqi":
        if before - after >= LQI_STEP:
            return "worse"
        return "better" if after - before >= LQI_STEP else "same"
    if key == "msg_per_hour":
        if before - after >= MSG_MIN and after <= before * (1 - MSG_SHARE):
            return "worse"
        return "changed" if after - before >= MSG_MIN and after >= before * (1 + MSG_SHARE) else "same"
    step, ratio = RISE_IS_WORSE[key]
    if after - before >= step and after >= before * ratio:
        return "worse"
    return "better" if before - after >= step and before >= after * ratio else "same"


def describe(upgrade: dict, after: dict | None, now: float) -> dict:
    """What the sidebar and the upgrade_worse check need about one upgrade."""
    at = ts(upgrade.get("at")) or now
    before = upgrade.get("before")
    elapsed = max(0.0, (now - at) / 86400)
    before_days = before.get("days", 0) if isinstance(before, dict) else 0
    after_days = after.get("days", 0) if isinstance(after, dict) else 0
    # The "after" side starts SETTLE_SECONDS late, so a day after the upgrade it holds a little under a day.
    ready = before_days >= MIN_DAYS and after_days >= MIN_DAYS - SETTLE_SECONDS / 86400 and elapsed >= MIN_DAYS
    rows = []
    if ready:
        for key in METRICS:
            if _num(before.get(key)) and _num(after.get(key)):
                rows.append(
                    {
                        "key": key,
                        "before": before[key],
                        "after": after[key],
                        "status": compare_metric(key, before[key], after[key]),
                    }
                )
    statuses = {row["status"] for row in rows}
    verdict = None if not rows else "worse" if "worse" in statuses else "better" if "better" in statuses else "same"
    return {
        "at": upgrade.get("at"),
        "changes": upgrade.get("changes") or [],
        "elapsed_days": round(min(elapsed, WINDOW_DAYS), 1),
        "complete": isinstance(upgrade.get("after"), dict) or elapsed * 86400 >= WINDOW_DAYS * 86400 + SETTLE_SECONDS,
        "ready": ready,
        "no_before": before_days < MIN_DAYS,
        "before_days": round(before_days, 1),
        "after_days": round(after_days, 1),
        "metrics": rows,
        "verdict": verdict,
    }


def summary(upgrades: list[dict], latest_after: dict | None, now: float) -> dict | None:
    """{"latest": describe(...), "earlier": [describe(...), ...]} for one network, newest first."""
    if not upgrades:
        return None
    earlier = [describe(u, u.get("after"), now) for u in reversed(upgrades[:-1])][:EARLIER_SHOWN]
    return {"latest": describe(upgrades[-1], latest_after, now), "earlier": earlier, "total": len(upgrades)}


def after_window(upgrade: dict, next_at: float | None = None) -> tuple[float, float]:
    """(start, end) of the week after an upgrade, cut short by the next upgrade."""
    at = ts(upgrade.get("at")) or 0.0
    start = at + SETTLE_SECONDS
    end = start + WINDOW_DAYS * 86400
    if next_at is not None:
        end = min(end, next_at)
    return start, end


def before_window(at: float, previous: dict | None) -> tuple[float, float]:
    """(start, end) of the week before an upgrade, starting no earlier than the previous upgrade settled."""
    start = at - WINDOW_DAYS * 86400
    if previous is not None:
        prev_at = ts(previous.get("at"))
        if prev_at is not None:
            start = max(start, prev_at + SETTLE_SECONDS)
    return start, at


def merge_changes(current: list[dict], new: list[dict]) -> list[dict]:
    """Add new changes to an upgrade: a second change of the same kind keeps the first 'from'."""
    out = [dict(c) for c in current]
    for change in new:
        same = next((c for c in out if c["what"] == change["what"]), None)
        if same is None:
            out.append(dict(change))
        else:
            same["to"] = change["to"]
    return [c for c in out if c["from"] != c["to"]]
