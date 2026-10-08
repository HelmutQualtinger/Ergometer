#!/usr/bin/env python3
"""Show one ride from one or more logs of ergometer.py as a Plotly page.

Usage:
    python3 lauf.py logs/log-26-10-08-12-14-59.csv logs/log-26-10-08-12-28-19.csv

Several logs are joined in the order given, as one ride. What happened between two logs is not
recorded - a break or a recording that was stopped by mistake - so the page only calls it a gap. The page lands next to
the first log as lauf-<yy-mm-dd-hh-mm-ss>.html.
"""

import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from statistics import mean

SMOOTH_SECONDS = 10  # the pulse curve is a moving average over this window
EFFICIENCY = 0.25    # share of the energy turned over that reaches the pedals; as in training_log.csv


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
    return {"seconds": seconds, "kj": work / 1000, "kcal": work / 1000 / 4.185 / EFFICIENCY, "power": work / seconds,
            "power_max": max(row["power"] for row in rows), "cadence": mean(row["cadence"] for row in rows),
            "pulse": mean(pulse) if pulse else None, "pulse_max": max(pulse) if pulse else None}


def analyse(paths: list[Path]) -> dict:
    runs, series, offset, previous_end = [], {"t": [], "power": [], "target": [], "pulse": []}, 0.0, None
    levels = {}
    for path in paths:
        rows = read_log(path)
        start = datetime.fromisoformat(rows[0]["time"])
        run = summary(rows) | {"file": path.name, "start": rows[0]["time"][11:16], "offset": offset,
                               "pause": (start - previous_end).total_seconds() if previous_end else None}
        runs.append(run)
        pulse = smooth_pulse(rows)
        for row, beat in zip(rows, pulse):
            series["t"].append(round((offset + row["seconds"]) / 60, 3))
            series["power"].append(row["power"])
            series["target"].append(row["target"])
            series["pulse"].append(beat)
        for key in series:  # a gap, so the curves of two logs are not joined
            series[key].append(None)
        for a, b in zip(rows, rows[1:]):
            if a["target"] > 0:
                level = levels.setdefault(a["target"], {"seconds": 0.0, "work": 0.0, "pulse": []})
                level["seconds"] += b["seconds"] - a["seconds"]
                level["work"] += a["power"] * (b["seconds"] - a["seconds"])
                if a["heart rate"] > 0:
                    level["pulse"].append(a["heart rate"])
        offset += rows[-1]["seconds"]
        previous_end = datetime.fromisoformat(rows[-1]["time"])

    work = sum(run["kj"] for run in runs)
    pulses = [run["pulse_max"] for run in runs if run["pulse_max"]]
    return {
        "runs": runs, "series": series, "seconds": offset, "kj": work, "kcal": work / 4.185 / EFFICIENCY,
        "power": work * 1000 / offset, "power_max": max(run["power_max"] for run in runs),
        "cadence": sum(run["cadence"] * run["seconds"] for run in runs) / offset,
        "pulse_max": max(pulses) if pulses else None,
        "levels": [{"target": target, "seconds": level["seconds"], "power": level["work"] / level["seconds"],
                    "pulse": mean(level["pulse"]) if level["pulse"] else None} for target, level in sorted(levels.items())],
    }


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
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; margin-bottom: 10px; }
  .card { background: var(--surface); border: 1px solid var(--grid); border-radius: 8px; padding: 12px 14px; margin-bottom: 10px; min-width: 0; }
  .tiles .card { margin: 0; }
  .pair { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 10px; }
  .label, .note { color: var(--ink-2); font-size: 13px; }
  .value { font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .value small { font-size: 14px; font-weight: 400; color: var(--ink-2); }
  .plot { height: 320px; }
  .plot.low { height: 240px; }
  .scroll { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
  th, td { text-align: right; padding: 5px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
  th { color: var(--ink-2); font-weight: 600; font-size: 13px; }
  th:first-child, td:first-child { text-align: left; }
  tr.total td { font-weight: 700; border-bottom: 0; }
</style>
</head>
<body>
<main>
<h1>Lauf vom __DATE__</h1>
<p class="sub" id="sub"></p>
<section class="tiles" id="tiles"></section>
<section class="card"><h2>Leistung im Verlauf</h2><div class="note">Ist und Vorgabe über die gefahrene Zeit; die senkrechte Linie zeigt eine Lücke in der Aufzeichnung.</div><div class="plot" id="power"></div></section>
<section class="card"><h2>Puls im Verlauf</h2><div class="note">Gleitendes Mittel über __SMOOTH__ Sekunden</div><div class="plot low" id="pulse"></div></section>
<section class="pair">
  <div class="card"><h2>Aufzeichnungen</h2><div class="scroll"><table id="runs"></table></div></div>
  <div class="card"><h2>Nach Vorgabestufe</h2><div class="note">Alle Abschnitte mit derselben Zielleistung zusammengenommen</div><div class="scroll"><table id="levels"></table></div></div>
</section>
<p class="note">Kalorien: Arbeit am Pedal bei __EFFICIENCY__ % Wirkungsgrad, wie im Trainingslog.</p>
</main>
<script>
const D = __DATA__;
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const n = (value, digits = 0) => value === null ? "–" : value.toLocaleString("de-DE", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const clock = (seconds) => `${Math.floor(Math.round(seconds) / 60)}:${String(Math.round(seconds) % 60).padStart(2, "0")}`;
const breaks = D.runs.filter((run) => run.pause !== null);

document.getElementById("sub").textContent = `${D.runs.length} Aufzeichnung${D.runs.length > 1 ? "en" : ""}, Start ${D.runs.map((run) => run.start).join(" und ")} Uhr` +
  (breaks.length ? ` · nicht aufgezeichnet dazwischen: ${breaks.map((run) => clock(run.pause)).join(", ")} min` : "");
document.getElementById("tiles").innerHTML = [
  ["Gefahrene Zeit", clock(D.seconds), "min", D.runs.map((run) => clock(run.seconds)).join(" + ")],
  ["Durchschnittsleistung", n(D.power), "W", `höchster Wert ${n(D.power_max)} W`],
  ["Arbeit", n(D.kj), "kJ", `etwa ${n(D.kcal)} kcal`],
  ["Höchster Puls", n(D.pulse_max), "bpm", `Ø Trittfrequenz ${n(D.cadence)} rpm`],
].map(([label, value, unit, note]) => `<div class="card"><div class="label">${label}</div><div class="value">${value} <small>${unit}</small></div><div class="note">${note}</div></div>`).join("");

const row = (cells, total) => `<tr${total ? ' class="total"' : ""}>${cells.map((cell) => `<td>${cell}</td>`).join("")}</tr>`;
document.getElementById("runs").innerHTML = "<tr><th>Start</th><th>Dauer</th><th>Ø Leistung</th><th>Arbeit</th><th>kcal</th><th>Puls Ø / max</th></tr>" +
  D.runs.map((run) => row([`${run.start} Uhr`, clock(run.seconds), `${n(run.power)} W`, `${n(run.kj)} kJ`, n(run.kcal), `${n(run.pulse)} / ${n(run.pulse_max)}`])).join("") +
  (D.runs.length > 1 ? row(["Zusammen", clock(D.seconds), `${n(D.power)} W`, `${n(D.kj)} kJ`, n(D.kcal), `– / ${n(D.pulse_max)}`], true) : "");
document.getElementById("levels").innerHTML = "<tr><th>Vorgabe</th><th>Zeit</th><th>Ø Ist</th><th>Ø Puls</th></tr>" +
  D.levels.map((level) => row([`${n(level.target)} W`, clock(level.seconds), `${n(level.power)} W`, level.pulse === null ? "–" : `${n(level.pulse)} bpm`])).join("");

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
                 legend: { orientation: "h", x: 0, y: -0.22, font: { color: ink } }, xaxis: minutes, ...marks };
  const config = { displayModeBar: false, responsive: true };
  Plotly.react("power", [
    { name: "Ist", x: D.series.t, y: D.series.power, mode: "lines", connectgaps: false, line: { color: s1, width: 2 }, hovertemplate: "%{y:.0f} W<extra>Ist</extra>" },
    { name: "Vorgabe", x: D.series.t, y: D.series.target, mode: "lines", connectgaps: false, line: { color: s2, width: 2, shape: "hv" }, hovertemplate: "%{y:.0f} W<extra>Vorgabe</extra>" },
  ], { ...base, margin: { ...base.margin, b: 70 }, yaxis: { ...axis, title: title("Leistung (W)"), rangemode: "tozero" } }, config);
  Plotly.react("pulse", [
    { x: D.series.t, y: D.series.pulse, mode: "lines", connectgaps: false, line: { color: s1, width: 2 }, hovertemplate: "%{y:.1f} bpm<extra></extra>" },
  ], { ...base, showlegend: false, yaxis: { ...axis, title: title("Puls (bpm)") } }, config);
}
draw();
addEventListener("load", () => document.querySelectorAll(".plot").forEach((plot) => Plotly.Plots.resize(plot)));
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
</script>
</body>
</html>
"""


def main():
    paths = [Path(argument) for argument in sys.argv[1:]]
    if not paths:
        sys.exit(__doc__)
    result = analyse(paths)
    first = read_log(paths[0])[0]["time"]
    date = f"{first[8:10]}.{first[5:7]}.{first[:4]}, {first[11:16]} Uhr"
    out = paths[0].with_name(paths[0].stem.replace("log-", "lauf-") + ".html")
    out.write_text(PAGE.replace("__DATE__", date).replace("__SMOOTH__", str(SMOOTH_SECONDS))
                   .replace("__EFFICIENCY__", f"{EFFICIENCY * 100:.0f}").replace("__DATA__", json.dumps(result)))
    for run in result["runs"]:
        print(f"{run['file']}  {run['seconds']:5.0f} s  {run['power']:4.0f} W  {run['kj']:4.0f} kJ  pulse max {run['pulse_max'] or 0:.0f}")
    print(f"together  {result['seconds']:5.0f} s  {result['power']:4.0f} W  {result['kj']:4.0f} kJ  {result['kcal']:.0f} kcal  pulse max {result['pulse_max'] or 0:.0f}")
    print(out)


if __name__ == "__main__":
    main()
