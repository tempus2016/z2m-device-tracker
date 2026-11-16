"""Incidents: related problem events on one Zigbee2MQTT network grouped into one story.

Pure functions over the stored events list and the live device/bridge records: no I/O, no clock.

Grouping rule
-------------
- Events are taken per network (the event's `instance`), oldest first.
- Problem events are: offline, left, network offline, zigbee2mqtt restarted, interview failed.
  Route changes are not events in the stored log (they only show between routing scans), and
  `moved` means a device moved between Z2M networks, so neither starts an incident.
- A problem event starts an incident, or extends the open one on the same network when it comes
  within GAP seconds (10 minutes) of that incident's previous problem event. After GAP seconds with
  no new problem event the incident closes for new problems; recovery events (online, joined,
  rejoined, added, interview successful, network online) for the devices in it are still added.
- Only incidents with 3 or more devices, or a network-level event (network offline, Zigbee2MQTT
  restarted), are kept. One battery sensor dropping off alone is not an incident.
- An incident is ongoing while a device in it is still offline now (live record: active and
  offline) or its network is still offline. A device that left and is no longer in the device list
  counts as not recovered but doesn't keep the incident ongoing.

Likely cause (first rule that matches)
--------------------------------------
1. A network offline or Zigbee2MQTT restarted event in the incident: "z2m_down".
2. A router in the incident went down no later than the devices behind it (its parent chain in
   the last routing scan that saw them) and at least one of them followed: "router_offline",
   naming the router with the most devices behind it.
3. Power loss in one Home Assistant area: POWER_MIN_DEVICES or more mains devices in the same area
   went offline within POWER_WINDOW seconds of each other, and at least one battery device in that
   area did not drop in the incident (so the mesh was fine and the mains power wasn't): "power_loss",
   with `power` = {area, n, seconds, back_together}. A router outage (rule 2) still wins when the
   router and the devices behind it cover every mains device that dropped in that area.
4. No router explains it, and 3 or more devices dropped within NETWORK_WIDE_SECONDS of the
   incident's start through 2 or more different parents: "network_wide".
5. Otherwise "unknown".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

GAP = 600
NETWORK_WIDE_SECONDS = 60
# Power loss: mains devices in one area dropping within this many seconds of each other. Zigbee2MQTT
# notices a mains device has gone by pinging it once it has been quiet for its availability timeout
# (10 minutes by default), so devices that lost power at the same moment are marked offline up to that
# long apart. The window covers the default timeout plus a minute.
POWER_WINDOW = 660
POWER_MIN_DEVICES = 3
# All of them back within this many seconds of each other counts as "came back together".
POWER_BACK_WINDOW = 300
MIN_DEVICES = 3
MAX_INCIDENTS = 50
MAX_INCIDENT_EVENTS = 200
# Recovery events further than this after an incident's last problem event no longer join it.
RECOVERY_WINDOW = 86400

PROBLEM_KINDS = frozenset({"offline", "left", "network offline", "zigbee2mqtt restarted", "interview failed"})
NETWORK_KINDS = frozenset({"network offline", "zigbee2mqtt restarted"})
RECOVERY_KINDS = frozenset({"online", "joined", "rejoined", "added", "interview successful"})
NETWORK_RECOVERY = "network online"
NETWORK_IEEE = "network"


def _ts(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _parent(record: dict | None) -> str | None:
    return (((record or {}).get("routing") or {}).get("parent") or {}).get("ieee")


def _chain(ieee: str, devices: dict[str, dict]) -> list[str]:
    """The device's parents up the last routing scans, nearest first, without loops."""
    out: list[str] = []
    seen = {ieee}
    parent = _parent(devices.get(ieee))
    while parent and parent not in seen and len(out) < 16:
        out.append(parent)
        seen.add(parent)
        parent = _parent(devices.get(parent))
    return out


def _still_down(ieee: str, devices: dict[str, dict]) -> bool:
    record = devices.get(ieee) or {}
    return record.get("status") == "active" and record.get("availability") == "offline"


def _back_now(ieee: str, devices: dict[str, dict]) -> bool:
    record = devices.get(ieee) or {}
    return record.get("status") == "active" and record.get("availability") != "offline"


def is_battery(record: dict) -> bool:
    return (
        record.get("power_source") == "Battery"
        or isinstance(record.get("battery"), (int, float))
        or bool((record.get("battery_info") or {}).get("type"))
    )


def is_mains(record: dict) -> bool:
    if record.get("type") == "Router":
        return True
    source = str(record.get("power_source") or "")
    return bool(source) and source not in ("Battery", "Unknown") and not is_battery(record)


def _power_loss(inc: dict, devices: dict[str, dict]) -> dict | None:
    """The area where POWER_MIN_DEVICES+ mains devices dropped together while its battery devices stayed up."""
    members = inc["devices"]
    by_area: dict[str, list[tuple[float, str]]] = {}
    for ieee, member in members.items():
        record = devices.get(ieee) or {}
        area = record.get("area")
        if area and "offline" in member["kinds"] and is_mains(record):
            by_area.setdefault(str(area), []).append((member["first"], ieee))
    best: dict | None = None
    for area, drops in by_area.items():
        if len(drops) < POWER_MIN_DEVICES:
            continue
        drops.sort()
        cluster: list[tuple[float, str]] = []
        start = 0
        for end in range(len(drops)):
            while drops[end][0] - drops[start][0] > POWER_WINDOW:
                start += 1
            if end - start + 1 > len(cluster):
                cluster = drops[start : end + 1]
        if len(cluster) < POWER_MIN_DEVICES:
            continue
        batteries = [
            ieee
            for ieee, record in devices.items()
            if record.get("area") == area
            and record.get("status") == "active"
            and record.get("type") != "Coordinator"
            and is_battery(record)
            and "offline" not in (members.get(ieee) or {}).get("kinds", [])
        ]
        if not batteries:
            continue
        backs = [members[ieee].get("back_ts") for _ts, ieee in cluster]
        together = all(b is not None for b in backs) and max(backs) - min(backs) <= POWER_BACK_WINDOW
        found = {
            "area": area,
            "n": len(cluster),
            "seconds": round(cluster[-1][0] - cluster[0][0]),
            "back_together": together,
            "ieees": [ieee for _ts, ieee in cluster],
        }
        if best is None or found["n"] > best["n"]:
            best = found
    return best


def _cause(inc: dict, devices: dict[str, dict]) -> tuple[str, dict | None, dict | None]:
    if inc["network_level"]:
        return "z2m_down", None, None
    members = inc["devices"]
    routers = [i for i in members if (devices.get(i) or {}).get("type") == "Router"]
    best: tuple[int, str] | None = None
    for router in routers:
        followers = [
            i
            for i in members
            if i != router and router in _chain(i, devices) and members[i]["first"] >= members[router]["first"]
        ]
        if followers and (best is None or len(followers) > best[0]):
            best = (len(followers), router)
    power = _power_loss(inc, devices)
    if best:
        router = best[1]
        covered = {router} | {i for i in members if i != router and router in _chain(i, devices)}
        if power is None or set(power["ieees"]) <= covered:
            return "router_offline", {"ieee": router, "name": members[router]["name"]}, None
    if power:
        power.pop("ieees")
        return "power_loss", None, power
    early = [i for i, m in members.items() if m["first"] - inc["start_ts"] <= NETWORK_WIDE_SECONDS]
    parents = {_parent(devices.get(i)) for i in early}
    if len(early) >= MIN_DEVICES and len(parents) >= 2:
        return "network_wide", None, None
    return "unknown", None, None


def build_incidents(
    events: list[dict],
    devices: dict[str, dict] | None = None,
    bridges: dict[str, dict] | None = None,
    gap: float = GAP,
    limit: int = MAX_INCIDENTS,
) -> list[dict]:
    """Incidents from the events list, newest first, at most `limit`. See the module docstring for the rules."""
    devices = devices or {}
    bridges = bridges or {}
    timed = []
    for index, event in enumerate(events or []):
        if not isinstance(event, dict):
            continue
        ts = _ts(event.get("time"))
        if ts is not None:
            timed.append((ts, index, event))
    timed.sort(key=lambda item: (item[0], item[1]))

    candidates: list[dict] = []
    open_by_net: dict[str, dict] = {}
    recent_by_net: dict[str, list[dict]] = {}

    for ts, _index, event in timed:
        kind = str(event.get("kind") or "")
        net = str(event.get("instance") or "")
        ieee = str(event.get("ieee") or "")
        if kind in PROBLEM_KINDS:
            inc = open_by_net.get(net)
            if inc is None or ts - inc["last_problem_ts"] > gap:
                inc = {
                    "instance": net,
                    "start_ts": ts,
                    "start": event.get("time"),
                    "last_problem_ts": ts,
                    "devices": {},
                    "network_level": False,
                    "network_down": False,
                    "events": [],
                    "events_total": 0,
                    "last": event.get("time"),
                }
                open_by_net[net] = inc
                candidates.append(inc)
                recent_by_net.setdefault(net, []).append(inc)
            inc["last_problem_ts"] = ts
            if kind in NETWORK_KINDS or ieee == NETWORK_IEEE:
                inc["network_level"] = True
                if kind == "network offline":
                    inc["network_down"] = True
            else:
                member = inc["devices"].setdefault(
                    ieee, {"name": event.get("name"), "first": ts, "kinds": [], "down": True}
                )
                if kind not in member["kinds"]:
                    member["kinds"].append(kind)
                member["down"] = True
                member.pop("back_ts", None)
                member["name"] = member["name"] or event.get("name")
            _add(inc, event)
        elif kind == NETWORK_RECOVERY:
            for inc in reversed(recent_by_net.get(net) or []):
                if inc["network_down"] and ts - inc["last_problem_ts"] <= RECOVERY_WINDOW:
                    inc["network_down"] = False
                    _add(inc, event)
                    break
        elif kind in RECOVERY_KINDS:
            for inc in reversed(recent_by_net.get(net) or []):
                member = inc["devices"].get(ieee)
                if member and member["down"] and ts - inc["last_problem_ts"] <= RECOVERY_WINDOW:
                    member["down"] = False
                    member["back_ts"] = ts
                    _add(inc, event)
                    break
        # Old candidates can no longer take recovery events; drop them so the lookups stay short.
        recent = recent_by_net.get(net)
        if recent and len(recent) > 1 and ts - recent[0]["last_problem_ts"] > RECOVERY_WINDOW:
            recent.pop(0)

    kept = [inc for inc in candidates if inc["network_level"] or len(inc["devices"]) >= MIN_DEVICES]
    return [_finish(inc, devices, bridges) for inc in reversed(kept[-limit:])] if limit > 0 else []


def _add(inc: dict, event: dict) -> None:
    inc["events_total"] += 1
    inc["last"] = event.get("time")
    if len(inc["events"]) < MAX_INCIDENT_EVENTS:
        inc["events"].append(
            {k: event.get(k) for k in ("time", "ieee", "name", "kind", "detail", "instance") if k in event}
        )


def _finish(inc: dict, devices: dict[str, dict], bridges: dict[str, dict]) -> dict:
    members = inc["devices"]
    rows = []
    for ieee, member in sorted(members.items(), key=lambda item: item[1]["first"]):
        back = not member["down"] or _back_now(ieee, devices)
        record = devices.get(ieee) or {}
        rows.append(
            {
                "ieee": ieee,
                "name": member["name"] or record.get("friendly_name") or ieee,
                "kinds": member["kinds"],
                "back": back,
            }
        )
    network_down = inc["network_down"] and (bridges.get(inc["instance"]) or {}).get("state") == "offline"
    ongoing = network_down or any(not row["back"] and _still_down(row["ieee"], devices) for row in rows)
    cause, router, power = _cause(inc, devices)
    return {
        "id": f"{inc['instance']}|{inc['start']}",
        "instance": inc["instance"],
        "start": inc["start"],
        "end": None if ongoing else inc["last"],
        "last": inc["last"],
        "ongoing": ongoing,
        "cause": cause,
        "router": router,
        "power": power,
        "network_level": inc["network_level"],
        "affected": len(rows),
        "recovered": sum(1 for row in rows if row["back"]),
        "devices": rows,
        "events": inc["events"],
        "events_total": inc["events_total"],
    }
