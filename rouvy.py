#!/usr/bin/env python3
# /// script
# dependencies = ["fitparse"]
# ///
"""Report on one ride from a Rouvy activity file (.fit), as the same Plotly page kinomap.py writes.

Usage:
    uv run rouvy.py logs/vegas.fit --betablocker
    uv run rouvy.py logs/vegas.fit --route "Las Vegas" --betablocker --training --rr 130/80/90 --note "..."

Rouvy connects to the ergometer itself (not through ergometer.py) and exports one .fit file per
ride, one record a second. The file does not name the route, so --route does (default: the file
name). The analysis and the page are kinomap.py's; the page lands in logs/ as
rouvy-<yy-mm-dd-hh-mm-ss>.html, named after the local start, where kalender.py links it to its
session. With --training the ride is filed the way lauf.py files a ride (add_training.py, then the
measured columns), with the route in Strecke and the Kinomap column left empty.
"""

import argparse
import csv
import io
from datetime import timezone
import sys
from pathlib import Path

import fitparse

import kinomap
import lauf


def read_fit(path: Path, route: str) -> tuple[list[dict], dict]:
    """The records as kinomap.py's rows (speed in km/h, distance in m) and the facts of the session."""
    fit = fitparse.FitFile(str(path))
    session = next(fit.get_messages("session")).get_values()
    rows, start = [], None
    for message in fit.get_messages("record"):
        record = message.get_values()
        start = start or record["timestamp"]
        rows.append({"seconds": (record["timestamp"] - start).total_seconds(), "power": float(record.get("power") or 0),
                     "pulse": float(record.get("heart_rate") or 0), "cadence": float(record.get("cadence") or 0),
                     "speed": (record.get("speed") or 0) * 3.6, "distance": record.get("distance") or 0,
                     "altitude": record.get("altitude") or 0})
    # FIT times are UTC without a zone.
    local = session["start_time"].replace(tzinfo=timezone.utc).astimezone()
    return rows, {"start": local, "kcal": float(session["total_calories"]) if session.get("total_calories") else None, "route": route}


def join_rides(parts: list[tuple[list[dict], dict]]) -> tuple[list[dict], dict]:
    """Several files of one ride (an app restarted on the same route) as one: each part's time and distance go on from the
    end of the one before, without the gap between them; the calories add up, the start is the first part's."""
    rows = []
    for part, _ in parts:
        seconds, distance = (rows[-1]["seconds"] + 1, rows[-1]["distance"]) if rows else (0.0, 0.0)
        rows += [row | {"seconds": row["seconds"] + seconds, "distance": row["distance"] + distance} for row in part]
    kcal = [facts["kcal"] for _, facts in parts]
    return rows, parts[0][1] | {"kcal": None if None in kcal else sum(kcal)}


def assume_pulse(rows: list[dict], first: float, last: float) -> None:
    """A pulse rising evenly from first to last over the ride, for a file without one (the rider's own estimate)."""
    end = rows[-1]["seconds"] or 1
    for row in rows:
        row["pulse"] = first + (last - first) * row["seconds"] / end


PAUSE_SECONDS = 10  # a stretch without pedalling at least this long, inside the ride, is a pause that --drop-pauses cuts out


def drop_pauses(rows: list[dict]) -> tuple[list[dict], list[tuple[float, float]]]:
    """The rows without the pauses inside the ride, with time and distance after each pause moved up so the ride runs on
    without a gap; and the pauses as (start in the cut ride, length) in seconds. Rouvy lets the rider coast through a
    pause, so the distance covered then is dropped too."""
    still = [row["cadence"] == 0 and row["power"] == 0 for row in rows]
    first = next(i for i, s in enumerate(still) if not s)
    last = len(rows) - 1 - next(i for i, s in enumerate(reversed(still)) if not s)
    kept, pauses, shift, gone, index = [], [], 0.0, 0.0, 0
    while index < len(rows):
        end = index
        while end < len(rows) and still[end]:
            end += 1
        if first < index and end <= last and end > index and rows[end]["seconds"] - rows[index]["seconds"] >= PAUSE_SECONDS:
            pauses.append((rows[index]["seconds"] - shift, rows[end]["seconds"] - rows[index]["seconds"]))
            shift += rows[end]["seconds"] - rows[index]["seconds"]
            gone += rows[end]["distance"] - rows[index]["distance"]
            index = end
            continue
        kept.append(rows[index] | {"seconds": rows[index]["seconds"] - shift, "distance": rows[index]["distance"] - gone})
        index += 1
    return kept, pauses


def refile(row: dict, rr: str | None, note: str | None) -> dict | None:
    """Update the row this ride already has in the training log (same date, route and calories, e.g. before its pauses were
    cut out) with the new figures; returns it, or None if there is none. Nr, blood pressure and comment stay unless given."""
    log = kinomap.TRAINING / "training_log.csv"
    raw = log.read_bytes()
    ending = b"\r\n" if raw.count(b"\r\n") * 2 > raw.count(b"\n") else b"\n"
    lines = raw.splitlines()
    header = next(csv.reader([lines[0].decode("utf-8-sig")]))
    for index, line in enumerate(lines[1:], 1):
        logged = dict(zip(header, next(csv.reader([line.decode()]), [])))
        if all(logged.get(key) == row[key] for key in ("Datum", "Strecke", "Kcal")):
            logged.update({key: value for key, value in row.items() if value or key not in ("Temperatur_C",)})
            logged["RR_training"] = rr or logged.get("RR_training", "")
            logged["Kommentar"] = note if note is not None else logged.get("Kommentar", "")
            out = io.StringIO()
            csv.writer(out, lineterminator="").writerow([logged.get(name, "") for name in header])
            lines[index] = out.getvalue().encode()
            log.write_bytes(ending.join(lines + [b""]))
            lauf.regenerate_training_pdf()
            return logged
    return None


def main():
    parser = argparse.ArgumentParser(description="Report on a ride exported from Rouvy (.fit).")
    parser.add_argument("fit", type=Path, nargs="+", help="the activity file from Rouvy; several files of one ride are joined in the order given")
    parser.add_argument("--app", default="Rouvy", help="the app that recorded the ride, for the page and the Strecke column (default Rouvy)")
    parser.add_argument("--route", help="the route's name (default: the file name)")
    parser.add_argument("--ftp", type=float, default=kinomap.FTP, help=f"functional threshold power in watts (default {kinomap.FTP:g})")
    parser.add_argument("--betablocker", action="store_true", help="the rider takes a beta blocker: the pulse is not judged")
    parser.add_argument("--assessment", action="append", default=[], help="a paragraph of assessment for the page; repeat for more")
    parser.add_argument("--training", action="store_true", help="file the ride in ~/training/training_log.csv (not twice)")
    parser.add_argument("--rr", help="blood pressure after the ride, SYS/DIA/PULSE, for the training log")
    parser.add_argument("--temperature", type=float, help="room temperature in °C, for the training log")
    parser.add_argument("--max-hf", type=float, help="highest pulse from the rider's own watch, for the training log when the file has none")
    parser.add_argument("--assumed-pulse", help="FIRST/LAST: for a file without pulse, assume it rose evenly from FIRST to LAST over the ride")
    parser.add_argument("--note", help="comment for the training log (default: <app> <route>)")
    parser.add_argument("--drop-pauses", action="store_true", help=f"cut out stretches of at least {PAUSE_SECONDS} s without pedalling inside the ride")
    args = parser.parse_args()

    route = args.route or args.fit[0].stem.replace("_", " ").title()
    rows, facts = join_rides([read_fit(path, route) for path in args.fit])
    if args.assumed_pulse and not any(row["pulse"] for row in rows):
        assume_pulse(rows, *(float(value) for value in args.assumed_pulse.split("/")))
    if args.drop_pauses:
        rows, pauses = drop_pauses(rows)
        for at, seconds in pauses:
            print(f"pause cut out: {seconds:.0f} s at {at // 60:.0f}:{at % 60:02.0f}")
    result = kinomap.analyse(rows, facts, args.ftp, args.betablocker)
    start = facts["start"]
    if args.training:
        row = {"Datum": f"{start.day}.{start.month}", "Dauer_sek": f"{result['seconds']:.0f}", "Kcal": f"{result['kcal']:.0f}",
               "Watt": f"{result['power']:.0f}", "MaxHF": f"{result['pulse_max'] or args.max_hf or 0:.0f}" if result["pulse_max"] or args.max_hf else "",
               "Strecke": f"{args.app} {route}", "Distanz_km": f"{result['distance']:.2f}", "Kadenz_rpm": f"{result['cadence']:.0f}" if result["cadence"] else "",
               "Watt_Max": f"{result['power_max']:.0f}", "Hoehenmeter": f"{result['climb']:.0f}", "Max_kmh": f"{result['speed_max']:.1f}",
               "Temperatur_C": f"{args.temperature:.0f}" if args.temperature is not None else ""}
        if (logged := refile(row, args.rr, args.note)):
            print(f"training log: Nr {logged['Nr']} updated")
        else:
            logged, new = lauf.file_in_training(row, args.rr, args.note or f"{args.app} {route}")
            print(f"training log: Nr {logged['Nr']} {'added' if new else 'already there'}")
        result = kinomap.analyse(rows, facts, args.ftp, args.betablocker)
    result["assessment"] = args.assessment
    out = kinomap.write_page(result, start, args.ftp, " + ".join(path.name for path in args.fit), args.app)

    print(f"{route}  {result['seconds']:.0f} s  {result['power']:.0f} W (NP {result['normalized']:.0f})  {result['kj']:.0f} kJ  {result['kcal']:.0f} kcal  "
          f"max {result['power_max']:.0f} W  {result['cadence'] or 0:.0f} rpm  {result['distance']:.2f} km  {result['climb']:.0f} m  pulse max {result['pulse_max'] or 0:.0f}")
    print("best " + ", ".join(f"{item['seconds']} s {item['power']:.0f} W" for item in result["best"]))
    print(f"training log: Nr {result['entry']['Nr']}" if result["entry"] else "training log: no row for this ride")
    print(out)


if __name__ == "__main__":
    sys.exit(main())
