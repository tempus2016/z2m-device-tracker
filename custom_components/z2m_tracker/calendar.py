"""Battery replacements as a calendar: predicted dates ahead, recorded replacements behind."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .batteries import past_replacements, predicted_replacements
from .const import DOMAIN, SIGNAL_UPDATED, VERSION
from .tracker import Z2MTracker


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([BatteryCalendar(entry, entry.runtime_data)])


def _all_day(day: date, summary: str, description: str, uid: str) -> CalendarEvent:
    return CalendarEvent(start=day, end=day + timedelta(days=1), summary=summary, description=description, uid=uid)


class BatteryCalendar(CalendarEntity):
    """All-day events: each device's predicted replacement date and its past replacements."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "batteries"

    def __init__(self, entry, tracker: Z2MTracker) -> None:
        self._tracker = tracker
        self._attr_unique_id = f"{entry.entry_id}_battery_calendar"
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

    def _events(self) -> list[CalendarEvent]:
        today = dt_util.now().date()
        events = []
        for item in predicted_replacements(self._tracker.data, today):
            level = f" It's at {round(item['battery'])}% now." if isinstance(item["battery"], (int, float)) else ""
            battery = f"Takes {item['label']}. " if item["label"] else ""
            events.append(
                _all_day(
                    item["date"],
                    f"Replace battery: {item['name']}",
                    f"{battery}Predicted from how fast the battery has been draining.{level} "
                    "The date moves as new readings come in.",
                    f"{item['ieee']}_due",
                )
            )
        for item in past_replacements(self._tracker.data):
            source = item["source"]
            if source == "Detected":
                source = "Spotted from a jump in the battery level."
            elif source:
                source = f"Recorded in {source}."
            detail = [f"Takes {item['label']}." if item["label"] else "", source]
            if item["note"]:
                detail.append(item["note"])
            events.append(
                _all_day(
                    item["date"],
                    f"Battery replaced: {item['name']}",
                    " ".join(part for part in detail if part),
                    f"{item['ieee']}_replaced_{item['date'].isoformat()}",
                )
            )
        return sorted(events, key=lambda e: (e.start, e.summary.lower()))

    @property
    def event(self) -> CalendarEvent | None:
        today = dt_util.now().date()
        return next((e for e in self._events() if e.uid.endswith("_due") and e.start >= today), None)

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        return [
            e
            for e in self._events()
            if dt_util.start_of_local_day(e.start) < end_date and dt_util.start_of_local_day(e.end) > start_date
        ]
