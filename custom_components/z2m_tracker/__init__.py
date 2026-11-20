"""Z2M Device Tracker: Home Assistant side of the Z2M Device Tracker app.

The app records the history; this integration mirrors it into sensors and
serves the dashboard card.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .const import (
    CARD_FILE,
    CARDS_FILE,
    CONF_APP_URL,
    CONF_CALENDAR_TODO,
    CONF_REPAIRS,
    CONF_TOKEN,
    DEFAULT_APP_URL,
    DOMAIN,
    I18N_FILE,
    URL_BASE,
    VERSION,
)
from .services import async_register_services, async_require_admin
from .tracker import TrackerAppError, Z2MTracker
from .websocket import async_register_websocket_commands

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CALENDAR, Platform.SENSOR, Platform.TODO]
# Loading either of these makes Home Assistant add its Calendar or To-do sidebar panel.
OPTIONAL_PLATFORMS = (Platform.CALENDAR, Platform.TODO)

type Z2MTrackerConfigEntry = ConfigEntry[Z2MTracker]


def _platforms(entry: ConfigEntry) -> list[Platform]:
    if entry.options.get(CONF_CALENDAR_TODO, True):
        return PLATFORMS
    return [platform for platform in PLATFORMS if platform not in OPTIONAL_PLATFORMS]


def _remove_calendar_todo_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Calendar and to-do turned off: drop their entities instead of leaving them unavailable."""
    registry = er.async_get(hass)
    domains = {str(platform) for platform in OPTIONAL_PLATFORMS}
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.domain in domains:
            registry.async_remove(entity.entity_id)


async def _async_register_frontend(hass: HomeAssistant) -> None:
    if hass.data.get(f"{DOMAIN}_frontend"):
        return
    www_path = Path(__file__).parent / "www"
    await hass.http.async_register_static_paths([StaticPathConfig(URL_BASE, str(www_path), False)])
    add_extra_js_url(hass, f"{URL_BASE}/{I18N_FILE}?v={VERSION}")
    add_extra_js_url(hass, f"{URL_BASE}/{CARD_FILE}?v={VERSION}")
    add_extra_js_url(hass, f"{URL_BASE}/{CARDS_FILE}?v={VERSION}")
    async_register_websocket_commands(hass)
    hass.data[f"{DOMAIN}_frontend"] = True


async def async_setup_entry(hass: HomeAssistant, entry: Z2MTrackerConfigEntry) -> bool:
    app_url = entry.options.get(CONF_APP_URL, entry.data.get(CONF_APP_URL, DEFAULT_APP_URL))
    token = entry.options.get(CONF_TOKEN, entry.data.get(CONF_TOKEN)) or None
    tracker = Z2MTracker(hass, app_url, entry.options.get(CONF_REPAIRS, True), token)
    await tracker.async_start()
    entry.runtime_data = tracker
    hass.data[DOMAIN] = tracker

    await _async_register_frontend(hass)

    if not hass.services.has_service(DOMAIN, "import_history"):

        async def _import_history(call: ServiceCall) -> ServiceResponse:
            await async_require_admin(hass, call.context.user_id)
            current: Z2MTracker | None = hass.data.get(DOMAIN)
            if current is None:
                raise HomeAssistantError("Z2M Device Tracker is not loaded")
            try:
                return await current.async_import_history()
            except TrackerAppError as err:
                raise HomeAssistantError(f"Z2M Device Tracker app error: {err}") from err

        hass.services.async_register(
            DOMAIN,
            "import_history",
            _import_history,
            supports_response=SupportsResponse.OPTIONAL,
        )

    async_register_services(hass)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    platforms = _platforms(entry)
    if platforms != PLATFORMS:
        _remove_calendar_todo_entities(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    hass.data[f"{DOMAIN}_platforms"] = platforms
    return True


async def _async_options_updated(hass: HomeAssistant, entry: Z2MTrackerConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: Z2MTrackerConfigEntry) -> bool:
    # The platforms set up last time: on an options change the entry already holds the new choice.
    platforms = hass.data.get(f"{DOMAIN}_platforms", PLATFORMS)
    unloaded = await hass.config_entries.async_unload_platforms(entry, platforms)
    if unloaded:
        hass.data.pop(f"{DOMAIN}_platforms", None)
        await entry.runtime_data.async_stop()
        hass.data.pop(DOMAIN, None)
    return unloaded
