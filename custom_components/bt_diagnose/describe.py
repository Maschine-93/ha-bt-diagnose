"""Reine Hilfsfunktionen: beschreibt eine Automations-Konfiguration (ohne HA-Abhaengigkeit)."""
from __future__ import annotations

import re
from typing import Any


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.split(",")]
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return []


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _describe(cfg: dict[str, Any], mine: set[str], relevant: set[str] | None = None,
              names: dict[str, str] | None = None, others: set[str] | None = None
              ) -> tuple[list[str], list[str], list[str], bool, bool, bool, bool, int]:
    """-> (Ausloeser-Text, Zeitausloeser, Aktionen-Text, acts_on_room, touches_mode, touches_temp,
          multi_room, Anzahl_fremder_Ausloeser)

    relevant = Entitaeten, deren Ausloeser fuer diesen Raum gezeigt werden (Raum selbst + gleicher Bereich).
    others   = Entitaeten ANDERER Better-Thermostat-Raeume (-> multi_room).
    """
    relevant = relevant if relevant is not None else set(mine)
    names = names or {}
    others = others or set()
    other_n = 0
    def nm(e: str) -> str:
        return names.get(e, e)
    trig_text: list[str] = []
    times: list[str] = []
    for t in _as_list_of_dicts(cfg.get("triggers", cfg.get("trigger"))):
        kind = t.get("trigger") or t.get("platform") or "?"
        if kind == "time":
            for at in _as_list(t.get("at")):
                if at and at[0].isdigit():
                    times.append(at)
                else:
                    trig_text.append("Zeitpunkt aus Hilfsentität")
        elif kind == "time_pattern":
            trig_text.append("zeitgesteuert (Intervall)")
        elif kind in ("state", "numeric_state"):
            ids = _as_list(t.get("entity_id"))
            rel = [e for e in ids if e in relevant]
            if rel:
                trig_text.append("Änderung von " + ", ".join(nm(e) for e in rel))
            else:
                other_n += 1
        elif kind == "homeassistant":
            trig_text.append("Home-Assistant-Start" if t.get("event") == "start" else "Home-Assistant-Ereignis")
        else:
            trig_text.append(str(kind))
    if times:
        uniq = sorted(dict.fromkeys(x[:5] for x in times))
        trig_text.insert(0, ("täglich um " + ", ".join(uniq) + " Uhr") if len(uniq) <= 3
                         else f"zeitgesteuert ({len(uniq)} Zeitpunkte)")
    acts, mode, temp = False, False, False
    actions: list[str] = []
    # Vorkommen der Raum-Entitaeten irgendwo in der Konfiguration (z. B. in einer for_each-Liste)
    mentions = any(isinstance(n, str) and n in mine for d in _walk(cfg) for n in d.values()) or any(
        isinstance(x, str) and x in mine for d in _walk(cfg) for v in d.values() if isinstance(v, list)
        for x in v)
    for node in _walk(cfg.get("actions", cfg.get("action"))):
        svc = node.get("action") or node.get("service")
        if not isinstance(svc, str) or "." not in svc:
            continue
        data = node.get("data") if isinstance(node.get("data"), dict) else {}
        target = node.get("target") if isinstance(node.get("target"), dict) else {}
        ids = set(_as_list(target.get("entity_id")) + _as_list(data.get("entity_id")) +
                  _as_list(node.get("entity_id")))
        templated = any("{{" in i or "{%" in i for i in ids)
        hit = (bool(ids & mine) or svc.startswith("better_thermostat.")
               or (templated and mentions and svc.startswith("climate.")))
        if not hit:
            continue
        acts = True
        if svc in ("climate.turn_off",):
            actions.append("schaltet aus"); mode = True
        elif svc == "climate.turn_on":
            actions.append("schaltet ein"); mode = True
        elif svc == "climate.set_hvac_mode":
            actions.append(f"setzt Modus „{data.get('hvac_mode', '?')}“"); mode = True
        elif svc == "climate.set_temperature":
            actions.append(f"setzt Temperatur {data.get('temperature', '?')} °C"); temp = True
        elif svc == "climate.set_preset_mode":
            pm = str(data.get("preset_mode", "?"))
            actions.append("setzt Preset (je nach Situation)" if "{{" in pm or "{%" in pm
                           else f"setzt Preset „{pm}“")
            temp = True
        elif svc.startswith("better_thermostat."):
            actions.append(f"ruft {svc} auf"); temp = True
        else:
            actions.append(f"ruft {svc} auf")
    # doppelte Eintraege entfernen, Reihenfolge behalten
    multi = bool(others) and any(isinstance(v, str) and v in others for d in _walk(cfg)
                                 for v in d.values())
    multi = multi or any(isinstance(x, str) and x in others for d in _walk(cfg)
                         for v in d.values() if isinstance(v, list) for x in v)
    return trig_text, times, list(dict.fromkeys(actions)), acts, mode, temp, multi, other_n


def _as_list_of_dicts(v: Any) -> list[dict[str, Any]]:
    if isinstance(v, dict):
        return [v]
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    return []


# --------------------------------------------------------------------------------------------
# Gegenpruefung einer Automation (statisch, nur gesichert falsche oder riskante Dinge)
# --------------------------------------------------------------------------------------------
_ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _static(v: Any) -> bool:
    return isinstance(v, (str, int, float)) and "{{" not in str(v) and "{%" not in str(v)


def check_config(cfg: dict[str, Any], mine_bt: set[str], mine_trv: set[str], ctx: dict[str, Any]
                 ) -> list[dict[str, Any]]:
    """Prueft eine Automation auf gesicherte Fehler/Risiken im Zusammenhang mit Better Thermostat.

    ctx = {"exists": callable(entity_id)->bool,
           "bt": {bt_entity_id: {"hvac_modes": [...], "preset_modes": [...], "min_temp": x, "max_temp": y}}}
    Jeder Eintrag: code, severity (warnung|hinweis), action (bool), title, explanation, recommendation, change.
    """
    issues: list[dict[str, Any]] = []
    seen: set[tuple] = set()

    def add(code, sev, action, title, expl, rec, change):
        key = (code, title)
        if key not in seen:
            seen.add(key)
            issues.append({"code": code, "severity": sev, "action": action, "title": title,
                           "explanation": expl, "recommendation": rec, "change": change})

    # 1) Entitaeten, die es nicht gibt
    for node in _walk(cfg):
        for e in _as_list(node.get("entity_id")):
            if _ENTITY_RE.match(e or "") and not ctx["exists"](e):
                add("cfg_missing_entity", "warnung", True, f"Entität „{e}“ existiert nicht",
                    f"Die Automation verwendet „{e}“, aber diese Entität gibt es nicht (mehr). Die Automation "
                    "läuft an dieser Stelle ins Leere oder bricht ab.",
                    f"LÖSUNG: Automation öffnen und „{e}“ durch die richtige Entität ersetzen. Oft wurde sie "
                    "umbenannt: unter Einstellungen › Geräte & Dienste › Entitäten nach dem alten Namen suchen.",
                    "Ja – sonst funktioniert dieser Teil der Automation nicht.")

    # 2) Dienstaufrufe auf Better Thermostat bzw. direkt auf das Ventil
    for node in _walk(cfg.get("actions", cfg.get("action"))):
        svc = node.get("action") or node.get("service")
        if not isinstance(svc, str) or not svc.startswith("climate."):
            continue
        data = node.get("data") if isinstance(node.get("data"), dict) else {}
        target = node.get("target") if isinstance(node.get("target"), dict) else {}
        ids = set(_as_list(target.get("entity_id")) + _as_list(data.get("entity_id")) +
                  _as_list(node.get("entity_id")))
        for e in ids & mine_bt:
            info = ctx["bt"].get(e) or {}
            modes = info.get("hvac_modes") or []
            hv = data.get("hvac_mode")
            if svc in ("climate.set_hvac_mode", "climate.set_temperature") and _static(hv) and modes \
                    and str(hv) not in modes:
                auto_hint = (" Better Thermostat unterstützt den Modus „auto“ nicht." if str(hv) == "auto" else "")
                add("cfg_bad_mode", "warnung", True, f"Modus „{hv}“ gibt es für {e} nicht",
                    f"Die Automation setzt den Modus „{hv}“, erlaubt sind aber nur: {', '.join(modes)}."
                    f"{auto_hint}",
                    f"LÖSUNG: In der Automation „{hv}“ durch einen erlaubten Modus ersetzen – zum Heizen "
                    "„heat“, zum Ausschalten „off“.", "Ja – sonst wird der Aufruf abgelehnt.")
            if svc == "climate.set_temperature":
                t = _num(data.get("temperature")) if _static(data.get("temperature")) else None
                lo, hi = _num(info.get("min_temp")), _num(info.get("max_temp"))
                if t is not None and lo is not None and hi is not None and not lo <= t <= hi:
                    add("cfg_bad_temp", "warnung", True, f"Temperatur {t:g} °C liegt außerhalb des Bereichs",
                        f"{e} akzeptiert nur {lo:g}–{hi:g} °C.",
                        f"LÖSUNG: In der Automation einen Wert zwischen {lo:g} und {hi:g} °C eintragen.",
                        "Ja – sonst wird der Aufruf abgelehnt.")
            if svc == "climate.set_preset_mode":
                pm = data.get("preset_mode")
                presets = info.get("preset_modes") or []
                if _static(pm) and presets and str(pm) not in presets:
                    add("cfg_bad_preset", "warnung", True, f"Preset „{pm}“ gibt es für {e} nicht",
                        f"Die Automation setzt das Preset „{pm}“, vorhanden sind: {', '.join(presets)}.",
                        f"LÖSUNG: Ein vorhandenes Preset eintragen (oder das Preset in Better Thermostat anlegen).",
                        "Ja – sonst wird der Aufruf abgelehnt.")
        for e in ids & mine_trv:
            add("cfg_direct_trv", "hinweis", False, f"Steuert das Ventil „{e}“ direkt",
                "Better Thermostat regelt dieses Ventil selbst. Direkte Änderungen am Ventil liest BT als "
                "Eingriff von außen – sie können überschrieben werden oder die Regelung durcheinanderbringen.",
                "Empfohlen: stattdessen das Better-Thermostat-Climate des Raums ansteuern (z. B. Preset oder "
                "Zieltemperatur dort setzen).", "Empfohlen – nicht zwingend.")
    return issues
