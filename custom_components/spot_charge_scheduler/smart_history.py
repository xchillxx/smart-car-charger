"""Rebuild the "Überschuss Smart" sample buffer from the recorder after a
restart, so the 30-min average (and the fast loop) are available right away
instead of after ~10 minutes of fresh sampling. Everything degrades safe: no
recorder / no history / odd states just return an empty list and the live
sampling in the coordinator fills the buffer as before."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant

from .smart_surplus import AVERAGING_WINDOW_MINUTES, Sample

_LOGGER = logging.getLogger(__name__)


def _to_kw(state) -> float | None:
    if state is None or state.state in ("unknown", "unavailable"):
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    if (getattr(state, "attributes", None) or {}).get("unit_of_measurement") == "W":
        value /= 1000.0
    return value


def resample(
    history: dict[str, list], pv_entity: str, load_entity: str, wallbox_entity: str, now: datetime
) -> list[Sample]:
    """One sample per minute over the averaging window, each series
    forward-filled from its last state change. A minute is skipped when PV
    or load has no valid value; a missing/unavailable wallbox reading counts
    as 0 (a sleeping car's power sensor reads unavailable)."""
    def series(entity: str) -> list:
        rows = [s for s in history.get(entity, []) if s is not None]
        rows.sort(key=lambda s: s.last_updated)
        return rows

    pv_rows, load_rows, wb_rows = series(pv_entity), series(load_entity), series(wallbox_entity)

    def at(rows: list, t: datetime):
        last = None
        for s in rows:
            if s.last_updated <= t:
                last = s
            else:
                break
        return last

    samples: list[Sample] = []
    start = now - timedelta(minutes=AVERAGING_WINDOW_MINUTES)
    t = start
    while t < now:
        pv, load = _to_kw(at(pv_rows, t)), _to_kw(at(load_rows, t))
        if pv is not None and load is not None:
            wb = _to_kw(at(wb_rows, t)) or 0.0
            samples.append(Sample(t, pv, load - wb, wb))
        t += timedelta(minutes=1)
    return samples


async def async_backfill(
    hass: HomeAssistant, pv_entity: str, load_entity: str, wallbox_entity: str | None, now: datetime
) -> list[Sample]:
    entities = [e for e in (pv_entity, load_entity, wallbox_entity) if e]
    try:
        from homeassistant.components.recorder import get_instance, history

        start = now - timedelta(minutes=AVERAGING_WINDOW_MINUTES + 2)
        result = await get_instance(hass).async_add_executor_job(
            lambda: history.get_significant_states(
                hass,
                start,
                now,
                entities,
                include_start_time_state=True,
                significant_changes_only=False,
            )
        )
        samples = resample(result, pv_entity, load_entity, wallbox_entity or "", now)
        _LOGGER.info("Smart: backfilled %d one-minute samples from the recorder", len(samples))
        return samples
    except Exception:  # noqa: BLE001 - history is a convenience, never a requirement
        _LOGGER.warning("Smart: recorder backfill failed, falling back to live sampling", exc_info=True)
        return []
