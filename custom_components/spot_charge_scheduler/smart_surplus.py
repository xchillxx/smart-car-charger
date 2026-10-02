"""Pure decision logic for the "Überschuss Smart" controller (no HA imports,
isolated-testable).

Every DECISION_INTERVAL_MINUTES the controller averages the last
AVERAGING_WINDOW_MINUTES of (PV production, house base load) and works out
which charging current the car should draw so that:
  * nothing is fed into the grid (the car soaks up the PV surplus), and
  * the home battery is not drained — it keeps charging at a target rate that
    shrinks the fuller it is: (100 − SoC) % × BATTERY_TARGET_AT_EMPTY_KW.

Live-tuned against a cloudy day (2026-10-02): a 15-min decision rhythm on a
30-min average gave the lowest feed-in / battery-discharge of the variants
compared (10/10, 15/15, 15/45, 30/30, 60/60). One-minute PV swings (±4 kW)
are absorbed by the home battery and are not tracked by any slow controller.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

DECISION_INTERVAL_MINUTES = 15
AVERAGING_WINDOW_MINUTES = 30
# Don't decide on a thinner base than this (e.g. right after an HA restart,
# when the in-memory sample buffer is empty).
MIN_WINDOW_COVERAGE_MINUTES = 10
SAMPLE_MIN_SPACING_SECONDS = 45

MIN_AMPS = 5
MAX_AMPS = 16
VOLTAGE = 230.0
PHASES = 3
# Fewer consecutive windows than this below MIN_AMPS keep the car charging at
# the minimum instead of stopping it (avoids start/stop flapping).
STOP_AFTER_LOW_WINDOWS = 2

BATTERY_TARGET_AT_EMPTY_KW = 2.0
# House base load can't be negative / is never really zero; the tiny mismatch
# between the car's power reading and the inverter's load reading would
# otherwise produce phantom surplus.
MIN_BASE_LOAD_KW = 0.2


@dataclass(frozen=True)
class Sample:
    ts: datetime
    pv_kw: float
    base_kw: float  # house load WITHOUT the wallbox


def amps_to_kw(amps: float) -> float:
    return amps * VOLTAGE * PHASES / 1000.0


def battery_target_kw(battery_soc: float) -> float:
    """Charge rate the home battery should keep getting: 1 kW at 50 %,
    0.2 kW at 90 %, 0 at 100 %."""
    return max(0.0, min(BATTERY_TARGET_AT_EMPTY_KW, (100.0 - battery_soc) / 100.0 * BATTERY_TARGET_AT_EMPTY_KW))


def prune(samples: list[Sample], now: datetime) -> list[Sample]:
    cutoff = now - timedelta(minutes=AVERAGING_WINDOW_MINUTES + 1)
    return [s for s in samples if s.ts >= cutoff]


def add_sample(samples: list[Sample], sample: Sample) -> list[Sample]:
    """Append unless the previous sample is too recent (state-change listeners
    can trigger extra refreshes between the regular 60 s polls, which would
    otherwise over-weight those moments in the average)."""
    if samples and (sample.ts - samples[-1].ts).total_seconds() < SAMPLE_MIN_SPACING_SECONDS:
        return samples
    return prune(samples + [sample], sample.ts)


def window_means(samples: list[Sample], now: datetime) -> tuple[float, float, float] | None:
    """(mean PV kW, mean base kW, covered minutes) over the averaging window,
    or None when the buffer covers less than MIN_WINDOW_COVERAGE_MINUTES."""
    cutoff = now - timedelta(minutes=AVERAGING_WINDOW_MINUTES)
    window = [s for s in samples if s.ts > cutoff]
    if len(window) < 2:
        return None
    coverage = (window[-1].ts - window[0].ts).total_seconds() / 60.0
    if coverage < MIN_WINDOW_COVERAGE_MINUTES:
        return None
    pv = sum(s.pv_kw for s in window) / len(window)
    base = sum(max(s.base_kw, MIN_BASE_LOAD_KW) for s in window) / len(window)
    return pv, base, coverage


def decision_slot(now: datetime) -> tuple:
    """Identifies the wall-clock quarter hour a decision belongs to."""
    return (now.date(), now.hour, now.minute // DECISION_INTERVAL_MINUTES)


def decide(
    samples: list[Sample],
    now: datetime,
    battery_soc: float,
    charging: bool,
    low_windows_before: int,
) -> dict | None:
    """One decision. `amps` is the desired charging current (0 = no
    charging). None when there isn't enough data yet."""
    means = window_means(samples, now)
    if means is None:
        return None
    pv, base, coverage = means
    target = battery_target_kw(battery_soc)
    surplus = pv - base - target
    raw = max(0, min(MAX_AMPS, math.floor(surplus * 1000.0 / (VOLTAGE * PHASES))))
    if raw >= MIN_AMPS:
        amps, low_windows, reason = raw, 0, "ueberschuss"
    else:
        low_windows = low_windows_before + 1
        if charging and low_windows < STOP_AFTER_LOW_WINDOWS:
            amps, reason = MIN_AMPS, "unter_minimum_haelt"
        else:
            amps, reason = 0, "unter_minimum_stopp" if charging else "unter_minimum"
    return {
        "zeit": now.isoformat(),
        "pv_mittel_kw": round(pv, 2),
        "grundlast_mittel_kw": round(base, 2),
        "akku_soc": battery_soc,
        "akku_ziel_kw": round(target, 2),
        "ueberschuss_kw": round(surplus, 2),
        "ampere_roh": raw,
        "ampere": amps,
        "soll_leistung_kw": round(amps_to_kw(amps), 2),
        "fenster_unter_minimum": low_windows,
        "grund": reason,
        "abgedeckt_min": round(coverage, 1),
    }
