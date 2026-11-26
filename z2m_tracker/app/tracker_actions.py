"""Requests to Zigbee2MQTT: reconfigure, scans, pings, pairing, rename, firmware updates.

Part of Tracker (tracker.py): these methods use its lock, data and metrics.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from time import monotonic as _monotonic
from typing import Any

from tracker_base import (
    PAIRING_OPEN_ALERT,
    PERMIT_HINT_SECONDS,
    PERMIT_ORIGIN_TEXT,
    PING_GRACE,
    PING_MIN_Z2M,
    PING_SPACING,
    PING_TIMEOUT,
    STATUS_ACTIVE,
    _loads,
    _now,
    _permit_opening,
    _z2m_version,
    friendly_z2m_error,
)

_LOGGER = logging.getLogger("z2m_tracker")


class ActionsMixin:
    # --------------------------------------------------------- MQTT requests

    def request(self, base_topic: str, path: str, payload: dict, timeout: float = 30.0) -> dict | None:
        if not self.publish:
            raise RuntimeError("MQTT is not connected")
        if base_topic not in self.base_topics:
            # A frozen device's network: nothing there to answer, and nothing is sent to it.
            raise RuntimeError("This device's network isn't one the app listens to")
        transaction = f"z2mt-{uuid.uuid4().hex[:10]}"
        waiter = {"event": threading.Event(), "result": None}
        with self.lock:
            self._pending[transaction] = waiter
        try:
            self.publish(f"{base_topic}/bridge/request/{path}", {**payload, "transaction": transaction})
            waiter["event"].wait(timeout)
            return waiter["result"]
        finally:
            with self.lock:
                self._pending.pop(transaction, None)

    def reconfigure(self, ieee: str) -> dict:
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None or record["status"] != STATUS_ACTIVE:
                return {"ok": False, "error": "Device is not active in Zigbee2MQTT"}
            base_topic = record["instance"]
            name = record["friendly_name"]
        result = self.request(base_topic, "device/configure", {"id": ieee}, timeout=40)
        ok = bool(result and result.get("status") == "ok")
        raw = None if ok else ((result or {}).get("error") or "No response from Zigbee2MQTT")
        error = None if ok else friendly_z2m_error(raw)
        with self.lock:
            self._add_event(
                _now(), base_topic, ieee, name, "reconfigured" if ok else "reconfigure failed", "" if ok else str(error)
            )
            if ok:
                for reading in (self.data["devices"][ieee].get("readings") or {}).values():
                    reading["reports"] = 0
            self._changed(structural=True)
        return {"ok": ok, "error": error, "detail": raw}

    def scan_networkmap(self, base_topic: str | None = None, timeout: float = 3600) -> dict:
        topics = [base_topic] if base_topic else list(self.mqtt_topics)
        results = {}
        for topic in topics:
            with self.lock:
                self.data["networkmap"].setdefault(topic, {})["running_since"] = _now()
                self._changed(structural=True)
            result = self.request(topic, "networkmap", {"type": "raw", "routes": False}, timeout=timeout)
            with self.lock:
                self.data["networkmap"].setdefault(topic, {}).pop("running_since", None)
                if result is None:
                    self.data["networkmap"][topic].update({"scanned_at": _now(), "status": "timeout"})
                self._changed(structural=True)
            results[topic] = (result or {}).get("status", "timeout")
        return results

    # ------------------------------------------------------------------- ping

    def can_ping(self, record: dict) -> str | None:
        """Why this device can't be pinged, or None if it can. Caller holds the lock."""
        if record["status"] != STATUS_ACTIVE or not record.get("instance"):
            return "Device is not active in Zigbee2MQTT"
        if record.get("type") == "Coordinator":
            return "The coordinator can't be pinged"
        if self.is_battery_device(record):
            return "Battery devices sleep, so they can't be pinged"
        if record["ieee"] not in self.addresses:
            return "Zigbee2MQTT hasn't listed this device's network address yet"
        version = _z2m_version(
            ((self.data.get("bridges") or {}).get(record["instance"]) or {}).get("info", {}).get("version")
        )
        if version is not None and version < PING_MIN_Z2M:
            return "Ping needs Zigbee2MQTT 2.7 or newer"
        return None

    def ping(self, ieee: str) -> dict:
        """Read genBasic zclVersion from a mains device and time the reply.

        Returns {"ok": True, "ms": n}, {"ok": False, "timeout": True, ...} when the device didn't answer
        within PING_TIMEOUT seconds, or {"ok": False, "error": ...} when it couldn't be asked at all.
        Read-only: nothing is written to the device and Z2M's stored state isn't touched.
        """
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None:
                return {"ok": False, "error": "Unknown device"}
            problem = self.can_ping(record)
            if problem:
                return {"ok": False, "error": problem}
            base_topic = record["instance"]
            address = dict(self.addresses[ieee])
        params = {
            "ieee_address": ieee,
            "network_address": address["nwk"],
            "dst_endpoint": address["ep"],
            "cluster_key": "genBasic",
            "zcl": {"frame_type": 0, "direction": 0, "command_key": "read", "payload": [{"attrId": 0}]},
            "timeout": PING_TIMEOUT * 1000,
        }
        started = _monotonic()
        # Z2M answers with an error once its own 10 s timeout runs out; the extra seconds cover the round trip.
        result = self.request(
            base_topic, "action", {"action": "raw", "params": params}, timeout=PING_TIMEOUT + PING_GRACE
        )
        ms = round((_monotonic() - started) * 1000)
        if result is None:
            return {"ok": False, "timeout": True, "seconds": PING_TIMEOUT, "detail": "No response from Zigbee2MQTT"}
        if result.get("status") == "ok":
            return {"ok": True, "ms": ms}
        raw = str(result.get("error") or "")
        if raw.startswith("Invalid") or "assert" in raw.lower():
            return {"ok": False, "error": f"Zigbee2MQTT refused the ping: {raw[:200]}"}
        return {"ok": False, "timeout": True, "seconds": PING_TIMEOUT, "detail": raw[:300]}

    def ping_routers(self, wait: Callable[[float], Any]) -> dict[str, int]:
        """Nightly: ping every active, online, mains router once, one at a time, and store the reply time.

        `wait(seconds)` sleeps between pings and returns True to stop early (main.py passes stop_event.wait).
        """
        with self.lock:
            targets = [
                r["ieee"]
                for r in self.data["devices"].values()
                if r.get("type") == "Router" and r.get("availability") != "offline" and self.can_ping(r) is None
            ]
        counts = {"replied": 0, "no_reply": 0, "skipped": 0}
        for index, ieee in enumerate(targets):
            if index and wait(PING_SPACING):
                break
            result = self.ping(ieee)
            if result.get("ok"):
                self.metrics.add_ping(ieee, result["ms"])
                counts["replied"] += 1
            elif result.get("timeout"):
                self.metrics.add_ping(ieee, None)
                counts["no_reply"] += 1
            else:
                counts["skipped"] += 1
        return counts

    def _handle_permit_request(self, base_topic: str, text: str) -> None:
        """Someone published bridge/request/permit_join. Remember it if it opens pairing and isn't ours."""
        try:
            payload = _loads(text)
        except ValueError:
            payload = text
        transaction = str(payload.get("transaction") or "") if isinstance(payload, dict) else ""
        if transaction in self._pending or transaction.startswith("z2mt-") or not _permit_opening(payload):
            return
        device = payload.get("device") if isinstance(payload, dict) else None
        self._permit_hint[base_topic] = {"origin": "mqtt", "at": time.time(), "device": device}

    def _permit_opened(self, base_topic: str, known_closed: bool) -> None:
        """Z2M reports pairing open and we hadn't seen it open: work out what opened it."""
        hint = self._permit_hint.pop(base_topic, None)
        if hint and time.time() - hint["at"] <= PERMIT_HINT_SECONDS:
            origin, device = hint["origin"], hint.get("device")
        else:
            # Closed a moment ago and no MQTT request: the frontend (it talks to Z2M over its websocket).
            # Open when we first hear from Z2M: we can't tell who opened it.
            origin, device = ("frontend" if known_closed else None), None
        self.permit_meta[base_topic] = {"since": time.time(), "origin": origin, "device": device}

    def _z2m_major(self, base_topic: str) -> int | None:
        version = str((self._bridge(base_topic).get("info") or {}).get("version") or "")
        match = re.match(r"(\d+)\.", version)
        return int(match.group(1)) if match else None

    def permit_payload(self, base_topic: str, seconds: int) -> dict[str, Any]:
        """bridge/request/permit_join payload: 1.x wants {"value": bool, "time"}, 2.x only {"time"} (0 closes)."""
        if self._z2m_major(base_topic) == 1:
            return {"value": True, "time": seconds} if seconds else {"value": False}
        return {"time": seconds}

    def pairing_open_issue(self, base_topic: str, now: float) -> dict | None:
        """The pairing_open network issue, or None while pairing is closed or only briefly open by us."""
        permit = self.permit.get(base_topic) or {}
        meta = self.permit_meta.get(base_topic)
        if not permit.get("on") or meta is None:
            return None
        age = max(0.0, now - meta["since"])
        origin = meta.get("origin")
        if age < PAIRING_OPEN_ALERT and origin in ("app", None):
            return None
        mins = max(1, round(age / 60))
        left = None
        if permit.get("end"):
            try:
                left = datetime.fromisoformat(permit["end"]).timestamp() - now
            except ValueError:
                left = None
            if left is not None and left <= 0:
                left = None
        label = (
            "Main"
            if base_topic == "zigbee2mqtt"
            else base_topic.replace("zigbee2mqtt_", "").replace("_", " ").capitalize()
        )
        parts = [f"Open for {mins} min"]
        if origin in PERMIT_ORIGIN_TEXT:
            parts.append(PERMIT_ORIGIN_TEXT[origin])
        if meta.get("device"):
            parts.append(f"only through {meta['device']}")
        if left is not None:
            parts.append(f"closes by itself in {max(1, round(left / 60))} min")
        accepted = (
            f"has accepted new devices for {mins} minute{'' if mins == 1 else 's'}"
            if age >= 60
            else "is accepting new devices"
        )
        return {
            "key": f"pairing_open:{base_topic}",
            "kind": "pairing_open",
            "label": "Pairing open",
            "severity": "warning",
            "ieee": f"network:{base_topic}",
            "title": "Pairing open",
            "detail": " · ".join(parts),
            "msg": "pairing_open",
            "params": {
                "seconds": int(age),
                "origin": origin,
                "device": meta.get("device"),
                "left": int(left) if left is not None else None,
            },
            "notify_title": "Zigbee pairing still open",
            "notify_message": f"{label} network {accepted}. Close it if you're not pairing anything.",
            "name": f"{label} network",
            "instance": base_topic,
        }

    def start_pairing(self, base_topic: str, seconds: int, via: str | None = None, target: str | None = None) -> dict:
        seconds = max(0, min(254, int(seconds)))
        with self.lock:
            payload = self.permit_payload(base_topic, seconds)
        via_name = None
        if via and seconds:
            with self.lock:
                router = self.data["devices"].get(via)
                if (
                    router is None
                    or router.get("instance") != base_topic
                    or router.get("type") not in ("Router", "Coordinator")
                    or router["status"] != STATUS_ACTIVE
                ):
                    return {"ok": False, "error": "Choose a router or the coordinator on this network"}
                via_name = router.get("friendly_name") or (
                    "Coordinator" if router.get("type") == "Coordinator" else via
                )
            payload["device"] = "Coordinator" if router.get("type") == "Coordinator" else via_name
        if seconds:
            with self.lock:
                self._permit_hint[base_topic] = {"origin": "app", "at": time.time(), "device": payload.get("device")}
        result = self.request(base_topic, "permit_join", payload, timeout=15)
        ok = bool(result and result.get("status") == "ok")
        with self.lock:
            if ok and seconds:
                self.pairing_sessions[base_topic] = {
                    "started": _now(),
                    "seconds": seconds,
                    "active": True,
                    "joined": [],
                    "via": via,
                    "via_name": via_name,
                    "target": target,
                    "rejoined": None,
                    "progress": {},
                }
                end = datetime.fromtimestamp(time.time() + seconds, tz=timezone.utc).isoformat(timespec="seconds")
                self.permit[base_topic] = {"on": True, "end": end}
                self.permit_meta[base_topic] = {"since": time.time(), "origin": "app", "device": payload.get("device")}
                self._permit_hint.pop(base_topic, None)
            elif ok:
                if base_topic in self.pairing_sessions:
                    self.pairing_sessions[base_topic]["active"] = False
                self.permit[base_topic] = {"on": False, "end": None}
                self.permit_meta.pop(base_topic, None)
                self._permit_hint.pop(base_topic, None)
            self.revision += 1
        return {"ok": ok, "error": None if ok else friendly_z2m_error((result or {}).get("error") or "No response")}

    def rename(self, ieee: str, new_name: str) -> dict:
        new_name = (new_name or "").strip()
        if not new_name or len(new_name) > 120 or new_name.endswith("/") or re.search(r"[+#]", new_name):
            return {"ok": False, "error": "Enter a name without + or # and no trailing slash"}
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None or record["status"] != STATUS_ACTIVE:
                return {"ok": False, "error": "Device is not active in Zigbee2MQTT"}
            base_topic = record["instance"]
        result = self.request(
            base_topic, "device/rename", {"from": ieee, "to": new_name, "homeassistant_rename": True}, timeout=20
        )
        ok = bool(result and result.get("status") == "ok")
        return {"ok": ok, "error": None if ok else friendly_z2m_error((result or {}).get("error") or "No response")}

    def ota(self, ieee: str, action: str) -> dict:
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None or record["status"] != STATUS_ACTIVE:
                return {"ok": False, "error": "Device is not active in Zigbee2MQTT"}
            base_topic = record["instance"]
            name = record.get("friendly_name")
        if action == "check":
            result = self.request(base_topic, "device/ota_update/check", {"id": ieee}, timeout=60)
            ok = bool(result and result.get("status") == "ok")
            available = ((result or {}).get("data") or {}).get(
                "update_available", ((result or {}).get("data") or {}).get("updateAvailable")
            )
            return {
                "ok": ok,
                "available": available,
                "error": None if ok else friendly_z2m_error((result or {}).get("error") or "No response"),
            }
        if action == "update":
            with self.lock:
                self._add_event(_now(), base_topic, ieee, name, "firmware update started", "")
                rec = self.data["devices"][ieee]
                rec["update"] = {**(rec.get("update") or {}), "state": "updating", "progress": 0}
                self._changed(structural=True)

            def run() -> None:
                result = self.request(base_topic, "device/ota_update/update", {"id": ieee}, timeout=3600)
                with self.lock:
                    if not result or result.get("status") != "ok":
                        self._add_event(
                            _now(),
                            base_topic,
                            ieee,
                            name,
                            "firmware update failed",
                            friendly_z2m_error((result or {}).get("error") or "No response"),
                        )
                        rec = self.data["devices"].get(ieee)
                        if rec and (rec.get("update") or {}).get("state") == "updating":
                            rec["update"]["state"] = "available"
                    self._changed(structural=True)

            threading.Thread(target=run, name="ota", daemon=True).start()
            return {"ok": True}
        return {"ok": False, "error": "Unknown action"}
