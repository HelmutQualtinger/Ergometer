#!/usr/bin/env python3
# /// script
# dependencies = ["fitparse"]
# ///
"""Pulse against power over all rides that recorded both, as a contour plot and a scatter plot: logs/puls-leistung.html.

Usage:
    uv run puls_leistung.py

The rides are the logs of ergometer.py (logs/log-*.csv), the Kinomap exports (logs/*.zip, ~/training/*.zip) and the
Rouvy files (logs/*.fit). Each ride is cut into windows of WINDOW seconds; a window counts if it was pedalled
throughout and the pulse was measured. The first WARMUP seconds of a ride are left out, since the pulse lags behind
and a log can start with the bike's leftover level. A ride that is in two places (the same export twice) counts once.
The first two charts take every session of the training log since it began (May): mean power against highest pulse,
as a density and as a scatter coloured by the days since the first session. The third is the density of the 10 s
windows, which exist only since October.
"""

import csv
import json
from datetime import date, datetime
from pathlib import Path
from statistics import mean

import kalender
import kinomap
import rouvy

HERE = Path(__file__).parent
WINDOW = 10    # seconds per point
POWER_RANGE = [100, 250]  # watts shown on the x axis
PULSE_RANGE = [80, 130]   # bpm shown on the y axis of the 10 s windows (mean pulse)
MAX_PULSE_RANGE = [95, 140]  # bpm shown on the y axis of the sessions (highest pulse)
WARMUP = 60    # seconds left out at the start of each ride
MIN_RIDE = 300  # seconds; shorter recordings are trial runs


def ergometer_logs() -> list[dict]:
    rides = []
    for path in sorted((HERE / "logs").glob("log-*.csv")):
        with path.open(newline="") as file:
            rows = list(csv.DictReader(file))
        if not rows:
            continue
        start = datetime.fromisoformat(rows[0]["time"])
        samples = [(float(r["seconds"]), float(r["power"] or 0), float(r["heart rate"] or 0), float(r["cadence"] or 0)) for r in rows]
        rides.append({"source": path.name, "start": start, "samples": samples})
    return rides


def kinomap_exports() -> list[dict]:
    rides = []
    for path in sorted(list((HERE / "logs").glob("*.zip")) + list((Path.home() / "training").glob("*.zip"))):
        try:
            rows, facts = kinomap.read_export(path)
        except (KeyError, ValueError, OSError):
            continue  # not a Kinomap export (e.g. Rouvy's share picture)
        if not rows or not facts["start"]:
            continue
        rides.append({"source": path.name, "start": facts["start"].replace(tzinfo=None),
                      "samples": [(r["seconds"], r["power"], r["pulse"], r["cadence"]) for r in rows]})
    return rides


def rouvy_files() -> list[dict]:
    rides = []
    for path in sorted((HERE / "logs").glob("*.fit")):
        rows, facts = rouvy.read_fit(path, path.stem)
        rides.append({"source": path.name, "start": facts["start"].replace(tzinfo=None),
                      "samples": [(r["seconds"], r["power"], r["pulse"], r["cadence"]) for r in rows]})
    return rides


def windows(ride: dict) -> list[tuple[float, float]]:
    """(mean power, mean pulse) per WINDOW seconds, for windows pedalled throughout with a measured pulse."""
    samples = ride["samples"]
    first = samples[0][0]
    buckets = {}
    for seconds, power, pulse, cadence in samples:
        if seconds - first >= WARMUP:
            buckets.setdefault(int((seconds - first) // WINDOW), []).append((power, pulse, cadence))
    points = []
    for bucket in buckets.values():
        if len(bucket) >= WINDOW // 2 and all(p > 0 and c > 0 and h > 0 for p, h, c in bucket):
            points.append((mean(p for p, _, _ in bucket), mean(h for _, h, _ in bucket)))
    return points


PAGE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Puls gegen Leistung</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  :root { color-scheme: light dark; --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
          --grid: #e1e0d9; --axis: #c3c2b7; --seq-lo: #cde2fb; --seq-mid: #3987e5; --seq-hi: #0d366b; }
  @media (prefers-color-scheme: dark) { :root { --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --grid: #2c2c2a;
          --axis: #383835; --seq-lo: #184f95; --seq-mid: #3987e5; --seq-hi: #cde2fb; } }
  body { margin: 0; padding: 20px 16px 40px; background: var(--page); color: var(--ink); font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  main { max-width: 1040px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0; }
  h2 { font-size: 16px; margin: 0 0 2px; }
  .sub, .note { color: var(--ink-2); }
  .note { font-size: 13px; }
  .card { background: var(--surface); border: 1px solid var(--grid); border-radius: 8px; padding: 12px 14px; margin-bottom: 10px; }
  .plot { height: 520px; }
  .back { font-size: 13px; color: var(--ink-2); }
  a { color: inherit; }
</style>
</head>
<body>
<main>
<nav class="back"><a href="index.html">← Trainingskalender</a> · <a href="../../training/training.html">Trainingsübersicht</a></nav>
<h1>Puls gegen Leistung</h1>
<p class="sub" id="sub"></p>
<section class="card"><h2>Einheiten seit Mai: Dichte</h2><div class="note">Jede Einheit des Trainingslogs mit Ø Leistung und höchstem Puls, als Höhenlinien; dunkler = häufiger</div><div class="plot" id="contour"></div></section>
<section class="card"><h2>Einheiten seit Mai nach Datum</h2><div class="note">Ein Punkt je Einheit: Ø Leistung und höchster Puls aus dem Trainingslog; Farbe = Tage seit der ersten Einheit (__FIRST__), hell = früh, dunkel = jüngst</div><div class="plot" id="scatter"></div></section>
<section class="card"><h2>Fahrten seit Oktober: Dichte der __WINDOW__-s-Werte</h2><div class="note">Erst seit Oktober gibt es Messwerte je Sekunde; hier mit dem mittleren statt dem höchsten Puls</div><div class="plot" id="windows"></div></section>
<p class="note">Der höchste Puls einer Einheit liegt naturgemäß über dem mittleren, deshalb hat die Puls-Achse der Einheiten einen anderen Bereich. Messwerte: je __WINDOW__ s gemittelt, nur Abschnitte mit Treten und gemessenem Puls, ohne die erste Minute jeder Fahrt. Der Puls folgt der Leistung mit Verzögerung, und unter Betablocker steigt er wenig – die Streuung nach oben stammt vor allem aus dem Ende langer Abschnitte.</p>
</main>
<script>
const D = __DATA__;
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const n = (value) => value.toLocaleString("de-DE");
document.getElementById("sub").textContent = `${n(D.sessions.length)} Einheiten vom ${D.first} bis ${D.last}; Messwerte je Sekunde von ${n(D.rides)} Fahrten seit ${D.windows_first} (${n(D.power.length)} Punkte zu je ${D.window} s)`;

function draw() {
  const ink = css("--ink-2"), muted = css("--muted"), surface = css("--surface");
  const scale = [[0, css("--seq-lo")], [0.5, css("--seq-mid")], [1, css("--seq-hi")]];
  const axis = { gridcolor: css("--grid"), linecolor: css("--axis"), zeroline: false, tickfont: { color: muted, size: 12 } };
  const title = (text) => ({ text, font: { color: ink, size: 13 } });
  const layout = { margin: { l: 56, r: 20, t: 16, b: 48 }, paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
                   font: { family: getComputedStyle(document.body).fontFamily, color: ink }, showlegend: false, hovermode: "closest",
                   hoverlabel: { bgcolor: surface, bordercolor: css("--axis"), font: { color: css("--ink") } },
                   xaxis: { ...axis, title: title("Ø Leistung (W)"), range: __POWER__ }, yaxis: { ...axis, title: title("höchster Puls (bpm)"), range: __MAXPULSE__ } };
  const fine = { ...layout, xaxis: { ...layout.xaxis, title: title("Leistung (W)") }, yaxis: { ...axis, title: title("mittlerer Puls (bpm)"), range: __PULSE__ } };
  const levels = [[0, css("--surface")], [0.02, css("--seq-lo")], [0.5, css("--seq-mid")], [1, css("--seq-hi")]];
  const S = D.sessions;
  const config = { displayModeBar: false, responsive: true };
  const bar = (text) => ({ title: { text, side: "right", font: { color: ink, size: 13 } }, tickfont: { color: muted, size: 12 }, outlinewidth: 0, thickness: 14 });

  Plotly.react("contour", [{ type: "histogram2dcontour", x: S.map((s) => s.power), y: S.map((s) => s.pulse), colorscale: levels,
    ncontours: 10, contours: { coloring: "fill", showlines: true }, line: { width: 0.6, color: surface },
    xbins: { size: 10 }, ybins: { size: 3 }, colorbar: bar("Anzahl Einheiten"),
    hovertemplate: "%{x} W, %{y} bpm: %{z} Einheiten<extra></extra>" }], layout, config);

  Plotly.react("scatter", [{ type: "scatter", mode: "markers", x: S.map((s) => s.power), y: S.map((s) => s.pulse),
    marker: { size: 10, color: S.map((s) => s.day), colorscale: scale, cmin: 0, cmax: Math.max(...S.map((s) => s.day)),
              line: { width: 1.5, color: surface }, colorbar: bar("Tage seit erster Einheit") },
    customdata: S.map((s) => [s.label, s.minutes]), hovertemplate: "%{customdata[0]}, %{customdata[1]} min: %{x:.0f} W, max. %{y:.0f} bpm<extra></extra>" }], layout, config);

  Plotly.react("windows", [{ type: "histogram2dcontour", x: D.power, y: D.pulse, colorscale: levels,
    ncontours: 14, contours: { coloring: "fill", showlines: true }, line: { width: 0.6, color: surface },
    xbins: { size: 5 }, ybins: { size: 2 }, colorbar: bar("Anzahl Punkte"),
    hovertemplate: "%{x} W, %{y} bpm: %{z} Punkte<extra></extra>" }], fine, config);
}
draw();
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
</script>
</body>
</html>
"""


def main():
    rides, seen = [], set()
    for ride in sorted(ergometer_logs() + kinomap_exports() + rouvy_files(), key=lambda r: r["start"]):
        key = ride["start"].replace(second=0, microsecond=0)
        length = ride["samples"][-1][0] - ride["samples"][0][0] if ride["samples"] else 0
        if key in seen or length < MIN_RIDE:
            continue
        points = windows(ride)
        if points:
            seen.add(key)
            rides.append(ride | {"points": points})
    # Every session of the training log with mean power and highest pulse, since the log began.
    sessions = [s for s in kalender.read_training(kalender.TRAINING / "training_log.csv", date.today()) if s["watt"] and s["pulse"]]
    start = date.fromisoformat(sessions[0]["date"])
    first = min(r["start"] for r in rides).date()
    data = {"power": [], "pulse": [], "day": [], "date": [], "window": WINDOW, "rides": len(rides),
            "windows_first": f"{first:%d.%m.%Y}", "first": f"{start:%d.%m.%Y}", "last": f"{date.fromisoformat(sessions[-1]['date']):%d.%m.%Y}",
            "sessions": [{"power": s["watt"], "pulse": s["pulse"], "day": (date.fromisoformat(s["date"]) - start).days, "minutes": round(s["seconds"] / 60),
                          "label": f"Nr {s['nr']}, {date.fromisoformat(s['date']):%d.%m.}" + (f" {s['route']}" if s["route"] else "")} for s in sessions]}
    for ride in rides:
        day = (ride["start"].date() - first).days
        for power, pulse in ride["points"]:
            data["power"].append(round(power, 1))
            data["pulse"].append(round(pulse, 1))
            data["day"].append(day)
            data["date"].append(f"{ride['start']:%d.%m.}")
    out = HERE / "logs" / "puls-leistung.html"
    out.write_text(PAGE.replace("__WINDOW__", str(WINDOW)).replace("__POWER__", json.dumps(POWER_RANGE)).replace("__PULSE__", json.dumps(PULSE_RANGE)).replace("__MAXPULSE__", json.dumps(MAX_PULSE_RANGE)).replace("__FIRST__", data["first"])
                   .replace("__DATA__", json.dumps(data, ensure_ascii=False)), encoding="utf-8")
    for ride in rides:
        print(f"{ride['start']:%y-%m-%d %H:%M}  {ride['source']:<34} {len(ride['points']):4d} points")
    print(f"{len(rides)} rides, {len(data['power'])} points, {data['first']} to {data['last']}")
    print(out)


if __name__ == "__main__":
    main()
