"""Hourly solar forecast (Forecast.Solar) for the departure-aware battery
target. The pure helpers (parse / energy_between) are isolated-testable; the
async fetch wraps the energy-dashboard platform function of the
forecast_solar integration and degrades to None when it is missing."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def parse_wh_hours(wh_hours: dict) -> list[tuple[datetime, float]]:
    """{iso timestamp: Wh} -> sorted [(aware datetime, Wh)]. Each value is the
    energy produced in the period ENDING at its timestamp (Forecast.Solar's
    `wh_period`: live data shows 0 Wh at sunrise 05:31, then 157 Wh at 06:00,
    and 661 Wh at 17:00 right before the 0 Wh at sunset)."""
    rows: list[tuple[datetime, float]] = []
    for key, wh in (wh_hours or {}).items():
        try:
            ts = datetime.fromisoformat(str(key))
            if ts.tzinfo is None:
                continue
            rows.append((ts, float(wh)))
        except (ValueError, TypeError):
            continue
    rows.sort(key=lambda r: r[0])
    return rows


def energy_between(rows: list[tuple[datetime, float]], start: datetime, end: datetime) -> float:
    """kWh forecast between two instants; a period that is only partly
    inside the interval counts proportionally."""
    if end <= start:
        return 0.0
    total_wh = 0.0
    for ts, wh in rows:
        period_start = ts - timedelta(hours=1)
        lo, hi = max(period_start, start), min(ts, end)
        if hi > lo:
            total_wh += wh * (hi - lo).total_seconds() / 3600.0
    return total_wh / 1000.0


async def async_fetch(hass: HomeAssistant) -> list[tuple[datetime, float]] | None:
    try:
        from homeassistant.components.forecast_solar.energy import (
            async_get_solar_forecast,
        )

        for entry in hass.config_entries.async_entries("forecast_solar"):
            result = await async_get_solar_forecast(hass, entry.entry_id)
            rows = parse_wh_hours((result or {}).get("wh_hours", {}))
            if rows:
                return rows
    except Exception:  # noqa: BLE001 - forecast is a convenience, never a requirement
        _LOGGER.debug("Smart: solar forecast unavailable", exc_info=True)
    return None
