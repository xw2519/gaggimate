#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["esptool>=5,<6", "pyserial>=3.5", "websockets>=13"]
# ///
"""Flash GaggiMate firmware over USB, like the web flasher on gaggimate.eu.

Run with uv so the dependencies install on first use:

    uv run scripts/flash.py ports
    uv run scripts/flash.py flash display --source nightly
    uv run scripts/flash.py flash controller --source release --version v1.9.0
    uv run scripts/flash.py flash display --source pr --pr 123 --full
    uv run scripts/flash.py flash display --source local
    uv run scripts/flash.py monitor --seconds 20
    uv run scripts/flash.py ota --host 192.168.1.11 --channel nightly

`ota` drives the display's own updater over its websocket API: the display
downloads from GitHub, streams the controller firmware over BLE, then updates
itself and reboots. The display needs WiFi with internet access for this.

Targets: display, controller, headless. Both boards are ESP32-S3, so the
bootloader goes to 0x0, the partition table to 0x8000 and the app to 0x10000.
Every app write also blanks otadata, so the board boots app0 even after an OTA.
The LittleFS offset is read from the partition table binary itself.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import serial
from serial.tools import list_ports

ROOT = Path(__file__).resolve().parent.parent
CACHE = Path(os.environ.get("GAGGIMATE_FLASH_CACHE", Path.home() / ".cache" / "gaggimate-flash"))
DEFAULT_REPO = "jniebuhr/gaggimate"
ESPRESSIF_USB = (0x303A, 0x1001)

BOOTLOADER_OFFSET = 0x0
PARTITIONS_OFFSET = 0x8000
APP_OFFSET = 0x10000


@dataclass(frozen=True)
class Target:
    name: str
    env: str  # PlatformIO env for local builds
    ci_prefix: str  # file prefix used by CI release assets and PR previews
    fs_env: str  # env whose littlefs.bin is used (CI reuses the display FS for headless)


TARGETS = {
    "display": Target("display", "display", "display", "display"),
    "controller": Target("controller", "controller", "board", "controller"),
    "headless": Target("headless", "display-headless", "display-headless", "display"),
}

PARTS = ("bootloader", "partitions", "firmware", "filesystem")
LOCAL_NAMES = {"bootloader": "bootloader.bin", "partitions": "partitions.bin", "firmware": "firmware.bin", "filesystem": "littlefs.bin"}


def die(msg: str, code: int = 1) -> NoReturn:
    print(f"flash.py: {msg}", file=sys.stderr)
    sys.exit(code)


# --------------------------------------------------------------------------- ports


def espressif_ports():
    return [p for p in list_ports.comports() if (p.vid, p.pid) == ESPRESSIF_USB]


def cmd_ports(args) -> int:
    ports = list(list_ports.comports())
    if args.json:
        print(json.dumps([{"port": p.device, "vid": p.vid, "pid": p.pid, "description": p.description,
                           "serial": p.serial_number, "gaggimate": (p.vid, p.pid) == ESPRESSIF_USB} for p in ports], indent=2))
        return 0
    if not ports:
        print("No serial ports found.")
        return 0
    for p in ports:
        tag = "  <- ESP32-S3 (display or controller)" if (p.vid, p.pid) == ESPRESSIF_USB else ""
        ids = f"{p.vid:04x}:{p.pid:04x}" if p.vid else "    -    "
        print(f"{p.device:32} {ids}  {p.description}{tag}")
    return 0


def resolve_port(port: str | None) -> str:
    if port:
        return port
    found = espressif_ports()
    if len(found) == 1:
        return found[0].device
    if not found:
        die("no ESP32-S3 USB device found; plug the board in or pass --port (see `ports`)")
    die("several ESP32-S3 devices found, pass --port: " + ", ".join(p.device for p in found))


# --------------------------------------------------------------------------- sources


def default_repo() -> str:
    return os.environ.get("GAGGIMATE_REPO", DEFAULT_REPO)


def fetch(url: str, dest: Path, fresh: bool) -> Path:
    if dest.exists() and not fresh:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  download {url}")
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
    except urllib.error.HTTPError as e:
        tmp.unlink(missing_ok=True)
        die(f"HTTP {e.code} for {url}")
    tmp.replace(dest)
    return dest


def latest_release_tag(repo: str) -> str:
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest", headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["tag_name"]
    except urllib.error.HTTPError as e:
        die(f"HTTP {e.code} looking up the latest release of {repo}")


def local_files(target: Target, parts: list[str]) -> dict[str, Path]:
    build = ROOT / ".pio" / "build"
    files = {}
    for part in parts:
        env = target.fs_env if part == "filesystem" else target.env
        path = build / env / LOCAL_NAMES[part]
        if part == "filesystem" and not path.exists():
            path = build / target.env / LOCAL_NAMES[part]
        if not path.exists():
            hint = f"pio run -t buildfs -e {env}" if part == "filesystem" else f"pio run -e {env}"
            die(f"missing {path.relative_to(ROOT)}; build it with `{hint}`")
        files[part] = path
    return files


def dir_files(target: Target, parts: list[str], directory: Path) -> dict[str, Path]:
    files = {}
    for part in parts:
        path = directory / f"{target.ci_prefix}-{part}.bin"
        if not path.exists():
            die(f"missing {path}")
        files[part] = path
    return files


def remote_files(target: Target, parts: list[str], base_url: str, cache_dir: Path, fresh: bool) -> dict[str, Path]:
    return {part: fetch(f"{base_url}/{target.ci_prefix}-{part}.bin", cache_dir / f"{target.ci_prefix}-{part}.bin", fresh) for part in parts}


def collect_files(args, target: Target, parts: list[str]) -> tuple[dict[str, Path], str]:
    repo = args.repo or default_repo()
    if args.source == "local":
        return local_files(target, parts), f"local build (.pio/build/{target.env})"
    if args.source == "dir":
        if not args.dir:
            die("--source dir needs --dir")
        return dir_files(target, parts, Path(args.dir)), f"directory {args.dir}"
    if args.source == "pr":
        if not args.pr:
            die("--source pr needs --pr NUMBER")
        owner, name = repo.split("/", 1)
        base = f"https://{owner}.github.io/{name}/pr-{args.pr}"
        return remote_files(target, parts, base, CACHE / repo / f"pr-{args.pr}", fresh=True), f"PR #{args.pr} preview ({repo})"
    if args.source == "nightly":
        base = f"https://github.com/{repo}/releases/download/nightly"
        return remote_files(target, parts, base, CACHE / repo / "nightly", fresh=True), f"nightly ({repo})"
    if args.source == "release":
        tag = args.version or latest_release_tag(repo)
        base = f"https://github.com/{repo}/releases/download/{tag}"
        return remote_files(target, parts, base, CACHE / repo / tag, fresh=False), f"release {tag} ({repo})"
    die(f"unknown source {args.source}")


# --------------------------------------------------------------------------- flashing


def find_partition(partitions_bin: Path, ptype: int, subtype: int) -> tuple[int, int] | None:
    """Return (offset, size) of the first partition with this type and subtype."""
    data = partitions_bin.read_bytes()
    for i in range(0, len(data) - 32, 32):
        magic, t, st, offset, size = struct.unpack_from("<HBBII", data, i)
        if magic != 0x50AA:
            break
        if t == ptype and st == subtype:
            return offset, size
    return None


def filesystem_offset(partitions_bin: Path) -> int:
    """Return the offset of the data/spiffs partition (LittleFS uses the spiffs subtype)."""
    found = find_partition(partitions_bin, 0x01, 0x82)
    if found is None:
        die(f"no spiffs/littlefs partition in {partitions_bin}")
    return found[0]


# Both boards use the stock Arduino layouts, where otadata sits at 0xe000 with size 0x2000.
DEFAULT_OTADATA = (0xE000, 0x2000)


def blank_otadata(partitions_bin: Path | None) -> tuple[int, Path]:
    """Return (offset, file) for an all-0xFF otadata image.

    The app is always written to app0. After an OTA the boot selector points at app1, so the
    bootloader would keep booting the old OTA image. A blank otadata makes it boot app0 again,
    which is what PlatformIO does with boot_app0.bin.
    """
    found = find_partition(partitions_bin, 0x01, 0x00) if partitions_bin else None
    offset, size = found or DEFAULT_OTADATA
    path = CACHE / f"otadata-blank-{size:#x}.bin"
    if not path.exists() or path.stat().st_size != size:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff" * size)
    return offset, path


def esptool_cmd(port: str, baud: int, *rest: str) -> list[str]:
    return [sys.executable, "-m", "esptool", "--chip", "esp32s3", "--port", port, "--baud", str(baud), *rest]


def run(cmd: list[str], dry_run: bool) -> None:
    print("  $ " + " ".join(cmd))
    if dry_run:
        return
    result = subprocess.run(cmd)
    if result.returncode != 0:
        die("esptool failed; if it could not connect, hold BOOT while plugging the board in and retry", result.returncode)


def cmd_flash(args) -> int:
    target = TARGETS[args.target]
    if args.app_only:
        parts = ["firmware"]
    elif args.full:
        parts = list(PARTS)
    else:
        parts = ["bootloader", "partitions", "firmware"]

    files, label = collect_files(args, target, parts)
    print(f"Target : {target.name}\nSource : {label}")
    for part in parts:
        print(f"  {part:10} {files[part]}  ({files[part].stat().st_size // 1024} KiB)")
    if "filesystem" in parts or args.erase:
        print("Note   : this wipes the LittleFS data (profiles, settings on the display).")

    port = args.port if args.dry_run and args.port else resolve_port(args.port)
    print(f"Port   : {port}")

    if args.erase:
        run(esptool_cmd(port, args.baud, "erase-flash"), args.dry_run)

    layout = []
    if "bootloader" in parts:
        layout += [hex(BOOTLOADER_OFFSET), str(files["bootloader"])]
    if "partitions" in parts:
        layout += [hex(PARTITIONS_OFFSET), str(files["partitions"])]
    if "firmware" in parts:
        otadata_offset, otadata = blank_otadata(files.get("partitions"))
        layout += [hex(otadata_offset), str(otadata)]
        layout += [hex(APP_OFFSET), str(files["firmware"])]
    if "filesystem" in parts:
        layout += [hex(filesystem_offset(files["partitions"])), str(files["filesystem"])]
    run(esptool_cmd(port, args.baud, "write-flash", *layout), args.dry_run)
    if not args.dry_run:
        print("Done. The board resets now.")
    return 0


# --------------------------------------------------------------------------- monitor


def cmd_monitor(args) -> int:
    port = resolve_port(args.port)
    print(f"Monitoring {port} at {args.baud} baud" + (f" for {args.seconds}s" if args.seconds else ", Ctrl-C to stop"))
    with serial.Serial(port, args.baud, timeout=0.2) as ser:
        if args.reset:
            ser.dtr = False
            ser.rts = True
            time.sleep(0.1)
            ser.rts = False
        end = time.monotonic() + args.seconds if args.seconds else None
        try:
            while end is None or time.monotonic() < end:
                line = ser.readline()
                if line:
                    sys.stdout.write(line.decode("utf-8", errors="replace"))
                    sys.stdout.flush()
        except KeyboardInterrupt:
            pass
    return 0



# --------------------------------------------------------------------------- ota

OTA_PHASES = {0: "idle", 1: "display firmware", 2: "display filesystem", 3: "controller firmware", 4: "finished", 5: "failed"}
OTA_FINISHED, OTA_FAILED = 4, 5


async def ws_recv_json(ws, timeout: float):
    """Return the next JSON message within timeout, or None."""
    import websockets

    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    except (asyncio.TimeoutError, websockets.ConnectionClosed):
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {}


async def ota_status(ws, request: dict, settle: float) -> dict | None:
    """Send an ota-settings request and return the last res:ota-settings seen within settle seconds."""
    await ws.send(json.dumps(request))
    status = None
    end = time.monotonic() + settle
    while (remaining := end - time.monotonic()) > 0:
        msg = await ws_recv_json(ws, remaining)
        if msg is None:
            break
        if msg.get("tp") == "res:ota-settings":
            status = msg
    return status


def print_ota_status(st: dict) -> None:
    print(f"  channel    {st.get('channel')}\n  latest     {st.get('latestVersion') or '(unknown)'}")
    print(f"  display    {st.get('displayVersion')}  update available: {st.get('displayUpdateAvailable')}")
    print(f"  controller {st.get('controllerVersion')}  update available: {st.get('controllerUpdateAvailable')}")


async def ota_run(args) -> int:
    import websockets

    url = f"ws://{args.host}/ws"
    print(f"Connecting to {url}")
    async with websockets.connect(url, max_size=None, open_timeout=15) as ws:
        print(f"Switching the update channel to {args.channel} and checking for updates ...")
        status = await ota_status(ws, {"tp": "req:ota-settings", "update": True, "channel": args.channel}, settle=3)
        # The display runs the (blocking, TLS) check in its main loop and broadcasts a fresh
        # status when done; poll until the latest version is known or the deadline passes.
        deadline = time.monotonic() + args.check_timeout
        while time.monotonic() < deadline and not (status and status.get("latestVersion")):
            status = await ota_status(ws, {"tp": "req:ota-settings"}, settle=5)
        if not status:
            die("no res:ota-settings received; is this a GaggiMate display with the web UI enabled?")
        print_ota_status(status)
        if status.get("updating"):
            die("the display reports an update already in progress")

        want_display = args.component in ("both", "display")
        want_controller = args.component in ("both", "controller")
        todo = []
        if want_controller and status.get("controllerUpdateAvailable"):
            todo.append("controller")
        if want_display and status.get("displayUpdateAvailable"):
            todo.append("display")
        if not todo:
            print("Nothing to update: the display does not offer a newer version on this channel.")
            return 0
        if args.dry_run:
            print(f"Dry run: would update {', '.join(todo)}.")
            return 0

        request = {"tp": "req:ota-start"}
        if args.component != "both":
            request["cp"] = args.component
        print(f"Starting OTA for {', '.join(todo)} (controller first over BLE, then display over WiFi)")
        await ws.send(json.dumps(request))

        last = None
        deadline = time.monotonic() + args.update_timeout
        while time.monotonic() < deadline:
            msg = await ws_recv_json(ws, 30)
            if msg is None:
                if ws.state.name != "OPEN" if hasattr(ws, "state") else False:
                    break
                continue
            if msg.get("tp") != "evt:ota-progress":
                continue
            phase, progress = msg.get("phase", 0), msg.get("progress", 0)
            if (phase, progress) != last:
                print(f"  {OTA_PHASES.get(phase, phase):20} {progress:3}%")
                last = (phase, progress)
            if phase == OTA_FAILED:
                die("the display reported the update as failed; see its serial log")
            if phase == OTA_FINISHED:
                print("Update finished. The display reboots if it flashed itself.")
                return 0
        if last and last[0] == 1 and last[1] >= 99:
            print("Connection closed after the display firmware was written; it is rebooting.")
            return 0
        die("lost the connection or timed out before the update finished")


def cmd_ota(args) -> int:
    try:
        return asyncio.run(ota_run(args))
    except OSError as e:
        die(f"cannot reach {args.host}: {e}")


# --------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ports", help="list serial ports and mark ESP32-S3 boards")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_ports)

    f = sub.add_parser("flash", help="flash a target")
    f.add_argument("target", choices=sorted(TARGETS))
    f.add_argument("-p", "--port", help="serial port; auto-detected when exactly one ESP32-S3 is attached")
    f.add_argument("-b", "--baud", type=int, default=921600)
    f.add_argument("--source", choices=["local", "release", "nightly", "pr", "dir"], default="local",
                   help="local = .pio/build, release = GitHub release (--version, default latest), nightly, pr = PR preview (--pr), dir = folder with CI-named files")
    f.add_argument("--version", help="release tag for --source release, e.g. v1.9.0")
    f.add_argument("--pr", type=int, help="PR number for --source pr")
    f.add_argument("--dir", help="folder for --source dir (e.g. out/ or a downloaded release)")
    f.add_argument("--repo", help="GitHub owner/repo that hosts the releases and PR previews (default jniebuhr/gaggimate; env GAGGIMATE_REPO)")
    mode = f.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true", help="also write the LittleFS image (wipes profiles/settings)")
    mode.add_argument("--app-only", action="store_true", help="write only firmware.bin")
    f.add_argument("--erase", action="store_true", help="erase the whole flash first")
    f.add_argument("--dry-run", action="store_true", help="fetch files and print the esptool command without flashing")
    f.set_defaults(fn=cmd_flash)

    m = sub.add_parser("monitor", help="print serial output")
    m.add_argument("-p", "--port")
    m.add_argument("-b", "--baud", type=int, default=115200)
    m.add_argument("--seconds", type=float, default=0, help="stop after N seconds (default: run until Ctrl-C)")
    m.add_argument("--reset", action="store_true", help="pulse RTS to reset the board before reading")
    m.set_defaults(fn=cmd_monitor)

    o = sub.add_parser("ota", help="update controller and/or display through the display's own OTA (needs WiFi)")
    o.add_argument("--host", required=True, help="display IP or hostname")
    o.add_argument("--channel", choices=["latest", "nightly"], default="latest", help="release channel (latest = stable)")
    o.add_argument("--component", choices=["both", "controller", "display"], default="both")
    o.add_argument("--check-timeout", type=float, default=90, help="seconds to wait for the update check")
    o.add_argument("--update-timeout", type=float, default=1200, help="seconds to wait for the update to finish")
    o.add_argument("--dry-run", action="store_true", help="only report what the display would update")
    o.set_defaults(fn=cmd_ota)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
