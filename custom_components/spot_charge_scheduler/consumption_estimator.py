"""Estimate the EV's real energy use *from the socket* per 100 km straight
out of Home Assistant's long-term statistics: Δ(charge-energy meter) ÷
Δ(odometer) over a rolling window. Same idea as capacity_estimator.py, but
the raw history is already in the recorder — nothing to sample live.

Everything here degrades safe: any missing entity, too little distance in
the window, or an unexpected statistics-API shape just returns None, and the
caller keeps the manually configured EV-consumption value.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import CHARGE_EFFICIENCY, EV_CONSUMPTION_LOOKBACK_DAYS, EV_CONSUMPTION_MIN_KM

_LOGGER = logging.getLogger(__name__)


def _row_start(row: dict) -> float | None:
    """Row's bucket-start as a comparable number, regardless of whether the
    statistics API handed it back as epoch (int/float) or a datetime."""
    start = row.get("start")
    if isinstance(start, (int, float)):
        return float(start)
    if hasattr(start, "timestamp"):
        return start.timestamp()
    return None


def _first_valid_start(rows: list[dict], key: str) -> float | None:
    """Bucket-start of this series' first row that actually carries `key` —
    i.e. when the series' recorder history really begins, which for a
    freshly-created entity can be far later than the requested window
    start."""
    for row in rows:
        if isinstance(row.get(key), (int, float)):
            return _row_start(row)
    return None


def _monotonic_span(rows: list[dict]) -> float | None:
    """Increase of a total_increasing series across the rows. Prefers the
    reset-proof `sum`; falls back to the raw `state` (meter reading). None
    if it can't get a positive delta from at least two points."""
    for key in ("sum", "state"):
        vals = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
        if len(vals) >= 2 and (vals[-1] - vals[0]) > 0:
            return float(vals[-1] - vals[0])
    return None


def _align_to_common_window(odo_rows: list[dict], energy_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Clip both series to the window where BOTH actually have recorder
    history. Without this, a freshly-created odometer entity (e.g. right
    after a migration to a new integration) whose history only goes back a
    few days gets divided against an energy meter's full 90-day total —
    correct numerator, wrong (much too short) denominator, wildly inflated
    result. Real-world trigger: 2026-09 Tesla Fleet migration replaced the
    odometer entity; the energy meter (a separate wallbox device) kept its
    full history, and once 300 km accumulated on the new entity the ratio
    used ~90 days of energy against ~5 days of distance."""
    odo_start = _first_valid_start(odo_rows, "state") or _first_valid_start(odo_rows, "sum")
    energy_start = _first_valid_start(energy_rows, "state") or _first_valid_start(energy_rows, "sum")
    if odo_start is None or energy_start is None:
        return odo_rows, energy_rows
    common_start = max(odo_start, energy_start)
    clip = lambda rows: [r for r in rows if (_row_start(r) or 0) >= common_start]
    return clip(odo_rows), clip(energy_rows)


def soc_adjusted_kwh(
    kwh_from_socket: float,
    soc_start: float,
    soc_end: float,
    capacity_kwh: float,
    efficiency: float = CHARGE_EFFICIENCY,
) -> float:
    """Energy actually USED for driving in the window: what came out of the
    wallbox minus what is still sitting in the battery because the state of
    charge ended higher than it started (valued at the wallbox side, i.e.
    divided by the charging efficiency). Without this a short window that
    happens to end right after a charge reads far too high (live 2026-10-03:
    21.7 instead of ≈17 kWh/100 km — the battery went 13 % → 49 % inside a
    7-day window)."""
    stored_kwh = (soc_end - soc_start) / 100.0 * capacity_kwh / efficiency
    return kwh_from_socket - stored_kwh


async def _soc_at(hass: HomeAssistant, soc_entity: str, when) -> float | None:
    """State of charge at `when` from the recorder (the state that was
    current at that moment), or None."""
    try:
        from homeassistant.components.recorder import get_instance, history

        result = await get_instance(hass).async_add_executor_job(
            lambda: history.get_significant_states(
                hass,
                when,
                when + timedelta(hours=3),
                [soc_entity],
                include_start_time_state=True,
                significant_changes_only=False,
                no_attributes=True,
            )
        )
    except Exception:  # noqa: BLE001
        _LOGGER.debug("SoC history query failed", exc_info=True)
        return None
    for state in result.get(soc_entity, []):
        try:
            return float(state.state)
        except (ValueError, TypeError):
            continue
    return None


async def async_estimate_ev_consumption_kwh_100km(
    hass: HomeAssistant,
    odometer_entity: str | None,
    charge_energy_entity: str | None,
    soc_entity: str | None = None,
    capacity_kwh: float | None = None,
) -> float | None:
    if not odometer_entity or not charge_energy_entity:
        return None
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )
    except ImportError:
        return None

    end = dt_util.utcnow()
    start = end - timedelta(days=EV_CONSUMPTION_LOOKBACK_DAYS)
    try:
        stats = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            start,
            end,
            {odometer_entity, charge_energy_entity},
            "day",
            None,
            {"state", "sum"},
        )
    except Exception:  # noqa: BLE001 - statistics API varies across HA versions
        _LOGGER.debug("EV-consumption statistics query failed", exc_info=True)
        return None

    odo_rows, energy_rows = _align_to_common_window(
        stats.get(odometer_entity) or [], stats.get(charge_energy_entity) or []
    )
    km = _monotonic_span(odo_rows)
    kwh = _monotonic_span(energy_rows)
    if km is None or kwh is None or km < EV_CONSUMPTION_MIN_KM:
        return None

    # The span runs from the END of the first day bucket (its cumulative sum
    # is the baseline) to now — correct for the battery's net SoC change
    # between exactly those two moments.
    if soc_entity and capacity_kwh and odo_rows:
        first = _row_start(odo_rows[0])
        if first is not None and first > 1e11:  # epoch milliseconds
            first /= 1000.0
        soc_now_state = hass.states.get(soc_entity)
        soc_end = None
        if soc_now_state is not None:
            try:
                soc_end = float(soc_now_state.state)
            except ValueError:
                soc_end = None
        soc_start = (
            await _soc_at(hass, soc_entity, dt_util.utc_from_timestamp(first + 86400))
            if first is not None
            else None
        )
        # No SoC data -> NO estimate (the caller keeps the previous value).
        # Falling back to the raw ratio is wrong: right after an HA restart
        # the SoC sensor is briefly unknown, and the raw value (24.7 live on
        # 2026-10-03, vs 17.7 corrected) would overwrite a good one.
        if soc_end is None or soc_start is None:
            return None
        kwh = soc_adjusted_kwh(kwh, soc_start, soc_end, capacity_kwh)
        if kwh <= 0:
            return None
    return round(kwh / km * 100.0, 2)
