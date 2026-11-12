"""Publishes per-device entities to Home Assistant via MQTT discovery.

Entities attach to the device Zigbee2MQTT already created (shared identifier
"zigbee2mqtt_<ieee>"), so they show up on that device's page in HA.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable

_LOGGER = logging.getLogger("z2m_tracker")

PREFIX = "homeassistant"
BASE = "z2m_tracker"
AVAILABILITY = f"{BASE}/status"


class Discovery:
    def __init__(self, tracker, version: str, enabled: bool) -> None:
        self.tracker = tracker
        self.version = version
        self.enabled = enabled
        self.publish: Callable[[str, str, bool], None] | None = None
        self.lock = threading.Lock()
        self._configs: dict[str, str] = {}
        self._states: dict[str, str] = {}

    # ------------------------------------------------------------- building

    def _entities(self, record: dict, summary: dict, issues: list[dict]) -> dict[str, dict]:
        ieee = record["ieee"]
        state_topic = f"{BASE}/{ieee}"
        base = {
            "availability_topic": AVAILABILITY,
            "device": {"identifiers": [f"zigbee2mqtt_{ieee}"]},
            "origin": {"name": "Z2M Device Tracker", "sw_version": self.version},
        }

        def entity(component: str, key: str, config: dict) -> tuple[str, dict]:
            payload = {**base, "unique_id": f"z2m_tracker_{ieee}_{key}", **config}
            return f"{PREFIX}/{component}/{BASE}/{ieee}_{key}/config", payload

        out: dict[str, dict] = {}
        topic, cfg = entity("binary_sensor", "attention", {
            "name": "Needs attention",
            "device_class": "problem",
            "state_topic": state_topic,
            "value_template": "{{ 'ON' if value_json.attention else 'OFF' }}",
            "json_attributes_topic": state_topic,
            "json_attributes_template": "{{ {'issues': value_json.issues} | tojson }}",
        })
        out[topic] = cfg
        if record.get("readings"):
            topic, cfg = entity("binary_sensor", "frozen", {
                "name": "Reading frozen",
                "device_class": "problem",
                "icon": "mdi:snowflake-alert",
                "state_topic": state_topic,
                "value_template": "{{ 'ON' if value_json.frozen else 'OFF' }}",
            })
            out[topic] = cfg
        topic, cfg = entity("button", "reconfigure", {
            "name": "Reconfigure",
            "icon": "mdi:refresh",
            "entity_category": "config",
            "command_topic": f"{BASE}/{ieee}/reconfigure",
            "payload_press": "PRESS",
        })
        out[topic] = cfg
        topic, cfg = entity("sensor", "lqi_24h", {
            "name": "Signal (24 h avg)",
            "icon": "mdi:signal",
            "state_class": "measurement",
            "entity_category": "diagnostic",
            "enabled_by_default": False,
            "state_topic": state_topic,
            "value_template": "{{ value_json.lqi_24h }}",
        })
        out[topic] = cfg
        topic, cfg = entity("sensor", "uptime_30d", {
            "name": "Uptime (30 days)",
            "icon": "mdi:percent-circle-outline",
            "unit_of_measurement": "%",
            "state_class": "measurement",
            "entity_category": "diagnostic",
            "enabled_by_default": False,
            "state_topic": state_topic,
            "value_template": "{{ value_json.uptime_30d }}",
        })
        out[topic] = cfg
        if record.get("power_source") == "Battery" or isinstance(record.get("battery"), (int, float)):
            topic, cfg = entity("sensor", "battery_days", {
                "name": "Battery days left",
                "icon": "mdi:battery-clock",
                "device_class": "duration",
                "unit_of_measurement": "d",
                "state_class": "measurement",
                "entity_category": "diagnostic",
                "enabled_by_default": False,
                "state_topic": state_topic,
                "value_template": "{{ value_json.battery_days }}",
            })
            out[topic] = cfg
        if self.tracker.is_battery_device(record):
            topic, cfg = entity("button", "battery_replaced", {
                "name": "Battery replaced",
                "icon": "mdi:battery-sync",
                "entity_category": "config",
                "command_topic": f"{BASE}/{ieee}/battery_replaced",
                "payload_press": "PRESS",
            })
            out[topic] = cfg
            topic, cfg = entity("sensor", "battery_last_replaced", {
                "name": "Battery last replaced",
                "icon": "mdi:battery-clock-outline",
                "device_class": "date",
                "entity_category": "diagnostic",
                "state_topic": state_topic,
                "value_template": "{{ value_json.battery_replaced }}",
            })
            out[topic] = cfg
            topic, cfg = entity("sensor", "battery_kind", {
                "name": "Battery type",
                "icon": "mdi:battery-unknown",
                "entity_category": "diagnostic",
                "enabled_by_default": False,
                "state_topic": state_topic,
                "value_template": "{{ value_json.battery_type }}",
            })
            out[topic] = cfg
        if record.get("type") == "EndDevice":
            topic, cfg = entity("sensor", "parent", {
                "name": "Connected via",
                "icon": "mdi:router-wireless",
                "entity_category": "diagnostic",
                "enabled_by_default": False,
                "state_topic": state_topic,
                "value_template": "{{ value_json.parent }}",
            })
            out[topic] = cfg
        return out

    def _state(self, record: dict, summary: dict, issues: list[dict]) -> dict:
        names = {}
        battery = self.tracker.battery_summary(record) if self.tracker.is_battery_device(record) else {}
        parent = (record.get("routing") or {}).get("parent") or {}
        if parent.get("ieee"):
            other = self.tracker.data["devices"].get(parent["ieee"])
            names["parent"] = (other and (other.get("friendly_name") or other["ieee"])) or parent["ieee"]
        return {
            "attention": any(issue["severity"] == "warning" for issue in issues),
            "frozen": any(issue["kind"] == "frozen" for issue in issues),
            "issues": [f"{issue['label']}: {issue['detail']}" for issue in issues],
            "lqi_24h": summary.get("lqi_24h"),
            "uptime_30d": record.get("_uptime"),
            "battery_days": summary.get("battery_estimate_days"),
            "parent": names.get("parent"),
            "battery_replaced": battery.get("last_replaced"),
            "battery_type": battery.get("label") or None,
        }

    # ------------------------------------------------------------- syncing

    def sync(self, force: bool = False) -> None:
        if not self.publish:
            return
        from health import uptime

        now = time.time()
        desired: dict[str, str] = {}
        states: dict[str, str] = {}
        with self.tracker.lock:
            issues_by_device: dict[str, list[dict]] = {}
            for issue in self.tracker.issues:
                issues_by_device.setdefault(issue["ieee"], []).append(issue)
            if self.enabled:
                for record in self.tracker.data["devices"].values():
                    if record.get("status") != "active" or record.get("type") == "Coordinator":
                        continue
                    summary = self.tracker.metrics.summary(record["ieee"], now)
                    issues = issues_by_device.get(record["ieee"], [])
                    for topic, cfg in self._entities(record, summary, issues).items():
                        desired[topic] = json.dumps(cfg, separators=(",", ":"))
                    record_for_state = dict(record, _uptime=uptime(record, now))
                    states[f"{BASE}/{record['ieee']}"] = json.dumps(self._state(record_for_state, summary, issues), separators=(",", ":"))
            previous_topics = set(self.tracker.data.get("discovery_topics") or [])

        with self.lock:
            stale = (set(self._configs) | previous_topics) - set(desired)
            for topic in stale:
                self.publish(topic, "", True)
                self._configs.pop(topic, None)
            for topic, payload in desired.items():
                if force or self._configs.get(topic) != payload:
                    self.publish(topic, payload, True)
                    self._configs[topic] = payload
            for topic, payload in states.items():
                if force or self._states.get(topic) != payload:
                    self.publish(topic, payload, True)
                    self._states[topic] = payload
            for topic in set(self._states) - set(states):
                self.publish(topic, "", True)
                self._states.pop(topic, None)

        with self.tracker.lock:
            if sorted(desired) != sorted(previous_topics):
                self.tracker.data["discovery_topics"] = sorted(desired)
                self.tracker.dirty = True
        if stale or force:
            _LOGGER.info("HA entities: %d published, %d removed", len(desired), len(stale))
