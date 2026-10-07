# Ergometer

Verbindet sich per Bluetooth LE mit einem Christopeit AX 4000 Ergometer, zeigt die Messwerte live an und regelt auf Wunsch eine Zielleistung.

- Messwerte in der Konsole: Geschwindigkeit, Trittfrequenz, Leistung, Widerstand, Distanz, Energie, Puls, Zeit
- Cockpit im Browser mit Kacheln und Plotly-Verlaufskurven
- Zielleistung in Watt: ein PID-Regler im Programm stellt dazu die Widerstandsstufe des Ergometers nach

## Voraussetzungen

- macOS mit Bluetooth (das Terminal braucht die Bluetooth-Berechtigung unter Systemeinstellungen → Datenschutz & Sicherheit → Bluetooth)
- [uv](https://docs.astral.sh/uv/) – die Abhängigkeit `bleak` wird beim ersten Start automatisch installiert
- Internetverbindung für das Cockpit (Plotly wird von einem CDN geladen)

## Starten

```
cd ~/Ergometer
uv run ergometer.py --panel
```

Das Programm sucht das Ergometer, verbindet sich und öffnet das Cockpit unter http://127.0.0.1:8050/. Beenden mit Ctrl+C.

Das Ergometer meldet sich per Bluetooth unter dem Namen seines FitShow-Moduls, z. B. `FS-1837D8`. Es nimmt nur eine Verbindung gleichzeitig an: Vor dem Start andere Apps (z. B. Kinomap) trennen und ein paar Umdrehungen treten, damit es aufwacht.

## Optionen

| Option | Bedeutung |
|---|---|
| `--panel` | Cockpit im Browser anzeigen |
| `--port N` | Port für das Cockpit (Standard: 8050) |
| `--power W` | Zielleistung in Watt schon beim Start vorgeben |
| `--scan` | nur die Bluetooth-Geräte in der Nähe auflisten |
| `--name TEXT` | Gerät mit diesem Namensbestandteil suchen |
| `--address ID` | mit einem bestimmten Gerät verbinden (auf macOS eine UUID) |
| `--timeout S` | Suchdauer in Sekunden (Standard: 10) |
| `--raw` | zusätzlich die Rohdaten aller übrigen Benachrichtigungen ausgeben |

Ohne `--panel` gibt es nur die Konsolenausgabe.

## Zielleistung und PID-Regler

Im Cockpit stellt der Schieber „Zielleistung" die gewünschte Leistung ein (0 = aus). Die Regelung läuft im Programm, nicht im Ergometer:

1. Einmal pro Sekunde wird die gemessene Leistung (Mittel der letzten drei Messwerte) mit dem Ziel verglichen.
2. Der PID-Regler berechnet daraus die Widerstandsstufe (1–24) und sendet sie an das Ergometer.
3. Abweichungen unter 8 W gelten als erreicht, damit der Regler nicht zwischen zwei Stufen pendelt. Eine Stufe entspricht etwa 15–20 W.
4. Unter 20 rpm hält der Regler die Stufe, statt den Widerstand hochzudrehen.

Die Abstimmung steht als Konstanten oben in `ergometer.py`:

| Konstante | Bedeutung |
|---|---|
| `PID_KP`, `PID_KI`, `PID_KD` | Verstärkungen (Stufen pro Watt) |
| `PID_DEADBAND` | Mindestabstand zur aktuellen Stufe, bevor verstellt wird |
| `PID_TOLERANCE` | Leistungsabweichung in Watt, die als erreicht gilt |
| `MIN_CADENCE` | Trittfrequenz, unter der der Regler pausiert |

## Technik

- Die Messwerte kommen über den Bluetooth-Standard FTMS (Fitness Machine Service, `0x1826`), Merkmal „Indoor Bike Data" (`0x2AD2`).
- Der Widerstand wird über den „Fitness Machine Control Point" (`0x2AD9`) gesetzt. Das AX 4000 landet dabei teils eine Stufe neben dem angeforderten Wert; das Programm liest die gemeldete Stufe zurück und korrigiert.
- `ergometer.py` enthält Bluetooth-Anbindung, PID-Regler und einen kleinen HTTP-Server; `panel.html` ist das Cockpit und holt die Daten jede Sekunde vom Server.
