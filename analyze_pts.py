#!/usr/bin/env python3
"""Analyze per-frame timestamps of a recorded video (step 2 of the handoff).

Usage: .venv/bin/python analyze_pts.py test.mp4 [--csv pts.csv]
       .venv/bin/python analyze_pts.py recordings/<timestamp>/

Reports frame count, effective fps, gap statistics and outliers, using the
container's packet PTS (never an assumed frame rate).
"""
import argparse
import csv
import sys
from fractions import Fraction
from pathlib import Path

import av
import numpy as np


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--csv", help="write pts_seconds per packet to this file")
    args = ap.parse_args()

    if Path(args.video).is_dir():  # a receiver.py recording directory
        with open(Path(args.video) / "packets.csv", newline="") as f:
            pts = [int(r["pts_us"]) for r in csv.DictReader(f) if r["config"] == "0"]
        tb = Fraction(1, 1_000_000)
        print(f"receiver recording, time_base={tb}")
    else:
        with av.open(args.video) as c:
            s = c.streams.video[0]
            tb = s.time_base
            print(f"codec={s.codec_context.name} size={s.codec_context.width}x{s.codec_context.height} "
                  f"time_base={tb} declared_rate={s.average_rate}")
            pts = [p.pts for p in c.demux(s) if p.pts is not None and p.size > 0]

    if len(pts) < 3:
        sys.exit(f"only {len(pts)} packets with PTS; recording may be truncated")

    t = np.sort(np.array(pts, dtype=np.float64) * float(tb))
    t -= t[0]
    if args.csv:
        np.savetxt(args.csv, t, fmt="%.6f")

    g = np.diff(t) * 1000.0  # ms
    dur = t[-1]
    print(f"frames={len(t)} duration={dur:.3f}s effective_fps={(len(t) - 1) / dur:.2f}")
    print(f"timestamp resolution ~ {float(tb) * 1000:.6f} ms")
    print(f"gap ms: median={np.median(g):.3f} mean={g.mean():.3f} std={g.std():.3f} "
          f"min={g.min():.3f} max={g.max():.3f}")
    for q in (1, 5, 50, 95, 99):
        print(f"  p{q:<2} = {np.percentile(g, q):.3f} ms")
    for thr in (6.0, 8.5, 12.5):
        n = int((g > thr).sum())
        print(f"gaps > {thr} ms: {n} ({100 * n / len(g):.2f}%)")

    med = np.median(g)
    ok = abs(med - 1000 / 240) < 0.5 and (g > 6.0).mean() < 0.01
    print("\nPASS: looks like stable 240 fps" if ok else
          f"\nNOT 240-stable (median {med:.2f} ms ≈ {1000 / med:.0f} fps)")


if __name__ == "__main__":
    main()
