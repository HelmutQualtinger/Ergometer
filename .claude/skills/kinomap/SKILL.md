---
name: kinomap
description: Report on a Kinomap ride from its activity export (a zip with .csv, .tcx, .gpx, .pwx, usually dropped into logs/ and named after the route, e.g. logs/köln.zip) and file it in the training log - an HTML page with power against FTP, best mean power, intensity steps, pulse, cadence, speed and altitude, plus the row in ~/training/training_log.csv, the refreshed calendar and a push of both repos. Use this whenever the user names a Kinomap route or export ("analysiere die Fahrt Köln", "analyse the last run köln", "werte den Kinomap-Export aus", "Bodensee eintragen") or gives a blood pressure together with such a ride. Not for rides recorded by the ergometer program (logs/log-*.csv); that is the lauf skill.
---

# Kinomap

`kinomap.py` in the repo root does all of it deterministically: it reads the export, builds the
page and, with `--training`, writes Kinomap's summary screen and hands it to the training
project's `add_training.py --paste`, which appends the row (Kinomap = 1, record watt, Nr) and
regenerates `training.pdf`. Your part: pick the export, pass what it cannot know, write the
assessment, and tell the user what the ride was like. Do not edit the generated HTML or patch
`training_log.csv` by hand; if something is wrong, fix the script.

## Which export

The user names it by route ("köln" → `logs/köln.zip`). If unclear, take the newest zip:

```bash
ls -lt logs/*.zip ~/training/*.zip | head -5
unzip -l "logs/köln.zip"   # the file dates are the day of the export
```

## What to pass

- `--betablocker`: always for this rider (see memory); the pulse is then not judged.
- `--training`: file the ride. The script does not file it twice: a row with the same date,
  calories and duration is recognised and left alone.
- `--rr SYS/DIA/PULS`: the blood pressure after the ride, only if the user gave one ("bldr 138/81/93").
- `--temperature X`: if the ride ended within the last half hour, read the sensor and subtract
  the 2 degrees the program also subtracts (`MQTT_OFFSET`); otherwise leave it out. The broker's
  address stays in the untracked `.env`:

  ```bash
  set -a; . ./.env; set +a; mosquitto_sub -h "$MQTT_HOST" -p "$MQTT_PORT" -t "$MQTT_TOPIC" -C 1 -W 15
  ```

- `--note "..."`: the comment column. For Kinomap rides it holds how the ride felt in the user's
  words ("muskulär am Limit"); leave it out unless they said so, and ask afterwards in plain
  words (never a Borg number). A note given later goes in with the training project's tools, not here.
- `--assessment "..."`: German paragraphs for the "Bewertung" card, repeatable (see below).

## Detailed analysis

Run once without `--training` to get the figures, then read the page's data:

```bash
python3 kinomap.py "logs/köln.zip" --betablocker
python3 - <<'EOF'
import re, json
d = json.loads(re.search(r'const D = (\{.*?\});\n', open('logs/kinomap-<start>.html').read(), re.S).group(1))
print({k: v for k, v in d.items() if k not in ('series', 'curve')})
EOF
```

Compare with the last ride on the same route (`grep "<route>" ~/training/training_log.csv`, then
its `logs/kinomap-*.html` the same way): the 5-minute blocks (power, pulse, cadence, climb),
best 60 s / 5 min / 20 min, halves, room temperature and blood pressure. Say whether the route
was ridden in full (distance against the full ride's). Place it in the recent days of the
training log. Under the beta blocker, describe the pulse but do not read fitness into it. The
user's goal is to hold about 200 W sustainably - describe the ride, do not push for more.

## Run, calendar, push

```bash
python3 kinomap.py "logs/köln.zip" --betablocker --training --rr 138/81/93 --temperature 24 \
  --assessment "..." --assessment "..."
python3 kalender.py
open logs/kinomap-<start>.html
```

Commit only the page, the calendar and the training files, never the zip, the `log-*.csv`
recordings or unrelated working-tree changes:

```bash
git add logs/kinomap-<start>.html logs/index.html logs/pages.json
git commit -m "Add the Kinomap ride of <d Month> and refresh the training calendar" && git push
cd ~/training && git add training_log.csv training.pdf
git commit -m "Add Kinomap ride of <d Month> (Nr <n>)" && git push
```

## Report

Answer in the user's language: entry number, page opened, pushed. Then the ride in a few lines
(route and how much of it, duration, mean and normalized power, kcal, best efforts, blocks
against the last ride on the route, pulse, blood pressure, temperature). Mention what is
missing from the row (no comment, no temperature) instead of guessing it.
