#!/usr/bin/env python3
"""Show every training of the training log as a calendar, coloured by intensity.

Usage:
    python3 kalender.py            # writes logs/index.html
    python3 kalender.py --ftp 210  # another reference for the power steps

Reads ~/training/training_log.csv (one row per session, dates without a year) and writes one page
with a month grid per month. A day's colour is its mean power as a share of the FTP; the page can
also colour by calories or duration. A session that was recorded by ergometer.py links to its
lauf-*.html and stufentest-*.html pages in logs/.
"""

import argparse
import csv
import json
import re
from datetime import date
from pathlib import Path

FTP = 200  # watts; the power steps are shares of it
TRAINING = Path.home() / "training"  # the training log project, as in lauf.py
PAGES = {"lauf": "Lauf", "kinomap": "Kinomap", "stufentest": "Stufentest"}  # generated pages in logs/ that a session links to
RIDES = ("lauf", "kinomap")  # those of them that embed the ride's figures (lauf.py, kinomap.py)
MATCH_SECONDS = 60  # a page and a session of the same day and calories are the same ride if their durations differ by no more


def number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def read_training(path: Path, today: date) -> list[dict]:
    """The sessions of the training log with full dates. The log only has day and month, in order, so the years are counted back from today."""
    with open(path, encoding="utf-8-sig", newline="") as file:
        rows = [row for row in csv.DictReader(file) if re.fullmatch(r"\d+\.\d+", row["Datum"] or "")]
    year, later = today.year, None
    sessions = []
    for row in reversed(rows):
        day, month = (int(part) for part in row["Datum"].split("."))
        if later is None and (month, day) > (today.month, today.day):
            year -= 1
        elif later is not None and month > later:
            year -= 1
        later = month
        seconds, watt = number(row["Dauer_sek"]), number(row["Watt"])
        if not seconds or watt is None:
            continue  # a rest day that only carries a blood pressure reading
        sessions.append({"date": date(year, month, day).isoformat(), "nr": row["Nr"], "seconds": seconds, "watt": watt, "kcal": number(row["Kcal"]),
                         "pulse": number(row["MaxHF"]), "watt_max": number(row["Watt_Max"]), "km": number(row["Distanz_km"]),
                         "route": row["Strecke"], "note": row["Kommentar"], "rr": row["RR_training"], "pages": []})
    return sessions[::-1]


def pages(logs: Path) -> list[dict]:
    """The ride pages in logs/, with the figures of the ride a lauf or kinomap page reports on."""
    found = []
    for path in sorted(logs.glob("*.html")):
        match = re.fullmatch(r"(\w+)-((\d\d)-(\d\d)-(\d\d)-(\d\d)-(\d\d)-\d\d)\.html", path.name)
        if not match or match[1] not in PAGES:
            continue
        page = {"file": path.name, "label": f"{PAGES[match[1]]} {match[6]}:{match[7]}", "date": f"20{match[3]}-{match[4]}-{match[5]}", "start": match[2]}
        data = re.search(r"^const D = (\{.*\});$", path.read_text(encoding="utf-8"), re.MULTILINE) if match[1] in RIDES else None
        if data:
            ride = json.loads(data[1])
            page |= {"seconds": ride["seconds"], "kcal": round(ride["kcal"])}
        found.append(page)
    return found


def link_pages(sessions: list[dict], found: list[dict]) -> dict[str, list[dict]]:
    """Give each session the pages of its ride; returns the pages that fit no session, by date.

    A lauf or kinomap page belongs to the session of its date with the same calories and about the same duration (the training log
    rounds some durations to minutes). A stufentest page belongs to the session of the lauf page made from the same log.
    """
    owner, rest = {}, {}
    for page in found:
        fits = [session for session in sessions if "kcal" in page and session["date"] == page["date"] and session["kcal"] == page["kcal"]
                and abs(session["seconds"] - page["seconds"]) <= MATCH_SECONDS]
        if fits:
            owner[page["start"]] = fits[0]
    for page in found:
        link = {"file": page["file"], "label": page["label"]}
        if page["start"] in owner:
            owner[page["start"]]["pages"].append(link)
        else:
            rest.setdefault(page["date"], []).append(link)
    return rest


PAGE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trainingskalender</title>
<style>
  :root { color-scheme: light dark; --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781; --grid: #e1e0d9; --axis: #c3c2b7;
          --step-1: #2e9f4d; --step-2: #d9b300; --step-3: #f08a24; --step-4: #d6362f; --step-5: #7b3fa0;
          --on-1: #0b0b0b; --on-2: #0b0b0b; --on-3: #0b0b0b; --on-4: #ffffff; --on-5: #ffffff; }
  @media (prefers-color-scheme: dark) { :root { --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --grid: #2c2c2a; --axis: #383835;
          --step-1: #3fb760; --step-2: #f0d03c; --step-3: #f59a3a; --step-4: #e5484d; --step-5: #9a5fd0;
          --on-1: #0b0b0b; --on-2: #0b0b0b; --on-3: #0b0b0b; --on-4: #0b0b0b; --on-5: #0b0b0b; } }
  body { margin: 0; padding: 20px 16px 40px; background: var(--page); color: var(--ink); font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  main { max-width: 1040px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0; }
  h2 { font-size: 16px; margin: 0 0 2px; }
  .sub { color: var(--ink-2); margin: 2px 0 16px; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 10px; }
  .card { background: var(--surface); border: 1px solid var(--grid); border-radius: 8px; padding: 12px 14px; margin-bottom: 10px; min-width: 0; }
  .tiles .card { margin: 0; }
  .label, .note { color: var(--ink-2); font-size: 13px; }
  .value { font-size: 24px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .value small { font-size: 14px; font-weight: 400; color: var(--ink-2); }
  .bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 20px; margin-bottom: 10px; }
  .switch { display: flex; border: 1px solid var(--axis); border-radius: 8px; overflow: hidden; }
  .switch button { font: inherit; font-size: 13px; padding: 5px 12px; border: 0; background: var(--surface); color: var(--ink-2); cursor: pointer; }
  .switch button + button { border-left: 1px solid var(--axis); }
  .switch button[aria-pressed="true"] { background: var(--ink); color: var(--page); }
  .legend { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 13px; color: var(--ink-2); }
  .legend span { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
  .legend i { width: 14px; height: 14px; border-radius: 3px; }
  .months { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 10px; margin-bottom: 10px; }
  .months .card { margin: 0; }
  .month { display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; margin-top: 8px; }
  .month b { font-size: 12px; font-weight: 600; color: var(--muted); text-align: center; }
  .day { aspect-ratio: 1; border-radius: 4px; display: flex; align-items: center; justify-content: center; position: relative;
         font: inherit; font-size: 13px; font-variant-numeric: tabular-nums; color: var(--muted); border: 0; padding: 0; background: none; }
  .day.rest { box-shadow: inset 0 0 0 1px var(--grid); }
  button.day { cursor: pointer; font-weight: 600; }
  button.day:hover, button.day:focus-visible { outline: 2px solid var(--ink); outline-offset: 1px; }
  button.day[aria-pressed="true"] { outline: 2px solid var(--ink); outline-offset: 1px; }
  .day sup { position: absolute; top: 1px; right: 3px; font-size: 9px; font-weight: 400; }
  #tip { position: fixed; pointer-events: none; background: var(--surface); color: var(--ink); border: 1px solid var(--axis); border-radius: 6px;
         padding: 6px 9px; font-size: 13px; line-height: 1.4; max-width: 260px; display: none; z-index: 2; }
  #tip div { color: var(--ink-2); }
  .scroll { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; margin-top: 4px; }
  th, td { text-align: right; padding: 5px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; vertical-align: top; }
  th { color: var(--ink-2); font-weight: 600; font-size: 13px; }
  td.text, th.text { text-align: left; white-space: normal; min-width: 180px; }
  tr:last-child td { border-bottom: 0; }
  a { color: inherit; }
  #links { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 6px 16px; }
  .back { font-size: 13px; color: var(--ink-2); }
</style>
</head>
<body>
<main>
<nav class="back"><a href="../../training/training.html">← Trainingsübersicht</a></nav>
<h1>Trainingskalender</h1>
<p class="sub" id="sub"></p>
<section class="tiles" id="tiles"></section>
<div class="bar">
  <div class="switch" id="switch" role="group" aria-label="Farbe nach"></div>
  <div class="legend" id="legend"></div>
</div>
<section class="months" id="months"></section>
<section class="card" id="detail"><h2 id="detail-title"></h2><div class="note" id="detail-note"></div><div class="scroll"><table id="sessions"></table></div><div id="links"></div></section>
<p class="note">Quelle: training_log.csv, Stand __STAND__. Leistung: mittlere Leistung des Tages, nach Dauer gewichtet, als Anteil der FTP von __FTP__ W. Ein Tag mit mehreren Einheiten zählt bei Kalorien und Dauer als Summe.</p>
</main>
<div id="tip"></div>
<script>
const D = __DATA__;
const FTP = __FTP__;
const n = (value, digits = 0) => value === null || value === undefined ? "–" : value.toLocaleString("de-DE", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const clock = (seconds) => `${Math.floor(Math.round(seconds) / 60)}:${String(Math.round(seconds) % 60).padStart(2, "0")}`;
const hours = (seconds) => `${Math.floor(seconds / 3600)}:${String(Math.round(seconds % 3600 / 60)).padStart(2, "0")}`;
const el = (tag, text, cls) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (cls) node.className = cls; return node; };
const longDate = (iso) => new Date(iso + "T12:00").toLocaleDateString("de-DE", { weekday: "long", day: "numeric", month: "long", year: "numeric" });

// What a day's colour can stand for: the value of a day and the lower edges of steps 2 to 5.
const METRICS = {
  watt: { name: "Leistung", unit: "W", value: (day) => day.watt, edges: [0.7, 0.8, 0.9, 1].map((share) => share * FTP),
          label: (low, high) => `${n(low / FTP * 100)}–${n(high / FTP * 100)} %`, first: (high) => `unter ${n(high / FTP * 100)} % FTP`, last: (low) => `ab ${n(low / FTP * 100)} %` },
  kcal: { name: "Kalorien", unit: "kcal", value: (day) => day.kcal, edges: [200, 300, 400, 500],
          label: (low, high) => `${n(low)}–${n(high)}`, first: (high) => `unter ${n(high)} kcal`, last: (low) => `ab ${n(low)}` },
  seconds: { name: "Dauer", unit: "min", value: (day) => day.seconds, edges: [20, 30, 40, 60].map((minutes) => minutes * 60),
             label: (low, high) => `${n(low / 60)}–${n(high / 60)}`, first: (high) => `unter ${n(high / 60)} min`, last: (low) => `ab ${n(low / 60)}` },
};
let metric = "watt";
let chosen = null;

// One entry per day: the sessions of that day and their sums.
const DAYS = new Map();
for (const session of D.sessions) {
  const day = DAYS.get(session.date) || { date: session.date, sessions: [], seconds: 0, kcal: 0, work: 0 };
  day.sessions.push(session);
  day.seconds += session.seconds;
  day.kcal += session.kcal || 0;
  day.work += session.watt * session.seconds;
  day.watt = day.work / day.seconds;
  DAYS.set(session.date, day);
}
const step = (day) => 1 + METRICS[metric].edges.filter((edge) => METRICS[metric].value(day) >= edge).length;

function tiles() {
  const days = [...DAYS.values()];
  const seconds = days.reduce((sum, day) => sum + day.seconds, 0);
  const span = (new Date(D.sessions.at(-1).date) - new Date(D.sessions[0].date)) / 864e5 + 1;
  const items = [["Einheiten", n(D.sessions.length), ""], ["Trainingstage", n(days.length), ` von ${n(span)}`], ["Zeit", hours(seconds), " h"],
                 ["Kalorien", n(days.reduce((sum, day) => sum + day.kcal, 0)), " kcal"], ["Mittlere Leistung", n(days.reduce((sum, day) => sum + day.work, 0) / seconds), " W"]];
  for (const [label, value, unit] of items) {
    const card = el("div", undefined, "card");
    const figure = el("div", value, "value");
    figure.append(el("small", unit));
    card.append(el("div", label, "label"), figure);
    document.getElementById("tiles").append(card);
  }
  document.getElementById("sub").textContent = `${longDate(D.sessions[0].date)} bis ${longDate(D.sessions.at(-1).date)}`;
}

function legend() {
  const { edges, label, first, last } = METRICS[metric];
  const box = document.getElementById("legend");
  box.replaceChildren();
  for (let index = 1; index <= 5; index++) {
    const item = el("span");
    const swatch = el("i");
    swatch.style.background = `var(--step-${index})`;
    item.append(swatch, index === 1 ? first(edges[0]) : index === 5 ? last(edges[3]) : label(edges[index - 2], edges[index - 1]));
    box.append(item);
  }
}

function summary(day) {
  return `${n(day.watt)} W (${n(day.watt / FTP * 100)} % FTP) · ${clock(day.seconds)} min · ${n(day.kcal)} kcal`;
}

function months() {
  const box = document.getElementById("months");
  box.replaceChildren();
  const first = new Date(D.sessions[0].date + "T12:00"), last = new Date(D.sessions.at(-1).date + "T12:00");
  for (let month = new Date(first.getFullYear(), first.getMonth(), 1, 12); month <= last; month.setMonth(month.getMonth() + 1)) {
    const card = el("div", undefined, "card");
    const grid = el("div", undefined, "month");
    for (const weekday of ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]) grid.append(el("b", weekday));
    for (let blank = (month.getDay() + 6) % 7; blank > 0; blank--) grid.append(el("span"));
    const length = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate();
    let count = 0, seconds = 0;
    for (let date = 1; date <= length; date++) {
      const iso = `${month.getFullYear()}-${String(month.getMonth() + 1).padStart(2, "0")}-${String(date).padStart(2, "0")}`;
      const day = DAYS.get(iso);
      if (!day) { grid.append(el("span", date, "day rest")); continue; }
      count += day.sessions.length;
      seconds += day.seconds;
      const cell = el("button", date, "day");
      const index = step(day);
      cell.style.background = `var(--step-${index})`;
      cell.style.color = `var(--on-${index})`;
      cell.setAttribute("aria-label", `${longDate(iso)}: ${summary(day)}`);
      cell.setAttribute("aria-pressed", String(iso === chosen));
      if (day.sessions.length > 1) cell.append(el("sup", day.sessions.length));
      cell.addEventListener("click", () => { chosen = iso; months(); detail(); });
      cell.addEventListener("pointermove", (event) => tip(event, day));
      cell.addEventListener("pointerleave", () => { document.getElementById("tip").style.display = "none"; });
      grid.append(cell);
    }
    card.append(el("h2", month.toLocaleDateString("de-DE", { month: "long", year: "numeric" })), el("div", `${n(count)} Einheiten · ${hours(seconds)} h`, "note"), grid);
    box.append(card);
  }
}

function tip(event, day) {
  const box = document.getElementById("tip");
  box.replaceChildren(el("b", longDate(day.date)), el("div", summary(day)));
  if (day.sessions.length > 1) box.append(el("div", `${day.sessions.length} Einheiten`));
  if (day.sessions.some((session) => session.pages.length)) box.append(el("div", "mit Auswertung"));
  const text = day.sessions.map((session) => session.route || session.note).filter(Boolean).join(" · ");
  if (text) box.append(el("div", text.length > 110 ? text.slice(0, 110) + " …" : text));
  box.style.display = "block";
  box.style.left = Math.min(event.clientX + 14, innerWidth - box.offsetWidth - 8) + "px";
  box.style.top = (event.clientY + 18 + box.offsetHeight > innerHeight ? event.clientY - box.offsetHeight - 10 : event.clientY + 18) + "px";
}

const link = (page) => { const node = el("a", page.label); node.href = page.file; return node; };

function detail() {
  const day = DAYS.get(chosen);
  document.getElementById("detail-title").textContent = longDate(day.date);
  document.getElementById("detail-note").textContent = summary(day);
  const table = document.getElementById("sessions");
  table.replaceChildren();
  const head = el("tr");
  for (const [title, text] of [["Nr", 0], ["Dauer", 0], ["Ø Watt", 0], ["% FTP", 0], ["Max W", 0], ["kcal", 0], ["Max HF", 0], ["km", 0], ["RR danach", 0], ["Auswertung", 0], ["Strecke / Kommentar", 1]]) head.append(el("th", title, text ? "text" : ""));
  table.append(head);
  for (const session of day.sessions) {
    const row = el("tr");
    for (const value of [session.nr, clock(session.seconds), n(session.watt), n(session.watt / FTP * 100), n(session.watt_max), n(session.kcal), n(session.pulse), n(session.km, 1), session.rr || "–"]) row.append(el("td", value));
    const pages = el("td", session.pages.length ? undefined : "–");
    session.pages.forEach((page, index) => pages.append(index ? " · " : "", link(page)));
    row.append(pages);
    row.append(el("td", [session.route, session.note].filter(Boolean).join(" – ") || "–", "text"));
    table.append(row);
  }
  const links = document.getElementById("links");
  links.replaceChildren();
  for (const page of D.pages[day.date] || []) links.append(link(page));
}

for (const [key, { name }] of Object.entries(METRICS)) {
  const button = el("button", name);
  button.setAttribute("aria-pressed", String(key === metric));
  button.addEventListener("click", () => {
    metric = key;
    for (const other of document.querySelectorAll("#switch button")) other.setAttribute("aria-pressed", String(other === button));
    legend();
    months();
  });
  document.getElementById("switch").append(button);
}
chosen = D.sessions.at(-1).date;
tiles();
legend();
months();
detail();
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="Show every training of the training log as a calendar, coloured by intensity.")
    parser.add_argument("--ftp", type=float, default=FTP, help=f"functional threshold power in watts (default {FTP})")
    args = parser.parse_args()

    logs = Path(__file__).with_name("logs")
    today = date.today()
    sessions = read_training(TRAINING / "training_log.csv", today)
    data = {"sessions": sessions, "pages": link_pages(sessions, pages(logs))}
    out = logs / "index.html"
    out.write_text(PAGE.replace("__FTP__", f"{args.ftp:g}").replace("__STAND__", today.strftime("%d.%m.%Y"))
                   .replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")), encoding="utf-8")

    print(f"{len(sessions)} sessions on {len({session['date'] for session in sessions})} days, {sessions[0]['date']} to {sessions[-1]['date']}")
    print(out)


if __name__ == "__main__":
    main()
