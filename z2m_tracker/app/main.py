"""Z2M Device Tracker app: MQTT listener, health monitor, alerts and web UI/API on the ingress port."""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt
from battery_library import BatteryLibrary
from discovery import AVAILABILITY, Discovery
from discovery import BASE as DISCOVERY_BASE
from ha import HomeAssistant
from health import CHECKS, DEFAULT_NOTIFY, mutes_of
from metrics import Metrics
from tracker import Tracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
_LOGGER = logging.getLogger("z2m_tracker")

VERSION = "1.0.1"
PORT = 8099
DATA_PATH = "/data/history.json"
METRICS_PATH = "/data/metrics.json"
SCANS_PATH = "/data/scans.json"
OPTIONS_PATH = "/data/options.json"
CONFIG_DIR = "/homeassistant"
MIGRATE_FROM = f"{CONFIG_DIR}/.storage/z2m_tracker"
APP_DIR = Path(__file__).parent
WWW = APP_DIR / "www"
# Fonts are served from this fixed list, so a request path never becomes a file path.
FONTS = {font.name: font for font in (WWW / "fonts").glob("*.woff2")}
SAVE_INTERVAL = 30
METRICS_SAVE_INTERVAL = 300
HEALTH_INTERVAL = 60
HA_REFRESH_INTERVAL = 6 * 3600
FIRST_IMPORT_WAIT = 120

# Ingress proxy (supervisor) and HA Core on the internal hassio network.
ALLOWED_CLIENTS = {"172.30.32.1", "172.30.32.2", "127.0.0.1", "::1"}

DEFAULT_OPTIONS = {
    "base_topics": ["zigbee2mqtt"],
    "z2m_panels": [],
    "persistent_notifications": True,
    "notify_service": "",
    "alert_on_join": True,
    "offline_alert_minutes": 60,
    "frozen_alert_hours": 24,
    "battery_alert_percent": 15,
    "networkmap_hour": 3,
    "ha_entities": True,
    "weekly_summary": True,
    "summary_day": 0,
    "summary_hour": 9,
    "alerts_enabled": True,
    "fire_events": True,
    "disabled_checks": [],
    "notify_checks": list(DEFAULT_NOTIFY),
    "wifi_channels": "",
    "chatty_per_hour": 1200,
}

CHECK_KEYS = {key for key, _, _, _ in CHECKS}
EVENT_KEYS = {"joined": "joined", "left": "left", "interview failed": "interview_failed"}


# Settings the web UI may change, with validators returning the clean value (or raising ValueError).
def _bool(value):
    if isinstance(value, bool):
        return value
    raise ValueError("must be true or false")


def _int(low, high):
    def check(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= int(value) <= high:
            raise ValueError(f"must be a number from {low} to {high}")
        return int(value)

    return check


def _service(value):
    value = str(value or "").strip()
    if value and not __import__("re").fullmatch(r"[a-z0-9_]+(\.[a-z0-9_]+)?", value):
        raise ValueError("must look like notify.mobile_app_phone")
    return value


def _wifi(value):
    parts = [p.strip() for p in str(value or "").replace(";", ",").split(",") if p.strip()]
    if len(parts) > 6 or any(not p.isdigit() or not 1 <= int(p) <= 14 for p in parts):
        raise ValueError("enter 2.4 GHz Wi-Fi channels from 1 to 14, separated by commas")
    return ", ".join(str(int(p)) for p in dict.fromkeys(parts))


def _kinds(value):
    if not isinstance(value, list) or any(item not in CHECK_KEYS for item in value):
        raise ValueError("unknown check")
    return sorted(set(value))


SETTINGS = {
    "alerts_enabled": _bool,
    "persistent_notifications": _bool,
    "fire_events": _bool,
    "notify_service": _service,
    "offline_alert_minutes": _int(0, 10080),
    "frozen_alert_hours": _int(0, 720),
    "battery_alert_percent": _int(0, 100),
    "networkmap_hour": _int(-1, 23),
    "ha_entities": _bool,
    "weekly_summary": _bool,
    "summary_day": _int(0, 6),
    "summary_hour": _int(0, 23),
    "wifi_channels": _wifi,
    "chatty_per_hour": _int(60, 36000),
    "disabled_checks": _kinds,
    "notify_checks": _kinds,
}


def load_options() -> dict:
    options = dict(DEFAULT_OPTIONS)
    try:
        with open(OPTIONS_PATH, encoding="utf-8") as handle:
            options.update(json.load(handle) or {})
    except (OSError, ValueError):
        pass
    panels = {}
    for entry in options.get("z2m_panels") or []:
        if "=" in str(entry):
            topic, slug = str(entry).split("=", 1)
            panels[topic.strip()] = f"/app/{slug.strip()}"
    options["_panels"] = panels
    if not options.get("alert_on_join", True) and "joined" in options["notify_checks"]:
        options["notify_checks"] = [k for k in options["notify_checks"] if k != "joined"]
    return options


def local_timezone():
    try:
        return ZoneInfo(os.environ.get("TZ") or "UTC")
    except Exception:  # noqa: BLE001 - unknown zone name falls back to UTC
        return ZoneInfo("UTC")


def load_oui() -> dict[str, str]:
    try:
        with open(APP_DIR / "oui.json", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


options = load_options()
base_topics = [topic.strip().strip("/") for topic in options.get("base_topics") or ["zigbee2mqtt"] if topic.strip()]
metrics = Metrics(METRICS_PATH)
tracker = Tracker(DATA_PATH, base_topics, CONFIG_DIR, local_timezone(), metrics, options, load_oui())
home_assistant = HomeAssistant(CONFIG_DIR)
tracker.battery_lib = BatteryLibrary(APP_DIR / "battery_library.json")
discovery = Discovery(tracker, VERSION, bool(options.get("ha_entities", True)))
stop_event = threading.Event()
scan_lock = threading.Lock()


# -------------------------------------------------------------------- alerts


class Alerts:
    """Sends notifications for new issues and notable events, off the MQTT thread."""

    def __init__(self) -> None:
        self.queue: queue.Queue = queue.Queue()
        threading.Thread(target=self._worker, name="alerts", daemon=True).start()

    def _worker(self) -> None:
        while True:
            job = self.queue.get()
            try:
                job()
            except Exception:  # noqa: BLE001 - never let a notification failure kill the worker
                _LOGGER.exception("Sending alert failed")

    def _send(self, key: str, kind: str, ieee: str, name: str, title: str, message: str, instance: str | None) -> None:
        data = {
            "key": key,
            "kind": kind,
            "ieee": ieee,
            "name": name,
            "title": title,
            "message": message,
            "network": instance,
        }
        _LOGGER.info("Alert: %s - %s", title, message)

        def job() -> None:
            if options.get("fire_events", True):
                home_assistant.fire_event("z2m_tracker_alert", data)
            if options.get("persistent_notifications"):
                home_assistant.persistent_notification(title, message, f"z2m_tracker_{key.replace(':', '_')}")
            if options.get("notify_service"):
                home_assistant.notify(options["notify_service"], title, message)

        self.queue.put(job)

    def _dismiss(self, key: str) -> None:
        if options.get("persistent_notifications"):
            self.queue.put(lambda: home_assistant.dismiss_notification(f"z2m_tracker_{key.replace(':', '_')}"))

    @staticmethod
    def notify_set() -> set[str]:
        if not options.get("alerts_enabled", True):
            return set()
        return set(options.get("notify_checks") or []) - set(options.get("disabled_checks") or [])

    def rebaseline(self) -> None:
        """Treat issues that already exist as notified, so enabling a type doesn't flood."""
        enabled = self.notify_set()
        with tracker.lock:
            current = sorted(issue["key"] for issue in tracker.issues if issue["kind"] in enabled)
            dismissed = set(tracker.data.get("alerted") or []) - set(current)
            tracker.data["alerted"] = current
            tracker.dirty = True
        for key in dismissed:
            self._dismiss(key)

    def test(self) -> list[str]:
        channels = []
        if options.get("persistent_notifications"):
            channels.append("HA notification")
        if options.get("notify_service"):
            channels.append(options["notify_service"])
        if options.get("fire_events", True):
            channels.append("z2m_tracker_alert event")
        self._send(
            "test",
            "test",
            "",
            "Zigbee Tracker",
            "Zigbee Tracker test",
            "Notifications from the Zigbee Tracker are working.",
            None,
        )
        return channels

    def on_event(self, event: dict) -> None:
        kind = event["kind"]
        if EVENT_KEYS.get(kind) not in self.notify_set():
            return
        name = event.get("name") or event["ieee"]
        vendor = tracker._oui_vendor(event["ieee"]) or "unknown manufacturer"
        messages = {
            "joined": f"{name} joined the {event['instance']} network (radio by {vendor}). If you weren't pairing anything, check it in the Zigbee Tracker.",
            "left": f"{name} left the {event['instance']} network.",
            "interview failed": f"{name} joined but its interview failed, so Zigbee2MQTT can't identify it.",
        }
        title = {
            "joined": "Zigbee device joined",
            "left": "Zigbee device left",
            "interview failed": "Zigbee interview failed",
        }.get(kind, f"Zigbee: {kind}")
        self._send(
            f"event:{kind}:{event['ieee']}:{event['time']}",
            kind,
            event["ieee"],
            name,
            title,
            messages.get(kind, event.get("detail") or ""),
            event.get("instance"),
        )

    def check(self) -> None:
        issues, _ = tracker.evaluate_health()
        enabled = self.notify_set()
        current = {issue["key"]: issue for issue in issues if issue["kind"] in enabled}
        with tracker.lock:
            initialised = tracker.data.get("alerts_initialised")
            sent = set(tracker.data.get("alerted") or [])
            tracker.data["alerted"] = sorted(current)
            tracker.data["alerts_initialised"] = True
            tracker.dirty = True
        if not initialised:
            _LOGGER.info("Alerts initialised with %d existing issues (not notified)", len(current))
            return
        for key in sorted(set(current) - sent):
            issue = current[key]
            self._send(
                key,
                issue["kind"],
                issue["ieee"],
                issue["name"],
                f"Zigbee: {issue['name']} - {issue['label']}",
                issue["detail"],
                issue.get("instance"),
            )
        for key in sent - set(current):
            self._dismiss(key)


alerts = Alerts()
tracker.on_event = alerts.on_event


# ---------------------------------------------------------------------- MQTT


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        _LOGGER.error("MQTT connect failed: %s", reason_code)
        return
    _LOGGER.info("Connected to MQTT, subscribing to %s", ", ".join(f"{topic}/#" for topic in base_topics))
    client.subscribe(
        [(f"{topic}/#", 0) for topic in base_topics]
        + [(f"{DISCOVERY_BASE}/+/reconfigure", 0), (f"{DISCOVERY_BASE}/+/battery_replaced", 0)]
    )
    client.publish(AVAILABILITY, "online", qos=1, retain=True)
    threading.Thread(target=discovery.sync, kwargs={"force": True}, name="discovery", daemon=True).start()


def on_disconnect(client, userdata, flags, reason_code, properties):
    if not stop_event.is_set():
        _LOGGER.warning("MQTT disconnected (%s), reconnecting", reason_code)


def on_message(client, userdata, message):
    topic = message.topic
    if topic.startswith(f"{DISCOVERY_BASE}/") and topic.endswith("/battery_replaced"):
        ieee = topic.split("/")[1]
        _LOGGER.info("Battery replacement recorded from Home Assistant for %s", ieee)
        threading.Thread(
            target=record_battery_replaced,
            args=(ieee, datetime.now(tracker.local_tz).date().isoformat(), "Home Assistant", ""),
            daemon=True,
        ).start()
        return
    if topic.startswith(f"{DISCOVERY_BASE}/") and topic.endswith("/reconfigure"):
        ieee = topic.split("/")[1]
        _LOGGER.info("Reconfigure requested from Home Assistant for %s", ieee)
        threading.Thread(target=tracker.reconfigure, args=(ieee,), name="reconfigure", daemon=True).start()
        return
    for base_topic in base_topics:
        if topic.startswith(f"{base_topic}/"):
            tracker.handle_message(base_topic, topic, message.payload)
            return


def start_mqtt() -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"z2m-tracker-{tracker.boot_id}")
    user = os.environ.get("MQTT_USER")
    if user:
        client.username_pw_set(user, os.environ.get("MQTT_PASS") or None)
    if (os.environ.get("MQTT_SSL") or "").lower() == "true":
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.will_set(AVAILABILITY, "offline", qos=1, retain=True)
    discovery.publish = lambda topic, payload, retain: client.publish(topic, payload, qos=1, retain=retain)
    host = os.environ.get("MQTT_HOST") or "core-mosquitto"
    port = int(os.environ.get("MQTT_PORT") or 1883)
    client.connect_async(host, port, keepalive=60)
    client.loop_start()
    tracker.publish = lambda topic, payload: client.publish(topic, json.dumps(payload), qos=0)
    return client


# ------------------------------------------------------------------ threads


def run_networkmap(instance: str | None = None) -> bool:
    if not scan_lock.acquire(blocking=False):
        return False

    def job() -> None:
        try:
            _LOGGER.info("Routing scan started (%s)", instance or "all networks")
            result = tracker.scan_networkmap(instance)
            _LOGGER.info("Routing scan finished: %s", result)
        except Exception:  # noqa: BLE001 - scans are best effort
            _LOGGER.exception("Routing scan failed")
        finally:
            scan_lock.release()

    threading.Thread(target=job, name="networkmap", daemon=True).start()
    return True


def record_battery_replaced(ieee: str, date: str, source: str, note: str) -> dict | None:
    entry = tracker.add_battery_replacement(ieee, date, source, note)
    if entry is None:
        return None
    device_id = tracker.ha_device_ids.get(ieee)
    if device_id and ieee in tracker.ha_battery:
        when = (
            f"{date}T12:00:00"
            if date != datetime.now(tracker.local_tz).date().isoformat()
            else datetime.now(tracker.local_tz).isoformat(timespec="seconds")
        )
        entry["battery_notes_synced"] = home_assistant.battery_notes_replaced(device_id, when)
        if entry["battery_notes_synced"]:
            tracker.ha_battery.setdefault(ieee, {})["last_replaced"] = when
    threading.Thread(target=discovery.sync, daemon=True).start()
    return entry


def settings_payload() -> dict:
    with tracker.lock:
        now_ts = time.time()
        muted = [
            {"ieee": r["ieee"], "name": r.get("friendly_name") or r["ieee"], "kind": kind, "until": until}
            for r in tracker.data["devices"].values()
            for kind, until in mutes_of(r).items()
            if until is None or datetime.fromisoformat(until).timestamp() > now_ts
        ]
        networkmap = dict(tracker.data.get("networkmap") or {})
    return {
        "settings": {key: options.get(key) for key in SETTINGS},
        "checks": [{"key": k, "label": label, "description": d, "event": e} for k, label, d, e in CHECKS],
        "networks": [{"topic": t, "label": t, "panel": options["_panels"].get(t)} for t in base_topics],
        "muted": sorted(muted, key=lambda m: m["name"].lower()),
        "entities": len(discovery._configs),
        "networkmap": networkmap,
        "version": VERSION,
        "home_assistant": home_assistant.available,
    }


def apply_settings(changes: dict) -> dict:
    clean = {}
    errors = {}
    for key, value in (changes or {}).items():
        validator = SETTINGS.get(key)
        if validator is None:
            errors[key] = "not a setting"
            continue
        try:
            clean[key] = validator(value)
        except ValueError as err:
            errors[key] = str(err)
    if errors:
        return {"ok": False, "errors": errors}
    options.update(clean)
    options["alert_on_join"] = "joined" in (options.get("notify_checks") or [])
    if "ha_entities" in clean:
        discovery.enabled = clean["ha_entities"]
    if {
        "disabled_checks",
        "offline_alert_minutes",
        "frozen_alert_hours",
        "battery_alert_percent",
        "chatty_per_hour",
    } & set(clean):
        tracker.evaluate_health()
    if {"alerts_enabled", "notify_checks", "disabled_checks"} & set(clean):
        alerts.rebaseline()
    threading.Thread(target=discovery.sync, daemon=True).start()
    with tracker.lock:
        tracker._changed(structural=True)
    persisted = home_assistant.save_app_options({k: v for k, v in options.items() if not k.startswith("_")})
    _LOGGER.info("Settings changed: %s (saved to app configuration: %s)", ", ".join(clean), persisted)
    return {"ok": True, "persisted": persisted, **settings_payload()}


def refresh_home_assistant() -> None:
    try:
        home_assistant.refresh()
        with tracker.lock:
            tracker.ha_device_ids = dict(home_assistant.device_ids)
            tracker.ha_battery = dict(home_assistant.battery)
            tracker._changed(structural=True)
        _LOGGER.info(
            "Linked %d HA devices, %d with Battery Notes", len(home_assistant.device_ids), len(home_assistant.battery)
        )
    except Exception:  # noqa: BLE001 - HA lookups are optional
        _LOGGER.exception("Reading Home Assistant registries failed")


def scheduler() -> None:
    last_save = last_metrics = last_ha = last_health = 0.0
    last_scan_day = None
    while not stop_event.wait(5):
        now = time.time()
        try:
            if now - last_save >= SAVE_INTERVAL:
                tracker.save()
                last_save = now
            if now - last_metrics >= METRICS_SAVE_INTERVAL:
                metrics.save()
                last_metrics = now
            if now - last_ha >= HA_REFRESH_INTERVAL:
                refresh_home_assistant()
                last_ha = now
            if now - last_health >= HEALTH_INTERVAL:
                alerts.check()
                discovery.sync()
                last_health = now
            hour = int(options.get("networkmap_hour", -1))
            local = datetime.now(tracker.local_tz)
            if hour >= 0 and local.hour == hour and last_scan_day != local.date():
                last_scan_day = local.date()
                run_networkmap()
            if (
                options.get("weekly_summary")
                and local.weekday() == int(options.get("summary_day", 0))
                and local.hour == int(options.get("summary_hour", 9))
                and tracker.data.get("summary_sent") != local.date().isoformat()
            ):
                with tracker.lock:
                    tracker.data["summary_sent"] = local.date().isoformat()
                    tracker.dirty = True
                send_weekly_summary()
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            _LOGGER.exception("Scheduler step failed")


def weekly_summary_text() -> tuple[str, str]:
    now = time.time()
    week_ago = datetime.fromtimestamp(now - 7 * 86400, tz=timezone.utc).isoformat(timespec="seconds")
    with tracker.lock:
        issues = [i for i in tracker.issues if i["severity"] == "warning"]
        events = [e for e in tracker.data["events"] if (e.get("time") or "") >= week_ago]
        active = sum(
            1 for r in tracker.data["devices"].values() if r["status"] == "active" and r.get("type") != "Coordinator"
        )
    shopping = tracker.battery_shopping(60)
    joined = sorted({e.get("name") or e["ieee"] for e in events if e["kind"] in ("added", "joined")})
    removed = sorted({e.get("name") or e["ieee"] for e in events if e["kind"] == "removed"})
    lines = [f"{active} active devices."]
    if issues:
        lines.append(
            f"Needs attention ({len(issues)}): "
            + "; ".join(f"{i['name']} ({i['label'].lower()})" for i in issues[:8])
            + ("…" if len(issues) > 8 else "")
        )
    else:
        lines.append("Nothing needs attention.")
    if shopping:
        lines.append(
            "Batteries for the next 60 days: " + ", ".join(f"{g['count']}× {g['type']}" for g in shopping) + "."
        )
    if joined:
        lines.append("New this week: " + ", ".join(joined[:6]) + ".")
    if removed:
        lines.append("Removed this week: " + ", ".join(removed[:6]) + ".")
    with tracker.lock:
        for bt, bridge in (tracker.data.get("bridges") or {}).items():
            outages = [o for o in bridge.get("outages", []) if (o.get("start") or "") >= week_ago]
            if outages:
                net = "Main" if bt == "zigbee2mqtt" else bt.replace("zigbee2mqtt_", "").replace("_", " ").capitalize()
                lines.append(
                    f"{net} network went offline {len(outages)} time{'s' if len(outages) != 1 else ''} this week."
                )
    title = f"Zigbee weekly summary: {len(issues)} need attention" if issues else "Zigbee weekly summary: all good"
    return title, "\n".join(lines)


def send_weekly_summary() -> dict:
    title, message = weekly_summary_text()
    _LOGGER.info("Weekly summary: %s", title)

    def job() -> None:
        if options.get("fire_events", True):
            home_assistant.fire_event("z2m_tracker_summary", {"title": title, "message": message})
        if options.get("alerts_enabled", True):
            if options.get("persistent_notifications"):
                home_assistant.persistent_notification(title, message, "z2m_tracker_weekly")
            if options.get("notify_service"):
                home_assistant.notify(options["notify_service"], title, message)

    alerts.queue.put(job)
    return {"title": title, "message": message}


def first_import() -> None:
    if not tracker.lists_ready.wait(FIRST_IMPORT_WAIT):
        _LOGGER.info("Not every Z2M instance published a device list; importing anyway")
    try:
        result = tracker.import_history()
        tracker.save()
        _LOGGER.info("Imported history: %s", result)
    except Exception:  # noqa: BLE001 - import is best effort, never kill the app
        _LOGGER.exception("History import failed")


# --------------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = f"Z2MTracker/{VERSION}"

    def log_message(self, format, *args):  # noqa: A002 - signature defined by base class
        return

    def _allowed(self) -> bool:
        if self.client_address[0] in ALLOWED_CLIENTS:
            return True
        self.send_error(403)
        return False

    def _send(
        self, status: int, body: bytes, content_type: str = "application/json", extra: dict | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"))

    def _body(self, limit: int = 100_000) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > limit:
            return {}
        try:
            parsed = json.loads(self.rfile.read(length))
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if not self._allowed():
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, (WWW / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path.startswith("/fonts/"):
            font = FONTS.get(path.removeprefix("/fonts/"))
            if font is not None:
                body = font.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "font/woff2")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)
        elif path == "/api/devices":
            self._send(200, tracker.snapshot_json())
        elif path == "/api/settings":
            self._json(200, settings_payload())
        elif path == "/api/version":
            self._json(200, {"version": tracker.version, "app": VERSION})
        elif path == "/api/scans":
            query = dict(
                part.split("=", 1)
                for part in (self.path.split("?", 1)[1] if "?" in self.path else "").split("&")
                if "=" in part
            )
            net = query.get("net", "")
            if net not in base_topics:
                self._json(400, {"error": "Unknown network"})
            else:
                self._json(200, {"scans": tracker.scan_history(net)})
        elif path == "/api/backup":
            stamp = datetime.now().strftime("%Y-%m-%d")
            self._send(
                200,
                tracker.backup(),
                "application/json",
                {"Content-Disposition": f'attachment; filename="zigbee-tracker-backup-{stamp}.json"'},
            )
        elif path == "/api/areas":
            try:
                self._json(200, {"areas": home_assistant.list_areas()})
            except Exception as err:  # noqa: BLE001 - report HA errors to the caller
                self._json(502, {"error": str(err)})
        elif path == "/api/export.csv":
            stamp = datetime.now().strftime("%Y-%m-%d")
            self._send(
                200,
                tracker.export_csv(),
                "text/csv; charset=utf-8",
                {"Content-Disposition": f'attachment; filename="zigbee-devices-{stamp}.csv"'},
            )
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        if not self._allowed():
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/restore":
            payload = self._body(limit=50_000_000)
            try:
                result = tracker.restore(payload)
            except Exception as err:  # noqa: BLE001 - report restore errors to the caller
                _LOGGER.exception("Restore failed")
                result = {"ok": False, "error": str(err)}
            if result.get("ok"):
                threading.Thread(target=discovery.sync, kwargs={"force": True}, daemon=True).start()
            self._json(200 if result["ok"] else 400, result)
            return
        body = self._body()
        ieee = str(body.get("ieee") or "")
        try:
            if path == "/api/note":
                ok = tracker.set_note(ieee, str(body.get("note") or "")[:2000])
                self._json(200 if ok else 404, {"ok": ok})
            elif path == "/api/asset":
                ok = tracker.set_asset(ieee, body.get("asset") if isinstance(body.get("asset"), dict) else {})
                self._json(200 if ok else 404, {"ok": ok})
            elif path == "/api/mute":
                days = body.get("days")
                ok = tracker.set_muted(
                    ieee,
                    str(body.get("kind") or ""),
                    bool(body.get("muted")),
                    float(days) if isinstance(days, (int, float)) and days > 0 else None,
                )
                self._json(200 if ok else 404, {"ok": ok})
            elif path == "/api/bindings/ack":
                ok = tracker.acknowledge_bindings(ieee)
                self._json(200 if ok else 404, {"ok": ok})
            elif path == "/api/link":
                result = tracker.link(str(body.get("old") or ""), str(body.get("new") or ""))
                self._json(200 if result["ok"] else 400, result)
            elif path == "/api/unlink":
                self._json(200, tracker.unlink(str(body.get("old") or ""), str(body.get("new") or "")))
            elif path == "/api/reconfigure":
                result = tracker.reconfigure(ieee)
                self._json(200 if result["ok"] else 502, result)
            elif path == "/api/networkmap":
                instance = body.get("instance") if body.get("instance") in base_topics else None
                started = run_networkmap(instance)
                self._json(
                    200 if started else 409, {"ok": started, "error": None if started else "A scan is already running"}
                )
            elif path == "/api/forget":
                ok = tracker.forget(ieee)
                self._json(200 if ok else 404, {"ok": ok})
            elif path == "/api/settings":
                result = apply_settings(body.get("changes") if isinstance(body.get("changes"), dict) else {})
                self._json(200 if result["ok"] else 400, result)
            elif path == "/api/test-notification":
                self._json(200, {"ok": True, "channels": alerts.test()})
            elif path == "/api/battery":
                ok = tracker.set_battery_info(ieee, body.get("info") if isinstance(body.get("info"), dict) else {})
                threading.Thread(target=discovery.sync, daemon=True).start()
                self._json(200 if ok else 404, {"ok": ok})
            elif path == "/api/battery/replaced":
                date = str(body.get("date") or datetime.now(tracker.local_tz).date().isoformat())
                entry = record_battery_replaced(ieee, date, "Zigbee Tracker", str(body.get("note") or ""))
                self._json(
                    200 if entry else 400,
                    {"ok": bool(entry), "entry": entry, "error": None if entry else "Invalid device or date"},
                )
            elif path == "/api/battery/remove":
                ok = tracker.remove_battery_replacement(ieee, int(body.get("index", -1)))
                threading.Thread(target=discovery.sync, daemon=True).start()
                self._json(200 if ok else 404, {"ok": ok})
            elif path == "/api/rename":
                result = tracker.rename(ieee, str(body.get("name") or ""))
                self._json(200 if result["ok"] else 400, result)
            elif path == "/api/area":
                device_id = tracker.ha_device_ids.get(ieee)
                if not device_id:
                    self._json(400, {"ok": False, "error": "This device isn't in Home Assistant's device list"})
                else:
                    area_id = body.get("area_id") or None
                    home_assistant.set_device_area(device_id, area_id)
                    names = {a["id"]: a["name"] for a in home_assistant.list_areas()}
                    with tracker.lock:
                        record = tracker.data["devices"].get(ieee)
                        if record is not None:
                            record["area"] = names.get(area_id) if area_id else None
                            tracker._changed(structural=True)
                    threading.Thread(target=discovery.sync, daemon=True).start()
                    self._json(200, {"ok": True})
            elif path == "/api/pairing":
                instance = body.get("instance") if body.get("instance") in base_topics else base_topics[0]
                result = tracker.start_pairing(
                    instance,
                    int(body.get("seconds") or 0),
                    str(body.get("via") or "") or None,
                    str(body.get("target") or "") or None,
                )
                self._json(200 if result["ok"] else 502, result)
            elif path == "/api/ota":
                result = tracker.ota(ieee, str(body.get("action") or ""))
                self._json(200 if result["ok"] else 502, result)
            elif path == "/api/summary":
                self._json(200, {"ok": True, **send_weekly_summary()})
            elif path == "/api/import":
                self._json(200, tracker.import_history())
            else:
                self.send_error(404)
        except Exception as err:  # noqa: BLE001 - report errors to the caller
            _LOGGER.exception("API %s failed", path)
            self._json(500, {"ok": False, "error": str(err)})


def main() -> None:
    source = tracker.load(migrate_from=MIGRATE_FROM)
    metrics.load()
    tracker.load_scans(SCANS_PATH)
    with tracker.lock:
        count = len(tracker.data["devices"])
    _LOGGER.info("Z2M Device Tracker %s: %d devices loaded (%s), base topics: %s", VERSION, count, source, base_topics)
    if source == "migrated":
        tracker.save(force=True)

    client = start_mqtt()
    threading.Thread(target=scheduler, name="scheduler", daemon=True).start()
    if not tracker.data.get("imported"):
        threading.Thread(target=first_import, name="first-import", daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True

    def shutdown(signum, frame):
        _LOGGER.info("Stopping, saving history")
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        server.serve_forever()
    finally:
        client.publish(AVAILABILITY, "offline", qos=1, retain=True).wait_for_publish(2)
        client.loop_stop()
        client.disconnect()
        tracker.save(force=True)
        metrics.save(force=True)
        time.sleep(0.2)


if __name__ == "__main__":
    main()
