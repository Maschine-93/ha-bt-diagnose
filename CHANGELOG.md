# Changelog

## 0.9.1 (prototype)
- Diagnosis of Better Thermostat rooms: explains why a heating turned itself off (parking-value misread with `no_off_system_mode`), TRV in auto mode, unavailable devices.
- Finds automations that control a BT room or its valve and checks them (missing entities, invalid mode/preset/temperature, direct valve control, errors in recent runs, Home Assistant repair issues).
- Room and time-range selectors, Markdown report with collapsible details per automation.
