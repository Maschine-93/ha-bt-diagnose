"""Sammelt Automationen, die ein Better-Thermostat-Climate oder dessen Ventil ansteuern.

Alles hier ist defensiv geschrieben: Die verwendeten Home-Assistant-Hilfsfunktionen (Verwendungs-
suche, Trace-Speicher, raw_config) sind interne, nicht garantierte Schnittstellen. Fehlt eine,
wird die Automations-Auswertung uebersprungen - die uebrige Diagnose laeuft weiter.
"""
from __future__ import annotations

import inspect
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .analyzer import AutoInfo, TraceInfo
from .describe import _describe, check_config

_LOGGER = logging.getLogger(__name__)


async def async_collect(hass: HomeAssistant, entity_ids: list[str], hours: int,
                        others: set[str] | None = None) -> list[AutoInfo]:
    try:
        from homeassistant.components import automation as auto_comp
    except Exception:  # noqa: BLE001
        return []
    mine = set(entity_ids)
    reg = er.async_get(hass)
    ids: set[str] = set()
    try:
        for e in entity_ids:
            ids.update(auto_comp.automations_with_entity(hass, e))
            ent = reg.async_get(e)
            if ent and ent.device_id:
                ids.update(auto_comp.automations_with_device(hass, ent.device_id))
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Verwendungssuche fuer Automationen nicht verfuegbar", exc_info=True)
        return []

    # Trace-Funktion (optional)
    list_traces = None
    try:
        from homeassistant.components.trace import async_list_traces as list_traces  # type: ignore
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Trace-Schnittstelle nicht verfuegbar")

    # Entitaeten, deren Ausloeser fuer diesen Raum relevant sind: der Raum selbst + gleicher Bereich
    dev_reg = dr.async_get(hass)

    def area_of(ent) -> str | None:
        if ent.area_id:
            return ent.area_id
        if ent.device_id:
            dev = dev_reg.async_get(ent.device_id)
            return dev.area_id if dev else None
        return None

    areas = {a for e in entity_ids if (ent := reg.async_get(e)) and (a := area_of(ent))}
    relevant = set(mine) | {x.entity_id for x in reg.entities.values() if area_of(x) in areas}
    names = {e: (st.name or e) for e in relevant if (st := hass.states.get(e))}

    bt_state = hass.states.get(entity_ids[0])
    ctx = {
        "exists": lambda e: hass.states.get(e) is not None or reg.async_get(e) is not None,
        "bt": {entity_ids[0]: {
            "hvac_modes": list(bt_state.attributes.get("hvac_modes") or []),
            "preset_modes": list(bt_state.attributes.get("preset_modes") or []),
            "min_temp": bt_state.attributes.get("min_temp"),
            "max_temp": bt_state.attributes.get("max_temp")}} if bt_state else {},
    }

    since = dt_util.utcnow() - timedelta(hours=hours)
    out: list[AutoInfo] = []
    for eid in sorted(ids):
        st = hass.states.get(eid)
        if st is None:
            continue
        info = AutoInfo(entity_id=eid, name=st.attributes.get("friendly_name") or eid, state=st.state)
        lt = st.attributes.get("last_triggered")
        if isinstance(lt, str):
            lt = dt_util.parse_datetime(lt)
        info.last_triggered = lt
        cfg = _raw_config(hass, eid)
        known = cfg is not None
        if known:
            tt, times, acts_txt, acts, mode, temp, multi, other_n = _describe(
                cfg, mine, relevant, names, others)
            info.triggers_text, info.trigger_times, info.actions_text = tt, times, acts_txt
            info.touches_mode, info.touches_temp = mode, temp
            info.multi_room, info.other_triggers = multi, other_n
            if not acts:
                continue          # liest den Raum nur (z. B. Protokoll-Automation) -> kein Einfluss
            try:
                info.issues = check_config(cfg, {entity_ids[0]}, set(entity_ids[1:]), ctx)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Gegenpruefung von %s fehlgeschlagen", eid, exc_info=True)
        info.repairs = _repairs(hass, eid)
        item_id = st.attributes.get("id")
        if item_id:
            try:
                for r in await _traces(hass, list_traces, str(item_id)):
                    ts = _ts((r.get("timestamp") or {}).get("start"))
                    if ts is None or ts < since:
                        continue
                    info.traces.append(TraceInfo(
                        ts, r.get("script_execution"), r.get("error"), r.get("last_step"),
                        r.get("trigger") if isinstance(r.get("trigger"), str) else None))
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Traces von %s nicht lesbar", eid, exc_info=True)
        out.append(info)
    return out


def _raw_config(hass: HomeAssistant, eid: str) -> dict[str, Any] | None:
    try:
        from homeassistant.components.automation import DATA_COMPONENT  # type: ignore
        comp = hass.data[DATA_COMPONENT]
    except Exception:  # noqa: BLE001
        try:
            comp = hass.data["automation"]
        except Exception:  # noqa: BLE001
            return None
    try:
        ent = comp.get_entity(eid)
        cfg = getattr(ent, "raw_config", None)
        return dict(cfg) if cfg else None
    except Exception:  # noqa: BLE001
        return None


def _ts(v: Any) -> datetime | None:
    """Zeitstempel aus dem Trace: intern datetime, ueber die API als Text."""
    if isinstance(v, datetime):
        return dt_util.as_utc(v)
    if isinstance(v, str):
        return dt_util.parse_datetime(v)
    return None


async def _traces(hass: HomeAssistant, list_traces, item_id: str) -> list[dict[str, Any]]:
    """Kurzformen der gespeicherten Traces einer Automation. Zwei Wege, falls einer wegfaellt."""
    if list_traces is not None:
        try:
            res = list_traces(hass, "automation", item_id)
            if inspect.isawaitable(res):
                res = await res
            return list(res or [])
        except Exception:  # noqa: BLE001
            _LOGGER.debug("async_list_traces fehlgeschlagen, versuche Direktzugriff", exc_info=True)
    # Weg 2: der Trace-Speicher selbst
    try:
        from homeassistant.components.trace.const import DATA_TRACE  # type: ignore
        store = hass.data.get(DATA_TRACE)
    except Exception:  # noqa: BLE001
        store = hass.data.get("trace")
    if not store:
        return []
    bucket = store.get(f"automation.{item_id}")
    if bucket is None:
        return []
    # TraceBuckets (neu): all_traces(); aeltere Versionen: dict run_id -> Trace
    if hasattr(bucket, "all_traces"):
        runs = list(bucket.all_traces())
    elif hasattr(bucket, "values"):
        runs = list(bucket.values())
    else:
        runs = []
    out = []
    for tr in runs:
        d = tr.as_short_dict() if hasattr(tr, "as_short_dict") else None
        if d:
            out.append(d)
    return out


def _repairs(hass: HomeAssistant, eid: str) -> list[dict[str, Any]]:
    """Eintraege aus 'Einstellungen > Reparaturen', die zu dieser Automation gehoeren."""
    out: list[dict[str, Any]] = []
    try:
        for issue in ir.async_get(hass).issues.values():
            if issue.domain != "automation":
                continue
            ph = issue.translation_placeholders or {}
            if eid in str(issue.issue_id) or ph.get("entity_id") == eid:
                detail = next((str(ph[k]) for k in ("service", "action", "entity") if ph.get(k)), "")
                out.append({"key": issue.translation_key, "detail": detail})
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Reparaturen nicht lesbar", exc_info=True)
    return out
