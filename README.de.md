# Better Thermostat Diagnose

[English version](README.md)

Eine rein lesende Home-Assistant-Integration, die in Klartext erklärt, **warum deine [Better-Thermostat](https://github.com/KartoffelToby/better_thermostat)-Heizung ausgegangen ist oder sich seltsam verhält** – mit konkreter Lösung.

> **Status: Prototyp (0.9.x).** Bisher nur mit Homematic-IP- und Aqara-Ventilen und Better Thermostat 1.9.x getestet. Rückmeldungen mit anderen Ventilen sind willkommen.

## Was sie macht
Für jeden Better-Thermostat-Raum wertet sie die letzten Stunden/Tage aus und meldet:
* 🔴 **Heizung hat sich selbst ausgeschaltet** (Parkwert-Fehler mit der Option `no_off_system_mode`) – mit Lösung oder „inzwischen behoben".
* 🟠 **Ventil lief im Auto-Modus** (Wochenprogramm), den BT ignoriert.
* ℹ️ **Von einer Automation ausgeschaltet** – zugeordnet über Zeitauslöser oder einen Lauf kurz davor.
* **Automationen, die den Raum steuern** – jede wird geprüft: fehlende Entitäten, ungültiger Modus (z. B. `auto`), unbekanntes Preset, Temperatur außerhalb des Bereichs, direkte Ventilsteuerung (nur Empfehlung), Fehler in den letzten Läufen und Einträge unter *Einstellungen › Reparaturen*. Anzeige: ✅ alles okay / ℹ️ Info / 🔴 Problem – mit aufklappbaren Details.
* ℹ️ **Nicht erreichbar** – kurze Aussetzer zusammengefasst, häufige mit Hinweis.

Jeder Befund nennt: was passiert ist, **was zu tun ist**, **ob eine Änderung nötig ist** und wie sicher die Einschätzung ist. Handlungsbedarf gibt es nur bei Beleg **und** konkreter Lösung.

## Installation
**HACS:** Repository als benutzerdefiniertes Repository (Kategorie *Integration*) hinzufügen, installieren, Home Assistant neu starten, dann *Einstellungen › Geräte & Dienste › Integration hinzufügen › Better Thermostat Diagnose*.
**Manuell:** `custom_components/bt_diagnose` nach `config/custom_components` kopieren, neu starten.

## Bedienung
Sensor `sensor.better_thermostat_diagnose_befunde` (Zahl der Befunde mit Handlungsbedarf, Attribut `report` = Markdown-Bericht), Auswahlfelder *Raum* und *Zeitraum*, Aktionen `bt_diagnose.refresh` und `bt_diagnose.analyze`. Dashboard: Markdown-Karte mit `{{ state_attr('sensor.better_thermostat_diagnose_befunde','report') }}`.

## Datenschutz und Sicherheit
Alles läuft lokal in Home Assistant. Die Integration **liest** nur (Verlauf, Automationen, Traces, Reparaturen) und ändert oder schaltet nichts.

## Bekannte Grenzen
Berichte derzeit nur auf Deutsch. Der Auslöser eines Ereignisses ist nur seit dem Start der Integration bekannt. Trace- und Verwendungssuche nutzen interne Home-Assistant-Schnittstellen. Werte aus Vorlagen werden nicht beurteilt.

## Lizenz
MIT
