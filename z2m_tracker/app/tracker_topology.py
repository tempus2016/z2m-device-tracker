"""Network maps: scans, routing history, single points of failure, router placement.

Part of Tracker (tracker.py): these methods use its lock, data and metrics.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from health import (
    FLAP_LEAVES,
    FLAP_OUTAGES,
    HOP_CHANGES,
    HOP_DAYS,
)
from route_whatif import what_if
from router_placement import rank_areas
from tracker_base import (
    MAX_DETAIL,
    STATUS_ACTIVE,
    _clean_scans,
    _display_name,
    _dumps,
    _finite,
    _ieee,
    _now,
    _text,
    _write_atomic,
    hop_summary,
    parent_rows,
)

_LOGGER = logging.getLogger("z2m_tracker")


class TopologyMixin:
    def _ingest_networkmap(self, base_topic: str, payload: dict) -> None:
        now = _now()
        status = payload.get("status")
        info = {"scanned_at": now, "status": _text(status, 40), "error": _text(payload.get("error"), MAX_DETAIL)}
        data = payload.get("data")
        value = data.get("value") if status == "ok" and isinstance(data, dict) else None
        if not isinstance(value, dict):
            self.data["networkmap"][base_topic] = info
            self._changed(structural=True)
            return
        links = [link for link in value.get("links") or [] if isinstance(link, dict)]
        neighbours: dict[str, list[dict]] = {}
        parents: dict[str, dict] = {}
        children: dict[str, int] = {}
        depth: dict[str, Any] = {}

        def addr(side: Any, fallback: Any) -> str:
            if isinstance(side, dict):
                return _ieee(side.get("ieeeAddr")) or ""
            return _ieee(fallback) or ""

        for link in links:
            source = addr(link.get("source"), link.get("sourceIeeeAddr"))
            target = addr(link.get("target"), link.get("targetIeeeAddr"))
            if not source or not target:
                continue
            lqi = link.get("lqi", link.get("linkquality"))
            # LQI is a number from 0 to 255; anything else (text, NaN) is dropped before it's stored.
            lqi = lqi if _finite(lqi) else None
            relationship = link.get("relationship")
            neighbours.setdefault(source, []).append({"ieee": target, "lqi": lqi, "rel": relationship})
            neighbours.setdefault(target, []).append({"ieee": source, "lqi": lqi, "rel": relationship})
            if relationship == 1:
                current = parents.get(source)
                if current is None or (lqi or 0) > (current.get("lqi") or 0):
                    parents[source] = {"ieee": target, "lqi": lqi}
                children[target] = children.get(target, 0) + 1
            if _finite(link.get("depth")):
                depth[source] = link.get("depth")

        for node in value.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            ieee = _ieee(node.get("ieeeAddr")) or ""
            record = self.data["devices"].get(ieee)
            if record is None:
                continue
            nbrs = {}
            for entry in neighbours.get(ieee, []):
                existing = nbrs.get(entry["ieee"])
                if existing is None or (entry["lqi"] or 0) > (existing["lqi"] or 0):
                    nbrs[entry["ieee"]] = entry
            record["routing"] = {
                "scanned_at": now,
                "parent": parents.get(ieee),
                "children": children.get(ieee, 0),
                "depth": depth.get(ieee),
                "neighbours": sorted(nbrs.values(), key=lambda e: -(e["lqi"] or 0))[:12],
            }
        info["nodes"] = len(value.get("nodes") or [])
        info["links"] = len(links)
        self._record_scan(base_topic, now, parents, neighbours)
        self.data["networkmap"][base_topic] = info
        self._add_event(
            now, base_topic, "network", "Network", "routing scanned", f"{info['nodes']} nodes, {info['links']} links"
        )
        self._changed(structural=True)

    # ------------------------------------------------------------ scan history

    def load_scans(self, path: str) -> None:
        self.scans_path = path
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self.scans = {topic: _clean_scans(scans) for topic, scans in data.items() if isinstance(scans, list)}
        except (OSError, ValueError):
            self.scans = {}

    def _save_scans(self) -> None:
        if not self.scans_path:
            return
        _write_atomic(self.scans_path, _dumps(self.scans, separators=(",", ":")))

    def _record_scan(self, base_topic: str, when: str, parents: dict, neighbours: dict) -> None:
        devices = self.data["devices"]
        # Only devices the tracker knows: a made-up response can't fill scans.json with strangers.
        record_parents = {
            ieee: [p["ieee"], p.get("lqi")] for ieee, p in parents.items() if ieee in devices and p["ieee"] in devices
        }
        links: dict[str, list] = {}
        for ieee, nbrs in neighbours.items():
            dev = self.data["devices"].get(ieee)
            if not dev or dev.get("type") not in ("Router", "Coordinator"):
                continue
            routers = [
                n for n in nbrs if (self.data["devices"].get(n["ieee"]) or {}).get("type") in ("Router", "Coordinator")
            ]
            for n in sorted(routers, key=lambda x: -(x.get("lqi") or 0))[:4]:
                key = "|".join(sorted([ieee, n["ieee"]]))
                if key not in links or (n.get("lqi") or 0) > (links[key][2] or 0):
                    links[key] = [ieee, n["ieee"], n.get("lqi")]
        scans = self.scans.setdefault(base_topic, [])
        scans.append({"at": when, "parents": record_parents, "links": list(links.values())})
        del scans[: max(0, len(scans) - 40)]
        try:
            self._save_scans()
        except OSError as err:
            _LOGGER.warning("Saving scan history failed: %s", err)

    def scan_history(self, base_topic: str) -> list:
        with self.lock:
            return list(self.scans.get(base_topic) or [])

    def _hop_rows(self, record: dict, now: float, days: int = HOP_DAYS) -> list[dict]:
        since = datetime.fromtimestamp(now - days * 86400, tz=timezone.utc).isoformat(timespec="seconds")
        return parent_rows(self.scans.get(record.get("instance") or "") or [], record["ieee"], since)

    def parent_history(self, ieee: str, days: int = HOP_DAYS) -> dict | None:
        """Parent per routing scan over the last `days`, newest first, for the device panel's History tab."""
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None:
                return None
            rows = self._hop_rows(record, time.time(), days)

            def name(parent: str) -> str:
                return _display_name(self.data["devices"].get(parent) or {}) or parent

            summary = hop_summary(rows)
            if summary["best"]:
                summary["best"]["name"] = name(summary["best"]["ieee"])
            return {
                "days": days,
                "threshold": HOP_CHANGES,
                "scans": [{**r, "name": name(r["ieee"])} for r in reversed(rows)],
                **summary,
            }

    def single_points(self, base_topic: str) -> list[dict]:
        """Routers that are the only path to the coordinator for other routers, from the latest scan."""
        scanned_at = (self.data.get("networkmap") or {}).get(base_topic, {}).get("scanned_at")
        if not scanned_at:
            return []
        cached = self._spof_cache.get(base_topic)
        if cached and cached[0] == scanned_at and cached[1] == self.revision:
            return cached[2]
        records = self.data["devices"]
        nodes = {
            ieee
            for ieee, r in records.items()
            if r.get("instance") == base_topic
            and r["status"] == STATUS_ACTIVE
            and r.get("type") in ("Router", "Coordinator")
            and (r.get("routing") or {}).get("scanned_at") == scanned_at
        }
        coordinator = next((i for i in nodes if records[i].get("type") == "Coordinator"), None)
        result: list[dict] = []
        if coordinator:
            edges: dict[str, set[str]] = {i: set() for i in nodes}
            for ieee in nodes:
                for n in (records[ieee].get("routing") or {}).get("neighbours") or []:
                    other = n.get("ieee")
                    if other in nodes and other != ieee and (n.get("lqi") or 0) > 0:
                        edges[ieee].add(other)
                        edges[other].add(ieee)

            def reach(skip: str | None) -> set[str]:
                seen = {coordinator}
                stack = [coordinator]
                while stack:
                    for other in edges[stack.pop()]:
                        if other != skip and other not in seen:
                            seen.add(other)
                            stack.append(other)
                return seen

            connected = reach(None)
            ends_by_parent: dict[str, list[str]] = {}
            for ieee, r in records.items():
                parent = ((r.get("routing") or {}).get("parent") or {}).get("ieee")
                if (
                    r.get("instance") == base_topic
                    and r["status"] == STATUS_ACTIVE
                    and r.get("type") == "EndDevice"
                    and parent
                    and (r.get("routing") or {}).get("scanned_at") == scanned_at
                ):
                    ends_by_parent.setdefault(parent, []).append(ieee)
            for router in sorted(connected - {coordinator}):
                cut = connected - reach(router) - {router}
                if not cut:
                    continue
                behind = [e for p in cut for e in ends_by_parent.get(p, [])]
                result.append(
                    {
                        "ieee": router,
                        "name": _display_name(records[router]) or router,
                        "routers": sorted(
                            ({"ieee": i, "name": _display_name(records[i]) or i} for i in cut),
                            key=lambda x: x["name"].lower(),
                        ),
                        "devices": len(behind),
                        "device_list": sorted(behind),
                        "children": len(ends_by_parent.get(router, [])),
                    }
                )
            result.sort(key=lambda x: -(len(x["routers"]) + x["devices"]))
        self._spof_cache[base_topic] = (scanned_at, self.revision, result)
        return result

    # ----------------------------------------------------- router what-if

    def _scan_nodes(self, base_topic: str) -> tuple[dict[str, dict], str | None, str | None]:
        """Active devices in the latest routing scan of a network, shaped for route_whatif, plus the coordinator."""
        scanned_at = (self.data.get("networkmap") or {}).get(base_topic, {}).get("scanned_at")
        nodes: dict[str, dict] = {}
        coordinator = None
        if not scanned_at:
            return nodes, None, None
        for ieee, record in self.data["devices"].items():
            routing = record.get("routing") or {}
            if (
                record.get("instance") != base_topic
                or record["status"] != STATUS_ACTIVE
                or routing.get("scanned_at") != scanned_at
                or record.get("type") not in ("Coordinator", "Router", "EndDevice")
            ):
                continue
            nodes[ieee] = {
                "type": record["type"],
                "name": _display_name(record) or ieee,
                "parent": (routing.get("parent") or {}).get("ieee"),
                "neighbours": {n["ieee"]: n.get("lqi") for n in routing.get("neighbours") or [] if n.get("ieee")},
            }
            if record["type"] == "Coordinator":
                coordinator = ieee
        return nodes, coordinator, scanned_at

    def router_what_if(self, base_topic: str, ieee: str) -> dict | None:
        """What the latest scan says happens if router `ieee` goes. None if it isn't a router in that scan."""
        with self.lock:
            nodes, coordinator, scanned_at = self._scan_nodes(base_topic)
            if not coordinator or (nodes.get(ieee) or {}).get("type") != "Router":
                return None
            return {"net": base_topic, "scanned_at": scanned_at, **what_if(nodes, coordinator, ieee)}

    def router_placement(self, base_topic: str, now: float) -> dict:
        """Areas where an extra plug-in router would help most (see router_placement.rank_areas)."""
        scanned_at = (self.data.get("networkmap") or {}).get(base_topic, {}).get("scanned_at")
        cached = self._placement_cache.get(base_topic)
        if cached and cached[0] == scanned_at and cached[1] == self.revision:
            return cached[2]
        nodes, coordinator, _ = self._scan_nodes(base_topic)
        # Devices that would have no other router in range if one particular router went.
        no_backup: set[str] = set()
        if coordinator:
            for ieee, node in nodes.items():
                if node["type"] == "Router":
                    no_backup.update(o["ieee"] for o in what_if(nodes, coordinator, ieee)["orphans"])
        day_ago = datetime.fromtimestamp(now - 86400, tz=timezone.utc).isoformat(timespec="seconds")
        leaves = self._leaves_since(day_ago)
        rows = []
        for ieee, record in self.data["devices"].items():
            if (
                record.get("instance") != base_topic
                or record["status"] != STATUS_ACTIVE
                or record.get("type") not in ("Router", "EndDevice")
            ):
                continue
            node = nodes.get(ieee)
            link_lqi = None
            if node and record["type"] == "EndDevice":
                link_lqi = ((record.get("routing") or {}).get("parent") or {}).get("lqi")
            elif node:
                heard = [
                    lqi
                    for other, lqi in node["neighbours"].items()
                    if isinstance(lqi, (int, float))
                    and (nodes.get(other) or {}).get("type") in ("Router", "Coordinator")
                ]
                link_lqi = max(heard) if heard else None
            outages = sum(1 for o in record.get("outages") or [] if (o.get("start") or "") >= day_ago)
            rows.append(
                {
                    "ieee": ieee,
                    "name": _display_name(record) or ieee,
                    "area": record.get("area"),
                    "type": record["type"],
                    "link_lqi": link_lqi,
                    "hops": hop_summary(self._hop_rows(record, now))["changes"] if record["type"] == "EndDevice" else 0,
                    "offline": record.get("availability") == "offline",
                    "flapping": outages >= FLAP_OUTAGES or leaves.get(ieee, 0) >= FLAP_LEAVES,
                    "no_backup": ieee in no_backup,
                }
            )
        result = rank_areas(rows)
        self._placement_cache[base_topic] = (scanned_at, self.revision, result)
        return result
