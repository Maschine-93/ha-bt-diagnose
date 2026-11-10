# Better Thermostat Diagnose

[Deutsche Version](README.de.md)

A read-only Home Assistant integration that tells you **why your [Better Thermostat](https://github.com/KartoffelToby/better_thermostat) heating turned off or misbehaves** – in plain language, with a concrete fix.

> **Status: prototype (0.9.x).** Tested so far only with Homematic IP and Aqara valves and Better Thermostat 1.9.x. Feedback with other valves is very welcome.

## What it does
For every Better Thermostat room it looks at the last hours/days and reports:

| Finding | Meaning |
|---|---|
| 🔴 Heating turned itself off | BT parks the valve at its minimum (e.g. 5 °C) when the room is warmer than the target and then reads its *own* value as "user turned it off" (option `no_off_system_mode`). Shows the fix – or "already fixed" if the option is off now. |
| 🟠 Valve ran in auto mode | The valve switched to its weekly program while BT shows "heat". BT does not support auto and ignores it. |
| ℹ️ Switched off by an automation | A scheduled or automated shutdown is attributed to the matching automation (time trigger or run shortly before). |
| 🟠 / ℹ️ Automations controlling the room | Finds every automation that controls a BT room or its valve and checks it: missing entities, invalid mode (e.g. `auto`), unknown preset, temperature out of range, direct valve control (recommendation only), errors in recent runs, and entries in *Settings › Repairs*. |
| ℹ️ Unavailable devices | Short gaps are summarised, frequent gaps get a hint. |

Every finding has: what happened, **what to do**, **whether a change is required**, and how certain the assessment is. Informational findings never raise an alarm; a finding only counts as "action required" when there is evidence *and* a concrete fix.

## Installation
**HACS:** add this repository as a custom repository (category *Integration*), install, restart Home Assistant, then *Settings › Devices & services › Add integration › Better Thermostat Diagnose*.
**Manual:** copy `custom_components/bt_diagnose` into your `config/custom_components` folder and restart.

## Usage
* Sensor `sensor.better_thermostat_diagnose_befunde` – number of findings that need action. Attribute `report` contains the full Markdown report.
* Selects *Raum* (room, detected automatically from your Better Thermostat entries) and *Zeitraum* (time range).
* Actions: `bt_diagnose.refresh` (update the sensor), `bt_diagnose.analyze` (returns the findings as response data).
* Dashboard: a Markdown card with `{{ state_attr('sensor.better_thermostat_diagnose_befunde','report') }}`, plus tiles for the two selects.

## Privacy and safety
Everything runs locally inside Home Assistant. The integration only **reads** (recorder history, automation configuration and traces, repair issues). It never changes settings, switches devices or calls external services.

## Known limitations
* Report texts are currently **German only** (translation planned).
* The origin of an event (user / automation) is only known for events since the integration started – the recorder does not store it. Older events are matched by pattern.
* Automation traces and usage search rely on internal Home Assistant interfaces that may change. If one is missing, only that part is skipped.
* Values that are templates at runtime are not judged.

## Development
`python3 tests/test_analyzer.py` runs the unit tests of the analysis logic (no Home Assistant needed).

## License
MIT
