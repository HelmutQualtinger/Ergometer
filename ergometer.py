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
import secrets
import shutil
import socket
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
PID_SETTLE = 3      # s; after a jump to a new level the controller waits for the measured power to follow
PID_MAX_TRIM = 3    # levels; how far the controller's correction to the table is carried over to a new target
MIN_CADENCE = 20    # rpm; below this the rider has stopped and the controller holds
MAX_TARGET = 300    # W; as far as the slider for the target power goes

# The AX 4000 advertises under its FitShow module name, e.g. "FS-1837D8".
# The room temperature comes from a sensor that publishes JSON telemetry over MQTT. Where the
# broker is stays out of the repository: MQTT_HOST, MQTT_PORT and MQTT_TOPIC come from the
# environment or from a file .env next to the program (KEY=value per line, see .env.example).
# Without MQTT_HOST and MQTT_PORT there is no temperature.
def read_env(path: Path) -> dict[str, str]:
    """The settings from a .env file; variables that are already set in the environment win."""
    settings = {}
    if path.exists():
        for line in path.read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator and not key.strip().startswith("#"):
                settings[key.strip()] = value.strip().strip("\"'")
    return {**settings, **os.environ}


ENV = read_env(Path(__file__).with_name(".env"))
MQTT_HOST, MQTT_PORT = ENV.get("MQTT_HOST", ""), int(ENV.get("MQTT_PORT") or 0)
MQTT_TOPIC = ENV.get("MQTT_TOPIC", "")
MQTT_FIELD = "roomtemp"
MQTT_OFFSET = -2.0  # °C; the sensor reads this much too warm
ROOM = {"temperature": None, "t": 0.0}  # the latest reading and when it arrived

# Power in watts by resistance level (rows, 1 to 24) and cadence (columns), from the manual of
# the AX 4000 (Art.-Nr. 2007, "WATT TABELLE"). The manual gives it up to 80 rpm.
WATT_TABLE_RPM = (20, 30, 40, 50, 60, 70, 80)
WATT_TABLE = (
    (4, 8, 14, 20, 28, 35, 42), (6, 11, 19, 27, 38, 48, 60), (7, 13, 23, 34, 48, 61, 77), (8, 16, 28, 41, 58, 74, 93),
    (9, 19, 33, 48, 68, 89, 110), (10, 21, 37, 54, 78, 100, 125), (11, 23, 41, 61, 88, 112, 142), (13, 27, 47, 68, 98, 124, 159),
    (15, 29, 52, 76, 108, 137, 176), (16, 31, 56, 82, 118, 148, 187), (17, 35, 62, 90, 128, 164, 203), (18, 37, 66, 96, 138, 172, 220),
    (19, 40, 70, 103, 148, 185, 236), (20, 43, 75, 110, 158, 201, 252), (22, 46, 79, 117, 168, 215, 269), (23, 50, 85, 125, 178, 228, 286),
    (25, 53, 90, 133, 188, 246, 304), (26, 56, 95, 141, 198, 251, 318), (28, 59, 100, 149, 208, 272, 332), (29, 63, 104, 156, 218, 283, 346),
    (31, 65, 108, 162, 228, 291, 361), (32, 67, 113, 168, 238, 303, 378), (34, 71, 120, 175, 248, 320, 397), (36, 74, 127, 182, 258, 335, 416),
)
WATT_TABLE_EXPONENT = 1.7  # above 80 rpm the power is taken to grow with cadence^1.7, as measured on this ergometer


def table_power(level: int, cadence: float) -> float:
    """The power the table gives for a level at a cadence, interpolated between its columns."""
    row, rpm = WATT_TABLE[level - 1], WATT_TABLE_RPM
    if cadence <= rpm[0]:
        return row[0] * cadence / rpm[0]
    if cadence >= rpm[-1]:
        return row[-1] * (cadence / rpm[-1]) ** WATT_TABLE_EXPONENT
    i = next(i for i in range(len(rpm) - 1) if cadence < rpm[i + 1])
    return row[i] + (row[i + 1] - row[i]) * (cadence - rpm[i]) / (rpm[i + 1] - rpm[i])


def table_level(power: float, cadence: float) -> float:
    """The level, with fractions, at which the table gives this power at this cadence (1 to 24)."""
    powers = [table_power(level, cadence) for level in range(1, len(WATT_TABLE) + 1)]
    if power <= powers[0]:
        return 1.0
    if power >= powers[-1]:
        return float(len(powers))
    i = next(i for i in range(len(powers) - 1) if power < powers[i + 1])
    return i + 1 + (power - powers[i]) / (powers[i + 1] - powers[i])


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
CONTROL = {"loop": None, "client": None, "range": None, "power_range": [0, MAX_TARGET, 5],
           "target_power": 0, "write_offset": 0,
           # Power profile from a file: {"name", "steps": [[seconds, watts], ...], "start": epoch ms or None}.
           # A manual run is a profile without a name and without steps: the slider sets the power.
           "profile": None,
           # Epoch ms of the "Start" press while a recording runs; see save_log().
           "log_start": None}
LOG_DIR = PANEL_HTML.parent / "logs"
# The panel for a phone in the same network: {"url", "qr"}; see start_panel().
COMPANION = {}
# QR code versions 1 to 6, which is plenty for a short address. Per error correction level:
# the two bits that name it in the code, then for each version the error correction bytes
# per block and the number of blocks. A code survives damage to about 7 % (L), 15 % (M),
# 25 % (Q) or 30 % (H) of its bytes; more redundancy makes it larger.
QR_CODEWORDS = [26, 44, 70, 100, 134, 172]  # all bytes of a version, data and error correction
QR_LEVELS = {"L": (1, [(7, 1), (10, 1), (15, 1), (20, 1), (26, 1), (18, 2)]),
             "M": (0, [(10, 1), (16, 1), (26, 1), (18, 2), (24, 2), (16, 4)]),
             "Q": (3, [(13, 1), (22, 1), (18, 2), (26, 2), (18, 4), (24, 4)]),
             "H": (2, [(17, 1), (28, 1), (22, 2), (16, 4), (22, 4), (28, 4)])}
QR_LEVEL = "Q"
QR_MASKS = [lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0, lambda x, y: (x + y) % 3 == 0,
            lambda x, y: (x // 3 + y // 2) % 2 == 0, lambda x, y: x * y % 2 + x * y % 3 == 0,
            lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0, lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0]


def qr_matrix(text: str) -> list[list[bool]]:
    """The QR code for a short text as rows of modules, True = dark (ISO 18004, byte mode, level QR_LEVEL)."""
    data = text.encode()
    level_bits, blocks_by_version = QR_LEVELS[QR_LEVEL]
    capacities = [total - ec * blocks for total, (ec, blocks) in zip(QR_CODEWORDS, blocks_by_version)]
    version = next((v for v, capacity in enumerate(capacities, 1) if len(data) <= capacity - 2), None)
    if version is None:
        raise ValueError("text too long for the QR code")
    capacity, (ec_length, n_blocks) = capacities[version - 1], blocks_by_version[version - 1]
    size = 17 + 4 * version

    # Mode and length, the bytes, a terminator, then padding up to the capacity.
    bits = "0100" + f"{len(data):08b}" + "".join(f"{byte:08b}" for byte in data)
    bits += "0" * min(4, capacity * 8 - len(bits))
    bits += "0" * (-len(bits) % 8)
    codewords = ([int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)] + [0xEC, 0x11] * capacity)[:capacity]

    def multiply(x: int, y: int) -> int:  # in the field GF(2^8) the Reed-Solomon code works in
        z = 0
        for i in range(7, -1, -1):
            z = (z << 1) ^ ((z >> 7) * 0x11D)
            z ^= ((y >> i) & 1) * x
        return z

    divisor, root = [0] * (ec_length - 1) + [1], 1
    for _ in range(ec_length):
        for j in range(ec_length):
            divisor[j] = multiply(divisor[j], root) ^ (divisor[j + 1] if j + 1 < ec_length else 0)
        root = multiply(root, 2)

    def error_correction(block: list[int]) -> list[int]:
        remainder = [0] * ec_length
        for byte in block:
            factor = byte ^ remainder[0]
            remainder = [r ^ multiply(d, factor) for r, d in zip(remainder[1:] + [0], divisor)]
        return remainder

    # The data is split into blocks that are protected separately; the later ones are a byte
    # longer when it doesn't divide evenly. Their bytes are then dealt out in turn, so damage
    # in one place is spread over all blocks.
    short, longer = divmod(capacity, n_blocks)
    blocks, at = [], 0
    for i in range(n_blocks):
        length = short + (i >= n_blocks - longer)
        blocks.append(codewords[at:at + length])
        at += length
    checks = [error_correction(block) for block in blocks]
    stream = [block[i] for i in range(short + 1) for block in blocks if i < len(block)]
    stream += [check[i] for i in range(ec_length) for check in checks]

    dark = [[False] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]  # modules that are not data

    def put(x: int, y: int, value: bool):
        if 0 <= x < size and 0 <= y < size:
            dark[y][x], fixed[y][x] = value, True

    def put_format(mask: int):
        bits = remainder = level_bits << 3 | mask  # the level and the mask, protected by a BCH code
        for _ in range(10):
            remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
        bits = (bits << 10 | remainder) ^ 0x5412
        for i in range(15):
            bit = bits >> i & 1 == 1
            put(*((8, i) if i < 6 else (8, 7) if i == 6 else (8, 8) if i == 7 else (7, 8) if i == 8 else (14 - i, 8)), bit)
            put(*((size - 1 - i, 8) if i < 8 else (8, size - 15 + i)), bit)
        put(8, size - 8, True)

    for i in range(size):  # timing patterns
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):  # finder patterns with their separators
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                put(cx + dx, cy + dy, max(abs(dx), abs(dy)) not in (2, 4))
    if version > 1:  # alignment pattern
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                put(size - 7 + dx, size - 7 + dy, max(abs(dx), abs(dy)) != 1)
    put_format(0)  # reserves the modules

    # The data runs in a zigzag of two-module columns from the bottom right, skipping the timing column.
    i, right = 0, size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vertical in range(size):
            for x in (right, right - 1):
                y = size - 1 - vertical if (right + 1) & 2 == 0 else vertical
                if not fixed[y][x] and i < len(stream) * 8:
                    dark[y][x] = stream[i >> 3] >> (7 - (i & 7)) & 1 == 1
                    i += 1
        right -= 2

    def penalty() -> int:
        lines = dark + [list(column) for column in zip(*dark)]
        score = 0
        for line in lines:
            run = 1
            for i in range(1, size + 1):  # runs of five or more of one colour
                if i < size and line[i] == line[i - 1]:
                    run += 1
                else:
                    score += run - 2 if run >= 5 else 0
                    run = 1
            pattern = "".join("1" if module else "0" for module in line)  # anything that looks like a finder pattern
            score += 40 * sum(pattern.startswith(("10111010000", "00001011101"), i) for i in range(size - 10))
        score += 3 * sum(dark[y][x] == dark[y][x + 1] == dark[y + 1][x] == dark[y + 1][x + 1]
                         for y in range(size - 1) for x in range(size - 1))
        count = sum(map(sum, dark))  # balance of dark and light
        return score + 10 * ((abs(count * 20 - size * size * 10) + size * size - 1) // (size * size) - 1)

    def apply(mask: int):
        for y in range(size):
            for x in range(size):
                dark[y][x] ^= QR_MASKS[mask](x, y) and not fixed[y][x]
        put_format(mask)

    # The mask that gives the calmest picture wins.
    scores = []
    for mask in range(8):
        apply(mask)
        scores.append(penalty())
        apply(mask)  # masking twice undoes it
    apply(scores.index(min(scores)))
    return dark


def qr_text(matrix: list[list[bool]]) -> str:
    """The QR code for a terminal: two rows of modules per line, black on white whatever the terminal's colours are."""
    quiet = 2
    size = len(matrix) + 2 * quiet
    module = lambda x, y: 0 <= x - quiet < len(matrix) and 0 <= y - quiet < len(matrix) and matrix[y - quiet][x - quiet]
    return "\n".join("\033[30;107m" + "".join(" ▄▀█"[2 * module(x, y) + module(x, y + 1)] for x in range(size)) + "\033[0m"
                     for y in range(0, size, 2))


def local_address() -> str:
    """This computer's address in the local network (nothing is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"


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
    if not profile or not profile["start"] or not profile["steps"]:
        return  # in a manual run the slider sets the power
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
    """Steer the resistance level so the measured power follows the target.

    A new target sets the level straight from the manufacturer's table for the current cadence.
    The PID loop only trims from there: it corrects what the table gets wrong on this
    ergometer and follows the rider's cadence.

    The AX 4000 acknowledges FTMS "Set Target Power" (opcode 0x05) but then ignores
    it and keeps its resistance level, so the control has to happen here.
    """
    integral = None
    last_error = None
    last_target = 0
    settled = 0.0  # from when on the PID may work again after a jump
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
        cadence = recent[-1].get("cadence", 0)
        if cadence < MIN_CADENCE:
            continue  # hold instead of winding the resistance up while nobody pedals

        level = recent[-1]["resistance"]
        low, high, _ = CONTROL["range"]
        if integral is None or target != last_target:
            # A new target: jump to the level the table gives for it at this cadence. What the
            # controller had to add to the table for the old target is carried over.
            trim = integral - table_level(last_target, cadence) if integral is not None and last_target else 0.0
            trim = min(max(trim, -PID_MAX_TRIM), PID_MAX_TRIM)
            integral = min(max(table_level(target, cadence) + trim, low), high)
            last_target, last_error = target, None
            try:
                if round(integral) != level:
                    await set_resistance(integral)
            except Exception as exc:
                print(f"{stamp()}  could not set resistance: {exc}")
            settled = time.monotonic() + PID_SETTLE
            continue
        if now < settled:
            continue  # the measured power still belongs to the old level

        error = target - sum(s["power"] for s in recent) / len(recent)
        if abs(error) < PID_TOLERANCE:
            error = 0.0
        if last_error is None:
            last_error = error  # no kick from the derivative on the first step after a jump
        integral = min(max(integral + PID_KI * error * dt, low), high)  # clamped: anti-windup
        output = min(max(integral + PID_KP * error + PID_KD * (error - last_error) / dt, low), high)
        last_error = error
        if abs(output - level) > PID_DEADBAND:
            try:
                await set_resistance(output)
            except Exception as exc:
                print(f"{stamp()}  could not set resistance: {exc}")


def mqtt_string(text: str) -> bytes:
    return struct.pack(">H", len(text.encode())) + text.encode()


def mqtt_packet(kind: int, body: bytes) -> bytes:
    """An MQTT control packet: type and flags, the length in 7-bit groups, then the body."""
    length, encoded = len(body), b""
    while True:
        length, digit = divmod(length, 128)
        encoded += bytes([digit | (0x80 if length else 0)])
        if not length:
            return bytes([kind]) + encoded + body


def follow_temperature():
    """Keep ROOM up to date from the MQTT topic; runs in its own thread and reconnects for ever.

    A small MQTT 3.1.1 client, enough to subscribe to one topic without acknowledgements.
    """
    def receive(connection: socket.socket, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = connection.recv(count - len(data))
            if not chunk:
                raise ConnectionError("MQTT broker closed the connection")
            data += chunk
        return data

    while True:
        try:
            with socket.create_connection((MQTT_HOST, MQTT_PORT), timeout=10) as connection:
                # Connect with a clean session and a keep-alive of 60 s, then subscribe.
                connection.sendall(mqtt_packet(0x10, mqtt_string("MQTT") + bytes([4, 2, 0, 60]) + mqtt_string(f"ergometer-{secrets.token_hex(4)}")))
                connection.sendall(mqtt_packet(0x82, bytes([0, 1]) + mqtt_string(MQTT_TOPIC) + bytes([0])))
                connection.settimeout(30)
                while True:
                    try:
                        kind = receive(connection, 1)[0]
                    except TimeoutError:
                        connection.sendall(mqtt_packet(0xC0, b""))  # ping, so the broker keeps the connection
                        continue
                    length, shift = 0, 0
                    while True:
                        digit = receive(connection, 1)[0]
                        length |= (digit & 0x7F) << shift
                        shift += 7
                        if not digit & 0x80:
                            break
                    body = receive(connection, length)
                    if kind >> 4 != 3:
                        continue  # only published messages matter
                    topic_length = struct.unpack_from(">H", body)[0]
                    payload = body[2 + topic_length + (2 if kind & 0x06 else 0):]
                    try:
                        ROOM.update(temperature=round(float(json.loads(payload)[MQTT_FIELD]) + MQTT_OFFSET, 1), t=time.time())
                    except (ValueError, KeyError, TypeError):
                        pass  # a message without a usable reading
        except OSError as exc:
            print(f"{stamp()}  room temperature: {exc}; trying again in 30 s")
            time.sleep(30)


class PanelHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            body, content_type = PANEL_HTML.read_bytes(), "text/html; charset=utf-8"
        elif url.path == "/qr":
            modules = ["".join("1" if module else "0" for module in row) for row in COMPANION["qr"]]
            body, content_type = json.dumps({"url": COMPANION["url"], "modules": modules}).encode(), "application/json"
        elif url.path == "/data":
            since = int(parse_qs(url.query).get("since", ["0"])[0])
            payload = {"status": STATUS["text"], "next": len(SAMPLES), "samples": SAMPLES[since:],
                       "connected": bool(CONTROL["range"]), "power_range": CONTROL["power_range"],
                       "target_power": CONTROL["target_power"], "profile": CONTROL["profile"],
                       "profiles": profile_files(),
                       # Room temperature in °C, or None while there is no reading from the last two minutes.
                       "temperature": ROOM["temperature"] if time.time() - ROOM["t"] < 120 else None,
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
            limit = lambda watts: min(max(float(watts), 0), CONTROL["power_range"][1])
            if self.path == "/target":
                # Setting the power by hand takes over from a running profile; a manual run just carries on.
                if profile and profile["steps"]:
                    stop_profile()
                CONTROL["target_power"] = limit(request["power"])
                print(f"{stamp()}  target power: {CONTROL['target_power']:g} W")
            elif self.path == "/profile" and request.get("name") in profile_files():
                load_profile(request["name"], (PANEL_HTML.parent / request["name"]).read_text())
                CONTROL["target_power"] = 0
                print(f"{stamp()}  profile loaded: {request['name']}")
            elif self.path == "/profile" and request.get("name") == "":
                save_log()
                CONTROL.update(profile=None, target_power=0)
            elif self.path == "/profile" and request.get("action") == "start":
                if not profile:
                    # "Start" without a profile is a manual run at the power the slider shows.
                    profile = CONTROL["profile"] = {"name": "", "steps": [], "start": None}
                    CONTROL["target_power"] = limit(request.get("power", CONTROL["target_power"]))
                start_profile()
                print(f"{stamp()}  " + (f"profile started: {profile['name']}" if profile["steps"]
                                         else f"manual run started at {CONTROL['target_power']:g} W"))
            elif self.path == "/profile" and profile and request.get("action") == "stop":
                stop_profile()
                CONTROL["target_power"] = 0
                if not profile["steps"]:
                    CONTROL["profile"] = None  # a manual run leaves nothing behind
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
    # The panel listens in the whole local network, so a phone can be the remote control at the
    # ergometer: anyone in that network can open it. The QR code only saves typing the address.
    companion = f"http://{local_address()}:{port}/"
    COMPANION.update(url=companion, qr=qr_matrix(companion))
    server = ThreadingHTTPServer(("0.0.0.0", port), PanelHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if MQTT_HOST and MQTT_PORT:
        threading.Thread(target=follow_temperature, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"Panel: {url}")
    print(f"Phone: {companion}\n{qr_text(COMPANION['qr'])}")
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
                CONTROL["power_range"] = [0, MAX_TARGET, max(step, 5)]  # the ergometer's own limit is not ours: the PID sets levels
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
