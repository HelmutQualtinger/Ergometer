# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-user tool for a Christopeit AX 4000 ergometer: `ergometer.py` connects over Bluetooth LE, prints the live values, serves a browser panel and holds a target power with its own PID loop. The Python version has no build step; there is no test suite and no linter. `ergometer.c` is the same program in C (see "C version").

The README, the panel's default language and the profile file names (`vorgabe-*.csv`) are German; code, comments and commit messages are English.

## Commands

```
uv run ergometer.py --panel                               # connect, serve the panel on http://127.0.0.1:8050/
uv run ergometer.py --panel --profile vorgabe-hit.csv     # same, and start that profile on connect
uv run ergometer.py --scan                                # list nearby BLE devices only
uv run --with bleak python3 -c "import ergometer; ..."    # import the module to exercise functions without the bike
```

`ergometer.py` declares its one dependency (`bleak`) inline (PEP 723), so `uv run` is the way to run it; plain `python3` lacks `bleak`.

## C version

`ergometer.c` is a port of `ergometer.py` with the same options, HTTP API, log format and QR encoder; `make` builds `./ergometer` (git-ignored). Bluetooth sits behind `ble.h`, implemented for macOS in `ble_macos.m` (CoreBluetooth, blocking calls on top of a dispatch queue). Threads replace asyncio: Bluetooth callbacks, `power_control`, one thread per HTTP request and the MQTT subscriber share `SAMPLES`, `CONTROL`, `STATUS` and `ROOM` under the recursive mutex `LOCK`, which is released around Bluetooth writes and sleeps.

- Keep both versions in step: a change to the control loop, the HTTP API, the log columns or the QR code goes into both files.
- To test without the bike, compile `ergometer.c` together with a stub that implements `ble.h` and simulates the ergometer, copy `panel.html` and the profiles next to that binary and run it on another port. For single functions, `#define main ergometer_main`, `#include "ergometer.c"` and compare the output with the Python module.

## Working without the bike

- The ergometer accepts one Bluetooth connection, and the user usually has an instance running in their own terminal. A second instance fails with `Address already in use` on port 8050 and could not connect anyway. Do not kill the running instance without being asked.
- `panel.html` is read from disk on every request, and an open page reloads itself when the file's mtime changes (`panel_version`). Panel edits therefore show up without a restart; changes to `ergometer.py` need one.
- To see panel changes with data, serve `panel.html` from a small mock HTTP server that answers `/data` with fabricated samples (same JSON shape as `PanelHandler.do_GET`) and screenshot it with headless Chrome. Pressing Start/Stop on the real panel drives the user's bike.
- Functions such as `parse_profile`, `follow_profile` and `save_log` work on the module globals `SAMPLES` and `CONTROL` and can be tested by importing the module and filling those by hand.

## Architecture

Two files: `ergometer.py` (Bluetooth, PID controller, HTTP server) and `panel.html` (the whole panel, Plotly from a CDN).

**Shared state.** The asyncio thread (Bluetooth and control loop) and the HTTP server thread share module globals without locks:

- `SAMPLES` — every Indoor Bike Data notification as a dict (`t` in epoch ms, `target`, then the parsed fields). It is append-only and never trimmed.
- `CONTROL` — client, resistance and power ranges, `target_power`, the loaded `profile` (`{"name", "steps", "start"}`) and `log_start`.

**Data path.** FTMS Indoor Bike Data (`0x2AD2`) → `parse_indoor_bike_data` (driven by the `BIKE_FIELDS` table) → `SAMPLES` → `GET /data?since=N` → the panel, which polls once a second and only asks for samples it has not seen.

**Control path.** `power_control()` runs once a second: `follow_profile()` turns a running profile into `target_power`, then a resistance level is computed and `set_resistance()` writes it to the control point (`0x2AD9`). A new target jumps straight to the level that `WATT_TABLE` (from the AX 4000 manual) gives for the current cadence, plus the trim the PID had accumulated; the PID then waits `PID_SETTLE` seconds and only trims. The loop can be exercised without the bike by replacing `set_resistance` and feeding `SAMPLES` from a model that uses `table_power()`. The AX 4000 acknowledges FTMS "Set Target Power" but ignores it, which is why the control lives here. It can also land one level beside the requested one; `set_resistance()` reads the reported level back and keeps a `write_offset`.

**Manual run.** Without a file profile the panel's menu says "manual": Start then creates a profile with no name and no steps and takes the target power from the slider (`MAX_TARGET` = 300 W). `follow_profile()` leaves such a profile alone, `/target` changes the power without ending it, and Stop removes it again. While nothing runs and the controller is off, the panel does not send slider moves at all (`live` in `panel.html`).

**Start/Stop is the central state.** `profile["start"]` (epoch ms or `None`) decides three things at once:

- whether the profile drives `target_power`,
- whether a recording runs (`start_profile()` sets `log_start`; `save_log()` writes `logs/log-<yy-mm-dd-hh-mm-ss>.csv` from the samples since then),
- whether the panel draws curves (`syncRun()` in `panel.html`; before Start the charts stand at 00:00, after Stop they freeze).

Every path that ends a run must go through `stop_profile()` or `save_log()`: the Stop button, the profile's own end, a manual `/target`, choosing another profile, a disconnect and program exit. `save_log()` is idempotent.

**Access.** The panel server listens on all interfaces so a phone can use it, without any authentication (the user removed a per-start key on purpose: home network only, and it made things complicated). `/qr` returns the panel's LAN address and the QR modules; the panel only draws them.

**Phone companion.** There is no app: the phone opens the same `panel.html` in its browser and talks only to the HTTP server, never to the bike. `start_panel()` fills `COMPANION` (`url`, `qr`) once at start from `local_address()`, which takes the address of the default route — so a VPN that is up at start yields the wrong address, no network yields `127.0.0.1`, and a new DHCP lease needs a restart. The QR card starts out visible only on the computer that runs the program (`onThisComputer`). Theme, language, sound and the QR card's visibility are kept per device in `localStorage`. The panel holds no wake lock and has no web app manifest; the phone's own auto-lock decides how long the screen stays on. Below 640 px the countdown gets a line of its own.

**QR code.** `qr_matrix()` is the program's own encoder (byte mode, versions 1-6, levels L/M/Q/H chosen by `QR_LEVEL`, with interleaved blocks). After touching it, check that a decoder still reads the result, e.g. OpenCV's `QRCodeDetector` (`uv run --with opencv-python-headless`).

**Room temperature.** `follow_temperature()` is a minimal MQTT 3.1.1 subscriber on a plain socket (no library) that keeps `ROOM` current. Broker host, port and topic come from the environment or from `.env` next to the program (`read_env()`; `.env` is git-ignored, `.env.example` is the committed template) and must never be written into tracked files — the repo is public. Without `MQTT_HOST` and `MQTT_PORT` the thread is not started; the code holds no default for either. `MQTT_FIELD` and `MQTT_OFFSET` stay constants; `/data` carries it as `temperature`, and the panel hides the tile when the field is missing.

**Profiles.** Any `vorgabe-<name>.csv` next to the program is offered in the panel. Rows are `mm:ss,watt` step functions; a final row with 0 W ends the profile.

**Panel.** Tiles and charts are generated from the `TILES` and `CHARTS` tables. Colours come from CSS variables that are read when the charts are built, so a theme change reloads the page. There are several themes (`data-theme`, default `sub`) and languages (`TEXTS`); both can be forced with `/?theme=…&language=…`. `PULSE_BANDS` defines the heart-rate colour zones, and `POWER_BANDS` reuses the same colours spread evenly over `POWER_BAND_RANGE`; `bandColor()` also colours the pulse and power tiles.

## Evaluating rides

Two stand-alone scripts read `logs/log-*.csv` and write a Plotly page next to the log; each has a project skill in `.claude/skills/` that says when and how to run it.

- `stufentest.py` rates a step test against reference data for men (FRIEND, SHIP) and can print a PDF.
- `lauf.py` reports on any ride, joins several logs into one, and with `--training` files the ride in `~/training/training_log.csv` through that project's `add_training.py`, then fills the columns a Kinomap summary would have filled.

Both pages embed their data; the generated files are personal and git-ignored.

## Conventions

- New profiles or user-visible features get a line in `README.md` (German).
- The README's "Installation" and "Handy als Fernbedienung" sections (setup, operation, troubleshooting table, security) describe the phone companion; keep them in step with changes to `start_panel()`, `local_address()`, `/qr` and the phone-specific parts of `panel.html`.
- `logs/` holds the user's own recordings, including heart rate. The GitHub repo is public; do not commit that directory unless asked.
