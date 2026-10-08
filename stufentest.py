#!/usr/bin/env python3
"""Evaluate a step test recorded by ergometer.py and write the result as a Plotly page.

Usage:
    python3 stufentest.py --age 50 --weight 80                    # newest log in logs/
    python3 stufentest.py --age 50 --weight 80 --height 180 --betablocker
    python3 stufentest.py logs/log-26-10-08-00-36-23.csv --age 50 --weight 80

The page lands next to the log as stufentest-<yy-mm-dd-hh-mm-ss>.html. The result is placed in
the measured distributions of two reference populations of men (see FRIEND and SHIP below).
"""

import argparse
import csv
import json
import subprocess
from pathlib import Path
from statistics import NormalDist, mean

STEADY_SECONDS = 60   # a stage is judged by the mean of its last minute
MIN_CADENCE = 40      # rpm; below this the rider has given up
SMOOTH_SECONDS = 10   # the pulse curve is a moving average over this window
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# FRIEND registry, cycle ergometer, men without cardiovascular disease, USA.
# Kaminsky et al., Mayo Clin Proc 2022;97(2):285-293, tables 1-3. Per age decade:
# n, weight (mean, SD), measured VO2peak deciles P10..P90 in ml/kg/min,
# VO2peak in l/min (mean, SD), peak workload in W (mean, SD), peak heart rate (mean, SD).
FRIEND = {
    20: (367, (80.8, 15.6), [28.8, 34.5, 37.4, 40.8, 44.0, 48.3, 52.8, 57.0, 62.2], (3.61, 1.00), (285, 70), (180.9, 16.7)),
    30: (251, (92.6, 20.3), [19.1, 22.6, 25.7, 27.9, 30.2, 31.6, 35.5, 39.0, 50.5], (2.92, 0.86), (236, 73), (165.5, 18.8)),
    40: (446, (94.4, 18.2), [19.8, 21.9, 23.8, 25.4, 27.4, 29.0, 31.4, 35.1, 41.9], (2.65, 0.67), (220, 64), (158.6, 18.2)),
    50: (601, (94.0, 16.9), [17.2, 20.2, 22.0, 23.1, 24.5, 26.3, 28.4, 31.6, 37.1], (2.42, 0.67), (197, 64), (149.3, 20.2)),
    60: (465, (91.1, 16.1), [14.7, 17.5, 19.1, 20.7, 21.7, 23.3, 24.5, 27.0, 31.4], (2.04, 0.58), (162, 54), (139.4, 19.8)),
    70: (257, (88.8, 16.5), [11.0, 14.7, 16.0, 17.1, 18.3, 19.4, 20.6, 22.6, 26.2], (1.63, 0.46), (129, 42), (127.8, 22.4)),
    80: (52, (88.7, 16.3), [8.4, 9.7, 11.1, 12.2, 13.2, 14.6, 16.2, 17.3, 18.7], (1.19, 0.38), (86, 34), (108.4, 25.9)),
}

# SHIP (Study of Health in Pomerania), cycle ergometer, 534 healthy adults, Germany.
# Koch et al., Eur Respir J 2009;33:389-397, tables 3 and 4: quantile regressions for VO2peak.
# Coding: age group 1-5 (25-34 ... 55-64, 64 and older), sex 1 = male, BMI 0 = up to 25, 1 = above.
# Columns: intercept, age, age^2, sex, BMI, age*BMI, sex*BMI, age*sex, age*sex*BMI
SHIP_PER_KG = {5: (30.9643, -2.5661, -0.0263, -3.7224, 1.8765, 0.1082, -2.9703, 0.7361, 0.2799),
               50: (47.7565, -0.9880, -0.2356, -8.8697, 2.3597, -2.0308, -3.7405, 0.2512, 1.3797),
               95: (61.3721, -1.9479, -0.3053, -9.1229, 3.8892, -1.9492, -6.7455, 0.0716, 1.6900)}
# Columns: intercept, age, age^2, sex, age*sex; in ml/min
SHIP_ABSOLUTE = {5: (2966.00, -192.00, -3.3333, -800.00, 76.6667),
                 50: (4307, -241, -20, -1281, 133),
                 95: (6107.67, -502.67, -8.3333, -1124.33, 177.6667)}


def read_log(path: Path) -> list[dict]:
    with path.open(newline="") as file:
        return [{key: value if key == "time" else float(value or 0) for key, value in row.items()}
                for row in csv.DictReader(file)]


def linear_fit(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Least squares: slope, intercept and the correlation coefficient r."""
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / sxx, my - sxy / sxx * mx, sxy / (sxx * syy) ** 0.5


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


def write_pdf(page: Path) -> Path:
    """Print the page with headless Chrome: A4 portrait, two columns, compact."""
    pdf = page.with_suffix(".pdf")
    # The window is as wide as the printable area (210 mm less the margins, at 96 dpi), so the
    # charts are drawn at the size they are printed at.
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--window-size=733,1060",
                    "--virtual-time-budget=8000", f"--print-to-pdf={pdf}", f"{page.resolve().as_uri()}?print"],
                   check=True, capture_output=True)
    return pdf


def percentile_from_deciles(value: float, deciles: list[float]) -> float | None:
    """Where a value lies between the deciles P10..P90; None outside of them."""
    if not deciles[0] <= value <= deciles[-1]:
        return None
    for i in range(len(deciles) - 1):
        low, high = deciles[i], deciles[i + 1]
        if value <= high:
            return 10 * (i + 1) + 10 * (value - low) / (high - low)


def reference(age: float, weight: float, height: float | None, p_max: float, vo2: float) -> dict:
    """Place the result in the measured distributions of men of the same age."""
    decade = min(max(int(age // 10) * 10, 20), 80)
    n, friend_weight, deciles, absolute, workload, pulse = FRIEND[decade]
    normal = {key: NormalDist(*pair) for key, pair in (("absolute", absolute), ("workload", workload), ("weight", friend_weight))}

    # Without a height the BMI is unknown; 81 kg is a BMI of 25 at 1.80 m.
    bmi = weight / (height / 100) ** 2 if height else None
    over = bmi > 25 if bmi else weight > 81
    group = min(max(int((age - 25) // 10) + 1, 1), 5)
    ship_terms = (1, group, group ** 2, 1, over, group * over, over, group, group * over)
    return {
        "decade": decade,
        "friend": {
            "n": n, "weight": friend_weight, "deciles": deciles, "absolute": absolute, "workload": workload, "pulse": pulse,
            "vo2_percentile": percentile_from_deciles(vo2, deciles),
            # Only mean and SD are published for these, so the percentile assumes a normal distribution.
            "workload_percentile": normal["workload"].cdf(p_max) * 100,
            "absolute_percentile": normal["absolute"].cdf(vo2 * weight / 1000) * 100,
            "weight_percentile": normal["weight"].cdf(weight) * 100,
            "workload_quantiles": {p: normal["workload"].inv_cdf(p / 100) for p in (5, 10, 25, 50, 75, 90, 95)},
        },
        "ship": {
            "group": ["25–34", "35–44", "45–54", "55–64", "ab 64"][group - 1], "bmi_over_25": over, "bmi": bmi,
            "per_kg": {p: sum(c * t for c, t in zip(row, ship_terms)) for p, row in SHIP_PER_KG.items()},
            "absolute": {p: sum(c * t for c, t in zip(row, (1, group, group ** 2, 1, group))) / 1000 for p, row in SHIP_ABSOLUTE.items()},
        },
    }


def analyse(rows: list[dict], age: float, weight: float, height: float | None, betablocker: bool) -> dict:
    # The test ends when the rider stops pedalling, whatever was recorded after that.
    end = max(row["seconds"] for row in rows if row["cadence"] >= MIN_CADENCE)
    rows = [row for row in rows if row["seconds"] <= end and row["target"] > 0]

    stages = []
    for row in rows:
        if not stages or stages[-1]["target"] != row["target"]:
            stages.append({"target": row["target"], "rows": []})
        stages[-1]["rows"].append(row)
    step_seconds = max(stage["rows"][-1]["seconds"] - stage["rows"][0]["seconds"] for stage in stages)
    for stage in stages:
        first, last = stage["rows"][0]["seconds"], stage["rows"][-1]["seconds"]
        steady = [row for row in stage["rows"] if row["seconds"] >= last - STEADY_SECONDS]
        pulse = [row["heart rate"] for row in steady if row["heart rate"] > 0]
        stage.update(start=first, duration=last - first, complete=last - first >= step_seconds - 5,
                     power=mean(row["power"] for row in steady), cadence=mean(row["cadence"] for row in steady),
                     # A stage only counts for the pulse if the sensor delivered most of its last minute.
                     pulse=mean(pulse) if len(pulse) > len(steady) / 2 else None)
        del stage["rows"]

    fitted = [stage for stage in stages if stage["complete"] and stage["pulse"]]
    slope, intercept, r = linear_fit([s["power"] for s in fitted], [s["pulse"] for s in fitted])

    # 10-second means of everything measured, for the scatter plot.
    bins = {}
    for row in rows:
        if row["heart rate"] > 0:
            bins.setdefault(int(row["seconds"] // 10), []).append(row)
    cloud = [[mean(r["power"] for r in b), mean(r["heart rate"] for r in b)] for b in bins.values() if len(b) > 10]
    _, _, r_cloud = linear_fit(*zip(*cloud))

    # Maximum power of a step test: the last full stage plus the share of the one that was broken off.
    complete = [stage for stage in stages if stage["complete"]]
    last = stages[-1]
    increment = stages[-1]["target"] - stages[-2]["target"]
    p_max = complete[-1]["target"] + (0 if last["complete"] else increment * last["duration"] / step_seconds)
    vo2 = 10.8 * p_max / weight + 7  # ACSM equation for leg cycling, ml/kg/min
    return {
        "age": age, "weight": weight, "height": height, "betablocker": betablocker, "stages": stages, "cloud": cloud,
        "slope": slope, "intercept": intercept, "r": r, "r_cloud": r_cloud, "fit_stages": len(fitted),
        "p_max": p_max, "vo2": vo2, "vo2_abs": vo2 * weight / 1000,
        "pulse_max": max(row["heart rate"] for row in rows), "pulse_max_expected": 220 - age,
        "pwc130": (130 - intercept) / slope, "duration": end,
        "reference": reference(age, weight, height, p_max, vo2),
        "series": {"t": [round(row["seconds"] / 60, 3) for row in rows], "power": [row["power"] for row in rows],
                   "target": [row["target"] for row in rows],
                   "pulse": smooth_pulse(rows)},
    }


PAGE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stufentest __DATE__</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  :root {
    color-scheme: light dark;
    --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
    --grid: #e1e0d9; --axis: #c3c2b7; --series-1: #2a78d6; --series-2: #eb6834;
  }
  @media (prefers-color-scheme: dark) {
    :root { --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --grid: #2c2c2a; --axis: #383835;
            --series-1: #3987e5; --series-2: #d95926; }
  }
  body { margin: 0; padding: 20px 16px 40px; background: var(--page); color: var(--ink);
         font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  main { max-width: 1040px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0; }
  h2 { font-size: 16px; margin: 0 0 2px; }
  h2.section { font-size: 18px; margin: 22px 0 8px; }
  .sub + h2.section { margin-top: 0; }
  .sub { color: var(--ink-2); margin: 2px 0 16px; }
  /* The key figures stand in a column beside the pulse-against-power chart. */
  .keyrow { display: grid; grid-template-columns: minmax(0, 3fr) minmax(190px, 1fr); gap: 10px; margin-bottom: 10px; }
  .keyrow > .card { margin: 0; min-width: 0; }
  .tiles { display: grid; grid-template-columns: 1fr; gap: 10px; }
  .tiles .card { display: flex; flex-direction: column; justify-content: center; }
  @media (max-width: 700px) { .keyrow { grid-template-columns: 1fr; } .tiles { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); } }
  .card { background: var(--surface); border: 1px solid var(--grid); border-radius: 8px; padding: 12px 14px; margin-bottom: 10px; }
  .tiles .card { margin: 0; }
  .label { color: var(--ink-2); font-size: 13px; }
  .value { font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .value small { font-size: 14px; font-weight: 400; color: var(--ink-2); }
  .note { color: var(--ink-2); font-size: 13px; }
  .plot { height: 300px; }
  .plot.tall { height: 420px; }
  .pair { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 10px; }
  .pair .card { min-width: 0; }
  .scroll { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
  th, td { text-align: right; padding: 5px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
  th { color: var(--ink-2); font-weight: 600; font-size: 13px; }
  th:first-child, td:first-child { text-align: left; white-space: normal; }
  td.you { font-weight: 700; }
  li { margin-bottom: 6px; }
  a { color: inherit; }

  /* Compact two-column layout for the portrait PDF; switched on with ?print in the address,
     so that Plotly draws the charts at their printed size. */
  @page { size: A4 portrait; margin: 8mm; }
  html.print { color-scheme: light; }
  html.print body { padding: 0; font-size: 10.5px; line-height: 1.35; background: #fff; }
  html.print main { max-width: none; display: grid; grid-template-columns: 1fr 1fr; grid-auto-flow: dense; gap: 6px; align-items: start; }
  html.print h1 { font-size: 16px; }
  html.print h2 { font-size: 12px; }
  html.print h2.section { font-size: 13px; margin: 4px 0 0; }
  html.print h2.newpage { break-before: page; }
  html.print h1, html.print .sub, html.print .keyrow, html.print .wide { grid-column: 1 / -1; }
  html.print .sub { margin: 0; }
  html.print .keyrow { gap: 6px; margin: 0; grid-template-columns: minmax(0, 3fr) minmax(150px, 1fr); }
  html.print .tiles { gap: 6px; }
  html.print .pair { display: contents; }
  html.print .card { padding: 6px 8px; margin: 0; min-width: 0; break-inside: avoid; background: #fff; }
  html.print .value { font-size: 17px; }
  html.print .value small, html.print .label, html.print .note, html.print th { font-size: 9.5px; }
  html.print .plot { height: 220px; }
  html.print .plot.tall { height: 300px; }
  html.print th, html.print td { padding: 2px 5px; }
  html.print ul { margin: 4px 0; padding-left: 16px; }
  html.print li { margin-bottom: 3px; }
</style>
<script>if (new URLSearchParams(location.search).has("print")) document.documentElement.classList.add("print");</script>
</head>
<body>
<main>
<h1>Stufentest vom __DATE__</h1>
<p class="sub" id="sub"></p>
<h2 class="section wide">Messung am Ergometer</h2>
<section class="card wide"><h2>Stufen</h2><div class="note">Mittelwerte der jeweils letzten Minute</div><div class="scroll"><table id="stages"></table></div></section>
<section class="pair">
  <div class="card"><h2>Leistung im Verlauf</h2><div class="note">Ist und Vorgabe</div><div class="plot" id="power"></div></div>
  <div class="card"><h2>Puls im Verlauf</h2><div class="note">Gleitendes Mittel über __SMOOTH__ Sekunden</div><div class="plot" id="pulse"></div></div>
</section>
<section class="keyrow">
  <div class="card"><h2>Puls gegen Leistung</h2><div class="note" id="fit-note"></div><div class="plot tall" id="scatter"></div></div>
  <div class="tiles" id="tiles"></div>
</section>
<h2 class="section wide newpage">Vergleich mit Männern desselben Alters</h2>
<section class="card wide"><h2>Beurteilung</h2><ul id="verdict"></ul></section>
<section class="pair">
  <div class="card"><h2>Maximale Leistung im Vergleich</h2><div class="note" id="workload-note"></div><div class="plot" id="workload"></div></div>
  <div class="card"><h2>VO₂max je Kilogramm im Vergleich</h2><div class="note" id="vo2-note"></div><div class="plot" id="vo2"></div></div>
</section>
<section class="card wide"><h2>Quantile der Vergleichsgruppen</h2><div class="note" id="quantile-note"></div><div class="scroll"><table id="quantiles"></table></div></section>
<p class="note wide" id="method"></p>
</main>
<script>
const D = __DATA__;
const F = D.reference.friend, S = D.reference.ship;
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const n = (value, digits = 0) => value.toLocaleString("de-DE", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const clock = (seconds) => `${Math.floor(Math.round(seconds) / 60)}:${String(Math.round(seconds) % 60).padStart(2, "0")}`;
const fit = (watts) => D.intercept + D.slope * watts;
const last = D.stages[D.stages.length - 1];
const ages = `${D.reference.decade} bis ${D.reference.decade + 9}`;
// "P91": the share of the comparison group with a lower value.
const rank = (p) => p === null ? (D.vo2 < F.deciles[0] ? "unter P10" : "über P90") : `P${n(Math.min(Math.max(p, 1), 99))}`;
const better = (p) => `${n(100 - Math.min(Math.max(p, 1), 99))} von 100`;
const shipRange = D.vo2 > S.per_kg[95] ? "über P95" : D.vo2 > S.per_kg[50] ? "zwischen Median und P95" : D.vo2 > S.per_kg[5] ? "zwischen P5 und Median" : "unter P5";

document.getElementById("sub").textContent =
  `${n(D.age)} Jahre, ${n(D.weight)} kg${D.height ? `, ${n(D.height)} cm` : ""}${D.betablocker ? ", Betablocker" : ""} · ` +
  `${D.stages[0].target} W Start, alle ${n(D.stages[0].duration / 60)} min +${D.stages[1].target - D.stages[0].target} W · ` +
  `Abbruch nach ${clock(D.duration)} min in der Stufe ${last.target} W`;

document.getElementById("tiles").innerHTML = [
  ["Maximale Leistung", n(D.p_max), "W", `${n(D.p_max / D.weight, 2)} W/kg`],
  ["Dauer", clock(D.duration), "min", `${D.stages.filter((s) => s.complete).length} volle Stufen, Abbruch bei ${last.target} W`],
  ["Höchster Puls", n(D.pulse_max), "bpm", D.betablocker ? "unter Betablocker" : `${n(D.pulse_max / D.pulse_max_expected * 100)} % von 220 − Alter`],
  ["VO₂max, geschätzt", n(D.vo2, 1), "ml/kg/min", `${n(D.vo2_abs, 1)} l/min, aus der Leistung berechnet`],
  ["Pulsanstieg", n(D.slope * 25, 1), "bpm je 25 W", `r = ${n(D.r, 3)}`],
].map(([label, value, unit, note]) =>
  `<div class="card"><div class="label">${label}</div><div class="value">${value} <small>${unit}</small></div><div class="note">${note}</div></div>`).join("");

document.getElementById("verdict").innerHTML = [
  `<b>Leistung: besser als etwa ${n(Math.min(F.workload_percentile, 99))} von 100 Männern deines Alters.</b> Gemessen wurden bei ${n(F.n)} Männern
   von ${ages} Jahren im Mittel ${n(F.workload[0])} ± ${n(F.workload[1])} W; deine ${n(D.p_max)} W liegen bei ${rank(F.workload_percentile)}.
   Die absolute Leistung begünstigt schwere Fahrer: Mit ${n(D.weight)} kg wiegst du mehr als etwa ${n(F.weight_percentile)} % dieser Gruppe
   (Mittel ${n(F.weight[0])} kg).`,
  `<b>Bezogen auf das Gewicht: ${rank(F.vo2_percentile)} in der US-Gruppe, ${shipRange} in der deutschen.</b> Die geschätzte VO₂max von
   ${n(D.vo2, 1)} ml/kg/min teilt die Leistung durch dein Gewicht, das Mehrgewicht ist hier also eingerechnet. In der deutschen Gruppe
   (gesunde Männer ${S.group}, BMI ${S.bmi_over_25 ? "über" : "bis"} 25) liegt der Median bei ${n(S.per_kg[50], 1)} und P95 bei ${n(S.per_kg[95], 1)} ml/kg/min.`,
  D.betablocker
    ? `<b>Der Puls taugt unter Betablocker nicht als Maßstab.</b> Der flache Anstieg (${n(D.slope * 25, 1)} Schläge je 25 W) und der niedrige
       Höchstpuls von ${n(D.pulse_max)} bpm sind die erwartete Wirkung des Medikaments und sagen nichts über Fitness oder Ausbelastung.
       Formeln wie 220 − Alter und Pulswerte der Vergleichsgruppen (${n(F.pulse[0])} ± ${n(F.pulse[1])} bpm) gelten für dich nicht. Die Gerade
       bleibt nützlich als deine persönliche Zuordnung von Puls zu Leistung, solange Medikament und Dosis gleich bleiben.`
    : `<b>Der Puls steigt flach und streng linear.</b> Je 25 W kommen ${n(D.slope * 25, 1)} Schläge dazu, bei r = ${n(D.r, 3)}. Der höchste Puls
       von ${n(D.pulse_max)} bpm liegt bei ${n(D.pulse_max / D.pulse_max_expected * 100)} % von 220 − Alter; Männer von ${ages} erreichten im
       Mittel ${n(F.pulse[0])} ± ${n(F.pulse[1])} bpm.`,
  `<b>Die Einordnung ist eher vorsichtig.</b> Die Vergleichswerte stammen aus Tests bis zur Erschöpfung${D.betablocker ? ", überwiegend ohne Betablocker, der die Höchstleistung etwas senken kann" : ""}.
   Ob dein Test ausbelastet war, lässt sich aus den Daten ${D.betablocker ? "ohne verwertbaren Puls " : ""}nicht ablesen. Die VO₂max ist aus der Leistung
   geschätzt, in den Vergleichsgruppen wurde sie gemessen; die maximale Leistung ist dagegen direkt vergleichbar.`,
].map((text) => `<li>${text}</li>`).join("");

document.getElementById("workload-note").textContent =
  `Männer ${ages} Jahre, USA (FRIEND), n = ${n(F.n)}: ${n(F.workload[0])} ± ${n(F.workload[1])} W gemessen. Veröffentlicht sind nur Mittelwert und Streuung; ` +
  `die Kurve nimmt eine Normalverteilung an.`;
document.getElementById("vo2-note").textContent =
  `FRIEND: gemessene Dezile, Männer ${ages} Jahre (n = ${n(F.n)}, im Mittel ${n(F.weight[0])} kg). SHIP: gesunde Männer ${S.group}, ` +
  `BMI ${S.bmi_over_25 ? "über" : "bis"} 25${D.height ? "" : " (ohne Größenangabe aus dem Gewicht angenommen)"}.`;

const P = [5, 10, 20, 25, 30, 40, 50, 60, 70, 75, 80, 90, 95];
const cell = (value, digits) => value === undefined ? "<td>–</td>" : `<td>${n(value, digits)}</td>`;
const decile = (p) => p % 10 === 0 && p >= 10 && p <= 90 ? F.deciles[p / 10 - 1] : undefined;
document.getElementById("quantile-note").textContent =
  "Pxx heißt: xx % der Gruppe liegen darunter. Leere Felder sind nicht veröffentlicht. Die Leistung ist aus Mittelwert und Streuung berechnet, alles andere gemessen.";
document.getElementById("quantiles").innerHTML =
  `<tr><th></th>${P.map((p) => `<th>P${p}</th>`).join("")}<th>Du</th><th>Dein Rang</th></tr>` + [
    [`Maximale Leistung (W), FRIEND, Männer ${ages}`, (p) => F.workload_quantiles[p], 0, n(D.p_max), rank(F.workload_percentile)],
    [`VO₂max (ml/kg/min), FRIEND, Männer ${ages}`, decile, 1, n(D.vo2, 1), rank(F.vo2_percentile)],
    [`VO₂max (ml/kg/min), SHIP, Männer ${S.group}, BMI ${S.bmi_over_25 ? ">" : "≤"} 25`, (p) => S.per_kg[p], 1, n(D.vo2, 1), shipRange],
    [`VO₂max (l/min), SHIP, Männer ${S.group}`, (p) => S.absolute[p], 2, n(D.vo2_abs, 2),
     D.vo2_abs > S.absolute[95] ? "über P95" : D.vo2_abs > S.absolute[50] ? "zwischen Median und P95" : "unter dem Median"],
  ].map(([label, value, digits, you, place]) =>
    `<tr><td>${label}</td>${P.map((p) => cell(value(p), digits)).join("")}<td class="you">${you}</td><td class="you">${place}</td></tr>`).join("");

document.getElementById("fit-note").textContent =
  `Ausgleichsgerade über die ${D.fit_stages} vollen Stufen mit Pulsmessung: Puls = ${n(D.intercept, 1)} + ${n(D.slope, 3)} × Leistung, ` +
  `r = ${n(D.r, 3)} (R² = ${n(D.r * D.r, 3)}). Korrelation der 10-Sekunden-Mittel: r = ${n(D.r_cloud, 3)}.`;

document.getElementById("stages").innerHTML =
  "<tr><th>Stufe</th><th>Dauer</th><th>Leistung</th><th>Trittfrequenz</th><th>Puls</th><th>Puls laut Gerade</th></tr>" +
  D.stages.map((s) => `<tr><td>${s.target} W${s.complete ? "" : " (abgebrochen)"}</td><td>${clock(s.duration)}</td><td>${n(s.power)} W</td>
    <td>${n(s.cadence)} rpm</td><td>${s.pulse ? n(s.pulse, 1) + " bpm" : "kein Signal"}</td><td>${n(fit(s.power), 1)} bpm</td></tr>`).join("");

document.getElementById("method").innerHTML =
  "Maximale Leistung: letzte volle Stufe plus der Zeitanteil der abgebrochenen Stufe. VO₂max: ACSM-Formel für das Fahrradergometer (10,8 × W/kg + 7). " +
  "Vergleichsdaten: <a href='https://doi.org/10.1016/j.mayocp.2021.08.020'>FRIEND-Register, Kaminsky et al., Mayo Clin Proc 2022</a> " +
  "(Fahrradergometrie mit Atemgasmessung, Männer ohne Herz-Kreislauf-Erkrankung, USA) und " +
  "<a href='https://doi.org/10.1183/09031936.00074208'>SHIP, Koch et al., Eur Respir J 2009</a> (534 gesunde Erwachsene, Vorpommern, Quantilsregression). " +
  "Alle Werte stammen aus einem Heimtest und ersetzen keine ärztliche Ergometrie.";

// Standard normal distribution function (Abramowitz-Stegun), for the workload curve.
const phi = (z) => { const t = 1 / (1 + 0.2316419 * Math.abs(z)), d = 0.3989423 * Math.exp(-z * z / 2);
  const p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274)))); return z > 0 ? 1 - p : p; };

function draw() {
  const ink = css("--ink-2"), s1 = css("--series-1"), s2 = css("--series-2"), surface = css("--surface"), strong = css("--ink");
  // The PDF layout is smaller, and so is the lettering of its charts.
  const compact = document.documentElement.classList.contains("print"), small = compact ? 9 : 12;
  const axis = { gridcolor: css("--grid"), linecolor: css("--axis"), zeroline: false, tickfont: { color: css("--muted"), size: small },
                 titlefont: { color: ink, size: small + 1 }, title: { standoff: compact ? 4 : 10 } };
  const layout = (x, y, extra = {}) => ({
    margin: { l: compact ? 40 : 52, r: 12, t: 10, b: compact ? 32 : 44 }, paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: getComputedStyle(document.body).fontFamily, color: ink, size: small }, hovermode: "x unified",
    hoverlabel: { bgcolor: surface, bordercolor: css("--axis"), font: { color: strong } },
    legend: { orientation: "h", x: 0, y: 1.14, font: { color: ink, size: small } },
    xaxis: { ...axis, title: { text: x, standoff: axis.title.standoff }, showgrid: false },
    yaxis: { ...axis, title: { text: y, standoff: axis.title.standoff } }, ...extra,
  });
  const config = { displayModeBar: false, responsive: true };
  // The rider's own value: a vertical line in ink, labelled at the top.
  const you = (x, text) => ({
    shapes: [{ type: "line", x0: x, x1: x, yref: "paper", y0: 0, y1: 1, line: { color: strong, width: 2 } }],
    annotations: [{ x, yref: "paper", y: 1, yanchor: "bottom", text, showarrow: false, font: { color: strong, size: small } }],
  });
  const share = { ...axis, title: { text: "Anteil darunter (%)", standoff: axis.title.standoff }, range: [0, 100], dtick: 25 };
  const top = { l: compact ? 40 : 52, r: 12, t: compact ? 18 : 26, b: compact ? 32 : 44 };

  const [mu, sd] = F.workload, watts = Array.from({ length: 81 }, (_, i) => mu - 3 * sd + i * sd * 6 / 80).filter((w) => w > 0);
  Plotly.react("workload", [
    { x: watts, y: watts.map((w) => 100 * phi((w - mu) / sd)), mode: "lines", line: { color: s1, width: 2 },
      hovertemplate: "%{x:.0f} W: %{y:.0f} % liegen darunter<extra></extra>" },
  ], layout("Maximale Leistung (W)", "", { showlegend: false, hovermode: "closest", yaxis: share, margin: top,
                                            ...you(D.p_max, `Du: ${n(D.p_max)} W, ${rank(F.workload_percentile)}`) }), config);

  Plotly.react("vo2", [
    { name: "FRIEND (USA), gemessene Dezile", x: F.deciles, y: F.deciles.map((_, i) => 10 * (i + 1)), mode: "lines+markers",
      line: { color: s1, width: 2 }, marker: { color: s1, size: 8, line: { color: surface, width: 2 } }, hovertemplate: "P%{y}: %{x:.1f} ml/kg/min<extra>FRIEND</extra>" },
    { name: "SHIP (Deutschland), P5 · Median · P95", x: [5, 50, 95].map((p) => S.per_kg[p]), y: [5, 50, 95], mode: "lines+markers",
      line: { color: s2, width: 2, dash: "dot" }, marker: { color: s2, size: 9, symbol: "diamond", line: { color: surface, width: 2 } },
      hovertemplate: "P%{y}: %{x:.1f} ml/kg/min<extra>SHIP</extra>" },
  ], layout("VO₂max (ml/kg/min)", "", { hovermode: "closest", yaxis: share, margin: { ...top, b: compact ? 58 : 80 }, legend: { orientation: "h", x: 0, y: compact ? -0.32 : -0.3, font: { color: ink, size: small } },
                                         ...you(D.vo2, `Du: ${n(D.vo2, 1)}, ${rank(F.vo2_percentile)}`) }), config);

  Plotly.react("power", [
    { name: "Ist", x: D.series.t, y: D.series.power, mode: "lines", line: { color: s1, width: 2 }, hovertemplate: "%{y:.0f} W<extra>Ist</extra>" },
    { name: "Vorgabe", x: D.series.t, y: D.series.target, mode: "lines", line: { color: s2, width: 2, shape: "hv" }, hovertemplate: "%{y:.0f} W<extra>Vorgabe</extra>" },
  ], layout("Zeit (min)", "Leistung (W)", { xaxis: { ...axis, title: { text: "Zeit (min)", standoff: axis.title.standoff }, showgrid: false, hoverformat: ".1f" },
                                            yaxis: { ...axis, title: { text: "Leistung (W)", standoff: axis.title.standoff }, range: [80, 300] } }), config);

  Plotly.react("pulse", [
    { x: D.series.t, y: D.series.pulse, mode: "lines", connectgaps: false, line: { color: s1, width: 2 }, hovertemplate: "%{y:.1f} bpm<extra></extra>" },
  ], layout("Zeit (min)", "Puls (bpm)", { showlegend: false, xaxis: { ...axis, title: { text: "Zeit (min)", standoff: axis.title.standoff }, showgrid: false, hoverformat: ".1f", range: [0, D.duration / 60] },
                                          yaxis: { ...axis, title: { text: "Puls (bpm)", standoff: axis.title.standoff }, range: [100, 130] } }), config);

  const full = D.stages.filter((s) => s.complete && s.pulse), broken = D.stages.filter((s) => !s.complete && s.pulse);
  const from = Math.min(...full.map((s) => s.power)), to = Math.max(...full.map((s) => s.power));
  const reach = Math.max(D.pwc130, last.power) + 10;
  Plotly.react("scatter", [
    { name: "10-Sekunden-Mittel", x: D.cloud.map((p) => p[0]), y: D.cloud.map((p) => p[1]), mode: "markers",
      marker: { color: s1, size: 5, opacity: 0.3 }, hovertemplate: "%{x:.0f} W · %{y:.0f} bpm<extra></extra>" },
    { name: "Ausgleichsgerade", x: [from, to], y: [fit(from), fit(to)], mode: "lines", line: { color: s2, width: 2 }, hoverinfo: "skip" },
    { name: "Gerade, verlängert", x: [to, reach], y: [fit(to), fit(reach)], mode: "lines", line: { color: s2, width: 2, dash: "dot" }, hoverinfo: "skip" },
    { name: "Stufenmittel", x: full.map((s) => s.power), y: full.map((s) => s.pulse), mode: "markers+text",
      text: full.map((s) => `${s.target} W`), textposition: "top left", textfont: { color: ink, size: small },
      marker: { color: s1, size: 11, line: { color: surface, width: 2 } }, hovertemplate: "%{x:.0f} W · %{y:.1f} bpm<extra>Stufenmittel</extra>" },
    { name: "Abgebrochene Stufe", x: broken.map((s) => s.power), y: broken.map((s) => s.pulse), mode: "markers",
      marker: { color: surface, size: 11, line: { color: s1, width: 2 } }, hovertemplate: "%{x:.0f} W · %{y:.1f} bpm<extra>abgebrochen</extra>" },
  ], layout("Leistung (W)", "Puls (bpm)", {
    hovermode: "closest", xaxis: { ...axis, title: { text: "Leistung (W)", standoff: axis.title.standoff } },
    annotations: [{ x: D.pwc130, y: 130, text: `130 bpm bei ${n(D.pwc130)} W`, showarrow: true, arrowcolor: css("--muted"), ax: -70, ay: -24, font: { color: ink, size: small } }],
  }), config);
}
draw();
// Once the layout has settled, fit every chart to its card (matters for the PDF's columns).
addEventListener("load", () => document.querySelectorAll(".plot").forEach((plot) => Plotly.Plots.resize(plot)));
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="Evaluate a step test recorded by ergometer.py (reference data: men).")
    parser.add_argument("log", nargs="?", help="log file (default: the newest in logs/)")
    parser.add_argument("--age", type=float, required=True, help="age in years")
    parser.add_argument("--weight", type=float, required=True, help="body weight in kg")
    parser.add_argument("--height", type=float, help="height in cm; decides the BMI group of the German reference data")
    parser.add_argument("--betablocker", action="store_true", help="the rider takes a beta blocker: the pulse is not judged")
    parser.add_argument("--pdf", action="store_true", help="also write a compact two-column portrait PDF (needs Google Chrome)")
    args = parser.parse_args()

    logs = Path(__file__).with_name("logs")
    path = Path(args.log) if args.log else max(logs.glob("log-*.csv"), key=lambda p: p.stat().st_mtime)
    rows = read_log(path)
    result = analyse(rows, args.age, args.weight, args.height, args.betablocker)
    date = rows[0]["time"][:16].replace("T", " ")
    date = f"{date[8:10]}.{date[5:7]}.{date[:4]}, {date[11:]} Uhr"
    out = path.with_name(path.stem.replace("log-", "stufentest-") + ".html")
    out.write_text(PAGE.replace("__DATE__", date).replace("__SMOOTH__", str(SMOOTH_SECONDS)).replace("__DATA__", json.dumps(result)))

    friend, ship = result["reference"]["friend"], result["reference"]["ship"]
    for stage in result["stages"]:
        pulse = f"{stage['pulse']:.1f} bpm" if stage["pulse"] else "no pulse"
        print(f"{stage['target']:4.0f} W  {stage['duration']:4.0f} s  {stage['power']:4.0f} W  {pulse}")
    print(f"pulse = {result['intercept']:.1f} + {result['slope']:.3f} x power, r = {result['r']:.3f}")
    print(f"max power {result['p_max']:.0f} W ({result['p_max'] / args.weight:.2f} W/kg): P{friend['workload_percentile']:.0f} "
          f"of men {result['reference']['decade']}-{result['reference']['decade'] + 9} (FRIEND {friend['workload'][0]} +- {friend['workload'][1]} W, normal approximation)")
    vo2_rank = f"P{friend['vo2_percentile']:.0f}" if friend["vo2_percentile"] else "outside P10-P90"
    print(f"VO2max {result['vo2']:.1f} ml/kg/min (estimated): {vo2_rank} in FRIEND, deciles {friend['deciles']}")
    print(f"SHIP men {ship['group']}, BMI {'>' if ship['bmi_over_25'] else '<='} 25: " +
          ", ".join(f"P{p} {value:.1f}" for p, value in ship["per_kg"].items()) + " ml/kg/min")
    print(out)
    if args.pdf:
        print(write_pdf(out))


if __name__ == "__main__":
    main()
