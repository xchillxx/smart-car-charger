"""Live planning state: the fixed set of editable charge-target "cycle
slots", capacity/power calibration, and the currently computed plan.

Deliberately NOT stored in the config entry's `entry.data` the way SLS's
number entities do it: those trigger a full integration reload on every
write (fine for rarely-changed setup fields like "which switch entity",
wrong for something meant to be edited freely — nudging a slot's target
SoC must not interrupt an active charge session via a reload). This lives
in its own Store instead, and the per-slot entities write straight into
the in-memory dict + ask the coordinator to recompute — no reload.

A "cycle slot" is a charge-target definition edited directly through native
entities on the dashboard (v0.13.0 — replaced the calendar-authored cycles
+ per-occurrence override model, which proved too fiddly). Shape:
    {slot: 1..NUM_CYCLE_SLOTS, enabled, name, target_soc,
     time ("HH:MM"), rhythm_days (0 = one-off), anchor (ISO | None)}
See schedule.py for expansion into occurrences.
"""
from __future__ import annotations

import logging
from statistics import median
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_EV_CONSUMPTION_KWH_100KM,
    DEFAULT_EXPENSIVE_PERCENTILE,
    DEFAULT_FUEL_PRICE_EUR_L,
    DEFAULT_ICE_CONSUMPTION_L_100KM,
    DEFAULT_OPPORTUNISTIC_PERCENTILE,
    DEFAULT_SLOT_RHYTHM_DAYS,
    DEFAULT_SLOT_TARGET_SOC,
    DEFAULT_SLOT_TIME,
    CONTROLLER_PRICE,
    MAX_CALIBRATION_SAMPLES,
    MIN_CALIBRATION_SAMPLES_TO_TRUST,
    NUM_CYCLE_SLOTS,
)

_LOGGER = logging.getLogger(__name__)


def _blank_slot(n: int) -> dict[str, Any]:
    return {
        "slot": n,
        "enabled": False,
        "name": "",
        "target_soc": DEFAULT_SLOT_TARGET_SOC,
        "time": DEFAULT_SLOT_TIME,
        "rhythm_days": DEFAULT_SLOT_RHYTHM_DAYS,
        "anchor": None,
    }


def _default_slots() -> list[dict[str, Any]]:
    return [_blank_slot(n) for n in range(1, NUM_CYCLE_SLOTS + 1)]


def _coerce_slots(raw: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Force a stored slot list back to exactly NUM_CYCLE_SLOTS well-formed
    entries (pad short, drop extra, fill missing keys) so nothing
    downstream has to defend against a half-written slot."""
    slots = _default_slots()
    for i, stored in enumerate((raw or [])[:NUM_CYCLE_SLOTS]):
        merged = {**slots[i], **{k: stored[k] for k in slots[i] if k in stored}}
        merged["slot"] = i + 1
        slots[i] = merged
    return slots


def _migrate_from_cycles(old_cycles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One-time upgrade from the pre-0.13.0 calendar-cycle model. Only the
    recurring cycles carry over (a bare one-off had no lasting identity
    worth a slot, and any that were "deleted" in the old UI still lingered
    as dead entries) — earliest anchor first, into the lowest slots."""
    recurring = [
        c for c in old_cycles
        if int(c.get("rhythm_days") or 0) > 0 and c.get("enabled", True)
    ]
    recurring.sort(key=lambda c: c.get("anchor") or "")
    slots = _default_slots()
    for i, cycle in enumerate(recurring[:NUM_CYCLE_SLOTS]):
        anchor = dt_util.parse_datetime(cycle.get("anchor") or "")
        name = str(cycle.get("summary") or "").strip()
        # strip a trailing "(NN%)" the old display code may have baked in
        if name.endswith("%)") and "(" in name:
            name = name[: name.rfind("(")].strip()
        slots[i] = {
            "slot": i + 1,
            "enabled": True,
            "name": name,
            "target_soc": float(cycle.get("target_soc") or DEFAULT_SLOT_TARGET_SOC),
            "time": anchor.strftime("%H:%M") if anchor else DEFAULT_SLOT_TIME,
            "rhythm_days": int(cycle.get("rhythm_days") or DEFAULT_SLOT_RHYTHM_DAYS),
            # keep the original anchor so the existing N-day phase is not
            # disturbed by the upgrade
            "anchor": cycle.get("anchor"),
        }
    _LOGGER.info(
        "Smart Car Charger: migrated %d recurring cycle(s) into slots", len(recurring[:NUM_CYCLE_SLOTS])
    )
    return slots

STORAGE_VERSION = 1  # unchanged on purpose — see async_load, old/new fields
# coexist fine via .get()-with-default; no async_migrate_func needed, and
# bumping the version without one risks Store raising on the existing live
# Store file instead of just handing back the old dict.
SAVE_DELAY_SECONDS = 10


class PlannerState:
    """Mutable planning state for one config entry, persisted via Store."""

    def __init__(
        self, hass: HomeAssistant, entry_id: str, default_capacity_kwh: float, default_charge_power_kw: float
    ) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, f"spot_charge_scheduler_{entry_id}")
        self._default_capacity_kwh = default_capacity_kwh
        self._default_charge_power_kw = default_charge_power_kw
        # Fixed list of NUM_CYCLE_SLOTS slot dicts (see module docstring +
        # schedule.py). Always exactly that many entries; unused ones just
        # have enabled=False.
        self.cycle_slots: list[dict[str, Any]] = _default_slots()
        self.battery_capacity_kwh: float = default_capacity_kwh
        self.capacity_samples: list[float] = []
        self.charge_power_kw: float = default_charge_power_kw
        self.power_samples: list[float] = []
        # Opportunistic top-up: a slot counts as "cheap" at/below this
        # percentile of the last few days' observed prices (see
        # price_baseline.cheap_price_threshold). Freely editable live via the
        # "Billig-Schwelle (Perzentil)" number entity.
        self.opportunistic_percentile: float = DEFAULT_OPPORTUNISTIC_PERCENTILE
        # Diagnostic mirror of the above: a slot counts as "expensive" at/above
        # this percentile of the same price window. Not read by the planner —
        # see the "Teuer-Schwelle" sensor.
        self.expensive_percentile: float = DEFAULT_EXPENSIVE_PERCENTILE
        # One-time "charge to X % today only" override (see coordinator
        # _apply_oneoff): valid for the local date it was set on, then lapses.
        self.oneoff_target_soc: float | None = None
        self.oneoff_date: str | None = None
        # Combustion-engine comparison inputs (see coordinator break-even calc).
        # ev_consumption_kwh_100km is auto-overwritten from recorder statistics
        # once there's enough odometer/energy history (consumption_estimator.py).
        self.ice_consumption_l_100km: float = DEFAULT_ICE_CONSUMPTION_L_100KM
        self.ev_consumption_kwh_100km: float = DEFAULT_EV_CONSUMPTION_KWH_100KM
        # Fuel price (€/L) for the break-even calc — hand-set, then
        # overwritten hourly by the coordinator once a Tankerkönig key is
        # configured (see FuelPriceNumber / _maybe_fetch_fuel_price).
        self.fuel_price_eur_l: float = DEFAULT_FUEL_PRICE_EUR_L
        self.master_switch_on: bool = False
        # One-off departure with an explicit date (e.g. "tomorrow 12:00"):
        # same meaning as a cycle slot's "reach target SoC by time", but for a
        # single dated occasion. Switches itself off after it has passed.
        self.oneoff_departure: dict[str, Any] = {
            "enabled": False, "date": None, "time": "12:00", "target_soc": 80.0,
        }
        # "Überschuss Smart", battery-first target: the home battery's usable
        # capacity and how long before sunset PV stops being useful.
        self.home_battery_capacity_kwh: float = 14.0
        self.pv_end_before_sunset_h: float = 1.5
        # Which controller drives the charge switch: "price" (planner) or
        # "smart" (PV-surplus current control, see smart_surplus.py).
        self.controller_mode: str = CONTROLLER_PRICE
        # Charge-session edge tracking for capacity/power calibration (see
        # capacity_estimator.py) — None/empty when no session is open.
        self.session_start_soc: float | None = None
        self.session_start_energy_added: float | None = None
        self.session_power_readings: list[float] = []
        # Rolling per-slot price archive: [{"start": iso, "price": float}],
        # built up by the coordinator from its own price fetches — see
        # price_baseline.py.
        self.price_history: list[dict[str, Any]] = []
        # Most recently computed plan (coordinator.py owns recomputing this;
        # this class only stores/persists the result for the sensor to read).
        self.plan: dict[str, Any] = {
            "slots": [],
            "estimated_cost_eur": None,
            "estimated_completion": None,
            "target_reachable": None,
            "computed_at": None,
        }

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if not data:
            return
        if "cycle_slots" in data:
            self.cycle_slots = _coerce_slots(data["cycle_slots"])
        elif data.get("cycles"):
            # pre-0.13.0 store — migrate once; the old keys are simply not
            # written back by _data_to_save, so they drop out on next save
            self.cycle_slots = _migrate_from_cycles(data["cycles"])
        self.battery_capacity_kwh = data.get("battery_capacity_kwh", self._default_capacity_kwh)
        self.capacity_samples = data.get("capacity_samples", [])
        self.charge_power_kw = data.get("charge_power_kw", self._default_charge_power_kw)
        self.power_samples = data.get("power_samples", [])
        self.opportunistic_percentile = data.get(
            "opportunistic_percentile", DEFAULT_OPPORTUNISTIC_PERCENTILE
        )
        self.expensive_percentile = data.get(
            "expensive_percentile", DEFAULT_EXPENSIVE_PERCENTILE
        )
        self.oneoff_target_soc = data.get("oneoff_target_soc")
        self.oneoff_date = data.get("oneoff_date")
        self.ice_consumption_l_100km = data.get(
            "ice_consumption_l_100km", DEFAULT_ICE_CONSUMPTION_L_100KM
        )
        self.ev_consumption_kwh_100km = data.get(
            "ev_consumption_kwh_100km", DEFAULT_EV_CONSUMPTION_KWH_100KM
        )
        self.fuel_price_eur_l = data.get(
            "fuel_price_eur_l",
            data.get("fuel_price_manual_eur_l", DEFAULT_FUEL_PRICE_EUR_L),
        )
        self.master_switch_on = data.get("master_switch_on", False)
        stored_dep = data.get("oneoff_departure")
        if isinstance(stored_dep, dict):
            self.oneoff_departure = {**self.oneoff_departure, **stored_dep}
        self.home_battery_capacity_kwh = data.get("home_battery_capacity_kwh", 14.0)
        self.pv_end_before_sunset_h = data.get("pv_end_before_sunset_h", 1.5)
        self.controller_mode = data.get("controller_mode", CONTROLLER_PRICE)
        self.session_start_soc = data.get("session_start_soc")
        self.session_start_energy_added = data.get("session_start_energy_added")
        self.session_power_readings = data.get("session_power_readings", [])
        self.price_history = data.get("price_history", [])
        self.plan = data.get("plan", self.plan)

    def add_calibration_sample(self, implied_capacity_kwh: float) -> None:
        """Record one session's implied capacity and refresh the estimate.

        Median of the last MAX_CALIBRATION_SAMPLES, not a mean — one
        glitched session (e.g. a sensor hiccup mid-charge) shouldn't swing
        the number the way it would in an average.
        """
        self.capacity_samples.append(implied_capacity_kwh)
        self.capacity_samples = self.capacity_samples[-MAX_CALIBRATION_SAMPLES:]
        if len(self.capacity_samples) >= MIN_CALIBRATION_SAMPLES_TO_TRUST:
            self.battery_capacity_kwh = round(median(self.capacity_samples), 2)
        self.async_save()

    def add_power_sample(self, observed_power_kw: float) -> None:
        """Deliberately a running MAXIMUM, not a median-of-samples like
        add_calibration_sample: charging power has a hard physical ceiling
        (cable/breaker/car limits), and most sessions happen under
        PV-surplus charging, which only ever ties actual power BELOW that
        ceiling — never above it. A median over recent sessions drifts
        down during a stretch of low-surplus days even though the
        hardware's real capability hasn't changed at all (live-confirmed:
        3 low-surplus sessions pulled the estimate down to 4 kW from a
        true ~10.9 kW). The highest peak ever observed is always at least
        as trustworthy as any more recent-but-lower one, so the estimate
        only ever moves up when something faster is genuinely seen, never
        down just because conditions were poor lately."""
        self.power_samples.append(observed_power_kw)
        self.power_samples = self.power_samples[-MAX_CALIBRATION_SAMPLES:]
        self.charge_power_kw = round(max(self.charge_power_kw, observed_power_kw), 2)
        self.async_save()

    def async_save(self) -> None:
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY_SECONDS)

    async def async_save_now(self) -> None:
        await self._store.async_save(self._data_to_save())

    def _data_to_save(self) -> dict:
        return {
            "cycle_slots": self.cycle_slots,
            "battery_capacity_kwh": self.battery_capacity_kwh,
            "capacity_samples": self.capacity_samples,
            "charge_power_kw": self.charge_power_kw,
            "power_samples": self.power_samples,
            "opportunistic_percentile": self.opportunistic_percentile,
            "expensive_percentile": self.expensive_percentile,
            "oneoff_target_soc": self.oneoff_target_soc,
            "oneoff_date": self.oneoff_date,
            "ice_consumption_l_100km": self.ice_consumption_l_100km,
            "ev_consumption_kwh_100km": self.ev_consumption_kwh_100km,
            "fuel_price_eur_l": self.fuel_price_eur_l,
            "master_switch_on": self.master_switch_on,
            "oneoff_departure": self.oneoff_departure,
            "home_battery_capacity_kwh": self.home_battery_capacity_kwh,
            "pv_end_before_sunset_h": self.pv_end_before_sunset_h,
            "controller_mode": self.controller_mode,
            "session_start_soc": self.session_start_soc,
            "session_start_energy_added": self.session_start_energy_added,
            "session_power_readings": self.session_power_readings,
            "price_history": self.price_history,
            "plan": self.plan,
        }
