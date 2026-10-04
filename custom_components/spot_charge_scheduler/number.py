"""Number entities: the live, freely-editable planning values. These read
and write coordinator.planner_state directly (see coordinator.py's setters)
rather than the config entry — editing them must not reload the integration
mid-session."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_CAR_CHARGE_LIMIT_ENTITY, DOMAIN, NUM_CYCLE_SLOTS
from .coordinator import SpotChargeCoordinator
from .device import hub_device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SpotChargeCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        BatteryCapacityNumber(coordinator, entry),
        ChargePowerNumber(coordinator, entry),
        OpportunisticPercentileNumber(coordinator, entry),
        ExpensivePercentileNumber(coordinator, entry),
        OneoffTargetNumber(coordinator, entry),
        CarChargeLimitNumber(coordinator, entry),
        IceConsumptionNumber(coordinator, entry),
        EvConsumptionNumber(coordinator, entry),
        FuelPriceNumber(coordinator, entry),
        SmartBatteryCapacityNumber(coordinator, entry),
        SmartPvEndNumber(coordinator, entry),
        OneoffDepartureSocNumber(coordinator, entry),
    ]
    for n in range(1, NUM_CYCLE_SLOTS + 1):
        entities.append(CycleSlotTargetSocNumber(coordinator, entry, n))
        entities.append(CycleSlotRhythmNumber(coordinator, entry, n))
    async_add_entities(entities)


class _BaseNumber(CoordinatorEntity[SpotChargeCoordinator], NumberEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry

    @property
    def device_info(self):
        return hub_device_info(self._entry.entry_id)


class BatteryCapacityNumber(_BaseNumber):
    """Used for plan sizing; overwritten automatically once the self-
    calibrating estimator (see capacity_estimator.py) has enough real
    charge sessions to trust — see sensor.py's diagnostic counterpart for
    how many samples that estimate is currently based on."""

    _attr_name = "Akkukapazität"
    _attr_icon = "mdi:battery-high"
    _attr_native_min_value = 1
    _attr_native_max_value = 200
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = "kWh"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_battery_capacity"

    @property
    def native_value(self) -> float:
        return self.coordinator.planner_state.battery_capacity_kwh

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_battery_capacity_kwh(value)


class ChargePowerNumber(_BaseNumber):
    """Used for plan sizing (how many slots are needed); overwritten
    automatically once the self-calibrating estimator has enough real
    charge sessions to trust, same as BatteryCapacityNumber — only kicks in
    when a charge-power sensor is configured, otherwise stays at the
    config default forever."""

    _attr_name = "Ladeleistung"
    _attr_icon = "mdi:ev-station"
    _attr_native_min_value = 0.1
    _attr_native_max_value = 50
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = "kW"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_charge_power"

    @property
    def native_value(self) -> float:
        return self.coordinator.planner_state.charge_power_kw

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_charge_power_kw(value)


class OpportunisticPercentileNumber(_BaseNumber):
    """How cheap a slot has to be, relative to the last few days of observed
    prices, before the planner grabs it as opportunistic top-up beyond the
    guaranteed target SoC (up to the configured car charge-limit entity).
    A slot counts as cheap at/below this percentile of that window. Lower =
    stricter (only the very cheapest slots top up); higher = more eager.
    Has no effect unless a car charge-limit entity is configured, and only
    once the price archive has ~2 days of history. The companion
    "Billig-Schwelle" sensor shows what this percentile currently works out
    to in ct/kWh."""

    _attr_name = "Billig-Schwelle (Perzentil)"
    _attr_icon = "mdi:sale"
    _attr_native_min_value = 1
    _attr_native_max_value = 50
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_opportunistic_percentile"

    @property
    def native_value(self) -> float:
        return self.coordinator.planner_state.opportunistic_percentile

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_opportunistic_percentile(value)


class ExpensivePercentileNumber(_BaseNumber):
    """Diagnostic mirror of OpportunisticPercentileNumber: how expensive a
    slot has to be, relative to the last few days of observed prices,
    before it counts as "expensive" — at/above this percentile of that
    window. Not read by the planner and doesn't actuate anything; purely
    informational (e.g. to decide whether right now is expensive enough
    that a free-charging credit elsewhere is worth more than home
    charging). The companion "Teuer-Schwelle" sensor shows what this
    percentile currently works out to in ct/kWh."""

    _attr_name = "Teuer-Schwelle (Perzentil)"
    _attr_icon = "mdi:cash-remove"
    _attr_native_min_value = 50
    _attr_native_max_value = 99
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_expensive_percentile"

    @property
    def native_value(self) -> float:
        return self.coordinator.planner_state.expensive_percentile

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_expensive_percentile(value)


class OneoffTargetNumber(_BaseNumber):
    """One-time charge target for today only. 0 = off. While set, the active
    cycle's target is raised to this value if its deadline falls today
    (otherwise charging aims at this value by end of day); it lapses on its
    own at midnight and never lowers a cycle's regular target."""

    _attr_name = "Einmaliges Ladeziel (heute)"
    _attr_icon = "mdi:battery-clock"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_oneoff_target"

    @property
    def native_value(self) -> float:
        return self.coordinator.oneoff_target_today(dt_util.now()) or 0

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_oneoff_target(value)


class CarChargeLimitNumber(_BaseNumber):
    """Input-box proxy for the car's own charge-limit entity (the ceiling for
    opportunistic top-up), so the limit can be typed instead of dragged on a
    slider that is easy to nudge by accident. Reads and writes the entity
    configured as the car charge-limit; unavailable without one."""

    _attr_name = "Auto-Ladelimit"
    _attr_icon = "mdi:battery-charging-80"
    _attr_native_min_value = 50
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_car_charge_limit"

    @property
    def _source(self) -> str | None:
        return self.coordinator._config.get(CONF_CAR_CHARGE_LIMIT_ENTITY)

    @property
    def available(self) -> bool:
        return self._source is not None and self.native_value is not None

    @property
    def native_value(self) -> float | None:
        if not self._source:
            return None
        state = self.hass.states.get(self._source)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    async def async_set_native_value(self, value: float) -> None:
        await self.hass.services.async_call(
            "number", "set_value", {"entity_id": self._source, "value": value}, blocking=True
        )
        await self.coordinator.async_request_refresh()


class IceConsumptionNumber(_BaseNumber):
    """Combustion car's fuel use, L/100 km — one half of the break-even
    comparison (see the "Verbrenner-Break-even" sensor)."""

    _attr_name = "Verbrenner-Verbrauch"
    _attr_icon = "mdi:gas-station"
    _attr_native_min_value = 1
    _attr_native_max_value = 30
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = "L/100 km"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_ice_consumption"

    @property
    def native_value(self) -> float:
        return self.coordinator.planner_state.ice_consumption_l_100km

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_ice_consumption_l_100km(value)


class EvConsumptionNumber(_BaseNumber):
    """EV's energy use *from the socket*, kWh/100 km (incl. charging losses)
    — the other half of the break-even comparison. Auto-overwritten from
    Home Assistant's own odometer + charge-energy statistics once an
    odometer and a charge-energy entity are configured and there's enough
    distance history; stays at the value set here otherwise."""

    _attr_name = "E-Auto-Verbrauch (ab Steckdose)"
    _attr_icon = "mdi:ev-station"
    _attr_native_min_value = 5
    _attr_native_max_value = 60
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "kWh/100 km"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_ev_consumption"

    @property
    def native_value(self) -> float:
        return self.coordinator.planner_state.ev_consumption_kwh_100km

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_ev_consumption_kwh_100km(value)


class FuelPriceNumber(_BaseNumber):
    """Fuel price (€/L) for the break-even comparison. Set it by hand; once
    a Tankerkönig API key is configured the integration overwrites it every
    hour with the cheapest local price (same idea as the auto-calibrated
    capacity / power numbers). The `quelle` attribute says whether the
    value showing is your manual one or the live one."""

    _attr_name = "Spritpreis"
    _attr_icon = "mdi:gas-station"
    _attr_native_min_value = 0.5
    _attr_native_max_value = 3
    _attr_native_step = 0.001
    _attr_native_unit_of_measurement = "€/L"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_fuel_price"

    @property
    def native_value(self) -> float:
        return self.coordinator.planner_state.fuel_price_eur_l

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_fuel_price_eur_l(value)

    @property
    def extra_state_attributes(self):
        c = (self.coordinator.data or {}).get("combustion") or {}
        return {
            "quelle": c.get("fuel_price_source"),
            "kraftstoffart": c.get("fuel_type"),
            "tankstelle": c.get("station"),
            "entfernung_km": c.get("station_distance_km"),
        }


class _SlotNumber(_BaseNumber):
    """Shared base for the per-slot number entities — see planner_state.py's
    slot model and coordinator.async_set_slot_field."""

    _attr_entity_category = EntityCategory.CONFIG
    _slot_field: str

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry, slot_no: int) -> None:
        super().__init__(coordinator, entry)
        self._slot_no = slot_no

    @property
    def native_value(self) -> float:
        return float(self.coordinator.get_slot(self._slot_no)[self._slot_field])

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_slot_field(self._slot_no, self._slot_field, value)


class CycleSlotTargetSocNumber(_SlotNumber):
    _slot_field = "target_soc"
    _attr_icon = "mdi:battery-charging-medium"
    _attr_native_min_value = 5
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry, slot_no: int) -> None:
        super().__init__(coordinator, entry, slot_no)
        self._attr_unique_id = f"{entry.entry_id}_slot_{slot_no}_target_soc"
        self._attr_name = f"Slot {slot_no} Ziel-SoC"


class CycleSlotRhythmNumber(_SlotNumber):
    _slot_field = "rhythm_days"
    _attr_icon = "mdi:calendar-sync"
    _attr_native_min_value = 0
    _attr_native_max_value = 30
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "Tage"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry, slot_no: int) -> None:
        super().__init__(coordinator, entry, slot_no)
        self._attr_unique_id = f"{entry.entry_id}_slot_{slot_no}_rhythm"
        self._attr_name = f"Slot {slot_no} Rhythmus (Tage)"

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_slot_field(self._slot_no, self._slot_field, int(value))


class SmartBatteryCapacityNumber(_BaseNumber):
    """Usable capacity of the HOME battery, used by "Überschuss Smart" to work
    out how much charge rate the battery needs to be full by the end of the
    PV day (see smart_surplus.battery_target_deadline_kw)."""

    _attr_name = "Smart: Hausakku-Kapazität"
    _attr_icon = "mdi:home-battery"
    _attr_native_min_value = 1
    _attr_native_max_value = 100
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "kWh"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_smart_home_battery_capacity"

    @property
    def native_value(self) -> float:
        return self.coordinator.planner_state.home_battery_capacity_kwh

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_home_battery_capacity_kwh(value)


class SmartPvEndNumber(_BaseNumber):
    """How long before sunset PV stops being useful (October ≈ 1.5 h). The
    home battery is reserved enough surplus to be full by then."""

    _attr_name = "Smart: PV-Ende vor Sonnenuntergang"
    _attr_icon = "mdi:weather-sunset-down"
    _attr_native_min_value = 0
    _attr_native_max_value = 5
    _attr_native_step = 0.25
    _attr_native_unit_of_measurement = "h"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_smart_pv_end_before_sunset"

    @property
    def native_value(self) -> float:
        return self.coordinator.planner_state.pv_end_before_sunset_h

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_pv_end_before_sunset_h(value)


class OneoffDepartureSocNumber(_BaseNumber):
    """Minimum state of charge wanted at the one-off departure."""

    _attr_name = "Einmalige Abfahrt Mindest-SoC"
    _attr_icon = "mdi:battery-arrow-up"
    _attr_native_min_value = 5
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: SpotChargeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_oneoff_departure_soc"

    @property
    def native_value(self) -> float:
        return float(self.coordinator.planner_state.oneoff_departure.get("target_soc") or 80.0)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_oneoff_departure("target_soc", value)
