#!/usr/bin/env python3
"""Set up adb over a network link (Ethernet hub, USB tethering, or Wi-Fi) and
check it, so receiver.py can run with --serial <ip>:5555.

With the phone on USB (first time, or after a phone reboot):
  .venv/bin/python link_setup.py              # Ethernet (eth*) only
  .venv/bin/python link_setup.py --iface ncm0  # other links: USB tethering, wlan0, ...

Phone already in network mode, no USB cable:
  .venv/bin/python link_setup.py --ip 192.168.2.2
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time

from tracker.scrcpy_client import DEFAULT_SCRCPY_DIR

ADB = str(DEFAULT_SCRCPY_DIR / "adb")
PORT = 5555
ETH = r"eth\d+"


def adb(*args, serial=None, timeout=15) -> subprocess.CompletedProcess:
    cmd = [ADB] + (["-s", serial] if serial else []) + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def devices() -> list[tuple[str, str]]:
    out = adb("devices").stdout.splitlines()[1:]
    return [tuple(l.split("\t")[:2]) for l in out if "\t" in l]


def phone_interfaces(serial: str) -> dict[str, dict]:
    """{iface: {"up": bool, "v4": [...], "v6ll": [...]}} from `ip -br addr`."""
    out = adb("shell", "ip -br addr", serial=serial).stdout
    res = {}
    for line in out.splitlines():
        f = line.split()
        if len(f) < 2:
            continue
        name = f[0].split("@")[0]
        addrs = f[2:]
        res[name] = {
            "up": f[1] == "UP",
            "v4": [a.split("/")[0] for a in addrs if re.match(r"\d+\.\d+\.\d+\.\d+/", a)],
            "v6ll": [a.split("/")[0] for a in addrs if a.startswith("fe80:")],
        }
    return res


def pick_iface(ifaces: dict, wanted: str | None) -> str | None:
    if wanted:
        return wanted if wanted in ifaces else None
    for name, info in ifaces.items():
        if re.fullmatch(ETH, name) and info["v4"]:
            return name
    return None


def ping(ip: str) -> str | None:
    r = subprocess.run(["ping", "-c", "5", "-i", "0.2", "-t", "5", ip], capture_output=True, text=True)
    m = re.search(r"([\d.]+) packets received.*?\n.*?= ([\d.]+)/([\d.]+)/([\d.]+)", r.stdout, re.S)
    loss = re.search(r"([\d.]+)% packet loss", r.stdout)
    if not loss or float(loss.group(1)) >= 100:
        return None
    rtt = re.search(r"= [\d.]+/([\d.]+)/", r.stdout)
    return f"{loss.group(1)}% loss, avg {rtt.group(1) if rtt else '?'} ms"


def mac_route(ip: str) -> str:
    r = subprocess.run(["route", "-n", "get", ip], capture_output=True, text=True).stdout
    m = re.search(r"interface: (\S+)", r)
    return m.group(1) if m else "?"


def fail(msg: str):
    print(f"\nFAILED: {msg}")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--iface", help="phone interface to use (e.g. eth0)")
    ap.add_argument("--ip", help="skip USB; connect to this phone IP directly")
    args = ap.parse_args()

    ip = args.ip
    if not ip:
        usb = [s for s, st in devices() if st == "device" and ":" not in s]
        if not usb:
            fail("no phone on USB. Plug it in (needed once per phone reboot), or pass --ip.")
        serial = usb[0]
        print(f"phone on USB: {serial}")

        ifaces = phone_interfaces(serial)
        for name, info in sorted(ifaces.items()):
            if re.fullmatch(r"(eth|ncm|rndis|usb|wlan)\d+", name):
                print(f"  {name:6s} {'UP  ' if info['up'] else 'down'}  "
                      f"{', '.join(info['v4']) or '(no IPv4)'}")
        name = pick_iface(ifaces, args.iface)
        if args.iface and not ifaces.get(name or "", {}).get("v4"):
            fail(f"{args.iface} not found or has no IPv4 address.")
        if not name:
            eth = {n: i for n, i in ifaces.items() if n.startswith("eth")}
            if not eth:
                fail("phone sees no Ethernet interface. Check the hub is plugged in and powered "
                     "(power bank into the hub first), and the cable's link lights are on.")
            n, i = next(iter(eth.items()))
            hint = (f" It has IPv6 link-local {i['v6ll'][0]} only, so the link is up but nothing "
                    "gave it an address: turn on Mac Internet Sharing to your USB Ethernet adapter "
                    "(or use a router), then unplug/replug the cable." if i["v6ll"] else
                    " Is the cable plugged in at both ends?")
            fail(f"{n} has no IPv4 address.{hint}")
        ip = ifaces[name]["v4"][0]
        print(f"using {name} -> {ip}")

        print(f"switching adb to network mode on port {PORT} ...")
        r = adb("tcpip", str(PORT), serial=serial)
        if r.returncode != 0:
            fail(f"adb tcpip failed: {(r.stderr or r.stdout).strip()}")
        time.sleep(2.0)

    print(f"Mac route to {ip}: via {mac_route(ip)}")
    p = ping(ip)
    print(f"ping: {p or 'NO REPLY'}")
    if not p:
        fail("the Mac can't reach the phone. If on Wi-Fi, the network may isolate clients; "
             "on Ethernet, check Internet Sharing is on for the right adapter.")

    target = f"{ip}:{PORT}"
    adb("disconnect", target)
    for attempt in range(5):
        r = adb("connect", target)
        if "connected" in r.stdout and "cannot" not in r.stdout:
            break
        time.sleep(1.0)
    else:
        fail(f"adb connect {target}: {(r.stdout + r.stderr).strip()}")

    r = adb("shell", "echo ok", serial=target, timeout=10)
    if r.stdout.strip() != "ok":
        fail(f"connected, but commands don't get through: {(r.stderr or r.stdout).strip()}")
    model = adb("shell", "getprop ro.product.model", serial=target).stdout.strip()
    print(f"\nOK: adb over network to {model} at {target}")
    print(f"next: .venv/bin/python receiver.py --serial {target} --duration 600")


if __name__ == "__main__":
    main()
