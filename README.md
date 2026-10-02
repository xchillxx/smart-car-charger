# Smart Car Charger

Home Assistant custom integration that charges an EV in one of two ways,
selectable at any time (`Regler`): **price-optimized** (cheapest Tibber
slots up to a target) or **surplus smart** (PV-surplus current control that
keeps the home battery from discharging — see below). *(Formerly "Spot
Charge Scheduler"; the integration domain and entity IDs are unchanged.)*

The price-optimized mode works with a target SoC/time and a repeating cycle — e.g. "65% charged by 04:30, every
4 days" for a shift schedule. Picks the cheapest 15-minute price slots that
still meet the deadline; if time runs out, it automatically shifts to
charging every remaining slot so the deadline is still met, price be damned.

Cycles are a fixed set of **editable slots** — each one a handful of native
entities (on/off, name, target SoC, time, rhythm) you change straight on
the dashboard. No calendar editing, no service calls.

It's not Tesla- or Tibber-specific by design: every controlled/read entity
(charge switch, SoC sensor, etc.) is picked in the config flow, so it works
with any vehicle integration that exposes a start/stop switch and a SoC
sensor. Today it only reads prices from Tibber's `tibber.get_prices`
service (15-minute resolution, full day-ahead forecast — not the same as
the `sensor.*_electricity_price` state, which only shows the current
price). A generic day-ahead/EPEX price source for non-Tibber households can
be added later behind the same `price_source.py` interface.

## What it does

- Charge targets are **6 fixed cycle slots**. Each slot is five native
  entities you edit directly on the dashboard: an on/off switch, a name, a
  target-SoC number, a time, and a rhythm-in-days number (`0` = one-off).
  Fill as many as you need — two shift patterns plus a few spares in
  practice. Nothing else to set up, no calendar editing, no services.
- All slots run at once; the integration always plans for whichever slot's
  next deadline comes first. A `0`-day slot is a one-off for an ad-hoc
  "I have to leave early tomorrow".
- The rhythm counts **from now**: switching a slot on (or changing its
  rhythm) sets "next in N days". To pause a slot for a holiday, just switch
  it off — every setting is kept and it resumes cleanly when you switch it
  back on.
- The **calendar** entity is a read-only month overview of what's coming
  up; you don't edit anything there.
- It fetches Tibber's day-ahead 15-minute prices for the window up to
  whichever cycle's deadline is currently active, and schedules the
  cheapest slots that add up to enough charging time — preferring
  contiguous blocks over switching on/off every 15 minutes to chase a
  fraction-of-a-cent difference (gaps of up to 30 min between two selected
  slots get bridged regardless of that gap's own price; relay wear costs
  more than the odd cent).
- If the deadline is close enough that being picky about price would miss
  it, it automatically schedules (near-)continuous charging instead —
  meeting the target always wins over saving money.
- **Opportunistic top-up beyond the target.** Point the config at your
  car's own charge-limit entity (e.g. `number.model_3_charge_limit`) and,
  once the guaranteed target SoC is covered, it will *also* grab any
  remaining slots that are genuinely cheap — priced at or below the Nth
  percentile (default 10, live-adjustable via the "Billig-Schwelle
  (Perzentil)" number; the "Billig-Schwelle" sensor shows what that
  percentile currently is in ct/kWh) of every price *observed over the
  last 8 days* —
  up to that car limit. This is never forced: it never pushes out the
  guaranteed completion time, the past-deadline "charge no matter what"
  fallback still only ever drives to the cycle target, and it stays fully
  inert until you configure the entity and the price archive has a couple
  of days of history. So a cycle target of "50% by 04:00" plus a car limit
  of 80% means *always at least 50%*, and *80% whenever the night is
  actually cheap*.
- Won't jump on today's cheapest-looking slots if the price data doesn't
  cover the full window yet (e.g. a target tomorrow morning, checked
  before tomorrow's prices are published) — it waits for fuller data
  first, as long as waiting can't itself risk the deadline. It also builds
  its own rolling week of observed prices to judge whether today's visible
  prices are unusually high for this time of day vs. that history, and
  waits longer in that case even once *some* data is technically
  available; a below-typical price is charged on right away instead.
- Once the target SoC is reached, charging stops immediately regardless of
  the remaining schedule, and the next-earliest slot occurrence becomes
  the new active target. A deadline that's missed by a lot (more than
  MISSED_DEADLINE_GRACE_HOURS, default 4h) is abandoned the same way
  instead of being chased forever — a shift's 04:30 target is no longer
  worth pursuing at 8 pm that evening if the next shift's own target is
  already coming up.
- Self-calibrating battery capacity **and** charging power estimates:
  instead of trusting the config-entered guesses, it derives real kWh
  capacity (energy added ÷ SoC delta) from your own charge sessions, and a
  robust (median-of-sessions) rolling estimate once it has enough of them.
  Charging power specifically uses each session's *peak* reading, then
  tracks the running MAXIMUM across sessions rather than a median — most
  sessions happen under PV-surplus charging (a separate system, throttled
  to whatever solar is available for that whole session, anywhere from
  ~1 kW up), so a median drifts down across a stretch of low-surplus days
  even though the hardware's real ceiling hasn't changed; the estimate
  only ever moves up when something genuinely faster is observed.
- A manual master switch. **This integration never auto-detects or
  auto-switches against a PV-surplus charging setup** — flip it on
  yourself once you've switched your wallbox/car out of solar-surplus mode
  for the season.

## Surplus-smart mode (`Regler`)

The `Regler` select picks who drives the charge switch: **Preis-Optimiert**
(the price planner above) or **Überschuss Smart**. Smart controls the car's
charging current from PV surplus — and runs regardless of the wallbox mode,
because the car-side current limit works below a wallbox's own PV mode too.

- Every **15 minutes** it averages the last **30 minutes** of PV production
  and house base load (house consumption minus the wallbox draw) and sets
  the current so that nothing is fed into the grid while the home battery
  keeps charging at a target rate that shrinks as it fills:
  `(100 − battery SoC %) × 2 kW`, e.g. 1 kW at 50 %, 0.2 kW at 90 %.
- Current is limited to **5–16 A** and only written when it changes. Below
  5 A (≈ 3.45 kW on three phases) the car holds the minimum for one window
  and **stops after two consecutive low windows**; it starts again as soon
  as a window shows enough surplus.
- An optional **fast loop** (currently disabled via `FAST_LOOP_ENABLED` in `smart_surplus.py`, to save vehicle-API commands) would run every minute while the car charges: it compares the
  last 3 minutes of `PV − house load` with the battery target and cuts the
  current **immediately** when the battery would discharge or the grid
  would be used (down to the 5 A minimum, then stop); it raises the current
  only after 5 minutes of clear surplus (> 1 kW). This is what makes
  it safe to leave the wallbox unregulated (e.g. fixed 11 kW) and let this
  integration be the only controller. Without it, a decision is taken
  immediately on plug-in and on a wallbox-mode change (`Smart Ampere` shows
  `befehle_heute`, the number of vehicle-API commands sent today).
- The sample buffer is rebuilt from the recorder history after a restart, so
  decisions don't wait for fresh data.
- Smart charges up to the car's own limit and ignores the cycle slots. It
  only acts while `Lademodus aktiv` is on and the car is plugged in and
  (if configured) at home. A manual change of the current between two
  decisions is not overridden.
- Needs four optional config entities: charging-current `number`, PV power,
  house consumption (incl. wallbox) and home-battery SoC sensors, plus the
  live charge-power sensor already used for calibration. `Smart Ampere` and
  `Smart Soll-Leistung` show the latest decision (with the inputs as
  attributes) in both modes, so it can be compared with reality before
  switching over.

## Installation

### HACS (custom repository)
1. HACS → Integrations → ⋮ → Custom repositories
2. Add this repository URL, category "Integration"
3. Install "Smart Car Charger", restart Home Assistant

### Manual
Copy `custom_components/spot_charge_scheduler` into your `config/custom_components/` folder and restart.

## Setup

Settings → Devices & Services → Add Integration → "Smart Car Charger". You'll be asked for:

| Field | Required | Notes |
|---|---|---|
| Charge start/stop switch | yes | e.g. `switch.model_3_charger` |
| SoC sensor | yes | e.g. `sensor.model_3_battery`, must be `%` |
| Charging status sensor | no | binary; drives the capacity calibrator's session detection |
| Plugged-in sensor | no | binary; without it, "not connected" is never checked |
| Energy added sensor | no | enables capacity self-calibration |
| Vehicle location tracker | no | `device_tracker.*`; paired with the zone below, only charges while the vehicle is actually there |
| Home zone | no | any `zone.*`, e.g. `zone.home` — requires the tracker above to have any effect |
| Assumed charging power (kW) | yes | starting value; self-calibrated over time once the sensor below is set |
| Live charging power sensor | no | e.g. `sensor.model_3_charger_power` — a real **power** sensor (kW), not a "rate" sensor (distance/hour); enables power self-calibration |
| Car charge-limit entity | no | a `number.*` holding the car's own target charge limit (e.g. `number.model_3_charge_limit`); enables opportunistic top-up beyond the cycle target while power is cheap vs. the last 8 days. Unset → feature off |
| Price source | yes | only "Tibber" today |
| Tibber home nickname | yes | as shown in the Tibber app, e.g. "Haus" |
| Battery capacity (kWh) | yes | starting estimate; overwritten automatically once enough real sessions are observed |
| Tankerkönig API key | no | free from [tankerkoenig.de](https://creativecommons.tankerkoenig.de/) — replaces the manual fallback price with the live cheapest-local one (hourly) |
| Fuel type | — | `e5` / `e10` / `diesel` for the comparison |
| Fuel-station search radius | — | km around your Home Assistant location (max 25) |
| Odometer sensor | no | `sensor.*` in km; with the next field, self-calibrates the EV's kWh/100 km from the socket |
| Charge-energy sensor | no | cumulative kWh delivered by the wallbox/socket (`total_increasing`) |
| Live electricity-price sensor | no | €/kWh; only for the "cheaper right now" comparison when no charge plan is active |

All fields are editable later via the integration's "Configure" option.

## Entities

| Entity | Type | Purpose |
|---|---|---|
| Slot N aktiv | `switch` | Cycle slot N on/off. Off = paused (settings kept). One per slot (N = 1…6) |
| Slot N Name | `text` | Free-form label, e.g. "Tagschicht" |
| Slot N Ziel-SoC | `number` | Target state of charge for that slot, 5–100 %, 5 % steps |
| Slot N Uhrzeit | `time` | Time of day the target must be reached by |
| Slot N Rhythmus (Tage) | `number` | Repeat every N days from activation; `0` = one-off |
| Ladeplan-Kalender | `calendar` | **Read-only** month overview of upcoming slot occurrences |
| Akkukapazität | `number` | Capacity used for planning; auto-overwritten by the calibrator |
| Ladeleistung | `number` | Charging power used for planning; auto-overwritten by the calibrator once a power sensor is set |
| Billig-Schwelle (Perzentil) | `number` | How cheap (percentile of the last 8 days' observed prices, default 10) a slot must be before opportunistic top-up takes it; no effect without a car charge-limit entity |
| Billig-Schwelle | `sensor` | What that percentile currently works out to, in ct/kWh, against the last 8 days — plus whether opportunistic top-up is active |
| Regler | `select` | `Preis-Optimiert` or `Überschuss Smart` (see above) |
| Smart Ampere / Smart Soll-Leistung | `sensor` | Latest surplus-smart decision (A / kW) with PV mean, base load, battery target, surplus, last action |
| Lademodus aktiv | `switch` | Master switch — only while on does this integration touch the charge switch |
| Ladeplan | `sensor` | Status (`kein_ziel`/`erreichbar`/`nicht_erreichbar`/`ziel_erreicht`/`opportunistisch`/`nicht_zuhause`/`wartet_auf_daten`) + attributes: active cycle, next slots, estimated cost, estimated completion, opportunistic-slot count, effective ceiling SoC, cheap-price threshold |
| Nächster Zyklus | `sensor` | Timestamp of the currently active target occurrence |
| Kalibrierte Kapazität | `sensor` | The calibrator's current capacity estimate + how many sessions it's based on |
| Kalibrierte Ladeleistung | `sensor` | The calibrator's current power estimate + how many sessions it's based on |

## Suggested dashboard

The integration only creates entities — build the view however you like.
This is the recommended layout: the calendar as a read-only overview, the
master switch, then one card per slot with **aktiv on top** (all six always
shown so an unused slot can still be switched on), and the opportunistic
top-up controls. Paste into a `type: sections` view (or adapt the cards for
a masonry view).

```yaml
type: sections
sections:
  - type: grid
    cards:
      - type: calendar
        title: Ladeplan-Kalender
        entities: [calendar.spot_charge_scheduler_ladeplan_kalender]
        initial_view: dayGridMonth
      - type: tile
        entity: switch.spot_charge_scheduler_lademodus_aktiv
        features: [{ type: toggle }]
  - type: grid
    cards:
      # repeat this card for slots 1..6
      - type: entities
        title: Slot 1
        entities:
          - entity: switch.spot_charge_scheduler_slot_1_aktiv
            name: aktiv (aus = pausiert)
          - entity: text.spot_charge_scheduler_slot_1_name
            name: Name
          - entity: number.spot_charge_scheduler_slot_1_ziel_soc
            name: Ziel-SoC
          - entity: time.spot_charge_scheduler_slot_1_uhrzeit
            name: Uhrzeit
          - entity: number.spot_charge_scheduler_slot_1_rhythmus_tage
            name: Rhythmus (Tage)
  - type: grid
    cards:
      - type: entities
        title: Opportunistisches Top-up
        entities:
          - entity: number.spot_charge_scheduler_billig_schwelle_perzentil
            name: Billig-Schwelle (Perzentil)
          - entity: sensor.spot_charge_scheduler_billig_schwelle
            name: = aktuell in ct/kWh
      - type: entities
        title: Diagnose
        entities:
          - sensor.spot_charge_scheduler_ladeplan
          - sensor.spot_charge_scheduler_nachster_zyklus
          - sensor.spot_charge_scheduler_kalibrierte_kapazitat
          - sensor.spot_charge_scheduler_kalibrierte_ladeleistung
```

## Combustion-engine comparison

Always on (it just needs the consumption numbers), refined by a (free)
Tankerkönig API key:

| Entity | Type | |
|---|---|---|
| Spritpreis | `number` | €/L used for the comparison. Set it by hand; with a Tankerkönig API key the integration overwrites it hourly with the cheapest local price (same as the auto-calibrated capacity/power numbers). Attrs: `quelle` (`manuell` / `tankerkoenig`), station, distance |
| Verbrenner-Break-even | `sensor` | **at/above how many ct/kWh the combustion car is cheaper per km.** Attrs: `guenstiger_jetzt` (eauto/verbrenner vs. the current spot price), ct/100 km each way, the assumptions used |
| Verbrenner-Verbrauch | `number` | combustion car's L/100 km |
| E-Auto-Verbrauch (ab Steckdose) | `number` | EV kWh/100 km incl. charging losses; auto-calibrated from the odometer + charge-energy statistics when both are configured (the battery's net state-of-charge change inside the window is subtracted, so a window that ends right after a charge doesn't read too high), otherwise the value set here |

`break-even (ct/kWh) = L/100 km × fuel ct/L ÷ EV kWh/100 km`.

## Upgrading from ≤ 0.12.0

The old model — cycles authored as calendar events, per-occurrence drag
overrides, `add_cycle` / `update_cycle` services, dynamic "Pausiert: …"
switches — is gone. On first start after the upgrade, every **recurring**
cycle you had is migrated into a slot (earliest first); bare one-off
calendar entries are dropped. Re-create any one-offs you still need in a
free slot with rhythm `0`.

## License

MIT
