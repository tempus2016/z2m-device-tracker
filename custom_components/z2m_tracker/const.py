"""Constants for Z2M Device Tracker, and the pure rules for what goes into Repairs (no HA imports)."""

from __future__ import annotations

import re

DOMAIN = "z2m_tracker"
VERSION = "2.7.0"

CONF_APP_URL = "app_url"
# The app's API token, entered by hand when there is no Supervisor to hand it over (plain Docker).
CONF_TOKEN = "token"
CONF_REPAIRS = "repairs"
# Off: no calendar or to-do entities, so Home Assistant adds no Calendar or To-do sidebar panel for them.
CONF_CALENDAR_TODO = "calendar_todo"
DEFAULT_APP_URL = "http://local-z2m-tracker:8099"
APP_SLUG_SUFFIX = "z2m_tracker"
APP_PORT = 8099
# Supervisor discovery service the app announces itself with (address and API token).
DISCOVERY_SERVICE = "z2m_tracker"

SIGNAL_UPDATED = f"{DOMAIN}_updated"

URL_BASE = "/z2m_tracker"
CARD_FILE = "z2m-tracker-card.js"
CARDS_FILE = "z2m-tracker-cards.js"
I18N_FILE = "z2m-tracker-i18n.js"

STATUS_ACTIVE = "active"
STATUS_REMOVED = "removed"


def serious_repair(issue: dict) -> str | None:
    """The Repairs translation key for a serious problem (a critical device offline, a Zigbee2MQTT
    network offline, a coordinator backup problem), or None for any other issue."""
    kind = issue.get("kind")
    if kind == "offline" and issue.get("critical"):
        return "critical_offline"
    if kind == "network_offline" and issue.get("expected"):
        # It goes offline at this time most days and comes back: listed in the app, not a repair.
        return None
    if kind in ("network_offline", "coordinator_backup"):
        return str(kind)
    return None


def repairs_on(options: dict) -> bool:
    """False while the app's alerts or its "Show problems in HA Repairs" switch are off."""
    return options.get("alerts_enabled") is not False and options.get("ha_repairs") is not False


def repairs_wanted(issues: list[dict], options: dict, limit: int) -> dict[str, dict]:
    """The app's issues to show in Repairs, by issue ID: warnings, checks set to notify and the
    serious problems. Serious ones come first, so a flood of other issues can't push them out.
    A device behind an offline router (or folded into another device's issue) is covered by that issue."""
    notify = set(options.get("notify_checks") or []) - set(options.get("disabled_checks") or [])
    keys = {str(issue.get("key", "")) for issue in issues}
    wanted: dict[str, dict] = {}
    for issue in sorted(issues, key=lambda issue: serious_repair(issue) is None):
        if issue.get("behind") and f"offline:{issue['behind']}" in keys:
            continue
        if len(wanted) >= limit:
            # A flood of (possibly spoofed) devices must not bury real problems in Settings > Repairs.
            break
        if serious_repair(issue) or issue.get("severity") == "warning" or issue.get("kind") in notify:
            wanted["issue_" + re.sub(r"[^a-z0-9_]+", "_", str(issue.get("key", "")).lower())] = issue
    return wanted
