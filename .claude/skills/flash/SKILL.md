---
name: flash
description: Flash or update GaggiMate firmware with scripts/flash.py, over USB or through the display's OTA. Use when asked to flash, upload, install or update firmware on the display, controller or headless board, to list serial ports, or to read the serial boot log after a flash.
---

# Flash GaggiMate firmware

`scripts/flash.py` wraps esptool and the display's own OTA updater. Run it with `uv run scripts/flash.py ...`; uv installs the dependencies on first use. `--help` on each subcommand is the source of truth for flags.

Two paths:

- **USB** (`ports`, `flash`, `monitor`): the board is plugged into this machine. Works for a bricked board and for any source, including local builds.
- **OTA** (`ota`): the display is on WiFi with internet. The display downloads the release from GitHub, streams the controller firmware over BLE, then flashes itself and reboots. Nothing needs to be plugged in, and this is the only way to update the controller while it sits inside the machine.

## OTA steps

1. Find the display's IP: from the serial log (`monitor`), the router, or the user.
2. Check what the display offers: `uv run --python /opt/homebrew/bin/python3 scripts/flash.py ota --host <ip> --channel nightly --dry-run`. On macOS the uv-managed Python has no Local Network permission (connections fail with "No route to host"), so pass the Homebrew interpreter for `ota`.
3. Run the same command without `--dry-run`. `--component controller` or `--component display` limits it; the default updates both. The controller takes a few minutes over BLE. The command ends on "finished" or "failed".
4. Confirm with `monitor --seconds 25` or another `--dry-run`: both versions should match the latest and no "Protocol version mismatch" line should appear.

The channel setting persists on the display. Switch back with `--channel latest --dry-run` when the user only wanted one nightly update.

## USB steps

1. Find the board: `uv run scripts/flash.py ports`. Both boards enumerate as ESP32-S3 USB (303A:1001). With one board attached, every command auto-detects the port; with two, pass `--port`.
2. Pick the target: `display` (LilyGo T-RGB), `controller` (GaggiMate board), `headless`.
3. Pick the source:
   - `--source local` (default): `.pio/build/<env>/`. Only works on a machine with PlatformIO; build first with `pio run -e <env>`.
   - `--source nightly`, `--source release [--version vX.Y.Z]`, `--source pr --pr N`: downloaded from the upstream repo jniebuhr/gaggimate into `~/.cache/gaggimate-flash/`.
   - `--source dir --dir out/`: a folder with CI-named files (`display-firmware.bin`, `board-firmware.bin`, ...).
4. Flash: `uv run scripts/flash.py flash display --source nightly`. The default writes bootloader, partition table and app, and keeps the LittleFS data. Add `--full` only when the user wants a factory state: it overwrites the LittleFS image and wipes profiles and settings. Use `--app-only` for a quick firmware swap.
5. Confirm it booted: `uv run scripts/flash.py monitor --seconds 20`. The display logs at 115200 baud. Report the version line and any crash to the user.

## After an OTA

OTA writes the display app to `app1` and points `otadata` at it. `flash` always writes the app to `app0` and blanks `otadata` in the same command, so the board boots the new USB image. If a USB flash "succeeds" but the old firmware still runs, check that `otadata` (0xe000) was in the write-flash command.

## When esptool cannot connect

Both boards use native USB. If "Failed to connect" appears, ask the user to hold BOOT, press RESET (or re-plug), release BOOT, then rerun the same command. The port name may change after a manual bootloader entry; run `ports` again.
