"""Date entity: the day of the one-off departure (see switch.py / time.py)."""
from __future__ import annotations

from datetime import date as dt_date

from homeassistant.components.date import DateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SpotChargeCoordinator
from .device import hub_device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SpotChargeCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([OneoffDepartureDate(coordinator, entry)])


class OneoffDepartureDate(CoordinatorEntity[SpotChargeCoordinator], DateEntity):
    _attr_has_entity_name = True
    _attr_name = "Einmalige Abfahrt Datum"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_oneoff_departure_date"

    @property
    def device_info(self):
        return hub_device_info(self._entry.entry_id)

    @property
    def native_value(self) -> dt_date | None:
        raw = self.coordinator.planner_state.oneoff_departure.get("date")
        try:
            return dt_date.fromisoformat(raw) if raw else None
        except ValueError:
            return None

    async def async_set_value(self, value: dt_date) -> None:
        await self.coordinator.async_set_oneoff_departure("date", value.isoformat())
