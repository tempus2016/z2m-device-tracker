"""Z2M Device Tracker app: MQTT listener, health monitor, alerts and web UI/API on the ingress port."""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import html
import json
import logging
import os
import queue
import re
import secrets
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, unquote_plus, urlsplit
from zoneinfo import ZoneInfo

import alert_actions
import daily_summary
import escalation
import feedback
import paho.mqtt.client as mqtt
import prometheus
import standalone
from battery_library import BatteryLibrary, clean_shop_url
from broker_stats import FILTER as SYS_FILTER
from broker_stats import BrokerStats
from community import CommunityRatings
from discovery import AVAILABILITY, Discovery
from discovery import BASE as DISCOVERY_BASE
from discovery import PREFIX as DISCOVERY_PREFIX
from ha import HomeAssistant, plan_entity_ids
from health import CHECKS, DEFAULT_NOTIFY, alert_title, mutes_of, nested, notify_kinds, offer_new_kinds
from matter import MATTER_INSTANCE, MatterSource
from metrics import Metrics
from mqtt_status import NOTIFICATION_ID, NOTIFICATION_TITLE, MqttStatus, explain_error, explain_refusal, probe
from naming import clean_pattern
from ota import OtaManager, parse_window
from reporting import clean_fix as clean_reporting_fix
from retained import OLD_NAME, RetainedStore
from retained import collect as collect_retained
from tracker import Tracker
from tracker_base import IEEE_RE, base_topic_list, shopping_text
from tracker_base import _display_name as tracker_display_name
from usage import UsageRefresher
from zha import ZHA_BLOCKED_PATHS, ZHA_INSTANCE, ZhaSource, sources, zha_checks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
_LOGGER = logging.getLogger("z2m_tracker")
# The last warnings and errors, for Settings > Help & feedback.
LOG_BUFFER = feedback.LogBuffer()
logging.getLogger().addHandler(LOG_BUFFER)

VERSION = "2.5.0"
PORT = 8099
# Prometheus scrape port: only opened when metrics_token is set.
METRICS_PORT = 9099
METRICS_TOKEN_MIN = 16
# Plain Docker, no Supervisor (Home Assistant Container): see standalone.py.
STANDALONE = standalone.is_standalone()
DATA_DIR = os.environ.get("DATA_DIR") or "/data"
DATA_PATH = f"{DATA_DIR}/history.json"
METRICS_PATH = f"{DATA_DIR}/metrics.json"
SCANS_PATH = f"{DATA_DIR}/scans.json"
OPTIONS_PATH = f"{DATA_DIR}/options.json"
TOKEN_PATH = f"{DATA_DIR}/api_token"
COMMUNITY_PATH = f"{DATA_DIR}/community_ratings.json"
COMMUNITY_ID_PATH = f"{DATA_DIR}/community_id"
CONFIG_DIR = os.environ.get("HA_CONFIG_DIR") or "/homeassistant"
MIGRATE_FROM = f"{CONFIG_DIR}/.storage/z2m_tracker"
APP_DIR = Path(__file__).parent
WWW = APP_DIR / "www"
I18N_DIR = WWW / "i18n"
LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$")
# Fonts are served from this fixed list, so a request path never becomes a file path.
FONTS = {font.name: font for font in (WWW / "fonts").glob("*.woff2")}
SAVE_INTERVAL = 30
METRICS_SAVE_INTERVAL = 900
HEALTH_INTERVAL = 60
HA_REFRESH_INTERVAL = 6 * 3600
# Wait this long after a registry change before re-reading, so a burst of changes
# (a new device brings a dozen entities) costs one refresh.
HA_CHANGE_DELAY = 5
FIRST_IMPORT_WAIT = 120

# Who may use the API. Supervisor's ingress proxy has already checked the HA login (and that the user
# is an admin), so it needs nothing more. Everything else, including HA Core, which shares its address
# with every host-network app, must send the API token. Supervisor hands that token to the integration
# through discovery, which only Core can read.
INGRESS_PROXY = "172.30.32.2"
LOOPBACK = {"127.0.0.1", "::1"}
# Commands from Home Assistant over MQTT (reconfigure, battery replaced): one per device per minute.
COMMAND_COOLDOWN = 60

DEFAULT_OPTIONS = {
    "zigbee2mqtt": True,
    "zha": False,
    "matter": False,
    "base_topics": ["zigbee2mqtt"],
    "z2m_panels": [],
    "persistent_notifications": True,
    "notify_service": "",
    "alert_actions": True,
    "alert_on_join": True,
    "offline_alert_minutes": 60,
    "critical_offline_minutes": 5,
    "frozen_alert_hours": 24,
    "battery_alert_percent": 15,
    "networkmap_hour": 3,
    "networkmap_timeout_minutes": 60,
    "ha_entities": True,
    "weekly_summary": True,
    "summary_day": 0,
    "summary_hour": 9,
    "daily_summary": False,
    "alerts_enabled": True,
    "fire_events": True,
    "ha_repairs": True,
    "disabled_checks": [],
    "notify_checks": list(DEFAULT_NOTIFY),
    "wifi_channels": "",
    "chatty_per_hour": 1200,
    "ota_window": "",
    "ota_min_battery": 30,
    "battery_auto_detect": True,
    "battery_confirm_detected": False,
    "quiet_hours": False,
    "learn_rhythm": True,
    "expected_outages": True,
    "quiet_start": "23:00",
    "quiet_end": "07:00",
    "escalate_enabled": False,
    "escalate_hours": 2,
    "escalate_service": "",
    "escalate_critical_only": True,
    "naming_pattern": "",
    "battery_shop_url": "",
    "retained_autoclear": False,
    "broker_stats": True,
    "impossible_temp_min": -30,
    "impossible_temp_max": 60,
    "humidity_stuck_hours": 6,
    "community_ratings_fetch": True,
}

CHECK_KEYS = {key for key, _, _, _ in CHECKS}
# Companion app data for a critical device's alert: Android delivers it at once (high priority,
# no time to live), iOS shows it as time-sensitive, which breaks through Focus modes.
CRITICAL_PUSH = {"priority": "high", "ttl": 0, "push": {"interruption-level": "time-sensitive"}}
EVENT_KEYS = {"joined": "joined", "left": "left", "interview failed": "interview_failed"}
# Health checks a "Mute 24 h" button may mute (events like joined happen once, so there is nothing to mute).
MUTABLE_KINDS = {key for key, _, _, event in CHECKS if not event}
# Alerts that don't get a "Used by" line: nothing in HA uses a device that has only just joined.
NO_USAGE_KINDS = {"joined", "interview failed", "test", "digest"}


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
    """A notify service, as notify.<name> (a bare name gets notify. added). Nothing else may be called."""
    value = str(value or "").strip()
    if value and "." not in value:
        value = f"notify.{value}"
    if value and not re.fullmatch(r"notify\.[a-z0-9_]+", value):
        raise ValueError("must be a notify service, like notify.mobile_app_phone")
    return value


def _wifi(value):
    parts = [p.strip() for p in str(value or "").replace(";", ",").split(",") if p.strip()]
    if len(parts) > 6 or any(not p.isdigit() or not 1 <= int(p) <= 14 for p in parts):
        raise ValueError("enter 2.4 GHz Wi-Fi channels from 1 to 14, separated by commas")
    return ", ".join(str(int(p)) for p in dict.fromkeys(parts))


def _ota_window(value):
    value = str(value or "").replace(" ", "")
    if value and parse_window(value) is None:
        raise ValueError("enter a time range like 02:00-05:00, or leave it empty")
    return value


def _clock(value):
    value = str(value or "").strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("enter a time like 23:00")
    return value


def _kinds(value):
    if not isinstance(value, list) or any(item not in CHECK_KEYS for item in value):
        raise ValueError("unknown check")
    return sorted(set(value))


SETTINGS = {
    "alerts_enabled": _bool,
    "persistent_notifications": _bool,
    "fire_events": _bool,
    "ha_repairs": _bool,
    "notify_service": _service,
    "offline_alert_minutes": _int(0, 10080),
    "critical_offline_minutes": _int(0, 1440),
    "frozen_alert_hours": _int(0, 720),
    "battery_alert_percent": _int(0, 100),
    "networkmap_hour": _int(-1, 23),
    "networkmap_timeout_minutes": _int(5, 240),
    "ha_entities": _bool,
    "weekly_summary": _bool,
    "summary_day": _int(0, 6),
    "summary_hour": _int(0, 23),
    "daily_summary": _bool,
    "wifi_channels": _wifi,
    "chatty_per_hour": _int(60, 36000),
    "disabled_checks": _kinds,
    "notify_checks": _kinds,
    "ota_window": _ota_window,
    "ota_min_battery": _int(0, 100),
    "battery_auto_detect": _bool,
    "battery_confirm_detected": _bool,
    "quiet_hours": _bool,
    "learn_rhythm": _bool,
    "expected_outages": _bool,
    "quiet_start": _clock,
    "quiet_end": _clock,
    "escalate_enabled": _bool,
    "escalate_hours": _int(1, 168),
    "escalate_service": _service,
    "escalate_critical_only": _bool,
    "naming_pattern": clean_pattern,
    "battery_shop_url": clean_shop_url,
    "retained_autoclear": _bool,
    "broker_stats": _bool,
    "impossible_temp_min": _int(-100, 100),
    "impossible_temp_max": _int(-100, 100),
    "humidity_stuck_hours": _int(1, 168),
    "community_ratings_fetch": _bool,
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
            panels[topic.strip("/")] = f"/app/{slug.strip()}"
            panels.setdefault(topic.strip().strip("/"), f"/app/{slug.strip()}")
    options["_panels"] = panels
    try:
        options["notify_service"] = _service(options.get("notify_service"))
    except ValueError:
        _LOGGER.warning("Ignoring notify_service %r: only notify services are allowed", options.get("notify_service"))
        options["notify_service"] = ""
    try:
        options["naming_pattern"] = clean_pattern(options.get("naming_pattern"))
    except ValueError as err:
        _LOGGER.warning("Ignoring naming_pattern %r: %s", options.get("naming_pattern"), err)
        options["naming_pattern"] = ""
    try:
        options["battery_shop_url"] = clean_shop_url(options.get("battery_shop_url"))
    except ValueError as err:
        _LOGGER.warning("Ignoring battery_shop_url %r: %s", options.get("battery_shop_url"), err)
        options["battery_shop_url"] = ""
    if not options.get("alert_on_join", True) and "joined" in options["notify_checks"]:
        options["notify_checks"] = [k for k in options["notify_checks"] if k != "joined"]
    return options


def app_panel() -> str | None:
    """This app's sidebar page, /app/<slug>. Supervisor names the container after the slug
    (7066b6f6_z2m_tracker runs as 7066b6f6-z2m-tracker); None when that doesn't look like one."""
    slug = socket.gethostname().replace("-", "_").lower()
    return f"/app/{slug}" if re.fullmatch(r"[a-z0-9]+_z2m_tracker", slug) else None


def offer_new_notify_kinds() -> None:
    with tracker.lock:
        checks, offered, added = offer_new_kinds(
            list(options.get("notify_checks") or []), list(tracker.data.get("notify_offered") or [])
        )
        if offered == sorted(tracker.data.get("notify_offered") or []):
            return
        tracker.data["notify_offered"] = offered
        tracker.dirty = True
    if not added:
        return
    options["notify_checks"] = checks
    _LOGGER.info("Alerts now notify for new check(s): %s", ", ".join(added))
    persisted = save_options()
    if not persisted:
        _LOGGER.warning("Couldn't save the updated alert list to the app configuration")


def save_options() -> bool:
    """Keep the options for the next start: in Supervisor's app configuration, or in plain Docker
    in /data/options.json, the file the app reads them from."""
    current = {k: v for k, v in options.items() if not k.startswith("_")}
    if STANDALONE:
        return standalone.save_options(OPTIONS_PATH, current)
    return home_assistant.save_app_options(current)


def local_timezone():
    try:
        return ZoneInfo(os.environ.get("TZ") or "UTC")
    except Exception:  # noqa: BLE001 - unknown zone name falls back to UTC
        return ZoneInfo("UTC")


def load_api_token() -> str:
    """The token HA Core sends with each request; made once and kept in /data (readable by this app only)."""
    try:
        with open(TOKEN_PATH, encoding="utf-8") as handle:
            token = handle.read().strip()
        if len(token) >= 32:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    try:
        fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token)
    except OSError as err:
        _LOGGER.warning("Couldn't save the API token (%s); the integration reconnects after a restart", err)
    return token


def load_oui() -> dict[str, str]:
    try:
        with open(APP_DIR / "oui.json", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


options = load_options()
API_TOKEN = load_api_token()
SOURCES = sources(options)
# Zigbee2MQTT networks (one per base topic) and the ZHA network can be read side by side.
# `stack` is "z2m" whenever any Zigbee2MQTT network is read: what needs MQTT runs only then.
stack = "z2m" if "z2m" in SOURCES else "zha"
base_topics = (
    (base_topic_list(options.get("base_topics")) if "z2m" in SOURCES else [])
    + ([ZHA_INSTANCE] if "zha" in SOURCES else [])
    + ([MATTER_INSTANCE] if "matter" in SOURCES else [])
)
# The Zigbee2MQTT base topics only: what is read and sent over MQTT. The ZHA network is not a topic.
mqtt_topics = [t for t in base_topics if t not in (ZHA_INSTANCE, MATTER_INSTANCE)]
if not SOURCES:
    _LOGGER.warning("zigbee2mqtt, zha and matter are all switched off: there is nothing to read")
for _topic in base_topics:
    if _topic != _topic.strip():
        _LOGGER.warning("Base topic %r starts or ends with a space; it is used exactly as written", _topic)
metrics = Metrics(METRICS_PATH)
tracker = Tracker(DATA_PATH, base_topics, CONFIG_DIR, local_timezone(), metrics, options, load_oui())
HA_URL = ""
if STANDALONE:
    _endpoints = standalone.ha_endpoints(os.environ.get("HA_URL"))
    if _endpoints and os.environ.get("HA_TOKEN"):
        HA_URL = (os.environ.get("HA_URL") or "").strip().rstrip("/")
        home_assistant = HomeAssistant(CONFIG_DIR, *_endpoints, token=os.environ.get("HA_TOKEN") or "")
    else:
        if os.environ.get("HA_URL") and not _endpoints:
            _LOGGER.warning("Ignoring HA_URL %r: use an http:// or https:// address", os.environ.get("HA_URL"))
        _LOGGER.warning("No Home Assistant access: set HA_URL and HA_TOKEN (a long-lived access token)")
        home_assistant = HomeAssistant(CONFIG_DIR, token="")
else:
    home_assistant = HomeAssistant(CONFIG_DIR)
home_assistant.stack = stack
retained_store = RetainedStore()
tracker.retained = retained_store
broker_stats = BrokerStats()
tracker.broker_stats = broker_stats
tracker.battery_lib = BatteryLibrary(APP_DIR / "battery_library.json")
community = CommunityRatings(APP_DIR / "community_ratings.json", Path(COMMUNITY_PATH), Path(COMMUNITY_ID_PATH))
tracker.community = community
discovery = Discovery(tracker, VERSION, bool(options.get("ha_entities", True)))
stop_event = threading.Event()
ha_changed_at = 0.0


def ha_registry_changed() -> None:
    global ha_changed_at
    ha_changed_at = time.time()


scan_lock = threading.Lock()
ping_lock = threading.Lock()
ota_manager = OtaManager(tracker, options, home_assistant.logbook)
usage_refresher = UsageRefresher(tracker, home_assistant)


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

    def _send(
        self,
        key: str,
        kind: str,
        ieee: str,
        name: str,
        title: str,
        message: str,
        instance: str | None,
        critical: bool = False,
    ) -> None:
        data = {
            "key": key,
            "kind": kind,
            "ieee": ieee,
            "name": name,
            "title": title,
            "message": message,
            "network": instance,
        }
        push = None
        if critical:
            data["critical"] = True
            push = dict(CRITICAL_PUSH)
        if options.get("notify_service") and options.get("alert_actions", True):
            push = {**(push or {}), **self.phone_extras(key, kind, ieee)}
        _LOGGER.info("Alert: %s - %s", title, message)

        def job() -> None:
            nonlocal message
            # Say what the problem breaks: the automations, scripts and scenes that use the device.
            if ieee and kind not in NO_USAGE_KINDS:
                try:
                    usage_refresher.refresh([ieee])
                    line = tracker.usage_line(ieee)
                except Exception:  # noqa: BLE001 - the alert still goes out without the line
                    _LOGGER.exception("Looking up what uses %s failed", ieee)
                    line = None
                if line:
                    message = f"{message}\n\n{line}"
                    data["message"] = message
            # Each way out on its own: one that fails must not stop the others.
            steps = []
            if options.get("fire_events", True):
                steps.append(lambda: home_assistant.fire_event("z2m_tracker_alert", data))
            if options.get("persistent_notifications"):
                note_id = f"z2m_tracker_{key.replace(':', '_')}"
                steps.append(lambda: home_assistant.persistent_notification(title, message, note_id))
            if options.get("notify_service"):
                steps.append(lambda: home_assistant.notify(options["notify_service"], title, message, push))
            for step in steps:
                try:
                    step()
                except Exception:  # noqa: BLE001 - the remaining channels still get the alert
                    _LOGGER.exception("Sending alert failed")

        self.queue.put(job)

    @staticmethod
    def buttons(kind: str, ieee: str) -> list[str]:
        """Buttons for one alert: none for network, group and event alerts, the digest or the test."""
        if kind not in MUTABLE_KINDS or not IEEE_RE.fullmatch(ieee or ""):
            return []
        with tracker.lock:
            record = tracker.data["devices"].get(ieee)
            if record is None:
                return []
            verbs = ["MUTE24"]
            if tracker.can_ping(record) is None:
                verbs.append("PING")
            if kind in alert_actions.BATTERY_KINDS and tracker.is_battery_device(record):
                verbs.append("BATTERY")
        return verbs

    def phone_extras(self, key: str, kind: str, ieee: str) -> dict:
        """Companion app data: buttons, a tag (a repeat of the same alert replaces the old one)
        and the page that opens on tap."""
        extras: dict = {"tag": "z2m_tracker_" + re.sub(r"[^A-Za-z0-9_]", "_", key)[:120]}
        panel = app_panel()
        if panel:
            url = f"{panel}/{ieee}" if IEEE_RE.fullmatch(ieee or "") else panel
            extras["url"] = url  # iOS
            extras["clickAction"] = url  # Android
        verbs = self.buttons(kind, ieee)
        if verbs:
            with tracker.lock:
                store = tracker.data.setdefault("alert_actions", {})
                extras["actions"] = alert_actions.issue(store, ieee, kind, verbs, time.time())
                tracker.dirty = True
        return extras

    def _dismiss(self, key: str) -> None:
        if options.get("persistent_notifications"):
            self.queue.put(lambda: home_assistant.dismiss_notification(f"z2m_tracker_{key.replace(':', '_')}"))

    @staticmethod
    def notify_set() -> set[str]:
        return notify_kinds(options)

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
        network = {ZHA_INSTANCE: "ZHA", MATTER_INSTANCE: "Matter"}.get(event["instance"], event["instance"])
        messages = {
            "joined": f"{name} joined the {network} network (radio by {vendor}). If you weren't pairing anything, check it in the Zigbee Tracker.",
            "left": f"{name} left the {network} network.",
            "interview failed": f"{name} joined but its interview failed, so Zigbee2MQTT can't identify it.",
        }
        title = {
            "joined": "Zigbee device joined",
            "left": "Zigbee device left",
            "interview failed": "Zigbee interview failed",
        }.get(kind, f"Zigbee: {kind}")
        if tracker.alerts_paused():
            # Events happen once, so there is nothing to send when the pause ends: drop it.
            _LOGGER.info("Alerts paused, not sent: %s - %s", title, messages.get(kind, ""))
            return
        critical = tracker.is_critical(event["ieee"])
        message = messages.get(kind, event.get("detail") or "")
        if not critical and tracker.hold_event(title, message):
            _LOGGER.info("Quiet hours, held for the digest: %s - %s", title, message)
            return
        self._send(
            f"event:{kind}:{event['ieee']}:{event['time']}",
            kind,
            event["ieee"],
            name,
            f"Critical: {title}" if critical else title,
            message,
            event.get("instance"),
            critical,
        )

    def check(self) -> None:
        issues, _ = tracker.evaluate_health()
        if tracker.alerts_paused():
            # Leave "alerted" alone, so issues found during the pause still count as new when it ends.
            # Issues that clear before then are never sent; notifications already shown stay up.
            return
        plan = tracker.plan_alerts(issues, self.notify_set())
        if plan["first"]:
            _LOGGER.info("Alerts initialised with existing issues (not notified)")
            return
        for issue in plan["send"]:
            critical = bool(issue.get("critical"))
            self._send(
                issue["key"],
                issue["kind"],
                issue["ieee"],
                issue["name"],
                issue.get("notify_title") or alert_title(issue),
                issue.get("notify_message") or issue["detail"],
                issue.get("instance"),
                critical,
            )
        for key in plan["dismiss"]:
            self._dismiss(key)
        if plan["digest"]:
            title, message = plan["digest"]
            self._send("digest", "digest", "", "Zigbee Tracker", title, message, None)
        self.escalate(issues, plan["send"])

    def escalate(self, issues: list[dict], sent: list[dict]) -> None:
        """Record the alerts just sent, and send unanswered ones again to the second service (escalation.py)."""
        conf = escalation.settings(options)
        now = time.time()
        enabled = self.notify_set()
        current = {issue["key"]: issue for issue in issues if issue["kind"] in enabled}
        with tracker.lock:
            store = escalation.clean(tracker.data.get("escalation"))
            before = len(store)
            if conf is None:
                # Off: nothing waits, so turning it on later doesn't escalate old alerts at once.
                store.clear()
                due = []
            else:
                escalation.track(store, sent, now, conf["critical_only"])
                due = escalation.due(store, current, now, conf["seconds"], tracker.in_quiet_hours(now))
            if due or store or before:
                tracker.data["escalation"] = store
                tracker.dirty = True
        for issue, waited in due:
            title, message = escalation.message(issue, alert_title(issue), waited)
            self._escalate(conf["service"], issue, title, message)

    def _escalate(self, service: str, issue: dict, title: str, message: str) -> None:
        key, kind, ieee = issue["key"], issue["kind"], issue.get("ieee") or ""
        push = dict(CRITICAL_PUSH) if issue.get("critical") else {}
        if options.get("alert_actions", True):
            push.update(self.phone_extras(key, kind, ieee))
        push["tag"] = "z2m_tracker_escalated_" + re.sub(r"[^A-Za-z0-9_]", "_", key)[:110]
        _LOGGER.info("Escalated to %s: %s - %s", service, title, message)
        data = {"key": key, "kind": kind, "ieee": ieee, "name": issue.get("name"), "title": title}
        data.update(message=message, network=issue.get("instance"), escalated=True, service=service)

        def job() -> None:
            if options.get("fire_events", True):
                home_assistant.fire_event("z2m_tracker_alert", data)
            home_assistant.notify(service, title, message, push)

        self.queue.put(job)


def acknowledge_alerts(ieee: str) -> None:
    """Someone answered the device's alert (pinged it, or pressed a button on a phone alert): no escalation."""
    with tracker.lock:
        store = tracker.data.get("escalation")
        if isinstance(store, dict) and escalation.acknowledge(store, ieee):
            tracker.dirty = True


alerts = Alerts()
tracker.on_event = alerts.on_event


def on_notification_action(data: dict) -> None:
    """A button on a phone alert was pressed (HA's mobile_app_notification_action event).
    Runs on the websocket thread: check the press here, do the work on its own thread."""
    with tracker.lock:
        store = tracker.data.get("alert_actions")
        claimed = alert_actions.claim(store, data.get("action"), time.time()) if isinstance(store, dict) else None
        if claimed:
            tracker.dirty = True
    if claimed is None:
        if str(data.get("action") or "").startswith(alert_actions.PREFIX):
            _LOGGER.warning("Ignored a notification button that is unknown, expired or already used")
        return
    threading.Thread(target=run_notification_action, args=claimed, name="alert-action", daemon=True).start()


def run_notification_action(verb: str, ieee: str, kind: str) -> None:
    acknowledge_alerts(ieee)
    with tracker.lock:
        record = tracker.data["devices"].get(ieee)
        name = (tracker_display_name(record) if record else None) or ieee
    if verb == "MUTE24":
        if kind in MUTABLE_KINDS and tracker.set_muted(ieee, kind, True, 1):
            _LOGGER.info("Muted %s on %s for 24 h from a phone alert", kind, name)
    elif verb == "PING":
        result = tracker.ping(ieee)
        if result.get("ok"):
            message = f"{name} answered the ping in {result['ms']} ms."
        elif result.get("timeout"):
            message = f"{name} didn't answer the ping within {result.get('seconds')} s."
        else:
            message = f"Couldn't ping {name}: {result.get('error')}"
        _LOGGER.info("Ping from a phone alert: %s", message)
        if options.get("notify_service"):
            tag = "z2m_tracker_ping_" + ieee
            alerts.queue.put(
                lambda: home_assistant.notify(options["notify_service"], "Zigbee ping", message, {"tag": tag})
            )
    elif verb == "BATTERY":
        if kind in alert_actions.BATTERY_KINDS:
            today = datetime.now(tracker.local_tz).date().isoformat()
            if record_battery_replaced(ieee, today, "Phone notification", ""):
                _LOGGER.info("Battery replaced on %s, from a phone alert", name)


# ---------------------------------------------------------------------- MQTT


if STANDALONE and not os.environ.get("MQTT_HOST") and options.get("mqtt_host"):
    # Plain Docker has no run.sh: the broker from the options file, the way run.sh would pass it on.
    os.environ["MQTT_HOST"] = str(options["mqtt_host"])
    os.environ["MQTT_PORT"] = str(options.get("mqtt_port") or 1883)
    os.environ["MQTT_USER"] = str(options.get("mqtt_user") or "")
    os.environ["MQTT_PASS"] = str(options.get("mqtt_password") or "")
    os.environ["MQTT_SSL"] = "true" if options.get("mqtt_ssl") else "false"
    os.environ["MQTT_SSL_VERIFY"] = "false" if options.get("mqtt_ssl_verify") is False else "true"
    os.environ["MQTT_MANUAL"] = "true"
MQTT_HOST = os.environ.get("MQTT_HOST") or ("localhost" if STANDALONE else "core-mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT") or 1883)
# Set by run.sh when the broker comes from this app's options rather than the Mosquitto app.
# In plain Docker every broker is one the user named, in the options file or in MQTT_HOST.
MQTT_MANUAL = os.environ.get("MQTT_MANUAL") == "true" or (STANDALONE and bool(os.environ.get("MQTT_HOST")))
mqtt_status = MqttStatus(MQTT_HOST, MQTT_PORT, configured=bool(os.environ.get("MQTT_HOST")))
discovery.connected = lambda: mqtt_status.connected
# Open pages and the integration all ask again within seconds of a change: they share one build.
tracker.snapshot_seconds = 5


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        line = mqtt_status.failed(explain_refusal(str(reason_code), manual=MQTT_MANUAL), refused=True)
        if line:
            _LOGGER.error("MQTT: %s", line)
        return
    if mqtt_status.up():
        alerts.queue.put(lambda: home_assistant.dismiss_notification(NOTIFICATION_ID))
    # Devices may have reported while we were away: Stopped reporting counts from now.
    tracker.mqtt_connected()
    _LOGGER.info("Connected to MQTT, subscribing to %s", ", ".join(f"{topic}/#" for topic in mqtt_topics))
    client.subscribe(
        [(f"{topic}/#", 0) for topic in mqtt_topics]
        + [(f"{DISCOVERY_BASE}/+/reconfigure", 0), (f"{DISCOVERY_BASE}/+/battery_replaced", 0)]
    )
    if options.get("broker_stats", True):
        client.subscribe(SYS_FILTER, 0)
        broker_stats.subscribed()
    client.publish(AVAILABILITY, "online", qos=1, retain=True)
    threading.Thread(target=discovery.sync, kwargs={"force": True}, name="discovery", daemon=True).start()
    retained_scan_soon(RETAINED_FIRST_DELAY)


def on_disconnect(client, userdata, flags, reason_code, properties):
    if stop_event.is_set():
        return
    if not mqtt_status.connected:
        # Dropped before the broker accepted us: a failed attempt, not a lost connection.
        # After a refusal, on_connect has already said why.
        if not mqtt_status.refused:
            on_connect_fail(client, userdata)
        return
    mqtt_status.lost()
    # Whatever the devices send while we're away is not heard: no rhythm gap spans it.
    tracker.mqtt_lost()
    _LOGGER.warning("MQTT disconnected (%s), reconnecting", reason_code)


def on_connect_fail(client, userdata):
    """paho retries on its own but doesn't say why; probe the broker to find out."""
    if stop_event.is_set():
        return
    line = mqtt_status.failed(explain_error(probe(MQTT_HOST, MQTT_PORT), MQTT_HOST, MQTT_PORT, manual=MQTT_MANUAL))
    if line:
        _LOGGER.error("MQTT: %s Retrying.", line)


def check_mqtt() -> None:
    """Tell Home Assistant when the broker has been unreachable for a while."""
    message = mqtt_status.notification_due()
    if message and options.get("persistent_notifications"):
        alerts.queue.put(lambda: home_assistant.persistent_notification(NOTIFICATION_TITLE, message, NOTIFICATION_ID))


# Commands from Home Assistant's buttons, run one at a time off the MQTT thread.
commands: queue.Queue = queue.Queue(maxsize=50)
command_seen: dict[tuple[str, str], float] = {}


def queue_command(kind: str, ieee: str) -> None:
    """Accept a reconfigure / battery-replaced command for a known device, at most once a minute each.

    Anything on the broker can publish these topics, so a flood must not start a thread per message.
    """
    with tracker.lock:
        known = IEEE_RE.match(ieee) and ieee in tracker.data["devices"]
    if not known:
        return
    now = time.monotonic()
    if now - command_seen.get((kind, ieee), -COMMAND_COOLDOWN) < COMMAND_COOLDOWN:
        _LOGGER.info("Ignoring repeated %s for %s (less than a minute since the last one)", kind, ieee)
        return
    command_seen[(kind, ieee)] = now
    try:
        commands.put_nowait((kind, ieee))
    except queue.Full:
        _LOGGER.warning("Too many commands from Home Assistant waiting; dropped %s for %s", kind, ieee)


def command_worker() -> None:
    while True:
        kind, ieee = commands.get()
        try:
            if kind == "battery_replaced":
                today = datetime.now(tracker.local_tz).date().isoformat()
                with tracker.lock:
                    record = tracker.data["devices"].get(ieee) or {}
                    replacements = (record.get("battery_info") or {}).get("replacements") or []
                    done = any(r.get("date") == today and r.get("source") == "Home Assistant" for r in replacements)
                if done:
                    _LOGGER.info("Battery replacement for %s is already recorded today", ieee)
                    continue
                _LOGGER.info("Battery replacement recorded from Home Assistant for %s", ieee)
                record_battery_replaced(ieee, today, "Home Assistant", "")
            elif kind == "reconfigure":
                _LOGGER.info("Reconfigure requested from Home Assistant for %s", ieee)
                tracker.reconfigure(ieee)
        except Exception:  # noqa: BLE001 - one failed command must not stop the next
            _LOGGER.exception("%s for %s failed", kind, ieee)


def on_message(client, userdata, message):
    # Never let an exception out: paho would stop its network thread, and with it all tracking.
    try:
        topic = message.topic
        if topic.startswith("$SYS/"):
            broker_stats.handle(topic, message.payload)
            return
        if topic.startswith(f"{DISCOVERY_BASE}/"):
            parts = topic.split("/")
            if len(parts) == 3 and parts[2] in ("battery_replaced", "reconfigure"):
                queue_command(parts[2], parts[1])
            return
        for base_topic in mqtt_topics:
            if topic.startswith(f"{base_topic}/"):
                tracker.handle_message(base_topic, topic, message.payload, bool(message.retain))
                return
    except Exception:  # noqa: BLE001 - malformed input from the broker
        _LOGGER.exception("Handling an MQTT message failed")


def _internal_broker(host: str) -> bool:
    """A broker on Supervisor's internal network (an app such as core-mosquitto), not reachable from outside."""
    return "." not in host or host.endswith(".local.hass.io") or host.startswith("172.30.")


def new_client(client_id: str) -> mqtt.Client:
    """An MQTT client with the broker login and TLS settings, not yet connected."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    # What can wait for a broker that is away: beyond this, publishing fails instead of filling memory.
    client.max_queued_messages_set(2000)
    user = os.environ.get("MQTT_USER")
    if user:
        client.username_pw_set(user, os.environ.get("MQTT_PASS") or None)
    if (os.environ.get("MQTT_SSL") or "").lower() == "true":
        if MQTT_MANUAL:
            # A broker from this app's options: checked against the system CAs unless mqtt_ssl_verify is off.
            verify = (os.environ.get("MQTT_SSL_VERIFY") or "true").lower() != "false"
        else:
            # The broker app on Supervisor's internal network: its certificate is for its public name,
            # not this hostname, so it can't be verified. Any other broker Supervisor offers is checked.
            verify = not _internal_broker(MQTT_HOST)
        if verify:
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        else:
            client.tls_set(cert_reqs=ssl.CERT_NONE)
            client.tls_insecure_set(True)
    client.suppress_exceptions = True
    return client


# The tracker's MQTT client, once started.
mqtt_client: mqtt.Client | None = None


def start_mqtt() -> mqtt.Client:
    global publish_retained, mqtt_client
    client = new_client(f"z2m-tracker-{tracker.boot_id}")
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_connect_fail = on_connect_fail
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.will_set(AVAILABILITY, "offline", qos=1, retain=True)
    discovery.publish = lambda topic, payload, retain: client.publish(topic, payload, qos=1, retain=retain)
    if not mqtt_status.configured:
        _LOGGER.error("MQTT: %s", mqtt_status.failed(""))
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    tracker.publish = lambda topic, payload: client.publish(topic, json.dumps(payload), qos=0)

    def clear_topic(topic: str) -> bool:
        return client.publish(topic, b"", qos=1, retain=True).rc == mqtt.MQTT_ERR_SUCCESS

    publish_retained = clear_topic
    mqtt_client = client
    return client


def remove_mqtt_entities() -> None:
    """ZHA mode: remove, once, what the app left on the broker while it read Zigbee2MQTT.

    The sensors it made through MQTT discovery would otherwise stay in Home Assistant as unavailable
    for good. This is the only time the app talks to the broker with ZHA; afterwards there is
    nothing left to remove and it never connects again. No broker (any more): nothing to do.
    """
    topics = discovery.cleanup_topics()
    if not topics:
        return
    if not mqtt_status.configured:
        _LOGGER.info("Old MQTT sensors can't be removed: Home Assistant offers no MQTT broker")
        return
    client = new_client(f"z2m-tracker-cleanup-{tracker.boot_id}")
    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        client.loop_start()
        sent = [client.publish(topic, b"", qos=1, retain=True) for topic in [*topics, AVAILABILITY]]
        for info in sent:
            info.wait_for_publish(10)
        if not all(info.is_published() for info in sent):
            raise RuntimeError("the broker didn't confirm every message")
        with tracker.lock:
            tracker.data["discovery_topics"] = []
            tracker.dirty = True
        _LOGGER.info("Removed the app's old MQTT sensors from Home Assistant (%d topics)", len(topics))
    except Exception as err:  # noqa: BLE001 - optional clean-up, tried again at the next start
        _LOGGER.warning("Couldn't remove the app's old MQTT sensors (%s); trying again at the next start", err)
    finally:
        client.loop_stop()
        client.disconnect()


# ------------------------------------------------------- retained messages

# First scan after connecting: long enough for every network's device list to arrive.
RETAINED_FIRST_DELAY = 30
# After a rename or removal: a short wait, so a burst of renames costs one scan.
RETAINED_CHANGE_DELAY = 20
RETAINED_RESCAN = 6 * 3600
# A scan ends once no retained message has arrived for this long, or after RETAINED_MAX_WAIT.
RETAINED_SETTLE = 2.0
RETAINED_MAX_WAIT = 30.0
retained_lock = threading.Lock()
retained_next = 0.0
# Publishes an empty retained message (deletes the topic on the broker). Set by start_mqtt.
publish_retained = None


def retained_scan_soon(delay: float = 0) -> None:
    global retained_next
    due = time.time() + delay
    if not retained_next or due < retained_next:
        retained_next = due


def scan_retained() -> bool:
    """Start a scan of the broker's retained messages on its own thread. False if one is running."""
    if not retained_lock.acquire(blocking=False):
        return False
    retained_store.running = True

    def job() -> None:
        try:
            _scan_retained()
        except Exception:  # noqa: BLE001 - scans are best effort
            _LOGGER.exception("Scanning retained messages failed")
            retained_store.failed("The scan failed, see the app log")
        finally:
            retained_store.running = False
            with tracker.lock:
                tracker.revision += 1
            retained_lock.release()

    threading.Thread(target=job, name="retained-scan", daemon=True).start()
    return True


def _scan_retained() -> None:
    """Connect a second, short-lived client and collect what the broker sends as retained.

    The broker replays every retained message on a fresh subscription. Doing it on a separate
    connection leaves the tracker's own subscription alone, so old states aren't read again as
    new messages. Live traffic arrives without the retain flag and is ignored."""
    found: dict[str, dict] = {}
    seen = {"last": 0.0, "subscribed": False, "full": False, "error": None}
    client = new_client(f"z2m-tracker-scan-{tracker.boot_id}")
    filters = [(f"{topic}/#", 0) for topic in mqtt_topics] + [
        (f"{DISCOVERY_PREFIX}/+/+/config", 0),
        (f"{DISCOVERY_PREFIX}/+/+/+/config", 0),
    ]

    def connected(c, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            seen["error"] = explain_refusal(str(reason_code), manual=MQTT_MANUAL)
            return
        c.subscribe(filters)

    def subscribed(c, userdata, mid, reason_codes, properties):
        seen["subscribed"] = True
        seen["last"] = time.monotonic()

    def message(c, userdata, msg):
        if not msg.retain:
            return
        seen["last"] = time.monotonic()
        if not seen["full"] and not collect_retained(found, msg.topic, msg.payload, mqtt_topics, DISCOVERY_PREFIX):
            seen["full"] = True

    client.on_connect = connected
    client.on_subscribe = subscribed
    client.on_message = message
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()
    start = time.monotonic()
    try:
        while time.monotonic() - start < RETAINED_MAX_WAIT and not stop_event.is_set():
            if seen["error"] or (seen["subscribed"] and time.monotonic() - seen["last"] >= RETAINED_SETTLE):
                break
            time.sleep(0.25)
    finally:
        client.disconnect()
        client.loop_stop()
    if not seen["subscribed"]:
        retained_store.failed(seen["error"] or "Couldn't reach the MQTT broker")
        _LOGGER.warning("Retained message scan: %s", seen["error"] or "couldn't reach the broker")
        return
    retained_store.replace(dict(found), seen["full"])
    stale = tracker.stale_retained() or []
    _LOGGER.info("Retained message scan: %d topics, %d stale", len(found), len(stale))
    if options.get("retained_autoclear"):
        old = [e["topic"] for e in stale if e["reason"] == OLD_NAME]
        if old:
            result = clear_retained(old)
            _LOGGER.info("Cleared %d retained topics under old device names", len(result["cleared"]))


def clear_retained(topics: list[str]) -> dict:
    """Delete stale retained topics from the broker by publishing an empty retained message.

    Only topics the last scan lists as stale right now are touched; anything else (a current
    device, a bridge topic, a topic the app never saw) is refused."""
    stale = tracker.stale_retained()
    if stale is None:
        return {"ok": False, "error": "The broker hasn't been scanned yet. Try again in a minute.", "cleared": []}
    allowed = {entry["topic"] for entry in stale}
    cleared, refused = [], []
    for topic in dict.fromkeys(str(t) for t in topics):
        if topic in allowed and publish_retained is not None and publish_retained(topic):
            cleared.append(topic)
        else:
            refused.append(topic)
    retained_store.forget(cleared)
    if cleared:
        _LOGGER.info("Cleared %d stale retained topic(s)", len(cleared))
        tracker.evaluate_health()
    with tracker.lock:
        tracker.revision += 1
    return {"ok": bool(cleared) or not refused, "cleared": cleared, "refused": refused}


def clear_retained_commands(ieee: str) -> dict:
    """Delete a device's retained /set topics from the broker with an empty retained message.
    Only topics the broker replayed as retained commands for this device are touched."""
    topics = tracker.retained_commands_of(ieee)
    if not topics:
        return {"ok": False, "error": "No retained command for this device", "cleared": []}
    cleared = [topic for topic in topics if publish_retained is not None and publish_retained(topic)]
    tracker.forget_retained_commands(cleared)
    if cleared:
        _LOGGER.info("Cleared retained command(s): %s", ", ".join(cleared))
        tracker.evaluate_health()
    ok = len(cleared) == len(topics)
    return {"ok": ok, "topics": topics, "cleared": cleared, "error": None if ok else "MQTT is not connected"}


# ------------------------------------------------------------------ threads


def networkmap_timeout() -> int:
    """Seconds to wait for Zigbee2MQTT's routing scan answer. Large meshes can take over 20 minutes."""
    try:
        minutes = int(options.get("networkmap_timeout_minutes", 60))
    except (TypeError, ValueError):
        minutes = 60
    return min(max(minutes, 5), 240) * 60


def run_networkmap(instance: str | None = None) -> bool:
    if not scan_lock.acquire(blocking=False):
        return False

    def job() -> None:
        try:
            _LOGGER.info("Routing scan started (%s)", instance or "all networks")
            result = tracker.scan_networkmap(instance, timeout=networkmap_timeout())
            _LOGGER.info("Routing scan finished: %s", result)
        except Exception:  # noqa: BLE001 - scans are best effort
            _LOGGER.exception("Routing scan failed")
        finally:
            scan_lock.release()

    threading.Thread(target=job, name="networkmap", daemon=True).start()
    return True


def run_router_pings() -> bool:
    if not ping_lock.acquire(blocking=False):
        return False

    def job() -> None:
        try:
            # Don't overlap a routing scan that's still running: wait as long as the scan may take.
            if scan_lock.acquire(timeout=networkmap_timeout() + 60):
                scan_lock.release()
            _LOGGER.info("Nightly router ping started")
            result = tracker.ping_routers(stop_event.wait)
            metrics.save()
            _LOGGER.info("Nightly router ping finished: %s", result)
        except Exception:  # noqa: BLE001 - pings are best effort
            _LOGGER.exception("Nightly router ping failed")
        finally:
            ping_lock.release()

    threading.Thread(target=job, name="router-ping", daemon=True).start()
    return True


def record_battery_replaced(ieee: str, date: str, source: str, note: str, brand: str | None = None) -> dict | None:
    entry = tracker.add_battery_replacement(ieee, date, source, note, brand)
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


REPLACE_CARRY = ("name", "area", "bindings", "notes", "battery", "remove", "rename_old")


def settled_entity_ids(ieee: str, base_topic: str, before: dict[str, str], timeout: float = 20.0) -> dict[str, str]:
    """The new device's entities once Home Assistant has re-created them after the rename: changed from
    before and the same on two reads in a row. Whatever is there when the time runs out otherwise."""
    deadline = time.monotonic() + timeout
    last = None
    while True:
        current = home_assistant.device_entity_ids(ieee, base_topic)
        if current and current != before and current == last:
            return current
        if time.monotonic() >= deadline:
            return current
        last = current
        time.sleep(2)


def keep_entity_ids(
    old_entities: dict[str, str], old: tuple[str, str], new: tuple[str, str], before: dict[str, str]
) -> dict:
    """Give the new device the old device's entity IDs, so automations and dashboards keep working.

    old and new are (ieee, base topic). Home Assistant makes new IDs when the new device is renamed, and
    can put the area in front ("living_room_living_room_lamp_plug" when the name already starts with
    the area). An ID the old device still holds (a disabled entity, or a kept old device) moves to
    "<id>_old" first.
    """
    if not old_entities:
        return {"ok": True, "kept": 0, "detail": "The old device has no entities in Home Assistant"}
    moves = plan_entity_ids(old_entities, settled_entity_ids(*new, before))
    if not moves:
        return {"ok": True, "kept": 0, "detail": "Already the same"}
    held = {entity_id: key for key, entity_id in home_assistant.device_entity_ids(*old).items()}
    kept, failed = 0, []
    for current, wanted in moves:
        try:
            if wanted in held:
                free_entity_id(wanted)
            home_assistant.set_entity_id(current, wanted)
            kept += 1
        except Exception as err:  # noqa: BLE001 - reported per entity
            failed.append(f"{wanted}: {err}")
    parts = [f"Kept {kept} entity ID{'' if kept == 1 else 's'}"] if kept else []
    return {"ok": not failed, "kept": kept, "detail": ". ".join(parts + failed)}


def free_entity_id(entity_id: str) -> None:
    """Move an entity to "<id>_old" (or "_old_2", ...) so its ID is free."""
    error: Exception | None = None
    for n in range(1, 10):
        try:
            home_assistant.set_entity_id(entity_id, f"{entity_id}_old" if n == 1 else f"{entity_id}_old_{n}")
            return
        except RuntimeError as err:
            if "already registered" not in str(err):
                raise
            error = err
    raise RuntimeError(f"No free ID to move the old entity to: {error}")


def replace_device(old_ieee: str, new_ieee: str, carry: dict) -> dict:
    """Swap a device (dead, or working and swapped on purpose) for a new one: carry over what was chosen, then link both histories.

    The old device either goes (remove) or stays under "<name> (old)" (rename_old), so its name is free for the new one.
    """
    problem = tracker.replace_problem(old_ieee, new_ieee)
    if problem:
        return {"ok": False, "error": problem, "steps": []}
    want = {key: bool(carry.get(key)) for key in REPLACE_CARRY}
    with tracker.lock:
        old = dict(tracker.data["devices"][old_ieee])
        new_instance = tracker.data["devices"][new_ieee]["instance"]
    old_name = tracker_display_name(old)
    steps: list[dict] = []
    carried: list[str] = []

    def step(key: str, label: str, ok: bool, detail: str = "") -> None:
        steps.append({"key": key, "label": label, "ok": ok, "detail": detail})

    # Read before the old device is removed or renamed: both change or drop its entity IDs.
    old_entities: dict[str, str] | None = None
    new_before: dict[str, str] = {}
    if want["name"] and old_name:
        try:
            old_entities = home_assistant.device_entity_ids(old_ieee, old["instance"])
            new_before = home_assistant.device_entity_ids(new_ieee, new_instance)
        except Exception as err:  # noqa: BLE001 - reported as its own step
            _LOGGER.warning("Reading the entity IDs to keep failed: %s", err)
            step("entity_ids", "Entity IDs", False, f"Couldn't read them from Home Assistant: {err}")

    if want["remove"]:
        if old["status"] != "active":
            step("remove", "Remove the old device", True, "Already gone from Zigbee2MQTT")
        else:
            # A working device is asked to leave the network; a dead one can't answer, so it's force-removed.
            result = tracker.remove_from_z2m(old_ieee, leave=old.get("availability") != "offline")
            if not result["ok"]:
                detail = result["error"]
            elif result.get("forced") and old.get("availability") != "offline":
                detail = (
                    "It didn't answer, so it was force-removed. Factory-reset it, or it stays paired to the network"
                )
            else:
                detail = "Removed from Zigbee2MQTT"
            step("remove", "Remove the old device", result["ok"], detail)
            if result["ok"]:
                # Give Home Assistant a moment to drop the old entities so their IDs are free for the new device.
                time.sleep(3)
    elif want["rename_old"] and old["status"] == "active" and old_name:
        old_new_name = tracker.old_name_for(old_ieee)
        result = (
            tracker.rename(old_ieee, old_new_name)
            if old_new_name
            else {"ok": False, "error": "No free name for the old device"}
        )
        step(
            "rename_old",
            "Rename the old device",
            result["ok"],
            f"Renamed to {old_new_name}" if result["ok"] else result["error"],
        )
        if result["ok"]:
            # Give Home Assistant a moment to rename the old entities so their IDs are free for the new device.
            time.sleep(3)
    new_name = None
    if want["name"]:
        if not old_name:
            step("name", "Name", False, "The old device never had a name")
        else:
            result = tracker.rename(new_ieee, old_name)
            if result["ok"]:
                new_name = old_name
                carried.append("name")
            step("name", "Name", result["ok"], f"Renamed to {old_name}" if result["ok"] else result["error"])
    if want["area"]:
        area_name = old.get("area")
        if not area_name:
            step("area", "Area", False, "The old device had no area")
        else:
            try:
                if new_ieee not in tracker.ha_device_ids:
                    refresh_home_assistant()
                device_id = tracker.ha_device_ids.get(new_ieee)
                area_id = next((a["id"] for a in home_assistant.list_areas() if a["name"] == area_name), None)
                if not device_id:
                    step("area", "Area", False, "The new device isn't in Home Assistant yet. Set the area later.")
                elif not area_id:
                    step("area", "Area", False, f"Home Assistant has no area called {area_name} any more")
                else:
                    home_assistant.set_device_area(device_id, area_id)
                    with tracker.lock:
                        record = tracker.data["devices"].get(new_ieee)
                        if record is not None:
                            record["area"] = area_name
                            tracker._changed(structural=True)
                    carried.append("area")
                    step("area", "Area", True, f"Set to {area_name}")
            except Exception as err:  # noqa: BLE001 - HA errors are reported per step
                step("area", "Area", False, str(err))
    if new_name and old_entities is not None:
        # After the area: Home Assistant may re-create the entities once more when it changes.
        try:
            kept = keep_entity_ids(old_entities, (old_ieee, old["instance"]), (new_ieee, new_instance), new_before)
            if kept["kept"]:
                carried.append("entity IDs")
            step("entity_ids", "Entity IDs", kept["ok"], kept["detail"])
        except Exception as err:  # noqa: BLE001 - HA errors are reported per step
            step("entity_ids", "Entity IDs", False, str(err))
    if want["bindings"]:
        if old.get("instance") != new_instance:
            step("bindings", "Bindings", False, "The new device is on a different network")
        else:
            result = tracker.rebind(old_ieee, new_ieee)
            parts = []
            if result.get("done"):
                parts.append(f"{result['done']} re-created")
            for failure in result.get("failed") or []:
                parts.append(f"{'; '.join(failure['labels'])}: {failure['error']}")
            if result.get("skipped"):
                parts.append(f"{result['skipped']} skipped because the target is no longer in Zigbee2MQTT")
            if result.get("unfit"):
                parts.append(f"{result['unfit']} don't fit the new model's endpoints. Set them up in Zigbee2MQTT")
            if result.get("error"):
                parts.append(result["error"])
            if result.get("done"):
                carried.append("bindings")
            step(
                "bindings", "Bindings", result["ok"], ". ".join(p.rstrip(".") for p in parts) or "Nothing to re-create"
            )
    keep_old = old["status"] == "active" and not want["remove"]
    copied = tracker.carry_over(old_ieee, new_ieee, want["notes"], want["battery"], keep_old=keep_old)
    if want["notes"]:
        got = [x for x in copied if x in ("note", "asset")]
        carried += ["notes"] if got else []
        step("notes", "Notes and asset details", True, "Copied" if got else "Nothing to copy")
    if want["battery"]:
        carried += ["battery record"] if "battery" in copied else []
        step("battery", "Battery record", True, "Copied" if "battery" in copied else "Nothing to copy")
    linked = tracker.link_replacement(old_ieee, new_ieee, carried, new_name)
    step("link", "Link both histories", linked, "Each logbook points to the other" if linked else "Couldn't link")
    if want["battery"]:
        threading.Thread(target=discovery.sync, daemon=True).start()
    _LOGGER.info("Replaced %s with %s: %s", old_ieee, new_ieee, ", ".join(carried) or "history only")
    return {"ok": all(s["ok"] for s in steps), "steps": steps}


def i18n_file(name: str) -> bytes | None:
    """Sidebar translation file www/i18n/<name>.json; "index" lists the available languages."""
    if name == "index":
        languages = []
        for path in sorted(I18N_DIR.glob("*.json")):
            if not LANG_RE.match(path.stem):
                continue
            try:
                label = json.loads(path.read_text(encoding="utf-8")).get("language.name") or path.stem
            except (OSError, ValueError, AttributeError):
                continue
            languages.append({"code": path.stem, "name": label})
        return json.dumps({"languages": languages}).encode("utf-8")
    # Looked up among the files that exist, so the requested name never becomes a file path.
    files = {path.stem: path for path in I18N_DIR.glob("*.json") if LANG_RE.match(path.stem)}
    path = files.get(name)
    return path.read_bytes() if path else None


_integration_seen = [0.0]
_feedback_env: dict = {"at": 0.0, "env": {}}
SOURCE_LABELS = {"z2m": "Zigbee2MQTT", ZHA_INSTANCE: "ZHA", MATTER_INSTANCE: "Matter"}


def _supervisor_get(path: str) -> dict:
    """GET a Supervisor API path and return its `data`, or {} without Supervisor."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if STANDALONE or not token:
        return {}
    request = urllib.request.Request(f"http://supervisor{path}", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return (json.loads(response.read() or b"{}") or {}).get("data") or {}
    except (urllib.error.URLError, TimeoutError, ValueError) as err:
        _LOGGER.debug("Supervisor %s failed: %s", path, err)
        return {}


def feedback_environment() -> dict:
    """What a bug report says about this install. Asked of Supervisor/HA at most every 10 minutes."""
    if time.time() - _feedback_env["at"] < 600 and _feedback_env["env"]:
        env = dict(_feedback_env["env"])
    else:
        info = _supervisor_get("/info")
        ha_version = str(info.get("homeassistant") or "")
        if not ha_version and home_assistant.available:
            ha_version = str((home_assistant._request("GET", "/config") or {}).get("version") or "")
        if STANDALONE:
            install = "Docker (standalone app)"
        elif info.get("hassos"):
            install = f"Home Assistant OS {info['hassos']}"
        elif info:
            install = "Home Assistant Supervised"
        else:
            install = ""
        env = {"ha_version": ha_version, "install": install, "arch": str(info.get("arch") or "")}
        _feedback_env.update(at=time.time(), env=dict(env))
    env["app_version"] = VERSION
    env["mode"] = "Docker" if STANDALONE else "Home Assistant app"
    with tracker.lock:
        records = list(tracker.data["devices"].values())
        bridges = dict(tracker.data.get("bridges") or {})
    networks = []
    z2m_count = len(mqtt_topics)
    for topic in base_topics:
        source = topic if topic in (ZHA_INSTANCE, MATTER_INSTANCE) else "z2m"
        info = (bridges.get(topic) or {}).get("info") or {}
        label = SOURCE_LABELS[source] + (
            f" #{mqtt_topics.index(topic) + 1}" if source == "z2m" and z2m_count > 1 else ""
        )
        networks.append(
            {
                "source": source,
                "label": label,
                "version": str(info.get("version") or "") if source == "z2m" else "",
                "coordinator": str(info.get("coordinator_type") or "") if source == "z2m" else "",
                "devices": sum(1 for r in records if r.get("instance") == topic and r.get("status") != "removed"),
            }
        )
    env["networks"] = networks
    if not mqtt_topics:
        env["broker"] = ""
    elif MQTT_MANUAL or STANDALONE:
        env["broker"] = "External broker (set in the app options)"
    else:
        env["broker"] = "Mosquitto broker app"
    env["integration"] = time.time() - _integration_seen[0] < 3600
    return env


def feedback_names() -> list[str]:
    """Every device name, so a report can leave them out."""
    with tracker.lock:
        return [str(r.get("friendly_name") or "") for r in tracker.data["devices"].values() if r.get("friendly_name")]


def feedback_report(body: dict) -> dict:
    env = feedback_environment()
    env["language"] = str(body.get("language") or "")[:20]
    env["browser"] = str(body.get("browser") or "")[:60]
    return feedback.build(
        str(body.get("kind") or "bug"),
        body.get("form") if isinstance(body.get("form"), dict) else {},
        env,
        LOG_BUFFER.snapshot(),
        feedback_names(),
        include_setup=body.get("setup") is not False,
        include_logs=body.get("logs") is not False,
    )


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
        # With ZHA, the checks that need Zigbee2MQTT are left out: they can never fire.
        "checks": [
            {"key": k, "label": label, "description": d, "event": e}
            for k, label, d, e in (zha_checks(CHECKS) if stack == "zha" else CHECKS)
        ],
        "networks": [{"topic": t, "label": t, "panel": options["_panels"].get(t)} for t in base_topics],
        "muted": sorted(muted, key=lambda m: m["name"].lower()),
        "entities": len(discovery._configs),
        "networkmap": networkmap,
        "version": VERSION,
        "home_assistant": home_assistant.available,
        "stack": stack,
        "sources": sorted(SOURCES),
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
    low = clean.get("impossible_temp_min", options.get("impossible_temp_min"))
    high = clean.get("impossible_temp_max", options.get("impossible_temp_max"))
    if {"impossible_temp_min", "impossible_temp_max"} & set(clean) and not low < high:
        key = "impossible_temp_min" if "impossible_temp_min" in clean else "impossible_temp_max"
        errors[key] = "the lowest temperature must be below the highest"
    if errors:
        return {"ok": False, "errors": errors}
    options.update(clean)
    options["alert_on_join"] = "joined" in (options.get("notify_checks") or [])
    if "ha_entities" in clean:
        discovery.enabled = clean["ha_entities"]
    if "broker_stats" in clean and mqtt_client is not None and mqtt_client.is_connected():
        if clean["broker_stats"]:
            mqtt_client.subscribe(SYS_FILTER, 0)
            broker_stats.subscribed()
        else:
            mqtt_client.unsubscribe(SYS_FILTER)
            broker_stats.reset()
    if {
        "disabled_checks",
        "offline_alert_minutes",
        "critical_offline_minutes",
        "frozen_alert_hours",
        "battery_alert_percent",
        "chatty_per_hour",
        "naming_pattern",
        "impossible_temp_min",
        "impossible_temp_max",
        "humidity_stuck_hours",
    } & set(clean):
        tracker.evaluate_health()
    if clean.get("community_ratings_fetch"):
        community.fetched_at = 0.0
    if {"alerts_enabled", "notify_checks", "disabled_checks"} & set(clean):
        alerts.rebaseline()
    threading.Thread(target=discovery.sync, daemon=True).start()
    with tracker.lock:
        tracker._changed(structural=True)
    persisted = save_options()
    _LOGGER.info("Settings changed: %s (saved to app configuration: %s)", ", ".join(clean), persisted)
    return {"ok": True, "persisted": persisted, **settings_payload()}


def refresh_home_assistant() -> None:
    try:
        home_assistant.refresh()
        with tracker.lock:
            tracker.ha_device_ids = dict(home_assistant.device_ids)
            tracker.ha_battery = dict(home_assistant.battery)
            for ieee, found in home_assistant.entities.items():
                tracker.ha_entities.setdefault(ieee, {}).update(found)
            for ieee, record in tracker.data["devices"].items():
                if ieee in home_assistant.device_ids:
                    record["area"] = home_assistant.areas.get(ieee)
                    record["ha_name"] = home_assistant.names.get(ieee)
            tracker._changed(structural=True)
        _LOGGER.info(
            "Linked %d HA devices, %d with Battery Notes", len(home_assistant.device_ids), len(home_assistant.battery)
        )
    except Exception:  # noqa: BLE001 - HA lookups are optional
        _LOGGER.exception("Reading Home Assistant registries failed")


def refresh_leftovers(usage: bool = False) -> list[dict] | None:
    """Re-read Zigbee2MQTT devices left behind in HA. None until it can be answered."""
    ready, known = tracker.leftover_context()
    if not ready or not home_assistant.available:
        return None
    found = home_assistant.leftovers(known, usage=usage)
    tracker.set_ha_leftovers(found)
    return found


def last_name_change() -> str | None:
    """Time of the latest rename or removal, which can leave retained topics behind."""
    with tracker.lock:
        events = tracker.data["events"][-200:]
        return next((e.get("time") for e in reversed(events) if e.get("kind") in ("renamed", "removed")), None)


entity_usage_lock = threading.Lock()


def refresh_entity_usage(entity_ids: list[str]) -> None:
    """Look up what uses the entities of keys a device stopped sending. One lookup at a time."""
    if not entity_usage_lock.acquire(blocking=False):
        return
    try:
        found = home_assistant.entity_usage(entity_ids)
    except Exception as err:  # noqa: BLE001 - tried again after USAGE_MAX_AGE
        _LOGGER.warning("Looking up what uses %d entities in Home Assistant failed: %s", len(entity_ids), err)
        found = {}
    finally:
        entity_usage_lock.release()
    tracker.set_entity_usage(found, entity_ids)


community_lock = threading.Lock()


def fetch_community() -> None:
    """Daily refresh of the community ratings from GitHub; a new file re-runs the health checks."""
    if not community_lock.acquire(blocking=False):
        return
    try:
        if community.fetch(time.time()):
            _LOGGER.info("Community ratings updated (%s, %d models)", community.updated, len(community.data["models"]))
            tracker.evaluate_health()
            with tracker.lock:
                tracker._changed(structural=True)
        elif community.fetch_error:
            _LOGGER.info("Couldn't fetch the community ratings (%s); trying again in an hour", community.fetch_error)
    finally:
        community_lock.release()


def scheduler() -> None:
    global ha_changed_at, retained_next
    last_save = last_metrics = last_ha = last_health = 0.0
    leftovers_due = False
    name_change = last_name_change()
    last_scan_day = last_ping_day = None
    while not stop_event.wait(5):
        now = time.time()
        try:
            if stack == "z2m":
                check_mqtt()
            # A save that fails (full disk) must not skip the checks and alerts below.
            if now - last_save >= SAVE_INTERVAL:
                last_save = now
                try:
                    tracker.save()
                except OSError as err:
                    _LOGGER.error("Saving the history failed: %s", err)
            if now - last_metrics >= METRICS_SAVE_INTERVAL:
                last_metrics = now
                try:
                    metrics.save()
                except OSError as err:
                    _LOGGER.error("Saving the metrics failed: %s", err)
            changed = ha_changed_at and now - ha_changed_at >= HA_CHANGE_DELAY
            if changed or now - last_ha >= HA_REFRESH_INTERVAL:
                ha_changed_at = 0.0
                refresh_home_assistant()
                leftovers_due = True
                usage_refresher.request_full()
                last_ha = now
            # Waits for every network's device list; until then all devices look unknown.
            if leftovers_due and tracker.lists_ready.is_set():
                leftovers_due = False
                try:
                    refresh_leftovers()
                except Exception as err:  # noqa: BLE001 - optional, try again next refresh
                    _LOGGER.warning("Checking for leftover devices in Home Assistant failed: %s", err)
            if now - last_health >= HEALTH_INTERVAL or tracker.late_due(now):
                alerts.check()
                if stack == "z2m":
                    discovery.sync()
                last_health = now
            # Retained messages: after connecting, shortly after a rename or removal, and every 6 hours.
            changed_names = last_name_change()
            if changed_names != name_change:
                name_change = changed_names
                retained_scan_soon(RETAINED_CHANGE_DELAY)
            if (
                stack == "z2m"
                and retained_next
                and now >= retained_next
                and mqtt_status.connected
                and tracker.lists_ready.is_set()
            ):
                if scan_retained():
                    retained_next = now + RETAINED_RESCAN
            if options.get("community_ratings_fetch", True) and community.fetch_due(now):
                threading.Thread(target=fetch_community, name="community", daemon=True).start()
            # What uses devices with a problem: after a registry refresh, for new issues,
            # and retries after a failure. Kept for the next round while a lookup runs.
            usage_refresher.start_due()
            if home_assistant.available and not entity_usage_lock.locked():
                wanted = tracker.entity_usage_wanted()
                if wanted:
                    threading.Thread(
                        target=refresh_entity_usage, args=(wanted,), name="ha-entity-usage", daemon=True
                    ).start()
            tracker.metrics.decay_rhythm()
            hour = int(options.get("networkmap_hour", -1))
            local = datetime.now(tracker.local_tz)
            if stack == "z2m" and hour >= 0 and local.hour == hour and last_scan_day != local.date():
                last_scan_day = local.date()
                run_networkmap()
            # Nightly router pings run the hour after the routing scan (04:00 when scans are off).
            ping_hour = (hour if hour >= 0 else 3) + 1
            if stack == "z2m" and local.hour == ping_hour % 24 and last_ping_day != local.date():
                last_ping_day = local.date()
                run_router_pings()
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
            daily = daily_summary.due(
                options, local, daily_summary.clean_state(tracker.data.get("daily_summary"))["sent"]
            )
            if daily:
                send_daily_summary(local.date().isoformat(), quiet=daily == "skip")
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            _LOGGER.exception("Scheduler step failed")


def weekly_summary_text() -> tuple[str, str]:
    now = time.time()
    week_ago = datetime.fromtimestamp(now - 7 * 86400, tz=timezone.utc).isoformat(timespec="seconds")
    with tracker.lock:
        keys = {i["key"] for i in tracker.issues}
        issues = [i for i in tracker.issues if i["severity"] == "warning" and not nested(i, keys)]
        events = [e for e in tracker.data["events"] if (e.get("time") or "") >= week_ago]
        active = sum(
            1
            for r in tracker.data["devices"].values()
            if r["status"] == "active" and r.get("type") != "Coordinator" and not tracker.is_frozen(r)
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
        lines.append(shopping_text(shopping, 60))
    if joined:
        lines.append("New this week: " + ", ".join(joined[:6]) + ".")
    if removed:
        lines.append("Removed this week: " + ", ".join(removed[:6]) + ".")
    with tracker.lock:
        for bt, bridge in (tracker.data.get("bridges") or {}).items():
            if bt not in base_topics:
                continue  # a network the app no longer listens to
            outages = [o for o in bridge.get("outages", []) if (o.get("start") or "") >= week_ago]
            if outages:
                net = "Main" if bt == "zigbee2mqtt" else bt.replace("zigbee2mqtt_", "").replace("_", " ").capitalize()
                if bt in (ZHA_INSTANCE, MATTER_INSTANCE):
                    net = "ZHA" if bt == ZHA_INSTANCE else "Matter"
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


def send_daily_summary(day: str | None = None, quiet: bool = False) -> dict:
    """Send today's daily summary. With `day` (the scheduler), remember what it listed so tomorrow's
    can say what's new and what cleared; `quiet` only remembers (the weekly summary goes out instead).
    Without `day` (Send one now) nothing is remembered. Returns {"title", "message"}; both empty when
    there was nothing to say."""
    with tracker.lock:
        now_open = daily_summary.snapshot(tracker.issues)
        before = daily_summary.clean_state(tracker.data.get("daily_summary"))["open"]
        if day:
            tracker.data["daily_summary"] = {"sent": day, "open": now_open}
            tracker.dirty = True
    if quiet:
        return {"title": "", "message": ""}
    shopping = tracker.battery_shopping(60)
    built = daily_summary.build(now_open, before, shopping_text(shopping, 60) if shopping else "")
    if not built:
        _LOGGER.info("Daily summary: nothing to say")
        return {"title": "", "message": ""}
    title, message = built
    _LOGGER.info("Daily summary: %s", title)

    def job() -> None:
        if options.get("fire_events", True):
            home_assistant.fire_event("z2m_tracker_summary", {"title": title, "message": message, "daily": True})
        if options.get("alerts_enabled", True):
            if options.get("persistent_notifications"):
                home_assistant.persistent_notification(title, message, "z2m_tracker_daily")
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


class _ScriptCollector(HTMLParser):
    """Collects the text of each inline <script>, exactly as the browser hashes it."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[str] = []
        self._current: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "script" and not dict(attrs).get("src"):
            self._current = []

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._current is not None:
            self.scripts.append("".join(self._current))
            self._current = None


def _inline_scripts(html: str) -> list[str]:
    parser = _ScriptCollector()
    parser.feed(html)
    parser.close()
    return parser.scripts


def _frame_ancestors() -> str:
    """Who may show the page in a frame: Home Assistant's own origin. In plain Docker that is HA_URL,
    so the UI can sit in a Webpage dashboard; under Supervisor the ingress frame is this origin."""
    if STANDALONE and HA_URL:
        parts = urlsplit(HA_URL)
        return f"'self' {parts.scheme}://{parts.netloc}"
    return "'self'"


def _page_csp() -> str:
    """Content-Security-Policy for the sidebar: only its own inline script (by hash) and this origin.

    The page runs on Home Assistant's origin inside the ingress frame, so a script injected through
    device data could reach the HA login. The policy stops any script but the page's own from running.
    """
    hashes = [
        "'sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode() + "'"
        for body in _inline_scripts((WWW / "index.html").read_text(encoding="utf-8"))
    ]
    scripts = " ".join(hashes) or "'none'"
    return "; ".join(
        (
            "default-src 'self'",
            f"script-src {scripts}",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self'",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'none'",
            f"frame-ancestors {_frame_ancestors()}",
        )
    )


PAGE_CSP = _page_csp()

# Standalone mode: the web UI's own sign-in, since no Supervisor checks the HA login first.
UI_PASSWORD = os.environ.get("UI_PASSWORD") or ""
sessions = standalone.Sessions()
login_throttle = standalone.LoginThrottle()
# The sign-in page has no script at all; its form may only post back to this app.
LOGIN_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
    f"frame-ancestors {_frame_ancestors()}; base-uri 'none'"
)
# The page a browser lands on: the UI itself, or the sign-in page.
UI_PAGES = ("/", "/index.html")
GZIP_HEADERS = {"Content-Encoding": "gzip", "Vary": "Accept-Encoding"}
_page: list = [None, b"", b""]


def page_bytes() -> tuple[bytes, bytes]:
    """index.html plain and gzipped, read again only when the file changed."""
    path = WWW / "index.html"
    stamp = path.stat().st_mtime_ns
    if _page[0] != stamp:
        raw = path.read_bytes()
        _page[:] = [stamp, raw, gzip.compress(raw, compresslevel=6)]
    return _page[1], _page[2]


class MetricsHandler(BaseHTTPRequestHandler):
    """GET /metrics in Prometheus text format, for a scraper that sends the metrics_token."""

    server_version = f"Z2MTracker/{VERSION}"
    timeout = 30

    def log_message(self, format, *args):  # noqa: A002 - signature defined by base class
        return

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        token = str(options.get("metrics_token") or "")
        auth = self.headers.get("Authorization") or ""
        if not (auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip().encode(), token.encode())):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer realm="z2m_tracker"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.split("?", 1)[0] != "/metrics":
            self.send_error(404)
            return
        body = prometheus.render(prometheus.collect(tracker.prometheus_snapshot(), VERSION)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def start_metrics_server() -> ThreadingHTTPServer | None:
    """Open the Prometheus port when metrics_token is set and long enough; otherwise stay closed."""
    token = str(options.get("metrics_token") or "")
    if not token:
        return None
    if len(token) < METRICS_TOKEN_MIN:
        _LOGGER.warning("Prometheus metrics stay off: metrics_token must be at least %d characters", METRICS_TOKEN_MIN)
        return None
    try:
        server = ThreadingHTTPServer(("0.0.0.0", METRICS_PORT), MetricsHandler)
    except OSError as err:
        _LOGGER.warning("Couldn't open the Prometheus metrics port %d: %s", METRICS_PORT, err)
        return None
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="metrics-http", daemon=True).start()
    _LOGGER.info("Prometheus metrics on port %d at /metrics", METRICS_PORT)
    return server


class Handler(BaseHTTPRequestHandler):
    server_version = f"Z2MTracker/{VERSION}"
    # A client that connects and then sends nothing (or half a request) must not hold a thread forever.
    timeout = 60

    def log_message(self, format, *args):  # noqa: A002 - signature defined by base class
        return

    def _allowed(self) -> bool:
        client = self.client_address[0]
        # In plain Docker nothing has checked the caller: neither address proves anything there.
        if not STANDALONE and (client == INGRESS_PROXY or client in LOOPBACK):
            return True
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip().encode(), API_TOKEN.encode()):
            # Only the integration holds the API token: a bug report says whether it is connected.
            _integration_seen[0] = time.time()
            return True
        if STANDALONE and sessions.valid(standalone.cookie_value(self.headers.get("Cookie"))):
            return True
        if STANDALONE and self.command == "GET" and self.path.split("?", 1)[0] in UI_PAGES:
            self._send(303, b"", "text/plain", {"Location": "login"})
            return False
        self.send_error(403)
        return False

    def _login_page(self, status: int = 200, error: str = "") -> None:
        if not UI_PASSWORD:
            message = "Set the UI_PASSWORD environment variable (8 characters or more) and restart the container."
        elif len(UI_PASSWORD) < standalone.MIN_UI_CHARS:
            message = f"UI_PASSWORD is too short: use {standalone.MIN_UI_CHARS} characters or more, then restart."
        else:
            message = ""
        body = (WWW / "login.html").read_text(encoding="utf-8")
        note = html.escape(error or message)
        body = body.replace("{{note}}", f'<p class="note" role="alert">{note}</p>' if note else "")
        body = body.replace("{{disabled}}", " disabled" if message else "")
        self._send(status, body.encode("utf-8"), "text/html; charset=utf-8", {"Content-Security-Policy": LOGIN_CSP})

    def _login(self) -> None:
        """POST /login from the sign-in form: a session cookie for the right password."""
        client = self.client_address[0]
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if site in ("cross-site", "same-site"):
            self.send_error(403)
            return
        if login_throttle.blocked(client):
            self._login_page(429, "Too many wrong passwords. Wait 15 minutes, then try again.")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length).decode("utf-8", "replace") if 0 < length <= 4096 else ""
        given = (parse_qs(raw).get("password") or [""])[0]
        if len(UI_PASSWORD) < standalone.MIN_UI_CHARS or not standalone.password_ok(given, UI_PASSWORD):
            login_throttle.failed(client)
            _LOGGER.warning("Wrong web UI password from %s", client)
            self._login_page(401, "Wrong password.")
            return
        login_throttle.succeeded(client)
        secure = (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"
        cookie = standalone.session_cookie(sessions.create(), secure)
        self._send(303, b"", "text/plain", {"Location": "./", "Set-Cookie": cookie})

    def _cross_site(self) -> bool:
        """True for a POST a browser sent from another page, or one that isn't JSON.

        The ingress cookie is SameSite=Strict, but a page on another port of the HA host counts as the
        same site. Requiring application/json forces the browser to ask first (CORS), which HA refuses
        for ingress; Sec-Fetch-Site rejects what the browser itself marks as coming from elsewhere.
        """
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if content_type != "application/json" or site in ("cross-site", "same-site"):
            self._json(403, {"ok": False, "error": "Requests must come from the Zigbee Tracker page"})
            return True
        return False

    def _gzip_ok(self) -> bool:
        accepted = (self.headers.get("Accept-Encoding") or "").lower()
        return "gzip" in (part.split(";")[0].strip() for part in accepted.split(","))

    def _send(
        self, status: int, body: bytes, content_type: str = "application/json", extra: dict | None = None
    ) -> None:
        if self.command == "POST":
            # The page asks for its data again right after a request: it must see what that changed.
            tracker.drop_snapshot()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"))

    def _body(self, limit: int = 100_000) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > limit:
            return {}
        try:
            parsed = json.loads(self.rfile.read(length))
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if STANDALONE and self.path.split("?", 1)[0] == "/login":
            self._login_page()
            return
        if not self._allowed():
            return
        path = self.path.split("?", 1)[0]
        if STANDALONE and path.startswith("/config/") and HA_URL:
            # "Used by" links point at HA's own editor pages, which live on Home Assistant, not here.
            self._send(302, b"", "text/plain", {"Location": HA_URL + path})
        elif path in UI_PAGES:
            raw, packed = page_bytes()
            headers = {"Content-Security-Policy": PAGE_CSP, "Vary": "Accept-Encoding"}
            if self._gzip_ok():
                self._send(200, packed, "text/html; charset=utf-8", {**headers, "Content-Encoding": "gzip"})
            else:
                self._send(200, raw, "text/html; charset=utf-8", headers)
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
        elif path.startswith("/i18n/") and path.endswith(".json"):
            body = i18n_file(path[len("/i18n/") : -len(".json")])
            if body is None:
                self.send_error(404)
            else:
                self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/devices":
            if self._gzip_ok():
                self._send(200, tracker.snapshot_json(gzipped=True), extra=GZIP_HEADERS)
            else:
                self._send(200, tracker.snapshot_json(), extra={"Vary": "Accept-Encoding"})
        elif path == "/api/settings":
            self._json(200, settings_payload())
        elif path == "/api/community":
            self._json(200, community.all_models())
        elif path == "/api/version":
            self._json(200, {"version": tracker.version, "app": VERSION})
        elif path == "/api/ota":
            self._json(200, ota_manager.snapshot())
        elif path == "/api/scans":
            query = dict(
                part.split("=", 1)
                for part in (self.path.split("?", 1)[1] if "?" in self.path else "").split("&")
                if "=" in part
            )
            # HA ingress re-encodes a space as "+"; a base topic can't hold a real "+" (an MQTT wildcard).
            net = unquote_plus(query.get("net", ""))
            if net not in base_topics:
                self._json(400, {"error": "Unknown network"})
            else:
                self._json(200, {"scans": tracker.scan_history(net)})
        elif path == "/api/whatif":
            query = dict(
                part.split("=", 1)
                for part in (self.path.split("?", 1)[1] if "?" in self.path else "").split("&")
                if "=" in part
            )
            net = unquote_plus(query.get("net", ""))
            ieee = unquote(query.get("ieee", "")).lower()
            if net not in base_topics:
                self._json(400, {"error": "Unknown network"})
            elif not IEEE_RE.match(ieee):
                self._json(400, {"error": "Not a device address"})
            else:
                result = tracker.router_what_if(net, ieee)
                if result is None:
                    self._json(404, {"error": "Not a router in the last routing scan"})
                else:
                    self._json(200, result)
        elif path == "/api/parents":
            query = dict(
                part.split("=", 1)
                for part in (self.path.split("?", 1)[1] if "?" in self.path else "").split("&")
                if "=" in part
            )
            result = tracker.parent_history(unquote(query.get("ieee", "")))
            if result is None:
                self._json(400, {"error": "Unknown device"})
            else:
                self._json(200, result)
        elif path == "/api/messages":
            query = dict(
                part.split("=", 1)
                for part in (self.path.split("?", 1)[1] if "?" in self.path else "").split("&")
                if "=" in part
            )
            ieee = unquote(query.get("ieee", "")).lower()
            messages = tracker.recent_messages(ieee) if IEEE_RE.match(ieee) else None
            if messages is None:
                self._json(404, {"error": "Unknown device"})
            else:
                self._json(200, {"ieee": ieee, "messages": messages, "now": time.time()})
        elif path == "/api/metrics":
            query = dict(
                part.split("=", 1)
                for part in (self.path.split("?", 1)[1] if "?" in self.path else "").split("&")
                if "=" in part
            )
            ieee = unquote(query.get("ieee", ""))
            result = metrics.history(ieee, query.get("range", "")) if ieee else None
            if result is None:
                self._json(400, {"error": "Unknown device or range"})
            else:
                self._json(200, result)
        elif path == "/api/backup":
            stamp = datetime.now().strftime("%Y-%m-%d")
            self._send(
                200,
                tracker.backup(),
                "application/json",
                {"Content-Disposition": f'attachment; filename="zigbee-tracker-backup-{stamp}.json"'},
            )
        elif path == "/api/coordinator_backup":
            query = dict(
                part.split("=", 1)
                for part in (self.path.split("?", 1)[1] if "?" in self.path else "").split("&")
                if "=" in part
            )
            # HA ingress re-encodes a space as "+"; a base topic can't hold a real "+" (an MQTT wildcard).
            net, copy_id = unquote_plus(query.get("net", "")), unquote(query.get("id", ""))
            found = tracker.backup_history.path(net, copy_id) if net in base_topics else None
            if not found:
                self._json(404, {"error": "No such backup copy"})
            else:
                with open(found, "rb") as handle:
                    body = handle.read()
                name = re.sub(r"[^A-Za-z0-9_-]", "_", net)
                self._send(
                    200,
                    body,
                    "application/json",
                    {"Content-Disposition": f'attachment; filename="coordinator_backup-{name}-{copy_id}.json"'},
                )
        elif path == "/api/areas":
            try:
                self._json(200, {"areas": home_assistant.list_areas(), "floors": home_assistant.list_floors()})
            except Exception as err:  # noqa: BLE001 - report HA errors to the caller
                self._json(502, {"error": str(err)})
        elif path == "/api/ha/leftovers":
            try:
                found = refresh_leftovers(usage=True)
            except Exception as err:  # noqa: BLE001 - report HA errors to the caller
                self._json(502, {"error": str(err)})
            else:
                self._json(200, {"ready": found is not None, "leftovers": found or []})
        elif path == "/api/retained":
            self._json(200, tracker.retained_payload())
        elif path == "/api/broker":
            self._json(200, broker_stats.snapshot(bool(options.get("broker_stats", True))))
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
        if STANDALONE and self.path.split("?", 1)[0] == "/login":
            self._login()
            return
        if not self._allowed() or self._cross_site():
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
        # Actions that go through Zigbee2MQTT: not without it, nor for a device on the ZHA network.
        if path in ZHA_BLOCKED_PATHS and (
            stack == "zha" or tracker.instance_of(ieee) in (ZHA_INSTANCE, MATTER_INSTANCE)
        ):
            self._json(409, {"ok": False, "error": "Only for Zigbee2MQTT devices"})
            return
        try:
            if path == "/api/feedback":
                self._json(200, feedback_report(body))
            elif path == "/api/community/report":
                choice = body.get("choice") if isinstance(body.get("choice"), dict) else {}
                result = tracker.community_report(ieee, choice)
                if result is None:
                    self._json(404, {"error": "Unknown device or no model"})
                else:
                    self._json(200, result)
            elif path == "/api/note":
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
            elif path == "/api/report_every":
                # One device ({ieee, seconds}) or many ({items: [{ieee, seconds}, ...]}).
                items = body.get("items") if "items" in body else [{"ieee": ieee, "seconds": body.get("seconds")}]
                if not isinstance(items, list):
                    self._json(400, {"ok": False, "error": "items must be a list"})
                    return
                result = tracker.set_report_every_many(items)
                if result["errors"]:
                    result["error"] = result["errors"][0]["error"]
                self._json(200 if result["applied"] or not result["errors"] else 400, result)
            elif path == "/api/report_profile":
                result = tracker.set_report_profile(ieee, body.get("profile"), body.get("seconds"))
                self._json(200 if result["ok"] else 400, result)
            elif path in ("/api/rhythm_normal", "/api/rhythm_forget"):
                act = tracker.rhythm_normal if path == "/api/rhythm_normal" else tracker.rhythm_forget
                result = act(ieee)
                self._json(200 if result["ok"] else 400, result)
            elif path == "/api/report_defaults":
                result = tracker.set_report_defaults(body.get("defaults"))
                self._json(200 if result["ok"] else 400, result)
            elif path == "/api/critical":
                ok = tracker.set_critical(ieee, bool(body.get("critical")))
                self._json(200 if ok else 404, {"ok": ok})
            elif path == "/api/limits":
                changes = {k: body[k] for k in ("battery_percent", "frozen_hours") if k in body}
                result = tracker.set_limits(ieee, changes)
                self._json(200 if result["ok"] else (404 if result["error"] == "Unknown device" else 400), result)
            elif path == "/api/bindings/ack":
                ok = tracker.acknowledge_bindings(ieee)
                self._json(200 if ok else 404, {"ok": ok})
            elif path == "/api/bindings/restore":
                keys = body.get("keys")
                result = tracker.restore_bindings(ieee, [str(k) for k in keys] if isinstance(keys, list) else None)
                self._json(200 if result["ok"] else 502, result)
            elif path == "/api/link":
                result = tracker.link(str(body.get("old") or ""), str(body.get("new") or ""))
                self._json(200 if result["ok"] else 400, result)
            elif path == "/api/replace":
                carry = body.get("carry") if isinstance(body.get("carry"), dict) else {}
                result = replace_device(str(body.get("old") or ""), str(body.get("new") or ""), carry)
                self._json(200 if result["ok"] or result["steps"] else 400, result)
            elif path == "/api/unlink":
                self._json(200, tracker.unlink(str(body.get("old") or ""), str(body.get("new") or "")))
            elif path == "/api/reconfigure":
                result = tracker.reconfigure(ieee)
                self._json(200 if result["ok"] else 502, result)
            elif path == "/api/reporting":
                # One configured reporting ({ieee, ep, cluster, attribute, min, max, change}) set through Z2M.
                fix, error = clean_reporting_fix(body)
                if fix is None:
                    self._json(400, {"ok": False, "error": error})
                    return
                result = tracker.configure_reporting(ieee, fix)
                status = {None: 200, "unknown": 404, "invalid": 400}.get(result.get("reason"), 502)
                self._json(status, result)
            elif path == "/api/networkmap":
                instance = body.get("instance") if body.get("instance") in base_topics else None
                started = run_networkmap(instance)
                self._json(
                    200 if started else 409, {"ok": started, "error": None if started else "A scan is already running"}
                )
            elif path == "/api/ping":
                acknowledge_alerts(ieee)
                result = tracker.ping(ieee)
                self._json(200 if result["ok"] or result.get("timeout") else 400, result)
            elif path == "/api/forget":
                ok = tracker.forget(ieee)
                self._json(200 if ok else 404, {"ok": ok})
            elif path == "/api/settings":
                result = apply_settings(body.get("changes") if isinstance(body.get("changes"), dict) else {})
                self._json(200 if result["ok"] else 400, result)
            elif path == "/api/alerts/pause":
                try:
                    state = tracker.pause_alerts(body.get("minutes"), body.get("until"), body.get("at"))
                except ValueError as err:
                    self._json(400, {"ok": False, "error": str(err)})
                else:
                    self._json(200, {"ok": True, "alerts_paused": state})
            elif path == "/api/alerts/resume":
                resumed = tracker.resume_alerts()
                if resumed:
                    # Send what is still wrong now rather than at the next health check.
                    threading.Thread(target=alerts.check, name="alerts-resume", daemon=True).start()
                self._json(200, {"ok": True, "resumed": resumed})
            elif path == "/api/test-notification":
                self._json(200, {"ok": True, "channels": alerts.test()})
            elif path == "/api/battery":
                ok = tracker.set_battery_info(ieee, body.get("info") if isinstance(body.get("info"), dict) else {})
                threading.Thread(target=discovery.sync, daemon=True).start()
                self._json(200 if ok else 404, {"ok": ok})
            elif path == "/api/battery/replaced":
                date = str(body.get("date") or datetime.now(tracker.local_tz).date().isoformat())
                brand = body.get("brand") if isinstance(body.get("brand"), str) else None
                entry = record_battery_replaced(ieee, date, "Zigbee Tracker", str(body.get("note") or ""), brand)
                self._json(
                    200 if entry else 400,
                    {"ok": bool(entry), "entry": entry, "error": None if entry else "Invalid device or date"},
                )
            elif path == "/api/battery_prices":
                result = tracker.set_battery_prices(body.get("prices"))
                self._json(200 if result["ok"] else 400, result)
            elif path == "/api/battery_stock":
                result = tracker.set_battery_stock(str(body.get("type") or ""), body.get("count"), body.get("delta"))
                self._json(200 if result["ok"] else 400, result)
            elif path == "/api/battery_brands/add":
                result = tracker.add_brand(body.get("brand"))
                self._json(200 if result["ok"] else 400, result)
            elif path == "/api/battery_brands/remove":
                result = tracker.remove_brand(body.get("brand"))
                self._json(200 if result["ok"] else 400, result)
            elif path == "/api/battery/brand":
                ok = tracker.set_battery_brand(ieee, body.get("index"), body.get("brand"))
                self._json(200 if ok else 400, {"ok": ok, "error": None if ok else "Invalid device, entry or brand"})
            elif path == "/api/battery/pending":
                action = body.get("action")
                ok = action in ("confirm", "dismiss") and tracker.resolve_battery_pending(ieee, action == "confirm")
                threading.Thread(target=discovery.sync, daemon=True).start()
                self._json(200 if ok else 404, {"ok": ok})
            elif path == "/api/battery/unreliable":
                ok = tracker.set_battery_unreliable(ieee, body.get("unreliable"))
                threading.Thread(target=discovery.sync, daemon=True).start()
                self._json(200 if ok else 400, {"ok": ok})
            elif path == "/api/battery/remove":
                ok = tracker.remove_battery_replacement(ieee, int(body.get("index", -1)))
                threading.Thread(target=discovery.sync, daemon=True).start()
                self._json(200 if ok else 404, {"ok": ok})
            elif path == "/api/maintenance":
                result = tracker.set_maintenance_task(ieee, body.get("task"))
                self._json(200 if result["ok"] else (404 if result["error"] == "Unknown device" else 400), result)
            elif path == "/api/firmware_notes":
                result = tracker.save_firmware_note(body.get("note"))
                self._json(200 if result["ok"] else 400, result)
            elif path == "/api/firmware_notes/delete":
                ok = tracker.delete_firmware_note(str(body.get("id") or ""))
                self._json(200 if ok else 404, {"ok": ok})
            elif path == "/api/maintenance/remove":
                ok = tracker.remove_maintenance_task(ieee, str(body.get("id") or ""))
                self._json(200 if ok else 404, {"ok": ok})
            elif path == "/api/maintenance/done":
                result = tracker.maintenance_done(ieee, str(body.get("id") or ""), body.get("date"))
                self._json(200 if result["ok"] else 400, result)
            elif path == "/api/rename":
                result = tracker.rename(ieee, str(body.get("name") or ""))
                self._json(200 if result["ok"] else 400, result)
            elif path == "/api/area":
                device_id = tracker.ha_device_ids.get(ieee)
                if not device_id:
                    # The device list is only re-read every few hours, so a newly
                    # paired device may be in HA already. Ask HA directly before giving up.
                    device_id = home_assistant.find_device(ieee)
                    if device_id:
                        with tracker.lock:
                            tracker.ha_device_ids[ieee] = device_id
                            home_assistant.device_ids[ieee] = device_id
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
            elif path == "/api/z2m/last_seen":
                result = tracker.turn_on_last_seen(str(body.get("instance") or ""))
                if result["ok"]:
                    # Zigbee2MQTT sends its new settings in bridge/info just after answering.
                    threading.Timer(2, tracker.evaluate_health).start()
                self._json(200 if result["ok"] else (400 if result["error"] == "Unknown network" else 502), result)
            elif path == "/api/advice/dismiss":
                ok = tracker.dismiss_advice(str(body.get("key") or ""))
                self._json(200 if ok else 404, {"ok": ok})
            elif path == "/api/pairing":
                instance = body.get("instance") if body.get("instance") in mqtt_topics else (mqtt_topics or [""])[0]
                result = tracker.start_pairing(
                    instance,
                    int(body.get("seconds") or 0),
                    str(body.get("via") or "") or None,
                    str(body.get("target") or "") or None,
                )
                if result["ok"] and not body.get("seconds"):
                    alerts.check()
                self._json(200 if result["ok"] else 502, result)
            elif path == "/api/ota":
                if body.get("action") == "update":
                    result = ota_manager.enqueue([ieee], force=True)
                    result["ok"] = bool(result["queued"])
                    if not result["ok"]:
                        result["error"] = (result["skipped"] or [{}])[0].get("reason") or "Already queued"
                else:
                    result = tracker.ota(ieee, str(body.get("action") or ""))
                self._json(200 if result["ok"] else 502, result)
            elif path == "/api/ota/check":
                result = ota_manager.check_all()
                self._json(200 if result["ok"] else 409, result)
            elif path == "/api/ota/queue":
                force = body.get("force") is True
                if body.get("all") is True:
                    result = ota_manager.enqueue_all(force)
                else:
                    ieees = [str(x) for x in body.get("ieees") or [] if isinstance(x, str)][:500]
                    result = ota_manager.enqueue(ieees, force)
                self._json(200, result)
            elif path == "/api/ota/cancel":
                result = ota_manager.cancel(ieee)
                self._json(200 if result["ok"] else 404, result)
            elif path == "/api/ha/remove_leftovers":
                ids = body.get("device_ids")
                ready, known = tracker.leftover_context()
                if not isinstance(ids, list) or not ids:
                    self._json(400, {"ok": False, "error": "No devices chosen"})
                elif not ready or not home_assistant.available:
                    self._json(409, {"ok": False, "error": "Not ready yet, try again in a minute"})
                else:
                    result = home_assistant.remove_leftovers([str(x) for x in ids[:500]], known)
                    try:
                        refresh_leftovers()
                    except Exception:  # noqa: BLE001 - the next refresh catches up
                        pass
                    self._json(200, result)
            elif path == "/api/retained/clear":
                topics = body.get("topics")
                if not isinstance(topics, list) or not topics:
                    self._json(400, {"ok": False, "error": "No topics chosen"})
                else:
                    result = clear_retained([str(t) for t in topics[:2000]])
                    self._json(200 if result["ok"] else 409, result)
            elif path == "/api/retained/scan":
                started = scan_retained()
                self._json(
                    200 if started else 409, {"ok": started, "error": None if started else "A scan is already running"}
                )
            elif path == "/api/retained_command/clear":
                result = clear_retained_commands(ieee)
                self._json(200 if result["ok"] else 404 if "topics" not in result else 502, result)
            elif path == "/api/summary":
                self._json(200, {"ok": True, **(send_daily_summary() if body.get("daily") else send_weekly_summary())})
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
    if STANDALONE:
        _LOGGER.info(
            "Standalone mode (no Supervisor): web UI on port %d, Home Assistant %s. The integration's API token is in %s",
            PORT,
            HA_URL or "not set",
            TOKEN_PATH,
        )
        if len(UI_PASSWORD) < standalone.MIN_UI_CHARS:
            _LOGGER.warning("The web UI stays locked: set UI_PASSWORD (%d characters or more)", standalone.MIN_UI_CHARS)
    threading.Thread(target=offer_new_notify_kinds, name="notify-kinds", daemon=True).start()

    client = None
    if "zha" in SOURCES:
        if stack == "zha":
            _LOGGER.info("ZHA mode (beta): reading Home Assistant's ZHA integration, MQTT is off")
            threading.Thread(target=remove_mqtt_entities, name="mqtt-cleanup", daemon=True).start()
        else:
            _LOGGER.info("Reading Zigbee2MQTT and Home Assistant's ZHA integration (beta) side by side")
        if home_assistant.available:
            source = ZhaSource(tracker, home_assistant._ws_connect, stop_event)
            threading.Thread(target=source.run, name="zha", daemon=True).start()
        else:
            _LOGGER.error("ZHA mode needs Home Assistant access, which the app doesn't have")
    if "matter" in SOURCES:
        _LOGGER.info("Reading Home Assistant's Matter integration (beta)")
        if home_assistant.available:
            matter_source = MatterSource(tracker, home_assistant._ws_connect, stop_event)
            threading.Thread(target=matter_source.run, name="matter", daemon=True).start()
        else:
            _LOGGER.error("Matter needs Home Assistant access, which the app doesn't have")
    if stack == "z2m":
        client = start_mqtt()
        threading.Thread(target=command_worker, name="commands", daemon=True).start()
    threading.Thread(target=scheduler, name="scheduler", daemon=True).start()
    if home_assistant.available and not STANDALONE:
        # The container's hostname is the app's hostname on Supervisor's network.
        threading.Thread(
            target=home_assistant.announce, args=(socket.gethostname(), PORT, API_TOKEN), name="announce", daemon=True
        ).start()
    if stack == "z2m":
        ota_manager.start(stop_event)
    if home_assistant.available:
        threading.Thread(
            target=home_assistant.watch,
            args=(ha_registry_changed, stop_event, on_notification_action),
            name="ha-watch",
            daemon=True,
        ).start()
    if stack == "z2m" and not tracker.data.get("imported"):
        threading.Thread(target=first_import, name="first-import", daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    metrics_server = start_metrics_server()

    def shutdown(signum, frame):
        _LOGGER.info("Stopping, saving history")
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
        if metrics_server is not None:
            threading.Thread(target=metrics_server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        server.serve_forever()
    finally:
        # Save first: nothing below may stop the history reaching disk.
        tracker.save(force=True)
        metrics.save(force=True)
        if client is not None and client.is_connected():
            try:
                client.publish(AVAILABILITY, "offline", qos=1, retain=True).wait_for_publish(2)
            except (RuntimeError, ValueError) as err:
                _LOGGER.warning("Couldn't mark the tracker offline in MQTT: %s", err)
        if client is not None:
            client.loop_stop()
            client.disconnect()
        time.sleep(0.2)


if __name__ == "__main__":
    main()
