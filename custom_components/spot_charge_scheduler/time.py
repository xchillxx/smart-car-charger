"""Time entities: the time of day each cycle slot's target SoC should be
reached by. Stored on the slot as an "HH:MM" string; edited directly on the
dashboard via coordinator.async_set_slot_field — see planner_state.py."""
from __future__ import annotations

from datetime import time as dt_time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NUM_CYCLE_SLOTS
from .coordinator import SpotChargeCoordinator
from .device import hub_device_info
from .schedule import parse_hhmm


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SpotChargeCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [CycleSlotTimeTime(coordinator, entry, n) for n in range(1, NUM_CYCLE_SLOTS + 1)]
        + [OneoffDepartureTime(coordinator, entry)]
    )


class CycleSlotTimeTime(CoordinatorEntity[SpotChargeCoordinator], TimeEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:clock-time-four-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry, slot_no: int) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._slot_no = slot_no
        self._attr_unique_id = f"{entry.entry_id}_slot_{slot_no}_time"
        self._attr_name = f"Slot {slot_no} Uhrzeit"

    @property
    def device_info(self):
        return hub_device_info(self._entry.entry_id)

    @property
    def native_value(self) -> dt_time:
        h, m = parse_hhmm(self.coordinator.get_slot(self._slot_no).get("time"))
        return dt_time(hour=h, minute=m)

    async def async_set_value(self, value: dt_time) -> None:
        await self.coordinator.async_set_slot_field(
            self._slot_no, "time", value.strftime("%H:%M")
        )


class OneoffDepartureTime(CoordinatorEntity[SpotChargeCoordinator], TimeEntity):
    """Time of day of the one-off departure (its date is a separate entity)."""

    _attr_has_entity_name = True
    _attr_name = "Einmalige Abfahrt Uhrzeit"
    _attr_icon = "mdi:clock-time-four-outline"

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_oneoff_departure_time"

    @property
    def device_info(self):
        return hub_device_info(self._entry.entry_id)

    @property
    def native_value(self) -> dt_time:
        h, m = parse_hhmm(self.coordinator.planner_state.oneoff_departure.get("time"))
        return dt_time(hour=h, minute=m)

    async def async_set_value(self, value: dt_time) -> None:
        await self.coordinator.async_set_oneoff_departure("time", value.strftime("%H:%M"))
