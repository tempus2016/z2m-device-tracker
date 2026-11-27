"""Matter support (beta): read Home Assistant's Matter integration and feed the tracker as one network.

Like ZHA, Matter's data is turned into the shapes the tracker already understands from Zigbee2MQTT and
fed in under one network, named "matter". Read-only: nothing here sends commands to Matter devices.

What is read, every POLL_SECONDS:
- the device registry: devices of the Matter integration (identifier "deviceid_<fabric>-<node>-...");
- the entity registry and states: a device's last report is the newest `last_reported` of its
  entities, its battery the state of its battery sensor, and it is offline when every entity is
  unavailable;
- the Matter server's network topology, when the server is new enough to give one: availability,
  node role, Thread or Wi-Fi, and for Thread the links between nodes (the mesh map).

Device keys: the tracker keys devices by a 64-bit Zigbee address. A Matter device gets one made from
its Matter identifier, starting 0xff4d ("M"): an address no Zigbee radio uses, so it never clashes
with a real device and every place that checks an address accepts it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from map_gate import MapGate

_LOGGER = logging.getLogger(__name__)

MATTER_INSTANCE = "matter"
KEY_PREFIX = "0xff4d"
POLL_SECONDS = 60
RETRY_SECONDS = 30
RECV_TIMEOUT = 5
CALL_TIMEOUT = 60
REGISTRY_REFRESH_SECONDS = 600
# Asks Home Assistant for the states of the Matter entities only (`ids`), in the shape get_states
# gives. Reading every state of the house each minute is megabytes on a large install.
STATES_TEMPLATE = (
    "{% set ns = namespace(out=[]) %}{% for e in ids %}{% set s = states[e] %}{% if s %}"
    "{% set ns.out = ns.out + [{'entity_id': e, 'state': s.state,"
    " 'last_reported': s.last_reported.isoformat() if s.last_reported else '',"
    " 'last_updated': s.last_updated.isoformat() if s.last_updated else '',"
    " 'attributes': {'device_class': s.attributes.device_class | default('', true),"
    " 'unit_of_measurement': s.attributes.unit_of_measurement | default('', true)}}] %}"
    "{% endif %}{% endfor %}{{ ns.out | tojson }}"
)
DEAD_STATES = ("unavailable", "unknown", None)
# Node roles from the Matter server's topology, as the device types the tracker knows.
ROLE_TYPES = {"router": "Router", "leader": "Router", "routing_end_device": "Router", "bridge": "Router"}


def matter_id(device: dict) -> str | None:
    """The Matter identifier of a device registry entry ("deviceid_<fabric>-<node>-..."), or None."""
    for ident in device.get("identifiers") or []:
        if isinstance(ident, (list, tuple)) and len(ident) == 2 and ident[0] == "matter":
            value = str(ident[1])
            if value.startswith("deviceid_"):
                return value[len("deviceid_") :]
    return None


def node_id(device: dict) -> int | None:
    """The Matter node id from the identifier: deviceid_<fabric>-<node, 16 hex>-<endpoint kind>."""
    mid = matter_id(device)
    parts = mid.split("-") if mid else []
    if len(parts) < 2:
        return None
    try:
        return int(parts[1], 16)
    except ValueError:
        return None


def device_key(device: dict) -> str | None:
    """The tracker's key for a Matter device: 0xff4d plus 12 hex characters from its Matter id."""
    mid = matter_id(device)
    if not mid:
        return None
    return KEY_PREFIX + hashlib.sha256(mid.encode("utf-8")).hexdigest()[:12]


def _name(device: dict) -> str:
    return str(device.get("name_by_user") or device.get("name") or device.get("model") or "Matter device")


def _ts(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    return None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def matter_devices(registry: list[dict], entry_ids: set[str]) -> list[dict]:
    """The Matter devices of the device registry: of the Matter config entries, with a node identifier,
    not disabled."""
    out = []
    for device in registry if isinstance(registry, list) else []:
        if not isinstance(device, dict) or device.get("disabled_by"):
            continue
        # HA 2026.8 gave each device one config_entry_id; config_entries goes in 2027.8.
        owners = set(device.get("config_entries") or [])
        if device.get("config_entry_id"):
            owners.add(device["config_entry_id"])
        if entry_ids and not owners & entry_ids:
            continue
        if matter_id(device):
            out.append(device)
    return out


def device_readings(devices: list[dict], entities: list[dict], states: list[dict]) -> dict[str, dict]:
    """Per device key: {"last_reported": newest time an entity reported, "available": False when every
    entity is unavailable, "battery": the battery sensor's level}."""
    by_device: dict[str, list[dict]] = {}
    for entity in entities if isinstance(entities, list) else []:
        if isinstance(entity, dict) and entity.get("device_id") and not entity.get("disabled_by"):
            by_device.setdefault(entity["device_id"], []).append(entity)
    state_of = {s.get("entity_id"): s for s in states if isinstance(s, dict)} if isinstance(states, list) else {}
    out: dict[str, dict] = {}
    for device in devices:
        key = device_key(device)
        if not key:
            continue
        reading: dict[str, Any] = {"last_reported": None, "available": None, "battery": None}
        live = 0
        seen = []
        for entity in by_device.get(device.get("id"), []):
            state = state_of.get(entity.get("entity_id"))
            if not state:
                continue
            if state.get("state") not in DEAD_STATES:
                live += 1
                at = _ts(state.get("last_reported")) or _ts(state.get("last_updated"))
                if at:
                    seen.append(at)
            attrs = state.get("attributes") or {}
            if attrs.get("device_class") == "battery" and attrs.get("unit_of_measurement") == "%":
                try:
                    reading["battery"] = float(state.get("state"))
                except (TypeError, ValueError):
                    pass
        if by_device.get(device.get("id")):
            reading["available"] = live > 0
        reading["last_reported"] = max(seen) if seen else None
        out[key] = reading
    return out


def topology_nodes(topology: Any) -> dict[int, dict]:
    """The Matter server's topology nodes by node id (Matter devices only)."""
    nodes = (topology or {}).get("nodes") if isinstance(topology, dict) else None
    return {n["node_id"]: n for n in nodes or [] if isinstance(n, dict) and isinstance(n.get("node_id"), int)}


def device_entries(devices: list[dict], readings: dict[str, dict], nodes: dict[int, dict]) -> list[dict]:
    """The Matter devices as Zigbee2MQTT's bridge/devices entries."""
    names: dict[str, int] = {}
    for device in devices:
        names[_name(device)] = names.get(_name(device), 0) + 1
    out = []
    for device in devices:
        key = device_key(device)
        if not key:
            continue
        name = _name(device)
        if names[name] > 1:
            name = f"{name} ({key[-4:]})"
        node = nodes.get(node_id(device)) or {}
        battery = (readings.get(key) or {}).get("battery") is not None
        out.append(
            {
                "ieee_address": key,
                "friendly_name": name,
                "type": ROLE_TYPES.get(str(node.get("role") or ""), "EndDevice"),
                "interview_state": "SUCCESSFUL",
                "power_source": "Battery" if battery else None,
                "manufacturer": device.get("manufacturer"),
                "model_id": device.get("model"),
                "definition": {"model": device.get("model"), "vendor": device.get("manufacturer")},
            }
        )
    return out


def networkmap_value(devices: list[dict], topology: Any) -> dict:
    """Thread links from the topology as a Zigbee2MQTT network map ({"nodes", "links"}), for the map."""
    key_of = {node_id(d): device_key(d) for d in devices}
    nodes = topology.get("nodes") if isinstance(topology, dict) else []
    id_to_key = {}
    for n in nodes or []:
        if isinstance(n, dict) and n.get("node_id") in key_of:
            id_to_key[str(n.get("id"))] = key_of[n["node_id"]]
    links = []
    for c in (topology or {}).get("connections") or [] if isinstance(topology, dict) else []:
        if not isinstance(c, dict) or c.get("network") != "thread":
            continue
        a, b = id_to_key.get(str(c.get("source"))), id_to_key.get(str(c.get("target")))
        if not a or not b:
            continue
        lqi = max([(c.get(k) or {}).get("lqi") or 0 for k in ("source_to_target", "target_to_source")] or [0])
        links.append({"sourceIeeeAddr": a, "targetIeeeAddr": b, "lqi": lqi, "relationship": 2, "depth": 1})
    return {"nodes": [{"ieeeAddr": k} for k in sorted(set(id_to_key.values()))], "links": links}


class MatterUnavailable(RuntimeError):
    """Home Assistant's Matter integration isn't loaded or doesn't answer."""


def _is_timeout(err: Exception) -> bool:
    return "timed out" in str(err).lower() or type(err).__name__ in ("TimeoutError", "WebSocketTimeoutException")


class MatterSource:
    """Reads Matter through Home Assistant's websocket and feeds the tracker under the "matter" network."""

    def __init__(
        self, tracker: Any, connect: Callable[[], Any], stop_event: threading.Event, poll_seconds: int = POLL_SECONDS
    ) -> None:
        self.tracker = tracker
        self.connect = connect
        self.stop_event = stop_event
        self.poll_seconds = poll_seconds
        self._last: dict[str, float] = {}
        self._online: bool | None = None
        self._next_id = 0
        self._logged_down = False
        self._registry: tuple[float, list[dict], list[dict]] = (0.0, [], [])
        self.topology_supported: bool | None = None
        self._topology_retry_at = 0.0
        self._map = MapGate()
        # None until tried; False once the short read failed, then every state is read as before.
        self.template_states: bool | None = None

    # ------------------------------------------------------------ feeding the tracker
    def apply(
        self, devices: list[dict] | None, readings: dict[str, dict], nodes: dict[int, dict], topology: Any = None
    ) -> None:
        """One read. None (a failed read) only marks the network offline."""
        if devices is None:
            if self._online is not False:
                self.tracker.set_bridge_state(MATTER_INSTANCE, "offline")
                self._online = False
            return
        if self._online is not True:
            self.tracker.set_bridge_state(MATTER_INSTANCE, "online")
            self._online = True
        entries = device_entries(devices, readings, nodes)
        self.tracker.apply_device_list(MATTER_INSTANCE, entries)
        for entry, device in zip(entries, [d for d in devices if device_key(d)], strict=True):
            key, name = entry["ieee_address"], entry["friendly_name"]
            reading = readings.get(key) or {}
            node = nodes.get(node_id(device)) or {}
            available = node.get("available") if isinstance(node.get("available"), bool) else reading.get("available")
            if isinstance(available, bool):
                self.tracker.apply_availability(MATTER_INSTANCE, name, "online" if available else "offline")
            at = reading.get("last_reported")
            payload: dict[str, Any] = {}
            if at and at != self._last.get(key):
                # The first read after a start replays a time from before we were listening: not a report.
                first = key not in self._last
                self._last[key] = at
                payload["last_seen"] = _iso(at)
                if reading.get("battery") is not None:
                    payload["battery"] = reading["battery"]
                self.tracker.apply_state(MATTER_INSTANCE, name, payload, rhythm=not first)
        if topology is not None:
            value = networkmap_value(devices, topology)
            # Not on every poll: each map is a routing scan in the tracker's history (map_gate.py).
            if value["links"] and self._map.due(value):
                self.tracker.apply_networkmap(MATTER_INSTANCE, value)

    # ------------------------------------------------------------ Home Assistant's websocket
    def run(self) -> None:
        while not self.stop_event.is_set():
            ws = None
            try:
                ws = self.connect()
                self._session(ws)
            except Exception as err:  # noqa: BLE001 - keep reading whatever went wrong
                if self.stop_event.is_set():
                    break
                if not self._logged_down:
                    _LOGGER.warning("Reading Matter failed (%s: %s); retrying", type(err).__name__, err)
                    self._logged_down = True
                self.apply(None, {}, {})
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:  # noqa: BLE001 - already broken
                        pass
            self.stop_event.wait(RETRY_SECONDS)

    def _call(self, ws: Any, message: dict) -> Any:
        self._next_id += 1
        wanted = self._next_id
        ws.send(json.dumps({"id": wanted, **message}))
        deadline = time.monotonic() + CALL_TIMEOUT
        while not self.stop_event.is_set():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"no reply to {message['type']}")
            try:
                reply = json.loads(ws.recv())
            except Exception as err:
                if _is_timeout(err):
                    continue
                raise
            if reply.get("id") != wanted:
                continue
            if not reply.get("success"):
                raise RuntimeError((reply.get("error") or {}).get("message") or "error")
            return reply.get("result")
        return None

    def read(self, ws: Any) -> tuple[list[dict], dict[str, dict], dict[int, dict], Any]:
        entries = self._call(ws, {"type": "config_entries/get", "domain": "matter"}) or []
        loaded = {e["entry_id"] for e in entries if isinstance(e, dict) and e.get("state") == "loaded"}
        if not loaded:
            raise MatterUnavailable("the Matter integration isn't loaded")
        at, registry, entities = self._registry
        if time.monotonic() - at >= REGISTRY_REFRESH_SECONDS or not registry:
            registry = self._call(ws, {"type": "config/device_registry/list"}) or []
            entities = self._call(ws, {"type": "config/entity_registry/list"}) or []
            self._registry = (time.monotonic(), registry, entities)
        devices = matter_devices(registry, loaded)
        states = self.states(ws, devices, entities)
        readings = device_readings(devices, entities, states)
        topology = None
        if self.topology_supported is not False and time.monotonic() >= self._topology_retry_at:
            try:
                topology = self._call(ws, {"type": "matter/network_topology"})
                self.topology_supported = True
            except RuntimeError as err:
                if "unknown command" in str(err).lower():
                    # A Matter integration from before the command existed: it never will answer.
                    self.topology_supported = False
                    _LOGGER.info("The Matter server gives no network topology: no Thread map or node roles")
                else:
                    # Anything else (the Matter server restarting) passes: ask again later.
                    self._topology_retry_at = time.monotonic() + REGISTRY_REFRESH_SECONDS
                    _LOGGER.info("Reading the Matter network topology failed (%s); trying again later", err)
        return devices, readings, topology_nodes(topology), topology

    def states(self, ws: Any, devices: list[dict], entities: list[dict]) -> list[dict]:
        """The states the readings come from: the Matter entities' alone when Home Assistant can
        give those, every state otherwise."""
        if self.template_states is not False:
            device_ids = {d.get("id") for d in devices if device_key(d)}
            ids = sorted(
                e["entity_id"]
                for e in entities
                if isinstance(e, dict)
                and e.get("device_id") in device_ids
                and isinstance(e.get("entity_id"), str)
                and not e.get("disabled_by")
            )
            if not ids:
                return []
            try:
                rows = self._render(ws, ids)
                # Without the report times the app could not tell a quiet device from a live one.
                if rows and not any(row.get("last_reported") for row in rows):
                    raise ValueError("no report times in the answer")
                self.template_states = True
                return rows
            except (RuntimeError, ValueError, TypeError, AttributeError, TimeoutError) as err:
                self.template_states = False
                _LOGGER.info("Reading only the Matter entities' states failed (%s); reading all states", err)
        return self._call(ws, {"type": "get_states"}) or []

    def _render(self, ws: Any, ids: list[str]) -> list[dict]:
        """One render of STATES_TEMPLATE. Home Assistant answers a template with a subscription:
        the first event carries the result, then the subscription is dropped."""
        self._call(
            ws,
            {
                "type": "render_template",
                "template": STATES_TEMPLATE,
                "variables": {"ids": ids},
                "report_errors": True,
                "timeout": 10,
            },
        )
        sub = self._next_id
        result: Any = None
        try:
            deadline = time.monotonic() + CALL_TIMEOUT
            while not self.stop_event.is_set():
                if time.monotonic() >= deadline:
                    raise TimeoutError("no answer to render_template")
                try:
                    message = json.loads(ws.recv())
                except Exception as err:
                    if _is_timeout(err):
                        continue
                    raise
                if message.get("id") != sub:
                    continue
                if message.get("type") == "event":
                    result = (message.get("event") or {}).get("result")
                    break
                if message.get("success") is False:
                    raise RuntimeError((message.get("error") or {}).get("message") or "error")
        finally:
            try:
                self._call(ws, {"type": "unsubscribe_events", "subscription": sub})
            except RuntimeError:
                pass
        # Home Assistant hands a JSON list back as a list, or as text when it can't read it as one.
        rows = json.loads(result) if isinstance(result, str) else result
        if not isinstance(rows, list):
            raise ValueError("unexpected answer")
        return [row for row in rows if isinstance(row, dict)]

    def _session(self, ws: Any) -> None:
        ws.settimeout(RECV_TIMEOUT)
        while not self.stop_event.is_set():
            devices, readings, nodes, topology = self.read(ws)
            if self._logged_down or self._online is not True:
                _LOGGER.info("Reading Matter: %d devices", len(devices))
            self.apply(devices, readings, nodes, topology)
            self._logged_down = False
            if self._idle(ws, self.poll_seconds):
                return

    def _idle(self, ws: Any, seconds: float) -> bool:
        """Wait between reads while still reading the socket. Home Assistant pings every websocket
        and closes one that doesn't answer within about a minute; the client only answers while
        it is in recv(). True when the app is stopping."""
        deadline = time.monotonic() + seconds
        while not self.stop_event.is_set():
            if time.monotonic() >= deadline:
                return False
            try:
                ws.recv()
            except Exception as err:
                if _is_timeout(err):
                    continue
                raise
        return True
