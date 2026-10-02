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
    base_kw: float  # house load WITHOUT the wallbox (raw, not floored)
    wallbox_kw: float = 0.0  # what the car is drawing right now


# --- fast loop (every coordinator cycle, ~60 s) ---
# The 15-min/30-min decision is too slow once the wallbox stops regulating on
# its own: a cloud front would drain the home battery (or import from the
# grid) until the next decision. The fast loop watches the balance
# PV − house load − battery target over a few minutes and corrects the
# current — quickly DOWN, patiently UP.
FAST_DOWN_WINDOW_MINUTES = 3
FAST_DOWN_MIN_SAMPLES = 2
FAST_DOWN_DEADBAND_KW = 0.3
FAST_UP_WINDOW_MINUTES = 5
FAST_UP_MIN_SAMPLES = 4
FAST_UP_THRESHOLD_KW = 1.0
# After the current was changed, ignore this long (the car and the load
# readings need to settle) and only judge samples taken afterwards.
SETTLE_SECONDS = 60
# The slow decision may not exceed what the last few minutes support.
SHORT_CAP_WINDOW_MINUTES = 5
SHORT_CAP_MIN_SAMPLES = 3


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
    raw_slow = max(0, min(MAX_AMPS, math.floor(surplus * 1000.0 / (VOLTAGE * PHASES))))
    # Never promise more than the last few minutes support (otherwise a slow
    # decision would undo the fast brake right after a cloud arrived).
    short = [s for s in samples if s.ts > now - timedelta(minutes=SHORT_CAP_WINDOW_MINUTES)]
    raw = raw_slow
    if len(short) >= SHORT_CAP_MIN_SAMPLES:
        s_pv = sum(x.pv_kw for x in short) / len(short)
        s_base = sum(max(x.base_kw, MIN_BASE_LOAD_KW) for x in short) / len(short)
        raw_short = max(0, min(MAX_AMPS, math.floor((s_pv - s_base - target) * 1000.0 / (VOLTAGE * PHASES))))
        raw = min(raw_slow, raw_short)
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
        "ampere_roh_30min": raw_slow,
        "ampere": amps,
        "soll_leistung_kw": round(amps_to_kw(amps), 2),
        "fenster_unter_minimum": low_windows,
        "grund": reason,
        "abgedeckt_min": round(coverage, 1),
    }


def fast_adjust(
    samples: list[Sample],
    now: datetime,
    battery_soc: float,
    current_amps: int,
    last_change: datetime | None,
) -> dict | None:
    """Fast correction of the CURRENT charging current while the car charges.
    Returns {"amps": new, "grund", "bilanz_kw", ...} or None (no change).
    new == 0 means stop. `current_amps` is what the car actually draws."""
    if current_amps <= 0:
        return None
    settled_from = (
        last_change + timedelta(seconds=SETTLE_SECONDS) if last_change is not None else None
    )

    def window(minutes: int) -> list[Sample]:
        start = now - timedelta(minutes=minutes)
        if settled_from is not None and settled_from > start:
            start = settled_from
        return [s for s in samples if s.ts > start]

    def balance(win: list[Sample]) -> float:
        # PV − house load (= base + wallbox): what the battery + grid absorb
        return sum(s.pv_kw - s.base_kw - s.wallbox_kw for s in win) / len(win)

    target = battery_target_kw(battery_soc)
    kw_per_amp = amps_to_kw(1)

    down = window(FAST_DOWN_WINDOW_MINUTES)
    if len(down) >= FAST_DOWN_MIN_SAMPLES:
        err = balance(down) - target
        if err < -FAST_DOWN_DEADBAND_KW:
            new = current_amps - math.ceil(-err / kw_per_amp)
            if new < MIN_AMPS:
                # still short of power at the minimum -> stop; first step
                # down to the minimum otherwise
                new = MIN_AMPS if current_amps > MIN_AMPS else 0
            return {
                "amps": new, "grund": "schnell_runter",
                "bilanz_kw": round(err, 2), "fenster_samples": len(down),
            }

    up = window(FAST_UP_WINDOW_MINUTES)
    if len(up) >= FAST_UP_MIN_SAMPLES and current_amps < MAX_AMPS:
        err = balance(up) - target
        if err > FAST_UP_THRESHOLD_KW:
            new = min(MAX_AMPS, current_amps + max(1, math.floor(err / kw_per_amp)))
            if new != current_amps:
                return {
                    "amps": new, "grund": "schnell_hoch",
                    "bilanz_kw": round(err, 2), "fenster_samples": len(up),
                }
    return None
