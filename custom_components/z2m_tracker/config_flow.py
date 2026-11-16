"""Config flow for Z2M Device Tracker."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from .const import APP_PORT, APP_SLUG_SUFFIX, CONF_APP_URL, CONF_REPAIRS, DEFAULT_APP_URL, DOMAIN


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


def valid_app_url(url: str) -> bool:
    """An http(s) URL with a host and no user:password (the app needs none, and it would end up in logs)."""
    try:
        parts = urlsplit(str(url).strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname) and not parts.username and not parts.password


class Z2MTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if valid_app_url(user_input[CONF_APP_URL]):
                return self.async_create_entry(title="Z2M Device Tracker", data=user_input)
            errors[CONF_APP_URL] = "invalid_url"
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_APP_URL, default=detect_app_url(self.hass)): str}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return Z2MTrackerOptionsFlow()


class Z2MTrackerOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if valid_app_url(user_input[CONF_APP_URL]):
                return self.async_create_entry(data=user_input)
            errors[CONF_APP_URL] = "invalid_url"
        current = self.config_entry.options.get(
            CONF_APP_URL, self.config_entry.data.get(CONF_APP_URL, detect_app_url(self.hass))
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_APP_URL, default=current): str,
                    vol.Required(CONF_REPAIRS, default=self.config_entry.options.get(CONF_REPAIRS, True)): bool,
                }
            ),
            errors=errors,
        )
