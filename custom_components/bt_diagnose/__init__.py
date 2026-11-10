"""Better Thermostat Diagnose - erklaert, warum eine BT-Heizung ausgegangen ist (nur lesend)."""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components.recorder import get_instance, history
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .analyzer import Finding, Point, analyze
from .automations import async_collect
from .const import BT_DOMAIN, DEFAULT_HOURS, DOMAIN, REFRESH_MINUTES

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor", "select"]
SERVICE_SCHEMA = vol.Schema({vol.Optional("hours"): vol.All(vol.Coerce(int), vol.Range(min=1, max=720))})


class DiagManager:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.findings: list[dict[str, Any]] = []
        self.report = "Noch keine Auswertung."
        self.problem_count = 0
        self.hours = DEFAULT_HOURS
        self.selected_room: str | None = None     # None = alle Raeume
        self.room_names: list[str] = []
        self.updated: str | None = None
        self._listeners: list = []
        # Live-Puffer: Ausloeser (Nutzer/Automation) kennt der Recorder-Verlauf nicht
        self._ctx: dict[str, deque] = defaultdict(lambda: deque(maxlen=400))
        self._watched: set[str] = set()

    # ---- Listener fuer Sensor ----
    def add_listener(self, cb):
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    # ---- Live-Kontext sammeln ----
    @callback
    def on_state_changed(self, event) -> None:
        eid = event.data.get("entity_id")
        if eid not in self._watched:
            return
        new = event.data.get("new_state")
        if new is None:
            return
        c = new.context
        self._ctx[eid].append((new.last_updated, c.user_id, c.parent_id))

    def _ctx_for(self, eid: str, t: datetime) -> tuple[bool, str | None, str | None]:
        for ts, uid, pid in reversed(self._ctx.get(eid, ())):
            if abs((ts - t).total_seconds()) < 0.01:
                return True, uid, pid
        return False, None, None

    # ---- Raeume finden ----
    def rooms(self) -> list[dict[str, Any]]:
        reg = er.async_get(self.hass)
        out = []
        for entry in self.hass.config_entries.async_entries(BT_DOMAIN):
            climates = [e.entity_id for e in er.async_entries_for_config_entry(reg, entry.entry_id)
                        if e.domain == "climate"]
            if not climates:
                continue
            members = [m for m in (entry.data.get("thermostat") or []) if isinstance(m, dict)]
            trvs = [m.get("trv") for m in members if m.get("trv")]
            out.append({"name": entry.title, "bt": climates[0], "trvs": trvs, "members": members})
        return out

    async def async_run(self, hours: int | None = None, room: str | None = ...) -> list[dict[str, Any]]:
        hours = hours or self.hours
        if room is not ...:
            self.selected_room = room
        all_rooms = self.rooms()
        self.room_names = [r["name"] for r in all_rooms]
        rooms = [r for r in all_rooms if self.selected_room in (None, r["name"])]
        rooms_all = all_rooms
        self._watched = {r["bt"] for r in rooms} | {t for r in rooms for t in r["trvs"]}
        end = dt_util.utcnow()
        start = end - timedelta(hours=hours)
        ids = sorted(self._watched)
        raw: dict[str, list] = {}
        if ids:
            raw = await get_instance(self.hass).async_add_executor_job(
                lambda: history.get_significant_states(
                    self.hass, start, end, ids, include_start_time_state=True,
                    significant_changes_only=False, minimal_response=False, no_attributes=False))
        result: list[dict[str, Any]] = []
        all_findings: list[Finding] = []
        for r in rooms:
            bt_pts = self._points(r["bt"], raw.get(r["bt"], []))
            trv_id = r["trvs"][0] if r["trvs"] else None
            trv_pts = self._points(trv_id, raw.get(trv_id, [])) if trv_id else []
            live = self.hass.states.get(trv_id) if trv_id else None
            info = {}
            if live is not None:
                info = {"min_temp": live.attributes.get("min_temp"),
                        "hvac_modes": live.attributes.get("hvac_modes")}
            try:
                others = {e for o in rooms_all if o is not r for e in (o["bt"], *o["trvs"])}
                autos = await async_collect(self.hass, [r["bt"], *r["trvs"]], hours, others)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Automations-Auswertung fehlgeschlagen")
                autos = []
            fs = analyze(bt_pts, trv_pts, r["members"], r["name"], trv_id or "", info, autos)
            fs.sort(key=lambda f: (f.when is not None, f.when or dt_util.utcnow()), reverse=True)
            all_findings += fs
            result.append({"room": r["name"], "bt": r["bt"], "trv": trv_id,
                           "findings": [f.as_dict() for f in fs]})
        self.hours = hours
        self.updated = dt_util.now().isoformat(timespec="seconds")
        self.findings = [dict(fd, room=res["room"]) for res in result for fd in res["findings"]]
        self.problem_count = sum(1 for f in all_findings if f.action)
        self.report = self._markdown(result, hours)
        self._notify()
        return result

    def _points(self, eid: str | None, states: list) -> list[Point]:
        pts: list[Point] = []
        for s in states:
            has, uid, pid = self._ctx_for(eid, s.last_updated) if eid else (False, None, None)
            pts.append(Point(s.last_updated, s.state, dict(s.attributes), uid, pid, has))
        return pts

    def _markdown(self, result: list[dict[str, Any]], hours: int) -> str:
        scope = self.selected_room or "alle Räume"
        span = f"{hours // 24} Tage" if hours >= 48 and hours % 24 == 0 else f"{hours} h"
        todo = [(r["room"], f) for r in result for f in r["findings"] if f["action"]]
        lines = []
        if todo:
            lines.append(f"## 🔴 Handlungsbedarf: {len(todo)}")
            for room, f in todo:
                lines.append(f"- **{room}:** {f['title']}")
        else:
            lines.append("## ✅ Kein Handlungsbedarf")
            lines.append("Es gibt aktuell nichts, was du tun müsstest.")
        lines += ["", f"*{len(self.room_names)} Better-Thermostat-Räume erkannt · Auswahl: {scope} · "
                      f"Zeitraum: {span} · Stand {self.updated[11:16] if self.updated else '-'} Uhr*", ""]
        lines += ["| Raum | Status | Zu tun | Nur Info |", "|---|---|:-:|:-:|"]
        for res in result:
            act = [f for f in res["findings"] if f["action"]]
            st = "🔴" if any(f["severity"] == "problem" for f in act) else "🟠" if act else "✅"
            n_info = sum(1 for f in res["findings"] if not f["action"] and f["code"] != "auto_info")
            lines.append(f"| {res['room']} | {st} | {len(act) or '–'} | {n_info or '–'} |")
        lines.append("")
        for res in result:
            lines.append(f"## {res['room']}")
            autos = [f for f in res["findings"] if f["code"] == "auto_info"]
            subjects = {f["subject"] for f in autos}
            # Befunde, die zu einer Automation gehoeren, stehen aufklappbar bei dieser Automation
            act = [f for f in res["findings"] if f["action"] and f["subject"] not in subjects]
            info = [f for f in res["findings"] if not f["action"] and f["code"] != "auto_info"
                    and f["subject"] not in subjects]
            if not res["findings"]:
                lines += ["✅ Keine Auffälligkeiten.", ""]
            if autos:
                lines.append("### Automationen für diesen Raum")
                for f in autos:
                    mine_f = [x for x in res["findings"] if x["subject"] == f["subject"]
                              and x["code"] != "auto_info"]
                    problem = any(x["action"] for x in mine_f)
                    st = "🔴 Problem entdeckt" if problem else "ℹ️ Info entdeckt" if mine_f else "✅ alles okay"
                    lines += [f"**{f['title']}** – {st}", ""]
                    if mine_f:
                        opened = " open" if problem else ""
                        lines += [f"<details{opened}><summary><b>{'🔴 Problem anzeigen' if problem else 'ℹ️ Info anzeigen'}"
                                  "</b></summary>", ""]
                        for x in sorted(mine_f, key=lambda y: y["when"] or "", reverse=True):
                            lines += self._finding_md(x)
                        lines += ["</details>", ""]
            if act:
                lines.append("### Zu tun")
                for f in sorted(act, key=lambda x: x["when"] or "", reverse=True):
                    lines += self._finding_md(f)
            if info:
                lines.append("### Zur Information (kein Handlungsbedarf)")
                for f in sorted(info, key=lambda x: x["when"] or "", reverse=True):
                    lines += self._finding_md(f)
        if not result:
            lines.append("Keine Better-Thermostat-Räume gefunden.")
        return "\n".join(lines)

    @staticmethod
    def _finding_md(f: dict[str, Any]) -> list[str]:
        icon = {"problem": "🔴", "warnung": "🟠", "hinweis": "🟡", "info": "ℹ️"}.get(f["severity"], "")
        when = ""
        if f["when"]:
            when = f"`{dt_util.as_local(datetime.fromisoformat(f['when'])):%d.%m. %H:%M}` "
        out = [f"{icon} {when}**{f['title']}**  "]
        if f["explanation"]:
            out.append(f"> **Was ist passiert:** {f['explanation']}  ")
        if f["recommendation"]:
            out.append(f"> **{'Lösung' if f['action'] else 'Was tun'}:** "
                       f"{f['recommendation'].replace('LÖSUNG: ', '')}  ")
        if f.get("change"):
            out.append(f"> **Änderung nötig?** {f['change']}  ")
        if f.get("certainty"):
            out.append(f"> *Sicherheit der Einschätzung: {f['certainty']}*")
        out.append("")
        return out


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    mgr = DiagManager(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = mgr
    entry.async_on_unload(hass.bus.async_listen(EVENT_STATE_CHANGED, mgr.on_state_changed))

    async def _analyze(call: ServiceCall):
        res = await mgr.async_run(call.data.get("hours"))
        return {"rooms": res}

    async def _refresh(call: ServiceCall) -> None:
        await mgr.async_run(call.data.get("hours"))

    hass.services.async_register(DOMAIN, "analyze", _analyze, schema=SERVICE_SCHEMA,
                                 supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "refresh", _refresh, schema=SERVICE_SCHEMA)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _periodic(_now) -> None:
        try:
            await mgr.async_run(mgr.hours)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Diagnose-Lauf fehlgeschlagen")

    entry.async_on_unload(async_track_time_interval(hass, _periodic, timedelta(minutes=REFRESH_MINUTES)))
    # erster Lauf erst, wenn HA komplett gestartet ist (vorher sind die Ventile noch 'unavailable'
    # und liefern keine min_temp)
    async def _first(_hass) -> None:
        await _periodic(None)

    entry.async_on_unload(async_at_started(hass, _first))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        hass.services.async_remove(DOMAIN, "analyze")
        hass.services.async_remove(DOMAIN, "refresh")
    return ok
