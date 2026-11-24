#!/usr/bin/env python3
"""The FTP estimated from the training log over time, as logs/ftp.html.

Usage:
    python3 ftp_verlauf.py

There is no FTP test in the log, so the estimate is the best mean power of a session of at least
LONG seconds within the last WINDOW days: a rider holds about their FTP for 30 to 40 minutes. It is
a lower bound, since few rides are all-out. The chart also shows every session of at least SHORT
seconds and the training FTP the profiles are scaled from.
"""

import json
from datetime import date, timedelta
from pathlib import Path

import kalender

FTP = 200      # watts, the training reference (kalender.py, kinomap.py)
SHORT = 1200   # seconds; sessions shown as points
LONG = 1800    # seconds; sessions that count for the estimate
WINDOW = 28    # days the estimate looks back

PAGE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FTP-Verlauf</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  :root { color-scheme: light dark; --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
          --grid: #e1e0d9; --axis: #c3c2b7; --series-1: #2a78d6; --series-2: #eb6834; --dots: #9ec5f4; }
  @media (prefers-color-scheme: dark) { :root { --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --grid: #2c2c2a;
          --axis: #383835; --series-1: #3987e5; --series-2: #d95926; --dots: #256abf; } }
  body { margin: 0; padding: 20px 16px 40px; background: var(--page); color: var(--ink); font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  main { max-width: 1040px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0; }
  .sub, .note { color: var(--ink-2); }
  .note { font-size: 13px; }
  .card { background: var(--surface); border: 1px solid var(--grid); border-radius: 8px; padding: 12px 14px; margin-bottom: 10px; }
  .plot { height: 480px; }
  .back { font-size: 13px; color: var(--ink-2); }
  a { color: inherit; }
</style>
</head>
<body>
<main>
<nav class="back"><a href="index.html">← Trainingskalender</a> · <a href="../../training/training.html">Trainingsübersicht</a></nav>
<h1>FTP-Verlauf</h1>
<p class="sub">Geschätzte FTP = beste Ø Leistung einer Einheit ab __LONG__ min in den letzten __WINDOW__ Tagen</p>
<section class="card"><div class="plot" id="plot"></div></section>
<p class="note">Kein FTP-Test: Die Schätzung beruht auf gefahrenen Einheiten und ist eine Untergrenze, weil kaum eine davon voll ausgefahren wurde. Punkte: alle Einheiten ab __SHORT__ min. Alle Leistungen sind Tabellenwerte des AX 4000 aus Stufe und Trittfrequenz.</p>
</main>
<script>
const D = __DATA__;
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
function draw() {
  const ink = css("--ink-2"), muted = css("--muted"), surface = css("--surface");
  const axis = { gridcolor: css("--grid"), linecolor: css("--axis"), zeroline: false, tickfont: { color: muted, size: 12 } };
  Plotly.react("plot", [
    { name: "Einheiten ab __SHORT__ min", x: D.points.map((p) => p.date), y: D.points.map((p) => p.watt), mode: "markers",
      marker: { size: 8, color: css("--dots"), line: { width: 1.5, color: surface } },
      customdata: D.points.map((p) => [p.label, p.minutes]), hovertemplate: "%{customdata[0]}, %{customdata[1]} min: %{y:.0f} W<extra></extra>" },
    { name: "geschätzte FTP", x: D.estimate.map((p) => p.date), y: D.estimate.map((p) => p.watt), mode: "lines", line: { color: css("--series-1"), width: 2, shape: "hv" },
      hovertemplate: "%{x|%d.%m.}: geschätzte FTP %{y:.0f} W<extra></extra>" },
  ], { margin: { l: 56, r: 110, t: 16, b: 40 }, paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)", hovermode: "closest",
       font: { family: getComputedStyle(document.body).fontFamily, color: ink },
       hoverlabel: { bgcolor: surface, bordercolor: css("--axis"), font: { color: css("--ink") } },
       legend: { orientation: "h", x: 0, y: -0.12, font: { color: ink } },
       xaxis: { ...axis, type: "date", tickformat: "%d.%m.", showgrid: false },
       yaxis: { ...axis, title: { text: "Leistung (W)", font: { color: ink, size: 13 } } },
       shapes: [{ type: "line", xref: "paper", x0: 0, x1: 1, y0: D.ftp, y1: D.ftp, line: { color: css("--series-2"), width: 2, dash: "dash" } }],
       annotations: [{ xref: "paper", x: 1, xanchor: "left", y: D.ftp, text: ` Trainings-FTP ${D.ftp} W`, showarrow: false, font: { color: ink, size: 12 } }] },
     { displayModeBar: false, responsive: true });
}
draw();
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
</script>
</body>
</html>
"""


def main():
    sessions = [s for s in kalender.read_training(kalender.TRAINING / "training_log.csv", date.today()) if s["watt"] and s["seconds"] >= SHORT]
    points = [{"date": s["date"], "watt": s["watt"], "minutes": round(s["seconds"] / 60),
               "label": f"Nr {s['nr']}, {date.fromisoformat(s['date']):%d.%m.}" + (f" {s['route']}" if s["route"] else "")} for s in sessions]
    long = [(date.fromisoformat(s["date"]), s["watt"]) for s in sessions if s["seconds"] >= LONG]
    estimate, day = [], long[0][0]
    while day <= date.today():
        recent = [watt for when, watt in long if day - timedelta(days=WINDOW) < when <= day]
        if recent:
            estimate.append({"date": day.isoformat(), "watt": max(recent)})
        day += timedelta(days=1)
    out = Path(__file__).with_name("logs") / "ftp.html"
    out.write_text(PAGE.replace("__LONG__", str(LONG // 60)).replace("__SHORT__", str(SHORT // 60)).replace("__WINDOW__", str(WINDOW))
                   .replace("__DATA__", json.dumps({"ftp": FTP, "points": points, "estimate": estimate}, ensure_ascii=False)), encoding="utf-8")
    print(f"{len(points)} sessions, estimate from {estimate[0]['date']} ({estimate[0]['watt']:.0f} W) to {estimate[-1]['date']} ({estimate[-1]['watt']:.0f} W)")
    print(out)


if __name__ == "__main__":
    main()
