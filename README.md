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
| `--profile DATEI` | Leistungsvorgabe aus einer Datei laden; sie startet, sobald das Ergometer verbunden ist |
| `--scan` | nur die Bluetooth-Geräte in der Nähe auflisten |
| `--name TEXT` | Gerät mit diesem Namensbestandteil suchen |
| `--address ID` | mit einem bestimmten Gerät verbinden (auf macOS eine UUID) |
| `--timeout S` | Suchdauer in Sekunden (Standard: 10) |
| `--raw` | zusätzlich die Rohdaten aller übrigen Benachrichtigungen ausgeben |

Ohne `--panel` gibt es nur die Konsolenausgabe.

## Cockpit

- Kacheln mit den aktuellen Werten, darunter Verlaufskurven für Leistung, Trittfrequenz, Geschwindigkeit und Puls über die letzten 20 Minuten
- Die Pulskurve liegt auf farbigen Zonen von 60 bis 160 bpm.
- Im Leistungsdiagramm zeigt die rote Linie die Vorgabe.

## Leistungsvorgabe aus einer Datei

Das Menü „Vorgabe" im Cockpit bietet alle Dateien `vorgabe-<name>.csv` an, die im selben Verzeichnis wie das Programm liegen; neue Dateien erscheinen dort von selbst. „Start" beginnt das Profil, „Stopp" beendet es und schaltet die Regelung aus. Während das Profil läuft, zeigt das Leistungsdiagramm die kommenden zwei Minuten als gepunktete rote Linie. Wer den Schieber „Zielleistung" bewegt, übernimmt von Hand und hält das Profil an.

Die Datei hat zwei Spalten, Zeit (`mm:ss`) und Watt, getrennt durch Komma, Semikolon oder Tabulator. Jede Zeile gilt ab ihrer Zeit bis zur nächsten; eine letzte Zeile mit 0 Watt beendet das Profil. Eine Kopfzeile ist erlaubt.

```
zeit,watt
00:00,80
02:00,200
02:15,100
25:00,0
```

Mitgeliefert sind zwei Profile, jeweils mit 2 Minuten Einfahren und 2 Minuten Ausfahren bei 80 W:

- `vorgabe-hit.csv` (25 Minuten): 21 Zyklen aus 15 Sekunden bei 200 W und 45 Sekunden bei 100 W
- `vorgabe-intervall-1-3.csv` (24 Minuten): 5 Zyklen aus 1 Minute bei 200 W und 3 Minuten bei 100 W
- `vorgabe-sprint-30-60.csv` (19 Minuten): 10 Zyklen aus 30 Sekunden bei 250 W und 60 Sekunden bei 90 W
- `vorgabe-stufentest.csv` (35 Minuten): ab 100 W alle 3 Minuten 25 W mehr, bis 350 W

## Aufzeichnung

Zwischen „Start" und „Stopp" werden alle Messwerte des Ergometers aufgezeichnet. Auch die Kurven im Cockpit laufen nur in dieser Zeit: Vor dem Start stehen die Diagramme bei 00:00 und nur die Kacheln zeigen die aktuellen Werte, nach dem Stopp bleiben die Kurven stehen. Beim Stopp – auch wenn das Profil von selbst endet, der Schieber übernimmt, die Verbindung abbricht oder das Programm beendet wird – entsteht daraus `logs/log-<jj-mm-tt-hh-mm-ss>.csv`, benannt nach dem Startzeitpunkt. Die Datei enthält je Messung eine Zeile mit Uhrzeit, Sekunden seit dem Start, Zielleistung und den gemeldeten Werten (Geschwindigkeit, Trittfrequenz, Widerstand, Leistung, Puls ...).

## Zielleistung und PID-Regler

Im Cockpit stellt der Schieber „Zielleistung" die gewünschte Leistung ein (0 = aus). Die Regelung läuft im Programm, nicht im Ergometer:

1. Einmal pro Sekunde wird die gemessene Leistung (Mittel der letzten drei Messwerte) mit dem Ziel verglichen.
2. Der PID-Regler berechnet daraus die Widerstandsstufe (1–24) und sendet sie an das Ergometer.
3. Abweichungen unter 8 W gelten als erreicht, damit der Regler nicht zwischen zwei Stufen pendelt. Eine Stufe entspricht etwa 15–20 W.
4. Unter 20 rpm hält der Regler die Stufe, statt den Widerstand hochzudrehen.
5. Ändert sich die Vorgabe, springt der Regler sofort um die erwartete Zahl an Stufen (`WATTS_PER_LEVEL`), statt sich langsam heranzutasten.

Das Ergometer bestätigt zwar auch eine direkte Watt-Vorgabe über Bluetooth, hält die Leistung dann aber nicht selbst; deshalb regelt das Programm.

Die Abstimmung steht als Konstanten oben in `ergometer.py`:

| Konstante | Bedeutung |
|---|---|
| `PID_KP`, `PID_KI`, `PID_KD` | Verstärkungen (Stufen pro Watt) |
| `PID_DEADBAND` | Mindestabstand zur aktuellen Stufe, bevor verstellt wird |
| `PID_TOLERANCE` | Leistungsabweichung in Watt, die als erreicht gilt |
| `WATTS_PER_LEVEL` | geschätzte Wirkung einer Stufe in Watt, für den Sprung bei neuer Vorgabe |
| `MIN_CADENCE` | Trittfrequenz, unter der der Regler pausiert |

## Technik

- Die Messwerte kommen über den Bluetooth-Standard FTMS (Fitness Machine Service, `0x1826`), Merkmal „Indoor Bike Data" (`0x2AD2`).
- Der Widerstand wird über den „Fitness Machine Control Point" (`0x2AD9`) gesetzt. Das AX 4000 landet dabei teils eine Stufe neben dem angeforderten Wert; das Programm liest die gemeldete Stufe zurück und korrigiert.
- `ergometer.py` enthält Bluetooth-Anbindung, PID-Regler und einen kleinen HTTP-Server; `panel.html` ist das Cockpit und holt die Daten jede Sekunde vom Server.
