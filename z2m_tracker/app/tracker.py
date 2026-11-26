"""Device tracking core for the Z2M Device Tracker app (thread-safe, no HA deps)."""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from datetime import date, datetime, timezone, tzinfo
from time import sleep as _sleep
from typing import Any

import expected_outage
import firmware_notes
from backup_history import BackupHistory
from community import build_report, community_issues, issue_url, model_key, record_model, report_text
from costs import clean_battery_prices
from group_history import apply as apply_group_history
from group_history import prune as prune_group_history
from health import (
    MISMATCH_MESSAGES,
    REPORT_EVERY_MAX,
    REPORT_EVERY_MIN,
    clean_limits,
    device_issues,
    group_power_loss,
    group_router_outages,
    late_deadline,
    mutes_of,
    offline_timing,
    uptime,
    z2m_availability,
    z2m_setup,
)
from history_import import IEEE_RE, _features, collect_history
from incidents import build_incidents
from live_messages import MessageLog
from mesh_score import record as record_mesh_score
from mesh_score import score as mesh_score
from mesh_score import summary as mesh_summary
from metrics import Metrics
from naming import kind_of, naming_issues
from odd_readings import CHECKED as ODD_KEYS
from odd_readings import believable
from odd_readings import note as note_odd_reading
from payload_keys import MERGE_SECONDS as PAYLOAD_MERGE_SECONDS
from payload_keys import SHOW_DAYS as PAYLOAD_SHOW_DAYS
from payload_keys import describe as describe_payload_change
from payload_keys import merge as merge_payload_change
from payload_keys import observe as observe_payload_keys
from reliability import device_ages, model_reliability
from reporting import audit as reporting_audit
from reporting import describe as describe_reporting
from reporting import note_sent as note_reporting_sent
from reporting import sending as reporting_sending
from reporting import slim as slim_reportings
from reporting_profiles import CHOICES as PROFILE_CHOICES
from reporting_profiles import chosen_profile
from reporting_profiles import clean_defaults as clean_profile_defaults
from reporting_profiles import defaults as profile_defaults
from reporting_profiles import effective as effective_profile
from retained import counts as retained_counts
from retained import stale_entries
from tracker_actions import ActionsMixin
from tracker_alerts import AlertsMixin
from tracker_base import (
    ADVICE_KINDS,
    ASSET_FIELDS,
    BACKUP_MAX_AGE,
    BACKUP_REFRESH,
    COMMAND_BURST,
    COMMAND_TIMEOUT,
    EVENT_TYPES,
    GROUP_LEFT_DAYS,
    LAST_SEEN_TEXT,
    MAX_DETAIL,
    MAX_EVENTS,
    MAX_GROUP_LEFT,
    MAX_OUTAGES,
    MAX_PAYLOAD,
    MAX_RETAINED_COMMANDS,
    MAX_TASKS,
    MAX_UNLISTED_AVAILABILITY,
    MEASURE_KEYS,
    RETAINED_COMMAND_PREVIEW,
    SECRET_OPTIONS,
    SHRINK_HOLD_SECONDS,
    SHRINK_MIN_DEVICES,
    STATUS_ACTIVE,
    STATUS_REMOVED,
    USAGE_KEYS,
    USAGE_LIMIT,
    USAGE_LINE_NAMES,
    USAGE_MAX_AGE,
    USAGE_NAME_MAX,
    WARRANTY_AHEAD_DAYS,
    WARRANTY_ENDED_DAYS,
    WARRANTY_SOON_DAYS,
    Z2M_ONLY_FIELDS,
    _add_historic_name,
    _bindings_of,
    _csv_safe,
    _display_name,
    _dumps,
    _endpoint_clusters,
    _expected_reply,
    _finite,
    _ieee,
    _iso_date,
    _list_label,
    _loads,
    _max_time,
    _min_time,
    _new_record,
    _now,
    _pairing_step,
    _permit_opening,
    _ping_address,
    _reply_matches,
    _set_current_name,
    _slug,
    _stack_of,
    _stamp_offline_since,
    _text,
    clean_task,
    friendly_z2m_error,
    hop_summary,
    memory_growth,
    record_memory,
    task_due,
    warranty_end,
)
from tracker_battery import BatteryMixin
from tracker_persistence import PersistenceMixin
from tracker_replace import ReplaceMixin
from tracker_topology import TopologyMixin
from upgrade_impact import (
    ISSUE_MAX_DAYS,
    ISSUE_MIN_DAYS,
    MAX_UPGRADES,
    MERGE_SECONDS,
    after_window,
    before_window,
    merge_changes,
    summary,
    window_stats,
)
from upgrade_impact import ts as upgrade_ts
from z2m_config import KEEP_CHANGES, describe
from z2m_config import diff as config_diff
from z2m_config import flatten as config_flatten

_LOGGER = logging.getLogger("z2m_tracker")

# The modules the class is spread over (each holds one mixin), for the tests' fake clock.
PARTS = (
    "tracker_persistence",
    "tracker_battery",
    "tracker_topology",
    "tracker_alerts",
    "tracker_actions",
    "tracker_replace",
)


def _give_way() -> None:
    """Between two turns with the lock: lets a thread that waits for it (the MQTT one) go first."""
    _sleep(0)


class Tracker(PersistenceMixin, BatteryMixin, TopologyMixin, AlertsMixin, ActionsMixin, ReplaceMixin):
    """Keeps a persistent history of every device seen on Zigbee2MQTT."""

    """Keeps a persistent history of every device seen on Zigbee2MQTT."""

    def __init__(
        self,
        data_path: str,
        base_topics: list[str],
        config_dir: str,
        local_tz: tzinfo,
        metrics: Metrics,
        options: dict,
        oui: dict[str, str],
    ) -> None:
        self.data_path = data_path
        self.base_topics = base_topics
        self.config_dir = config_dir
        self.local_tz = local_tz
        self.metrics = metrics
        metrics.local_tz = local_tz  # rhythm slots are local time
        metrics.no_battery_estimate = self.battery_unreliable
        self.options = options
        self.oui = oui
        self.lock = threading.RLock()
        self.data: dict[str, Any] = {"devices": {}, "events": [], "imported": None, "networkmap": {}, "alerted": []}
        self.names: dict[str, dict[str, str]] = {}
        self.lists_received: set[str] = set()
        self.lists_ready = threading.Event()
        # _dirty: something to save. _hard: something that must not wait for SOFT_SAVE_SECONDS.
        self._dirty = False
        self._hard = False
        self._saved_at = 0.0
        # /api/devices as built last: (time, revision, body, gzipped body or None). Off (0) unless the
        # app turns it on: several clients asking within seconds of a change then share one build.
        self.snapshot_seconds = 0.0
        # Off unless the app turns it on (see _health_changed).
        self.defer_health = False
        self._health_pending = False
        self._snapshot: list | None = None
        # Each device's metrics summary from the last health check, for the entities published after it.
        self._summaries: tuple[float, dict[str, dict]] = (0.0, {})
        self.boot_id = uuid.uuid4().hex[:8]
        self.revision = 0
        self._incidents_cache: tuple[tuple, list[dict]] | None = None
        self.publish: Callable[[str, dict], None] | None = None
        self.on_event: Callable[[dict], None] | None = None
        self.issues: list[dict] = []
        self.ha_device_ids: dict[str, str] = {}
        self.ha_battery: dict[str, dict] = {}
        self.ha_leftovers: list[dict] = []
        # IEEE -> {"automations", "scripts", "scenes", "at"} for devices with a problem (see usage_wanted).
        self.ha_usage: dict[str, dict] = {}
        # IEEE -> {Zigbee2MQTT unique id after "<ieee>_" -> HA entity id}, merged at each registry read and
        # never trimmed while running, so an entity HA has just removed can still be named.
        self.ha_entities: dict[str, dict[str, str]] = {}
        # Entity id -> {"automations", "scripts"} that use it, for keys a device stopped sending.
        self.entity_usage: dict[str, dict] = {}
        self._entity_usage_at: dict[str, float] = {}
        self._usage_keys: set[str] = set()
        self._pending: dict[str, dict] = {}
        self.battery_lib = None
        # community.CommunityRatings, set by main.py.
        self.community = None
        self.permit: dict[str, dict] = {}
        self.scans: dict[str, list] = {}
        self.scans_path: str | None = None
        self.pairing_sessions: dict[str, dict] = {}
        # Per base topic while pairing is open: {"since", "origin" (app/mqtt/frontend/None), "device"}.
        self.permit_meta: dict[str, dict] = {}
        self._permit_hint: dict[str, dict] = {}
        self.groups: dict[str, dict[str, str]] = {}
        # Current member IEEEs per group per base topic (memory only; bridge/groups is retained).
        self.group_members: dict[str, dict[str, list[str]]] = {}
        self._unbinds: dict[str, float] = {}
        self._spof_cache: dict[str, tuple] = {}
        self._placement_cache: dict[str, tuple] = {}
        self._backups: dict[str, dict] = {}
        self._backups_at = 0.0
        # Last copies of each network's coordinator_backup.json, next to history.json.
        self.backup_history = BackupHistory(os.path.join(os.path.dirname(data_path) or ".", "coordinator_backups"))
        # Current mesh score per network (mesh_score.summary), set by evaluate_health.
        self.mesh: dict[str, dict] = {}
        self.started = time.time()
        # When the app could hear again after a gap: the last MQTT reconnect, and per network the last time
        # its bridge came back online. The Stopped reporting clock starts no earlier (see _listening_since).
        self._mqtt_back = 0.0
        self._bridge_back: dict[str, float] = {}
        self._late_checked: dict[str, float] = {}
        # IEEE -> silent limit in seconds from its reporting profile (0 = none), set by evaluate_health.
        self._report_limits: dict[str, int] = {}
        self._hours: dict[str, dict] = {}
        self._hours_at: int | None = None
        self.addresses: dict[str, dict] = {}
        self.endpoint_clusters: dict[str, dict[str, set[str]]] = {}
        self._save_lock = threading.Lock()
        # Per base topic: (IEEEs, first seen) of a device list that would remove most devices, held until it repeats.
        self._held_list: dict[str, tuple[frozenset, float]] = {}
        self._bad_message_logged = 0.0
        # upgrade_impact() result, recomputed once an hour or when an upgrade is recorded or restored.
        self._impact: dict[str, dict] = {}
        self._impact_key: tuple | None = None
        self._upgrade_rev = 0
        # IEEE -> when its oldest unanswered command (<name>/set) arrived. Memory only.
        # ieee -> (when the first unanswered command was sent, the values a reply must show)
        self._commands: dict[str, tuple[float, dict]] = {}
        # The last scan of retained messages on the broker (retained.RetainedStore), set by main.py.
        self.retained = None
        # Full topic -> {"ieee", "payload", "seen"} for each retained <name>/set the broker replayed when
        # the tracker subscribed. Memory only: the broker sends them again on every connect.
        self.retained_commands: dict[str, dict] = {}
        # IEEE -> arrival times of its last few live messages (memory only), for the availability mismatch check.
        self._recent: dict[str, deque] = {}
        # base topic -> friendly name -> last availability payload for a name not in the device list yet.
        # Zigbee2MQTT says a re-paired or renamed device is online before its new device list arrives;
        # the list replays it. Memory only.
        self._unlisted_availability: dict[str, dict[str, str]] = {}
        # Mosquitto's $SYS statistics (broker_stats.BrokerStats), set by main.py.
        self.broker_stats = None
        # The last messages to and from each device, for its Messages tab. Memory only.
        self.messages = MessageLog(clock=lambda: time.time())

    def drop_snapshot(self) -> None:
        """Something changed that the page will ask about straight away (a request from it)."""
        self._snapshot = None

    def summary(self, ieee: str, now: float) -> dict[str, Any]:
        """The device's metrics summary: the one the health check just worked out when there is one."""
        at, summaries = self._summaries
        if 0 <= now - at < 90 and ieee in summaries:
            return summaries[ieee]
        return self.metrics.summary(ieee, now)

    @property
    def version(self) -> str:
        return f"{self.boot_id}-{self.revision}"

    # ------------------------------------------------------------------- MQTT

    def handle_message(self, base_topic: str, topic: str, payload: bytes, retained: bool = False) -> None:
        """Handle one MQTT message. Never raises: a bad message must not stop the MQTT thread."""
        if len(payload) > MAX_PAYLOAD:
            self._log_bad_message(topic, f"payload of {len(payload)} bytes is too large")
            return
        try:
            self._handle_message(base_topic, topic, payload, retained)
        except ValueError:
            return
        except Exception as err:  # noqa: BLE001 - malformed input from the broker
            self._log_bad_message(topic, f"{type(err).__name__}: {err}")

    def _log_bad_message(self, topic: str, reason: str) -> None:
        # At most once a minute, so a flood of bad messages can't flood the log.
        if time.time() - self._bad_message_logged >= 60:
            self._bad_message_logged = time.time()
            _LOGGER.warning("Ignored a malformed MQTT message on %s (%s)", _text(topic), _text(reason))

    # ------------------------------------------------- input from non-MQTT sources (ZHA)
    # Same handlers as Zigbee2MQTT's messages, without topic parsing: a name may hold any text.

    def _apply(self, what: str, handler: Callable[..., None], *args: Any) -> None:
        try:
            with self.lock:
                handler(*args)
        except ValueError:
            return
        except Exception as err:  # noqa: BLE001 - malformed input from the source
            self._log_bad_message(what, f"{type(err).__name__}: {err}")

    def apply_device_list(self, instance: str, devices: list[dict]) -> None:
        self._apply(f"{instance} device list", self._handle_devices, instance, devices)

    def apply_availability(self, instance: str, name: str, state: str) -> None:
        self._apply(f"{instance} availability", self._handle_availability, instance, name, state)

    def apply_state(self, instance: str, name: str, payload: dict, rhythm: bool = True) -> None:
        """rhythm=False: an event that isn't the device reporting (ZHA battery), so it files no report gap."""
        self._apply(f"{instance} state", self._handle_state, instance, name, json.dumps(payload), False, rhythm)

    def apply_networkmap(self, instance: str, value: dict) -> None:
        payload = {"status": "ok", "data": {"value": value}}
        self._apply(f"{instance} network map", self._ingest_networkmap, instance, payload)

    def set_bridge_state(self, instance: str, state: str) -> None:
        self._apply(f"{instance} network state", self._handle_bridge_state, instance, state)

    def set_network_info(self, instance: str, channel: int, pan_id: int | None) -> None:
        """The channel and PAN ID of a network that isn't Zigbee2MQTT's (which sends bridge/info)."""
        with self.lock:
            bridge = self._bridge(instance)
            info = {"channel": channel, "pan_id": pan_id}
            if bridge.get("info") != info:
                bridge["info"] = info
                self._changed(structural=True)

    def _handle_message(self, base_topic: str, topic: str, payload: bytes, retained: bool = False) -> None:
        rest = topic[len(base_topic) + 1 :]
        text = payload.decode("utf-8", errors="ignore")
        try:
            with self.lock:
                if rest.startswith("bridge/"):
                    if rest == "bridge/devices":
                        parsed = _loads(text)
                        if isinstance(parsed, list):
                            self._handle_devices(base_topic, parsed)
                    elif rest == "bridge/event":
                        parsed = _loads(text)
                        if isinstance(parsed, dict):
                            self._handle_event(base_topic, parsed)
                    elif rest.startswith("bridge/response/"):
                        parsed = _loads(text)
                        if isinstance(parsed, dict):
                            self._handle_response(base_topic, rest[len("bridge/response/") :], parsed)
                    elif rest == "bridge/info":
                        parsed = _loads(text)
                        if isinstance(parsed, dict):
                            self._handle_info(base_topic, parsed)
                    elif rest == "bridge/state":
                        self._handle_bridge_state(base_topic, text)
                    elif rest == "bridge/request/permit_join":
                        self._handle_permit_request(base_topic, text)
                    elif rest == "bridge/groups":
                        parsed = _loads(text)
                        if isinstance(parsed, list):
                            self._handle_groups(base_topic, parsed)
                    elif rest == "bridge/health":
                        parsed = _loads(text)
                        if isinstance(parsed, dict):
                            self._handle_bridge_health(base_topic, parsed)
                    return
                if rest.endswith("/availability"):
                    self._handle_availability(base_topic, rest[: -len("/availability")], text)
                    return
                if rest.endswith("/set") or "/set/" in rest:
                    self._handle_command(base_topic, rest, retained, text)
                    return
                if rest.endswith("/get") or "/get/" in rest:
                    return
                self._handle_state(base_topic, rest, text, retained)
        except ValueError:
            return

    @property
    def mqtt_topics(self) -> list[str]:
        """The Zigbee2MQTT base topics read: every network except ZHA's, which isn't an MQTT topic."""
        return [bt for bt in self.base_topics if _stack_of(bt) == "z2m"]

    def instance_of(self, ieee: str) -> str | None:
        """The network a known device belongs to, or None."""
        record = self.data["devices"].get(ieee) if isinstance(ieee, str) else None
        return record.get("instance") if record else None

    def is_frozen(self, record: dict) -> bool:
        """A device from a network this app no longer listens to: history kept, no checks or requests."""
        instance = record.get("instance")
        return isinstance(instance, str) and instance not in ("", "unknown") and instance not in self.base_topics

    def _record_for(self, base_topic: str, name: str) -> dict | None:
        ieee = self.names.get(base_topic, {}).get(name)
        return self.data["devices"].get(ieee) if ieee else None

    def _handle_devices(self, base_topic: str, devices: list[dict]) -> None:
        now = _now()
        records = self.data["devices"]
        # ZHA sends no join and leave events: its device list changing is the only sign, so a device
        # appearing in it or going from it also counts (and alerts) as joined or left.
        zha = _stack_of(base_topic) != "z2m"
        tracking_already = any(record["instance"] == base_topic for record in records.values())
        listed: set[str] = set()
        names: dict[str, str] = {}
        changed = False

        entries = [(d, _ieee(d.get("ieee_address"))) for d in devices if isinstance(d, dict)]
        entries = [(d, ieee) for d, ieee in entries if ieee]
        if self._hold_shrinking_list(base_topic, {ieee for _, ieee in entries}, now):
            return

        for device, ieee in entries:
            listed.add(ieee)
            name = _text(device.get("friendly_name"))
            if name:
                names[name] = ieee

            record = records.get(ieee)
            if record is None:
                record = _new_record(ieee, base_topic)
                record["first_seen"] = now
                records[ieee] = record
                changed = True
                if tracking_already:
                    self._add_event(
                        now, base_topic, ieee, name, "added", f"Appeared in the {_list_label(base_topic)} device list"
                    )
                    if zha:
                        self._add_event(now, base_topic, ieee, name, "joined", "", notify=True)

            if record["instance"] != base_topic:
                if record["instance"] not in (None, "unknown"):
                    self._add_event(now, base_topic, ieee, name, "moved", f"From {record['instance']}")
                    if _stack_of(record["instance"]) != _stack_of(base_topic):
                        fresh = _new_record(ieee, base_topic)
                        for key in Z2M_ONLY_FIELDS:
                            if key in fresh:
                                record[key] = fresh[key]
                            else:
                                record.pop(key, None)
                        self.endpoint_clusters.pop(ieee, None)
                        self.addresses.pop(ieee, None)
                record["instance"] = base_topic
                changed = True

            if record["status"] != STATUS_ACTIVE:
                if record["availability"] is not None:
                    # Its online/offline state is from before it left: unknown until Zigbee2MQTT says again.
                    outages = record.get("outages") or []
                    if outages and not outages[-1].get("end"):
                        outages[-1]["end"] = now
                    record["availability"] = None
                    record["availability_since"] = None
                if record["status"] == STATUS_REMOVED and tracking_already:
                    self._add_event(
                        now, base_topic, ieee, name, "rejoined", f"Back in the {_list_label(base_topic)} device list"
                    )
                    if zha:
                        self._add_event(now, base_topic, ieee, name, "joined", "", notify=True)
                record["status"] = STATUS_ACTIVE
                record["removed_at"] = None
                changed = True

            previous = _set_current_name(record, name, now)
            if previous is not None:
                changed = True
                if previous:
                    self._add_event(now, base_topic, ieee, name, "renamed", f"{previous} → {name}")

            definition = device.get("definition") if isinstance(device.get("definition"), dict) else {}
            interview_state = device.get("interview_state")
            if not interview_state:
                if device.get("interviewing"):
                    interview_state = "IN_PROGRESS"
                elif device.get("interview_completed"):
                    interview_state = "SUCCESSFUL"
                elif device.get("interview_completed") is False:
                    interview_state = "FAILED"
            exposes = definition.get("exposes")
            updates = {
                "type": _text(device.get("type")),
                "model": _text(definition.get("model")),
                "vendor": _text(definition.get("vendor")),
                "description": _text(definition.get("description")),
                "model_id": _text(device.get("model_id")),
                "manufacturer": _text(device.get("manufacturer")),
                "power_source": _text(device.get("power_source")),
                "software_build_id": _text(device.get("software_build_id")),
                "date_code": _text(device.get("date_code")),
                "supported": device.get("supported") if isinstance(device.get("supported"), bool) else None,
                "interview_state": _text(interview_state),
                "z2m_description": _text(device.get("description"), MAX_DETAIL),
                "features": _features(exposes) if isinstance(exposes, list) else [],
            }
            for key, value in updates.items():
                if value is None or value == []:
                    continue
                if record.get(key) != value:
                    if key == "model" and record.get(key):
                        self._add_event(now, base_topic, ieee, name, "model changed", f"{record[key]} → {value}")
                    if key == "software_build_id" and record.get(key):
                        # Device age view: when the firmware last changed (an update from anywhere).
                        record["firmware_changed"] = now
                    record[key] = value
                    changed = True
            address = _ping_address(device)
            if address:
                self.addresses[ieee] = address
            clusters = _endpoint_clusters(device)
            if clusters:
                self.endpoint_clusters[ieee] = clusters
            if self._update_bindings(base_topic, record, device, name, now, tracking_already):
                changed = True
            if self._update_reportings(record, device):
                changed = True
            record["last_listed"] = now
            source = f"{_list_label(base_topic)} live"
            if source not in record["sources"]:
                record["sources"].append(source)

        for ieee, record in records.items():
            if record["instance"] == base_topic and record["status"] == STATUS_ACTIVE and ieee not in listed:
                record["status"] = STATUS_REMOVED
                record["removed_at"] = now
                self._add_event(
                    now,
                    base_topic,
                    ieee,
                    record["friendly_name"],
                    "removed",
                    f"Gone from the {_list_label(base_topic)} device list",
                )
                if zha:
                    self._add_event(
                        now, base_topic, ieee, record["friendly_name"], "left", "Device left the network", notify=True
                    )
                changed = True

        self.names[base_topic] = names
        unlisted = self._unlisted_availability.pop(base_topic, {})
        for name, payload in unlisted.items():
            if name in names:
                self._handle_availability(base_topic, name, payload)
        self.lists_received.add(base_topic)
        if self.lists_received >= set(self.base_topics):
            self.lists_ready.set()
        self._changed(structural=changed)

    def _hold_shrinking_list(self, base_topic: str, listed: set[str], now: str) -> bool:
        """True when this device list would drop most of the network and hasn't been seen before.

        Anything on the broker can publish bridge/devices, so a list that empties the network has to
        arrive again at least SHRINK_HOLD_SECONDS later before devices are marked removed. Zigbee2MQTT
        republishes the list on every change and restart, so a real mass removal still lands. This
        stops a stray message or a quick burst; only broker ACLs stop someone determined.
        """
        active = {
            ieee
            for ieee, record in self.data["devices"].items()
            if record["instance"] == base_topic and record["status"] == STATUS_ACTIVE
        }
        gone = active - listed
        if len(active) < SHRINK_MIN_DEVICES or len(gone) * 2 <= len(active):
            self._held_list.pop(base_topic, None)
            return False
        key = frozenset(listed)
        held = self._held_list.get(base_topic)
        if held and held[0] == key:
            if time.time() - held[1] >= SHRINK_HOLD_SECONDS:
                self._held_list.pop(base_topic, None)
                return False
            return True
        self._held_list[base_topic] = (key, time.time())
        _LOGGER.warning(
            "%s: device list would remove %d of %d devices; waiting for Zigbee2MQTT to send it again",
            base_topic,
            len(gone),
            len(active),
        )
        self._add_event(
            now,
            base_topic,
            "network",
            "Network",
            "device list held",
            f"A device list without {len(gone)} of {len(active)} devices arrived; it is applied if it arrives again",
        )
        self._changed(structural=True)
        return True

    # ----------------------------------------------------------------- groups

    def _handle_groups(self, base_topic: str, groups: list) -> None:
        """bridge/groups: [{id, friendly_name, members: [{ieee_address, endpoint}], scenes}]."""
        names: dict[str, str] = {}
        members: dict[str, list[str]] = {}
        for group in groups:
            if not isinstance(group, dict) or group.get("id") is None:
                continue
            gid = str(group["id"])
            names[gid] = _text(group.get("friendly_name")) or f"Group {gid}"
            ieees: list[str] = []
            for member in group.get("members") or []:
                ieee = str((member or {}).get("ieee_address") or "").lower() if isinstance(member, dict) else ""
                if ieee and ieee not in ieees:
                    ieees.append(ieee)
            members[gid] = ieees
        previous = self.group_members.get(base_topic)
        facts = list(self.data.get("group_left") or [])
        # Forget facts for groups that are gone, and for members that are back in their group.
        kept = [
            f
            for f in facts
            if f.get("instance") != base_topic
            or (f.get("group") in members and f.get("ieee") not in members[f.get("group")])
        ]
        if previous is not None:
            now = _now()
            for gid, current in members.items():
                for ieee in previous.get(gid) or []:
                    if ieee in current:
                        continue
                    kept = [
                        f
                        for f in kept
                        if not (f.get("instance") == base_topic and f.get("group") == gid and f.get("ieee") == ieee)
                    ]
                    kept.append({"instance": base_topic, "group": gid, "ieee": ieee, "at": now})
        kept = kept[-MAX_GROUP_LEFT:]
        if kept != facts:
            self.data["group_left"] = kept
            self._changed(structural=True)
        if names != self.groups.get(base_topic) or members != previous:
            self.revision += 1
        self.groups[base_topic] = names
        self.group_members[base_topic] = members
        self._record_group_history(base_topic, names, members)

    def _record_group_history(self, base_topic: str, names: dict[str, str], members: dict[str, list[str]]) -> None:
        """Membership, name and created/removed changes per group, kept in history.json (see group_history)."""
        states = self.data.setdefault("group_state", {})
        history = self.data.setdefault("group_history", {}).setdefault(base_topic, {})
        devices = self.data["devices"]
        new_state, changed = apply_group_history(
            states.get(base_topic),
            history,
            names,
            members,
            _now(),
            lambda ieee: _display_name(devices.get(ieee) or {}) or ieee,
        )
        if prune_group_history(history, set(members), datetime.fromtimestamp(time.time(), tz=timezone.utc)):
            changed = True
        if changed:
            states[base_topic] = new_state
            self._changed(structural=True)

    def group_health(self, now: float) -> list[dict]:
        """Each group with its members' state and members that left it recently (still on the network)."""
        devices = self.data["devices"]
        cutoff = datetime.fromtimestamp(now - GROUP_LEFT_DAYS * 86400, tz=timezone.utc).isoformat(timespec="seconds")
        left_by: dict[tuple[str, str], list[dict]] = {}
        for fact in self.data.get("group_left") or []:
            record = devices.get(fact.get("ieee"))
            # A device that left the network drops out of its groups first; only flag it once it's back.
            if (fact.get("at") or "") < cutoff or record is None or record["status"] != STATUS_ACTIVE:
                continue
            left_by.setdefault((fact.get("instance"), fact.get("group")), []).append(
                {"ieee": fact["ieee"], "name": _display_name(record) or fact["ieee"], "at": fact["at"]}
            )
        out = []
        for bt in self.base_topics:
            names = self.groups.get(bt) or {}
            for gid, ieees in (self.group_members.get(bt) or {}).items():
                left = left_by.get((bt, gid), [])
                if not ieees and not left and not ((self.data.get("group_history") or {}).get(bt) or {}).get(gid):
                    continue
                members = []
                for ieee in ieees:
                    record = devices.get(ieee) or {}
                    offline = record.get("status") == STATUS_ACTIVE and record.get("availability") == "offline"
                    seconds = None
                    if offline and record.get("availability_since"):
                        try:
                            seconds = max(
                                0, int(now - datetime.fromisoformat(record["availability_since"]).timestamp())
                            )
                        except ValueError:
                            seconds = None
                    members.append(
                        {
                            "ieee": ieee,
                            "name": _display_name(record) or ieee,
                            "offline": offline,
                            "offline_seconds": seconds,
                        }
                    )
                state = ((self.data.get("group_state") or {}).get(bt) or {}).get(gid) or {}
                history = ((self.data.get("group_history") or {}).get(bt) or {}).get(gid) or []
                out.append(
                    {
                        "instance": bt,
                        "id": gid,
                        "name": names.get(gid) or f"Group {gid}",
                        "members": members,
                        "total": len(members),
                        "offline": sum(1 for m in members if m["offline"]),
                        "left": left,
                        "since": state.get("since"),
                        "history": list(reversed(history)),
                    }
                )
        out.sort(key=lambda g: (-g["offline"], not g["left"], g["name"].lower()))
        return out

    def _group_issues(self, now: float) -> list[dict]:
        if "group_member_offline" in set(self.options.get("disabled_checks") or []):
            return []
        limit = max(0, int(self.options.get("offline_alert_minutes", 60))) * 60
        out = []
        for group in self.group_health(now):
            late = [m for m in group["members"] if m["offline"] and (m["offline_seconds"] or 0) >= limit]
            if not late:
                continue
            names = ", ".join(m["name"] for m in late)
            n, total = len(late), group["total"]
            out.append(
                {
                    "key": f"group_member_offline:{group['instance']}:{group['id']}",
                    "kind": "group_member_offline",
                    "label": "Group members offline",
                    "severity": "info",
                    "ieee": f"group:{group['instance']}:{group['id']}",
                    "title": "Group members offline",
                    "detail": f"{n} of {total} member{'' if total == 1 else 's'} offline: {names}.",
                    "msg": "group_member_offline",
                    "params": {"n": n, "total": total, "names": names},
                    "name": group["name"],
                    "instance": group["instance"],
                }
            )
        return out

    # --------------------------------------------------------------- bindings

    def binding_label(self, base_topic: str, binding: dict) -> str:
        target = binding.get("target") or ""
        if target.startswith("group:"):
            gid = target.split(":", 1)[1]
            dest = f"group {self.groups.get(base_topic, {}).get(gid) or gid}"
        else:
            ieee, _, ep = target.partition("/")
            rec = self.data["devices"].get(ieee)
            if rec and rec.get("type") == "Coordinator":
                dest = "Coordinator"
            else:
                dest = (_display_name(rec) if rec else None) or ieee
                if ep and ep not in ("None", "1"):
                    dest += f" (endpoint {ep})"
        return f"{binding.get('cluster')} → {dest}"

    def _is_coordinator_target(self, binding: dict) -> bool:
        ieee = (binding.get("target") or "").partition("/")[0]
        return (self.data["devices"].get(ieee) or {}).get("type") == "Coordinator"

    def _update_bindings(
        self, base_topic: str, record: dict, device: dict, name: str | None, now: str, tracking_already: bool
    ) -> bool:
        current = _bindings_of(device)
        if current is None or record.get("type") == "Coordinator":
            return False
        interview = record.get("interview_state")
        if interview and interview != "SUCCESSFUL":
            return False
        previous = record.get("bindings")
        record["bindings"] = current
        if previous is None:
            return True
        old = {b["key"]: b for b in previous}
        new = {b["key"]: b for b in current}
        if old.keys() == new.keys():
            return False
        removed = [old[k] for k in sorted(old.keys() - new.keys())]
        added = [new[k] for k in sorted(new.keys() - old.keys())]
        lost = record.setdefault("lost_bindings", [])
        if added:
            back = {b["key"] for b in added}
            restored = [b for b in lost if b["key"] in back]
            record["lost_bindings"] = lost = [b for b in lost if b["key"] not in back]
            text = "; ".join(self.binding_label(base_topic, b) for b in added[:6]) + ("…" if len(added) > 6 else "")
            self._add_event(
                now,
                base_topic,
                record["ieee"],
                name,
                "binding restored" if restored and len(restored) == len(added) else "binding added",
                text,
            )
        if removed:
            unbound_at = self._unbinds.pop(record["ieee"], 0)
            intended = time.time() - unbound_at < 180
            text = "; ".join(self.binding_label(base_topic, b) for b in removed[:6]) + ("…" if len(removed) > 6 else "")
            self._add_event(now, base_topic, record["ieee"], name, "unbound" if intended else "binding removed", text)
            if not intended and tracking_already:
                known = {b["key"] for b in lost}
                for b in removed:
                    if b["key"] not in known:
                        lost.append(
                            {
                                **b,
                                "label": self.binding_label(base_topic, b),
                                "coordinator": self._is_coordinator_target(b),
                                "lost_at": now,
                            }
                        )
                del lost[: max(0, len(lost) - 50)]
        return True

    # ------------------------------------------------------------- reporting

    def _update_reportings(self, record: dict, device: dict) -> bool:
        """Keep a slim copy of the device's configured reportings. True when it changed."""
        if record.get("type") == "Coordinator":
            return False
        interview = record.get("interview_state")
        if interview and interview != "SUCCESSFUL":
            return False
        found = slim_reportings(device)
        if found is None:
            return False
        items, inputs = found
        if record.get("reportings") == items and record.get("reporting_inputs") == inputs:
            return False
        record["reportings"] = items
        record["reporting_inputs"] = inputs
        return True

    def reporting_audit(self, record: dict, rate: float | None) -> dict | None:
        """The reporting rules applied to one device, rate = its messages an hour over 6 h (caller holds the lock)."""
        return reporting_audit(
            record.get("reportings"),
            record.get("reporting_inputs"),
            record.get("features"),
            self.is_battery_device(record),
            rate,
            reporting_sending(record.get("reporting_sent"), time.time()),
        )

    def configure_reporting(self, ieee: str, fix: dict) -> dict:
        """Send bridge/request/device/configure_reporting for one reporting Zigbee2MQTT listed for the device.

        fix comes from reporting.clean_fix: {ep, cluster, attribute, min, max, change}.
        """
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None:
                return {"ok": False, "error": "Unknown device", "reason": "unknown"}
            if record["status"] != STATUS_ACTIVE or not record.get("instance"):
                return {"ok": False, "error": "Device is not active in Zigbee2MQTT", "reason": "invalid"}
            match = next(
                (
                    item
                    for item in record.get("reportings") or []
                    if (item["ep"], item["cluster"], item["attribute"]) == (fix["ep"], fix["cluster"], fix["attribute"])
                ),
                None,
            )
            if match is None:
                return {
                    "ok": False,
                    "error": "Zigbee2MQTT doesn't list that reporting setting for this device",
                    "reason": "invalid",
                }
            base_topic = record["instance"]
            name = record.get("friendly_name")
        payload = {
            "id": ieee,
            "endpoint": int(fix["ep"]) if fix["ep"].isdigit() else fix["ep"],
            "cluster": fix["cluster"],
            "attribute": fix["attribute"],
            "minimum_report_interval": fix["min"],
            "maximum_report_interval": fix["max"],
            "reportable_change": fix["change"],
        }
        result = self.request(base_topic, "device/configure_reporting", payload, timeout=30)
        ok = bool(result and result.get("status") == "ok")
        raw = None if ok else ((result or {}).get("error") or "No response from Zigbee2MQTT")
        error = None if ok else friendly_z2m_error(raw)
        new = {**match, "min": fix["min"], "max": fix["max"], "change": fix["change"]}
        where = f" (endpoint {fix['ep']})" if fix["ep"] != "1" else ""
        with self.lock:
            rec = self.data["devices"].get(ieee)
            if ok and rec is not None:
                for item in rec.get("reportings") or []:
                    if (item["ep"], item["cluster"], item["attribute"]) == (
                        fix["ep"],
                        fix["cluster"],
                        fix["attribute"],
                    ):
                        item.update(min=fix["min"], max=fix["max"], change=fix["change"])
            self._add_event(
                _now(),
                base_topic,
                ieee,
                name,
                "reporting changed" if ok else "reporting change failed",
                f"{describe_reporting(new)}{where}" if ok else f"{describe_reporting(match)}{where}: {error}",
            )
            self._changed(structural=True)
        if ok:
            self._health_changed()
        return {"ok": ok, "error": error, "detail": raw, "reason": None if ok else "z2m"}

    def acknowledge_bindings(self, ieee: str) -> bool:
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None:
                return False
            if record.get("lost_bindings"):
                self._add_event(
                    _now(),
                    record.get("instance") or "unknown",
                    ieee,
                    record.get("friendly_name"),
                    "binding change accepted",
                    f"{len(record['lost_bindings'])} lost binding(s) dismissed",
                )
            record["lost_bindings"] = []
            self._changed(structural=True)
        self._health_changed()
        return True

    def restore_bindings(self, ieee: str, keys: list[str] | None = None) -> dict:
        """Re-create lost bindings: a bind request for device/group targets, reconfigure for the coordinator.

        keys limits it to those lost bindings; None restores all of them.
        """
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None or record["status"] != STATUS_ACTIVE:
                return {"ok": False, "error": "Device is not active in Zigbee2MQTT"}
            base_topic = record["instance"]
            lost = [dict(b) for b in record.get("lost_bindings") or []]
            if keys is not None:
                lost = [b for b in lost if b.get("key") in keys]
        if not lost:
            return {"ok": True, "restored": 0, "failed": []}
        failed: list[str] = []
        errors: list[str] = []
        restored = 0
        if any(b.get("coordinator") for b in lost):
            result = self.reconfigure(ieee)
            if result["ok"]:
                restored += sum(1 for b in lost if b.get("coordinator"))
            else:
                failed += [b.get("label") or b["key"] for b in lost if b.get("coordinator")]
                errors.append(str(result.get("error")))
        for binding in lost:
            if binding.get("coordinator"):
                continue
            target = binding.get("target") or ""
            payload: dict[str, Any] = {"from": ieee, "clusters": [binding.get("cluster")]}
            ep = str(binding.get("ep") or "")
            if ep.isdigit():
                payload["from_endpoint"] = int(ep)
            if target.startswith("group:"):
                # Zigbee2MQTT only accepts a group's friendly name as a bind target, not its ID.
                gid = target.split(":", 1)[1]
                group_name = self.groups.get(base_topic, {}).get(gid)
                if not group_name:
                    failed.append(binding.get("label") or binding["key"])
                    errors.append(f"Group {gid} no longer exists in Zigbee2MQTT")
                    continue
                payload["to"] = group_name
            else:
                to_ieee, _, to_ep = target.partition("/")
                payload["to"] = to_ieee
                if to_ep.isdigit():
                    payload["to_endpoint"] = int(to_ep)
            result = self.request(base_topic, "device/bind", payload, timeout=30)
            if result and result.get("status") == "ok" and not (result.get("data") or {}).get("failed"):
                restored += 1
            else:
                failed.append(binding.get("label") or binding["key"])
                errors.append(friendly_z2m_error((result or {}).get("error") or "No response from Zigbee2MQTT"))
        with self.lock:
            rec = self.data["devices"].get(ieee)
            if rec is not None:
                self._add_event(
                    _now(),
                    base_topic,
                    ieee,
                    rec.get("friendly_name"),
                    "binding restore requested" if not failed else "binding restore failed",
                    f"{restored} of {len(lost)} re-created" + (f": {errors[0]}" if errors else ""),
                )
                self._changed(structural=True)
        ok = not failed
        return {"ok": ok, "restored": restored, "failed": failed, "error": None if ok else errors[0]}

    def _handle_event(self, base_topic: str, payload: dict) -> None:
        event_type = payload.get("type")
        data = payload.get("data")
        if event_type not in EVENT_TYPES or not isinstance(data, dict):
            return
        ieee = _ieee(data.get("ieee_address"))
        if not ieee:
            return
        now = _now()
        name = _text(data.get("friendly_name"))
        record = self.data["devices"].get(ieee)
        if record is None:
            record = _new_record(ieee, base_topic)
            record["first_seen"] = now
            self.data["devices"][ieee] = record
        record["last_seen"] = _max_time(record["last_seen"], now)

        if (
            event_type in ("device_joined", "device_interview", "device_announce")
            and base_topic in self.pairing_sessions
        ):
            session = self.pairing_sessions[base_topic]
            if session.get("target") == ieee and session.get("active") and not session.get("rejoined"):
                session["rejoined"] = now
                via = session.get("via_name") or "the chosen router"
                self._add_event(
                    now, base_topic, ieee, name, "re-paired", f"Rejoined while pairing was open only through {via}"
                )
                self._changed(structural=True)
            if event_type != "device_announce" and ieee not in session["joined"]:
                session["joined"].append(ieee)
                self.revision += 1
            if ieee in session["joined"]:
                progress = session.setdefault("progress", {})
                step = _pairing_step(event_type, _text(data.get("status"), 40))
                # An announce after the interview (the device waking up) doesn't undo its result.
                if step and not (
                    step == "announced" and progress.get(ieee) in ("interviewing", "successful", "failed")
                ):
                    if progress.get(ieee) != step:
                        progress[ieee] = step
                        self.revision += 1
        if event_type == "device_announce":
            self._changed(structural=False)
            return
        if event_type == "device_joined":
            self._add_event(now, base_topic, ieee, name, "joined", "", notify=True)
        elif event_type == "device_leave":
            self._add_event(now, base_topic, ieee, name, "left", "Device left the network", notify=True)
        elif event_type == "device_interview":
            status = _text(data.get("status"), 40) or "?"
            detail = ""
            definition = data.get("definition") if isinstance(data.get("definition"), dict) else {}
            # bridge/devices follows with the same state; setting it now lets the pairing dialog move on at once.
            if status in ("successful", "failed"):
                record["interview_state"] = status.upper()
            if status == "successful" and definition:
                for key in ("model", "vendor", "description"):
                    if _text(definition.get(key)):
                        record[key] = _text(definition[key])
                exposes = definition.get("exposes")
                features = _features(exposes) if isinstance(exposes, list) else []
                if features:
                    record["features"] = features
                detail = f"{record.get('vendor') or ''} {record.get('model') or ''}".strip()
            self._add_event(now, base_topic, ieee, name, f"interview {status}", detail, notify=status == "failed")
        else:
            return
        self._changed(structural=True)

    def _handle_response(self, base_topic: str, kind: str, payload: dict) -> None:
        transaction = payload.get("transaction")
        if transaction and transaction in self._pending:
            pending = self._pending[transaction]
            pending["result"] = payload
            pending["event"].set()
        elif kind == "networkmap" and str(transaction or "").startswith("z2mt-"):
            _LOGGER.info(
                "Routing scan on %s answered after the time limit (status %s); saved it",
                base_topic,
                _text(payload.get("status"), 40),
            )
        if kind == "networkmap":
            self._ingest_networkmap(base_topic, payload)
        elif kind == "permit_join" and payload.get("status") == "ok":
            data = payload.get("data") or {}
            device = data.get("device")
            if device and _permit_opening(data):
                for holder in (self.permit_meta.get(base_topic), self._permit_hint.get(base_topic)):
                    if holder is not None:
                        holder["device"] = device
                self.revision += 1
        elif kind == "device/unbind" and payload.get("status") == "ok":
            source = str((payload.get("data") or {}).get("from") or "")
            ieee = (
                self.names.get(base_topic, {}).get(source)
                or self.names.get(base_topic, {}).get(source.rsplit("/", 1)[0])
                or source.lower()
            )
            if ieee in self.data["devices"]:
                self._unbinds[ieee] = time.time()

    def _handle_availability(self, base_topic: str, name: str, payload: str) -> None:
        record = self._record_for(base_topic, name)
        if record is None:
            # Not in the device list (yet): keep the latest so the next list can apply it.
            pending = self._unlisted_availability.setdefault(base_topic, {})
            pending.pop(name, None)
            pending[name] = payload
            while len(pending) > MAX_UNLISTED_AVAILABILITY:
                del pending[next(iter(pending))]
            return
        self.messages.add(record["ieee"], "availability", payload.strip())
        state = payload.strip()
        if not state:
            # An empty message is someone deleting the retained topic: Zigbee2MQTT no longer
            # says anything about this device, so forget the old state instead of keeping it.
            if record["availability"] is not None:
                outages = record.get("outages") or []
                if outages and not outages[-1].get("end"):
                    outages[-1]["end"] = _now()
                record["availability"] = None
                record["availability_since"] = None
                self._changed(structural=True)
            return
        if state.startswith("{"):
            parsed = _loads(state)
            state = parsed.get("state", "") if isinstance(parsed, dict) else ""
        if state not in ("online", "offline"):
            return
        previous = record["availability"]
        if previous == state:
            if _stamp_offline_since(record):
                self._changed(structural=True)
            return
        now = _now()
        record["availability"] = state
        record["availability_since"] = now
        outages = record.setdefault("outages", [])
        if state == "offline":
            if not outages or outages[-1].get("end"):
                outages.append({"start": now, "end": None})
                del outages[: max(0, len(outages) - MAX_OUTAGES)]
        elif outages and not outages[-1].get("end"):
            outages[-1]["end"] = now
        if previous == "online" and state == "offline":
            self._add_event(now, base_topic, record["ieee"], name, "offline", "")
        elif previous == "offline" and state == "online":
            self._add_event(now, base_topic, record["ieee"], name, "online", "")
        self._changed(structural=True)

    # --------------------------------------------------------------- commands

    def _handle_command(self, base_topic: str, rest: str, retained: bool, text: str = "") -> None:
        """A <name>/set (or <name>/set/<attribute>, <name>/<endpoint>/set) from anyone: HA, automations, scripts.

        Starts the clock for the device's reply. A burst of commands before the reply counts as one,
        timed from the first. A retained /set is an old message replayed by the broker, not a command.
        Devices Zigbee2MQTT already marks offline aren't timed: the offline check covers them.
        """
        topic = f"{base_topic}/{rest}"
        if not text.strip():
            # An empty /set is someone clearing a retained command, not a command.
            if self.retained_commands.pop(topic, None) is not None:
                self.revision += 1
            return
        name = rest[: -len("/set")] if rest.endswith("/set") else rest.rsplit("/set/", 1)[0]
        if retained:
            self._remember_retained_command(topic, base_topic, name, text)
            return
        record = self._record_for(base_topic, name)
        if record is None and "/" in name:
            name = name.rsplit("/", 1)[0]
            record = self._record_for(base_topic, name)
        if record is None:
            return
        if record.get("status") == STATUS_ACTIVE and record.get("availability") != "offline":
            now = time.time()
            self.expire_commands(now)
            pending = self._commands.get(record["ieee"])
            if pending and now - pending[0] > COMMAND_BURST:
                # Still unanswered when the next command came: that one was missed.
                self.metrics.add_command(record["ieee"], None, now=pending[0])
                self.messages.settle(record["ieee"], pending[0], answered=False, now=now)
                pending = None
            # A burst is timed from its first command and answered by the state the last one asked for.
            self._commands[record["ieee"]] = (pending[0] if pending else now, _expected_reply(rest, text))
        # Logged after the timing above, so settling the previous burst can't touch this command.
        self.messages.add(record["ieee"], "command", text.strip(), rest[len(name) + 1 :])

    def _remember_retained_command(self, topic: str, base_topic: str, name: str, text: str) -> None:
        """A retained /set: the broker replays it on every restart. Kept by name, since the broker may
        send it before Zigbee2MQTT's device list; which device it belongs to is looked up when needed."""
        if topic not in self.retained_commands and len(self.retained_commands) >= MAX_RETAINED_COMMANDS:
            return
        payload = text.strip()
        if len(payload) > RETAINED_COMMAND_PREVIEW:
            payload = payload[: RETAINED_COMMAND_PREVIEW - 1] + "…"
        previous = self.retained_commands.get(topic)
        if previous and previous["payload"] == payload:
            return
        self.retained_commands[topic] = {"base": base_topic, "name": name, "payload": payload, "seen": _now()}
        self.revision += 1

    def _retained_command_device(self, entry: dict) -> str | None:
        """The active device a retained /set is for, by its current name (or IEEE address), or None."""
        base_topic, name = entry["base"], entry["name"]
        record = self._record_for(base_topic, name)
        if record is None and "/" in name:
            # <name>/<endpoint>/set
            record = self._record_for(base_topic, name.rsplit("/", 1)[0])
        if record is None and IEEE_RE.fullmatch(name.lower()):
            # Zigbee2MQTT also takes a device's IEEE address in place of its name.
            found = self.data["devices"].get(name.lower())
            record = found if found and found.get("instance") == base_topic else None
        if record is None or record.get("status") != STATUS_ACTIVE:
            return None
        return record["ieee"]

    def retained_commands_of(self, ieee: str) -> list[str]:
        with self.lock:
            return sorted(
                topic for topic, entry in self.retained_commands.items() if self._retained_command_device(entry) == ieee
            )

    def forget_retained_commands(self, topics: list[str]) -> None:
        with self.lock:
            for topic in topics:
                self.retained_commands.pop(topic, None)
            self.revision += 1

    def _command_answered(self, ieee: str, state: Any) -> None:
        pending = self._commands.get(ieee)
        if pending is None:
            return
        start, expect = pending
        # Zigbee2MQTT repeats the device's whole state in every message, so a regular report would look
        # like a reply. Only a message showing the values the command asked for counts.
        if not _reply_matches(expect, state):
            return
        del self._commands[ieee]
        now = time.time()
        elapsed = now - start
        self.metrics.add_command(ieee, elapsed * 1000 if elapsed <= COMMAND_TIMEOUT else None, now=start)
        self.messages.settle(ieee, start, answered=elapsed <= COMMAND_TIMEOUT, now=now)

    def expire_commands(self, now: float | None = None) -> None:
        """Count commands still unanswered after COMMAND_TIMEOUT as missed."""
        now = now or time.time()
        with self.lock:
            for ieee, (start, _) in list(self._commands.items()):
                if now - start > COMMAND_TIMEOUT:
                    del self._commands[ieee]
                    self.metrics.add_command(ieee, None, now=start)
                    self.messages.settle(ieee, start, answered=False, now=now)

    def _handle_state(
        self, base_topic: str, name: str, payload: str, retained: bool = False, rhythm: bool = True
    ) -> None:
        record = self._record_for(base_topic, name)
        if record is None:
            return
        if not retained:
            # Live messages only: a retained state replayed on connect says nothing about now.
            recent = self._recent.setdefault(record["ieee"], deque(maxlen=MISMATCH_MESSAGES))
            recent.append(time.time())
        self.messages.add(record["ieee"], "state", payload)
        parsed = None
        if payload[:1] == "{":
            parsed = _loads(payload)
        if self._commands:
            self._command_answered(record["ieee"], parsed)
        if not isinstance(parsed, dict):
            record["last_seen"] = _max_time(record["last_seen"], _now())
            self._changed(structural=False)
            return
        now = _now()
        ieee = record["ieee"]
        seen = self._to_utc_iso(parsed.get("last_seen")) or now
        record["last_seen"] = _max_time(record["last_seen"], seen)
        structural = False
        self.metrics.add_message(ieee, real=rhythm and not retained, limit=self._report_limits.get(ieee))

        lqi = parsed.get("linkquality")
        if _finite(lqi):
            record["lqi"] = lqi
            self.metrics.add_lqi(ieee, lqi)

        # Impossible readings: the believable range of each checked reading, and when it wasn't believable.
        checked = [(key, parsed[key]) for key in ODD_KEYS if _finite(parsed.get(key))]
        if checked:
            store = record.get("odd_readings")
            if not isinstance(store, dict):
                store = record["odd_readings"] = {}
            day = self._today().isoformat()
            for key, value in checked:
                if note_odd_reading(store, key, value, self.options, time.time(), day):
                    structural = True
        # Which keys a device sends, and how often: Zigbee2MQTT's payloads only. ZHA's updates come in
        # pieces (signal from the device list, battery from its sensor), which would look like changes.
        if _stack_of(base_topic) == "z2m":
            keys_state = record.get("payload_keys")
            if not isinstance(keys_state, dict):
                keys_state = record["payload_keys"] = {}
            added, removed = observe_payload_keys(keys_state, parsed.keys())
            sent_state = record.get("reporting_sent")
            if not isinstance(sent_state, dict):
                sent_state = record["reporting_sent"] = {}
            note_reporting_sent(sent_state, parsed, time.time())
            if added or removed:
                self._payload_changed(base_topic, record, name, added, removed, now)
                structural = True

        # ZHA readings carry the time ZHA heard them; a value replayed after a restart isn't new.
        reported = seen if _stack_of(base_topic) != "z2m" else now
        battery = parsed.get("battery")
        # A battery outside 0-100 % isn't a real level: kept out of the record and the battery estimates.
        if _finite(battery) and believable("battery", battery, self.options):
            if record.get("battery") != battery:
                structural = True
            record["battery"] = battery
            if self._replaced_since_reading(record):
                structural = True
            record["battery_reported"] = reported
            replaced_from = self.metrics.add_battery(ieee, battery)
            if replaced_from is not None and self._battery_jump(record, now[:10], replaced_from, battery):
                structural = True
        if isinstance(parsed.get("battery_low"), bool):
            if record.get("battery_low") != parsed["battery_low"]:
                structural = True
            record["battery_low"] = parsed["battery_low"]
            if self._replaced_since_reading(record):
                structural = True
            record["battery_reported"] = reported

        update = parsed.get("update")
        if isinstance(update, dict):
            slim = {
                "state": update.get("state"),
                "installed_version": update.get("installed_version"),
                "latest_version": update.get("latest_version"),
            }
            if update.get("state") == "updating":
                slim["progress"] = update.get("progress")
                slim["remaining"] = update.get("remaining")
            previous = record.get("update") or {}
            if previous.get("state") == "updating" and slim["state"] == "idle":
                self._add_event(now, base_topic, ieee, name, "firmware updated", f"Now on {slim['installed_version']}")
                record["firmware_changed"] = now
            if slim != previous:
                if slim["state"] == "available" and previous.get("state") != "available":
                    self._add_event(
                        now,
                        base_topic,
                        ieee,
                        name,
                        "update available",
                        f"{slim['installed_version']} → {slim['latest_version']}",
                    )
                record["update"] = slim
                structural = True

        readings = record.setdefault("readings", {})
        for key in MEASURE_KEYS:
            value = parsed.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            reading = readings.get(key)
            if reading is None or reading.get("value") != value:
                readings[key] = {"value": value, "changed_at": seen, "reports": 0}
            else:
                reading["reports"] = int(reading.get("reports", 0)) + 1

        self._changed(structural=structural)

    def _payload_changed(
        self, base_topic: str, record: dict, name: str, added: list[str], removed: list[str], now: str
    ) -> None:
        """Record a change in the keys a device sends: on the device (for Health) and in its logbook.
        Changes within a day of the last one are folded into it, so one firmware update is one entry."""
        ieee = record["ieee"]
        ts = time.time()
        last = record.get("payload_change")
        try:
            recent = bool(last) and ts - datetime.fromisoformat(last["at"]).timestamp() < PAYLOAD_MERGE_SECONDS
        except (KeyError, TypeError, ValueError):
            recent = False
        merged = merge_payload_change(last if recent else None, added, removed)
        entities = dict((last or {}).get("entities") or {}) if recent else {}
        known = self.ha_entities.get(ieee) or {}
        for key in removed:
            entity_id = known.get(f"{key}_{base_topic}")
            if entity_id:
                entities[key] = entity_id
        entities = {k: v for k, v in entities.items() if k in merged["removed"]}
        at = last["at"] if recent else now
        change = {**merged, "at": at, "entities": entities, "firmware": self._recent_firmware(ieee, ts)}
        record["payload_change"] = change if merged["added"] or merged["removed"] else None
        detail = describe_payload_change(merged["added"], merged["removed"])
        event = None
        if recent:
            event = next(
                (
                    e
                    for e in reversed(self.data["events"][-500:])
                    if e.get("ieee") == ieee and e.get("kind") == "payload changed" and e.get("time") == at
                ),
                None,
            )
        if event is None:
            self._add_event(now, base_topic, ieee, name, "payload changed", detail)
            event = self.data["events"][-1]
        event["detail"] = detail
        event["added"], event["removed"] = merged["added"], merged["removed"]
        if change["firmware"]:
            event["firmware"] = change["firmware"]

    def _recent_firmware(self, ieee: str, now: float, days: int = 7) -> str | None:
        """The version from the device's last firmware update in the past few days, if any."""
        since = datetime.fromtimestamp(now - days * 86400, tz=timezone.utc).isoformat(timespec="seconds")
        for event in reversed(self.data["events"]):
            if (event.get("time") or "") < since:
                break
            if event.get("ieee") == ieee and event.get("kind") == "firmware updated":
                detail = str(event.get("detail") or "")
                return _text(detail.removeprefix("Now on "), 40) if detail.startswith("Now on ") else None
        return None

    def entity_usage_wanted(self, max_age: float = USAGE_MAX_AGE) -> list[str]:
        """HA entities of keys a device recently stopped sending, not looked up in the last max_age seconds."""
        now = time.time()
        wanted = []
        with self.lock:
            for record in self.data["devices"].values():
                change = record.get("payload_change")
                if not change or record.get("status") != STATUS_ACTIVE:
                    continue
                try:
                    if now - datetime.fromisoformat(change["at"]).timestamp() > PAYLOAD_SHOW_DAYS * 86400:
                        continue
                except (KeyError, TypeError, ValueError):
                    continue
                wanted.extend(v for v in (change.get("entities") or {}).values() if isinstance(v, str))
            return [e for e in dict.fromkeys(wanted) if now - self._entity_usage_at.get(e, 0) >= max_age][:USAGE_LIMIT]

    def set_entity_usage(self, found: dict[str, dict], asked: list[str]) -> None:
        """Store what uses each asked entity. One missing from found (a failed lookup) keeps what was
        known and is asked again after USAGE_MAX_AGE."""
        now = time.time()
        with self.lock:
            for entity_id in asked:
                self._entity_usage_at[entity_id] = now
                if entity_id not in found:
                    continue
                usage = found[entity_id] or {}
                shown = {key: list(usage.get(key) or []) for key in ("automations", "scripts")}
                if self.entity_usage.get(entity_id) != shown:
                    self.entity_usage[entity_id] = shown
                    self.revision += 1

    def _payload_usage(self, record: dict) -> list[dict]:
        """For each key the device stopped sending: its HA entity and what uses it, when something does."""
        out = []
        for key, entity_id in sorted(((record.get("payload_change") or {}).get("entities") or {}).items()):
            usage = self.entity_usage.get(entity_id) or {}
            if usage.get("automations") or usage.get("scripts"):
                out.append({"key": key, "entity_id": entity_id, **usage})
        return out

    def _to_utc_iso(self, value: Any) -> str | None:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            if not _finite(value) or value <= 0:
                return None
            seconds = value / 1000 if value > 1e11 else value
            try:
                return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds")
            except (OverflowError, OSError, ValueError):
                return None
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=self.local_tz)
        return moment.astimezone(timezone.utc).isoformat(timespec="seconds")

    def _add_event(
        self, when: str, base_topic: str, ieee: str, name: str | None, kind: str, detail: str, notify: bool = False
    ) -> None:
        event = {
            "time": when,
            "instance": base_topic,
            "ieee": ieee,
            "name": _text(name),
            "kind": kind,
            "detail": _text(str(detail or ""), MAX_DETAIL) or "",
        }
        events = self.data["events"]
        events.append(event)
        if len(events) > MAX_EVENTS:
            del events[: len(events) - MAX_EVENTS]
        if notify and self.on_event:
            self.on_event(dict(event))

    # ----------------------------------------------------------------- health

    def _suggestions(self) -> dict[str, list[dict]]:
        removed_by_name: dict[str, list[dict]] = {}
        for record in self.data["devices"].values():
            if record["status"] != STATUS_REMOVED or record.get("successor"):
                continue
            for entry in record.get("names") or []:
                if entry.get("name") and not IEEE_RE.match(entry["name"].lower()):
                    removed_by_name.setdefault(_slug(entry["name"]), []).append(record)
        out: dict[str, list[dict]] = {}
        for record in self.data["devices"].values():
            if record["status"] != STATUS_ACTIVE or not record.get("friendly_name"):
                continue
            seen = set(record.get("predecessors") or [])
            for old in removed_by_name.get(_slug(record["friendly_name"]), []):
                if old["ieee"] != record["ieee"] and old["ieee"] not in seen:
                    seen.add(old["ieee"])
                    out.setdefault(record["ieee"], []).append(
                        {"ieee": old["ieee"], "name": _display_name(old), "removed_at": old.get("removed_at")}
                    )
        return out

    # ------------------------------------------------- single points of failure

    def community_report(self, ieee: str, choice: dict) -> dict | None:
        """The rating report for the model of `ieee`, as it will be posted, and the GitHub link that posts it.

        Covers every active device of that model on all networks. None for an unknown device or one
        without a model. `problems` lists choices that still need fixing (only the verdict can be missing).
        """
        if self.community is None:
            return None
        now = time.time()
        with self.lock:
            record = self.data["devices"].get(ieee)
            found = record_model(record) if record else None
            if found is None or record.get("type") == "Coordinator":
                return None
            same = [
                r
                for r in self.data["devices"].values()
                if r.get("status") == STATUS_ACTIVE and r.get("type") != "Coordinator" and record_model(r) == found
            ] or [record]
            bridge = ((self.data.get("bridges") or {}).get(record.get("instance")) or {}).get("info") or {}
            rows = model_reliability(same, self.data["events"], now)
            lqis = []
            for r in same:
                summary = self.metrics.summary(r["ieee"], now)
                value = summary.get("lqi_prev_7d")
                if value is None:
                    value = summary.get("lqi_24h")
                if isinstance(value, (int, float)):
                    lqis.append(float(value))
        # Keyed by the model as this install names it, so it stays the same once the model is in the file.
        reporter = self.community.reporter(model_key(*found))
        fields, comment, problems = build_report(same, bridge, rows[0] if rows else None, lqis, reporter, choice, now)
        return {
            "fields": [[k, v] for k, v in fields],
            "text": report_text(fields),
            "comment": comment,
            "url": issue_url(fields, comment),
            "problems": problems,
        }

    # ------------------------------------------------------ coordinator backup

    def _z2m_dirs(self) -> dict[str, str]:
        """Map base topic -> Zigbee2MQTT data folder, for folders inside the HA config directory."""
        found: dict[str, str] = {}
        try:
            entries = sorted(os.listdir(self.config_dir))
        except OSError:
            return found
        for entry in entries:
            folder = os.path.join(self.config_dir, entry)
            config = os.path.join(folder, "configuration.yaml")
            if not entry.lower().startswith("zigbee2mqtt") or not os.path.isfile(config):
                continue
            topic = "zigbee2mqtt"
            try:
                with open(config, encoding="utf-8") as handle:
                    in_mqtt = False
                    for line in handle:
                        if re.match(r"^\S", line):
                            in_mqtt = line.startswith("mqtt:")
                            continue
                        match = re.match(r"^\s+base_topic:\s*['\"]?([^'\"#\s]+)", line)
                        if in_mqtt and match:
                            topic = match.group(1)
                            break
            except OSError:
                continue
            found.setdefault(topic, folder)
        return found

    def coordinator_backups(self, force: bool = False) -> dict[str, dict]:
        if not force and time.time() - self._backups_at < BACKUP_REFRESH:
            return self._backups
        dirs = self._z2m_dirs()
        out: dict[str, dict] = {}
        for bt in self.base_topics:
            folder = dirs.get(bt)
            if not folder:
                out[bt] = {"found": False}
                continue
            path = os.path.join(folder, "coordinator_backup.json")
            info: dict[str, Any] = {
                "found": True,
                "folder": os.path.relpath(folder, self.config_dir),
                "exists": os.path.isfile(path),
            }
            if info["exists"]:
                try:
                    info["modified"] = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).isoformat(
                        timespec="seconds"
                    )
                    with open(path, "rb") as handle:
                        raw = handle.read()
                    backup = json.loads(raw)
                    meta = backup.get("metadata") or {}
                    info.update(
                        {
                            "date": self._to_utc_iso((meta.get("internal") or {}).get("date")) or info["modified"],
                            "source": meta.get("source"),
                            "channel": backup.get("channel"),
                            "pan_id": backup.get("pan_id"),
                            "devices": len(backup.get("devices") or []),
                            "has_key": bool((backup.get("network_key") or {}).get("key")),
                        }
                    )
                    self.backup_history.consider(bt, raw, backup, info["date"], time.time(), self._names_by_ieee())
                except (OSError, ValueError, AttributeError) as err:
                    info["error"] = f"Couldn't read it: {err}"
            out[bt] = info
        self._backups = out
        self._backups_at = time.time()
        return out

    def _names_by_ieee(self) -> dict[str, str]:
        with self.lock:
            return {ieee: name for ieee, record in self.data["devices"].items() if (name := _display_name(record))}

    def backup_copies(self) -> dict[str, list[dict]]:
        """Kept copies of each network's coordinator backup, newest first (see backup_history)."""
        names = self._names_by_ieee()
        return {bt: self.backup_history.rows(bt, names) for bt in self.base_topics}

    # English text of each backup problem; the sidebar translates the same keys (issue.<msg>.detail).
    BACKUP_TEXT = {
        "backup_missing": "There's no coordinator_backup.json in its data folder. Without it, a replacement coordinator can't take over the network and every device must be re-paired.",
        "backup_unreadable": "coordinator_backup.json exists but is unreadable. {error}",
        "backup_no_key": "coordinator_backup.json has no network key, so it can't restore the network.",
        "backup_channel": "The backup is for channel {backup} but the network runs on channel {network}. It's from an older network and wouldn't restore this one.",
        "backup_pan": "The backup's PAN ID ({backup}) doesn't match the running network ({network}). It's from an older network and wouldn't restore this one.",
        "backup_old": "The last coordinator backup is {n} days old. Zigbee2MQTT normally refreshes it at least daily, so writing it may be failing.",
    }

    def _backup_issue(self, bt: str, now: float) -> str | None:
        problem = self._backup_problem(bt, now)
        return self.BACKUP_TEXT[problem[0]].format(**problem[1]) if problem else None

    def _backup_problem(self, bt: str, now: float) -> tuple[str, dict] | None:
        """(msg, params) for the coordinator backup problem, if any."""
        info = self._backups.get(bt) or {}
        if not info.get("found"):
            return None
        bridge = (self.data.get("bridges") or {}).get(bt) or {}
        if not info.get("exists"):
            return "backup_missing", {}
        if info.get("error"):
            return "backup_unreadable", {"error": info["error"]}
        if not info.get("has_key"):
            return "backup_no_key", {}
        net = bridge.get("info") or {}
        try:
            pan = int(str(info.get("pan_id")), 16) if info.get("pan_id") is not None else None
        except ValueError:
            pan = None
        if net.get("channel") and info.get("channel") and int(net["channel"]) != int(info["channel"]):
            return "backup_channel", {"backup": info["channel"], "network": net["channel"]}
        if net.get("pan_id") is not None and pan is not None and int(net["pan_id"]) != pan:
            return "backup_pan", {"backup": pan, "network": net["pan_id"]}
        try:
            age = now - datetime.fromisoformat(info.get("date") or "").timestamp()
        except ValueError:
            age = 0
        uptime = (bridge.get("health") or {}).get("uptime_sec") or 0
        if age > BACKUP_MAX_AGE and bridge.get("state") == "online" and uptime > 86400:
            return "backup_old", {"n": int(age // 86400)}
        return None

    def _memory_problem(self, bridge: dict) -> dict | None:
        if bridge.get("state") == "offline":
            return None
        return memory_growth(
            bridge.get("memory_days") or [], self._today(), (bridge.get("health") or {}).get("memory_mb")
        )

    def network_hours(self, now: float) -> dict[str, dict]:
        """Link quality by hour of day per network (Metrics.network_hours), recomputed once an hour."""
        hour = int(now // 3600)
        if self._hours_at == hour:
            return self._hours
        with self.lock:
            members: dict[str, list[str]] = {}
            for ieee, record in self.data["devices"].items():
                if record.get("status") == STATUS_ACTIVE and record.get("type") != "Coordinator":
                    members.setdefault(record.get("instance") or "", []).append(ieee)
        out = {}
        for bt in self.base_topics:
            result = self.metrics.network_hours(members.get(bt, []), self.local_tz, now)
            if result:
                out[bt] = result
        self._hours, self._hours_at = out, hour
        return out

    @staticmethod
    def _dip_text(dip: dict) -> tuple[str, dict]:
        """English detail and params for a signal_dip issue."""
        start, end = dip["start"], (dip["start"] + dip["hours"]) % 24
        middle = (start + dip["hours"] / 2) % 24
        period = (
            "mornings"
            if 5 <= middle < 12
            else "afternoons"
            if 12 <= middle < 17
            else "evenings"
            if 17 <= middle < 22
            else "nights"
        )
        params = {"n": dip["devices"], "period": period, "start": f"{start:02d}:00", "end": f"{end:02d}:00"}
        text = (
            f"Signal drops across {params['n']} device{'' if params['n'] == 1 else 's'} most {period}, {params['start']}–{params['end']}. "
            "Something switches on then: Wi-Fi traffic on an overlapping channel, a microwave, "
            "or a USB 3 device near the coordinator."
        )
        return text, params

    def _network_issues(self, now: float) -> list[dict]:
        out = []
        disabled = set(self.options.get("disabled_checks") or [])
        hours = self.network_hours(now) if "signal_dip" not in disabled else {}
        for bt in self.base_topics:
            bridge = (self.data.get("bridges") or {}).get(bt) or {}
            zha = _stack_of(bt) == "zha"
            matter = _stack_of(bt) == "matter"
            label = (
                "Matter"
                if matter
                else "ZHA"
                if zha
                else "Main"
                if bt == "zigbee2mqtt"
                else bt.replace("zigbee2mqtt_", "").replace("_", " ").capitalize()
            )
            if bridge.get("state") == "offline" and "network_offline" not in disabled:
                try:
                    age = now - datetime.fromisoformat(bridge["since"]).timestamp()
                except (TypeError, ValueError):
                    age = 0
                if age >= 120:
                    mins = int(age // 60)
                    issue = {
                        "key": f"network_offline:{bt}",
                        "kind": "network_offline",
                        "label": "Network offline",
                        "severity": "warning",
                        "ieee": f"network:{bt}",
                        "title": "Matter offline" if matter else "ZHA offline" if zha else "Zigbee2MQTT offline",
                        "detail": f"Not responding for {mins} min. Every device on this network is unreachable. Check the coordinator's power and network connection.",
                        "msg": "network_offline_matter"
                        if matter
                        else "network_offline_zha"
                        if zha
                        else "network_offline",
                        "params": {"seconds": int(age)},
                        "name": f"{label} network",
                        "instance": bt,
                    }
                    usual = (
                        expected_outage.pattern(bridge.get("outages"), now, self.local_tz)
                        if self.options.get("expected_outages", True)
                        else None
                    )
                    if expected_outage.expected(usual, now - age, now, self.local_tz):
                        # Listed, not alerted (see plan_alerts): it does this most days and comes back.
                        around = expected_outage.clock(usual["at"])
                        issue.update(
                            severity="info",
                            expected=True,
                            title="Expected outage",
                            detail=f"Goes offline most days around {around} for about {usual['minutes']} min. "
                            f"Not responding for {mins} min now.",
                            msg="network_expected",
                            params={"seconds": int(age), "time": around, "minutes": usual["minutes"]},
                        )
                    out.append(issue)
            if (bridge.get("info") or {}).get("restart_required") and "restart_required" not in disabled:
                out.append(
                    {
                        "key": f"restart_required:{bt}",
                        "kind": "restart_required",
                        "label": "Restart needed",
                        "severity": "info",
                        "ieee": f"network:{bt}",
                        "title": "Restart needed",
                        "detail": "Zigbee2MQTT says it needs a restart to apply a settings change.",
                        "msg": "restart_required",
                        "params": {},
                        "name": f"{label} network",
                        "instance": bt,
                    }
                )
            if "pairing_open" not in disabled:
                issue = self.pairing_open_issue(bt, now)
                if issue:
                    out.append(issue)
            growth = None if "z2m_memory" in disabled else self._memory_problem(bridge)
            if growth:
                out.append(
                    {
                        "key": f"z2m_memory:{bt}",
                        "kind": "z2m_memory",
                        "label": "Memory climbing",
                        "severity": "info",
                        "ieee": f"network:{bt}",
                        "title": "Zigbee2MQTT memory climbing",
                        "detail": f"Memory up {growth['percent']} % in {growth['days']} days. Restarting Zigbee2MQTT frees it; if it keeps climbing, check its log for an extension or converter that leaks.",
                        "msg": "z2m_memory",
                        "params": growth,
                        "name": f"{label} network",
                        "instance": bt,
                    }
                )
            if "coordinator_backup" not in disabled:
                problem = self._backup_problem(bt, now)
                if problem:
                    out.append(
                        {
                            "key": f"coordinator_backup:{bt}",
                            "kind": "coordinator_backup",
                            "label": "Coordinator backup",
                            "severity": "warning",
                            "ieee": f"network:{bt}",
                            "title": "Coordinator backup",
                            "detail": self.BACKUP_TEXT[problem[0]].format(**problem[1]),
                            "msg": problem[0],
                            "params": problem[1],
                            "name": f"{label} network",
                            "instance": bt,
                        }
                    )
            if "upgrade_worse" not in disabled:
                issue = self._upgrade_issue(bt, label, now)
                if issue:
                    out.append(issue)
            if self.last_seen_off(bt) and f"last_seen_off:{bt}" not in (self.data.get("dismissed_advice") or []):
                out.append(
                    {
                        "key": f"last_seen_off:{bt}",
                        "kind": "last_seen_off",
                        "label": "last_seen is off",
                        "severity": "info",
                        "ieee": f"network:{bt}",
                        "title": "last_seen is off in Zigbee2MQTT",
                        "detail": LAST_SEEN_TEXT,
                        "msg": "last_seen_off",
                        "params": {},
                        "name": f"{label} network",
                        "instance": bt,
                    }
                )
            dips = (hours.get(bt) or {}).get("dips") or []
            if dips:
                text, params = self._dip_text(dips[0])
                out.append(
                    {
                        "key": f"signal_dip:{bt}",
                        "kind": "signal_dip",
                        "label": "Daily signal dip",
                        "severity": "info",
                        "ieee": f"network:{bt}",
                        "title": "Daily signal dip",
                        "detail": text,
                        "msg": "signal_dip",
                        "params": params,
                        "name": f"{label} network",
                        "instance": bt,
                    }
                )
        return out

    def stale_retained(self) -> list[dict] | None:
        """Retained topics from the last broker scan that belong to no current device.

        None until there is a scan and every network has sent its device list: before that, the
        current names aren't known and every topic would look stale."""
        store = self.retained
        if store is None or store.scanned_at is None or not self.lists_ready.is_set():
            return None
        topics = store.items()
        with self.lock:
            return stale_entries(topics, self.data["devices"], self.names, self.groups, self.mqtt_topics)

    def _retained_issues(self) -> list[dict]:
        if "stale_retained" in set(self.options.get("disabled_checks") or []):
            return []
        stale = self.stale_retained()
        if not stale:
            return []
        n = len(stale)
        example = stale[0]["topic"]
        return [
            {
                "key": "stale_retained:broker",
                "kind": "stale_retained",
                "label": "Stale retained messages",
                "severity": "info",
                "ieee": "network:broker",
                "title": "Stale retained messages",
                "detail": f"{n} retained message{'s belong' if n != 1 else ' belongs'} to no current device, "
                f"e.g. {example}. Review on Network → Broker.",
                "msg": "stale_retained",
                "params": {"n": n, "topic": example},
                "name": "MQTT broker",
                "instance": None,
            }
        ]

    def retained_payload(self) -> dict:
        """The Broker page's list of stale retained messages (first 500) and the scan state."""
        store = self.retained
        status = store.status() if store is not None else {"scanned_at": None, "error": None, "truncated": False}
        stale = self.stale_retained()
        return {
            **status,
            "ready": stale is not None,
            "total": len(stale or []),
            "counts": retained_counts(stale or []),
            "stale": (stale or [])[:500],
        }

    def last_seen_off(self, base_topic: str) -> bool:
        """True when the network's Zigbee2MQTT settings say advanced.last_seen is "disable".
        False while its settings haven't been seen, or don't mention it."""
        value = ((self.data.get("z2m_config") or {}).get(base_topic) or {}).get("advanced.last_seen")
        try:
            return value is not None and json.loads(value) in ("disable", False)
        except ValueError:
            return False

    def dismiss_advice(self, key: str) -> bool:
        """Hide one piece of settings advice (such as last_seen_off:<base topic>) for good."""
        with self.lock:
            if not any(i["key"] == key and i["kind"] in ADVICE_KINDS for i in self.issues):
                return False
            dismissed = self.data.setdefault("dismissed_advice", [])
            if key not in dismissed:
                dismissed.append(key)
            self.issues = [i for i in self.issues if i["key"] != key]
            self._changed(structural=True)
            return True

    def turn_on_last_seen(self, base_topic: str) -> dict:
        """Ask Zigbee2MQTT to add a last_seen time to every message (advanced.last_seen: ISO_8601)."""
        if base_topic not in self.base_topics:
            return {"ok": False, "error": "Unknown network"}
        try:
            reply = self.request(base_topic, "options", {"options": {"advanced": {"last_seen": "ISO_8601"}}}, 15)
        except RuntimeError as err:
            return {"ok": False, "error": str(err)}
        if reply is None:
            return {"ok": False, "error": "Zigbee2MQTT didn't answer within 15 s."}
        if reply.get("status") != "ok":
            return {"ok": False, "error": friendly_z2m_error(reply.get("error"))}
        _LOGGER.info("Turned on last_seen in Zigbee2MQTT (%s)", base_topic)
        return {"ok": True, "restart_required": bool((reply.get("data") or {}).get("restart_required"))}

    def radio(self) -> dict:
        """Zigbee/Wi-Fi channel overlap analysis."""
        zfreq = lambda c: 2405 + 5 * (int(c) - 11)
        wfreq = lambda n: 2484 if int(n) == 14 else 2407 + 5 * int(n)
        nets = []
        for bt in self.base_topics:
            info = ((self.data.get("bridges") or {}).get(bt) or {}).get("info") or {}
            if info.get("channel"):
                nets.append(
                    {
                        "topic": bt,
                        "channel": int(info["channel"]),
                        "pan_id": info.get("pan_id"),
                        "mhz": zfreq(info["channel"]),
                    }
                )
        wifi = []
        for part in str(self.options.get("wifi_channels") or "").replace(";", ",").split(","):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= 14 and int(part) not in wifi:
                wifi.append(int(part))
        findings = []
        label = lambda bt: (
            "Main" if bt == "zigbee2mqtt" else bt.replace("zigbee2mqtt_", "").replace("_", " ").capitalize()
        )

        def score(channel: int, avoid: list[int]) -> float:
            f = zfreq(channel)
            wifi_gap = min([abs(f - wfreq(w)) for w in wifi] or [50])
            zig_gap = min([abs(channel - a) for a in avoid] or [16])
            return min(wifi_gap, 40) + min(zig_gap, 4) * 6 - (10 if channel == 26 else 0)

        for net in nets:
            for w in wifi:
                gap = abs(net["mhz"] - wfreq(w))
                if gap <= 12:
                    best_w = max((1, 6, 11), key=lambda c: abs(net["mhz"] - wfreq(c)))
                    findings.append(
                        {
                            "sev": 2,
                            "text": f"{label(net['topic'])} network (Zigbee channel {net['channel']}, {net['mhz']} MHz) overlaps Wi-Fi channel {w}. The easiest fix is moving your Wi-Fi to channel {best_w} in the router settings. Changing the Zigbee channel means re-pairing every device.",
                            "msg": "overlap",
                            "params": {
                                "net": net["topic"],
                                "channel": net["channel"],
                                "mhz": net["mhz"],
                                "wifi": w,
                                "best": best_w,
                            },
                        }
                    )
                elif gap <= 16:
                    findings.append(
                        {
                            "sev": 1,
                            "text": f"{label(net['topic'])} network (Zigbee channel {net['channel']}) sits just beside Wi-Fi channel {w}. Usually fine, but busy Wi-Fi can still cause occasional drops.",
                            "msg": "beside",
                            "params": {"net": net["topic"], "channel": net["channel"], "wifi": w},
                        }
                    )
        by_channel: dict[int, list] = {}
        for net in nets:
            by_channel.setdefault(net["channel"], []).append(net)
        for channel, group in by_channel.items():
            if len(group) > 1:
                others = [n["channel"] for n in nets]
                candidates = [c for c in range(11, 27) if c != channel]
                best = max(candidates, key=lambda c: score(c, others))
                names = " and ".join(label(n["topic"]) for n in group)
                findings.append(
                    {
                        "sev": 1,
                        "text": f"{names} both use Zigbee channel {channel}. Separate networks on one channel share its airtime, which matters if their coordinators or devices are within range of each other. If one of them has dropouts, moving the smaller network to channel {best} would separate them. That needs every device on it re-paired, so only do it if there's a real problem.",
                        "msg": "shared",
                        "params": {"nets": [n["topic"] for n in group], "channel": channel, "best": best},
                    }
                )
        if nets and not findings:
            findings.append(
                {
                    "sev": 0,
                    "text": "No overlap: your Zigbee channels are clear of the Wi-Fi channels you've entered"
                    + ("" if wifi else " (add your Wi-Fi channels in Settings to check them too)")
                    + ".",
                    "msg": "clear" if wifi else "clear_no_wifi",
                    "params": {},
                }
            )
        return {"networks": nets, "wifi": wifi, "findings": findings}

    def _leaves_since(self, since: str) -> dict[str, int]:
        """How many times each device left the network since `since` (an ISO time)."""
        leaves: dict[str, int] = {}
        for event in reversed(self.data["events"]):
            if (event.get("time") or "") < since:
                break
            if event.get("kind") == "left":
                leaves[event["ieee"]] = leaves.get(event["ieee"], 0) + 1
        return leaves

    def _context(self, now: float) -> dict[str, dict]:
        """Per-device extras for the health checks: recent outages/leaves, parent, single points of failure."""
        day_ago = datetime.fromtimestamp(now - 86400, tz=timezone.utc).isoformat(timespec="seconds")
        leaves = self._leaves_since(day_ago)
        spof = {item["ieee"]: item for bt in self.base_topics for item in self.single_points(bt)}
        bridges = self.data.get("bridges") or {}
        down = {bt for bt, bridge in bridges.items() if (bridge or {}).get("state") == "offline"}
        today = self._today()
        out: dict[str, dict] = {}
        for ieee, record in self.data["devices"].items():
            extra: dict[str, Any] = {
                "outages_24h": sum(1 for o in record.get("outages") or [] if (o.get("start") or "") >= day_ago),
                "leaves_24h": leaves.get(ieee, 0),
                "started": self._listening_since(record),
                "network_down": record.get("instance") in down,
                "battery_device": self.is_battery_device(record),
            }
            parent = (record.get("routing") or {}).get("parent") or {}
            if parent.get("ieee"):
                extra["parent"] = {
                    "name": _display_name(self.data["devices"].get(parent["ieee"]) or {}) or parent["ieee"],
                    "lqi": parent.get("lqi"),
                }
            if ieee in spof:
                extra["single_point"] = spof[ieee]
            # Only end devices pick a parent to route through; a router's tree parent is just
            # whoever let it join and says little about how its traffic flows.
            if record.get("type") == "EndDevice":
                extra["router_hops"] = hop_summary(self._hop_rows(record, now))
            overdue = [
                {"title": task["title"], "days": (today - due).days}
                for task in record.get("maintenance") or []
                if (due := task_due(task)) < today
            ]
            if overdue:
                extra["maintenance_overdue"] = sorted(overdue, key=lambda x: -x["days"])
            recent = self._recent.get(ieee)
            if recent:
                extra["recent_messages"] = list(recent)
            extra["z2m_availability"] = ((bridges.get(record.get("instance")) or {}).get("info") or {}).get(
                "availability"
            )
            if record.get("payload_change"):
                extra["payload_usage"] = self._payload_usage(record)
            ends = warranty_end(record)
            if ends is not None and 0 <= (ends - today).days <= WARRANTY_SOON_DAYS:
                extra["warranty"] = {"until": ends.isoformat(), "days": (ends - today).days}
            out[ieee] = extra
        for topic, entry in sorted(self.retained_commands.items()):
            owner = self._retained_command_device(entry)
            if owner in out:
                out[owner].setdefault("retained_commands", []).append(
                    {"topic": topic, "payload": entry["payload"], "seen": entry["seen"]}
                )
        return out

    def _health_changed(self) -> None:
        """Something a check looks at was set. The checks run at once, or (defer_health) just before
        the issues are next read: thirty settings in a row then cost one pass over the devices, not thirty."""
        if self.defer_health:
            self._health_pending = True
        else:
            self.evaluate_health()

    def flush_health(self) -> None:
        if self._health_pending:
            self.evaluate_health()

    def evaluate_health(self) -> tuple[list[dict], bool]:
        self._health_pending = False
        now = time.time()
        self.coordinator_backups()
        with self.lock:
            self.expire_commands(now)
            suggestions = self._suggestions()
            context = self._context(now)
            issues = []
            lqi_by_net: dict[str, list[float]] = {}
            # Devices of a network not listened to keep their history but get no checks.
            live = {ieee: r for ieee, r in self.data["devices"].items() if not self.is_frozen(r)}
            summaries: dict[str, dict] = {}
            self._summaries = (now, summaries)
            for record in live.values():
                summaries[record["ieee"]] = self.metrics.summary(record["ieee"], now)
                summary = {**summaries[record["ieee"]], **context.get(record["ieee"], {})}
                if (
                    record["status"] == STATUS_ACTIVE
                    and record.get("type") != "Coordinator"
                    and isinstance(summary.get("lqi_24h"), (int, float))
                ):
                    lqi_by_net.setdefault(record.get("instance") or "", []).append(summary["lqi_24h"])
                info = self._rhythm(record, now) if self._uses_rhythm(record) else None
                profile = self.report_profile(record, summary, now, info)
                limit = profile["every"] or 0
                self._report_limits[record["ieee"]] = limit
                summary["report_every"] = limit
                if profile["source"] == "learned" and info:
                    summary["report_source"] = "learned"
                    # The silence the limit that fired was built from: the one for this time of week.
                    summary["rhythm_longest"] = info["slot_longest"]
                for issue in device_issues(record, summary, self.options, now, suggestions.get(record["ieee"], [])):
                    issue["name"] = _display_name(record) or record["ieee"]
                    issue["instance"] = record["instance"]
                    issues.append(issue)
            group_router_outages(issues, live, self.options, now)
            group_power_loss(issues, live, self.options, now)
            issues.extend(self._network_issues(now))
            issues.extend(self._retained_issues())
            issues.extend(self._group_issues(now))
            for issue in naming_issues(list(live.values()), self.options, now):
                record = self.data["devices"][issue["ieee"]]
                issue["name"] = _display_name(record) or record["ieee"]
                issue["instance"] = record["instance"]
                issues.append(issue)
            notes = firmware_notes.clean_notes(self.data.get("firmware_notes"))
            for issue in firmware_notes.note_issues(notes, live, self.options, now):
                record = self.data["devices"][issue["ieee"]]
                issue["name"] = _display_name(record) or record["ieee"]
                issue["instance"] = record["instance"]
                issues.append(issue)
            if self.community is not None:
                for issue in community_issues(list(live.values()), self.community, self.options, now):
                    record = self.data["devices"][issue["ieee"]]
                    issue["name"] = _display_name(record) or record["ieee"]
                    issue["instance"] = record["instance"]
                    issues.append(issue)
            order = {"warning": 0, "info": 1}
            issues.sort(key=lambda i: (order.get(i["severity"], 2), i["name"].lower()))

            def shape(items: list[dict]) -> list:
                return [(i["key"], i.get("behind"), i.get("devices"), i.get("power_devices")) for i in items]

            changed = shape(issues) != shape(self.issues)
            self.issues = issues
            if self._update_mesh_scores(issues, lqi_by_net):
                changed = True
            if changed:
                self.revision += 1
        return issues, changed

    def _mesh_inputs(self, bt: str, issues: list[dict], lqis: list[float]) -> dict | None:
        """The four parts of a network's mesh score (see mesh_score), or None without devices."""
        devs = [
            r
            for r in self.data["devices"].values()
            if r.get("instance") == bt and r["status"] == STATUS_ACTIVE and r.get("type") != "Coordinator"
        ]
        scanned_at = (self.data.get("networkmap") or {}).get(bt, {}).get("scanned_at")
        backup = backup_of = None
        if scanned_at:
            scanned = {r["ieee"] for r in devs if (r.get("routing") or {}).get("scanned_at") == scanned_at}
            if scanned:
                cut: set[str] = set()
                for spof in self.single_points(bt):
                    cut.update(x["ieee"] for x in spof["routers"])
                    cut.update(spof["device_list"])
                backup_of = len(scanned)
                backup = len(scanned - cut)
        problems = sum(
            1 for i in issues if i["severity"] == "warning" and i.get("instance") == bt and not i.get("behind")
        )
        return mesh_score(
            online=sum(1 for r in devs if r.get("availability") != "offline"),
            total=len(devs),
            lqi=sum(lqis) / len(lqis) if lqis else None,
            backup=backup,
            backup_of=backup_of,
            problems=problems,
        )

    def _update_mesh_scores(self, issues: list[dict], lqi_by_net: dict[str, list[float]]) -> bool:
        """Work out each network's mesh score and keep today's in the history. True if a score changed."""
        today = self._today()
        history = self.data.setdefault("mesh_scores", {})
        out: dict[str, dict] = {}
        for bt in self.base_topics:
            current = self._mesh_inputs(bt, issues, lqi_by_net.get(bt, []))
            if current is None:
                continue
            rows = history.setdefault(bt, [])
            if record_mesh_score(rows, today, current):
                self.dirty = True
            out[bt] = mesh_summary(rows, today, current)
        changed = {bt: s["score"] for bt, s in out.items()} != {bt: s["score"] for bt, s in self.mesh.items()}
        self.mesh = out
        return changed

    # ------------------------------------------------------ leftovers in HA

    def leftover_context(self) -> tuple[bool, dict[str, dict]]:
        """What the tracker knows about each IEEE, for ha.find_leftovers.

        Not ready until every tracked network has sent its device list since the
        app started: before that, every device would look unknown."""
        with self.lock:
            ready = self.lists_received >= set(self.base_topics)
            devices = self.data["devices"]
            known = {}
            for ieee, record in devices.items():
                successor = record.get("successor")
                known[ieee] = {
                    "active": record["status"] == STATUS_ACTIVE,
                    "name": _display_name(record),
                    "removed_at": record.get("removed_at"),
                    "successor": {
                        "ieee": successor,
                        "name": _display_name(devices.get(successor) or {}) or successor,
                    }
                    if successor
                    else None,
                }
            return ready, known

    def set_ha_leftovers(self, leftovers: list[dict]) -> None:
        with self.lock:
            if leftovers != self.ha_leftovers:
                self.ha_leftovers = leftovers
                self.revision += 1

    # ------------------------------------------- what uses a device in HA

    def usage_wanted(self) -> list[str]:
        """Devices worth knowing the automations, scripts and scenes of: those with an
        issue first, then critical ones. Only devices linked to an HA device."""
        with self.lock:
            devices = self.data["devices"]
            wanted = dict.fromkeys(i["ieee"] for i in self.issues if i.get("ieee") in devices)
            wanted.update(dict.fromkeys(ieee for ieee, record in devices.items() if record.get("critical")))
            return [ieee for ieee in wanted if ieee in self.ha_device_ids][:USAGE_LIMIT]

    def usage_stale(self, ieees, max_age: float = USAGE_MAX_AGE) -> list[str]:
        """The given devices (linked to HA) never looked up or looked up longer than max_age ago."""
        now = time.time()
        with self.lock:
            return [
                ieee
                for ieee in dict.fromkeys(ieees)
                if ieee in self.ha_device_ids and now - (self.ha_usage.get(ieee) or {}).get("at", 0) >= max_age
            ][:USAGE_LIMIT]

    def usage_due(self) -> list[str]:
        """Devices with an issue that opened since the last call and whose lookup is stale."""
        with self.lock:
            keys = {i["key"]: i["ieee"] for i in self.issues if i.get("ieee") in self.data["devices"]}
            opened = [ieee for key, ieee in keys.items() if key not in self._usage_keys]
            self._usage_keys = set(keys)
        return self.usage_stale(opened)

    def set_ha_usage(self, found: dict[str, dict], keep: set[str] | None = None) -> None:
        """Store lookups by IEEE. With keep, forget devices outside it that weren't just looked up."""
        now = time.time()

        def shown(usage: dict) -> dict:
            return {key: usage.get(key) or [] for key in USAGE_KEYS}

        with self.lock:
            before = {ieee: shown(u) for ieee, u in self.ha_usage.items()}
            for ieee, usage in found.items():
                self.ha_usage[ieee] = {**shown(usage), "at": now}
            if keep is not None:
                for ieee in [i for i in self.ha_usage if i not in keep and i not in found]:
                    del self.ha_usage[ieee]
            if before != {ieee: shown(u) for ieee, u in self.ha_usage.items()}:
                self.revision += 1

    def used_by(self, ieee: str) -> dict | None:
        """What uses the device, for the snapshot; None when nothing does or it isn't known."""
        with self.lock:
            usage = self.ha_usage.get(ieee)
            if not usage or not any(usage.get(key) for key in USAGE_KEYS):
                return None
            return {key: list(usage.get(key) or []) for key in USAGE_KEYS}

    def usage_line(self, ieee: str) -> str | None:
        """One line for a notification, e.g. "Used by: Hall lights at night, Leak shut-off (+2 more)"."""
        usage = self.used_by(ieee)
        if not usage:
            return None
        names = []
        for key in USAGE_KEYS:
            for item in usage[key]:
                name = " ".join(str(item.get("name") or item.get("entity_id") or "").split())
                names.append(name if len(name) <= USAGE_NAME_MAX else name[: USAGE_NAME_MAX - 1].rstrip() + "\u2026")
        line = "Used by: " + ", ".join(names[:USAGE_LINE_NAMES])
        if len(names) > USAGE_LINE_NAMES:
            line += f" (+{len(names) - USAGE_LINE_NAMES} more)"
        return line

    # ----------------------------------------------------------------- public

    def recent_messages(self, ieee: str) -> list[dict] | None:
        """The device's last messages, newest first, or None for an unknown device."""
        with self.lock:
            if ieee not in self.data["devices"]:
                return None
            return self.messages.recent(ieee)

    def _oui_vendor(self, ieee: str) -> str | None:
        return self.oui.get(ieee[2:8].upper()) if ieee.startswith("0x") else None

    def save_firmware_note(self, raw: Any) -> dict:
        """Add or change a firmware note (see firmware_notes.py)."""
        with self.lock:
            notes = firmware_notes.clean_notes(self.data.get("firmware_notes"))
            note, error = firmware_notes.save_note(notes, raw, _now())
            if error:
                return {"ok": False, "error": error}
            self.data["firmware_notes"] = notes
            self._changed(structural=True)
        self._health_changed()
        return {"ok": True, "note": note}

    def delete_firmware_note(self, note_id: str) -> bool:
        with self.lock:
            notes = firmware_notes.clean_notes(self.data.get("firmware_notes"))
            if not firmware_notes.delete_note(notes, str(note_id or "")):
                return False
            self.data["firmware_notes"] = notes
            self._changed(structural=True)
        self._health_changed()
        return True

    def firmware_note_block(self, record: dict | None) -> str | None:
        """Why an update shouldn't be in "Update all" or the overnight run: you noted its version."""
        with self.lock:
            notes = firmware_notes.clean_notes(self.data.get("firmware_notes"))
            return firmware_notes.ota_block(notes, record, self.data["devices"])

    def incidents(self) -> list[dict]:
        """Grouped problem events (see incidents.py), newest first. Call with the lock held.

        Cached until the events list or a device/bridge state changes (every such change bumps
        `revision`), so the snapshot doesn't regroup thousands of events on every request.
        """
        events = self.data["events"]
        key = (len(events), id(events[-1]) if events else None, self.revision)
        if self._incidents_cache and self._incidents_cache[0] == key:
            return self._incidents_cache[1]
        result = build_incidents(events, self.data["devices"], self.data.get("bridges") or {})
        self._incidents_cache = (key, result)
        return result

    def snapshot_json(self, gzipped: bool = False) -> bytes:
        """The page's data. gzipped=True gives it compressed (about a tenth of the size)."""
        now = time.time()
        entry = self._snapshot
        if not (entry and entry[1] == self.revision and 0 <= now - entry[0] < self.snapshot_seconds):
            entry = [now, self.revision, self._build_snapshot(now), None]
            # Only kept when nothing changed while it was built.
            if self.snapshot_seconds and entry[1] == self.revision:
                self._snapshot = entry
        if not gzipped:
            return entry[2]
        if entry[3] is None:
            entry[3] = gzip.compress(entry[2], compresslevel=5)
        return entry[3]

    def _build_snapshot(self, now: float) -> bytes:
        """The page's data, built in turns with the lock rather than in one long hold: a message
        that arrives meanwhile waits for one device or one section, not for the whole build. Each
        part is encoded while its turn lasts, so nothing changes under it afterwards."""
        self.flush_health()
        with self.lock:
            self.expire_commands(now)
            suggestions = self._suggestions()
            issues_by_device: dict[str, list[dict]] = {}
            for issue in self.issues:
                issues_by_device.setdefault(issue["ieee"], []).append(issue)
            ieees = list(self.data["devices"])
            version = self.version
        devices: list[str] = []
        for ieee in ieees:
            with self.lock:
                record = self.data["devices"].get(ieee)
                if record is not None:
                    item = self._snapshot_device(record, now, issues_by_device, suggestions)
                    devices.append(_dumps(item, ensure_ascii=False))
            _give_way()
        sections: list[str] = []
        for build in (self._snapshot_state, self._snapshot_lists, self._snapshot_rest):
            with self.lock:
                sections.append(_dumps(build(now), ensure_ascii=False)[1:-1])
            _give_way()
        sections.append(f'"version":{json.dumps(version)}')
        body = '{"devices":[' + ",".join(devices) + "]," + ",".join(part for part in sections if part) + "}"
        return body.encode("utf-8")

    def _snapshot_device(self, record: dict, now: float, issues_by_device: dict, suggestions: dict) -> dict:
        ieee = record["ieee"]
        item = dict(record)
        # Only the health check reads it; the issue carries what the page shows.
        item.pop("odd_readings", None)
        # The learning counts only matter to the tracker.
        item.pop("payload_keys", None)
        item.pop("reporting_sent", None)
        item["metrics"] = self.metrics.summary(ieee, now)
        item["uptime_30d"] = uptime(record, now)
        item["oui_vendor"] = self._oui_vendor(ieee)
        item["ha_device_id"] = self.ha_device_ids.get(ieee)
        item["battery_notes"] = self.ha_battery.get(ieee)
        used_by = self.used_by(ieee)
        if used_by:
            item["used_by"] = used_by
        item["issues"] = issues_by_device.get(ieee, [])
        item["battery_record"] = self.battery_summary(record) if self.is_battery_device(record) else None
        item["suggestions"] = suggestions.get(ieee, [])
        bridge_info = (self.data.get("bridges") or {}).get(record.get("instance")) or {}
        item["rhythm"] = self._rhythm(record, now)
        item["reporting_profile"] = self.report_profile(record, item["metrics"], now, item["rhythm"])
        item["offline_timing"] = offline_timing(
            record,
            (bridge_info.get("info") or {}).get("availability"),
            self.options,
            now,
            item["reporting_profile"]["every"] or 0,
        )
        item["can_ping"] = self.can_ping(record) is None
        item["reporting_audit"] = self.reporting_audit(record, item["metrics"].get("msg_rate_6h"))
        item["kind"] = kind_of(record)
        item["frozen"] = self.is_frozen(record)
        if self.community is not None:
            item["community_key"] = self.community.key_for(record)
        return item

    def _snapshot_state(self, now: float) -> dict:
        return {
            "events": self.data["events"],
            "incidents": self.incidents(),
            "firmware_notes": firmware_notes.clean_notes(self.data.get("firmware_notes")),
            "firmware_note_devices": firmware_notes.matches(
                firmware_notes.clean_notes(self.data.get("firmware_notes")), self.data["devices"]
            ),
            "instances": self.base_topics,
            # "zha" only when no Zigbee2MQTT network is read: the UI then hides what needs it.
            "stack": "z2m" if any(_stack_of(bt) == "z2m" for bt in self.base_topics) else "zha",
            "imported": self.data.get("imported"),
            "issues": self.issues,
            "networkmap": self.data.get("networkmap") or {},
            "permit": self.permit,
        }

    def _snapshot_lists(self, now: float) -> dict:
        # The Upkeep lists are about the networks in use, not frozen devices.
        live = [r for r in self.data["devices"].values() if not self.is_frozen(r)]
        return {
            "battery_shopping": self.battery_shopping(60),
            "model_reliability": model_reliability(live, self.data["events"], now),
            "device_ages": device_ages(live, self.data["events"], self._today()),
            "community": self.community.snapshot(live, bool(self.options.get("community_ratings_fetch", True)))
            if self.community is not None
            else None,
            "ha_leftovers": self.ha_leftovers,
            "retained": self.retained_payload(),
            "broker": self.broker_stats.snapshot(bool(self.options.get("broker_stats", True)))
            if self.broker_stats is not None
            else None,
            "battery_stock": dict(self.data.get("battery_stock") or {}),
            "battery_prices": clean_battery_prices(self.data.get("battery_prices")),
            "running_costs": self.running_costs(now),
            "battery_brands": self.battery_brands(),
            "battery_brand_list": list(self.brand_list()),
            "maintenance": self.maintenance_list(),
            "warranties": self.warranty_list(),
            "bridges": self.data.get("bridges") or {},
            "radio": self.radio(),
            "network_hours": self.network_hours(now),
            "upgrade_impact": self.upgrade_impact(now),
            "single_points": {bt: self.single_points(bt) for bt in self.base_topics},
            "router_placement": {bt: self.router_placement(bt, now) for bt in self.base_topics},
        }

    def _snapshot_rest(self, now: float) -> dict:
        return {
            "backups": self._backups,
            "backup_copies": self.backup_copies(),
            "mesh_scores": self.mesh,
            "report_defaults": profile_defaults(self.data.get("report_profile_defaults")),
            "groups": self.groups,
            "group_health": self.group_health(now),
            "scan_times": {bt: [x["at"] for x in self.scans.get(bt) or []] for bt in self.base_topics},
            "pairing": {
                bt: {**s, "joined": list(s["joined"]), "progress": dict(s.get("progress") or {})}
                for bt, s in self.pairing_sessions.items()
            },
            "panels": self.options.get("_panels", {}),
            "options": {k: v for k, v in self.options.items() if not k.startswith("_") and k not in SECRET_OPTIONS},
            "alerts_paused": self.alert_pause_state(),
        }

    def prometheus_snapshot(self) -> dict[str, Any]:
        """The few values the /metrics endpoint needs, read under the lock in one pass."""
        self.flush_health()
        now = time.time()
        with self.lock:
            devices = []
            for record in self.data["devices"].values():
                if record.get("type") == "Coordinator" or self.is_frozen(record):
                    continue
                summary = self.metrics.summary(record["ieee"], now) if record["status"] == STATUS_ACTIVE else {}
                devices.append(
                    {
                        "ieee": record["ieee"],
                        "name": _display_name(record),
                        "instance": record.get("instance"),
                        "status": record["status"],
                        "type": record.get("type"),
                        "power_source": record.get("power_source"),
                        "vendor": record.get("vendor"),
                        "model": record.get("model"),
                        "area": record.get("area"),
                        "availability": record.get("availability"),
                        "lqi": record.get("lqi"),
                        "battery": record.get("battery"),
                        "last_seen": record.get("last_seen"),
                        "lqi_24h": summary.get("lqi_24h"),
                        "msg_24h": summary.get("msg_24h"),
                        "battery_days": summary.get("battery_estimate_days"),
                        "uptime_30d": uptime(record, now) if record["status"] == STATUS_ACTIVE else None,
                    }
                )
            return {
                "devices": devices,
                "issues": [{"ieee": i["ieee"], "kind": i["kind"], "severity": i["severity"]} for i in self.issues],
                "bridges": {
                    bt: ((self.data.get("bridges") or {}).get(bt) or {}).get("state") for bt in self.base_topics
                },
                "alerts_paused": self.alerts_paused(),
                "mesh_scores": {bt: s["score"] for bt, s in self.mesh.items()},
            }

    def export_csv(self) -> bytes:
        now = time.time()
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        columns = [
            "ieee",
            "name",
            "former_names",
            "status",
            "network",
            "vendor",
            "model",
            "model_id",
            "manufacturer",
            "radio_vendor",
            "description",
            "type",
            "power_source",
            "firmware",
            "first_seen",
            "last_seen",
            "removed_at",
            "lqi_24h",
            "battery",
            "battery_type",
            "battery_quantity",
            "battery_installed",
            "battery_last_replaced",
            "battery_replacements",
            "battery_brand",
            "uptime_30d",
            "commands_24h",
            "command_typical_ms_24h",
            "commands_missed_24h",
            "area",
            "ha_name",
            *ASSET_FIELDS,
            "critical",
            "note",
        ]
        writer.writerow(columns)
        with self.lock:
            for record in sorted(self.data["devices"].values(), key=lambda r: (_display_name(r) or "~").lower()):
                if record.get("type") == "Coordinator":
                    continue
                name = _display_name(record) or ""
                former = sorted(
                    {
                        n["name"]
                        for n in record.get("names") or []
                        if n.get("name") and n["name"] != name and not IEEE_RE.match(n["name"].lower())
                    }
                )
                summary = self.metrics.summary(record["ieee"], now)
                cmd = summary.get("cmd") or {}
                battery = self.battery_summary(record)
                asset = record.get("asset") or {}
                writer.writerow(
                    _csv_safe(
                        [
                            record["ieee"],
                            name,
                            "; ".join(former),
                            record["status"],
                            record.get("instance") or "",
                            record.get("vendor") or "",
                            record.get("model") or "",
                            record.get("model_id") or "",
                            record.get("manufacturer") or "",
                            self._oui_vendor(record["ieee"]) or "",
                            record.get("description") or "",
                            record.get("type") or "",
                            record.get("power_source") or "",
                            " ".join(str(x) for x in (record.get("software_build_id"), record.get("date_code")) if x),
                            record.get("first_seen") or "",
                            record.get("last_seen") or "",
                            record.get("removed_at") or "",
                            summary.get("lqi_24h") if summary.get("lqi_24h") is not None else "",
                            record.get("battery") if record.get("battery") is not None else "",
                            battery["type"],
                            battery["quantity"] or "",
                            battery["installed"],
                            battery["last_replaced"] or "",
                            len(battery["replacements"]),
                            next((r.get("brand") for r in reversed(battery["replacements"]) if r.get("brand")), ""),
                            uptime(record, now) or "",
                            cmd.get("commands_24h", ""),
                            cmd.get("median_ms") if cmd.get("median_ms") is not None else "",
                            cmd.get("missed_24h", ""),
                            record.get("area") or "",
                            record.get("ha_name") or "",
                            *[asset.get(field, "") for field in ASSET_FIELDS],
                            "yes" if record.get("critical") else "",
                            record.get("note") or "",
                        ]
                    )
                )
        return buffer.getvalue().encode("utf-8-sig")

    def set_note(self, ieee: str, note: str) -> bool:
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None:
                return False
            record["note"] = note
            self._changed(structural=True)
            return True

    def set_asset(self, ieee: str, asset: dict) -> bool:
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None:
                return False
            clean = {field: str(asset.get(field) or "").strip()[:200] for field in ASSET_FIELDS}
            record["asset"] = {k: v for k, v in clean.items() if v}
            self._changed(structural=True)
        # The warranty date drives the warranty_ending check.
        self._health_changed()
        return True

    # ------------------------------------------------------------ maintenance

    def set_maintenance_task(self, ieee: str, raw: Any) -> dict:
        """Add a task, or change the one with the same id. Keeps its added date and, unless given, its last done date."""
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None:
                return {"ok": False, "error": "Unknown device"}
            tasks = record.setdefault("maintenance", [])
            raw = dict(raw) if isinstance(raw, dict) else raw
            current = next((x for x in tasks if isinstance(raw, dict) and x["id"] == raw.get("id")), None)
            if current is not None:
                raw = {"last_done": current["last_done"], **raw, "created": current["created"]}
            elif isinstance(raw, dict):
                raw.pop("created", None)
                raw.pop("id", None)
            task, error = clean_task(raw, self._today())
            if task is None:
                return {"ok": False, "error": error}
            if current is not None:
                tasks[tasks.index(current)] = task
            elif len(tasks) >= MAX_TASKS:
                return {"ok": False, "error": f"A device can have at most {MAX_TASKS} tasks"}
            else:
                tasks.append(task)
            self._changed(structural=True)
        self._health_changed()
        return {"ok": True, "task": task}

    def remove_maintenance_task(self, ieee: str, task_id: str) -> bool:
        with self.lock:
            tasks = (self.data["devices"].get(ieee) or {}).get("maintenance") or []
            found = next((x for x in tasks if x["id"] == task_id), None)
            if found is None:
                return False
            tasks.remove(found)
            self._changed(structural=True)
        self._health_changed()
        return True

    def maintenance_done(self, ieee: str, task_id: str, day: Any = None) -> dict:
        """Mark a task done (today unless `day` is given) and log it on the device's history."""
        today = self._today()
        when = today if day in (None, "") else _iso_date(day)
        if when is None:
            return {"ok": False, "error": "Date must look like 2026-01-31"}
        if when > today:
            return {"ok": False, "error": "The date can't be in the future"}
        with self.lock:
            record = self.data["devices"].get(ieee)
            task = next((x for x in (record or {}).get("maintenance") or [] if x["id"] == task_id), None)
            if record is None or task is None:
                return {"ok": False, "error": "Unknown device or task"}
            task["last_done"] = when.isoformat()
            self._add_event(
                _now(),
                record.get("instance") or "unknown",
                ieee,
                _display_name(record),
                "maintenance done",
                task["title"] if when == today else f"{task['title']} ({when.isoformat()})",
            )
            self._changed(structural=True)
            task = dict(task)
        self._health_changed()
        return {"ok": True, "task": task, "due": task_due(task).isoformat()}

    def maintenance_list(self) -> list[dict]:
        """Every task on an active device with its next due date and days until then (negative when overdue)."""
        today = self._today()
        out = []
        with self.lock:
            for record in self.data["devices"].values():
                if record["status"] != STATUS_ACTIVE or self.is_frozen(record):
                    continue
                for task in record.get("maintenance") or []:
                    due = task_due(task)
                    out.append(
                        {
                            **task,
                            "ieee": record["ieee"],
                            "name": _display_name(record) or record["ieee"],
                            "due": due.isoformat(),
                            "days": (due - today).days,
                        }
                    )
        return sorted(out, key=lambda x: (x["due"], x["name"].lower(), x["title"].lower()))

    def warranty_list(self) -> list[dict]:
        """Active devices whose warranty ends in the next WARRANTY_AHEAD_DAYS or ended in the last
        WARRANTY_ENDED_DAYS: soonest first, then the ended ones, most recent first."""
        today = self._today()
        out = []
        with self.lock:
            for record in self.data["devices"].values():
                if record["status"] != STATUS_ACTIVE or record.get("type") == "Coordinator" or self.is_frozen(record):
                    continue
                ends = warranty_end(record)
                if ends is None:
                    continue
                days = (ends - today).days
                if not -WARRANTY_ENDED_DAYS <= days <= WARRANTY_AHEAD_DAYS:
                    continue
                asset = record.get("asset") or {}
                out.append(
                    {
                        "ieee": record["ieee"],
                        "name": _display_name(record) or record["ieee"],
                        "until": ends.isoformat(),
                        "days": days,
                        "purchase_date": asset.get("purchase_date") or "",
                    }
                )
        return sorted(out, key=lambda x: (x["days"] < 0, abs(x["days"]), x["name"].lower()))

    def _rhythm(self, record: dict, now: float | None = None) -> dict:
        """Metrics.rhythm for a device record, with its last_seen (ISO) as the time of week."""
        try:
            seen = datetime.fromisoformat(record["last_seen"]).timestamp()
        except (KeyError, TypeError, ValueError):
            seen = None
        return self.metrics.rhythm(record["ieee"], now, seen)

    def _uses_rhythm(self, record: dict) -> bool:
        """Whether the learned limit can matter: learning on, and the device on Auto or its type's default."""
        return bool(self.options.get("learn_rhythm", True)) and chosen_profile(record) in (None, "auto")

    def report_profile(self, record: dict, summary: dict, now: float | None = None, info: dict | None = None) -> dict:
        """The device's reporting profile and silent limit (see reporting_profiles). info: the device's
        _rhythm() when already worked out; without it, it is worked out only where it can matter."""
        if info is None and self._uses_rhythm(record):
            info = self._rhythm(record, now)
        return effective_profile(
            record,
            kind_of(record),
            summary,
            self.data.get("report_profile_defaults"),
            learned=info["limit"] if info else None,
            learn=bool(self.options.get("learn_rhythm", True)),
            excused=bool(info and info.get("excused")),
        )

    def _listening_since(self, record: dict) -> float:
        """When the app could last start hearing this device: its own start, the last MQTT reconnect or the
        last time the device's network came back online, whichever is latest."""
        return max(self.started, self._mqtt_back, self._bridge_back.get(record.get("instance"), 0.0))

    def mqtt_connected(self) -> None:
        """MQTT is (re)connected: silences are counted from now, not from before the drop."""
        self._mqtt_back = time.time()

    def mqtt_lost(self) -> None:
        """MQTT dropped: whatever devices send now is not heard, so no rhythm gap spans it."""
        self.metrics.deaf()

    def set_report_profile(self, ieee: str, profile: Any, seconds: Any = None) -> dict:
        """Pick a device's reporting profile. "default" goes back to its type's default; "custom" keeps
        its own interval (seconds, or the one it has)."""
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None:
                return {"ok": False, "error": "unknown device"}
            if profile not in (*PROFILE_CHOICES, "default"):
                return {"ok": False, "error": "unknown profile"}
            if profile == "custom":
                seconds = seconds if seconds is not None else record.get("report_every")
                if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
                    return {"ok": False, "error": "seconds must be a number"}
                if not REPORT_EVERY_MIN <= seconds <= REPORT_EVERY_MAX:
                    return {"ok": False, "error": f"seconds must be {REPORT_EVERY_MIN} to {REPORT_EVERY_MAX}"}
                record["report_every"] = int(seconds)
            if profile == "default":
                record.pop("report_profile", None)
                record.pop("report_every", None)
            else:
                record["report_profile"] = profile
            self._changed(structural=True)
        self._health_changed()
        return {"ok": True}

    def rhythm_normal(self, ieee: str) -> dict:
        """ "Normal for this device": the silence the device is in now raises no Stopped reporting from the
        learned limit until it reports again, and is then learned (Metrics.teach)."""
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None:
                return {"ok": False, "error": "unknown device"}
            try:
                seen = datetime.fromisoformat(record["last_seen"]).timestamp()
            except (KeyError, TypeError, ValueError):
                return {"ok": False, "error": "no last message to learn from"}
            if time.time() - seen <= 0 or not self.metrics.teach(ieee, seen):
                return {"ok": False, "error": "no silence to learn from"}
            self._changed(structural=True)
        self._health_changed()
        return {"ok": True}

    def rhythm_forget(self, ieee: str) -> dict:
        """Clear everything learned about a device's rhythm."""
        with self.lock:
            if ieee not in self.data["devices"]:
                return {"ok": False, "error": "unknown device"}
            self.metrics.forget_rhythm(ieee)
            self._changed(structural=True)
        self._health_changed()
        return {"ok": True}

    def set_report_defaults(self, changes: Any) -> dict:
        """Change the default profile of one or more device types: {group: profile}."""
        try:
            clean = clean_profile_defaults(changes)
        except ValueError as err:
            return {"ok": False, "error": str(err)}
        with self.lock:
            stored = dict(self.data.get("report_profile_defaults") or {})
            stored.update(clean)
            self.data["report_profile_defaults"] = stored
            self._changed(structural=True)
        self._health_changed()
        return {"ok": True, "defaults": profile_defaults(stored)}

    def set_report_every(self, ieee: str, seconds: int | None) -> bool:
        """Per-device "should report at least every" interval in seconds; None or 0 turns it off."""
        return not self.set_report_every_many([{"ieee": ieee, "seconds": seconds}])["errors"]

    def set_report_every_many(self, items: list) -> dict:
        """Set or clear the report interval for several devices, with one health run at the end.

        Each item is {"ieee", "seconds"}; seconds None or 0 turns it off. Valid items are applied,
        invalid ones are listed in "errors" as {"ieee", "error"}.
        """
        applied = 0
        errors: list[dict] = []
        with self.lock:
            for item in items:
                item = item if isinstance(item, dict) else {}
                ieee, seconds = str(item.get("ieee") or ""), item.get("seconds")
                record = self.data["devices"].get(ieee)
                if record is None:
                    errors.append({"ieee": ieee, "error": "unknown device"})
                elif seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, (int, float))):
                    errors.append({"ieee": ieee, "error": "seconds must be a number"})
                elif seconds and not REPORT_EVERY_MIN <= seconds <= REPORT_EVERY_MAX:
                    errors.append({"ieee": ieee, "error": f"seconds must be {REPORT_EVERY_MIN} to {REPORT_EVERY_MAX}"})
                else:
                    # Setting an interval makes it the device's Custom profile; turning it off turns the check off,
                    # rather than falling back to the type default.
                    if seconds:
                        record["report_every"] = int(seconds)
                        record["report_profile"] = "custom"
                    else:
                        record.pop("report_every", None)
                        record["report_profile"] = "off"
                    applied += 1
            if applied:
                self._changed(structural=True)
        if applied:
            self._health_changed()
        return {"ok": not errors, "applied": applied, "errors": errors}

    def late_due(self, now: float) -> bool:
        """True once for each device that passes its report interval, so health runs then, not up to 60 s later."""
        due = False
        with self.lock:
            for ieee, record in self.data["devices"].items():
                deadline = late_deadline(record, self._listening_since(record), self._report_limits.get(ieee))
                if deadline is not None and now >= deadline and self._late_checked.get(ieee) != deadline:
                    self._late_checked[ieee] = deadline
                    due = True
        return due

    def set_muted(self, ieee: str, kind: str, muted: bool, days: float | None = None) -> bool:
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None or not re.fullmatch(r"[a-z_]{2,30}", kind):
                return False
            current = mutes_of(record)
            if muted:
                until = None
                if days:
                    until = datetime.fromtimestamp(time.time() + float(days) * 86400, tz=timezone.utc).isoformat(
                        timespec="seconds"
                    )
                current[kind] = until
            else:
                current.pop(kind, None)
            record["muted"] = current
            self._changed(structural=True)
        self._health_changed()
        return True

    def set_limits(self, ieee: str, changes: dict) -> dict:
        """Set or clear the device's own battery/frozen limits. Only keys in `changes` are touched."""
        with self.lock:
            record = self.data["devices"].get(ieee)
            if record is None:
                return {"ok": False, "error": "Unknown device"}
            limits, error = clean_limits(record.get("limits") or {}, changes)
            if error:
                return {"ok": False, "error": error}
            if limits:
                record["limits"] = limits
            else:
                record.pop("limits", None)
            self._changed(structural=True)
        self._health_changed()
        return {"ok": True, "limits": limits}

    # ------------------------------------------------------------ Z2M actions

    def _bridge(self, base_topic: str) -> dict:
        bridges = self.data.setdefault("bridges", {})
        return bridges.setdefault(
            base_topic, {"state": None, "since": None, "outages": [], "restarts": [], "info": {}, "health": {}}
        )

    def _handle_bridge_state(self, base_topic: str, text: str) -> None:
        state = text.strip()
        if state.startswith("{"):
            state = (_loads(state) or {}).get("state", "")
        if state not in ("online", "offline"):
            return
        bridge = self._bridge(base_topic)
        previous = bridge.get("state")
        if previous == state:
            return
        now = _now()
        bridge["state"] = state
        bridge["since"] = now
        if state == "offline":
            # Nothing is heard from this network now: the gap each device's next message ends isn't its rhythm.
            self.metrics.deaf(
                [r["ieee"] for r in self.data["devices"].values() if r.get("instance") == base_topic and r.get("ieee")]
            )
            bridge["outages"].append({"start": now, "end": None})
            del bridge["outages"][: max(0, len(bridge["outages"]) - 100)]
            self._add_event(
                now,
                base_topic,
                "network",
                "Network",
                "network offline",
                "ZHA stopped (coordinator or Home Assistant's ZHA integration unavailable)"
                if _stack_of(base_topic) == "zha"
                else "Matter stopped (the Matter server or Home Assistant's Matter integration unavailable)"
                if _stack_of(base_topic) == "matter"
                else "Zigbee2MQTT stopped (coordinator or service offline)",
            )
        else:
            # The network is heard again: its devices' Stopped reporting clock restarts now.
            self._bridge_back[base_topic] = time.time()
        if state == "online" and previous == "offline":
            if bridge["outages"] and not bridge["outages"][-1].get("end"):
                bridge["outages"][-1]["end"] = now
            bridge["restarts"].append(now)
            del bridge["restarts"][: max(0, len(bridge["restarts"]) - 100)]
            running = {"zha": "ZHA is running again", "matter": "Matter is running again"}.get(
                _stack_of(base_topic), "Zigbee2MQTT is running again"
            )
            self._add_event(now, base_topic, "network", "Network", "network online", running)
        self._changed(structural=True)

    def _handle_bridge_health(self, base_topic: str, health: dict) -> None:
        def obj(value: Any) -> dict:
            return value if isinstance(value, dict) else {}

        def num(value: Any) -> Any:
            return value if _finite(value) else None

        bridge = self._bridge(base_topic)
        process = obj(health.get("process"))
        uptime = num(process.get("uptime_sec"))
        if uptime is not None and not 0 <= uptime < 1e10:
            uptime = None
        previous = obj(bridge.get("health")).get("uptime_sec")
        if uptime is not None and _finite(previous) and uptime + 120 < previous:
            now = _now()
            bridge["restarts"].append(now)
            del bridge["restarts"][: max(0, len(bridge["restarts"]) - 100)]
            self._add_event(
                now, base_topic, "network", "Network", "zigbee2mqtt restarted", f"Was up {int(previous // 3600)} h"
            )
            self.revision += 1
        for ieee, stats in obj(health.get("devices")).items():
            record = self.data["devices"].get(str(ieee).lower())
            if record is None or not isinstance(stats, dict):
                continue
            record["z2m_stats"] = {
                "messages": num(stats.get("messages")),
                "per_sec": num(stats.get("messages_per_sec")),
                "leave_count": num(stats.get("leave_count")),
                "nwk_changes": num(stats.get("network_address_changes")),
                "since": _now()
                if uptime is None
                else datetime.fromtimestamp(time.time() - uptime, tz=timezone.utc).isoformat(timespec="minutes"),
            }
        mqtt = obj(health.get("mqtt"))
        memory = num(process.get("memory_used_mb"))
        load = obj(health.get("os")).get("load_average")
        bridge["health"] = {
            "uptime_sec": uptime,
            "memory_mb": memory,
            "mqtt_connected": mqtt.get("connected") if isinstance(mqtt.get("connected"), bool) else None,
            "mqtt_published": num(mqtt.get("published")),
            "mqtt_received": num(mqtt.get("received")),
            "os_load": num(load[0]) if isinstance(load, list) and load else None,
            "updated": _now(),
        }
        if memory is not None and 0 < memory < 1e7:
            bridge["memory_days"] = record_memory(bridge.get("memory_days") or [], self._today(), round(memory, 1))
        self._soft_change()

    def _today(self) -> date:
        return datetime.fromtimestamp(time.time(), tz=self.local_tz).date()

    def _handle_info(self, base_topic: str, info: dict) -> None:
        def obj(value: Any) -> dict:
            return value if isinstance(value, dict) else {}

        coordinator = obj(info.get("coordinator"))
        network = obj(info.get("network"))
        meta = obj(coordinator.get("meta"))
        firmware = meta.get("revision") or meta.get("version") or meta.get("firmware")
        slim = {
            "version": _text(info.get("version"), 40),
            "commit": _text(info.get("commit"), 40),
            "coordinator_type": _text(coordinator.get("type"), 40),
            "coordinator_ieee": _ieee(coordinator.get("ieee_address")),
            "firmware": _text(str(firmware), 80) if firmware is not None else None,
            "channel": network.get("channel") if _finite(network.get("channel")) else None,
            "pan_id": network.get("pan_id") if _finite(network.get("pan_id")) else None,
            "restart_required": bool(info.get("restart_required")),
            # Only Z2M's availability settings, not its whole config, to keep history.json small.
            "availability": z2m_availability(info),
            # Connection, adapter, versions etc. for the Coordinators card: a few display fields, no secrets.
            **z2m_setup(info),
        }
        bridge = self._bridge(base_topic)
        if bridge.get("info") != slim:
            old = bridge.get("info") or {}
            changes = []
            if old.get("version") and slim["version"] and old["version"] != slim["version"]:
                self._add_event(
                    _now(),
                    base_topic,
                    "network",
                    "Network",
                    "zigbee2mqtt updated",
                    f"{old['version']} → {slim['version']}",
                )
                changes.append({"what": "zigbee2mqtt", "from": old["version"], "to": slim["version"]})
            if old.get("firmware") and slim["firmware"] and old["firmware"] != slim["firmware"]:
                self._add_event(
                    _now(),
                    base_topic,
                    "network",
                    "Network",
                    "coordinator firmware updated",
                    f"{old['firmware']} → {slim['firmware']}",
                )
                changes.append({"what": "firmware", "from": old["firmware"], "to": slim["firmware"]})
            if changes:
                self._record_upgrade(base_topic, changes)
            bridge["info"] = slim
            self._changed(structural=True)
        if isinstance(info.get("config"), dict):
            self._track_config(base_topic, info["config"])
        on = bool(info.get("permit_join"))
        # Z2M 2.x: permit_join_end is an epoch in ms. 1.x: permit_join_timeout is seconds left.
        end = info.get("permit_join_end") or info.get("permit_join_timeout")
        end_iso = None
        if _finite(end) and 0 < end < 1e14:
            end_ts = end / 1000 if end > 1e11 else time.time() + min(end, 86400)
            end_iso = datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat(timespec="seconds")
        state = {"on": on, "end": end_iso}
        previous = self.permit.get(base_topic)
        if on and base_topic not in self.permit_meta:
            self._permit_opened(base_topic, known_closed=previous is not None and not previous.get("on"))
        elif not on:
            self.permit_meta.pop(base_topic, None)
            self._permit_hint.pop(base_topic, None)
        if previous != state:
            self.permit[base_topic] = state
            if not on and base_topic in self.pairing_sessions:
                self.pairing_sessions[base_topic]["active"] = False
            self.revision += 1

    def _track_config(self, base_topic: str, config: dict) -> None:
        """Compare Zigbee2MQTT's settings with the last copy and log what changed.

        The first copy is the baseline, like the first device list. Network-wide changes go on the
        Coordinators card and one network event; a device's own options go on that device's logbook."""
        flat = config_flatten(config)
        store = self.data.setdefault("z2m_config", {})
        previous = store.get(base_topic)
        if previous == flat:
            return
        store[base_topic] = flat
        if previous is None:
            self.dirty = True
            return
        changes = config_diff(previous, flat)
        when = _now()
        bridge = self._bridge(base_topic)
        log = bridge.setdefault("config_changes", [])
        network_lines = []
        by_device: dict[str, list[str]] = {}
        for change in changes:
            log.append({"time": when, **change})
            ieee = change.get("ieee")
            if ieee and ieee in self.data["devices"]:
                by_device.setdefault(ieee, []).append(describe(change))
            elif not ieee:
                network_lines.append(describe(change))
        if len(log) > KEEP_CHANGES:
            del log[: len(log) - KEEP_CHANGES]
        if network_lines:
            more = f" (+{len(network_lines) - 3} more)" if len(network_lines) > 3 else ""
            self._add_event(
                when, base_topic, "network", "Network", "settings changed", "; ".join(network_lines[:3]) + more
            )
        for ieee, lines in by_device.items():
            self._add_event(
                when, base_topic, ieee, _display_name(self.data["devices"][ieee]), "settings changed", "; ".join(lines)
            )
        self._changed(structural=True)

    # ------------------------------------------------------- upgrade impact

    def _upgrade_window(self, base_topic: str, start: float, end: float) -> dict | None:
        events = self.data["events"]
        # A full event list has been trimmed: leaves before its oldest event are no longer known.
        events_from = upgrade_ts(events[0].get("time")) if len(events) >= MAX_EVENTS and events else None
        with self.metrics.lock:
            return window_stats(
                base_topic,
                start,
                end,
                self.data["devices"],
                self.metrics.series,
                events,
                self.scans.get(base_topic) or [],
                events_from,
            )

    def _record_upgrade(self, base_topic: str, changes: list[dict]) -> None:
        """Remember a Zigbee2MQTT or coordinator firmware change, with the network's numbers for the week before."""
        now = time.time()
        upgrades = self.data.setdefault("upgrades", {}).setdefault(base_topic, [])
        last = upgrades[-1] if upgrades else None
        last_at = upgrade_ts(last.get("at")) if last else None
        if last is not None and last_at is not None and now - last_at < MERGE_SECONDS:
            last["changes"] = merge_changes(last["changes"], changes)
            if not last["changes"]:
                upgrades.pop()
        else:
            if last is not None and last.get("after") is None:
                # The next upgrade ends this one's "after" week early; keep what it got.
                start, end = after_window(last, now)
                last["after"] = self._upgrade_window(base_topic, start, end)
            start, end = before_window(now, last)
            upgrades.append(
                {
                    "at": _now(),
                    "changes": changes,
                    "before": self._upgrade_window(base_topic, start, end),
                    "after": None,
                }
            )
            del upgrades[: max(0, len(upgrades) - MAX_UPGRADES)]
        self._upgrade_rev += 1
        self._changed(structural=True)

    def upgrade_impact(self, now: float) -> dict[str, dict]:
        """Per network: the latest upgrade's week before against the week after, and earlier upgrades.

        Recomputed once an hour. When the latest upgrade's week is over, its "after" numbers are stored.
        """
        key = (int(now // 3600), self._upgrade_rev)
        if self._impact_key == key:
            return self._impact
        out: dict[str, dict] = {}
        with self.lock:
            for bt, upgrades in (self.data.get("upgrades") or {}).items():
                if bt not in self.base_topics or not upgrades:
                    continue
                latest = upgrades[-1]
                after = latest.get("after")
                if after is None:
                    start, end = after_window(latest)
                    after = self._upgrade_window(bt, start, min(end, now))
                    if now >= end:
                        latest["after"] = after
                        self.dirty = True
                result = summary(upgrades, after, now)
                if result:
                    out[bt] = result
        self._impact, self._impact_key = out, key
        return out

    def _upgrade_issue(self, bt: str, label: str, now: float) -> dict | None:
        latest = (self.upgrade_impact(now).get(bt) or {}).get("latest")
        if not latest or latest["verdict"] != "worse" or latest["elapsed_days"] < ISSUE_MIN_DAYS:
            return None
        at = upgrade_ts(latest["at"])
        if at is None or now - at > ISSUE_MAX_DAYS * 86400:
            return None
        names = {
            "offline_per_day": "devices going offline per day",
            "lqi": "average link quality",
            "msg_per_hour": "messages per hour",
            "leaves_per_day": "devices leaving per day",
            "parent_changes_per_scan": "parent router changes per routing scan",
        }
        worse = [m for m in latest["metrics"] if m["status"] == "worse"]
        what = " and ".join(
            f"{'Zigbee2MQTT' if c['what'] == 'zigbee2mqtt' else 'coordinator firmware'} {c['to']}"
            for c in latest["changes"]
        )
        moment = datetime.fromtimestamp(at, tz=self.local_tz)
        day = f"{moment.day} {moment.strftime('%b')}"
        listing = ", ".join(f"{names[m['key']]} {m['before']:g} → {m['after']:g}" for m in worse)
        return {
            "key": f"upgrade_worse:{bt}",
            "kind": "upgrade_worse",
            "label": "Worse since upgrade",
            "severity": "info",
            "ieee": f"network:{bt}",
            "title": "Worse since the upgrade",
            "detail": f"Since {what} ({day}): {listing}. Health → Coordinators has the full comparison.",
            "msg": "upgrade_worse",
            "params": {"changes": latest["changes"], "at": latest["at"], "metrics": worse},
            "name": f"{label} network",
            "instance": bt,
        }

    def link(self, old_ieee: str, new_ieee: str) -> dict:
        with self.lock:
            old = self.data["devices"].get(old_ieee)
            new = self.data["devices"].get(new_ieee)
            if old is None or new is None or old_ieee == new_ieee:
                return {"ok": False, "error": "Both devices must exist and differ"}
            if old_ieee not in new.setdefault("predecessors", []):
                new["predecessors"].append(old_ieee)
            old["successor"] = new_ieee
            if not new.get("note") and old.get("note"):
                new["note"] = old["note"]
            if not new.get("asset") and old.get("asset"):
                new["asset"] = dict(old["asset"])
            now = _now()
            self._add_event(
                now,
                new["instance"],
                new_ieee,
                new.get("friendly_name"),
                "linked",
                f"Replaces {_display_name(old) or old_ieee} ({old_ieee})",
            )
            self._add_event(
                now,
                old["instance"] or "unknown",
                old_ieee,
                _display_name(old),
                "linked",
                f"Replaced by {_display_name(new) or new_ieee} ({new_ieee})",
            )
            self._changed(structural=True)
        self._health_changed()
        return {"ok": True}

    def unlink(self, old_ieee: str, new_ieee: str) -> dict:
        with self.lock:
            old = self.data["devices"].get(old_ieee)
            new = self.data["devices"].get(new_ieee)
            if new and old_ieee in (new.get("predecessors") or []):
                new["predecessors"].remove(old_ieee)
            if old and old.get("successor") == new_ieee:
                old["successor"] = None
            self._changed(structural=True)
        self._health_changed()
        return {"ok": True}

    def forget(self, ieee: str) -> bool:
        with self.lock:
            if self.data["devices"].pop(ieee, None) is None:
                return False
            self.data["events"] = [event for event in self.data["events"] if event.get("ieee") != ieee]
            for record in self.data["devices"].values():
                if ieee in (record.get("predecessors") or []):
                    record["predecessors"].remove(ieee)
                if record.get("successor") == ieee:
                    record["successor"] = None
            self._commands.pop(ieee, None)
            self._recent.pop(ieee, None)
            self.messages.forget(ieee)
            self.metrics.forget(ieee)
            self._changed(structural=True)
            return True

    def import_history(self) -> dict[str, int]:
        result = collect_history(self.config_dir, list(self.mqtt_topics), self.local_tz)
        added = 0
        new_events = 0
        with self.lock:
            records = self.data["devices"]
            for ieee, info in result["devices"].items():
                record = records.get(ieee)
                if record is None:
                    record = _new_record(ieee, info["instance"] or "unknown")
                    record["status"] = STATUS_REMOVED
                    records[ieee] = record
                    added += 1
                if record["instance"] in (None, "unknown") and info["instance"]:
                    record["instance"] = info["instance"]
                for key, value in info["fields"].items():
                    if value not in (None, "", []) and not record.get(key):
                        record[key] = value
                record["first_seen"] = _min_time(record["first_seen"], *info["times"])
                record["last_seen"] = _max_time(record["last_seen"], *info["last"])
                for name, when, source in info["names"]:
                    _add_historic_name(record, name, when, source)
                if info["ha_name"]:
                    record["ha_name"] = info["ha_name"]
                if info["area"]:
                    record["area"] = info["area"]
                for source in info["sources"]:
                    if source not in record["sources"]:
                        record["sources"].append(source)
                if record["status"] == STATUS_REMOVED and not record["removed_at"] and info["removed_hint"]:
                    record["removed_at"] = info["removed_hint"]

            existing = {(event["time"], event["ieee"], event["kind"]) for event in self.data["events"]}
            for event in result["events"]:
                key = (event["time"], event["ieee"], event["kind"])
                if key not in existing:
                    existing.add(key)
                    self.data["events"].append(event)
                    new_events += 1
            self.data["events"].sort(key=lambda event: event.get("time") or "")
            if len(self.data["events"]) > MAX_EVENTS:
                del self.data["events"][: len(self.data["events"]) - MAX_EVENTS]
            self.data["imported"] = _now()
            self._changed(structural=True)
        return {"devices_found": len(result["devices"]), "new_devices": added, "new_events": new_events}
