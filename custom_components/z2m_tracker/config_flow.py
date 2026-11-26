"""Config flow for Z2M Device Tracker."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from .const import (
    APP_PORT,
    APP_SLUG_SUFFIX,
    CONF_APP_URL,
    CONF_CALENDAR_TODO,
    CONF_REPAIRS,
    CONF_TOKEN,
    DEFAULT_APP_URL,
    DOMAIN,
)
from .tracker import async_test_connection


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


async def _checked(
    hass, user_input: dict[str, Any], errors: dict[str, str], ask_app: bool = True
) -> dict[str, Any] | None:
    """The form's values ready to store, once the app answers at that address with that token.
    Otherwise None, with the reason in errors. ask_app=False only checks the address's shape."""
    url = str(user_input[CONF_APP_URL]).strip()
    token = str(user_input.get(CONF_TOKEN) or "").strip()
    if not valid_app_url(url):
        errors[CONF_APP_URL] = "invalid_url"
        return None
    problem = await async_test_connection(hass, url, token or None) if ask_app else None
    if problem == "invalid_auth":
        errors[CONF_TOKEN if CONF_TOKEN in user_input else "base"] = problem
        return None
    if problem:
        errors[CONF_APP_URL] = problem
        return None
    return {**user_input, CONF_APP_URL: url, CONF_TOKEN: token}


class Z2MTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    _discovered_url: str | None = None

    async def async_step_hassio(self, discovery_info: Any) -> ConfigFlowResult:
        """The app announced itself through Supervisor: offer the address it gave, not a guess."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        config = getattr(discovery_info, "config", None) or {}
        if config.get("host") and config.get("port"):
            self._discovered_url = f"http://{config['host']}:{config['port']}"
        return await self.async_step_user()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = await _checked(self.hass, user_input, errors)
            if data is not None:
                return self.async_create_entry(title="Z2M Device Tracker", data=data)
        given = user_input or {}
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_APP_URL, default=given.get(CONF_APP_URL) or self._discovered_url or detect_app_url(self.hass)
            ): str
        }
        if not self._discovered_url:
            # Supervisor hands the token over by itself; without it (plain Docker) it is typed in.
            schema[vol.Optional(CONF_TOKEN, default=given.get(CONF_TOKEN) or "")] = str
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(schema),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change where the app is (it moved, or its API token changed) without removing the integration."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = await _checked(self.hass, user_input, errors)
            if data is not None:
                # The options hold the address too once they were saved: both are kept the same.
                return self.async_update_reload_and_abort(entry, data_updates=data, options={**entry.options, **data})
        given = user_input or {}
        url = given.get(CONF_APP_URL) or entry.options.get(CONF_APP_URL, entry.data.get(CONF_APP_URL, DEFAULT_APP_URL))
        token = given.get(CONF_TOKEN) or entry.options.get(CONF_TOKEN, entry.data.get(CONF_TOKEN, ""))
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_APP_URL, default=url): str,
                    vol.Optional(CONF_TOKEN, description={"suggested_value": token}): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return Z2MTrackerOptionsFlow()


class Z2MTrackerOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self.config_entry
        current = entry.options.get(CONF_APP_URL, entry.data.get(CONF_APP_URL, detect_app_url(self.hass)))
        token = entry.options.get(CONF_TOKEN, entry.data.get(CONF_TOKEN, ""))
        if user_input is not None:
            # The other options must stay changeable while the app is stopped: it is only asked
            # when the address or the token is new.
            moved = str(user_input[CONF_APP_URL]).strip() != current or str(
                user_input.get(CONF_TOKEN) or ""
            ).strip() != (token or "")
            data = await _checked(self.hass, user_input, errors, ask_app=moved)
            if data is not None:
                return self.async_create_entry(data=data)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_APP_URL, default=current): str,
                    vol.Optional(CONF_TOKEN, description={"suggested_value": token}): str,
                    vol.Required(CONF_REPAIRS, default=self.config_entry.options.get(CONF_REPAIRS, True)): bool,
                    vol.Required(
                        CONF_CALENDAR_TODO, default=self.config_entry.options.get(CONF_CALENDAR_TODO, True)
                    ): bool,
                }
            ),
            errors=errors,
        )
