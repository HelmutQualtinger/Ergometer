#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["bleak"]
# ///
"""Connect to a Christopeit AX 4000 ergometer over Bluetooth LE and print its live values.

Usage:
    uv run ergometer.py                 # find the ergometer automatically
    uv run ergometer.py --scan          # just list nearby BLE devices
    uv run ergometer.py --panel         # also show a live Plotly panel in the browser
    uv run ergometer.py --power 150     # hold 150 W by adjusting the resistance (PID)
    uv run ergometer.py --panel --profile vorgabe-hit.csv   # follow a power profile from a file
    uv run ergometer.py --raw           # also dump the vendor-specific raw data
    uv run ergometer.py --name FS-1837  # match on a different name fragment
    uv run ergometer.py --address <id>  # connect to a specific device
"""

import argparse
import asyncio
import csv
import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bleak import BleakClient, BleakScanner

FTMS_SERVICE = "00001826-0000-1000-8000-00805f9b34fb"
INDOOR_BIKE_DATA = "00002ad2-0000-1000-8000-00805f9b34fb"
HEART_RATE = "00002a37-0000-1000-8000-00805f9b34fb"
CONTROL_POINT = "00002ad9-0000-1000-8000-00805f9b34fb"
RESISTANCE_RANGE = "00002ad6-0000-1000-8000-00805f9b34fb"
POWER_RANGE = "00002ad8-0000-1000-8000-00805f9b34fb"

# PID gains for the power controller: output is the resistance level, error is in watts.
# One level is worth roughly 15-20 W at 70 rpm. The gains are deliberately gentle,
# because a level change takes a second or two to show up in the measured power.
PID_KP = 0.01    # levels per W
PID_KI = 0.008   # levels per W*s
PID_KD = 0.005   # levels per W/s
PID_DEADBAND = 0.6  # only move when the output is this far from the current level
PID_TOLERANCE = 8   # W; smaller errors count as zero, so it doesn't hunt between two levels
WATTS_PER_LEVEL = 17  # rough effect of one level; used to jump ahead when the target changes
MIN_CADENCE = 20    # rpm; below this the rider has stopped and the controller holds

# The AX 4000 advertises under its FitShow module name, e.g. "FS-1837D8".
NAME_HINTS = ("fs-", "ax4000", "ax 4000", "christopeit")

# Indoor Bike Data fields in transmission order: (flag bit, label, struct format, scale, unit).
# Bit 0 is inverted: instantaneous speed is present when the bit is NOT set.
BIKE_FIELDS = [
    (0, "speed", "<H", 0.01, "km/h"),
    (1, "avg speed", "<H", 0.01, "km/h"),
    (2, "cadence", "<H", 0.5, "rpm"),
    (3, "avg cadence", "<H", 0.5, "rpm"),
    (4, "distance", "<I", 1, "m"),  # uint24
    (5, "resistance", "<h", 1, ""),
    (6, "power", "<h", 1, "W"),
    (7, "avg power", "<h", 1, "W"),
    (8, "energy", "<H", 1, "kcal"),
    (8, "energy/h", "<H", 1, "kcal/h"),
    (8, "energy/min", "<B", 1, "kcal/min"),
    (9, "heart rate", "<B", 1, "bpm"),
    (10, "MET", "<B", 0.1, ""),
    (11, "elapsed", "<H", 1, "s"),
    (12, "remaining", "<H", 1, "s"),
]


def parse_indoor_bike_data(data: bytes) -> dict[str, tuple[float, str]]:
    flags = struct.unpack_from("<H", data)[0]
    offset = 2
    values = {}
    for bit, label, fmt, scale, unit in BIKE_FIELDS:
        present = not flags & 1 if bit == 0 else flags & (1 << bit)
        if not present:
            continue
        size = 3 if label == "distance" else struct.calcsize(fmt)
        if offset + size > len(data):
            break
        raw = struct.unpack(fmt, data[offset:offset + size].ljust(struct.calcsize(fmt), b"\0"))[0]
        offset += size
        values[label] = (raw * scale, unit)
    return values


def parse_heart_rate(data: bytes) -> dict[str, tuple[float, str]]:
    bpm = struct.unpack_from("<H", data, 1)[0] if data[0] & 1 else data[1]
    return {"heart rate": (bpm, "bpm")}


def format_value(value: float, unit: str) -> str:
    number = f"{value:.1f}" if isinstance(value, float) else str(value)
    return f"{number} {unit}".strip()


PARSERS = {INDOOR_BIKE_DATA: parse_indoor_bike_data, HEART_RATE: parse_heart_rate}


# Shared with the browser panel: every Indoor Bike Data sample and the connection state.
SAMPLES: list[dict] = []
STATUS = {"text": "starting"}
PANEL_HTML = Path(__file__).with_name("panel.html")
# Set once connected, so the panel's HTTP thread can send commands to the ergometer.
CONTROL = {"loop": None, "client": None, "range": None, "power_range": [0, 400, 5],
           "target_power": 0, "write_offset": 0,
           # Power profile from a file: {"name", "steps": [[seconds, watts], ...], "start": epoch ms or None}
           "profile": None,
           # Epoch ms of the "Start" press while a recording runs; see save_log().
           "log_start": None}
LOG_DIR = PANEL_HTML.parent / "logs"


def parse_profile(text: str) -> list[list[float]]:
    """Read "mm:ss,watt" rows; each row sets the target power from that time on."""
    steps = []
    for line in text.splitlines():
        fields = [field.strip() for field in re.split(r"[,;\t]", line) if field.strip()]
        try:
            seconds = 0
            for part in fields[0].replace("::", ":").split(":"):
                seconds = seconds * 60 + int(part)
            steps.append([seconds, float(fields[1])])
        except (ValueError, IndexError):
            continue  # header, blank or comment line
    if not steps:
        raise ValueError("no rows of the form mm:ss,watt found")
    return sorted(steps)


def load_profile(name: str, text: str):
    save_log()  # a running profile is replaced
    CONTROL["profile"] = {"name": name, "steps": parse_profile(text), "start": None}


def start_profile():
    """Start the loaded profile and, with it, the recording of the measured values."""
    save_log()
    CONTROL["profile"]["start"] = CONTROL["log_start"] = time.time() * 1000


def stop_profile():
    if CONTROL["profile"]:
        CONTROL["profile"]["start"] = None
    save_log()


def save_log():
    """End the recording and write its samples to logs/log-<yy-mm-dd-hh-mm-ss>.csv, named after the start.

    Samples only arrive while the ergometer is connected, so the file holds exactly
    what was measured between "Start" and "Stop" (or the end of the profile).
    """
    start, CONTROL["log_start"] = CONTROL["log_start"], None
    rows = [sample for sample in SAMPLES if start and sample["t"] >= start]
    if not rows:
        return
    labels = [label for _, label, *_ in BIKE_FIELDS if any(label in row for row in rows)]
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"log-{datetime.fromtimestamp(start / 1000):%y-%m-%d-%H-%M-%S}.csv"
    with path.open("w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["time", "seconds", "target", *labels])
        for row in rows:
            writer.writerow([datetime.fromtimestamp(row["t"] / 1000).isoformat(timespec="milliseconds"),
                             f"{(row['t'] - start) / 1000:.1f}", f"{row['target'] or 0:g}",
                             *(f"{row[label]:g}" if label in row else "" for label in labels)])
    print(f"{stamp()}  log saved: {path} ({len(rows)} samples)")


def profile_files() -> list[str]:
    """The profiles offered in the panel: vorgabe-<name>.csv next to this program."""
    return sorted(path.name for path in PANEL_HTML.parent.glob("vorgabe-*.csv"))


def follow_profile():
    """Take the target power from the running profile, if there is one."""
    profile = CONTROL["profile"]
    if not profile or not profile["start"]:
        return
    steps = profile["steps"]
    elapsed = time.time() - profile["start"] / 1000
    CONTROL["target_power"] = next((watts for seconds, watts in reversed(steps) if seconds <= elapsed), 0)
    if elapsed >= steps[-1][0] and steps[-1][1] == 0:
        stop_profile()
        print(f"{stamp()}  profile {profile['name']} finished")


def set_status(text: str):
    STATUS["text"] = text
    print(text)


async def set_resistance(level: float):
    low, high, _ = CONTROL["range"]
    level = min(max(round(level), low), high)

    async def write(target: float):
        # FTMS "Set Target Resistance Level": opcode 0x04, sint16 in steps of 0.1.
        await CONTROL["client"].write_gatt_char(CONTROL_POINT, struct.pack("<Bh", 0x04, round(target * 10)), response=True)

    await write(level + CONTROL["write_offset"])
    # The AX 4000 can land one level beside the requested one, so check what it
    # reports, remember the difference for later writes and correct.
    await asyncio.sleep(1.5)
    reported = SAMPLES[-1].get("resistance") if SAMPLES else None
    if reported is not None and reported != level:
        CONTROL["write_offset"] = min(max(CONTROL["write_offset"] + level - reported, -2), 2)
        await write(max(level + CONTROL["write_offset"], 0))
    print(f"{stamp()}  resistance set to {level:g}")
    return level


async def power_control():
    """PID loop: steer the resistance level so the measured power follows the target.

    The AX 4000 acknowledges FTMS "Set Target Power" (opcode 0x05) but then ignores
    it and keeps its resistance level, so the control has to happen here.
    """
    integral = None
    last_error = 0.0
    last_target = 0
    last_time = time.monotonic()
    while True:
        await asyncio.sleep(1)
        now = time.monotonic()
        dt, last_time = now - last_time, now
        follow_profile()
        target = CONTROL["target_power"]
        recent = [s for s in SAMPLES[-3:] if "power" in s and "resistance" in s]
        if not target or not recent or time.time() * 1000 - recent[-1]["t"] > 3000:
            integral = None
            continue
        if recent[-1].get("cadence", 0) < MIN_CADENCE:
            continue  # hold instead of winding the resistance up while nobody pedals

        level = recent[-1]["resistance"]
        error = target - sum(s["power"] for s in recent) / len(recent)
        if abs(error) < PID_TOLERANCE:
            error = 0.0
        if integral is None:
            # Start from the current level so switching the controller on causes no jump.
            integral, last_error = float(level), error
        elif target != last_target:
            # Feedforward: on a new target, jump by the expected number of levels
            # instead of waiting for the integral to get there (matters for intervals).
            integral += (target - last_target) / WATTS_PER_LEVEL
        last_target = target
        low, high, _ = CONTROL["range"]
        integral = min(max(integral + PID_KI * error * dt, low), high)  # clamped: anti-windup
        output = min(max(integral + PID_KP * error + PID_KD * (error - last_error) / dt, low), high)
        last_error = error
        if abs(output - level) > PID_DEADBAND:
            try:
                await set_resistance(output)
            except Exception as exc:
                print(f"{stamp()}  could not set resistance: {exc}")


class PanelHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            body, content_type = PANEL_HTML.read_bytes(), "text/html; charset=utf-8"
        elif url.path == "/data":
            since = int(parse_qs(url.query).get("since", ["0"])[0])
            payload = {"status": STATUS["text"], "next": len(SAMPLES), "samples": SAMPLES[since:],
                       "connected": bool(CONTROL["range"]), "power_range": CONTROL["power_range"],
                       "target_power": CONTROL["target_power"], "profile": CONTROL["profile"],
                       "profiles": profile_files(),
                       # Lets an open page notice that panel.html was edited and reload itself.
                       "panel_version": PANEL_HTML.stat().st_mtime}
            body, content_type = json.dumps(payload).encode(), "application/json"
        else:
            self.send_error(404)
            return
        self.reply(200, body, content_type)

    def do_POST(self):
        try:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            profile = CONTROL["profile"]
            if self.path == "/target":
                # Setting the power by hand takes over from a running profile.
                stop_profile()
                CONTROL["target_power"] = min(max(float(request["power"]), 0), CONTROL["power_range"][1])
                print(f"{stamp()}  target power: {CONTROL['target_power']:g} W")
            elif self.path == "/profile" and request.get("name") in profile_files():
                load_profile(request["name"], (PANEL_HTML.parent / request["name"]).read_text())
                CONTROL["target_power"] = 0
                print(f"{stamp()}  profile loaded: {request['name']}")
            elif self.path == "/profile" and request.get("name") == "":
                save_log()
                CONTROL.update(profile=None, target_power=0)
            elif self.path == "/profile" and profile and request.get("action") == "start":
                start_profile()
                print(f"{stamp()}  profile started: {profile['name']}")
            elif self.path == "/profile" and profile and request.get("action") == "stop":
                stop_profile()
                CONTROL["target_power"] = 0
                print(f"{stamp()}  profile stopped")
            else:
                raise ValueError("unknown request")
            reply = {"power": CONTROL["target_power"], "profile": CONTROL["profile"]}
            self.reply(200, json.dumps(reply).encode(), "application/json")
        except Exception as exc:
            self.reply(400, json.dumps({"error": str(exc)}).encode(), "application/json")

    def reply(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_panel(port: int, open_browser: bool = True):
    server = ThreadingHTTPServer(("127.0.0.1", port), PanelHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"Panel: {url}")
    if open_browser:
        webbrowser.open(url)
    return server


def stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


async def scan(timeout: float):
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    return sorted(found.values(), key=lambda pair: pair[1].rssi, reverse=True)


async def find_ergometer(args):
    set_status(f"Scanning for {args.timeout:.0f} s ...")
    devices = await scan(args.timeout)
    hints = (args.name.lower(),) if args.name else NAME_HINTS

    def matches(device, adv):
        if args.address:
            return device.address.lower() == args.address.lower()
        name = (adv.local_name or device.name or "").lower()
        if any(hint in name for hint in hints):
            return True
        # Without an explicit name, any fitness machine is a good candidate.
        return not args.name and FTMS_SERVICE in adv.service_uuids

    for device, adv in devices:
        if matches(device, adv):
            return device

    set_status("Ergometer not found")
    print("Nearby devices:")
    print_devices(devices)
    print("\nWake the ergometer (pedal a few turns), make sure no phone/app is connected to it,")
    print("then retry or pick one with --name or --address.")
    return None


def print_devices(devices):
    for device, adv in devices:
        ftms = "  [fitness machine]" if FTMS_SERVICE in adv.service_uuids else ""
        print(f"  {device.address}  {adv.rssi:4d} dBm  {adv.local_name or device.name or '(no name)'}{ftms}")


async def run(args):
    if args.scan:
        print(f"Scanning for {args.timeout:.0f} s ...")
        print_devices(await scan(args.timeout))
        return

    if shutil.which("caffeinate"):
        # macOS: keep the display awake (no screen saver, no sleep) for as long as this program runs.
        subprocess.Popen(["caffeinate", "-d", "-i", "-w", str(os.getpid())])
    CONTROL["target_power"] = args.power
    if args.profile:
        load_profile(Path(args.profile).name, Path(args.profile).read_text())
    if args.panel:
        start_panel(args.port)
    device = await find_ergometer(args)
    if device is None:
        return

    disconnected = asyncio.Event()
    set_status(f"Connecting to {device.name or '(no name)'} [{device.address}] ...")
    async with BleakClient(device, disconnected_callback=lambda _: disconnected.set()) as client:
        set_status(f"Connected to {device.name or device.address}")
        print("Services:")
        notifiable = []
        for service in client.services:
            print(f"  {service.uuid}  {service.description}")
            for char in service.characteristics:
                print(f"    {char.uuid}  {','.join(char.properties):<28} {char.description}")
                if "notify" in char.properties or "indicate" in char.properties:
                    notifiable.append(char)

        def on_data(char, data: bytearray):
            if char.uuid == CONTROL_POINT and not args.raw:
                # Response: 0x80, request opcode, result (1 = success).
                if len(data) >= 3 and data[2] != 1:
                    print(f"{stamp()}  ergometer rejected command 0x{data[1]:02x} (result {data[2]})")
                return
            parser = PARSERS.get(char.uuid)
            if parser:
                try:
                    values = parser(bytes(data))
                    if char.uuid == INDOOR_BIKE_DATA:
                        SAMPLES.append({"t": time.time() * 1000, "target": CONTROL["target_power"] or None,
                                        **{k: v for k, (v, _) in values.items()}})
                    print(f"{stamp()}  " + "  ".join(f"{k}: {format_value(*v)}" for k, v in values.items()))
                    return
                except (struct.error, IndexError):
                    pass
            # Unknown or unparseable characteristic: show the raw bytes.
            if args.raw:
                print(f"{stamp()}  {char.uuid[4:8]} raw: {data.hex(' ')}")

        has_bike_data = any(char.uuid == INDOOR_BIKE_DATA for char in notifiable)
        if not has_bike_data:
            print("\nNo standard FTMS Indoor Bike Data characteristic - showing raw data only.")
            args.raw = True
        for char in notifiable:
            if not args.raw and char.uuid not in PARSERS and char.uuid != CONTROL_POINT:
                continue
            try:
                await client.start_notify(char, on_data)
            except Exception as exc:
                print(f"  could not subscribe to {char.uuid}: {exc}")

        uuids = {char.uuid for service in client.services for char in service.characteristics}
        if {CONTROL_POINT, RESISTANCE_RANGE} <= uuids:
            low, high, step = struct.unpack("<hhH", await client.read_gatt_char(RESISTANCE_RANGE))
            await client.write_gatt_char(CONTROL_POINT, b"\x00", response=True)  # request control
            CONTROL.update(loop=asyncio.get_running_loop(), client=client,
                           range=[low / 10, high / 10, (step or 10) / 10])
            if POWER_RANGE in uuids:
                low, high, step = struct.unpack("<hhH", await client.read_gatt_char(POWER_RANGE))
                CONTROL["power_range"] = [0, high, max(step, 5)]
            if args.profile:
                start_profile()  # a profile from the command line starts on connect
            CONTROL["task"] = asyncio.create_task(power_control())

        print("\nReceiving data, start pedalling. Press Ctrl+C to stop.\n")
        await disconnected.wait()
        CONTROL["range"] = None
        save_log()
        set_status("Ergometer disconnected")


def main():
    parser = argparse.ArgumentParser(description="Show live values from a Christopeit AX 4000 ergometer.")
    parser.add_argument("--name", help="name fragment to look for (default: FS-... / Christopeit / any fitness machine)")
    parser.add_argument("--address", help="connect to this device address (a UUID on macOS)")
    parser.add_argument("--timeout", type=float, default=10, help="scan time in seconds (default: 10)")
    parser.add_argument("--panel", action="store_true", help="show a live Plotly panel in the browser")
    parser.add_argument("--port", type=int, default=8050, help="port for the panel (default: 8050)")
    parser.add_argument("--power", type=float, default=0, help="target power in W, held by a PID controller on the resistance")
    parser.add_argument("--profile", help="CSV file with rows mm:ss,watt; the target power follows it once connected")
    parser.add_argument("--raw", action="store_true", help="also print raw bytes of all other notifications")
    parser.add_argument("--scan", action="store_true", help="only list nearby BLE devices")
    try:
        asyncio.run(run(parser.parse_args()))
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        save_log()  # a recording that is still running is kept


if __name__ == "__main__":
    main()
