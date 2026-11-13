#!/usr/bin/env python3
"""Draw every power profile in profiles/ as a step chart on one page, logs/vorgaben.html.

Usage:
    python3 vorgaben.py

Each chart shows the target power over the profile's time with the FTP as a line; the tiles give
duration, mean power, work and the time at or above the FTP. The description of each profile is
its line in README.md. The page links back to the calendar, which links here.
"""

import html
import json
import re
from pathlib import Path

FTP = 200  # watts, as in kalender.py and kinomap.py
HERE = Path(__file__).parent


def read_profile(path: Path) -> list[tuple[int, int]]:
    """The rows "mm:ss,watt" as (seconds, watt), as ergometer.py's parse_profile reads them."""
    steps = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if match := re.fullmatch(r"\s*(\d+):(\d\d)\s*,\s*(\d+(?:\.\d+)?)\s*", line):
            steps.append((int(match[1]) * 60 + int(match[2]), round(float(match[3]))))
    return steps


def describe(readme: str, name: str) -> str:
    """The profile's description from its line in the README's list of profiles."""
    match = re.search(rf"^- `{re.escape(name)}` \(([^)]*)\): (.*)$", readme, re.MULTILINE)
    return match[2] if match else ""


def profiles() -> list[dict]:
    readme = (HERE / "README.md").read_text(encoding="utf-8")
    found = []
    for path in sorted((HERE / "profiles").glob("vorgabe-*.csv")):
        steps = read_profile(path)
        if len(steps) < 2:
            continue
        spans = list(zip(steps, steps[1:]))
        seconds = steps[-1][0] - steps[0][0]
        work = sum((b[0] - a[0]) * a[1] for a, b in spans)
        found.append({"name": path.stem.removeprefix("vorgabe-"), "file": path.name, "seconds": seconds, "power": work / seconds, "kj": work / 1000,
                      "max": max(watt for _, watt in steps), "above": sum(b[0] - a[0] for a, b in spans if a[1] >= FTP),
                      "description": describe(readme, path.name),
                      # The steps as a line that holds each value until the next row; the last row (0 W) ends it.
                      "t": [time / 60 for time, _ in steps], "watt": [watt for _, watt in steps[:-1]] + [steps[-2][1]]})
    return found


PAGE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vorgaben</title>
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
  .card { background: var(--surface); border: 1px solid var(--grid); border-radius: 8px; padding: 12px 14px; margin-bottom: 10px; }
  .facts { display: flex; flex-wrap: wrap; gap: 4px 18px; font-variant-numeric: tabular-nums; margin: 4px 0; }
  .facts b { font-size: 17px; }
  .note { color: var(--ink-2); font-size: 13px; }
  .plot { height: 220px; }
  .index { columns: 2 260px; margin: 0; padding-left: 20px; }
  .back { font-size: 13px; color: var(--ink-2); }
  a { color: inherit; }
</style>
</head>
<body>
<main>
<nav class="back"><a href="index.html">← Trainingskalender</a> · <a href="../../training/training.html">Trainingsübersicht</a></nav>
<h1>Vorgaben</h1>
<p class="sub">Alle Profile aus <code>profiles/</code>, Zielleistung über die Zeit; gestrichelt die FTP von __FTP__ W</p>
<section class="card"><ul class="index" id="index"></ul></section>
<div id="profiles"></div>
</main>
<script>
const P = __DATA__, FTP = __FTP__;
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const n = (value, digits = 0) => value.toLocaleString("de-DE", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const clock = (seconds) => `${Math.floor(seconds / 60)}:${String(Math.round(seconds) % 60).padStart(2, "0")}`;
const text = (value) => Object.assign(document.createElement("span"), { textContent: value }).innerHTML;

document.getElementById("index").innerHTML = P.map((p) => `<li><a href="#${p.name}">${text(p.name)}</a> – ${clock(p.seconds)} min, Ø ${n(p.power)} W</li>`).join("");
document.getElementById("profiles").innerHTML = P.map((p) => `<section class="card" id="${p.name}">
  <h2>${text(p.name)}</h2>
  <div class="facts"><span><b>${clock(p.seconds)}</b> min</span><span>Ø <b>${n(p.power)}</b> W (${n(p.power / FTP * 100)} % FTP)</span>
    <span><b>${n(p.kj)}</b> kJ</span><span>bis <b>${n(p.max)}</b> W</span><span><b>${clock(p.above)}</b> min ab FTP</span></div>
  <div class="note">${text(p.description).replace(/`([^`]*)`/g, "<code>$1</code>")}</div>
  <div class="plot" id="plot-${p.name}"></div></section>`).join("");

function draw() {
  const ink = css("--ink-2"), s1 = css("--series-1"), s2 = css("--series-2"), muted = css("--muted");
  const axis = { gridcolor: css("--grid"), linecolor: css("--axis"), zeroline: false, tickfont: { color: muted, size: 12 } };
  const top = Math.max(...P.map((p) => p.max)) * 1.08;
  for (const p of P) Plotly.react(`plot-${p.name}`, [
    { x: p.t, y: p.watt, mode: "lines", line: { shape: "hv", color: s1, width: 2 }, fill: "tozeroy", fillcolor: s1 + "33", hovertemplate: "%{y} W ab %{x:.2f} min<extra></extra>" },
  ], { margin: { l: 48, r: 76, t: 10, b: 38 }, paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)", showlegend: false, hovermode: "closest",
       font: { family: getComputedStyle(document.body).fontFamily, color: ink },
       xaxis: { ...axis, title: { text: "Zeit (min)", font: { color: ink, size: 13 } }, range: [0, p.seconds / 60], showgrid: false },
       yaxis: { ...axis, title: { text: "Watt", font: { color: ink, size: 13 } }, range: [0, top] },
       shapes: [{ type: "line", xref: "paper", x0: 0, x1: 1, y0: FTP, y1: FTP, line: { color: s2, width: 2, dash: "dash" } }],
       annotations: [{ xref: "paper", x: 1, xanchor: "left", y: FTP, text: ` FTP ${FTP} W`, showarrow: false, font: { color: ink, size: 12 } }] },
     { displayModeBar: false, responsive: true });
}
draw();
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
</script>
</body>
</html>
"""


def main():
    found = profiles()
    out = HERE / "logs" / "vorgaben.html"
    out.write_text(PAGE.replace("__FTP__", str(FTP)).replace("__DATA__", json.dumps(found, ensure_ascii=False).replace("</", "<\\/")), encoding="utf-8")
    for p in found:
        print(f"{p['name']:<22} {p['seconds'] / 60:5.1f} min  {p['power']:4.0f} W  {p['kj']:4.0f} kJ" + ("" if p["description"] else "  (no README line)"))
    print(out)


if __name__ == "__main__":
    main()
