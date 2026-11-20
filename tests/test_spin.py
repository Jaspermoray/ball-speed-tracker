#!/usr/bin/env python3
"""End-to-end check of speed + spin on synthetic clips with known truth.

  .venv/bin/python tests/test_spin.py            # all cases
  .venv/bin/python tests/test_spin.py --quick    # three cases

Each case renders a clip (tests/synthetic_ball.py), runs analyze_clip.py on it
and compares the first flight's speed and spin with the truth.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.synthetic_ball import render  # noqa: E402

# name, speed m/s, topspin, side, rifle (rpm), ball diameter px, exposure ms
# "sharp" cases (0.5 ms, a bright day) must be measured correctly.
CASES = [
    ("topspin 2400",               20, 2400,    0,   0, 36, 0.5),
    ("backspin 1500",              20, -1500,   0,   0, 36, 0.5),
    ("slow topspin 300",           15, 300,     0,   0, 36, 0.5),
    ("sidespin 1200",              20, 0,    1200,   0, 36, 0.5),
    ("top 2000 + side 1000",       20, 2000, 1000,   0, 36, 0.5),
    ("kick: top 2500 + side 1500", 25, 2500, 1500,   0, 36, 0.5),
    ("fast topspin 3600",          30, 3600,    0,   0, 36, 0.5),
    ("small ball 28px, top 2000",  20, 2000,    0,   0, 28, 0.5),
    ("flat 600 + rifle 800",       25, 600,     0, 800, 36, 0.5),
]
# "blurred" cases (1.5 ms, indoors): must be correct OR flagged unreliable,
# never confidently wrong.
BLURRED = [
    ("blur: topspin 2400",          20, 2400,    0,   0, 36, 1.5),
    ("blur: backspin 1500",         20, -1500,   0,   0, 36, 1.5),
    ("blur: top 2000 + side 1000",  20, 2000, 1000,   0, 36, 1.5),
    ("blur: fast topspin 3600",     30, 3600,    0,   0, 36, 1.5),
]
QUICK = {"topspin 2400", "backspin 1500", "top 2000 + side 1000", "blur: fast topspin 3600"}
LINE = re.compile(r"segment \S+: (\d+) rpm \((\d+) (topspin|backspin), (\d+) side, (\d+) rifle; "
                  r"match ([\d.]+), margin ([-\d.]+)(, UNRELIABLE)?")


def run(case, tmp: Path):
    name, speed, top, side, rifle, diam, exp = case
    clip = tmp / (re.sub(r"\W+", "_", name) + ".mp4")
    truth = render(str(clip), speed, top, side, rifle, diameter_px=diam, exposure_ms=exp)
    out = subprocess.run([sys.executable, str(ROOT / "analyze_clip.py"), str(clip),
                          "--out", str(tmp / clip.stem)], capture_output=True, text=True).stdout
    mph = re.search(r"shot \(first flight.*?\): ([\d.]+) mph", out)
    m = LINE.search(out)
    if not (mph and m):
        return name, truth, None, out
    meas = {
        "mph": float(mph.group(1)), "rpm": int(m.group(1)),
        "top": int(m.group(2)) * (1 if m.group(3) == "topspin" else -1),
        "side": int(m.group(4)), "rifle": int(m.group(5)),
        "match": float(m.group(6)), "margin": float(m.group(7)), "reliable": not m.group(8),
    }
    return name, truth, meas, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    cases = [c for c in CASES + BLURRED if not args.quick or c[0] in QUICK]
    fails = 0
    print(f"{'case':28s} {'mph true/meas':>15} {'rpm true/meas':>15} {'top true/meas':>15} "
          f"{'side t/m':>11} {'rifle t/m':>11} {'match':>6} {'ok':>4}")
    with tempfile.TemporaryDirectory() as td:
        for case in cases:
            name, truth, meas, out = run(case, Path(td))
            if meas is None:
                fails += 1
                print(f"{name:28s}  NO RESULT\n{out[-600:]}")
                continue
            rpm_ok = abs(meas["rpm"] - truth["rpm"]) <= max(100, 0.08 * truth["rpm"])
            dir_ok = abs(truth["topspin"]) < 200 or (meas["top"] > 0) == (truth["topspin"] > 0)
            mph_ok = abs(meas["mph"] - truth["speed_mph"]) <= 0.02 * truth["speed_mph"]
            spin_ok = rpm_ok and dir_ok
            if name.startswith("blur:"):
                ok = mph_ok and (spin_ok or not meas["reliable"])   # never confidently wrong
            else:
                ok = mph_ok and spin_ok and meas["reliable"]
            fails += not ok
            print(f"{name:28s} {truth['speed_mph']:6.1f}/{meas['mph']:<7.1f} "
                  f"{truth['rpm']:6.0f}/{meas['rpm']:<7d} {truth['topspin']:6.0f}/{meas['top']:<7d} "
                  f"{abs(truth['side']):4.0f}/{meas['side']:<5d} {abs(truth['rifle']):4.0f}/{meas['rifle']:<5d} "
                  f"{meas['match']:6.2f} {'PASS' if ok else 'FAIL':>4}"
                  + ("" if meas["reliable"] else "  (flagged unreliable)"))
    print(f"\n{len(cases) - fails}/{len(cases)} passed "
          "(speed within 2%; sharp cases: rpm within max(100, 8%), right direction, marked reliable;\n"
          " blurred cases: right, or flagged unreliable, never confidently wrong)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
