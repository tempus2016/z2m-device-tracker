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

from .const import CARD_FILE, CONF_APP_URL, CONF_REPAIRS, DEFAULT_APP_URL, DOMAIN, URL_BASE, VERSION
from .services import async_register_services
from .tracker import TrackerAppError, Z2MTracker
from .websocket import async_register_websocket_commands

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

type Z2MTrackerConfigEntry = ConfigEntry[Z2MTracker]


async def _async_register_frontend(hass: HomeAssistant) -> None:
    if hass.data.get(f"{DOMAIN}_frontend"):
        return
    www_path = Path(__file__).parent / "www"
    await hass.http.async_register_static_paths([StaticPathConfig(URL_BASE, str(www_path), False)])
    add_extra_js_url(hass, f"{URL_BASE}/{CARD_FILE}?v={VERSION}")
    async_register_websocket_commands(hass)
    hass.data[f"{DOMAIN}_frontend"] = True


async def async_setup_entry(hass: HomeAssistant, entry: Z2MTrackerConfigEntry) -> bool:
    app_url = entry.options.get(CONF_APP_URL, entry.data.get(CONF_APP_URL, DEFAULT_APP_URL))
    tracker = Z2MTracker(hass, app_url, entry.options.get(CONF_REPAIRS, True))
    await tracker.async_start()
    entry.runtime_data = tracker
    hass.data[DOMAIN] = tracker

    await _async_register_frontend(hass)

    if not hass.services.has_service(DOMAIN, "import_history"):

        async def _import_history(call: ServiceCall) -> ServiceResponse:
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
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: Z2MTrackerConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: Z2MTrackerConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
        hass.data.pop(DOMAIN, None)
    return unloaded
