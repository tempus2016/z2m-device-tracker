"""Constants for Z2M Device Tracker."""

DOMAIN = "z2m_tracker"
VERSION = "1.2.0"

CONF_APP_URL = "app_url"
CONF_REPAIRS = "repairs"
DEFAULT_APP_URL = "http://local-z2m-tracker:8099"
APP_SLUG_SUFFIX = "z2m_tracker"
APP_PORT = 8099

SIGNAL_UPDATED = f"{DOMAIN}_updated"

URL_BASE = "/z2m_tracker"
CARD_FILE = "z2m-tracker-card.js"
CARDS_FILE = "z2m-tracker-cards.js"
I18N_FILE = "z2m-tracker-i18n.js"

STATUS_ACTIVE = "active"
STATUS_REMOVED = "removed"
