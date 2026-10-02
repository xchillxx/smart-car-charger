"""Smart Car Charger — Home Assistant custom integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_CHARGING_STATUS_SENSOR,
    CONF_PAUSE_MODE_SENSOR,
    CONF_PLUGGED_IN_SENSOR,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import SpotChargeCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = SpotChargeCoordinator(hass, {**entry.data, **entry.options}, entry.entry_id)
    await coordinator.async_setup()
    await coordinator.async_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # React to the car starting/stopping a charge or being plugged in right
    # away instead of waiting for the next 60 s poll — otherwise a session
    # that starts outside the plan keeps running for up to a minute.
    watched = [
        e
        for e in (
            coordinator._config.get(CONF_CHARGING_STATUS_SENSOR),
            coordinator._config.get(CONF_PLUGGED_IN_SENSOR),
            coordinator._config.get(CONF_PAUSE_MODE_SENSOR),
        )
        if e
    ]
    if watched:

        async def _on_vehicle_change(event: Event) -> None:
            await coordinator.async_request_refresh()

        entry.async_on_unload(async_track_state_change_event(hass, watched, _on_vehicle_change))

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator: SpotChargeCoordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_flush_state()

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded
