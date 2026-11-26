"""Replacing a device: carrying bindings, names and history over to its successor.

Part of Tracker (tracker.py): these methods use its lock, data and metrics.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from tracker_base import (
    MAX_TASKS,
    STATUS_ACTIVE,
    _display_name,
    _now,
    friendly_z2m_error,
)

_LOGGER = logging.getLogger("z2m_tracker")


class ReplaceMixin:
    # ------------------------------------------------------------ replacement

    def replacement_bindings(self, ieee: str) -> list[dict]:
        """The old device's last known bindings worth re-creating (not the ones Z2M sets up to the coordinator)."""
        with self.lock:
            record = self.data["devices"].get(ieee) or {}
            known = list(record.get("bindings") or [])
            keys = {b["key"] for b in known}
            known += [b for b in record.get("lost_bindings") or [] if b.get("key") not in keys]
            return [b for b in known if not self._is_coordinator_target(b)]

    def replace_problem(self, old_ieee: str, new_ieee: str) -> str | None:
        with self.lock:
            old = self.data["devices"].get(old_ieee)
            new = self.data["devices"].get(new_ieee)
            if old is None or new is None:
                return "Both devices must be known to the tracker"
            if old_ieee == new_ieee:
                return "Choose a different device as the replacement"
            if "Coordinator" in (old.get("type"), new.get("type")):
                return "The coordinator can't be replaced here"
            if new["status"] != STATUS_ACTIVE:
                return "The new device must be in Zigbee2MQTT"
        return None

    def remove_from_z2m(self, ieee: str, leave: bool = False) -> dict:
        """Remove a device from Zigbee2MQTT. The tracker keeps its history.

        leave asks a working device to leave the network first, so it doesn't stay paired; a device that
        doesn't answer is then force-removed, which only drops it from Zigbee2MQTT's database.
        """
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None or record["status"] != STATUS_ACTIVE:
                return {"ok": True, "skipped": True, "forced": False}
            base_topic = record["instance"]
        if leave:
            result = self.request(base_topic, "device/remove", {"id": ieee, "force": False}, timeout=30)
            if result and result.get("status") == "ok":
                return {"ok": True, "error": None, "forced": False}
        result = self.request(base_topic, "device/remove", {"id": ieee, "force": True}, timeout=30)
        ok = bool(result and result.get("status") == "ok")
        error = None if ok else friendly_z2m_error((result or {}).get("error") or "No response")
        return {"ok": ok, "error": error, "forced": ok}

    def old_name_for(self, ieee: str) -> str | None:
        """A free name for a replaced device that stays in Zigbee2MQTT: "<name> (old)", numbered if taken."""
        with self.lock:
            record = self.data["devices"].get(ieee) or {}
            name = record.get("friendly_name")
            if not name:
                return None
            taken = {
                str(r.get("friendly_name") or "").lower()
                for k, r in self.data["devices"].items()
                if k != ieee and r.get("status") == STATUS_ACTIVE and r.get("instance") == record.get("instance")
            }
        for n in range(1, 100):
            candidate = f"{name} (old)" if n == 1 else f"{name} (old {n})"
            if candidate.lower() not in taken:
                return candidate
        return None

    def rebind(self, old_ieee: str, new_ieee: str) -> dict:
        """Re-create the old device's bindings on the new one through bridge/request/device/bind."""
        wanted = self.replacement_bindings(old_ieee)
        with self.lock:
            new = self.data["devices"].get(new_ieee)
            if new is None or new["status"] != STATUS_ACTIVE:
                return {"ok": False, "error": "The new device isn't in Zigbee2MQTT", "done": 0, "failed": []}
            base_topic = new["instance"]
            old_model = (self.data["devices"].get(old_ieee) or {}).get("model")
            # Another model may number its endpoints differently: bind from the endpoint that has the cluster.
            fit = self.endpoint_clusters.get(new_ieee) if old_model != new.get("model") else None
            unfit = 0
            version = str(((self.data.get("bridges") or {}).get(base_topic) or {}).get("info", {}).get("version") or "")
            groups = dict(self.groups.get(base_topic) or {})
            requests: dict[tuple[str, str], dict] = {}
            for binding in wanted:
                target = binding.get("target") or ""
                if target.startswith("group:"):
                    gid = target.split(":", 1)[1]
                    to, to_ep = groups.get(gid) or (int(gid) if gid.isdigit() else gid), None
                else:
                    ieee, _, ep = target.partition("/")
                    rec = self.data["devices"].get(ieee)
                    if rec is None or rec["status"] != STATUS_ACTIVE or rec.get("instance") != base_topic:
                        continue
                    to, to_ep = ieee, ep if ep.isdigit() else None
                from_ep = binding.get("ep") or ""
                if fit:
                    cluster = binding.get("cluster")
                    if cluster not in fit.get(from_ep, set()):
                        having = [e for e, clusters in fit.items() if cluster in clusters]
                        if len(having) != 1:
                            # None, or several (a multi-gang switch): a guess could bind the wrong gang.
                            unfit += 1
                            continue
                        from_ep = having[0]
                entry = requests.setdefault(
                    (from_ep, target),
                    {"ep": from_ep, "to": to, "to_ep": to_ep, "clusters": [], "labels": []},
                )
                entry["clusters"].append(binding.get("cluster"))
                entry["labels"].append(binding.get("label") or self.binding_label(base_topic, binding))
        skipped = len(wanted) - sum(len(r["clusters"]) for r in requests.values()) - unfit
        legacy = version[:1].isdigit() and int(version.split(".", 1)[0]) < 2
        done, failed = 0, []
        for entry in list(requests.values())[:12]:
            ep = entry["ep"]
            if legacy:
                payload: dict[str, Any] = {
                    "from": f"{new_ieee}/{ep}" if ep.isdigit() else new_ieee,
                    "to": f"{entry['to']}/{entry['to_ep']}" if entry["to_ep"] else entry["to"],
                }
            else:
                payload = {"from": new_ieee, "to": entry["to"]}
                if ep.isdigit():
                    payload["from_endpoint"] = int(ep)
                if entry["to_ep"]:
                    payload["to_endpoint"] = int(entry["to_ep"])
            payload["clusters"] = entry["clusters"]
            result = self.request(base_topic, "device/bind", payload, timeout=20)
            if result and result.get("status") == "ok":
                done += len(entry["clusters"])
            else:
                failed.append(
                    {
                        "labels": entry["labels"],
                        "error": friendly_z2m_error((result or {}).get("error") or "No response"),
                    }
                )
        return {"ok": not failed, "done": done, "failed": failed, "skipped": skipped, "unfit": unfit}

    def carry_over(self, old_ieee: str, new_ieee: str, notes: bool, battery: bool, keep_old: bool = False) -> list[str]:
        """Copy notes, asset details, the battery record and the critical mark. Returns what was copied.

        keep_old: the old device stays in use, so its maintenance tasks are copied rather than moved.
        """
        copied = []
        with self.lock:
            old = self.data["devices"].get(old_ieee)
            new = self.data["devices"].get(new_ieee)
            if old is None or new is None:
                return copied
            if notes:
                if old.get("note"):
                    mine = (new.get("note") or "").strip()
                    new["note"] = old["note"] if not mine or mine == old["note"] else f"{mine}\n\n{old['note']}"
                    copied.append("note")
                asset = {k: v for k, v in (old.get("asset") or {}).items() if k != "serial"}
                if asset:
                    new["asset"] = {**asset, **(new.get("asset") or {})}
                    copied.append("asset")
                # Maintenance belongs to the spot, not the hardware: the new device takes over the jobs.
                tasks = [t for t in old.get("maintenance") or [] if isinstance(t, dict)]
                if tasks:
                    mine = list(new.get("maintenance") or [])
                    titles = {str(t.get("title") or "").lower() for t in mine}
                    for task in tasks:
                        if len(mine) >= MAX_TASKS or str(task.get("title") or "").lower() in titles:
                            continue
                        mine.append({**task, "id": uuid.uuid4().hex[:8]})
                    new["maintenance"] = mine
                    if not keep_old:
                        old["maintenance"] = []
                    copied.append("maintenance")
            # How often the spot reports was learned on the old device: the new one adds it to what it has.
            self.metrics.merge_rhythm(old_ieee, new_ieee)
            if battery and old.get("battery_info"):
                info = json.loads(json.dumps(old["battery_info"]))
                for entry in info.get("replacements") or []:
                    # The old device's changes already took from stock; removing the copy mustn't refund it.
                    entry.pop("stock_used", None)
                    entry.pop("stock_type", None)
                new["battery_info"] = {**(new.get("battery_info") or {}), **info}
                copied.append("battery")
            if old.get("critical"):
                # The replacement does the same job, so it stays critical.
                new["critical"] = True
                copied.append("critical")
            self._changed(structural=True)
        return copied

    def link_replacement(self, old_ieee: str, new_ieee: str, carried: list[str], new_name: str | None = None) -> bool:
        """Join both histories: "replaced" on the new device, "replaced by" on the old one."""
        with self.lock:
            old = self.data["devices"].get(old_ieee)
            new = self.data["devices"].get(new_ieee)
            if old is None or new is None or old_ieee == new_ieee:
                return False
            if old_ieee not in new.setdefault("predecessors", []):
                new["predecessors"].append(old_ieee)
            old["successor"] = new_ieee
            now = _now()
            old_name = _display_name(old) or old_ieee
            new_name = new_name or _display_name(new) or new_ieee
            extra = f". Carried over: {', '.join(carried)}" if carried else ""
            self._add_event(now, new["instance"], new_ieee, new_name, "replaced", f"{old_name} ({old_ieee}){extra}")
            self.data["events"][-1]["ref"] = old_ieee
            self._add_event(
                now, old["instance"] or "unknown", old_ieee, old_name, "replaced by", f"{new_name} ({new_ieee})"
            )
            self.data["events"][-1]["ref"] = new_ieee
            self._changed(structural=True)
        self._health_changed()
        return True
