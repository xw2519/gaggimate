# Gaggimate Development Scripts

This directory contains various utility scripts for development and debugging.

## USB Flashing

### `flash.py`

Flashes the display, controller or headless board over USB, like the web flasher on gaggimate.eu. It runs with
[uv](https://docs.astral.sh/uv/), which installs esptool on first use.

```bash
uv run scripts/flash.py ports                                    # list serial ports, ESP32-S3 boards are marked
uv run scripts/flash.py flash display --source nightly           # bootloader + partitions + app from the nightly release
uv run scripts/flash.py flash controller --source release --version v1.9.0
uv run scripts/flash.py flash display --source pr --pr 123 --full   # PR preview build, also writes LittleFS (wipes profiles)
uv run scripts/flash.py flash display                            # local .pio/build output
uv run scripts/flash.py monitor --seconds 20                     # print the boot log
uv run scripts/flash.py ota --host 192.168.1.11 --channel nightly   # update controller + display through the display's OTA
```

`ota` needs the display on WiFi with internet; the display downloads the release itself and pushes the controller firmware
over BLE. On macOS run it with `uv run --python /opt/homebrew/bin/python3 ...`, because the uv-managed Python has no
Local Network permission.

The port is auto-detected when exactly one ESP32-S3 is attached. Downloads are cached in `~/.cache/gaggimate-flash/`.
Set `GAGGIMATE_REPO=owner/repo` or pass `--repo` to flash builds from a fork.

## Core Dump Analysis

### `analyze_coredump.py` / `analyze_coredump.sh`

Automated ESP32 core dump analysis for PlatformIO projects.

**Features:**
- Automatically extracts ELF core dump from ESP32 proprietary format
- Uses ESP-IDF GDB tools for detailed analysis
- Shows exact crash location with line numbers
- Displays full call stack (backtrace)
- Shows register values at time of crash
- Lists all threads and their states
- Provides actionable debugging recommendations

**Usage:**
```bash
# Python script (direct)
python3 scripts/analyze_coredump.py <coredump_file> [environment]

# Shell wrapper (simpler)
./scripts/analyze_coredump.sh <coredump_file> [environment]
```

**Examples:**
```bash
# Analyze core dump with default environment (display)
python3 scripts/analyze_coredump.py ~/Downloads/coredump.bin

# Analyze core dump with specific environment
python3 scripts/analyze_coredump.py ~/Downloads/coredump.bin display
python3 scripts/analyze_coredump.py ~/Downloads/coredump.bin controller
python3 scripts/analyze_coredump.py ~/Downloads/coredump.bin display-headless

# Using shell wrapper
./scripts/analyze_coredump.sh ~/Downloads/coredump.bin
./scripts/analyze_coredump.sh ~/Downloads/coredump.bin controller
```

**Requirements:**
- ESP-IDF tools installed (automatic with VS Code ESP-IDF extension)
- PlatformIO project with built firmware
- Python 3.x

**Sample Output:**
```
🚀 ESP32 Core Dump Analyzer
==================================================
Core dump: /home/user/Downloads/coredump.bin
Environment: display

✅ Found GDB: xtensa-esp32s3-elf-gdb
✅ ELF header found at offset: 20
✅ Extracted ELF core dump to: /tmp/tmpXXXXX.elf

================================================================================
🔍 CORE DUMP ANALYSIS
================================================================================
#0  DefaultUI::updateStatusScreen (this=0x3fced5e4) at src/display/ui/default/DefaultUI.cpp:655
655         if (process->getType() != MODE_BREW) {
#1  0x420254df in DefaultUI::loop (this=0x3fced5e4) at src/display/ui/default/DefaultUI.cpp:230
#2  0x42025510 in DefaultUI::loopTask (arg=0x3fced5e4) at src/display/ui/default/DefaultUI.cpp:766
...
```

**Getting Core Dumps:**
1. **From Web Interface:** Visit `http://your-device-ip/`, go to System & Updates, click "Download Core Dump"
2. **From Serial Monitor:** Core dumps appear in terminal output after crashes
3. **From Device Flash:** Use `esptool.py` to read core dump partition

**Interactive Analysis:**
For deeper debugging, use the extracted ELF file with GDB interactively:
```bash
xtensa-esp32s3-elf-gdb .pio/build/display/firmware.elf
(gdb) core-file /tmp/extracted_coredump.elf
(gdb) bt
(gdb) list
(gdb) info locals
(gdb) print variable_name
```

## One-off Firmware Releases

### `release.sh`

Builds the three firmware targets the same way CI does and publishes them as a GitHub release for an arbitrary tag.
Use it for one-off test builds handed to individual users; `v*` tags stay with the CI pipeline (`build.yml`).

**What it does:**
- Firmware reports the usual nightly-style version (`v1.8.1-234-gabc`, only `v*` tags count); the release tag never lands in the binary
- Builds the web UI, `controller`, `display` (+ LittleFS seed image) and `display-headless`
- Stages the artifacts in `out/` with the CI file names (`board-*.bin`, `display-*.bin`, `display-headless-*.bin`, `version.txt`)
- Force-pushes the tag (by SHA, no local tag is created) to `origin`, deletes any existing release with that tag and recreates it

**Defaults mirror the nightly channel:** `-DNIGHTLY_BUILD`, marked as pre-release, never marked "Latest".
It refuses to run on a dirty working tree and refuses `v<digits>` tags. It does not upload to the update
server or the gh-pages flasher directory; those remain CI-only.

**Usage:**
```bash
# Build everything and publish/overwrite the release "pairing-test"
./scripts/release.sh pairing-test

# Custom title and notes, stable build flags instead of -DNIGHTLY_BUILD
./scripts/release.sh -t "Pairing test build" -b "Try the steam-switch pairing window" --release-flags pairing-test

# Build only, print the publish steps without touching GitHub
./scripts/release.sh --dry-run pairing-test

# Re-publish what is already in out/ (e.g. after a failed upload)
./scripts/release.sh --skip-build pairing-test
```

Run `./scripts/release.sh --help` for all options (`--skip-web`, `--allow-dirty`, `--no-prerelease`, `--repo`, `--notes-file`).
