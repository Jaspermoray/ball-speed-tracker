#!/usr/bin/env python3
"""Push one reading to a running display.

  .venv/bin/python send_shot.py --mph 112 --rpm 2850
  .venv/bin/python send_shot.py --mps 50.1
"""
import argparse
import json
import urllib.request


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--mph", type=float)
    g.add_argument("--kmh", type=float)
    g.add_argument("--mps", type=float)
    ap.add_argument("--rpm", type=float)
    ap.add_argument("--url", default="http://localhost:8765")
    args = ap.parse_args()

    mps = args.mps if args.mps is not None else (
        args.mph / 2.2369363 if args.mph is not None else args.kmh / 3.6)
    body = json.dumps({"speed_mps": mps, "rpm": args.rpm}).encode()
    req = urllib.request.Request(args.url.rstrip("/") + "/shot", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        print(r.read().decode())


if __name__ == "__main__":
    main()
