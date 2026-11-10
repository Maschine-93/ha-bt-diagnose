"""Reine Analyse-Logik (ohne Home-Assistant-Abhaengigkeit, daher einzeln testbar).

Leitlinie: Ein Befund wird nur dann als Problem/Warnung (= Handlungsbedarf) gemeldet, wenn
  1. ein Beleg im Verlauf vorliegt UND
  2. es eine konkrete Loesung gibt, die der Nutzer jetzt umsetzen kann.
Alles andere ist eine Information ohne Handlungsbedarf. Jeder Befund nennt, wie sicher er ist.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

PARK_WINDOW = timedelta(seconds=180)
AUTO_MIN = timedelta(seconds=60)
UNAVAIL_MIN = timedelta(seconds=120)
SCHEDULE_TOLERANCE_MIN = 5          # gleiche Uhrzeit +- 5 min an >= 2 Tagen = Zeitplan

SEV_ORDER = {"problem": 0, "warnung": 1, "hinweis": 2, "info": 3}


@dataclass
class Point:
    t: datetime
    state: str
    attrs: dict[str, Any] = field(default_factory=dict)
    user_id: str | None = None
    parent_id: str | None = None
    has_context: bool = False   # Recorder kennt den Ausloeser nicht; der Live-Puffer schon


@dataclass
class TraceInfo:
    start: datetime
    result: str | None = None      # finished | error | aborted | failed_conditions | ...
    error: str | None = None
    last_step: str | None = None
    trigger: str | None = None


@dataclass
class AutoInfo:
    """Eine Automation, die diesen Raum (Better Thermostat oder Ventil) betrifft."""
    entity_id: str
    name: str
    state: str = "on"                       # on | off | unavailable
    last_triggered: datetime | None = None
    trigger_times: list[str] = field(default_factory=list)   # z. B. "20:00:00" (Zeitausloeser)
    triggers_text: list[str] = field(default_factory=list)
    actions_text: list[str] = field(default_factory=list)
    touches_mode: bool = False              # setzt HVAC-Modus (an/aus/heat) direkt
    touches_temp: bool = False              # setzt Temperatur/Preset direkt
    multi_room: bool = False                # steuert auch andere Raeume
    issues: list[dict[str, Any]] = field(default_factory=list)   # Ergebnis der Gegenpruefung
    repairs: list[dict[str, Any]] = field(default_factory=list)  # Eintraege aus "Einstellungen > Reparaturen"
    other_triggers: int = 0                 # Ausloeser, die andere Geraete/Raeume betreffen
    traces: list[TraceInfo] = field(default_factory=list)


@dataclass
class Finding:
    severity: str            # problem | warnung (=Handlungsbedarf) | hinweis | info
    code: str
    when: datetime | None
    title: str
    explanation: str          # was ist passiert / warum
    recommendation: str = ""  # konkrete Loesung ("Nichts zu tun" bei info)
    certainty: str = ""       # sicher | wahrscheinlich | unklar
    action: bool = False      # True = Nutzer sollte etwas tun
    change: str = ""          # "Ja – ..." / "Nein" / "Empfohlen – ...": muss etwas geaendert werden?
    subject: str = ""         # Name der betroffenen Automation (falls es um eine geht)

    def as_dict(self) -> dict[str, Any]:
        return {"severity": self.severity, "code": self.code,
                "when": self.when.isoformat() if self.when else None,
                "title": self.title, "explanation": self.explanation,
                "recommendation": self.recommendation, "certainty": self.certainty,
                "action": self.action, "change": self.change, "subject": self.subject}


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _at(points: list[Point], t: datetime) -> Point | None:
    last = None
    for p in points:
        if p.t <= t:
            last = p
        else:
            break
    return last


def _origin(p: Point) -> str:
    if not p.has_context:
        return "unknown"
    if p.user_id:
        return "user"
    if p.parent_id:
        return "automation"
    return "internal"


def _no_off_on(members: list[dict[str, Any]]) -> bool:
    return any((m.get("advanced") or {}).get("no_off_system_mode") for m in members)


def analyze(bt: list[Point], trv: list[Point], members: list[dict[str, Any]] | None = None,
            name: str = "", trv_name: str = "", trv_info: dict[str, Any] | None = None,
            autos: list[AutoInfo] | None = None) -> list[Finding]:
    """`autos` = Automationen, die BT oder das Ventil dieses Raums ansprechen (optional).
    `trv_info` = statische Ventildaten aus dem AKTUELLEN Zustand (min_temp, hvac_modes);
    Home Assistant schreibt diese Climate-Attribute nicht in den Verlauf."""
    out: list[Finding] = []
    members, trv_info = members or [], trv_info or {}
    bt = sorted(bt, key=lambda p: p.t)
    trv = sorted(trv, key=lambda p: p.t)
    min_temp = _num(trv_info.get("min_temp"))
    opt_on = _no_off_on(members)

    # ---- 1) BT-Aus-Ereignisse einsammeln und erklaeren --------------------------------------
    offs: list[tuple[Point, Point]] = []
    prev = None
    for p in bt:
        if prev is not None and prev.state in ("heat", "auto", "heat_cool") and p.state == "off":
            offs.append((prev, p))
        prev = p

    unexplained: list[Point] = []
    autos = autos or []
    for before, off in offs:
        f = _explain_off(before, off, trv, min_temp, name, trv_name, opt_on)
        if f is None:
            f = _match_automation(off, autos)
        if f is None:
            unexplained.append(off)
        else:
            out.append(f)

    # ---- 2) Unerklaerte Abschaltungen: wiederkehrende Uhrzeit = Zeitplan ---------------------
    out += _schedule_or_single(unexplained, name)

    # ---- 3) Ventil im Auto-Modus ---------------------------------------------------------------
    start = None
    for p in trv:
        in_auto = p.state == "auto"
        bt_here = _at(bt, p.t)
        bt_heat = bt_here is not None and bt_here.state == "heat"
        if in_auto and bt_heat and start is None:
            start = p
        elif start is not None and not in_auto:
            if p.t - start.t >= AUTO_MIN:
                out.append(_auto_finding(start, p.t, name, trv_name))
            start = None
    if start is not None and trv and trv[-1].t - start.t >= AUTO_MIN:
        out.append(_auto_finding(start, trv[-1].t, name, trv_name, ongoing=True))

    # ---- 4) Nicht erreichbar (zusammengefasst) -------------------------------------------------
    out += _unavailable(bt, trv, name, trv_name)

    # ---- 4b) Automationen, die diesen Raum steuern (aufgeschluesselt + Fehler) ----------------
    out += _automation_findings(autos)

    # ---- 5) Einstellung (nur Information, nie ein Alarm) ---------------------------------------
    if opt_on and "off" in (trv_info.get("hvac_modes") or []) and not any(
            f.code == "off_parking_misread" for f in out):
        out.append(Finding(
            "info", "cfg_no_off_info", None,
            "Option „No off system mode“ ist an",
            "Better Thermostat schickt statt „Aus“ die Mindesttemperatur (z. B. 5 °C) ans Ventil. Das ist "
            "richtig, wenn das Ventil „Aus“ nicht zuverlässig befolgt (Beispiel: Aqara W600 öffnet trotz „Aus“ "
            "von selbst). Im Verlauf der letzten Zeit gab es damit keinen Fehler.",
            "Nichts zu tun. Nur wenn dein Ventil „Aus“ sauber umsetzt UND sich die Heizung ungewollt "
            "ausschaltet, die Option ausschalten (siehe Befund „Parkwert“ falls er erscheint).",
            "sicher", False))

    out.sort(key=lambda f: (0 if f.action else 1, SEV_ORDER.get(f.severity, 9)))
    return out


# --------------------------------------------------------------------------------------------
def _explain_off(prev: Point, off: Point, trv: list[Point], min_temp: float | None,
                 name: str, trv_name: str, opt_on: bool) -> Finding | None:
    """Gibt None zurueck, wenn der Grund unbekannt ist (wird dann gesammelt ausgewertet)."""
    origin = _origin(off)
    t = off.t
    stamp = f"{t.astimezone():%d.%m. %H:%M}"

    if origin == "user":
        return Finding("info", "off_by_user", t, f"Von dir ausgeschaltet ({stamp})",
                       "Das Ausschalten kam von einem Nutzer (App, Dashboard oder Sprachbefehl).",
                       "Nichts zu tun.", "sicher")
    if origin == "automation":
        return Finding("info", "off_by_automation", t, f"Von einer Automation ausgeschaltet ({stamp})",
                       "Das Ausschalten kam von einer Automation oder einem Skript.",
                       "Nichts zu tun, wenn gewollt. Sonst: Einstellungen › Automationen › die Automation "
                       "suchen, die zu dieser Zeit lief (Tab „Verlauf“).", "sicher")
    if off.attrs.get("window_open") or prev.attrs.get("window_open"):
        return Finding("info", "off_window", t, f"Fenster offen – Heizung aus ({stamp})",
                       "Das Fenster war offen, deshalb hat Better Thermostat abgeschaltet (Fensterautomatik).",
                       "Nichts zu tun.", "sicher")

    # Parkwert-Muster
    near = [p for p in trv if t - PARK_WINDOW <= p.t <= t]
    park = None
    if min_temp is not None:
        for p in near:
            tt = _num(p.attrs.get("temperature"))
            if tt is not None and tt <= min_temp + 0.01:
                park = p
                break
    if park is None:
        return None

    cur = _num(off.attrs.get("current_temperature"))
    tgt_before, tgt_after = _num(prev.attrs.get("temperature")), _num(off.attrs.get("temperature"))
    parts = [f"Das Ventil wurde {(t - park.t).total_seconds():.0f} s vor dem Ausschalten auf das Minimum "
             f"({min_temp:g} °C) gesetzt."]
    if cur is not None and tgt_after is not None and cur > tgt_after:
        parts.append(f"Better Thermostat macht das, wenn die Raumtemperatur ({cur:g} °C) über dem Ziel "
                     f"({tgt_after:g} °C) liegt, um das Ventil zu schließen.")
    if tgt_before is not None and tgt_after is not None and tgt_after < tgt_before:
        parts.append(f"Auslöser: Das Ziel wurde von {tgt_before:g} auf {tgt_after:g} °C gesenkt.")
    parts.append("Mit eingeschalteter Option „No off system mode“ liest BT diesen eigenen Wert als "
                 "„Nutzer hat ausgeschaltet“ und schaltet die Heizung ab – die Heizung bleibt dann aus, "
                 "bis jemand sie wieder einschaltet.")
    expl = " ".join(parts)
    certainty = "sicher" if off.has_context else "wahrscheinlich"
    if opt_on:
        return Finding(
            "problem", "off_parking_misread", t, f"Heizung hat sich selbst ausgeschaltet ({stamp})", expl,
            "LÖSUNG: Einstellungen › Geräte & Dienste › Better Thermostat › Eintrag dieses Raums › "
            "Konfigurieren › (Erweitert) › „No off system mode“ AUSSCHALTEN und speichern. "
            "Ausnahme: Wenn dein Ventil bei „Aus“ von selbst heizt (z. B. Aqara W600), Option anlassen – "
            "dann bitte melden, dies wäre ein anderer Fall.",
            certainty, True, "Ja – sonst schaltet sich die Heizung weiter selbst aus.")
    return Finding(
        "info", "off_parking_misread", t, f"Selbst ausgeschaltet – inzwischen behoben ({stamp})", expl,
        "Nichts zu tun: Die Option „No off system mode“ ist aktuell AUS. Falls es nach dem Umstellen "
        "erneut passiert, erscheint hier ein neuer Befund mit Handlungsbedarf.", certainty, False)


def _schedule_or_single(offs: list[Point], name: str) -> list[Finding]:
    """Unerklaerte Abschaltungen: gleiche Uhrzeit an >= 2 Tagen = vermutlich Zeitplan."""
    if not offs:
        return []
    groups: list[list[Point]] = []
    for p in sorted(offs, key=lambda x: x.t):
        mins = p.t.astimezone().hour * 60 + p.t.astimezone().minute
        for g in groups:
            gm = g[0].t.astimezone().hour * 60 + g[0].t.astimezone().minute
            if abs(gm - mins) <= SCHEDULE_TOLERANCE_MIN:
                g.append(p)
                break
        else:
            groups.append([p])
    out: list[Finding] = []
    for g in groups:
        days = {x.t.astimezone().date() for x in g}
        last = g[-1]
        hhmm = f"{last.t.astimezone():%H:%M}"
        if len(days) >= 2:
            out.append(Finding(
                "info", "off_recurring", last.t, f"Schaltet täglich um ca. {hhmm} aus",
                f"An {len(days)} verschiedenen Tagen ging die Heizung zur gleichen Uhrzeit aus. Das passt zu "
                "einer geplanten Abschaltung (Automation, Zeitplan oder Nachtabschaltung), nicht zu einem Fehler.",
                "Nichts zu tun, wenn du eine solche Abschaltung eingerichtet hast. Sonst: Einstellungen › "
                "Automationen › nach einer Automation suchen, die um diese Uhrzeit läuft.", "wahrscheinlich"))
        else:
            out.append(Finding(
                "hinweis", "off_unexplained", last.t,
                f"Heizung ging aus – Ursache nicht erkennbar ({last.t.astimezone():%d.%m. %H:%M})",
                "Es gibt keinen Hinweis auf Nutzer, Fenster oder das bekannte Parkwert-Muster, und der "
                "Auslöser wird im Verlauf nicht gespeichert.",
                "Wenn die Heizung jetzt wieder läuft und es einmalig war: nichts zu tun. Tritt es erneut auf, "
                "zeigt dieses Tool beim nächsten Mal den Auslöser an (er wird ab jetzt live mitgeschrieben). "
                "Zusätzlich: Einstellungen › System › Protokolle nach „better_thermostat“ zu dieser Uhrzeit "
                "durchsuchen.", "unklar"))
    return out


def _auto_finding(start: Point, end_t: datetime, name: str, trv_name: str, ongoing: bool = False) -> Finding:
    mins = int((end_t - start.t).total_seconds() // 60)
    return Finding(
        "warnung", "trv_auto_ignored", start.t,
        f"Ventil lief {mins} min im Auto-Modus{' (läuft noch)' if ongoing else ''}",
        "Das Ventil hat auf Auto/Wochenprogramm umgeschaltet, während Better Thermostat „Heizen“ anzeigt. "
        "BT unterstützt Auto nicht und ignoriert es – das Ventil folgt dann seinem eigenen Programm statt "
        "den BT-Zielwerten.",
        "LÖSUNG: Das Ventil wieder auf „Heizen“ stellen (Entität des Ventils › Modus „Heizen“) und das "
        "Wochenprogramm in der Zentrale/am Ventil deaktivieren, damit es nicht erneut umschaltet.",
        "sicher", True, "Ja – sonst folgt das Ventil weiter seinem eigenen Programm statt Better Thermostat.")


def _unavailable(bt: list[Point], trv: list[Point], name: str, trv_name: str) -> list[Finding]:
    def gaps(series: list[Point]) -> list[tuple[datetime, datetime]]:
        res, s = [], None
        for p in series:
            bad = p.state in ("unavailable", "unknown")
            if bad and s is None:
                s = p.t
            elif not bad and s is not None:
                if p.t - s >= UNAVAIL_MIN:
                    res.append((s, p.t))
                s = None
        return res

    g_bt, g_trv = gaps(bt), gaps(trv)
    both = [(a, b) for a, b in g_bt for c, d in g_trv if a < d and c < b]
    allg = sorted(set(g_bt) | set(g_trv))
    if not allg:
        return []
    longest = max((b - a) for a, b in allg)
    last = max(b for _, b in allg)
    mins = int(longest.total_seconds() // 60)
    if both and len(both) == len(g_bt):
        cause = ("Better Thermostat und das Ventil waren gleichzeitig weg – das spricht für einen "
                 "Home-Assistant-Neustart oder einen Ausfall des Funk-Hubs, nicht für ein defektes Gerät.")
    else:
        cause = "Das Gerät war zeitweise nicht erreichbar."
    if len(allg) >= 4:
        return [Finding("hinweis", "unavailable", last,
                        f"Häufige Aussetzer: {len(allg)}× nicht erreichbar (längster {mins} min)", cause,
                        "LÖSUNG: Funkverbindung prüfen – Abstand zu Hub/Repeater, Batterie des Ventils, "
                        "bei Zigbee/Homematic ein zusätzlicher Repeater in der Nähe.", "wahrscheinlich", True,
                        "Empfohlen – nur wenn die Aussetzer die Heizung merklich stören.")]
    return [Finding("info", "unavailable", last,
                    f"{len(allg)}× kurz nicht erreichbar (längster {mins} min)", cause,
                    "Nichts zu tun. Erst bei häufigen Aussetzern (4× oder mehr im Zeitraum) lohnt eine Prüfung.",
                    "sicher")]


# --------------------------------------------------------------------------------------------
# Automationen
# --------------------------------------------------------------------------------------------
def _hhmm_to_min(txt: str) -> int | None:
    try:
        h, m = txt.split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _match_automation(off: Point, autos: list[AutoInfo]) -> Finding | None:
    """Passt eine Automation zeitlich zu dieser Abschaltung? (Trace kurz davor oder Zeitausloeser)"""
    t = off.t
    local = t.astimezone()
    off_min = local.hour * 60 + local.minute
    best: tuple[AutoInfo, str, str] | None = None
    for a in autos:
        # nur Automationen, die tatsaechlich AUSschalten koennen (nicht solche, die nur "heat" setzen)
        if not a.touches_mode or not any("„off“" in x or "schaltet aus" in x for x in a.actions_text):
            continue
        for tr in a.traces:
            if timedelta(seconds=-5) <= t - tr.start <= timedelta(seconds=120):
                best = (a, f"Die Automation lief um {tr.start.astimezone():%H:%M:%S}, "
                           f"{(t - tr.start).total_seconds():.0f} s vor dem Ausschalten.", "wahrscheinlich")
                break
        if best:
            break
        for tt in a.trigger_times:
            m = _hhmm_to_min(tt)
            if m is not None and abs(m - off_min) <= SCHEDULE_TOLERANCE_MIN:
                best = (a, f"Die Automation hat einen Zeitauslöser um {tt[:5]} Uhr und setzt den Modus direkt.",
                        "wahrscheinlich")
                break
        if best:
            break
    if not best:
        return None
    a, why, cert = best
    return Finding(
        "info", "off_by_automation", t, f"Von Automation „{a.name}“ ausgeschaltet ({local:%d.%m. %H:%M})",
        f"{why} Better Thermostat regelt sonst selbst – diese Automation schaltet die Heizung gezielt aus.",
        "Nichts zu tun, wenn das gewollt ist (z. B. Nachtabschaltung). Sonst: Einstellungen › Automationen › "
        f"„{a.name}“ anpassen oder deaktivieren.", cert, False, "Nein", a.name)


def _automation_findings(autos: list[AutoInfo]) -> list[Finding]:
    out: list[Finding] = []
    for a in autos:
        last = max([tr.start for tr in a.traces] + ([a.last_triggered] if a.last_triggered else []),
                   default=None)
        errors = [tr for tr in a.traces if tr.result == "error" or tr.error]
        what = "; ".join(a.actions_text) or "Aktion unbekannt"
        # Jede betroffene Automation steht in der Liste; Details nur bei Auffaelligkeiten
        out.append(Finding("info", "auto_info", last, a.name, "", "", "sicher", False, "Nein", a.name))
        for rp in a.repairs:
            detail = f" („{rp['detail']}“)" if rp.get("detail") else ""
            out.append(Finding(
                "warnung", "auto_repair", None, f"{a.name}: Home Assistant meldet einen Fehler",
                f"Unter Einstellungen › Reparaturen steht ein Eintrag zu dieser Automation{detail} – meist "
                "verwendet sie eine Aktion oder Entität, die es nicht (mehr) gibt.",
                "LÖSUNG: Einstellungen › Reparaturen öffnen – dort steht genau, was fehlt. Danach die Automation "
                "öffnen und die Stelle korrigieren (oder die Automation löschen, wenn sie nicht mehr gebraucht wird).",
                "sicher", True, "Ja – sonst läuft der betroffene Teil der Automation nicht.", a.name))
        for it in a.issues:
            out.append(Finding(it["severity"], it["code"], None, f"{a.name}: {it['title']}", it["explanation"],
                               it["recommendation"], "sicher", it["action"], it["change"], a.name))
        if a.state == "unavailable":
            out.append(Finding(
                "warnung", "auto_unavailable", last, f"Automation „{a.name}“ ist nicht verfügbar",
                "Die Automation hat einen Konfigurationsfehler und läuft deshalb gar nicht. "
                f"Sie sollte diesen Raum steuern ({what}).",
                "LÖSUNG: Einstellungen › Automationen › diese Automation öffnen: Die rote Fehlermeldung nennt "
                "die Ursache, meist eine nicht mehr vorhandene Entität oder ein Tippfehler im YAML. "
                "Beheben und speichern.", "sicher", True, "Ja – sonst läuft die Automation gar nicht.", a.name))
            continue
        if errors:
            e = errors[0]
            msg = (e.error or "Ausführung mit Fehler beendet").strip()
            out.append(Finding(
                "warnung", "auto_errors", e.start,
                f"Automation „{a.name}“ hat Fehler ({len(errors)} von {len(a.traces)} Läufen)",
                f"Letzter Fehler am {e.start.astimezone():%d.%m. %H:%M}: „{msg}“"
                + (f" (Schritt {e.last_step})" if e.last_step else "")
                + f". Die Automation steuert diesen Raum: {what}.",
                "LÖSUNG: Einstellungen › Automationen › „" + a.name + "“ › Verläufe: den Lauf mit dem "
                "Fehler öffnen – der markierte Schritt zeigt, was fehlschlägt. Häufige Ursachen: Entität "
                "existiert nicht mehr, Gerät war nicht erreichbar, falscher Wert in der Aktion.",
                "sicher", True, "Ja – sonst steuert die Automation diesen Raum nicht richtig.", a.name))
            continue
    return out
