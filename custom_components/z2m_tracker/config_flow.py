"""Config flow for Z2M Device Tracker."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from .const import APP_PORT, APP_SLUG_SUFFIX, CONF_APP_URL, DEFAULT_APP_URL, DOMAIN


def detect_app_url(hass) -> str:
    """Find the Z2M Device Tracker app, whichever repository it was installed from."""
    try:
        from homeassistant.components.hassio import get_addons_info

        infos = get_addons_info(hass) or {}
    except Exception:  # noqa: BLE001 - Supervisor not available, or API changed
        infos = {}
    for slug, info in infos.items():
        if slug == APP_SLUG_SUFFIX or slug.endswith(f"_{APP_SLUG_SUFFIX}"):
            host = (info or {}).get("hostname") or slug.replace("_", "-")
            return f"http://{host}:{APP_PORT}"
    return DEFAULT_APP_URL


class Z2MTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="Z2M Device Tracker", data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_APP_URL, default=detect_app_url(self.hass)): str}),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return Z2MTrackerOptionsFlow()


class Z2MTrackerOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(
            CONF_APP_URL, self.config_entry.data.get(CONF_APP_URL, detect_app_url(self.hass))
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required(CONF_APP_URL, default=current): str}),
        )
