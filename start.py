#!/usr/bin/env python3
"""Find the phone (USB or Ethernet), check it's ready, and start the live tracker.

  ./start.sh                       # auto: network phone, else USB, else last/Internet-Sharing IP
  ./start.sh --ip 192.168.3.2      # a specific network address
  ./start.sh --usb                 # force USB
  ./start.sh --enable-network      # phone on USB: switch adb to network mode for Ethernet use
  ./start.sh --replay clip.mp4     # no phone: replay a recording through the tracker

Anything else is passed to live.py (e.g. --min-mph 5, --no-preview, --port 9000).
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from tracker.scrcpy_client import DEFAULT_SCRCPY_DIR  # noqa: E402

ADB = str(DEFAULT_SCRCPY_DIR / "adb")
PORT = 5555
LAST_IP = ROOT / ".last_phone_ip"
LEASES = Path("/var/db/dhcpd_leases")   # macOS Internet Sharing


def say(msg: str):
    print(f"\033[1m{msg}\033[0m", flush=True)


def warn(msg: str):
    print(f"\033[33m! {msg}\033[0m", flush=True)


def die(msg: str):
    print(f"\n\033[31m✗ {msg}\033[0m", flush=True)
    sys.exit(1)


def adb(*args, serial=None, timeout=10) -> subprocess.CompletedProcess:
    cmd = [ADB] + (["-s", serial] if serial else []) + list(args)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 1, "", "timed out")


def devices() -> list[tuple[str, str]]:
    out = adb("devices").stdout.splitlines()[1:]
    return [tuple(l.split("\t")[:2]) for l in out if "\t" in l]


def reachable(ip: str) -> bool:
    flag = "-t" if sys.platform == "darwin" else "-w"
    return subprocess.run(["ping", "-c", "1", flag, "1", ip], capture_output=True).returncode == 0


def try_connect(ip: str) -> str | None:
    target = f"{ip}:{PORT}"
    if not reachable(ip):
        return None
    r = adb("connect", target)
    if "connected" not in r.stdout or "cannot" in r.stdout:
        return None
    if adb("shell", "echo ok", serial=target).stdout.strip() == "ok":
        return target
    return None


def lease_ips() -> list[str]:
    """IPs handed out by macOS Internet Sharing, newest first."""
    try:
        text = LEASES.read_text()
    except OSError:
        return []
    return list(reversed(re.findall(r"ip_address=([\d.]+)", text)))


def find_phone(args) -> str:
    if args.ip:
        s = try_connect(args.ip)
        if not s:
            die(f"can't reach adb at {args.ip}:{PORT}. Is the cable in, Internet Sharing on, "
                f"and adb network mode enabled (./start.sh --enable-network while on USB)?")
        return s

    devs = devices()
    for serial, state in devs:
        if state == "unauthorized":
            die("the phone is asking for permission. Unlock it, tick 'Always allow from this "
                "computer', tap Allow, then run ./start.sh again.")
    usb = [s for s, st in devs if st == "device" and ":" not in s]
    net = [s for s, st in devs if st == "device" and ":" in s]

    if args.usb:
        if not usb:
            die("no phone on USB. Check the cable and that USB debugging is on.")
        return usb[0]
    if net:
        return net[0]
    if usb:
        return usb[0]

    candidates = []
    if LAST_IP.exists():
        candidates.append(LAST_IP.read_text().strip())
    candidates += [ip for ip in lease_ips() if ip not in candidates]
    if candidates:
        say(f"Looking for the phone on the network: {', '.join(candidates[:5])}")
    for ip in candidates[:5]:
        s = try_connect(ip)
        if s:
            return s
    die("no phone found.\n"
        "  • USB: plug it in, unlock it, and make sure USB debugging is on.\n"
        "  • Ethernet: Internet Sharing must be on for your USB Ethernet adapter; if the phone\n"
        "    has no address yet, unplug and replug the cable at the phone end.\n"
        "    After a phone restart, run ./start.sh --enable-network once over USB.")


def enable_network():
    usb = [s for s, st in devices() if st == "device" and ":" not in s]
    if not usb:
        die("plug the phone in by USB first (this is needed once after each phone restart).")
    r = adb("tcpip", str(PORT), serial=usb[0])
    if r.returncode != 0:
        die(f"adb tcpip failed: {(r.stderr or r.stdout).strip()}")
    say(f"adb network mode is on (port {PORT}) until the phone restarts.")
    print("Now unplug the USB cable, connect the Ethernet adapter and cable, and run ./start.sh")


def check_phone(serial: str):
    out = adb("shell", "dumpsys power | grep -m1 mWakefulness=; "
                       "dumpsys window | grep -m1 isKeyguardShowing; "
                       "settings get global stay_on_while_plugged_in; "
                       "getprop ro.product.model", serial=serial).stdout
    model = out.strip().splitlines()[-1] if out.strip() else "phone"
    say(f"Phone: {model} via {'network ' + serial if ':' in serial else 'USB'}")

    stay = re.search(r"^\s*(\d+)\s*$", out, re.M)
    if stay and stay.group(1) == "0":
        warn("Stay awake is off. Turn it on (Settings → System → Developer options → Stay awake), "
             "or the phone will lock and Face Unlock will block the camera.")

    deadline = time.monotonic() + 120
    told = False
    while True:
        out = adb("shell", "dumpsys power | grep -m1 mWakefulness=; "
                           "dumpsys window | grep -m1 isKeyguardShowing", serial=serial).stdout
        awake = "mWakefulness=Awake" in out
        locked = "isKeyguardShowing=true" in out
        if awake and not locked:
            return
        if not told:
            warn("Unlock the phone and leave it on the home screen (waiting up to 2 minutes)...")
            told = True
        if time.monotonic() > deadline:
            die("the phone stayed locked. Unlock it and run ./start.sh again.")
        time.sleep(1.5)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", help="phone's network address (Ethernet/Wi-Fi)")
    ap.add_argument("--usb", action="store_true", help="use the USB connection")
    ap.add_argument("--enable-network", action="store_true",
                    help="phone on USB: turn on adb network mode, then exit")
    ap.add_argument("--replay", metavar="VIDEO", help="replay a recorded clip instead of the phone")
    ap.add_argument("--no-open", action="store_true", help="don't open the display in the browser")
    args, extra = ap.parse_known_args()

    if not (DEFAULT_SCRCPY_DIR / "adb").exists():
        die("scrcpy/adb not installed. Run ./setup.sh first.")
    live = [sys.executable, str(ROOT / "live.py")] + ([] if args.no_open else ["--open"])

    if args.replay:
        os.execv(sys.executable, live + ["--file", args.replay] + extra)
    if args.enable_network:
        enable_network()
        return

    serial = find_phone(args)
    if ":" in serial:
        LAST_IP.write_text(serial.split(":")[0])
    check_phone(serial)
    say("Starting the tracker. Press Ctrl+C to stop.\n")
    os.execv(sys.executable, live + ["--serial", serial] + extra)


if __name__ == "__main__":
    main()
