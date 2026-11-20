#!/usr/bin/env python3
"""Run the big-number display on its own (for testing, or before the tracker
publishes to it directly).

  .venv/bin/python display_server.py --open          # serve + open the browser
  .venv/bin/python display_server.py --demo 4 --open # fake shot every 4 s

Then push readings with send_shot.py, or POST JSON to /shot.
In the page: F full screen, U mph/km/h, D local demo reading, H hide footer, ? help.
"""
import argparse
import random
import socket
import time
import webbrowser

from tracker.display import DisplayServer


def lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packets sent; picks the LAN interface
            return s.getsockname()[0]
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true", help="open the display in the default browser")
    ap.add_argument("--demo", type=float, metavar="SEC", help="publish a fake shot every SEC seconds")
    args = ap.parse_args()

    disp = DisplayServer(port=args.port).start()
    url = f"http://localhost:{args.port}/"
    print(f"display: {url}")
    ip = lan_ip()
    if ip:
        print(f"other devices on this network: http://{ip}:{args.port}/")
    if args.open:
        webbrowser.open(url)

    try:
        while True:
            if args.demo:
                time.sleep(args.demo)
                mph = random.uniform(70, 130)
                rpm = random.uniform(800, 4000) if random.random() < 0.85 else None
                shot = disp.publish(speed_mps=mph / 2.2369363, rpm=rpm, speed_err_mps=0.3,
                                    rpm_err=120 if rpm else None)
                print(f"demo shot {shot['id']}: {mph:.0f} mph, {rpm and round(rpm)} rpm")
            else:
                time.sleep(3600)
    except KeyboardInterrupt:
        disp.stop()


if __name__ == "__main__":
    main()
