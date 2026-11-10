"""Auswahlfelder: Raum (automatisch aus den vorhandenen Better-Thermostat-Eintraegen) und Zeitraum."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN

ALL = "Alle Räume"
SPANS = {"24 Stunden": 24, "3 Tage": 72, "7 Tage": 168, "30 Tage": 720}


async def async_setup_entry(hass, entry, async_add_entities):
    mgr = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([RoomSelect(mgr), SpanSelect(mgr)])


class _Base(SelectEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, mgr) -> None:
        self._mgr = mgr
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, DOMAIN)}, name="Better Thermostat Diagnose")

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._mgr.add_listener(self._changed))
        last = await self.async_get_last_state()
        if last and last.state in self.options:
            self._restore(last.state)

    @callback
    def _changed(self) -> None:
        self.async_write_ha_state()


class RoomSelect(_Base):
    _attr_name = "Raum"
    _attr_icon = "mdi:radiator"

    def __init__(self, mgr) -> None:
        super().__init__(mgr)
        self._attr_unique_id = f"{DOMAIN}_raum"

    @property
    def options(self) -> list[str]:
        names = self._mgr.room_names or [r["name"] for r in self._mgr.rooms()]
        return [ALL, *names]

    @property
    def current_option(self) -> str:
        return self._mgr.selected_room or ALL

    def _restore(self, value: str) -> None:
        self._mgr.selected_room = None if value == ALL else value

    async def async_select_option(self, option: str) -> None:
        await self._mgr.async_run(room=None if option == ALL else option)


class SpanSelect(_Base):
    _attr_name = "Zeitraum"
    _attr_icon = "mdi:calendar-clock"
    _attr_options = list(SPANS)

    def __init__(self, mgr) -> None:
        super().__init__(mgr)
        self._attr_unique_id = f"{DOMAIN}_zeitraum"

    @property
    def current_option(self) -> str:
        for k, v in SPANS.items():
            if v == self._mgr.hours:
                return k
        return "3 Tage"

    def _restore(self, value: str) -> None:
        self._mgr.hours = SPANS[value]

    async def async_select_option(self, option: str) -> None:
        await self._mgr.async_run(hours=SPANS[option])
