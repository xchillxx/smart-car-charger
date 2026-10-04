"""Config flow: one-step setup + an options flow with the same schema for
later edits (entities/prices only — the live target SoC/time/rhythm live in
their own number/datetime entities, not here, see planner_state.py)."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BATTERY_CAPACITY_KWH_DEFAULT,
    CONF_CAR_CHARGE_LIMIT_ENTITY,
    CONF_CAR_CHARGE_LIMIT_FALLBACK,
    CONF_PAUSE_MODE_SENSOR,
    CONF_PAUSE_MODE_VALUE,
    DEFAULT_PAUSE_MODE_VALUE,
    CONF_CHARGE_CURRENT_ENTITY,
    CONF_CHARGE_ENERGY_ENTITY,
    CONF_CHARGE_POWER_KW,
    CONF_CHARGE_POWER_SENSOR,
    CONF_CHARGE_SWITCH,
    CONF_CHARGING_STATUS_SENSOR,
    CONF_CURRENT_PRICE_SENSOR,
    CONF_ENERGY_ADDED_SENSOR,
    CONF_FUEL_RADIUS_KM,
    CONF_FUEL_TYPE,
    CONF_HOME_BATTERY_SOC_SENSOR,
    CONF_HOME_ZONE_ENTITY,
    CONF_HOUSE_LOAD_SENSOR,
    CONF_LOCATION_TRACKER_ENTITY,
    CONF_ODOMETER_ENTITY,
    CONF_PLUGGED_IN_SENSOR,
    CONF_PV_POWER_SENSOR,
    CONF_PRICE_SOURCE,
    CONF_SOC_SENSOR,
    CONF_TANKERKOENIG_API_KEY,
    CONF_TIBBER_HOME_NICKNAME,
    DEFAULT_FUEL_RADIUS_KM,
    DEFAULT_FUEL_TYPE,
    DOMAIN,
    FUEL_TYPES,
    PRICE_SOURCE_TIBBER,
    PRICE_SOURCES,
)


def _default(d: dict, key: str) -> dict:
    """Only apply default= when a real value exists — see surplus-load-switch's
    identical helper for why (an empty default fails EntitySelector validation)."""
    value = d.get(key)
    return {"default": value} if value is not None else {}


def _schema(defaults: dict | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema({
        vol.Required(CONF_CHARGE_SWITCH, **_default(d, CONF_CHARGE_SWITCH)): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="switch")
        ),
        vol.Required(CONF_SOC_SENSOR, **_default(d, CONF_SOC_SENSOR)): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="battery")
        ),
        vol.Optional(
            CONF_CHARGING_STATUS_SENSOR, **_default(d, CONF_CHARGING_STATUS_SENSOR)
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="binary_sensor")),
        vol.Optional(
            CONF_PLUGGED_IN_SENSOR, **_default(d, CONF_PLUGGED_IN_SENSOR)
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="binary_sensor")),
        vol.Optional(
            CONF_ENERGY_ADDED_SENSOR, **_default(d, CONF_ENERGY_ADDED_SENSOR)
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        vol.Optional(
            CONF_LOCATION_TRACKER_ENTITY, **_default(d, CONF_LOCATION_TRACKER_ENTITY)
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="device_tracker")),
        vol.Optional(
            CONF_HOME_ZONE_ENTITY, **_default(d, CONF_HOME_ZONE_ENTITY)
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="zone")),
        vol.Required(
            CONF_CHARGE_POWER_KW, default=d.get(CONF_CHARGE_POWER_KW, 3.7)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0.1, max=50, step=0.1, unit_of_measurement="kW")
        ),
        vol.Optional(
            CONF_CHARGE_POWER_SENSOR, **_default(d, CONF_CHARGE_POWER_SENSOR)
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="power")),
        vol.Optional(
            CONF_CAR_CHARGE_LIMIT_ENTITY, **_default(d, CONF_CAR_CHARGE_LIMIT_ENTITY)
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="number")),
        vol.Optional(
            CONF_CAR_CHARGE_LIMIT_FALLBACK, **_default(d, CONF_CAR_CHARGE_LIMIT_FALLBACK)
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        vol.Optional(
            CONF_PAUSE_MODE_SENSOR, **_default(d, CONF_PAUSE_MODE_SENSOR)
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        vol.Optional(
            CONF_PAUSE_MODE_VALUE, default=d.get(CONF_PAUSE_MODE_VALUE, DEFAULT_PAUSE_MODE_VALUE)
        ): selector.TextSelector(),
        vol.Required(
            CONF_PRICE_SOURCE, default=d.get(CONF_PRICE_SOURCE, PRICE_SOURCE_TIBBER)
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(options=PRICE_SOURCES, translation_key="price_source")
        ),
        vol.Required(
            CONF_TIBBER_HOME_NICKNAME, **_default(d, CONF_TIBBER_HOME_NICKNAME)
        ): selector.TextSelector(),
        vol.Required(
            CONF_BATTERY_CAPACITY_KWH_DEFAULT, default=d.get(CONF_BATTERY_CAPACITY_KWH_DEFAULT, 60)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=200, step=0.1, unit_of_measurement="kWh")
        ),
        # --- "Überschuss Smart" controller (all optional) ---
        vol.Optional(
            CONF_CHARGE_CURRENT_ENTITY, **_default(d, CONF_CHARGE_CURRENT_ENTITY)
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="number")),
        vol.Optional(
            CONF_PV_POWER_SENSOR, **_default(d, CONF_PV_POWER_SENSOR)
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        vol.Optional(
            CONF_HOUSE_LOAD_SENSOR, **_default(d, CONF_HOUSE_LOAD_SENSOR)
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        vol.Optional(
            CONF_HOME_BATTERY_SOC_SENSOR, **_default(d, CONF_HOME_BATTERY_SOC_SENSOR)
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        # --- combustion-engine comparison (all optional) ---
        vol.Optional(
            CONF_TANKERKOENIG_API_KEY, **_default(d, CONF_TANKERKOENIG_API_KEY)
        ): selector.TextSelector(selector.TextSelectorConfig(type="password")),
        vol.Optional(
            CONF_FUEL_TYPE, default=d.get(CONF_FUEL_TYPE, DEFAULT_FUEL_TYPE)
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(options=FUEL_TYPES, translation_key="fuel_type")
        ),
        vol.Optional(
            CONF_FUEL_RADIUS_KM, default=d.get(CONF_FUEL_RADIUS_KM, DEFAULT_FUEL_RADIUS_KM)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=25, step=0.5, unit_of_measurement="km")
        ),
        vol.Optional(
            CONF_ODOMETER_ENTITY, **_default(d, CONF_ODOMETER_ENTITY)
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        vol.Optional(
            CONF_CHARGE_ENERGY_ENTITY, **_default(d, CONF_CHARGE_ENERGY_ENTITY)
        ): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="energy")
        ),
        vol.Optional(
            CONF_CURRENT_PRICE_SENSOR, **_default(d, CONF_CURRENT_PRICE_SENSOR)
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
    })


class SpotChargeConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            return self.async_create_entry(title="Smart Car Charger", data=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema(), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SpotChargeOptionsFlow()


class SpotChargeOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(current))
