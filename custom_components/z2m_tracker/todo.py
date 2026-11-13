"""Batteries to buy, as a Home Assistant to-do list. Ticking an item marks those batteries replaced."""

from __future__ import annotations

import re

from homeassistant.components.todo import TodoItem, TodoItemStatus, TodoListEntity, TodoListEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .batteries import describe_member, group_summary, recent_replacements, shopping_groups, stock_line
from .const import DOMAIN, SIGNAL_UPDATED, VERSION
from .tracker import TrackerAppError, Z2MTracker

DONE_NOTE = "Ticked on the Home Assistant to-do list"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "unknown"


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([BatteryTodoList(entry, entry.runtime_data)])


class BatteryTodoList(TodoListEntity):
    """One open item per battery type needed soon; recent replacements show as completed."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "batteries"
    _attr_supported_features = TodoListEntityFeature.UPDATE_TODO_ITEM

    def __init__(self, entry, tracker: Z2MTracker) -> None:
        self._tracker = tracker
        self.entity_id = "todo.z2m_tracker_batteries"
        self._attr_unique_id = f"{entry.entry_id}_battery_todo"
        # Devices ticked here, hidden until the integration next reads the app (which by then leaves them out).
        self._ticked: set[str] = set()
        self._ticked_data: dict | None = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Z2M Device Tracker",
            entry_type=DeviceEntryType.SERVICE,
            sw_version=VERSION,
        )

    @property
    def available(self) -> bool:
        return self._tracker.available

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATED, self._refresh))

    @callback
    def _refresh(self) -> None:
        self.async_write_ha_state()

    def _open_groups(self) -> dict[str, dict]:
        today = dt_util.now().date()
        if self._ticked and self._tracker.data is not self._ticked_data:
            self._ticked.clear()
        return {f"buy_{_slug(g['type'])}": g for g in shopping_groups(self._tracker.data, today, self._ticked)}

    @property
    def todo_items(self) -> list[TodoItem]:
        today = dt_util.now().date()
        items = [
            TodoItem(
                summary=group_summary(group),
                uid=uid,
                status=TodoItemStatus.NEEDS_ACTION,
                due=group["due"],
                description=stock_line(group) + ", ".join(describe_member(m) for m in group["devices"]),
            )
            for uid, group in sorted(self._open_groups().items(), key=lambda kv: (kv[1]["due"], kv[0]))
        ]
        for group in recent_replacements(self._tracker.data, today)[:10]:
            items.append(
                TodoItem(
                    summary=f"{group['type']} × {group['count']}",
                    uid=f"done_{group['date'].isoformat()}_{_slug(group['type'])}",
                    status=TodoItemStatus.COMPLETED,
                    description=f"{', '.join(sorted(group['names'], key=str.lower))}. Replaced {group['date'].day} {group['date']:%b}.",
                )
            )
        return items

    async def async_update_todo_item(self, item: TodoItem) -> None:
        if item.uid and item.uid.startswith("done_"):
            if item.status == TodoItemStatus.NEEDS_ACTION:
                raise HomeAssistantError(
                    "To undo a battery replacement, remove it from the device in the Zigbee Tracker app."
                )
            return
        group = self._open_groups().get(item.uid or "")
        if group is None:
            raise HomeAssistantError("That item is no longer on the list. It may already be done.")
        if item.status != TodoItemStatus.COMPLETED:
            return
        ieees = [d["ieee"] for d in group["devices"]]
        self._ticked_data = self._tracker.data
        self._ticked.update(ieees)
        try:
            await self._tracker.async_battery_replaced(ieees, note=DONE_NOTE)
        except TrackerAppError as err:
            self._ticked.difference_update(ieees)
            raise HomeAssistantError(f"Z2M Device Tracker app error: {err}") from err
        finally:
            self.async_write_ha_state()
