"""What happens to the mesh if one router goes away, worked out from the last routing scan.

Pure functions, no tracker state. `nodes` maps IEEE address to a dict with:
  type        "Coordinator", "Router" or "EndDevice"
  name        display name (optional)
  parent      IEEE of the parent in the scan, or None
  neighbours  {ieee: lqi} heard in the scan (either side of the link may have reported it)
"""

from __future__ import annotations

from typing import Any

ROUTING = ("Router", "Coordinator")


def hearing(nodes: dict[str, dict]) -> dict[str, dict[str, int]]:
    """Best LQI between every pair of scanned nodes that heard each other, in both directions."""
    out: dict[str, dict[str, int]] = {ieee: {} for ieee in nodes}
    for ieee, node in nodes.items():
        for other, lqi in (node.get("neighbours") or {}).items():
            if other == ieee or other not in nodes or not isinstance(lqi, (int, float)) or lqi <= 0:
                continue
            for a, b in ((ieee, other), (other, ieee)):
                if lqi > out[a].get(b, 0):
                    out[a][b] = lqi
    return out


def reachable(nodes: dict[str, dict], hear: dict[str, dict[str, int]], coordinator: str, skip: str | None) -> set[str]:
    """Routers (and the coordinator) that can still reach the coordinator through other routers."""
    seen = {coordinator}
    stack = [coordinator]
    while stack:
        for other in hear.get(stack.pop(), {}):
            if other != skip and other not in seen and (nodes.get(other) or {}).get("type") in ROUTING:
                seen.add(other)
                stack.append(other)
    return seen


def what_if(nodes: dict[str, dict], coordinator: str, removed: str) -> dict[str, Any]:
    """Devices that lose their path when `removed` goes, where each one most likely goes, and which have nowhere.

    A device loses its path when its parent is the removed router, or when its parent is a router that
    can no longer reach the coordinator. Routers cut off from the coordinator are orphaned with their
    children. Every other device picks the strongest router or coordinator it heard in the scan that
    still reaches the coordinator. A router that heard none is orphaned. An end device that heard none
    goes in `rejoin`: sleepy end devices don't answer the scan, so it only ever shows the link to their
    parent, never the other routers they can hear. It will look for a new parent itself.
    """
    hear = hearing(nodes)
    before = reachable(nodes, hear, coordinator, None)
    after = reachable(nodes, hear, coordinator, removed)
    cut = {ieee for ieee in before - after if ieee != removed and nodes[ieee].get("type") == "Router"}

    def name(ieee: str) -> str:
        return (nodes.get(ieee) or {}).get("name") or ieee

    affected: dict[str, str] = {}
    for ieee, node in nodes.items():
        if ieee in (removed, coordinator):
            continue
        parent = node.get("parent")
        if ieee in cut:
            affected[ieee] = "cut_off"
        elif parent == removed:
            affected[ieee] = "child"
        elif parent in cut and node.get("type") == "EndDevice":
            affected[ieee] = "behind"

    moves: list[dict] = []
    orphans: list[dict] = []
    rejoin: list[dict] = []
    for ieee, reason in affected.items():
        node = nodes[ieee]
        base = {
            "ieee": ieee,
            "name": name(ieee),
            "type": node.get("type"),
            "reason": reason,
            "from": node.get("parent"),
        }
        options = [
            (lqi, other)
            for other, lqi in hear[ieee].items()
            if other != removed and other in after and other not in cut and other != ieee
        ]
        if reason == "cut_off" or (not options and node.get("type") != "EndDevice"):
            orphans.append(base)
            continue
        if not options:
            rejoin.append(base)
            continue
        # Strongest link wins; on a tie the coordinator, then the name, so the answer is stable.
        lqi, best = min(options, key=lambda o: (-o[0], o[1] != coordinator, name(o[1]).lower()))
        moves.append({**base, "to": best, "to_name": name(best), "lqi": lqi})

    moves.sort(key=lambda m: (m["to_name"].lower(), m["name"].lower()))
    orphans.sort(key=lambda o: o["name"].lower())
    rejoin.sort(key=lambda o: o["name"].lower())
    targets: dict[str, dict] = {}
    for m in moves:
        entry = targets.setdefault(m["to"], {"ieee": m["to"], "name": m["to_name"], "count": 0})
        entry["count"] += 1
    return {
        "ieee": removed,
        "name": name(removed),
        "moves": moves,
        "orphans": orphans,
        "rejoin": rejoin,
        "targets": sorted(targets.values(), key=lambda x: (-x["count"], x["name"].lower())),
    }
