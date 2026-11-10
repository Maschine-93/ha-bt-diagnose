from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    mgr = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([BtDiagnoseSensor(mgr)])


class BtDiagnoseSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Befunde"
    _attr_icon = "mdi:stethoscope"
    _attr_should_poll = False
    _attr_native_unit_of_measurement = "Befunde"

    def __init__(self, mgr) -> None:
        self._mgr = mgr
        self._attr_unique_id = f"{DOMAIN}_befunde"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, DOMAIN)}, name="Better Thermostat Diagnose")

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._mgr.add_listener(self._changed))

    @callback
    def _changed(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self):
        return self._mgr.problem_count

    @property
    def extra_state_attributes(self):
        return {
            "report": self._mgr.report,
            "findings": self._mgr.findings,
            "hours": self._mgr.hours,
            "room_count": len(self._mgr.room_names),
            "rooms": self._mgr.room_names,
            "selected_room": self._mgr.selected_room or "Alle",
            "updated": self._mgr.updated,
        }
