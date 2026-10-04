"""The manual master switch (only while ON does this integration actuate
the configured charge switch at all — see coordinator._actuate_switch's
hands-off-when-off policy; deliberately manual, no auto-detection against a
PV-surplus setup), plus one "aktiv" switch per cycle slot.

A slot's "aktiv" switch is both its on/off and its holiday pause: OFF keeps
every setting (name / target SoC / time / rhythm) and just stops producing
occurrences; ON resumes, re-basing the N-day rhythm to "now".
"""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NUM_CYCLE_SLOTS
from .coordinator import SpotChargeCoordinator
from .device import hub_device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SpotChargeCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [MasterSwitch(coordinator, entry), OneoffDepartureSwitch(coordinator, entry)]
        + [CycleSlotEnabledSwitch(coordinator, entry, n) for n in range(1, NUM_CYCLE_SLOTS + 1)]
    )


class MasterSwitch(CoordinatorEntity[SpotChargeCoordinator], SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "Lademodus aktiv"
    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_master_switch"

    @property
    def device_info(self):
        return hub_device_info(self._entry.entry_id)

    @property
    def is_on(self) -> bool:
        return self.coordinator.planner_state.master_switch_on

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_master_switch(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_master_switch(False)


class CycleSlotEnabledSwitch(CoordinatorEntity[SpotChargeCoordinator], SwitchEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar-check"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry, slot_no: int) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._slot_no = slot_no
        self._attr_unique_id = f"{entry.entry_id}_slot_{slot_no}_enabled"
        self._attr_name = f"Slot {slot_no} aktiv"

    @property
    def device_info(self):
        return hub_device_info(self._entry.entry_id)

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.get_slot(self._slot_no).get("enabled"))

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_slot_field(self._slot_no, "enabled", True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_slot_field(self._slot_no, "enabled", False)


class OneoffDepartureSwitch(CoordinatorEntity[SpotChargeCoordinator], SwitchEntity):
    """One dated departure on top of the cycle slots (e.g. "tomorrow 12:00");
    it switches itself off once it has passed."""

    _attr_has_entity_name = True
    _attr_name = "Einmalige Abfahrt aktiv"
    _attr_icon = "mdi:car-clock"

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_oneoff_departure_enabled"

    @property
    def device_info(self):
        return hub_device_info(self._entry.entry_id)

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.planner_state.oneoff_departure.get("enabled"))

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_oneoff_departure("enabled", True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_oneoff_departure("enabled", False)
