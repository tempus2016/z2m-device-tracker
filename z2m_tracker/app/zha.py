"""ZHA support (beta): read Home Assistant's ZHA integration instead of Zigbee2MQTT over MQTT.

ZHA's data is turned into the shapes the tracker already understands from Zigbee2MQTT and fed in
under one network named "zha". Read-only: nothing here sends commands to ZHA or the Zigbee network.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

_LOGGER = logging.getLogger(__name__)

ZHA_INSTANCE = "zha"
STACKS = ("z2m", "zha")
# App actions that go through Zigbee2MQTT over MQTT; for ZHA and Matter devices they answer "Only for Zigbee2MQTT devices".
ZHA_BLOCKED_PATHS = frozenset(
    {
        "/api/bindings/restore",
        "/api/replace",
        "/api/reconfigure",
        "/api/reporting",
        "/api/networkmap",
        "/api/ping",
        "/api/rename",
        "/api/z2m/last_seen",
        "/api/pairing",
        "/api/ota",
        "/api/ota/check",
        "/api/ota/queue",
        "/api/ota/cancel",
        "/api/retained/clear",
        "/api/retained/scan",
        "/api/retained_command/clear",
        "/api/import",
    }
)

# Checks that need what only Zigbee2MQTT provides (readings, firmware, bindings, commands, its own
# settings and health, the broker). With ZHA they can never fire, so Settings doesn't list them.
ZHA_UNUSED_CHECKS = frozenset(
    {
        "silent",
        "frozen",
        "impossible_reading",
        "unidentified",
        "update",
        "restart_required",
        "pairing_open",
        "chatty",
        "binding_lost",
        "slow_router",
        "slow_commands",
        "missed_commands",
        "coordinator_backup",
        "z2m_memory",
        "upgrade_worse",
        "group_member_offline",
        "reporting_config",
        "payload_changed",
        "firmware_note",
        "stale_retained",
        "retained_command",
        "availability_mismatch",
        "interview_failed",
    }
)


def zha_checks(checks: list) -> list:
    """The health checks that can fire with ZHA, from health.CHECKS."""
    return [check for check in checks if check[0] not in ZHA_UNUSED_CHECKS]


def zigbee_stack(options: dict) -> str:
    """The older single choice, `zigbee_stack`: "zha" only when chosen, "z2m" otherwise."""
    value = options.get("zigbee_stack")
    if isinstance(value, str) and value.strip().lower() == "zha":
        return "zha"
    return "z2m"


# Where devices come from: each one on is read side by side, as its own network.
SOURCE_SWITCHES = (("zigbee2mqtt", "z2m"), ("zha", "zha"), ("matter", "matter"))


def sources(options: dict) -> set[str]:
    """The sources to read: "z2m" (the `zigbee2mqtt` switch, on unless switched off), "zha", "matter".

    An install from before the switches that chose `zigbee_stack: zha` keeps reading ZHA only, as
    long as the switches are left as they came (Zigbee2MQTT on, ZHA off). Changing a switch wins."""
    on = {key: options.get(key) for key, _ in SOURCE_SWITCHES}
    untouched = on["zigbee2mqtt"] is not False and on["zha"] is not True and on["matter"] is not True
    if untouched and zigbee_stack(options) == "zha":
        return {"zha"}
    return {
        name for key, name in SOURCE_SWITCHES if (on[key] is not False if key == "zigbee2mqtt" else on[key] is True)
    }


IEEE_PARTS = 8
# ZHA's neighbour relationship as the network map's number; 1 marks a parent link (see networkmap_value).
RELATIONSHIPS = {"Parent": 1, "Child": 1, "Sibling": 2, "None": 3, "PreviousChild": 4}
POWER_SOURCES = {"Mains": "Mains (single phase)"}


def zha_ieee(value: Any) -> str | None:
    """ZHA's "aa:bb:cc:dd:ee:ff:00:11" as Zigbee2MQTT's "0xaabbccddeeff0011"."""
    if not isinstance(value, str):
        return None
    parts = value.strip().lower().split(":")
    if len(parts) != IEEE_PARTS or any(len(p) != 2 or any(c not in "0123456789abcdef" for c in p) for p in parts):
        return None
    return "0x" + "".join(parts)


def _name(device: dict) -> str:
    return str(device.get("user_given_name") or device.get("name") or "").strip()


def device_names(devices: list[dict]) -> dict[str, str]:
    """IEEE → the name the tracker uses, different for every device. A name two devices share gets
    the end of the IEEE added, so each device's updates reach its own record (ZHA's default name is
    just manufacturer and model). Names that still clash get the whole address."""
    named = [(zha_ieee(d.get("ieee")), _name(d)) for d in devices if isinstance(d, dict)]
    named = [(ieee, name or ieee) for ieee, name in named if ieee]

    def counted(names: list[str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for name in names:
            counts[name] = counts.get(name, 0) + 1
        return counts

    counts = counted([name for _, name in named])
    short = {ieee: (f"{name} ({ieee[-4:]})" if counts[name] > 1 else name) for ieee, name in named}
    clashes = counted(list(short.values()))
    base = dict(named)
    return {ieee: (f"{base[ieee]} ({ieee})" if clashes[label] > 1 else label) for ieee, label in short.items()}


def device_entries(devices: list[dict], battery_ieees: set[str]) -> list[dict]:
    """ZHA's device list as Zigbee2MQTT's bridge/devices entries."""
    names = device_names(devices)
    out = []
    for device in devices:
        if not isinstance(device, dict):
            continue
        ieee = zha_ieee(device.get("ieee"))
        if not ieee:
            continue
        power = device.get("power_source")
        if power == "Battery or Unknown" and ieee in battery_ieees:
            power = "Battery"
        power = POWER_SOURCES.get(power, power)
        out.append(
            {
                "ieee_address": ieee,
                "friendly_name": names[ieee],
                "type": device.get("device_type"),
                "interview_state": "SUCCESSFUL",
                "power_source": power,
                "manufacturer": device.get("manufacturer"),
                "model_id": device.get("model"),
                "definition": {"model": device.get("model"), "vendor": device.get("manufacturer")},
            }
        )
    return out


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def state_payload(device: dict, previous_last_seen: str | None) -> dict | None:
    """Last seen and signal as a Zigbee2MQTT state message, only when ZHA heard from the device again."""
    seen = device.get("last_seen")
    # Unchanged means no new message. Not "<=": naive local times jump back when the clocks go back.
    if not isinstance(seen, str) or not seen or seen == previous_last_seen:
        return None
    payload: dict[str, Any] = {"last_seen": seen}
    lqi = _number(device.get("lqi"))
    if lqi is not None:
        payload["linkquality"] = lqi
    rssi = _number(device.get("rssi"))
    if rssi is not None:
        payload["rssi"] = rssi
    return payload


def networkmap_value(devices: list[dict]) -> dict:
    """Each device's neighbour table as Zigbee2MQTT's network map (nodes and links).

    In the map a link with relationship 1 means its source's parent is its target. A neighbour ZHA
    lists as "Parent" is the table owner's parent; one listed as "Child" has the owner as parent
    (end devices have no table of their own, so this is the only place their parent shows).
    """
    nodes, links = [], []
    parent_links: dict[tuple, dict] = {}
    for device in devices:
        ieee = zha_ieee(device.get("ieee")) if isinstance(device, dict) else None
        if not ieee:
            continue
        nodes.append({"ieeeAddr": ieee})
        for neighbor in device.get("neighbors") or []:
            other = zha_ieee(neighbor.get("ieee")) if isinstance(neighbor, dict) else None
            if not other:
                continue
            relationship = str(neighbor.get("relationship"))
            source, target = (ieee, other) if relationship == "Parent" else (other, ieee)
            link = {
                "sourceIeeeAddr": source,
                "targetIeeeAddr": target,
                "lqi": _number(neighbor.get("lqi")),
                "relationship": RELATIONSHIPS.get(relationship, 3),
            }
            # A parent link is usually listed by both devices: keep one, with the better LQI.
            key = (source, target, link["relationship"])
            kept = parent_links.get(key) if link["relationship"] == 1 else None
            if kept is None:
                links.append(link)
                if link["relationship"] == 1:
                    parent_links[key] = link
            elif (link["lqi"] or 0) > (kept["lqi"] or 0):
                kept["lqi"] = link["lqi"]
    return {"nodes": nodes, "links": links}


def battery_entities(devices: list[dict], registry: list[dict], states: list[dict]) -> dict[str, str]:
    """Entity id → IEEE for the battery sensors (level or low) of ZHA's devices. HA's entity registry
    list links entities to devices but has no device class, so that comes from the states."""
    by_device = {d.get("device_reg_id"): zha_ieee(d.get("ieee")) for d in devices if isinstance(d, dict)}
    battery = {
        s.get("entity_id")
        for s in states
        if isinstance(s, dict) and (s.get("attributes") or {}).get("device_class") == "battery"
    }
    out = {}
    for entry in registry:
        if not isinstance(entry, dict) or entry.get("platform") != "zha":
            continue
        ieee = by_device.get(entry.get("device_id"))
        entity_id = entry.get("entity_id")
        if ieee and entity_id in battery and str(entity_id).split(".")[0] in ("sensor", "binary_sensor"):
            out[entity_id] = ieee
    return out


DEAD_STATES = (None, "", "unavailable", "unknown")
RETRY_SECONDS = 10
RECV_TIMEOUT = 5
# Longest wait for one reply (get_states can be large); past it the connection counts as dead.
CALL_TIMEOUT = 60
# The network map is taken again after this long even when no link changed, so link quality stays current.
MAP_REFRESH_SECONDS = 24 * 3600


def _state_time(state: dict) -> str | None:
    """When HA last heard this entity's value (last_reported is HA 2024.3+)."""
    return state.get("last_reported") or state.get("last_updated") or None


class ZhaUnavailable(RuntimeError):
    """Home Assistant answered, but ZHA isn't set up or isn't loaded."""


def _is_timeout(err: Exception) -> bool:
    # websocket-client raises its own WebSocketTimeoutException; tests use TimeoutError.
    return isinstance(err, TimeoutError) or type(err).__name__.endswith("TimeoutException")


class ZhaSource:
    """Reads ZHA through Home Assistant's websocket and feeds the tracker under the "zha" network."""

    def __init__(
        self, tracker: Any, connect: Callable[[], Any], stop_event: threading.Event, poll_seconds: int = 60
    ) -> None:
        self.tracker = tracker
        self.connect = connect
        self.stop_event = stop_event
        self.poll_seconds = poll_seconds
        self._battery: dict[str, str] = {}
        self._names: dict[str, str] = {}
        self._last_seen: dict[str, str] = {}
        # Last seen of unavailable devices, waiting for a second read to confirm it (see apply_devices).
        self._unconfirmed: dict[str, str] = {}
        # Network map: the links of the last read, of the last map given to the tracker, and when.
        self._map_seen: frozenset | None = None
        self._map_applied: frozenset | None = None
        self._map_applied_at = 0.0
        # Home Assistant's time zone, for ZHA's zoneless last seen. None: the tracker uses its own.
        self._tz: ZoneInfo | None = None
        # The subscription that reports battery changes, and the entities it covers.
        self._battery_sub: int | None = None
        self._battery_watched: list[str] = []
        self._online: bool | None = None
        self._next_id = 0
        self._logged_down = False
        self._registry_stale = False
        self.call_timeout = CALL_TIMEOUT

    # ------------------------------------------------------------ feeding the tracker

    def set_battery_entities(self, entities: dict[str, str]) -> None:
        self._battery = dict(entities)

    def set_time_zone(self, name: Any) -> None:
        """Home Assistant's time zone. ZHA writes last seen as local time without a zone, and the
        app's own zone isn't certain to match."""
        try:
            self._tz = ZoneInfo(name) if isinstance(name, str) and name else None
        except Exception:  # noqa: BLE001 - unknown zone name: leave it to the tracker's zone
            self._tz = None

    def _utc(self, local: Any) -> Any:
        """A zoneless local time from ZHA as UTC; unchanged when it can't be converted."""
        if self._tz is None or not isinstance(local, str):
            return local
        try:
            moment = datetime.fromisoformat(local)
        except ValueError:
            return local
        if moment.tzinfo is not None:
            return local
        return moment.replace(tzinfo=self._tz).astimezone(timezone.utc).isoformat(timespec="seconds")

    def apply_network(self, settings: Any) -> None:
        """ZHA's network settings: the channel and PAN ID, for the radio channel view."""
        info = ((settings or {}).get("settings") or {}).get("network_info") if isinstance(settings, dict) else None
        if not isinstance(info, dict):
            return
        channel = _number(info.get("channel"))
        if channel is None or not 11 <= channel <= 26:
            return
        try:
            pan_id = int(str(info.get("pan_id")), 16)
        except (TypeError, ValueError):
            pan_id = None
        self.tracker.set_network_info(ZHA_INSTANCE, int(channel), pan_id)

    def apply_devices(self, devices: list[dict] | None) -> None:
        """One ZHA device list. None or empty (a failed read) only marks the network offline."""
        if not devices:
            if self._online is not False:
                self.tracker.set_bridge_state(ZHA_INSTANCE, "offline")
                self._online = False
            return
        if self._online is not True:
            self.tracker.set_bridge_state(ZHA_INSTANCE, "online")
            self._online = True
        entries = device_entries(devices, set(self._battery.values()))
        self._names = {e["ieee_address"]: e["friendly_name"] for e in entries}
        self.tracker.apply_device_list(ZHA_INSTANCE, entries)
        for device in devices:
            ieee = zha_ieee(device.get("ieee")) if isinstance(device, dict) else None
            name = self._names.get(ieee) if ieee else None
            if not name:
                continue
            if isinstance(device.get("available"), bool):
                self.tracker.apply_availability(ZHA_INSTANCE, name, "online" if device["available"] else "offline")
            payload = state_payload(device, self._last_seen.get(ieee))
            if payload and device.get("available") is False:
                # ZHA gives the current time as last seen for a device it has never heard, so it moves
                # on every read. A real time stays put: only take one that two reads agree on.
                if self._unconfirmed.get(ieee) != payload["last_seen"]:
                    self._unconfirmed[ieee] = payload["last_seen"]
                    payload = None
            if payload:
                self._unconfirmed.pop(ieee, None)
                # The first read after a start replays a last_seen from before we were listening: not a report.
                first = ieee not in self._last_seen
                self._last_seen[ieee] = payload["last_seen"]
                self.tracker.apply_state(
                    ZHA_INSTANCE, name, {**payload, "last_seen": self._utc(payload["last_seen"])}, rhythm=not first
                )
        self._apply_map(devices)

    def _apply_map(self, devices: list[dict]) -> None:
        """Give the tracker a network map when the mesh changed, not on every read.

        ZHA scans its neighbours every few hours and fills the tables one device at a time, and link
        quality differs on every scan. A map goes to the tracker once two reads in a row show the
        same links and those differ from the last map, or once a day. Each map is a routing scan in
        the tracker's history, so this keeps that history as long as Zigbee2MQTT's nightly scan does.
        """
        value = networkmap_value(devices)
        # Empty until ZHA has scanned its neighbours; an empty map would wipe the routing shown now.
        if not value["links"]:
            return
        shape = frozenset(
            (link["sourceIeeeAddr"], link["targetIeeeAddr"], link["relationship"]) for link in value["links"]
        )
        settled = shape == self._map_seen
        self._map_seen = shape
        if not settled:
            return
        stale = time.monotonic() - self._map_applied_at >= MAP_REFRESH_SECONDS
        if shape != self._map_applied or stale:
            self._map_applied = shape
            self._map_applied_at = time.monotonic()
            self.tracker.apply_networkmap(ZHA_INSTANCE, value)

    def apply_battery(self, entity_id: str, state: Any, when: str | None = None, replay: bool = False) -> None:
        """One battery entity state. when is HA's time for it. A replay (the current states read on
        connect) is stamped with when ZHA last heard the device, so a value HA kept across a restart
        isn't taken as a new reading (it would undo a battery marked replaced since)."""
        ieee = self._battery.get(entity_id)
        name = self._names.get(ieee) if ieee else None
        if not name or state in DEAD_STATES:
            return
        if replay:
            when = self._utc(self._last_seen.get(ieee))
        if entity_id.startswith("binary_sensor."):
            if state in ("on", "off"):
                self.tracker.apply_state(
                    ZHA_INSTANCE, name, self._stamped({"battery_low": state == "on"}, when), rhythm=False
                )
            return
        try:
            level = float(state)
        except (TypeError, ValueError):
            return
        self.tracker.apply_state(ZHA_INSTANCE, name, self._stamped({"battery": level}, when), rhythm=False)

    @staticmethod
    def _stamped(payload: dict, when: Any) -> dict:
        if isinstance(when, str) and when:
            payload["last_seen"] = when
        return payload

    # ------------------------------------------------------------ websocket

    def run(self) -> None:
        """Read ZHA until stop_event is set, reconnecting after any failure."""
        while not self.stop_event.is_set():
            ws = None
            try:
                ws = self.connect()
                self._session(ws)
            except Exception as err:  # noqa: BLE001 - keep reading ZHA whatever went wrong
                if self.stop_event.is_set():
                    break  # the app is stopping: not an outage
                if not self._logged_down:
                    if isinstance(err, ZhaUnavailable):
                        _LOGGER.error("ZHA isn't available in Home Assistant (%s); retrying", err)
                    else:
                        _LOGGER.warning("Reading ZHA failed (%s: %s); retrying", type(err).__name__, err)
                    self._logged_down = True
                self.apply_devices(None)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:  # noqa: BLE001 - already broken
                        pass
            self.stop_event.wait(RETRY_SECONDS)

    def _send(self, ws: Any, message: dict) -> int:
        self._next_id += 1
        ws.send(json.dumps({"id": self._next_id, **message}))
        return self._next_id

    def _call(self, ws: Any, message: dict) -> Any:
        """Send one command and wait for its reply, handling events that arrive meanwhile."""
        wanted = self._send(ws, message)
        deadline = time.monotonic() + self.call_timeout
        while not self.stop_event.is_set():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"no reply to {message['type']}")
            try:
                reply = json.loads(ws.recv())
            except Exception as err:
                if _is_timeout(err):
                    continue
                raise
            if reply.get("type") == "event":
                self._event(reply.get("event") or {})
                continue
            if reply.get("id") != wanted:
                continue
            if not reply.get("success"):
                error = (reply.get("error") or {}).get("message") or "error"
                if message["type"].startswith("zha/"):
                    raise ZhaUnavailable(error)
                raise RuntimeError(error)
            return reply.get("result")
        return None

    def _optional(self, ws: Any, message: dict) -> Any:
        """A command the app can do without: an error answer is None, not the end of the session."""
        try:
            return self._call(ws, message)
        except RuntimeError as err:
            _LOGGER.debug("Home Assistant refused %s: %s", message["type"], err)
            return None

    def _load_batteries(self, ws: Any, devices: list[dict]) -> list[dict]:
        """Find the battery entities of ZHA's devices; returns HA's states for their first values."""
        registry = self._call(ws, {"type": "config/entity_registry/list"}) or []
        states = self._call(ws, {"type": "get_states"}) or []
        self.set_battery_entities(battery_entities(devices, registry, states))
        return states

    def _refresh_batteries(self, ws: Any, devices: list[dict]) -> None:
        """After HA's entity registry changed (a device was paired, an entity renamed): find the
        battery entities again and give the tracker the current value of the ones that are new."""
        known = set(self._battery)
        states = self._load_batteries(ws, devices)
        self.apply_devices(devices)
        for state in states:
            entity_id = state.get("entity_id") if isinstance(state, dict) else None
            if entity_id in self._battery and entity_id not in known:
                self.apply_battery(entity_id, state.get("state"), replay=True)

    def _session(self, ws: Any) -> None:
        ws.settimeout(RECV_TIMEOUT)
        self._battery_sub, self._battery_watched = None, []
        devices = self._call(ws, {"type": "zha/devices"})
        if self.stop_event.is_set():
            return
        self.set_time_zone((self._optional(ws, {"type": "get_config"}) or {}).get("time_zone"))
        states = self._load_batteries(ws, devices or [])
        self.apply_devices(devices)
        for state in states:
            if isinstance(state, dict) and state.get("entity_id") in self._battery:
                self.apply_battery(state["entity_id"], state.get("state"), replay=True)
        if devices:
            self.apply_network(self._optional(ws, {"type": "zha/network/settings"}))
        self._watch_batteries(ws)
        self._send(ws, {"type": "subscribe_events", "event_type": "entity_registry_updated"})
        if devices:
            _LOGGER.info("Reading ZHA: %d devices", len(devices))
            self._logged_down = False
        self._registry_stale = False
        next_poll = time.monotonic() + self.poll_seconds
        while not self.stop_event.is_set():
            if time.monotonic() >= next_poll:
                devices = self._call(ws, {"type": "zha/devices"})
                if self.stop_event.is_set():
                    return
                if self._registry_stale and devices:
                    self._registry_stale = False
                    self._refresh_batteries(ws, devices)
                    self._watch_batteries(ws)
                else:
                    self.apply_devices(devices)
                next_poll = time.monotonic() + self.poll_seconds
                continue
            try:
                message = json.loads(ws.recv())
            except Exception as err:
                if _is_timeout(err):
                    continue
                raise
            if message.get("type") == "event":
                self._event(message.get("event") or {})

    def _watch_batteries(self, ws: Any) -> None:
        """Ask HA for the state changes of the battery entities only, not of everything in the house."""
        wanted = sorted(self._battery)
        if wanted == self._battery_watched:
            return
        if self._battery_sub is not None:
            self._send(ws, {"type": "unsubscribe_events", "subscription": self._battery_sub})
            self._battery_sub = None
        if wanted:
            trigger = {"platform": "state", "entity_id": wanted}
            try:
                self._call(ws, {"type": "subscribe_trigger", "trigger": trigger})
            except RuntimeError as err:
                # Not expected, but battery levels matter more than a quiet connection.
                _LOGGER.warning("Watching the battery sensors alone failed (%s); watching all state changes", err)
                self._call(ws, {"type": "subscribe_events", "event_type": "state_changed"})
            self._battery_sub = self._next_id
        self._battery_watched = wanted

    def _event(self, event: dict) -> None:
        trigger = (event.get("variables") or {}).get("trigger")
        if isinstance(trigger, dict):
            self._battery_changed(trigger.get("entity_id"), trigger.get("from_state"), trigger.get("to_state"))
        elif event.get("event_type") == "state_changed":
            data = event.get("data") or {}
            self._battery_changed(data.get("entity_id"), data.get("old_state"), data.get("new_state"))
        elif event.get("event_type") == "entity_registry_updated":
            self._registry_stale = True

    def _battery_changed(self, entity_id: Any, old: Any, new: Any) -> None:
        if entity_id not in self._battery:
            return
        old, new = old or {}, new or {}
        # Back from unavailable (ZHA reloaded, the coordinator reconnected): the old value
        # again, not a report from the device.
        restored = old.get("state") in DEAD_STATES
        self.apply_battery(entity_id, new.get("state"), _state_time(new), replay=restored)
