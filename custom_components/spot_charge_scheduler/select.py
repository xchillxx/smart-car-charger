"""Controller selector: who drives the charge switch — the price planner
("Preis-Optimiert") or the PV-surplus current control ("Überschuss Smart",
see smart_surplus.py). There is deliberately no "off" option: the master
switch ("Lademodus aktiv") is the off switch, and with the wallbox in its own
PV mode the price planner stands down by itself (pause-mode sensor)."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONTROLLER_OPTIONS, DOMAIN
from .coordinator import SpotChargeCoordinator
from .device import hub_device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SpotChargeCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ControllerSelect(coordinator, entry)])


class ControllerSelect(CoordinatorEntity[SpotChargeCoordinator], SelectEntity):
    _attr_has_entity_name = True
    _attr_name = "Regler"
    _attr_icon = "mdi:tune-variant"
    _attr_options = list(CONTROLLER_OPTIONS.values())

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_controller_mode"

    @property
    def device_info(self):
        return hub_device_info(self._entry.entry_id)

    @property
    def current_option(self) -> str:
        return CONTROLLER_OPTIONS.get(
            self.coordinator.planner_state.controller_mode, CONTROLLER_OPTIONS["price"]
        )

    async def async_select_option(self, option: str) -> None:
        for key, label in CONTROLLER_OPTIONS.items():
            if label == option:
                await self.coordinator.async_set_controller_mode(key)
                return
