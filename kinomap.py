#!/usr/bin/env python3
"""Report on one ride from a Kinomap activity export, as a Plotly page next to the pages of lauf.py.

Usage:
    python3 kinomap.py ~/training/cern.zip
    python3 kinomap.py ~/training/cern.zip --betablocker --ftp 200

The export is the zip Kinomap offers for an activity (.csv, .tcx, .gpx and .pwx of the same ride, one
sample per second). Such a ride follows a video and has no target power, so the page shows what a
free ride can be judged by: the power against the FTP, its best mean values per duration, the time in
each intensity step of the calendar, and pulse, cadence, speed and altitude over the ride.

The page lands in logs/ as kinomap-<yy-mm-dd-hh-mm-ss>.html, named after the local start time, where
kalender.py finds it and links it to the session of the training log. Nothing is written to the
training log: the ride is filed there with that project's add-training script.
"""

import argparse
import csv
import html
import io
import json
import re
import zipfile
from datetime import datetime
from itertools import accumulate
from pathlib import Path
from statistics import mean, median

FTP = 200             # watts; the intensity steps are shares of it, as in kalender.py
STEPS = [0.7, 0.8, 0.9, 1.0]  # lower edges of steps 2 to 5 as shares of the FTP, as in kalender.py
MEDIAN_SAMPLES = 5    # the power, pulse and cadence curves are a median filter over this many samples
POWER_SECONDS = 30    # the window of the normalized power
BLOCK_SECONDS = 300   # the ride is summed up in blocks of this length
BEST = [5, 30, 60, 300, 600, 1200]  # durations whose best mean power is listed
CURVE = [1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 300, 420, 600, 900, 1200, 1800, 2700, 3600, 5400, 7200]
EFFICIENCY = 0.25     # share of the energy turned over that reaches the pedals; as in training_log.csv
MATCH_SECONDS = 60    # as in kalender.py: how far the duration in the training log may be off
TRAINING = Path.home() / "training"  # the training log project


def read_export(path: Path) -> tuple[list[dict], dict]:
    """The samples of the ride and what only the other files of the export know: start, calories and the route's name."""
    with zipfile.ZipFile(path) as archive:
        text = {Path(name).suffix: archive.read(name).decode("utf-8") for name in archive.namelist()}
    names = {"Altitude": "altitude", "Distance": "distance", "Speed": "speed", "Heart rate": "pulse", "Power": "power", "Cadence": "cadence"}
    rows = []
    for row in csv.DictReader(io.StringIO(text[".csv"])):
        # The export counts its time from 1970-01-01 00:00:00.
        seconds = (datetime.fromisoformat(row["Date"]) - datetime(1970, 1, 1)).total_seconds()
        rows.append({"seconds": seconds} | {key: float(row[name] or 0) for name, key in names.items()})
    find = lambda pattern, suffix: (re.search(pattern, text.get(suffix, "")) or [None, None])[1]
    start, calories, route = find(r"<Id>([^<]+)</Id>", ".tcx"), find(r"<Calories>(\d+)</Calories>", ".tcx"), find(r"<name>(?:Kinomap - )?([^<]*)</name>", ".gpx")
    return rows, {"start": datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone() if start else None,
                  "kcal": float(calories) if calories else None, "route": html.unescape(route).strip() if route else ""}


def smooth(values: list[float], sensor: bool = False) -> list[float | None]:
    """A column as a centred median filter over MEDIAN_SAMPLES samples. For a sensor that can drop out, zeros are no values: None there."""
    half = MEDIAN_SAMPLES // 2
    smoothed = []
    for index, value in enumerate(values):
        window = [v for v in values[max(index - half, 0):index + half + 1] if v > 0 or not sensor]
        smoothed.append(median(window) if window and (value > 0 or not sensor) else None)
    return smoothed


def best_mean(power: list[float], seconds: int) -> tuple[float, int]:
    """The highest mean power held for that long, and the second it began."""
    total = [0.0] + list(accumulate(power))
    start = max(range(len(power) - seconds + 1), key=lambda i: total[i + seconds] - total[i])
    return (total[start + seconds] - total[start]) / seconds, start


def climb(rows: list[dict]) -> float:
    return sum(max(b["altitude"] - a["altitude"], 0) for a, b in zip(rows, rows[1:]))


def logged(day: str, kcal: float, seconds: float) -> dict | None:
    """The ride's row in the training log, found the way kalender.py matches a page to a session."""
    log = TRAINING / "training_log.csv"
    if not log.exists():
        return None
    with open(log, encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            if row["Datum"] == day and row["Kcal"] == f"{kcal:.0f}" and abs(float(row["Dauer_sek"] or 0) - seconds) <= MATCH_SECONDS:
                return row
    return None


def analyse(rows: list[dict], facts: dict, ftp: float, betablocker: bool) -> dict:
    power = [row["power"] for row in rows]
    seconds = rows[-1]["seconds"] - rows[0]["seconds"]
    work = sum(a["power"] * (b["seconds"] - a["seconds"]) for a, b in zip(rows, rows[1:])) / 1000
    pulse = [row["pulse"] for row in rows if row["pulse"] > 0]
    rolling = [mean(power[i:i + POWER_SECONDS]) for i in range(len(power) - POWER_SECONDS + 1)] or power
    peak = max(range(len(power)), key=lambda i: power[i])
    blocks = []
    for first in range(0, len(rows) - 1, BLOCK_SECONDS):
        block = rows[first:first + BLOCK_SECONDS + 1]
        beats = [row["pulse"] for row in block if row["pulse"] > 0]
        blocks.append({"start": block[0]["seconds"], "seconds": block[-1]["seconds"] - block[0]["seconds"], "power": mean(row["power"] for row in block[:-1] or block),
                       "pulse": mean(beats) if beats else None, "cadence": mean(row["cadence"] for row in block), "speed": mean(row["speed"] for row in block),
                       "climb": climb(block)})
    edges = [share * ftp for share in STEPS]
    zones = [0.0] * (len(edges) + 1)
    for a, b in zip(rows, rows[1:]):
        zones[sum(a["power"] >= edge for edge in edges)] += b["seconds"] - a["seconds"]
    half = len(power) // 2
    kcal = facts["kcal"] if facts["kcal"] is not None else work / 4.185 / EFFICIENCY
    start = facts["start"]
    return {
        "route": facts["route"], "start": start.strftime("%H:%M") if start else None, "ftp": ftp, "betablocker": betablocker,
        "seconds": seconds, "kj": work, "kcal": kcal, "kcal_work": work / 4.185 / EFFICIENCY, "power": work * 1000 / seconds, "power_max": power[peak], "peak_at": rows[peak]["seconds"],
        "power_min": min(power), "normalized": mean(value ** 4 for value in rolling) ** 0.25, "halves": [mean(power[:half]), mean(power[half:])],
        "above": sum(zones[-1:]), "cadence": mean(row["cadence"] for row in rows if row["cadence"] >= 40), "pulse": mean(pulse) if pulse else None,
        "pulse_max": max(pulse) if pulse else None, "distance": rows[-1]["distance"] / 1000, "speed": mean(row["speed"] for row in rows),
        "speed_max": max(row["speed"] for row in rows), "climb": climb(rows), "altitude": [min(row["altitude"] for row in rows), max(row["altitude"] for row in rows)],
        "best": [dict(zip(("power", "start"), best_mean(power, span)), seconds=span) for span in BEST if span <= len(power)],
        "curve": [{"seconds": span, "power": best_mean(power, span)[0]} for span in CURVE if span <= len(power)],
        "blocks": blocks, "zones": zones,
        "series": {"t": [round(row["seconds"] / 60, 3) for row in rows], "power": smooth(power),
                   "pulse": smooth([row["pulse"] for row in rows], sensor=True), "cadence": smooth([row["cadence"] for row in rows]),
                   "speed": [row["speed"] for row in rows], "altitude": [row["altitude"] for row in rows]},
        "entry": logged(f"{start.day}.{start.month}", kcal, seconds) if start else None,
    }


PAGE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kinomap-Fahrt __DATE__</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  :root { color-scheme: light dark; --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
          --grid: #e1e0d9; --axis: #c3c2b7; --series-1: #2a78d6; --series-2: #eb6834;
          --step-1: #2e9f4d; --step-2: #d9b300; --step-3: #f08a24; --step-4: #d6362f; --step-5: #7b3fa0; }
  @media (prefers-color-scheme: dark) { :root { --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --grid: #2c2c2a;
          --axis: #383835; --series-1: #3987e5; --series-2: #d95926;
          --step-1: #3fb760; --step-2: #f0d03c; --step-3: #f59a3a; --step-4: #e5484d; --step-5: #9a5fd0; } }
  body { margin: 0; padding: 20px 16px 40px; background: var(--page); color: var(--ink); font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  main { max-width: 1040px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0; }
  h2 { font-size: 16px; margin: 0 0 2px; }
  a { color: inherit; }
  .back { font-size: 13px; color: var(--ink-2); }
  .sub { color: var(--ink-2); margin: 2px 0 16px; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 10px; }
  .card { background: var(--surface); border: 1px solid var(--grid); border-radius: 8px; padding: 12px 14px; margin-bottom: 10px; min-width: 0; }
  .tiles .card { margin: 0; }
  .pair { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 10px; }
  .label, .note { color: var(--ink-2); font-size: 13px; }
  .value { font-size: 24px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .value small { font-size: 14px; font-weight: 400; color: var(--ink-2); }
  .plot { height: 300px; }
  .plot.low { height: 220px; }
  .scroll { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
  th, td { text-align: right; padding: 5px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
  th { color: var(--ink-2); font-weight: 600; font-size: 13px; }
  th:first-child, td:first-child { text-align: left; white-space: normal; }
  tr:last-child td { border-bottom: 0; }
  ul { margin: 6px 0 0; padding-left: 20px; }
  li { margin-bottom: 6px; }
</style>
</head>
<body>
<main>
<a class="back" href="index.html">← Trainingskalender</a>
<h1>Kinomap-Fahrt vom __DATE__</h1>
<p class="sub" id="sub"></p>
<section class="tiles" id="tiles"></section>
<section class="pair">
  <div class="card"><h2>Beobachtungen</h2><ul id="remarks"></ul></div>
  <div class="card"><h2>Eintrag im Trainingslog</h2><div class="note" id="entry-note"></div><div class="scroll"><table id="entry"></table></div></div>
</section>
<section class="card"><h2>Leistung im Verlauf</h2><div class="note">Medianfilter über __MEDIAN__ Messwerte, dazu die FTP</div><div class="plot" id="power"></div></section>
<section class="pair">
  <div class="card"><h2>Puls im Verlauf</h2><div class="note">Medianfilter über __MEDIAN__ Messwerte</div><div class="plot low" id="pulse"></div></div>
  <div class="card"><h2>Trittfrequenz im Verlauf</h2><div class="note">Medianfilter über __MEDIAN__ Messwerte</div><div class="plot low" id="cadence"></div></div>
</section>
<section class="pair">
  <div class="card"><h2>Geschwindigkeit im Verlauf</h2><div class="note">Von Kinomap aus Leistung und Gefälle der Strecke berechnet</div><div class="plot low" id="speed"></div></div>
  <div class="card"><h2>Höhe im Verlauf</h2><div class="note">Höhenprofil der Strecke aus dem Export</div><div class="plot low" id="altitude"></div></div>
</section>
<section class="card"><h2>Mittlere Leistung je __BLOCK__ Minuten</h2><div class="note">Jeder Balken ein Block der Fahrt, der letzte kann kürzer sein</div><div class="plot" id="bars"></div></section>
<section class="pair">
  <div class="card"><h2>Beste mittlere Leistung je Dauer</h2><div class="note">Höchster Mittelwert, der in dieser Fahrt so lange gehalten wurde</div><div class="plot low" id="curve"></div></div>
  <div class="card"><h2>Zeit je Intensitätsstufe</h2><div class="note">Stufen des Kalenders: Leistung als Anteil der FTP von __FTP__ W</div><div class="plot low" id="zones"></div></div>
</section>
<section class="pair">
  <div class="card"><h2>Blöcke</h2><div class="scroll"><table id="blocks"></table></div></div>
  <div class="card"><h2>Bestwerte</h2><div class="scroll"><table id="best"></table></div></div>
</section>
<p class="note">Quelle: Kinomap-Export __ZIP__. Arbeit: Leistung über die Zeit summiert; Kalorien wie von Kinomap gemeldet.</p>
</main>
<script>
const D = __DATA__;
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const n = (value, digits = 0) => value === null || value === undefined ? "–" : value.toLocaleString("de-DE", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const clock = (seconds) => `${Math.floor(Math.round(seconds) / 60)}:${String(Math.round(seconds) % 60).padStart(2, "0")}`;
const span = (seconds) => seconds < 60 ? `${seconds} s` : `${n(seconds / 60)} min`;
const share = (watt) => `${n(watt / D.ftp * 100)} % FTP`;
const best = (seconds) => D.best.find((item) => item.seconds === seconds);

document.getElementById("sub").textContent = [D.route, D.start ? `Start ${D.start} Uhr` : ""].filter(Boolean).join(" · ");
document.getElementById("tiles").innerHTML = [
  ["Gefahrene Zeit", clock(D.seconds), "min", `${n(D.distance, 2)} km`],
  ["Durchschnittsleistung", n(D.power), "W", `${share(D.power)} · höchster Wert ${n(D.power_max)} W`],
  ["Normalized Power", n(D.normalized), "W", `Verhältnis zum Mittel ${n(D.normalized / D.power, 2)}`],
  ["Arbeit", n(D.kj), "kJ", `${n(D.kcal)} kcal laut Kinomap`],
  ["Höchster Puls", n(D.pulse_max), "bpm", `im Mittel ${n(D.pulse)}` + (D.betablocker ? " · unter Betablocker" : "")],
  ["Trittfrequenz", n(D.cadence), "rpm", ""],
  ["Geschwindigkeit", n(D.speed, 1), "km/h", `bis ${n(D.speed_max, 1)} km/h`],
  ["Höhenmeter", n(D.climb), "m", `zwischen ${n(D.altitude[0])} und ${n(D.altitude[1])} m`],
].map(([label, value, unit, note]) => `<div class="card"><div class="label">${label}</div><div class="value">${value} <small>${unit}</small></div><div class="note">${note}</div></div>`).join("");

// What stands out, worked out from the figures.
const remarks = [];
remarks.push(`<b>Im Mittel ${n(D.power)} W, das sind ${share(D.power)}.</b> ${clock(D.above)} von ${clock(D.seconds)} min lagen bei der FTP oder darüber.`);
remarks.push(D.normalized / D.power < 1.05
  ? `<b>Gleichmäßig gefahren:</b> Die Normalized Power (${n(D.normalized)} W) liegt kaum über dem Mittel, die Leistung fiel nie unter ${n(D.power_min)} W.`
  : `<b>Wechselhaft gefahren:</b> Die Normalized Power (${n(D.normalized)} W) liegt ${n((D.normalized / D.power - 1) * 100)} % über dem Mittel, der niedrigste Wert war ${n(D.power_min)} W.`);
const [early, late] = D.halves;
remarks.push(Math.abs(late - early) < 0.03 * D.power
  ? `<b>Beide Hälften gleich stark:</b> ${n(early)} W in der ersten, ${n(late)} W in der zweiten.`
  : `<b>Zweite Hälfte ${late > early ? "stärker" : "schwächer"}:</b> ${n(early)} W in der ersten, ${n(late)} W in der zweiten.`);
const long = [1200, 600, 300].map(best).find(Boolean);
if (long) remarks.push(`<b>Beste ${span(long.seconds)}: ${n(long.power)} W</b> (${share(long.power)}), ab ${clock(long.start)} min.`);
if (best(5)) remarks.push(`<b>Höchster Einzelwert ${n(D.power_max)} W</b> bei ${clock(D.peak_at)} min` +
  (D.power_max > 1.1 * best(5).power ? ` – ein Ausschlag, über 5 Sekunden gehalten waren es höchstens ${n(best(5).power)} W.` : `, über 5 Sekunden gehalten ${n(best(5).power)} W.`));
if (D.pulse_max) remarks.push(D.betablocker
  ? `<b>Puls bis ${n(D.pulse_max)} bpm.</b> Unter Betablocker bewegt er sich kaum und sagt nichts über die Belastung.`
  : `<b>Puls bis ${n(D.pulse_max)} bpm</b>, im Mittel ${n(D.pulse)}.`);
document.getElementById("remarks").innerHTML = remarks.map((text) => `<li>${text}</li>`).join("");

const row = (cells) => `<tr>${cells.map((cell) => `<td>${cell}</td>`).join("")}</tr>`;
const E = D.entry;
document.getElementById("entry-note").textContent = E ? `Nr. ${E.Nr} in training_log.csv` : "Nicht im Trainingslog gefunden.";
const entry = document.getElementById("entry");
for (const [label, value] of E ? [
  ["Datum", E.Datum], ["Strecke", E.Strecke || "–"], ["Dauer", `${E.Dauer_sek} s (${clock(+E.Dauer_sek)} min)`], ["Kcal / Watt", `${E.Kcal} / ${E.Watt}`], ["MaxHF", E.MaxHF || "–"],
  ["RR nach Training", E.RR_training || "–"], ["Distanz, Trittfrequenz", `${E.Distanz_km} km, ${E.Kadenz_rpm} rpm`], ["Watt_Max, Max_kmh", `${E.Watt_Max} W, ${E.Max_kmh} km/h`],
  ["Höhenmeter", E.Hoehenmeter || "–"], ["Temperatur_C", E.Temperatur_C || "–"], ["Kommentar", E.Kommentar || "–"]] : []) {
  const line = entry.insertRow();
  line.insertCell().textContent = label;
  const cell = line.insertCell();
  cell.textContent = value;
  cell.style.whiteSpace = "normal";
}

document.getElementById("blocks").innerHTML = "<tr><th>ab</th><th>Dauer</th><th>Ø Watt</th><th>% FTP</th><th>Puls</th><th>rpm</th><th>km/h</th><th>Anstieg</th></tr>" +
  D.blocks.map((b) => row([clock(b.start), clock(b.seconds), `${n(b.power)} W`, n(b.power / D.ftp * 100), n(b.pulse), n(b.cadence), n(b.speed, 1), `${n(b.climb)} m`])).join("");
document.getElementById("best").innerHTML = "<tr><th>Dauer</th><th>Ø Watt</th><th>% FTP</th><th>ab</th></tr>" +
  D.best.map((b) => row([span(b.seconds), `${n(b.power)} W`, n(b.power / D.ftp * 100), clock(b.start)])).join("");

function draw() {
  const ink = css("--ink-2"), s1 = css("--series-1"), s2 = css("--series-2"), surface = css("--surface"), strong = css("--ink"), muted = css("--muted");
  const axis = { gridcolor: css("--grid"), linecolor: css("--axis"), zeroline: false, tickfont: { color: muted, size: 12 } };
  const title = (text) => ({ text, font: { color: ink, size: 13 } });
  const minutes = { ...axis, title: title("Gefahrene Zeit (min)"), showgrid: false, range: [0, D.seconds / 60], hoverformat: ".1f" };
  const base = { margin: { l: 52, r: 16, t: 24, b: 44 }, paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
                 font: { family: getComputedStyle(document.body).fontFamily, color: ink }, hovermode: "x unified",
                 hoverlabel: { bgcolor: surface, bordercolor: css("--axis"), font: { color: strong } },
                 legend: { orientation: "h", x: 0, y: -0.24, font: { color: ink } }, xaxis: minutes };
  const config = { displayModeBar: false, responsive: true };
  // The FTP as a line across a power chart.
  const ftp = { shapes: [{ type: "line", xref: "paper", x0: 0, x1: 1, y0: D.ftp, y1: D.ftp, line: { color: s2, width: 2, dash: "dash" } }],
                annotations: [{ xref: "paper", x: 1, xanchor: "left", y: D.ftp, text: ` FTP ${n(D.ftp)} W`, showarrow: false, font: { color: ink, size: 12 } }] };
  const wide = { ...base.margin, r: 84 };  // room for that label beside the plot
  const line = (id, y, unit, label, extra = {}, digits = 0) => Plotly.react(id, [
    { x: D.series.t, y, mode: "lines", connectgaps: false, line: { color: s1, width: 2 }, hovertemplate: `%{y:.${digits}f} ${unit}<extra></extra>`, ...extra },
  ], { ...base, showlegend: false, yaxis: { ...axis, title: title(label) } }, config);

  Plotly.react("power", [
    { x: D.series.t, y: D.series.power, mode: "lines", line: { color: s1, width: 2 }, hovertemplate: "%{y:.0f} W<extra></extra>" },
  ], { ...base, ...ftp, margin: wide, showlegend: false, yaxis: { ...axis, title: title("Leistung (W)"), rangemode: "tozero" } }, config);
  line("pulse", D.series.pulse, "bpm", "Puls (bpm)");
  line("cadence", D.series.cadence, "rpm", "Trittfrequenz (rpm)");
  line("speed", D.series.speed, "km/h", "Geschwindigkeit (km/h)", {}, 1);
  line("altitude", D.series.altitude, "m", "Höhe (m)", {}, 1);

  // One bar per block, placed where and as wide as the block lies in time.
  Plotly.react("bars", [
    { type: "bar", x: D.blocks.map((b) => (b.start + b.seconds / 2) / 60), y: D.blocks.map((b) => b.power), width: D.blocks.map((b) => Math.max(b.seconds / 60 - 0.12, 0.05)),
      marker: { color: s1 }, text: D.blocks.map((b) => n(b.power)), textposition: "inside", insidetextanchor: "end", textfont: { color: "#ffffff", size: 13 },
      customdata: D.blocks.map((b) => [clock(b.start), clock(b.start + b.seconds)]), hovertemplate: "%{customdata[0]}–%{customdata[1]} min: %{y:.0f} W<extra></extra>" },
  ], { ...base, ...ftp, margin: wide, hovermode: "closest", showlegend: false, yaxis: { ...axis, title: title("Leistung (W)"), rangemode: "tozero" } }, config);

  const ticks = [1, 5, 30, 60, 300, 1200, 3600].filter((seconds) => seconds <= D.curve.at(-1).seconds);
  Plotly.react("curve", [
    { x: D.curve.map((point) => point.seconds), y: D.curve.map((point) => point.power), mode: "lines+markers", line: { color: s1, width: 2 }, marker: { color: s1, size: 6 },
      customdata: D.curve.map((point) => span(point.seconds)), hovertemplate: "%{customdata}: %{y:.0f} W<extra></extra>" },
  ], { ...base, ...ftp, margin: wide, hovermode: "closest", showlegend: false, yaxis: { ...axis, title: title("Leistung (W)") },
       xaxis: { ...axis, type: "log", title: title("Dauer"), showgrid: false, tickvals: ticks, ticktext: ticks.map(span) } }, config);

  const edges = __STEPS__, last = edges.length;
  const names = D.zones.map((_, index) => index === 0 ? `unter ${n(edges[0] * 100)} %` : index === last ? `ab ${n(edges[last - 1] * 100)} %` : `${n(edges[index - 1] * 100)}–${n(edges[index] * 100)} %`);
  Plotly.react("zones", [
    { type: "bar", x: names, y: D.zones.map((seconds) => seconds / 60), marker: { color: D.zones.map((_, index) => css(`--step-${index + 1}`)) },
      text: D.zones.map((seconds) => clock(seconds)), textposition: "outside", textfont: { color: ink, size: 12 }, cliponaxis: false,
      customdata: D.zones.map((seconds) => [clock(seconds), n(seconds / D.seconds * 100)]), hovertemplate: "%{x}: %{customdata[0]} min (%{customdata[1]} % der Fahrt)<extra></extra>" },
  ], { ...base, hovermode: "closest", showlegend: false, yaxis: { ...axis, title: title("Zeit (min)"), rangemode: "tozero" },
       xaxis: { ...axis, title: title("Anteil der FTP"), showgrid: false, type: "category" } }, config);
}
draw();
addEventListener("load", () => document.querySelectorAll(".plot").forEach((plot) => Plotly.Plots.resize(plot)));
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="Report on a ride exported from Kinomap.")
    parser.add_argument("zip", type=Path, help="the activity export from Kinomap")
    parser.add_argument("--ftp", type=float, default=FTP, help=f"functional threshold power in watts (default {FTP})")
    parser.add_argument("--betablocker", action="store_true", help="the rider takes a beta blocker: the pulse is not judged")
    args = parser.parse_args()

    rows, facts = read_export(args.zip)
    result = analyse(rows, facts, args.ftp, args.betablocker)
    start = facts["start"] or datetime.fromtimestamp(args.zip.stat().st_mtime)
    out = Path(__file__).with_name("logs") / f"kinomap-{start:%y-%m-%d-%H-%M-%S}.html"
    out.write_text(PAGE.replace("__DATE__", f"{start:%d.%m.%Y, %H:%M} Uhr").replace("__MEDIAN__", str(MEDIAN_SAMPLES))
                  .replace("__BLOCK__", f"{BLOCK_SECONDS / 60:g}").replace("__FTP__", f"{args.ftp:g}")
                   .replace("__STEPS__", json.dumps(STEPS)).replace("__ZIP__", html.escape(args.zip.name))
                   .replace("__DATA__", json.dumps(result, ensure_ascii=False).replace("</", "<\\/")), encoding="utf-8")

    print(f"{result['route']}  {result['seconds']:.0f} s  {result['power']:.0f} W (NP {result['normalized']:.0f})  {result['kj']:.0f} kJ  {result['kcal']:.0f} kcal  "
          f"max {result['power_max']:.0f} W  {result['cadence']:.0f} rpm  {result['distance']:.2f} km  {result['climb']:.0f} m  pulse max {result['pulse_max'] or 0:.0f}")
    print("best " + ", ".join(f"{item['seconds']} s {item['power']:.0f} W" for item in result["best"]))
    print(f"training log: Nr {result['entry']['Nr']}" if result["entry"] else "training log: no row for this ride")
    print(out)


if __name__ == "__main__":
    main()
