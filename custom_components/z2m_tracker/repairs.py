"""Fix flows for the issues the integration mirrors into Settings > Repairs."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN
from .tracker import TrackerAppError, Z2MTracker

BATTERY_KINDS = {"battery_low", "battery_soon", "battery_drain"}

# How long a fixed issue stays out of Repairs while the fix takes effect.
SNOOZE_BATTERY = 24 * 3600  # a new battery can take hours to report its level
SNOOZE_BINDINGS = 10 * 60  # Z2M republishes the bindings once the device answers


def menu_for(kind: str) -> list[str]:
    """The fixes that make sense for one kind of issue; muting fits every device issue."""
    if kind == "binding_lost":
        options = ["restore_binding", "bindings_removed"]
    elif kind in BATTERY_KINDS:
        options = ["battery_replaced"]
    elif kind == "frozen":
        options = ["reconfigure"]
    elif kind == "update":
        options = ["install_update"]
    else:
        options = []
    return [*options, "mute_day", "mute_forever"]


class DeviceIssueRepairFlow(RepairsFlow):
    """Offers the app actions that fix (or silence) one device issue."""

    def __init__(self, issue_id: str, data: dict[str, Any]) -> None:
        self._issue_id = issue_id
        self._ieee = str(data.get("ieee") or "")
        self._kind = str(data.get("kind") or "")

    def _tracker(self) -> Z2MTracker | None:
        return self.hass.data.get(DOMAIN)

    def _placeholders(self, tracker: Z2MTracker) -> dict[str, str]:
        issue = next(
            (i for i in tracker.issues() if i.get("ieee") == self._ieee and i.get("kind") == self._kind),
            {},
        )
        record = tracker.data["devices"].get(self._ieee) or {}
        return {
            "name": str(issue.get("name") or record.get("friendly_name") or self._ieee),
            "title": str(issue.get("label") or issue.get("title") or ""),
            "detail": str(issue.get("detail") or "The app no longer reports this problem."),
            "panel": tracker.panel,
        }

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> FlowResult:
        tracker = self._tracker()
        if tracker is None:
            return self.async_abort(reason="not_loaded")
        return self.async_show_menu(
            step_id="init",
            menu_options=menu_for(self._kind),
            description_placeholders=self._placeholders(tracker),
        )

    async def _run(self, action, snooze: float = 0) -> FlowResult:
        tracker = self._tracker()
        if tracker is None:
            return self.async_abort(reason="not_loaded")
        if snooze:
            tracker.snooze_repair(self._issue_id, snooze)
        try:
            await action(tracker)
        except TrackerAppError as err:
            tracker.snooze_repair(self._issue_id, 0)
            return self.async_abort(reason="app_error", description_placeholders={"error": str(err)})
        # Finishing the flow deletes the issue; it comes back if the app still reports it.
        return self.async_create_entry(data={})

    async def async_step_restore_binding(self, user_input: dict[str, str] | None = None) -> FlowResult:
        tracker = self._tracker()
        if tracker is None:
            return self.async_abort(reason="not_loaded")
        if user_input is None:
            lost = (tracker.data["devices"].get(self._ieee) or {}).get("lost_bindings") or []
            bindings = "\n".join(f"- {b.get('label') or b.get('key')}" for b in lost[:10]) or "- (none left)"
            return self.async_show_form(
                step_id="restore_binding",
                data_schema=vol.Schema({}),
                description_placeholders={**self._placeholders(tracker), "bindings": bindings},
            )
        return await self._run(lambda t: t.async_restore_bindings(self._ieee), SNOOZE_BINDINGS)

    async def async_step_bindings_removed(self, user_input: dict[str, str] | None = None) -> FlowResult:
        return await self._run(lambda t: t.async_acknowledge_bindings(self._ieee))

    async def async_step_battery_replaced(self, user_input: dict[str, str] | None = None) -> FlowResult:
        return await self._run(lambda t: t.async_battery_replaced(self._ieee), SNOOZE_BATTERY)

    async def async_step_reconfigure(self, user_input: dict[str, str] | None = None) -> FlowResult:
        return await self._run(lambda t: t.async_reconfigure(self._ieee))

    async def async_step_install_update(self, user_input: dict[str, str] | None = None) -> FlowResult:
        return await self._run(lambda t: t.async_ota_update(self._ieee))

    async def async_step_mute_day(self, user_input: dict[str, str] | None = None) -> FlowResult:
        return await self._run(lambda t: t.async_mute(self._ieee, [self._kind], 1))

    async def async_step_mute_forever(self, user_input: dict[str, str] | None = None) -> FlowResult:
        return await self._run(lambda t: t.async_mute(self._ieee, [self._kind], None))


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    return DeviceIssueRepairFlow(issue_id, dict(data or {}))
