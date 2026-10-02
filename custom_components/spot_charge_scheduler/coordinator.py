"""DataUpdateCoordinator: reads vehicle/price state, (re)computes the charge
plan, and actuates the configured charge switch accordingly.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import (
    capacity_estimator,
    consumption_estimator,
    fuel_source,
    price_baseline,
    readiness,
    schedule,
    smart_history,
    smart_surplus,
)
from .const import (
    CONF_BATTERY_CAPACITY_KWH_DEFAULT,
    CONF_CAR_CHARGE_LIMIT_ENTITY,
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
    CONTROLLER_SMART,
    CONF_PRICE_SOURCE,
    CONF_SOC_SENSOR,
    CONF_TANKERKOENIG_API_KEY,
    CONF_TIBBER_HOME_NICKNAME,
    DEFAULT_FUEL_RADIUS_KM,
    DEFAULT_FUEL_TYPE,
    DOMAIN,
    EV_CONSUMPTION_RECALC_INTERVAL_SECONDS,
    FUEL_FETCH_MIN_INTERVAL_SECONDS,
    FUEL_FETCH_RETRY_AFTER_FAILURE_SECONDS,
    PRICE_FETCH_MIN_INTERVAL_SECONDS,
    PRICE_FETCH_RETRY_AFTER_FAILURE_SECONDS,
    UPDATE_INTERVAL_SECONDS,
)
from .fuel_source import FuelPrice
from .planner import ChargePlan, compute_plan
from .planner_state import PlannerState
from .price_source import SLOT_DURATION, PricePoint, get_price_provider
from .schedule import Occurrence

_LOGGER = logging.getLogger(__name__)


def _get_float_state(hass: HomeAssistant, entity_id: str | None) -> float | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable"):
        return None
    try:
        return float(state.state)
    except ValueError:
        return None


def _get_power_kw(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """A power sensor's value in kW (converts W; other units assumed kW)."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable"):
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    if state.attributes.get("unit_of_measurement") == "W":
        value /= 1000.0
    return value


def _get_bool_state(hass: HomeAssistant, entity_id: str | None) -> bool | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable"):
        return None
    return state.state == "on"


def _get_is_home(hass: HomeAssistant, tracker_entity: str | None, zone_entity: str | None) -> bool | None:
    """None when location gating isn't configured (both fields optional and
    only meaningful together) — callers must not gate on that as False."""
    if not tracker_entity or not zone_entity:
        return None
    tracker_state = hass.states.get(tracker_entity)
    if tracker_state is None or tracker_state.state in ("unknown", "unavailable"):
        return None
    # A device_tracker's state is the object_id of whichever zone it's
    # currently inside (e.g. "home" for zone.home), or "not_home" — the
    # standard HA zone-matching convention, not something this integration
    # computes itself.
    zone_object_id = zone_entity.split(".", 1)[1]
    return tracker_state.state == zone_object_id


class SpotChargeCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, config: dict, entry_id: str) -> None:
        super().__init__(
            hass, _LOGGER, name=DOMAIN, update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS)
        )
        self._config = config
        self.planner_state = PlannerState(
            hass,
            entry_id,
            default_capacity_kwh=config[CONF_BATTERY_CAPACITY_KWH_DEFAULT],
            default_charge_power_kw=config[CONF_CHARGE_POWER_KW],
        )
        self._price_provider = get_price_provider(
            config[CONF_PRICE_SOURCE], config[CONF_TIBBER_HOME_NICKNAME]
        )
        self._cached_prices: list[PricePoint] = []
        self._last_price_fetch: datetime | None = None
        self._was_charging: bool | None = None
        # Tracks the active occurrence's start so a change is detected even
        # when nobody edited anything — e.g. the previous target got
        # abandoned (MISSED_DEADLINE_GRACE_HOURS) or simply completed and
        # the next one in line took over. Without this, the cache from the
        # old target's fetch just sits there not covering the new one, and
        # the 15-min fetch-spacing throttle (meant to avoid hammering the
        # service while genuinely waiting on the SAME target) ends up
        # delaying the fetch this actually-new target needs right now.
        self._last_active_target_dt: datetime | None = None
        self._price_fetch_retry_interval: float = PRICE_FETCH_MIN_INTERVAL_SECONDS
        # Combustion-engine comparison (all optional / degrades safe).
        self._fuel_price: FuelPrice | None = None
        self._last_fuel_fetch: datetime | None = None
        self._fuel_fetch_retry_interval: float = FUEL_FETCH_MIN_INTERVAL_SECONDS
        self._last_ev_recalc: datetime | None = None
        # Charge-switch failure backoff (see _actuate_switch): consecutive
        # failures, earliest next attempt, and the last error for display.
        self._act_fail_count = 0
        self._act_retry_at: datetime | None = None
        self.actuation_error: dict | None = None
        self.last_decision: dict | None = None
        # "Überschuss Smart" (see smart_surplus.py). Samples are kept in
        # memory only — after a restart the first decision waits until
        # MIN_WINDOW_COVERAGE_MINUTES of data exist again.
        self._smart_samples: list[smart_surplus.Sample] = []
        self._smart_last_slot: tuple | None = None
        self._smart_low_windows = 0
        self._smart_applied = True  # nothing pending until a decision exists
        self._smart_retry_at: datetime | None = None
        self._smart_fail_count = 0
        self._smart_last_plugged: bool | None = None
        self.smart_decision: dict | None = None
        self.smart_action: str | None = None
        self.smart_error: dict | None = None
        self._smart_last_change: datetime | None = None
        self._smart_last_wallbox_kw: float = 0.0
        self._smart_last_bat_soc: float | None = None
        self.smart_fast: dict | None = None
        self._smart_backfilled = False
        self._smart_last_wb_mode: str | None = None
        self._smart_is_charging: bool | None = None
        # After a plug-in / mode-change decision the car may still start
        # charging by itself (wallbox unregulated): see _smart_enforce_idle.
        self._smart_forced_at: datetime | None = None
        self._smart_autostart_handled = True
        # Vehicle-API commands sent by Smart per local day (Fleet API budget).
        self._smart_cmd_date: str | None = None
        self._smart_cmd_count = 0

    async def async_setup(self) -> None:
        await self.planner_state.async_load()

    async def async_flush_state(self) -> None:
        await self.planner_state.async_save_now()

    # --- per-slot setters (called by the slot entities; bypass the
    #     config-entry reload path so editing a slot never interrupts a
    #     running charge session) ---

    def get_slot(self, slot_no: int) -> dict:
        """Slots are numbered 1..NUM_CYCLE_SLOTS; the list is always exactly
        that long (planner_state guarantees it)."""
        return self.planner_state.cycle_slots[slot_no - 1]

    async def async_set_slot_field(self, slot_no: int, field: str, value) -> None:
        slot = self.get_slot(slot_no)
        slot[field] = value
        # Re-base the N-day rhythm phase to "now" when a slot is switched
        # on, or its rhythm changes — that's what "ab jetzt alle N Tage"
        # (and resuming after a holiday) is supposed to mean. Editing the
        # name / target SoC / time-of-day leaves the phase alone.
        if (field == "enabled" and value) or (
            field == "rhythm_days" and slot.get("enabled")
        ):
            slot["anchor"] = dt_util.now().isoformat()
        self._invalidate_price_cache()
        self.planner_state.async_save()
        await self.async_request_refresh()

    def _invalidate_price_cache(self) -> None:
        # Any change to the schedule can change which deadline is active,
        # which changes the price window that needs fetching.
        self._cached_prices = []
        self._last_price_fetch = None

    async def async_set_battery_capacity_kwh(self, value: float) -> None:
        self.planner_state.battery_capacity_kwh = value
        self.planner_state.async_save()
        await self.async_request_refresh()

    async def async_set_charge_power_kw(self, value: float) -> None:
        self.planner_state.charge_power_kw = value
        self.planner_state.async_save()
        await self.async_request_refresh()

    async def async_set_opportunistic_percentile(self, value: float) -> None:
        self.planner_state.opportunistic_percentile = value
        self.planner_state.async_save()
        await self.async_request_refresh()

    async def async_set_expensive_percentile(self, value: float) -> None:
        self.planner_state.expensive_percentile = value
        self.planner_state.async_save()
        await self.async_request_refresh()

    async def async_set_oneoff_target(self, value: float) -> None:
        """0 (or below) clears the override; anything else applies to today."""
        if value <= 0:
            self.planner_state.oneoff_target_soc = None
            self.planner_state.oneoff_date = None
        else:
            self.planner_state.oneoff_target_soc = value
            self.planner_state.oneoff_date = dt_util.now().date().isoformat()
        self._invalidate_price_cache()
        self.planner_state.async_save()
        await self.async_request_refresh()

    def is_paused_by_mode(self) -> bool:
        """True while the configured pause-mode sensor shows the pause value
        (another controller owns the wallbox). Unknown/unavailable or an
        unconfigured sensor never pauses."""
        entity = self._config.get(CONF_PAUSE_MODE_SENSOR)
        if not entity:
            return False
        state = self.hass.states.get(entity)
        if state is None or state.state in ("unknown", "unavailable"):
            return False
        value = self._config.get(CONF_PAUSE_MODE_VALUE) or DEFAULT_PAUSE_MODE_VALUE
        return state.state == value

    def oneoff_target_today(self, now: datetime) -> float | None:
        st = self.planner_state
        if st.oneoff_target_soc is None or st.oneoff_date != now.date().isoformat():
            return None
        return st.oneoff_target_soc

    def _apply_oneoff(
        self, active: Occurrence | None, now: datetime, current_soc: float | None
    ) -> Occurrence | None:
        """Fold the one-time "today only" target into the active occurrence.
        A cycle whose deadline falls today gets its target raised (never
        lowered); otherwise a synthetic end-of-day deadline stands in until
        the target is met. Lapses at the date change."""
        override = self.oneoff_target_today(now)
        if override is None:
            if self.planner_state.oneoff_target_soc is not None:
                self.planner_state.oneoff_target_soc = None
                self.planner_state.oneoff_date = None
                self.planner_state.async_save()
            return active
        end_of_day = now.replace(hour=23, minute=45, second=0, microsecond=0)
        if active is not None and active.start <= end_of_day:
            if override > active.target_soc:
                return replace(active, target_soc=override, name=f"{active.name} (einmalig {override:.0f} %)")
            return active
        if current_soc is not None and current_soc >= override:
            return active
        return Occurrence(slot=0, start=max(end_of_day, now), target_soc=override, name="Einmalig heute", rhythm_days=0)

    async def async_set_ice_consumption_l_100km(self, value: float) -> None:
        self.planner_state.ice_consumption_l_100km = value
        self.planner_state.async_save()
        await self.async_request_refresh()

    async def async_set_ev_consumption_kwh_100km(self, value: float) -> None:
        self.planner_state.ev_consumption_kwh_100km = value
        self.planner_state.async_save()
        await self.async_request_refresh()

    async def async_set_fuel_price_eur_l(self, value: float) -> None:
        self.planner_state.fuel_price_eur_l = value
        self.planner_state.async_save()
        await self.async_request_refresh()

    async def async_set_controller_mode(self, mode: str) -> None:
        self.planner_state.controller_mode = mode
        self.planner_state.async_save()
        if mode == CONTROLLER_SMART:
            # Re-evaluate right away instead of waiting for the next quarter.
            self._smart_last_slot = None
        await self.async_request_refresh()

    @property
    def smart_active(self) -> bool:
        return self.planner_state.controller_mode == CONTROLLER_SMART

    async def async_set_master_switch(self, value: bool) -> None:
        self.planner_state.master_switch_on = value
        self.planner_state.async_save()
        if not value:
            # A one-time action tied to the explicit off transition, not
            # ongoing per-cycle enforcement (see _actuate_switch's hands-off
            # policy while master is off) — otherwise turning "Lademodus
            # aktiv" off would silently leave an already-running managed
            # charge session going until it happened to hit a stop
            # condition on its own.
            await self.hass.services.async_call(
                "switch", "turn_off", {"entity_id": self._config[CONF_CHARGE_SWITCH]}, blocking=True
            )
        await self.async_request_refresh()

    # --- main cycle ---

    async def _async_update_data(self) -> dict:
        now = dt_util.now()

        current_soc = _get_float_state(self.hass, self._config[CONF_SOC_SENSOR])
        is_charging = _get_bool_state(self.hass, self._config.get(CONF_CHARGING_STATUS_SENSOR))
        plugged_in = _get_bool_state(self.hass, self._config.get(CONF_PLUGGED_IN_SENSOR))
        energy_added = _get_float_state(self.hass, self._config.get(CONF_ENERGY_ADDED_SENSOR))
        current_power_kw = _get_float_state(self.hass, self._config.get(CONF_CHARGE_POWER_SENSOR))
        # The car's own target charge limit (e.g. number.model_3_charge_limit),
        # if the user pointed us at it — the ceiling for opportunistic top-up.
        # None (unset or unavailable) simply disables the top-up; the
        # guaranteed target is unaffected.
        car_charge_limit = _get_float_state(self.hass, self._config.get(CONF_CAR_CHARGE_LIMIT_ENTITY))
        is_home = _get_is_home(
            self.hass, self._config.get(CONF_LOCATION_TRACKER_ENTITY), self._config.get(CONF_HOME_ZONE_ENTITY)
        )

        await self._smart_backfill_once(now)
        smart_ready = self._smart_cycle_inputs(now, is_charging, plugged_in)

        if is_charging is not None:
            capacity_estimator.process_charging_edge(
                self.planner_state, self._was_charging, is_charging, current_soc, energy_added, current_power_kw
            )
            self._was_charging = is_charging

        # Which slot's occurrence we're planning/charging toward right now
        # — see schedule.find_active_occurrence for how several independent
        # slots interleave without any explicit "advance to next" step;
        # it's always freshly derived.
        active: Occurrence | None = schedule.find_active_occurrence(
            self.planner_state.cycle_slots, now, current_soc
        )
        active = self._apply_oneoff(active, now, current_soc)
        target_dt = active.start if active else None
        target_soc = active.target_soc if active else None

        if target_dt != self._last_active_target_dt:
            self._invalidate_price_cache()
            self._last_active_target_dt = target_dt

        if target_dt is not None:
            await self._maybe_fetch_prices(now, target_dt)

        cheap_threshold = price_baseline.cheap_price_threshold(
            self.planner_state.price_history, now, self.planner_state.opportunistic_percentile
        )
        # Same percentile function, opposite end of the distribution — see
        # the "Teuer-Schwelle" sensor. Diagnostic only, not read by the plan.
        expensive_threshold = price_baseline.cheap_price_threshold(
            self.planner_state.price_history, now, self.planner_state.expensive_percentile
        )

        plan = self._compute_plan(
            now, target_dt, target_soc, current_soc, car_charge_limit, cheap_threshold
        )
        self.planner_state.plan = _plan_to_dict(plan)

        defer_for_data = self._should_defer_for_data(now, target_dt, plan)

        if self.smart_active:
            await self._smart_apply(now, plugged_in, is_home, current_soc, car_charge_limit)
            await self._smart_enforce_idle(now, plugged_in, is_home)
            if smart_surplus.FAST_LOOP_ENABLED:
                await self._smart_fast_cycle(now, plugged_in, is_home)
        else:
            await self._actuate_switch(
                plan, current_soc, target_soc, plugged_in, is_home, defer_for_data, now, target_dt
            )

        await self._maybe_fetch_fuel_price(now)
        await self._maybe_recalc_ev_consumption(now)
        combustion = self._combustion_comparison(now)

        return {
            "current_soc": current_soc,
            "is_charging": is_charging,
            "plugged_in": plugged_in,
            "is_home": is_home,
            "active_occurrence": active,
            "target_soc": target_soc,
            "target_datetime": target_dt,
            "car_charge_limit": car_charge_limit,
            "cheap_price_threshold": cheap_threshold,
            "expensive_price_threshold": expensive_threshold,
            "defer_for_data": defer_for_data,
            "battery_capacity_kwh": self.planner_state.battery_capacity_kwh,
            "capacity_sample_count": len(self.planner_state.capacity_samples),
            "charge_power_kw": self.planner_state.charge_power_kw,
            "power_sample_count": len(self.planner_state.power_samples),
            "master_switch_on": self.planner_state.master_switch_on,
            "paused_by_mode": self.is_paused_by_mode() and not self.smart_active,
            "controller_mode": self.planner_state.controller_mode,
            "smart_decision": self.smart_decision,
            "smart_action": self.smart_action,
            "smart_error": self.smart_error,
            "smart_fast": self.smart_fast,
            "smart_commands_today": (
                self._smart_cmd_count if self._smart_cmd_date == now.date().isoformat() else 0
            ),
            "smart_inputs_ready": smart_ready,
            "actuation_error": self.actuation_error,
            "last_decision": self.last_decision,
            "plan": plan,
            "combustion": combustion,
        }

    # --- "Überschuss Smart" ---

    async def _smart_backfill_once(self, now: datetime) -> None:
        """After a restart, rebuild the 30-min sample buffer from the
        recorder once, so decisions don't wait for fresh sampling."""
        if self._smart_backfilled:
            return
        self._smart_backfilled = True
        pv_entity = self._config.get(CONF_PV_POWER_SENSOR)
        load_entity = self._config.get(CONF_HOUSE_LOAD_SENSOR)
        if not pv_entity or not load_entity:
            return
        history = await smart_history.async_backfill(
            self.hass, pv_entity, load_entity, self._config.get(CONF_CHARGE_POWER_SENSOR), now
        )
        if history and not self._smart_samples:
            self._smart_samples = history

    def _smart_cycle_inputs(
        self, now: datetime, is_charging: bool | None, plugged_in: bool | None
    ) -> bool:
        """Record one PV/base-load sample and, once per wall-clock quarter
        hour (or right after the car gets plugged in), take a new decision.
        Runs in both controller modes so the recommendation sensors can be
        compared with reality before switching to Smart; only
        _smart_apply ever writes to the car. Returns False when a needed
        input sensor isn't configured / readable."""
        cfg = self._config
        pv = _get_power_kw(self.hass, cfg.get(CONF_PV_POWER_SENSOR))
        load = _get_power_kw(self.hass, cfg.get(CONF_HOUSE_LOAD_SENSOR))
        bat_soc = _get_float_state(self.hass, cfg.get(CONF_HOME_BATTERY_SOC_SENSOR))
        wallbox = _get_power_kw(self.hass, cfg.get(CONF_CHARGE_POWER_SENSOR))
        if wallbox is None and is_charging is False:
            wallbox = 0.0  # a sleeping car's power sensor can read unavailable
        if pv is None or load is None or bat_soc is None or wallbox is None:
            return False

        self._smart_samples = smart_surplus.add_sample(
            self._smart_samples, smart_surplus.Sample(now, pv, load - wallbox, wallbox)
        )
        self._smart_last_wallbox_kw = wallbox
        self._smart_last_bat_soc = bat_soc

        # Only a real False -> True edge counts (not the first reading after
        # an HA restart, which would otherwise look like a fresh plug-in).
        just_plugged = plugged_in is True and self._smart_last_plugged is False
        if plugged_in is not None:
            self._smart_last_plugged = plugged_in
        self._smart_is_charging = is_charging
        # A wallbox mode change (e.g. PV Power -> Normal Charge) also
        # warrants an immediate decision: the 30-min buffer is always there.
        mode_state = self.hass.states.get(cfg.get(CONF_PAUSE_MODE_SENSOR) or "")
        wb_mode = mode_state.state if mode_state and mode_state.state not in ("unknown", "unavailable") else None
        mode_changed = (
            wb_mode is not None and self._smart_last_wb_mode is not None and wb_mode != self._smart_last_wb_mode
        )
        if wb_mode is not None:
            self._smart_last_wb_mode = wb_mode
        forced = just_plugged or mode_changed
        slot = smart_surplus.decision_slot(now)
        if slot == self._smart_last_slot and not forced:
            return True

        # Live TeslaMate "is charging" beats the slowly polled Fleet switch:
        # a car that starts charging by itself on plug-in must be seen at once.
        charging_now = is_charging if is_charging is not None else self._charge_switch_on()
        decision = smart_surplus.decide(
            self._smart_samples, now, bat_soc, charging_now, self._smart_low_windows, forced
        )
        if decision is None:
            return True  # not enough data yet; retried next cycle
        if forced:
            self._smart_forced_at = now
            self._smart_autostart_handled = False
        self._smart_last_slot = slot
        self._smart_low_windows = decision["fenster_unter_minimum"]
        self.smart_decision = decision
        self._smart_applied = False
        self._smart_retry_at = None
        return True

    def _smart_count_command(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self._smart_cmd_date != today:
            self._smart_cmd_date = today
            self._smart_cmd_count = 0
        self._smart_cmd_count += 1

    def _charge_switch_on(self) -> bool:
        state = self.hass.states.get(self._config[CONF_CHARGE_SWITCH])
        return state is not None and state.state == "on"

    def _smart_charge_active(self) -> bool:
        """Charging per the Fleet switch OR the live TeslaMate sensor."""
        return self._charge_switch_on() or self._smart_is_charging is True

    async def _smart_enforce_idle(
        self, now: datetime, plugged_in: bool | None, is_home: bool | None
    ) -> None:
        """A plug-in decision of 0 A can be followed by the car starting to
        charge on its own seconds later (unregulated wallbox, live-observed
        2026-10-02): stop it once, but only within a few minutes of that
        decision — a deliberate manual start later on is not fought."""
        d = self.smart_decision
        if (
            d is None or not self._smart_applied or d["ampere"] != 0
            or self._smart_autostart_handled or self._smart_forced_at is None
            or (now - self._smart_forced_at).total_seconds() > 300
            or self._smart_is_charging is not True
            or not self.planner_state.master_switch_on
            or plugged_in is False or is_home is False
            or (self._smart_retry_at is not None and now < self._smart_retry_at)
        ):
            return
        try:
            await self.hass.services.async_call(
                "switch", "turn_off", {"entity_id": self._config[CONF_CHARGE_SWITCH]}, blocking=True
            )
        except Exception as err:  # noqa: BLE001
            self._smart_fail_count += 1
            delay = (1, 2, 5, 10)[min(self._smart_fail_count, 4) - 1]
            self._smart_retry_at = now + timedelta(minutes=delay)
            self.smart_error = {
                "zeit": now.isoformat(), "ziel_ampere": 0,
                "fehler": f"{type(err).__name__}: {err}".strip(": "),
                "versuche": self._smart_fail_count,
                "naechster_versuch": self._smart_retry_at.isoformat(),
            }
            _LOGGER.warning("Smart: could not stop the auto-started charge", exc_info=self._smart_fail_count == 1)
            return
        self._smart_count_command(now)
        self._smart_autostart_handled = True
        self._smart_fail_count = 0
        self._smart_retry_at = None
        self.smart_error = None
        self.smart_action = "autostart_gestoppt"
        _LOGGER.info("Smart: stopped a charge the car started by itself after plug-in")

    async def _smart_apply(
        self,
        now: datetime,
        plugged_in: bool | None,
        is_home: bool | None,
        current_soc: float | None,
        car_charge_limit: float | None,
    ) -> None:
        """Carry out the latest Smart decision ONCE (a manual change of the
        current between two decisions is therefore never fought), retrying
        with backoff if the vehicle API fails."""
        decision = self.smart_decision
        if decision is None or self._smart_applied:
            return
        if not self.planner_state.master_switch_on:
            self.smart_action = "master_aus"
            return
        if self._smart_retry_at is not None and now < self._smart_retry_at:
            return
        if plugged_in is False or is_home is False:
            self._smart_applied = True
            self.smart_action = "nicht_angesteckt" if plugged_in is False else "nicht_zuhause"
            return
        if (
            car_charge_limit is not None
            and current_soc is not None
            and current_soc >= car_charge_limit
        ):
            self._smart_applied = True
            self.smart_action = "ladelimit_erreicht"
            return
        current_entity = self._config.get(CONF_CHARGE_CURRENT_ENTITY)
        if not current_entity:
            self._smart_applied = True
            self.smart_action = "keine_ladestrom_entitaet"
            return

        amps = decision["ampere"]
        switch_entity = self._config[CONF_CHARGE_SWITCH]
        is_on = self._smart_charge_active()
        try:
            if amps == 0:
                if is_on:
                    await self.hass.services.async_call(
                        "switch", "turn_off", {"entity_id": switch_entity}, blocking=True
                    )
                    self._smart_count_command(now)
                    self.smart_action = "gestoppt"
                else:
                    self.smart_action = "bleibt_aus"
            else:
                current = _get_float_state(self.hass, current_entity)
                if current is None or round(current) != amps:
                    await self.hass.services.async_call(
                        "number", "set_value", {"entity_id": current_entity, "value": amps}, blocking=True
                    )
                    self._smart_count_command(now)
                    self.smart_action = f"ampere_auf_{amps}"
                else:
                    self.smart_action = f"ampere_unveraendert_{amps}"
                if not is_on:
                    await self.hass.services.async_call(
                        "switch", "turn_on", {"entity_id": switch_entity}, blocking=True
                    )
                    self._smart_count_command(now)
                    self.smart_action += "_gestartet"
        except Exception as err:  # noqa: BLE001 - vehicle API hiccups must not crash the cycle
            self._smart_fail_count += 1
            delay = (1, 2, 5, 10)[min(self._smart_fail_count, 4) - 1]
            self._smart_retry_at = now + timedelta(minutes=delay)
            self.smart_error = {
                "zeit": now.isoformat(),
                "ziel_ampere": amps,
                "fehler": f"{type(err).__name__}: {err}".strip(": "),
                "versuche": self._smart_fail_count,
                "naechster_versuch": self._smart_retry_at.isoformat(),
            }
            _LOGGER.warning(
                "Smart: could not apply %s A (attempt %d); retrying in %d min",
                amps, self._smart_fail_count, delay, exc_info=self._smart_fail_count == 1,
            )
            return
        _LOGGER.info("Smart: %s (decision %s)", self.smart_action, decision)
        if self.smart_action and "unveraendert" not in self.smart_action and self.smart_action != "bleibt_aus":
            self._smart_last_change = now
        self._smart_applied = True
        self._smart_fail_count = 0
        self._smart_retry_at = None
        self.smart_error = None

    async def _smart_fast_cycle(
        self, now: datetime, plugged_in: bool | None, is_home: bool | None
    ) -> None:
        """Per-cycle fast correction (see smart_surplus.fast_adjust). Skipped
        while a slow decision is still pending/retrying, without master
        switch, or when the car isn't charging. The current actually drawn is
        derived from the live wallbox power, not from the (slowly polled)
        current entity."""
        if not self._smart_applied or not self.planner_state.master_switch_on:
            return
        if plugged_in is False or is_home is False:
            return
        if self._smart_retry_at is not None and now < self._smart_retry_at:
            return
        current_entity = self._config.get(CONF_CHARGE_CURRENT_ENTITY)
        if not current_entity or self._smart_last_bat_soc is None or not self._charge_switch_on():
            return
        drawn_amps = round(self._smart_last_wallbox_kw * 1000.0 / (smart_surplus.VOLTAGE * smart_surplus.PHASES))
        if drawn_amps < 1:
            return
        adj = smart_surplus.fast_adjust(
            self._smart_samples, now, self._smart_last_bat_soc, drawn_amps, self._smart_last_change
        )
        if adj is None:
            return
        new = adj["amps"]
        try:
            if new == 0:
                await self.hass.services.async_call(
                    "switch", "turn_off", {"entity_id": self._config[CONF_CHARGE_SWITCH]}, blocking=True
                )
            else:
                await self.hass.services.async_call(
                    "number", "set_value", {"entity_id": current_entity, "value": new}, blocking=True
                )
        except Exception as err:  # noqa: BLE001
            self._smart_fail_count += 1
            delay = (1, 2, 5, 10)[min(self._smart_fail_count, 4) - 1]
            self._smart_retry_at = now + timedelta(minutes=delay)
            self.smart_error = {
                "zeit": now.isoformat(), "ziel_ampere": new,
                "fehler": f"{type(err).__name__}: {err}".strip(": "),
                "versuche": self._smart_fail_count,
                "naechster_versuch": self._smart_retry_at.isoformat(),
            }
            _LOGGER.warning("Smart fast: could not apply %s A", new, exc_info=self._smart_fail_count == 1)
            return
        self._smart_last_change = now
        self._smart_fail_count = 0
        self._smart_retry_at = None
        self.smart_error = None
        self.smart_fast = {
            "zeit": now.isoformat(), "von_ampere": drawn_amps, "auf_ampere": new,
            "grund": adj["grund"], "bilanz_kw": adj["bilanz_kw"],
        }
        self.smart_action = f"{adj['grund']}_{drawn_amps}_auf_{new}"
        _LOGGER.info("Smart fast: %s A -> %s A (%s, balance %s kW)", drawn_amps, new, adj["grund"], adj["bilanz_kw"])

    # --- combustion-engine comparison ---

    def _current_electricity_price_eur_kwh(self, now: datetime) -> float | None:
        """The price we're paying right now: the cached 15-min slot covering
        `now` if we have one, else an optional live spot-price sensor."""
        for p in self._cached_prices:
            if p.start <= now < p.start + SLOT_DURATION:
                return p.price
        return _get_float_state(self.hass, self._config.get(CONF_CURRENT_PRICE_SENSOR))

    async def _maybe_fetch_fuel_price(self, now: datetime) -> None:
        api_key = self._config.get(CONF_TANKERKOENIG_API_KEY)
        if not api_key:
            self._fuel_price = None
            return
        if self._last_fuel_fetch is not None and (
            now - self._last_fuel_fetch
        ).total_seconds() < self._fuel_fetch_retry_interval:
            return
        try:
            self._fuel_price = await fuel_source.async_get_cheapest_fuel_price(
                self.hass,
                api_key,
                self.hass.config.latitude,
                self.hass.config.longitude,
                float(self._config.get(CONF_FUEL_RADIUS_KM) or DEFAULT_FUEL_RADIUS_KM),
                str(self._config.get(CONF_FUEL_TYPE) or DEFAULT_FUEL_TYPE),
            )
            if self._fuel_price is not None:
                # Self-calibrating: the "Spritpreis" number now tracks the
                # live cheapest-local price (still hand-editable; overwritten
                # again on the next hourly fetch).
                self.planner_state.fuel_price_eur_l = self._fuel_price.price_eur_per_l
                self.planner_state.async_save()
            self._fuel_fetch_retry_interval = FUEL_FETCH_MIN_INTERVAL_SECONDS
        except Exception:  # noqa: BLE001 - a fuel-price hiccup must not crash the cycle
            _LOGGER.warning("Tankerkönig fuel-price fetch failed", exc_info=True)
            self._fuel_fetch_retry_interval = FUEL_FETCH_RETRY_AFTER_FAILURE_SECONDS
        finally:
            self._last_fuel_fetch = now

    async def _maybe_recalc_ev_consumption(self, now: datetime) -> None:
        if self._last_ev_recalc is not None and (
            now - self._last_ev_recalc
        ).total_seconds() < EV_CONSUMPTION_RECALC_INTERVAL_SECONDS:
            return
        self._last_ev_recalc = now
        estimate = await consumption_estimator.async_estimate_ev_consumption_kwh_100km(
            self.hass,
            self._config.get(CONF_ODOMETER_ENTITY),
            self._config.get(CONF_CHARGE_ENERGY_ENTITY),
        )
        if estimate is not None and estimate > 0:
            self.planner_state.ev_consumption_kwh_100km = estimate
            self.planner_state.async_save()

    def _combustion_comparison(self, now: datetime) -> dict | None:
        """Break-even electricity price: at/above how many €/kWh would the
        combustion car cost the same per km. Always computable — the
        "Spritpreis" number holds either a live Tankerkönig price or the
        hand-set one; `fuel_price_source` says which."""
        ev_kwh = self.planner_state.ev_consumption_kwh_100km
        ice_l = self.planner_state.ice_consumption_l_100km
        if ev_kwh <= 0:
            return None

        # The "Spritpreis" number always holds the price to use — either
        # hand-set, or last written by a live fetch. `fuel_price_source`
        # just says which it currently is.
        fuel_eur_l = self.planner_state.fuel_price_eur_l
        fp = self._fuel_price
        if fp is not None:
            source = "tankerkoenig"
            station: str | None = fp.station
            distance: float | None = fp.distance_km
            fuel_type = fp.fuel_type
        else:
            source = "manuell"
            station = None
            distance = None
            fuel_type = str(self._config.get(CONF_FUEL_TYPE) or DEFAULT_FUEL_TYPE)

        ice_eur_100km = ice_l * fuel_eur_l
        break_even_eur_kwh = ice_eur_100km / ev_kwh
        cur = self._current_electricity_price_eur_kwh(now)
        ev_eur_100km_now = ev_kwh * cur if cur is not None else None
        cheaper_now = None
        if cur is not None:
            cheaper_now = "eauto" if cur < break_even_eur_kwh else "verbrenner"
        return {
            "break_even_eur_kwh": round(break_even_eur_kwh, 4),
            "fuel_price_eur_l": round(fuel_eur_l, 3),
            "fuel_price_source": source,
            "fuel_type": fuel_type,
            "station": station,
            "station_distance_km": distance,
            "ice_l_100km": ice_l,
            "ev_kwh_100km": ev_kwh,
            "ice_eur_100km": round(ice_eur_100km, 2),
            "current_price_eur_kwh": cur,
            "ev_eur_100km_now": round(ev_eur_100km_now, 2) if ev_eur_100km_now is not None else None,
            "cheaper_now": cheaper_now,
        }

    async def _maybe_fetch_prices(self, now: datetime, target_dt: datetime) -> None:
        cache_covers_target = bool(self._cached_prices) and (
            self._cached_prices[-1].start + SLOT_DURATION >= target_dt
        )
        if cache_covers_target:
            return
        if self._last_price_fetch is not None and (
            now - self._last_price_fetch
        ).total_seconds() < self._price_fetch_retry_interval:
            return
        try:
            # Start at the beginning of the running 15-min slot, not at `now`:
            # a price service returning slots starting at/after `now` omits
            # the slot that is running right now, which would then never be
            # in the plan (and the switch never turned on) until the next slot.
            slot_start = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
            self._cached_prices = await self._price_provider.async_get_prices(self.hass, slot_start, target_dt)
            self.planner_state.price_history = price_baseline.merge_observations(
                self.planner_state.price_history, self._cached_prices, now
            )
            self.planner_state.async_save()
            self._price_fetch_retry_interval = PRICE_FETCH_MIN_INTERVAL_SECONDS
        except Exception:  # noqa: BLE001 - a failed fetch must not crash the cycle
            _LOGGER.exception("Failed to fetch prices")
            # A transient failure (e.g. another integration's service
            # briefly unavailable right after a reload — live-observed)
            # shouldn't cost the full success-case spacing before trying
            # again; that left the plan sitting on zero data for minutes
            # after the underlying problem was already gone.
            self._price_fetch_retry_interval = PRICE_FETCH_RETRY_AFTER_FAILURE_SECONDS
        finally:
            self._last_price_fetch = now

    def _should_defer_for_data(
        self, now: datetime, target_dt: datetime | None, plan: ChargePlan
    ) -> bool:
        """See readiness.py. Only meaningful with an actual target and a
        real plan (plan.required_slot_count/target_reachable are None when
        there's no active occurrence at all)."""
        if target_dt is None or plan.target_reachable is None or plan.required_slot_count == 0:
            return False
        data_covers_target = bool(self._cached_prices) and (
            self._cached_prices[-1].start + SLOT_DURATION >= target_dt
        )
        eligible_prices = [p.price for p in self._cached_prices if now <= p.start < target_dt]
        best_known_price = min(eligible_prices) if eligible_prices else None
        historical_typical_price = price_baseline.typical_price_for_time_of_day(
            self.planner_state.price_history, now
        )
        return readiness.should_defer_for_better_data(
            now, target_dt, plan.required_slot_count, data_covers_target,
            best_known_price, historical_typical_price,
            has_eligible_prices=plan.available_slot_count > 0,
        )

    def _compute_plan(
        self,
        now: datetime,
        target_dt: datetime | None,
        target_soc: float | None,
        current_soc: float | None,
        car_charge_limit: float | None,
        cheap_price_threshold: float | None,
    ) -> ChargePlan:
        if target_dt is None or target_soc is None or current_soc is None:
            return ChargePlan(
                slots=[], estimated_cost_eur=0.0, estimated_completion=None,
                target_reachable=None, required_slot_count=0, available_slot_count=0,
            )
        return compute_plan(
            now=now,
            target_datetime=target_dt,
            target_soc=target_soc,
            current_soc=current_soc,
            battery_capacity_kwh=self.planner_state.battery_capacity_kwh,
            charge_power_kw=self.planner_state.charge_power_kw,
            price_points=self._cached_prices,
            max_soc=car_charge_limit,
            cheap_price_threshold=cheap_price_threshold,
        )

    async def _actuate_switch(
        self,
        plan: ChargePlan,
        current_soc: float | None,
        target_soc: float | None,
        plugged_in: bool | None,
        is_home: bool | None,
        defer_for_data: bool,
        now: datetime,
        target_dt: datetime | None,
    ) -> None:
        if not self.planner_state.master_switch_on:
            return  # hands-off: don't touch the charge switch at all
        if self.is_paused_by_mode():
            return  # another controller (e.g. PV-surplus) owns the wallbox

        desired_on = self._decide_desired_state(
            plan, current_soc, target_soc, plugged_in, is_home, defer_for_data, now, target_dt
        )

        switch_entity = self._config[CONF_CHARGE_SWITCH]
        current_state = self.hass.states.get(switch_entity)
        currently_on = current_state is not None and current_state.state == "on"
        # Everything the decision hinged on, for the plan sensor's
        # "entscheidung" attribute — makes "why didn't it switch?" answerable.
        self.last_decision = {
            "zeit": now.isoformat(),
            "soll_an": desired_on,
            "schalter_ist_an": currently_on,
            "schalter_zustand": current_state.state if current_state else None,
            "im_slot": any(s.start <= now < s.start + SLOT_DURATION for s in plan.slots),
            "soc": current_soc,
            "ziel_soc": target_soc,
            "angesteckt": plugged_in,
            "zuhause": is_home,
            "wartet_auf_daten": defer_for_data,
        }
        if desired_on == currently_on:
            self._act_fail_count = 0
            self._act_retry_at = None
            self.actuation_error = None
            return

        # A vehicle-API failure (car asleep, cloud error, or the vehicle API
        # rejecting unsigned commands with 403) must not abort the cycle nor
        # be hammered every minute: the underlying client retries for ~25 s
        # per attempt. Back off exponentially (1, 2, 5, 10 min) and surface
        # the error; the first success clears everything.
        if self._act_retry_at is not None and now < self._act_retry_at:
            return
        _LOGGER.info(
            "Switching %s %s (in slot: %s, soc: %s, target: %s)",
            switch_entity, "on" if desired_on else "off",
            self.last_decision["im_slot"], current_soc, target_soc,
        )
        try:
            await self.hass.services.async_call(
                "switch",
                "turn_on" if desired_on else "turn_off",
                {"entity_id": switch_entity},
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001
            self._act_fail_count += 1
            delay = (1, 2, 5, 10)[min(self._act_fail_count, 4) - 1]
            self._act_retry_at = now + timedelta(minutes=delay)
            self.actuation_error = {
                "zeit": now.isoformat(),
                "schalter": switch_entity,
                "aktion": "an" if desired_on else "aus",
                "fehler": f"{type(err).__name__}: {err}".strip(": "),
                "versuche": self._act_fail_count,
                "naechster_versuch": self._act_retry_at.isoformat(),
            }
            _LOGGER.warning(
                "Could not switch %s %s (attempt %d); retrying in %d min",
                switch_entity, "on" if desired_on else "off", self._act_fail_count, delay,
                exc_info=self._act_fail_count == 1,
            )
            return
        self._act_fail_count = 0
        self._act_retry_at = None
        self.actuation_error = None

    def _decide_desired_state(
        self,
        plan: ChargePlan,
        current_soc: float | None,
        target_soc: float | None,
        plugged_in: bool | None,
        is_home: bool | None,
        defer_for_data: bool,
        now: datetime,
        target_dt: datetime | None,
    ) -> bool:
        if target_soc is None:
            return False  # no active occurrence at all — nothing to charge toward
        # Stop point is the effective ceiling: the car's charge limit while
        # opportunistic top-up is active, otherwise just the target SoC.
        ceiling = plan.effective_ceiling_soc if plan.effective_ceiling_soc is not None else target_soc
        if current_soc is not None and current_soc >= ceiling:
            return False
        if defer_for_data:
            # Price data doesn't cover the full window yet, and there's
            # provably enough slack to wait for it without risking the
            # deadline — see readiness.py. Once that stops being true
            # (deadline gets close, or fuller/better-looking data arrives),
            # this flips back to False on its own next cycle.
            return False
        if plugged_in is False:
            return False
        if is_home is False:
            # Location gating configured and the vehicle isn't in the
            # chosen zone right now — never mind the schedule, there's
            # nothing to charge here. Checked even in the past-deadline
            # fallback below, since forcing a switch on remotely with
            # nothing connected accomplishes nothing.
            return False
        if (
            target_dt is not None
            and now >= target_dt
            and (current_soc is None or current_soc < target_soc)
        ):
            # Deadline already blown and the GUARANTEED target not met yet:
            # best-effort charge regardless of the (now stale/empty) plan.
            # Section 2.5's "target beats cost" fallback taken to its logical
            # extreme — but only up to the guaranteed floor, never forcing
            # the opportunistic ceiling. Past the floor, we fall through to
            # the plan below, which only holds cheap opportunistic slots.
            return True
        return any(s.start <= now < s.start + SLOT_DURATION for s in plan.slots)


def _plan_to_dict(plan: ChargePlan) -> dict:
    return {
        "slots": [{"start": s.start.isoformat(), "price": s.price} for s in plan.slots],
        "estimated_cost_eur": plan.estimated_cost_eur,
        "estimated_completion": plan.estimated_completion.isoformat() if plan.estimated_completion else None,
        "target_reachable": plan.target_reachable,
        "required_slot_count": plan.required_slot_count,
        "available_slot_count": plan.available_slot_count,
        "opportunistic_slot_count": plan.opportunistic_slot_count,
        "effective_ceiling_soc": plan.effective_ceiling_soc,
    }
