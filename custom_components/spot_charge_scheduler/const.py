"""Constants for Smart Car Charger."""
from __future__ import annotations

DOMAIN = "spot_charge_scheduler"
PLATFORMS = ["sensor", "number", "switch", "select", "text", "time", "date", "calendar"]

UPDATE_INTERVAL_SECONDS = 60

# Charge-target "cycles" are a FIXED set of editable slots (v0.13.0) — each
# one a handful of native entities (enabled switch, name text, target-SoC
# number, time, rhythm number) shown directly on the dashboard, no calendar
# editing / service calls. Most stay empty; the user fills as many as they
# need (two shift patterns + a few spares in practice).
NUM_CYCLE_SLOTS = 6
DEFAULT_SLOT_TARGET_SOC = 65.0
DEFAULT_SLOT_TIME = "04:30"  # HH:MM, local
DEFAULT_SLOT_RHYTHM_DAYS = 4  # 0 = one-off

# How often (minimum spacing) we're willing to call a price-source's fetch,
# even if our cached data doesn't yet cover the target — avoids hammering
# the service every 60s while waiting for tomorrow's prices to be published.
PRICE_FETCH_MIN_INTERVAL_SECONDS = 15 * 60
# After a FAILED fetch attempt (e.g. another integration's service briefly
# unavailable right after a reload — live-observed), retry much sooner
# than the success-case spacing above — a failure shouldn't leave the plan
# sitting on zero price data for minutes after the underlying problem is
# already gone.
PRICE_FETCH_RETRY_AFTER_FAILURE_SECONDS = 90

# Below this SoC delta (%), a charge session is too short/noisy to trust for
# capacity calibration (e.g. a session interrupted seconds after starting).
MIN_CALIBRATION_DELTA_SOC = 5.0
# Number of most-recent calibration samples kept; the estimate is their
# median, so one bad session can't skew it the way a mean would.
MAX_CALIBRATION_SAMPLES = 20
# Calibration only takes over from the config-provided default once this
# many independent sessions have been observed.
MIN_CALIBRATION_SAMPLES_TO_TRUST = 3
# A charging session needs at least this many power readings (one per
# coordinator cycle, ~60s apart) before its median is trusted as a power
# calibration sample — otherwise a session ended seconds after starting
# would contribute one barely-ramped-up reading.
MIN_POWER_READINGS_PER_SESSION = 3

# Config-entry keys (set via config flow, edited via options flow) — these
# describe *which entities* the integration talks to, not the live planning
# state (target SoC/time/etc. live in a separate Store, see planner_state.py,
# so editing them doesn't reload the whole integration mid-session).
CONF_CHARGE_SWITCH = "charge_switch_entity"
CONF_SOC_SENSOR = "soc_sensor_entity"
CONF_CHARGING_STATUS_SENSOR = "charging_status_sensor_entity"
CONF_PLUGGED_IN_SENSOR = "plugged_in_sensor_entity"
CONF_ENERGY_ADDED_SENSOR = "energy_added_sensor_entity"
# Both optional together: without a tracker, or without a zone picked, no
# location gating happens at all (same opt-out philosophy as the other
# optional sensors) — set both to only charge while the vehicle is inside
# the chosen zone, e.g. so a charge slot starting while the car is out
# doesn't just do nothing to a switch entity that isn't even connected to
# anything at that location.
CONF_LOCATION_TRACKER_ENTITY = "location_tracker_entity"
CONF_HOME_ZONE_ENTITY = "home_zone_entity"
CONF_CHARGE_POWER_KW = "charge_power_kw"
# Optional: a live power sensor (kW) that reports actual draw while
# charging (e.g. sensor.model_3_charger_power — not a "rate" sensor, which
# on Tesla reports distance/hour added, not power). When set, the assumed
# charging power for planning is self-calibrated from real sessions'
# median power instead of staying pinned to the static config value —
# same philosophy as the battery-capacity calibrator.
CONF_CHARGE_POWER_SENSOR = "charge_power_sensor_entity"
# Optional: a live number entity holding the car's own target charge limit
# (e.g. number.model_3_charge_limit). When set and readable, the planner is
# allowed to ALSO grab still-cheap slots beyond the active cycle's
# guaranteed target SoC, up to this limit — an opportunistic top-up, never
# forced (see planner.compute_plan / OPPORTUNISTIC_* below). Unset, or the
# entity unavailable, and nothing changes: only the guaranteed target is
# pursued, exactly as before.
CONF_CAR_CHARGE_LIMIT_ENTITY = "car_charge_limit_entity"
# Optional hands-off gate: while this sensor's state equals the given value
# (e.g. a wallbox working mode "PV Power" handled by another controller),
# the scheduler never touches the charge switch.
# Optional fallback for the car charge limit: a sensor (e.g. the always-live
# TeslaMate charge-limit SoC) read when the primary number entity is
# unknown/unavailable — typical right after the car woke up and its Fleet
# entities have not refreshed yet.
CONF_CAR_CHARGE_LIMIT_FALLBACK = "car_charge_limit_fallback_sensor_entity"
CONF_PAUSE_MODE_SENSOR = "pause_mode_sensor_entity"
CONF_PAUSE_MODE_VALUE = "pause_mode_value"
DEFAULT_PAUSE_MODE_VALUE = "PV Power"
# --- "Überschuss Smart" controller (all optional; needed only for that mode,
# the recommendation sensors stay "unknown" without them) ---
# number entity holding the car's charging current in A (e.g. the Tesla
# Fleet "Ladestromstärke" number) — the setpoint this controller writes.
CONF_CHARGE_CURRENT_ENTITY = "charge_current_entity"
# Live PV production (kW), total house consumption INCLUDING the wallbox
# (kW) and the home battery's state of charge (%). The wallbox draw itself is
# taken from CONF_CHARGE_POWER_SENSOR and subtracted from the house load to
# get the base load.
CONF_PV_POWER_SENSOR = "pv_power_sensor_entity"
CONF_HOUSE_LOAD_SENSOR = "house_load_sensor_entity"
CONF_HOME_BATTERY_SOC_SENSOR = "home_battery_soc_sensor_entity"

CONTROLLER_PRICE = "price"
CONTROLLER_SMART = "smart"
CONTROLLER_OPTIONS = {
    CONTROLLER_PRICE: "Preis-Optimiert",
    CONTROLLER_SMART: "Überschuss Smart",
}

CONF_PRICE_SOURCE = "price_source"
CONF_TIBBER_HOME_NICKNAME = "tibber_home_nickname"
CONF_BATTERY_CAPACITY_KWH_DEFAULT = "battery_capacity_kwh_default"

# --- Combustion-engine cost comparison (all optional) ---
# A free Tankerkönig API key (tankerkoenig.de) turns on a fuel-price sensor
# and, together with the two consumption numbers, a "break-even" sensor:
# above how many ct/kWh would driving the combustion car be cheaper per km.
CONF_TANKERKOENIG_API_KEY = "tankerkoenig_api_key"
CONF_FUEL_TYPE = "fuel_type"
CONF_FUEL_RADIUS_KM = "fuel_radius_km"
# Optional pair for self-calibrating "kWh from the socket per 100 km" out of
# Home Assistant's own long-term statistics (Δ energy ÷ Δ odometer).
CONF_ODOMETER_ENTITY = "odometer_entity"
CONF_CHARGE_ENERGY_ENTITY = "charge_energy_entity"
# Optional: a live electricity-price sensor (€/kWh) used only for the
# "cheaper right now" comparison when no charge plan is active to read the
# current spot price from (e.g. sensor.<tibber_home>_electricity_price).
CONF_CURRENT_PRICE_SENSOR = "current_price_sensor_entity"

FUEL_TYPES = ["e5", "e10", "diesel"]
DEFAULT_FUEL_TYPE = "diesel"
DEFAULT_FUEL_RADIUS_KM = 5.0
FUEL_FETCH_MIN_INTERVAL_SECONDS = 3600  # Tankerkönig asks for ~hourly polling
FUEL_FETCH_RETRY_AFTER_FAILURE_SECONDS = 300
# Starting fuel price (€/L) for the break-even calc. Lives on the
# "Spritpreis" number entity: set it by hand, and once a Tankerkönig API
# key is configured the coordinator overwrites it hourly with the cheapest
# local price — same self-calibrating pattern as battery capacity / charge
# power. Used as-is whenever no live price is available.
DEFAULT_FUEL_PRICE_EUR_L = 1.75

DEFAULT_ICE_CONSUMPTION_L_100KM = 6.5
DEFAULT_EV_CONSUMPTION_KWH_100KM = 20.0  # from the socket, incl. charging losses
# EV-consumption self-calibration from the recorder's long-term statistics.
EV_CONSUMPTION_LOOKBACK_DAYS = 90
EV_CONSUMPTION_MIN_KM = 300.0  # need this much distance in the window to trust the ratio
EV_CONSUMPTION_RECALC_INTERVAL_SECONDS = 6 * 3600
# Share of the energy drawn at the wallbox that ends up in the car's battery
# (charger + cable losses ≈ 10 %). Used to value the battery's net state-of-
# charge change inside the consumption window, see consumption_estimator.py.
CHARGE_EFFICIENCY = 0.90

PRICE_SOURCE_TIBBER = "tibber"
# Only Tibber is implemented today (see price_source.py) — kept as a select
# rather than hardcoded so a second provider (e.g. a generic day-ahead/EPEX
# sensor) can be added later without changing the config flow shape.
PRICE_SOURCES = [PRICE_SOURCE_TIBBER]

# Selecting the N cheapest slots by raw price alone can fragment into many
# isolated slots when nearby prices differ by a fraction of a cent — cheaper
# on paper, but relay/contactor wear from switching every 15 min is worth
# more than that. A gap between two selected slots is bridged (charged
# through, no matter how long) as long as every slot in it costs no more
# than this fraction above the priciest slot already selected — i.e. "I'm
# already paying up to X, so filling the gap for at most X*1.05 is a
# rounding error, not a real cost decision". See planner.py's
# _bridge_gaps_by_price.
PRICE_BRIDGE_TOLERANCE = 0.05

DEFAULT_TARGET_SOC = 50.0
# Last-resort fallback if a slot somehow has no parseable target_soc
# (see schedule.expand_slot). Normal slots default to DEFAULT_SLOT_TARGET_SOC.

# --- Opportunistic top-up ("charge past the guaranteed target, up to the
# car's own charge limit, but only while power is genuinely cheap") ---
# The guaranteed floor stays the active cycle's target SoC: reached by the
# deadline no matter the price, and the only thing the past-deadline
# "target beats cost" fallback ever forces. On top of that, if
# CONF_CAR_CHARGE_LIMIT_ENTITY is configured and currently readable, the
# planner ALSO takes any still-cheap eligible slots up to that limit —
# purely "it's cheap right now, might as well", never forced, never
# extending the promised completion time.
# "Cheap" = at or below this percentile of every price OBSERVED (i.e. in
# the past, not the forward forecast) over the last OPPORTUNISTIC_LOOKBACK_DAYS
# days. Live-tunable via the "Billig-Schwelle (Perzentil)" number entity.
DEFAULT_OPPORTUNISTIC_PERCENTILE = 10.0
OPPORTUNISTIC_LOOKBACK_DAYS = 8
# Below this many archived past-price points in the lookback window, the
# percentile is too thin to trust — opportunistic top-up stays off until
# the archive matures (degrades safe, same idea as the calibration guards;
# ~2 days of 15-minute slots).
OPPORTUNISTIC_MIN_SAMPLES = 192

# --- Expensive threshold (diagnostic only, no actuation) ---
# The mirror-image of the percentile above: the price at/above which a slot
# counts as "expensive" against the same last OPPORTUNISTIC_LOOKBACK_DAYS
# days of observed prices (see price_baseline.cheap_price_threshold, called
# with this percentile instead). Purely informational today — e.g. to
# decide "grid is expensive enough right now that a free-charging credit
# elsewhere would be worth more than charging at home". Live-tunable via
# the "Teuer-Schwelle (Perzentil)" number entity.
DEFAULT_EXPENSIVE_PERCENTILE = 90.0

# How far back/forward from "now" to expand cycle occurrences when looking
# for the currently active target (schedule.find_active_occurrence). The
# backward half catches a just-passed, still-unmet deadline (the "charge
# anyway, deadline blown" fallback); the forward half only needs to reach
# the next occurrence, but a generous window costs nothing since expansion
# is pure in-memory arithmetic, not I/O.
ACTIVE_LOOKBACK_DAYS = 14
ACTIVE_LOOKAHEAD_DAYS = 60
# A missed deadline stays "active" (worth force-charging for) only up to
# this long past its own start — beyond that, chasing it stops making
# sense (the shift it was for has already happened) and the next
# occurrence takes over instead. Live-confirmed need: a "04:30" cycle
# still being pursued at 19:40 the same day (15h overdue, 36 points short)
# while a same-cycle-family occurrence for the next day was already due —
# without this, an unmet deadline blocked everything after it forever.
MISSED_DEADLINE_GRACE_HOURS = 4.0
# How far ahead the calendar entity expands occurrences for display when
# Home Assistant doesn't constrain the query itself (defensive cap).
CALENDAR_MAX_LOOKAHEAD_DAYS = 365

# --- Data-completeness / "wait for better price data" gating ---
# Tibber publishes the next day's prices sometime in the afternoon — before
# that, a target beyond "today" is only visible through a partial window
# (today's remaining hours), and committing to "cheapest so far" can mean
# charging through a merely-okay afternoon slot instead of a much cheaper
# overnight one that simply isn't known yet. Hold off actuating (not
# planning — the plan/estimate stays visible) while data is incomplete, as
# long as there's at least this many hours of slack beyond what's strictly
# required — i.e. waiting can't itself cause the deadline to be missed.
# Tunable: shrink it if data usually arrives sooner than assumed here.
DATA_WAIT_SAFETY_BUFFER_HOURS = 12.0
# With no eligible price slot known at all, keep waiting while at least this
# much slack (hours) remains before the deadline.
NO_DATA_MIN_SLACK_HOURS = 1.0

# Rolling per-slot price archive (planner_state.price_history) built from
# every price fetch this integration already makes — used to judge whether
# today's visible prices are unusually high vs. typical for this time of
# day, refining the wait-for-data decision above (see price_baseline.py).
# No dependency on any specific price-sensor entity; starts empty and
# matures over about a week.
PRICE_HISTORY_RETENTION_DAYS = 10
PRICE_HISTORY_LOOKBACK_DAYS = 7
# Matched against the same minute-of-day across those days, +/- this many
# minutes (~1.5 fifteen-minute slots) to allow for slight fetch-timing
# jitter without pulling in a meaningfully different hour.
PRICE_HISTORY_TIME_TOLERANCE_MINUTES = 22
# Need at least this many historical samples at a given time-of-day before
# trusting their median as "typical" — early on, the archive is too sparse.
PRICE_HISTORY_MIN_SAMPLES = 3
