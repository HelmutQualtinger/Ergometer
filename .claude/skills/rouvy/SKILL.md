---
name: rouvy
description: Report on a Rouvy ride from its activity file (a .fit, usually dropped into logs/ and named after the route, e.g. logs/vegas.fit) and file it in the training log - the same HTML page as for Kinomap rides (power against FTP, best mean power, intensity steps, pulse, cadence, speed, altitude, 5-minute blocks), plus the row in ~/training/training_log.csv, the refreshed calendar and a push of both repos. Use this whenever the user names a Rouvy route or file ("analysiere das vegas log", "werte die Rouvy-Fahrt aus", "Rouvy run von gerade") or gives a blood pressure together with such a ride. Not for Kinomap exports (zip; that is the kinomap skill) or rides recorded by the ergometer program (logs/log-*.csv; that is the lauf skill).
---

# Rouvy

`rouvy.py` in the repo root reads the `.fit` file and reuses `kinomap.py`'s analysis and page, so
the result looks exactly like a Kinomap report (`logs/rouvy-<local start>.html`). With `--training`
it files the row through `lauf.py`'s `file_in_training()` (never twice; a later `--rr` or `--note`
updates the row). Your part: pick the file, pass what it cannot know, write the assessment, and
tell the user what the ride was like. Do not edit the generated HTML or patch `training_log.csv`
by hand; if something is wrong, fix the script.

It needs `fitparse`, declared inline, so always run it with `uv run rouvy.py`, not `python3`.

## Which file

Rouvy exports one `.fit` per ride; the user drops it into `logs/` and names it after the route.
If unclear, take the newest:

```bash
ls -lt logs/*.fit | head -5
```

A zip with only a `.jpg` in it is Rouvy's share picture (FTP estimate, "all-time" medals), not
data: ask for the `.fit` export. Rouvy's own FTP estimate after a short ride is not a test; the
rider's FTP stays 200 W (see memory).

## What to pass

- `--route NAME`: the route's name, which the file does not contain ("vegas" → `--route Vegas`).
  It goes into the page and, as `Rouvy <route>`, into the Strecke column.
- `--betablocker`: always for this rider (see memory).
- `--training`: file the ride.
- `--note "..."`: the comment. Default is `Rouvy <route>`; better say what the ride was in the
  user's style (duration, pauses, which session of the day it was). How it felt only in the user's
  own words, never a Borg number.
- `--rr SYS/DIA/PULS`: only if the user gave one.
- `--temperature X`: only if the ride ended within the last half hour (read the sensor as in the
  kinomap skill, minus 2 degrees); otherwise leave it out.
- `--assessment "..."`: German paragraphs for the "Bewertung" card, repeatable.

## Detailed analysis

Run once without `--training`, then read the page's data:

```bash
uv run rouvy.py logs/vegas.fit --route Vegas --betablocker
python3 - <<'EOF'
import re, json
d = json.loads(re.search(r'const D = (\{.*?\});\n', open('logs/rouvy-<start>.html').read(), re.S).group(1))
print({k: v for k, v in d.items() if k not in ('series', 'curve')})
EOF
```

Look at the blocks, halves, best 1/5/10/20 min and the series for stretches with power and
cadence at 0 (a pause: say so, it pulls the block and the mean down). Rouvy talks to the AX 4000
directly; the bike reports its table power from level and cadence, and the power has so far
followed the cadence rather than the terrain - mention it when the power does not match the
gradient. Compare with the last ride on the same route (`grep "Rouvy <route>"
~/training/training_log.csv`, then its page), and place the ride in the day (other sessions on
the same date) and in the week's plan (`~/training/trainingsplan_*.md`). Under the beta blocker,
describe the pulse but do not read fitness into it. The goal is to hold about 200 W sustainably -
describe the ride, do not push for more.

## Run, calendar, push

```bash
uv run rouvy.py logs/vegas.fit --route Vegas --betablocker --training --note "..." \
  --assessment "..." --assessment "..."
python3 kalender.py
open logs/rouvy-<start>.html
```

When the user asks to push, commit only the page, the calendar and the training files, never the
`.fit`, zips, `log-*.csv` recordings or unrelated changes:

```bash
git add logs/rouvy-<start>.html logs/index.html logs/pages.json
git commit -m "Add the Rouvy ride of <d Month> and refresh the training calendar" && git push
cd ~/training && git add training_log.csv training.pdf
git commit -m "Add Rouvy ride of <d Month> (Nr <n>)" && git push
```

## Report

Answer in the user's language: entry number, page opened, pushed or not. Then the ride in a few
lines (route, duration, mean and normalized power, kcal, best efforts, blocks, pauses, pulse) and
what is missing from the row (blood pressure, temperature, how it felt).
