"""Summary sensors for Z2M Device Tracker."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, SIGNAL_UPDATED, VERSION
from .tracker import Z2MTracker


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    tracker: Z2MTracker = entry.runtime_data
    async_add_entities(
        [
            CountSensor(entry, tracker, "active", "Active devices", "mdi:zigbee"),
            CountSensor(entry, tracker, "offline", "Offline devices", "mdi:lan-disconnect"),
            CountSensor(entry, tracker, "removed", "Removed devices", "mdi:archive-outline"),
            LastEventSensor(entry, tracker),
            IssuesSensor(entry, tracker),
        ]
    )


class _TrackerEntity(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry, tracker: Z2MTracker, key: str) -> None:
        self._tracker = tracker
        self._attr_unique_id = f"{entry.entry_id}_{key}"
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
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATED, self._refresh))

    @callback
    def _refresh(self) -> None:
        self.async_write_ha_state()


class CountSensor(_TrackerEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "devices"

    def __init__(self, entry, tracker: Z2MTracker, key: str, name: str, icon: str) -> None:
        super().__init__(entry, tracker, key)
        self._key = key
        self._attr_name = name
        self._attr_icon = icon

    @property
    def native_value(self) -> int:
        return self._tracker.counts()[self._key]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self._key == "offline":
            return {"devices": self._tracker.offline_names()}
        return None


class LastEventSensor(_TrackerEntity):
    _attr_name = "Last event"
    _attr_icon = "mdi:history"

    def __init__(self, entry, tracker: Z2MTracker) -> None:
        super().__init__(entry, tracker, "last_event")

    @property
    def native_value(self) -> str | None:
        event = self._tracker.last_event()
        if not event:
            return None
        text = f"{event['kind']}: {event.get('name') or event['ieee']}"
        return text[:255]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self._tracker.last_event()


class IssuesSensor(_TrackerEntity):
    """Devices that need attention, as found by the app's health checks."""

    _attr_name = "Issues"
    _attr_icon = "mdi:alert-circle-outline"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "issues"

    def __init__(self, entry, tracker: Z2MTracker) -> None:
        super().__init__(entry, tracker, "issues")

    @property
    def native_value(self) -> int:
        return sum(1 for issue in self._tracker.issues() if issue.get("severity") == "warning")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        issues = self._tracker.issues()
        return {
            "warnings": [
                {
                    "device": i.get("name"),
                    "issue": i.get("label"),
                    "detail": i.get("detail"),
                    "critical": bool(i.get("critical")),
                }
                for i in issues
                if i.get("severity") == "warning"
            ][:25],
            "info_count": sum(1 for i in issues if i.get("severity") != "warning"),
        }
