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

from .const import EV_CONSUMPTION_LOOKBACK_DAYS, EV_CONSUMPTION_MIN_KM

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


async def async_estimate_ev_consumption_kwh_100km(
    hass: HomeAssistant, odometer_entity: str | None, charge_energy_entity: str | None
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
    return round(kwh / km * 100.0, 2)
