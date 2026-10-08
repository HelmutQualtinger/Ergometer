---
name: lauf
description: >
  Report on a ride recorded by the ergometer program and file it in the training log: an
  HTML page with charts (power against target, pulse, cadence, mean power per section),
  worked-out remarks and the training-log entry, plus the row in ~/training/training_log.csv.
  Use this whenever the user wants the last ride (or named logs) visualised, summarised,
  judged or stored - "visualisiere den letzten Lauf", "lege in Training ab", "trag den Lauf
  ein", "Zusammenfassung des Laufs", "wie war die Fahrt", "mach einen Eintrag aus dem Log" -
  also when they only give a blood pressure reading right after riding. Not for step tests
  that should be rated against reference data; that is the stufentest skill.
---

# Lauf

`lauf.py` in the repo root does all of it deterministically: it reads the log(s), builds the
page and, with `--training`, writes the training-log row. Your part is to pick the right logs,
pass what the logs cannot know, look at the output, and tell the user what the ride was like.
Do not edit the generated HTML or patch `training_log.csv` by hand; if something is wrong,
fix the script.

## Which logs

Without arguments the script takes the newest `logs/log-*.csv`. Look before you run it:

```bash
ls -lt logs/log-*.csv | head -5
```

A ride can be split over several logs when the recording was stopped and restarted (a slip
on the phone, a changed profile). Logs that start within a few minutes of the previous one
ending and continue the same kind of session belong together; pass them in order and they
become one ride. A 30-second log with no pedalling is a trial run, not a ride - leave it out.
If it is unclear whether two logs are one ride, ask.

## What to pass

- `--training`: file the ride in `~/training/training_log.csv`. Pass it when the user asks to
  store, log or "ablegen"; leave it out when they only want to see the ride. The script will
  not add a ride twice - it recognises one that is already there and says so.
- `--note "..."`: the comment in the training log. Say what the session was, in the user's
  style: the profile and its structure, e.g. `HIIT 12x(30:30) 230W/80W, Vorgabe hiit-20, ohne Kinomap`.
  The targets in the log (printed section counts, the page's section table) tell you which
  profile it was; keep "ohne Kinomap" so these rides can be told apart from Kinomap ones.
- `--rr SYS/DIA/PULS`: blood pressure after the ride, only if the user gave one. Never invent it.
- `--betablocker`: pass it for a rider on a beta blocker (see memory); the page then does not
  read anything into the pulse.
- `--temperature X`: only for logs without a `room temperature` column (recorded before the
  program logged it). If the ride ended minutes ago, read the sensor (field `roomtemp`) and
  subtract the 2 degrees the program also subtracts (`MQTT_OFFSET`); otherwise leave it out
  rather than log a temperature from another time. The broker's address is in the untracked
  `.env` and must stay out of anything that is committed:

```bash
set -a; . ./.env; set +a; mosquitto_sub -h "$MQTT_HOST" -p "$MQTT_PORT" -t "$MQTT_TOPIC" -C 1 -W 15
```

## Run

```bash
python3 lauf.py --training --betablocker --note "HIIT 12x(30:30) 230W/80W, Vorgabe hiit-20, ohne Kinomap"
python3 lauf.py logs/log-26-10-08-12-14-59.csv logs/log-26-10-08-12-28-19.csv --rr 126/83/87 --training --note "..."
open logs/lauf-<timestamp>.html
```

The page is written next to the first log and needs no server. With `--training` the script
also regenerates `~/training/training.pdf` (about 10 seconds).

## Report

Answer in the user's language and lead with what was done: page opened, entry number (or that
it was already logged). Then the ride in a few lines - duration, mean power, work and kcal,
the hard sections against their target, anything the remarks on the page flag (late or
overshooting sections, a peak that is only a spike, a pulse sensor that dropped out).

Two things the numbers do not show, so do not claim them: whether the user stopped pedalling
in a gap between logs, and how hard the ride felt. The user's goal is to hold about 200 W
with a sustainable amount of training (see memory) - describe the ride, do not push for more.

The log entry is the user's record. Mention what is missing from it (no blood pressure given,
no temperature) instead of filling gaps with guesses. Nothing is committed in either repo
unless the user asks.
