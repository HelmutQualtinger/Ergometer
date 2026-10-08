#!/usr/bin/env python3
"""Report on one ride from one or more logs of ergometer.py, as a Plotly page, and file it in the training log.

Usage:
    python3 lauf.py                                  # newest log in logs/
    python3 lauf.py logs/log-A.csv logs/log-B.csv    # several logs joined as one ride
    python3 lauf.py --training --note "HIIT 12x(30:30)" --rr 126/83/87

Several logs are joined in the order given, as one ride. What happened between two logs is not
recorded - a break or a recording that was stopped by mistake - so the page only calls it a gap.
The page lands next to the first log as lauf-<yy-mm-dd-hh-mm-ss>.html.

--training appends the ride to ~/training/training_log.csv through that project's own script
and then fills in the columns a Kinomap summary would have filled (distance, cadence, peak
power, top speed, temperature). A ride that is already in the log is not added twice.
"""

import argparse
import csv
import io
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from statistics import mean

SMOOTH_SECONDS = 10  # the pulse curve is a moving average over this window
EFFICIENCY = 0.25    # share of the energy turned over that reaches the pedals; as in training_log.csv
WORK_SHARE = 0.8     # sections at this share of the highest target or more count as the hard ones
OVERSHOOT = 1.2      # a peak this far above the target is worth a remark
TRAINING = Path.home() / "training"  # the training log project: training_log.csv and its add-training script
ADD_TRAINING = TRAINING / ".claude/skills/add-training/scripts/add_training.py"


def read_log(path: Path) -> list[dict]:
    with path.open(newline="") as file:
        return [{key: value if key == "time" else float(value or 0) for key, value in row.items()}
                for row in csv.DictReader(file)]


def smooth_pulse(rows: list[dict]) -> list[float | None]:
    """The pulse as a centred moving average; None where the sensor delivered nothing."""
    smoothed, first, last = [], 0, 0
    for row in rows:
        while rows[first]["seconds"] < row["seconds"] - SMOOTH_SECONDS / 2:
            first += 1
        while last < len(rows) and rows[last]["seconds"] <= row["seconds"] + SMOOTH_SECONDS / 2:
            last += 1
        window = [r["heart rate"] for r in rows[first:last] if r["heart rate"] > 0]
        smoothed.append(round(mean(window), 1) if window and row["heart rate"] > 0 else None)
    return smoothed


def summary(rows: list[dict]) -> dict:
    """Duration, work and the usual means of a stretch of samples (time-weighted where it matters)."""
    work = sum(a["power"] * (b["seconds"] - a["seconds"]) for a, b in zip(rows, rows[1:]))
    seconds = rows[-1]["seconds"] - rows[0]["seconds"]
    pulse = [row["heart rate"] for row in rows if row["heart rate"] > 0]
    return {"seconds": seconds, "kj": work / 1000, "kcal": work / 1000 / 4.185 / EFFICIENCY, "power": work / seconds if seconds else 0,
            "power_max": max(row["power"] for row in rows), "cadence": mean(row["cadence"] for row in rows),
            "pulse": mean(pulse) if pulse else None, "pulse_max": max(pulse) if pulse else None}


def sections(rows: list[dict], offset: float) -> list[dict]:
    """The stretches with one target power each, with what was actually ridden in them."""
    found, current, before = [], [], 0.0
    for row in rows + [None]:
        if current and (row is None or row["target"] != current[0]["target"]):
            target = current[0]["target"]
            if target > 0 and len(current) > 1:
                # How long until the power was within 10 % of the target, coming from below or from above.
                rising = target > before
                reached = next((r["seconds"] - current[0]["seconds"] for r in current
                                if (r["power"] >= 0.9 * target if rising else r["power"] <= 1.1 * target)), None)
                found.append(summary(current) | {"start": offset + current[0]["seconds"], "target": target, "reached": reached})
            before, current = target, []
        if row is not None:
            current.append(row)
    return found


def analyse(paths: list[Path], temperature: float | None, betablocker: bool) -> dict:
    runs, parts, offset, previous_end = [], [], 0.0, None
    series = {"t": [], "power": [], "target": [], "pulse": [], "cadence": []}
    levels, distance, speed, room = {}, 0.0, 0.0, []
    for path in paths:
        rows = read_log(path)
        start = datetime.fromisoformat(rows[0]["time"])
        runs.append(summary(rows) | {"file": path.name, "start": rows[0]["time"][11:16], "offset": offset,
                                     "pause": (start - previous_end).total_seconds() if previous_end else None})
        parts += sections(rows, offset)
        for row, beat in zip(rows, smooth_pulse(rows)):
            series["t"].append(round((offset + row["seconds"]) / 60, 3))
            series["power"].append(row["power"])
            series["target"].append(row["target"])
            series["pulse"].append(beat)
            series["cadence"].append(row["cadence"])
        for key in series:  # a gap, so the curves of two logs are not joined
            series[key].append(None)
        for a, b in zip(rows, rows[1:]):
            if a["target"] > 0:
                level = levels.setdefault(a["target"], {"seconds": 0.0, "work": 0.0, "pulse": []})
                level["seconds"] += b["seconds"] - a["seconds"]
                level["work"] += a["power"] * (b["seconds"] - a["seconds"])
                if a["heart rate"] > 0:
                    level["pulse"].append(a["heart rate"])
        # The ergometer counts the distance from when it woke up, and starts again from zero now and then.
        distance += sum(max(b.get("distance", 0) - a.get("distance", 0), 0) for a, b in zip(rows, rows[1:]))
        speed = max(speed, max(row.get("speed", 0) for row in rows))
        room += [row["room temperature"] for row in rows if row.get("room temperature")]
        offset += rows[-1]["seconds"]
        previous_end = datetime.fromisoformat(rows[-1]["time"])

    work = sum(run["kj"] for run in runs)
    pulses = [run["pulse_max"] for run in runs if run["pulse_max"]]
    pedalling = [value for value in series["cadence"] if value and value >= 40]
    top = max((part["target"] for part in parts), default=0)
    for i, part in enumerate(parts, 1):
        part.update(nr=i, work=part["target"] >= WORK_SHARE * top and len({p["target"] for p in parts}) > 1)
    first = read_log(paths[0])[0]["time"]
    peak = max(range(len(series["power"])), key=lambda i: series["power"][i] or 0)
    return {
        "peak_at": series["t"][peak] * 60, "peak_target": series["target"][peak],
        "date": f"{int(first[8:10])}.{int(first[5:7])}", "betablocker": betablocker,
        "runs": runs, "series": series, "sections": parts, "seconds": offset, "kj": work, "kcal": work / 4.185 / EFFICIENCY,
        "power": work * 1000 / offset, "power_max": max(run["power_max"] for run in runs),
        "cadence": mean(pedalling) if pedalling else 0, "pulse_max": max(pulses) if pulses else None,
        "distance": distance / 1000, "speed_max": speed,
        # Measured during the ride if the log has it, otherwise whatever was passed in.
        "temperature": mean(room) if room else temperature,
        "levels": [{"target": target, "seconds": level["seconds"], "power": level["work"] / level["seconds"],
                    "pulse": mean(level["pulse"]) if level["pulse"] else None} for target, level in sorted(levels.items())],
    }


def training_row(result: dict) -> dict:
    """The ride as it goes into the training log: the columns of training_log.csv that the logs can fill."""
    row = {"Datum": result["date"], "Dauer_sek": f"{result['seconds']:.0f}", "Kcal": f"{result['kcal']:.0f}", "Watt": f"{result['power']:.0f}",
           "MaxHF": f"{result['pulse_max']:.0f}" if result["pulse_max"] else "", "Distanz_km": f"{result['distance']:.2f}",
           "Kadenz_rpm": f"{result['cadence']:.0f}", "Watt_Max": f"{result['power_max']:.0f}", "Max_kmh": f"{result['speed_max']:.1f}",
           "Temperatur_C": f"{result['temperature']:.0f}" if result["temperature"] is not None else ""}
    return row


def file_in_training(row: dict, rr: str | None, note: str) -> tuple[dict, bool]:
    """Append the ride to the training log, unless it is there already. Returns the row as logged and whether it is new."""
    log = TRAINING / "training_log.csv"
    raw = log.read_bytes()
    ending = b"\r\n" if raw.endswith(b"\r\n") else b"\n"
    lines = raw.split(ending)
    header = next(csv.reader([lines[0].decode("utf-8-sig")]))
    same = ("Datum", "Dauer_sek", "Kcal", "Watt")
    for line in lines[1:]:
        logged = dict(zip(header, next(csv.reader([line.decode()]), [])))
        if all(logged.get(key) == row[key] for key in same):
            return logged, False

    # The project's own script assigns the number and knows the conventions; it only takes whole minutes.
    command = [sys.executable, str(ADD_TRAINING), "--date", row["Datum"], "--duration", str(max(round(float(row["Dauer_sek"]) / 60), 1)),
               "--kcal", row["Kcal"], "--watt", row["Watt"], "--note", note, "--no-pdf"]
    command += ["--hf", row["MaxHF"]] if row["MaxHF"] else []
    command += ["--rr", rr] if rr else []
    subprocess.run(command, check=True, capture_output=True, text=True, cwd=TRAINING)

    # Then the measured columns, which that script only fills from a Kinomap summary.
    lines = log.read_bytes().split(ending)
    logged = dict(zip(header, next(csv.reader([lines[-2].decode()]))))
    logged.update(row)
    out = io.StringIO()
    csv.writer(out, lineterminator="").writerow([logged[name] for name in header])
    lines[-2] = out.getvalue().encode()
    log.write_bytes(ending.join(lines))

    sys.path.insert(0, str(ADD_TRAINING.parent))
    import add_training
    add_training.regenerate_pdf(TRAINING / "training.html", TRAINING / "training.pdf")
    return logged, True


PAGE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lauf __DATE__</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  :root { color-scheme: light dark; --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
          --grid: #e1e0d9; --axis: #c3c2b7; --series-1: #2a78d6; --series-2: #eb6834; }
  @media (prefers-color-scheme: dark) { :root { --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --grid: #2c2c2a;
          --axis: #383835; --series-1: #3987e5; --series-2: #d95926; } }
  body { margin: 0; padding: 20px 16px 40px; background: var(--page); color: var(--ink); font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  main { max-width: 1040px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0; }
  h2 { font-size: 16px; margin: 0 0 2px; }
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
  tr.total td, tr.work td:first-child { font-weight: 700; }
  tr.total td { border-bottom: 0; }
  ul { margin: 6px 0 0; padding-left: 20px; }
  li { margin-bottom: 6px; }
</style>
</head>
<body>
<main>
<h1>Lauf vom __DATE__</h1>
<p class="sub" id="sub"></p>
<section class="tiles" id="tiles"></section>
<section class="pair">
  <div class="card"><h2>Beobachtungen</h2><ul id="remarks"></ul></div>
  <div class="card"><h2>Eintrag im Trainingslog</h2><div class="note" id="entry-note"></div><div class="scroll"><table id="entry"></table></div></div>
</section>
<section class="card"><h2>Leistung im Verlauf</h2><div class="note">Ist und Vorgabe über die gefahrene Zeit</div><div class="plot" id="power"></div></section>
<section class="pair">
  <div class="card"><h2>Puls im Verlauf</h2><div class="note">Gleitendes Mittel über __SMOOTH__ Sekunden</div><div class="plot low" id="pulse"></div></div>
  <div class="card"><h2>Trittfrequenz im Verlauf</h2><div class="note">Umdrehungen pro Minute</div><div class="plot low" id="cadence"></div></div>
</section>
<section class="card"><h2>Ist gegen Vorgabe je Abschnitt</h2><div class="note">Mittlere Leistung jedes Abschnitts mit gleicher Vorgabe, dazu der höchste Einzelwert</div><div class="plot" id="bars"></div></section>
<section class="pair">
  <div class="card"><h2>Abschnitte</h2><div class="note">Fett: die harten Abschnitte</div><div class="scroll"><table id="sections"></table></div></div>
  <div>
    <div class="card"><h2>Nach Vorgabestufe</h2><div class="note">Alle Abschnitte mit derselben Zielleistung zusammengenommen</div><div class="scroll"><table id="levels"></table></div></div>
    <div class="card" id="runs-card"><h2>Aufzeichnungen</h2><div class="scroll"><table id="runs"></table></div></div>
  </div>
</section>
<p class="note">Kalorien: Arbeit am Pedal bei __EFFICIENCY__ % Wirkungsgrad, wie im Trainingslog.</p>
</main>
<script>
const D = __DATA__;
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const n = (value, digits = 0) => value === null || value === undefined ? "–" : value.toLocaleString("de-DE", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const clock = (seconds) => `${Math.floor(Math.round(seconds) / 60)}:${String(Math.round(seconds) % 60).padStart(2, "0")}`;
const breaks = D.runs.filter((run) => run.pause !== null);
const work = D.sections.filter((s) => s.work);

document.getElementById("sub").textContent = `${D.runs.length} Aufzeichnung${D.runs.length > 1 ? "en" : ""}, Start ${D.runs.map((run) => run.start).join(" und ")} Uhr` +
  (breaks.length ? ` · nicht aufgezeichnet dazwischen: ${breaks.map((run) => clock(run.pause)).join(", ")} min` : "");
document.getElementById("tiles").innerHTML = [
  ["Gefahrene Zeit", clock(D.seconds), "min", D.runs.length > 1 ? D.runs.map((run) => clock(run.seconds)).join(" + ") : `${n(D.distance, 2)} km`],
  ["Durchschnittsleistung", n(D.power), "W", `höchster Wert ${n(D.power_max)} W`],
  ["Arbeit", n(D.kj), "kJ", `etwa ${n(D.kcal)} kcal`],
  ["Höchster Puls", n(D.pulse_max), "bpm", D.betablocker ? "unter Betablocker" : ""],
  ["Trittfrequenz", n(D.cadence), "rpm", `bis ${n(D.speed_max, 1)} km/h`],
  ["Raumtemperatur", n(D.temperature, 1), "°C", ""],
].map(([label, value, unit, note]) => `<div class="card"><div class="label">${label}</div><div class="value">${value} <small>${unit}</small></div><div class="note">${note}</div></div>`).join("");

// What stands out, worked out from the sections.
const remarks = [];
if (work.length) {
  const mean = (list, key) => list.reduce((sum, s) => sum + s[key], 0) / list.length;
  const targets = [...new Set(work.map((s) => s.target))].sort((a, b) => a - b);
  remarks.push(`<b>${work.length} harte Abschnitte gefahren</b> (Vorgabe ${targets.map((t) => n(t)).join(" bis ")} W), im Mittel ${n(mean(work, "power"))} W bei ${n(mean(work, "cadence"))} Umdrehungen.`);
  const slow = work.filter((s) => s.reached === null || s.reached > 5);
  remarks.push(slow.length ? `<b>In ${slow.length} davon kam die Zielleistung spät:</b> erst nach mehr als 5 Sekunden oder gar nicht (Abschnitt ${slow.map((s) => s.nr).join(", ")}).`
                           : `<b>Die Zielleistung war jeweils nach höchstens ${n(Math.max(...work.map((s) => s.reached)), 0)} Sekunden erreicht.</b>`);
  const over = work.filter((s) => s.power_max > __OVERSHOOT__ * s.target);
  if (over.length) remarks.push(`<b>${over.length} Abschnitte schossen über:</b> Spitzen bis ${n(Math.max(...over.map((s) => s.power_max)))} W bei ${n(mean(over, "cadence"))} Umdrehungen im Mittel` +
    (over.length < work.length ? `; in den übrigen waren es ${n(mean(work.filter((s) => !over.includes(s)), "cadence"))} Umdrehungen.` : "."));
}
remarks.push(`<b>Höchster Einzelwert ${n(D.power_max)} W</b> bei ${clock(D.peak_at)} min, als die Vorgabe ${n(D.peak_target)} W war` +
  (work.some((s) => Math.abs(s.start + s.seconds - D.peak_at) < 5 || Math.abs(s.start - D.peak_at) < 5) ? " – ein Ausschlag am Wechsel, kein gehaltener Wert." : "."));
if (D.pulse_max) remarks.push(D.betablocker
  ? `<b>Puls bis ${n(D.pulse_max)} bpm.</b> Unter Betablocker bewegt er sich kaum und sagt nichts über die Belastung.`
  : `<b>Puls bis ${n(D.pulse_max)} bpm</b>${work.length ? `, in den harten Abschnitten im Mittel ${n(work.reduce((sum, s) => sum + (s.pulse ?? 0), 0) / work.length)}` : ""}.`);
document.getElementById("remarks").innerHTML = remarks.map((text) => `<li>${text}</li>`).join("");

const row = (cells, cls) => `<tr${cls ? ` class="${cls}"` : ""}>${cells.map((cell) => `<td>${cell}</td>`).join("")}</tr>`;
const E = D.entry;
document.getElementById("entry-note").textContent = E.Nr ? `Nr. ${E.Nr} in training_log.csv${D.entry_new ? ", eben angelegt" : ", war schon eingetragen"}` : "Noch nicht eingetragen: so würde die Zeile aussehen.";
document.getElementById("entry").innerHTML = [
  ["Datum", E.Datum], ["Dauer", `${E.Dauer_sek} s (${clock(+E.Dauer_sek)} min)`], ["Kcal / Watt", `${E.Kcal} / ${E.Watt}`], ["MaxHF", E.MaxHF || "–"],
  ["RR nach Training", E.RR_training || "–"], ["Distanz, Trittfrequenz", `${E.Distanz_km} km, ${E.Kadenz_rpm} rpm`],
  ["Watt_Max, Max_kmh", `${E.Watt_Max} W, ${E.Max_kmh} km/h`], ["Temperatur_C", E.Temperatur_C || "–"], ["Kommentar", E.Kommentar || "–"],
].map(([label, value]) => `<tr><td>${label}</td><td style="white-space: normal">${value}</td></tr>`).join("");

document.getElementById("sections").innerHTML = "<tr><th>Nr.</th><th>ab</th><th>Dauer</th><th>Vorgabe</th><th>Ø Ist</th><th>Spitze</th><th>rpm</th><th>Puls</th></tr>" +
  D.sections.map((s) => row([s.nr, clock(s.start), clock(s.seconds), `${n(s.target)} W`, `${n(s.power)} W`, `${n(s.power_max)} W`, n(s.cadence), n(s.pulse)], s.work ? "work" : "")).join("");
document.getElementById("levels").innerHTML = "<tr><th>Vorgabe</th><th>Zeit</th><th>Ø Ist</th><th>Ø Puls</th></tr>" +
  D.levels.map((level) => row([`${n(level.target)} W`, clock(level.seconds), `${n(level.power)} W`, level.pulse === null ? "–" : `${n(level.pulse)} bpm`])).join("");
document.getElementById("runs-card").hidden = D.runs.length < 2;
document.getElementById("runs").innerHTML = "<tr><th>Start</th><th>Dauer</th><th>Ø Leistung</th><th>Arbeit</th><th>kcal</th><th>Puls Ø / max</th></tr>" +
  D.runs.map((run) => row([`${run.start} Uhr`, clock(run.seconds), `${n(run.power)} W`, `${n(run.kj)} kJ`, n(run.kcal), `${n(run.pulse)} / ${n(run.pulse_max)}`])).join("") +
  row(["Zusammen", clock(D.seconds), `${n(D.power)} W`, `${n(D.kj)} kJ`, n(D.kcal), `– / ${n(D.pulse_max)}`], "total");

function draw() {
  const ink = css("--ink-2"), s1 = css("--series-1"), s2 = css("--series-2"), surface = css("--surface"), strong = css("--ink");
  const axis = { gridcolor: css("--grid"), linecolor: css("--axis"), zeroline: false, tickfont: { color: css("--muted"), size: 12 } };
  const title = (text) => ({ text, font: { color: ink, size: 13 } });
  const minutes = { ...axis, title: title("Gefahrene Zeit (min)"), showgrid: false, range: [0, D.seconds / 60], hoverformat: ".1f" };
  // Where one log ends and the next begins, with the length of the gap between them.
  const marks = { shapes: breaks.map((run) => ({ type: "line", x0: run.offset / 60, x1: run.offset / 60, yref: "paper", y0: 0, y1: 1, line: { color: strong, width: 1, dash: "dash" } })),
                  annotations: breaks.map((run) => ({ x: run.offset / 60, yref: "paper", y: 1, yanchor: "bottom", text: `Lücke ${clock(run.pause)} min`, showarrow: false, font: { color: ink, size: 12 } })) };
  const base = { margin: { l: 52, r: 16, t: 24, b: 44 }, paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
                 font: { family: getComputedStyle(document.body).fontFamily, color: ink }, hovermode: "x unified",
                 hoverlabel: { bgcolor: surface, bordercolor: css("--axis"), font: { color: strong } },
                 legend: { orientation: "h", x: 0, y: -0.24, font: { color: ink } }, xaxis: minutes, ...marks };
  const config = { displayModeBar: false, responsive: true };
  Plotly.react("power", [
    { name: "Ist", x: D.series.t, y: D.series.power, mode: "lines", connectgaps: false, line: { color: s1, width: 2 }, hovertemplate: "%{y:.0f} W<extra>Ist</extra>" },
    { name: "Vorgabe", x: D.series.t, y: D.series.target, mode: "lines", connectgaps: false, line: { color: s2, width: 2, shape: "hv" }, hovertemplate: "%{y:.0f} W<extra>Vorgabe</extra>" },
  ], { ...base, margin: { ...base.margin, b: 70 }, yaxis: { ...axis, title: title("Leistung (W)"), rangemode: "tozero" } }, config);
  Plotly.react("pulse", [
    { x: D.series.t, y: D.series.pulse, mode: "lines", connectgaps: false, line: { color: s1, width: 2 }, hovertemplate: "%{y:.1f} bpm<extra></extra>" },
  ], { ...base, showlegend: false, yaxis: { ...axis, title: title("Puls (bpm)") } }, config);
  Plotly.react("cadence", [
    { x: D.series.t, y: D.series.cadence, mode: "lines", connectgaps: false, line: { color: s1, width: 2 }, hovertemplate: "%{y:.0f} rpm<extra></extra>" },
  ], { ...base, showlegend: false, yaxis: { ...axis, title: title("Trittfrequenz (rpm)"), range: [50, Math.max(...D.series.cadence.filter((v) => v !== null)) + 5] } }, config);

  // One bar per section, placed where and as wide as the section lies in time.
  const mid = D.sections.map((s) => (s.start + s.seconds / 2) / 60), width = D.sections.map((s) => Math.max(s.seconds / 60 - 0.06, 0.05));
  Plotly.react("bars", [
    { name: "Ø Ist", type: "bar", x: mid, y: D.sections.map((s) => s.power), width, marker: { color: s1 },
      customdata: D.sections.map((s) => [s.nr, s.target]), hovertemplate: "Abschnitt %{customdata[0]}: %{y:.0f} W (Vorgabe %{customdata[1]} W)<extra></extra>" },
    { name: "Vorgabe", x: D.sections.flatMap((s) => [s.start / 60, (s.start + s.seconds) / 60, null]), y: D.sections.flatMap((s) => [s.target, s.target, null]),
      mode: "lines", connectgaps: false, line: { color: s2, width: 2 }, hoverinfo: "skip" },
    { name: "Spitze", x: mid, y: D.sections.map((s) => s.power_max), mode: "markers", marker: { color: surface, size: 8, line: { color: s1, width: 2 } },
      hovertemplate: "Spitze %{y:.0f} W<extra></extra>" },
  ], { ...base, hovermode: "closest", margin: { ...base.margin, b: 70 }, yaxis: { ...axis, title: title("Leistung (W)"), rangemode: "tozero" } }, config);
}
draw();
addEventListener("load", () => document.querySelectorAll(".plot").forEach((plot) => Plotly.Plots.resize(plot)));
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="Report on a ride recorded by ergometer.py and file it in the training log.")
    parser.add_argument("logs", nargs="*", help="log files, joined as one ride (default: the newest in logs/)")
    parser.add_argument("--temperature", type=float, help="room temperature in °C, for logs that did not record it")
    parser.add_argument("--betablocker", action="store_true", help="the rider takes a beta blocker: the pulse is not judged")
    parser.add_argument("--training", action="store_true", help=f"also file the ride in {TRAINING / 'training_log.csv'}")
    parser.add_argument("--rr", help="blood pressure after the ride, SYS/DIA/PULS, for the training log")
    parser.add_argument("--note", default="ohne Kinomap", help="comment for the training log")
    args = parser.parse_args()

    logs = Path(__file__).with_name("logs")
    paths = [Path(log) for log in args.logs] or [max(logs.glob("log-*.csv"), key=lambda p: p.stat().st_mtime)]
    result = analyse(paths, args.temperature, args.betablocker)
    entry = training_row(result) | {"Kommentar": args.note, "RR_training": args.rr or ""}
    result["entry"], result["entry_new"] = file_in_training(entry, args.rr, args.note) if args.training else (entry, False)

    first = read_log(paths[0])[0]["time"]
    date = f"{first[8:10]}.{first[5:7]}.{first[:4]}, {first[11:16]} Uhr"
    out = paths[0].with_name(paths[0].stem.replace("log-", "lauf-") + ".html")
    out.write_text(PAGE.replace("__DATE__", date).replace("__SMOOTH__", str(SMOOTH_SECONDS)).replace("__OVERSHOOT__", str(OVERSHOOT))
                   .replace("__EFFICIENCY__", f"{EFFICIENCY * 100:.0f}").replace("__DATA__", json.dumps(result)))

    for run in result["runs"]:
        print(f"{run['file']}  {run['seconds']:5.0f} s  {run['power']:4.0f} W  {run['kj']:4.0f} kJ  pulse max {run['pulse_max'] or 0:.0f}")
    print(f"together  {result['seconds']:5.0f} s  {result['power']:4.0f} W  {result['kj']:4.0f} kJ  {result['kcal']:.0f} kcal  "
          f"max {result['power_max']:.0f} W  {result['cadence']:.0f} rpm  {result['distance']:.2f} km  pulse max {result['pulse_max'] or 0:.0f}")
    hard = [part for part in result["sections"] if part["work"]]
    print(f"{len(result['sections'])} sections, {len(hard)} hard" + (f", mean {mean(p['power'] for p in hard):.0f} W" if hard else ""))
    if args.training:
        print(f"training log: Nr {result['entry'].get('Nr')} " + ("added" if result["entry_new"] else "was already there, nothing added"))
    else:
        print("training log: not filed (pass --training)")
    print(out)


if __name__ == "__main__":
    main()
