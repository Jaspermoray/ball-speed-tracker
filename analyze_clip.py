#!/usr/bin/env python3
"""Find the ball in a video clip and measure its speed.

  .venv/bin/python analyze_clip.py PXL_20261120_161336298.mp4
  .venv/bin/python analyze_clip.py clip.mp4 --hfov 66 --publish   # also send to the display

Timing: always from the file's frame timestamps. Phone slow-motion files
store 120/240 fps footage with 30 fps timestamps; the Android metadata key
com.android.capture.fps is used to convert back to real time.

Outputs go to analysis/<clip name>/: detections.csv, overlay.jpg, crops.jpg.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import urllib.request
from pathlib import Path

import tracker  # noqa: F401  (first: loads PyAV + OpenCV quietly on macOS)
import av
import cv2
import numpy as np

from tracker.ball import (BALL_DIAMETER_M, Camera, background_median, clean_segment,
                          drop_partials, segment_speed, split_segments, track)

MPH = 2.2369363


def open_clip(path: str):
    c = av.open(path)
    s = c.streams.video[0]
    return c, s


def time_scale(path: str, override_capture_fps: float | None) -> tuple[float, str]:
    """Factor that converts file time to real time."""
    c, s = open_clip(path)
    meta = {**c.metadata, **s.metadata}
    tb = float(s.time_base)
    pts = [p.pts for p in c.demux(s) if p.pts is not None and p.size > 0]
    c.close()
    pts = np.sort(np.array(pts, float)) * tb
    file_fps = (len(pts) - 1) / (pts[-1] - pts[0])
    cap = override_capture_fps or (float(meta["com.android.capture.fps"])
                                   if "com.android.capture.fps" in meta else None)
    if cap and cap > file_fps * 1.5:
        return file_fps / cap, f"slow-motion file: {file_fps:.2f} fps timestamps, captured at {cap:g} fps"
    return 1.0, f"real-time file: {file_fps:.2f} fps"


def frames(path: str, scale: float, step: int = 1):
    c, s = open_clip(path)
    s.thread_type = "AUTO"
    t0 = None
    for i, f in enumerate(c.decode(s)):
        if i % step:
            continue
        t = float(f.pts * s.time_base)
        t0 = t if t0 is None else t0
        yield i, (t - t0) * scale, f.to_ndarray(format="bgr24")
    c.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--hfov", type=float, default=66.0,
                    help="camera horizontal field of view in degrees (assumed; affects 3D only)")
    ap.add_argument("--capture-fps", type=float, help="override real capture rate")
    ap.add_argument("--out", help="output folder (default analysis/<clip>)")
    ap.add_argument("--publish", nargs="?", const="http://localhost:8765",
                    help="send the shot speed to the display server")
    args = ap.parse_args()

    scale, desc = time_scale(args.video, args.capture_fps)
    print(desc)
    out = Path(args.out or Path("analysis") / Path(args.video).stem)
    out.mkdir(parents=True, exist_ok=True)

    bg = background_median([f for _, _, f in frames(args.video, scale, step=6)], step=1)
    tstats: dict = {}
    dets = track(frames(args.video, scale), bg, stats=tstats)
    if tstats.get("camera_motion_frames"):
        print(f"skipped {tstats['camera_motion_frames']} frames where the camera itself moved")
    h, w = bg.shape[:2]
    cam = Camera(w, h, args.hfov)
    print(f"ball found in {len(dets)} frames"
          + (f" ({dets[0].frame}-{dets[-1].frame})" if dets else ""))
    if len(dets) < 5:
        print("too few detections to measure speed")
        return

    with open(out / "detections.csv", "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["frame", "t_s", "x_px", "y_px", "d_minor_px", "d_major_px", "angle_deg",
                     "X_m", "Y_m", "Z_m"])
        for d in dets:
            X, Y, Z = cam.to_3d(d)
            wr.writerow([d.frame, f"{d.t:.6f}", f"{d.x:.2f}", f"{d.y:.2f}", f"{d.d_minor:.2f}",
                         f"{d.d_major:.2f}", f"{d.angle:.1f}", f"{X:.4f}", f"{Y:.4f}", f"{Z:.4f}"])

    dt = np.diff([d.t for d in dets])
    print(f"real frame interval: median {np.median(dt) * 1000:.2f} ms "
          f"(≈{1 / np.median(dt):.0f} fps)")
    whole = drop_partials(dets)
    if len(whole) < len(dets):
        print(f"dropped {len(dets) - len(whole)} partial detection(s) (ball only partly segmented)")
    raw = split_segments(whole)
    segs = [c for c in (clean_segment(s) for s in raw) if c]
    if len(segs) < len(raw):
        print(f"dropped {len(raw) - len(segs)} segment(s) that weren't a clean flight")
    results = [segment_speed(s, cam) for s in segs]

    print(f"\n{'segment':>15} {'frames':>6} {'2D mph':>14} {'3D mph':>14} {'start 3D':>9} "
          f"{'depth':>6} {'blur':>5} {'exposure':>9} {'gravity':>9}")
    for r in results:
        exp = f"{r.exposure_ms:.1f} ms" if r.exposure_ms else "n/a"
        grav = f"{r.gravity:.2f}" if r.gravity is not None else "n/a"
        print(f"{r.first_frame:>6}-{r.last_frame:<8} {r.n:>6} "
              f"{r.speed_2d * MPH:7.1f} ±{r.speed_2d_err * MPH:4.1f} "
              f"{r.speed_3d * MPH:7.1f} ±{r.speed_3d_err * MPH:4.1f} "
              f"{r.v_start_3d * MPH:9.1f} {r.depth_m:5.2f}m {r.blur_ratio:5.2f} {exp:>9} {grav:>9}")
    if not results:
        print("no clean flight segments found")
        return

    print("gravity column: measured downward acceleration in m/s^2; 9.81 means the timing and "
          "ball-size scale agree with physics (needs a level camera and >= 10 frames)")
    shot = results[0]
    print(f"\nshot (first flight segment, frames {shot.first_frame}-{shot.last_frame}): "
          f"{shot.speed_2d * MPH:.1f} mph ({shot.speed_2d:.2f} m/s) in the image plane; "
          f"3D cross-check {shot.speed_3d * MPH:.1f} ±{shot.speed_3d_err * MPH:.1f} mph")

    # ---- overlay: background + trajectory coloured by segment ----
    ov = bg.copy()
    colours = [(0, 0, 255), (255, 128, 0), (0, 200, 0), (255, 0, 255), (0, 200, 255)]
    in_seg = {d.frame: k for k, s in enumerate(segs) for d in s}
    for d in dets:
        k = in_seg.get(d.frame)
        col = colours[k % len(colours)] if k is not None else (160, 160, 160)
        cv2.ellipse(ov, ((d.x, d.y), (d.d_minor, d.d_major), d.angle), col, 1)
        cv2.circle(ov, (round(d.x), round(d.y)), 2, col, -1)
    for k, (s, r) in enumerate(zip(segs, results)):
        m = s[len(s) // 2]
        cv2.putText(ov, f"{r.speed_2d * MPH:.1f} mph", (round(m.x) + 40, round(m.y)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 0), 5)
        cv2.putText(ov, f"{r.speed_2d * MPH:.1f} mph", (round(m.x) + 40, round(m.y)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.1, colours[k % len(colours)], 2)
    cv2.imwrite(str(out / "overlay.jpg"), ov)

    # ---- crops around each detection in the first segment (QA) ----
    want = {d.frame: d for d in segs[0]}
    crops = []
    for i, _, f in frames(args.video, scale):
        if i in want and len(crops) < 16:
            d = want[i]
            r = int(max(d.d_major, 40))
            x0, y0 = max(0, int(d.x) - r), max(0, int(d.y) - r)
            c = f[y0:y0 + 2 * r, x0:x0 + 2 * r].copy()
            cv2.ellipse(c, ((d.x - x0, d.y - y0), (d.d_minor, d.d_major), d.angle), (0, 0, 255), 1)
            c = cv2.resize(c, (200, 200))
            cv2.putText(c, str(i), (5, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
            crops.append(c)
    if crops:
        while len(crops) % 8:
            crops.append(np.zeros_like(crops[0]))
        cv2.imwrite(str(out / "crops.jpg"),
                    np.vstack([np.hstack(crops[i:i + 8]) for i in range(0, len(crops), 8)]))
    print(f"\nwrote {out}/detections.csv, overlay.jpg, crops.jpg")

    if args.publish:
        body = json.dumps({"speed_mps": shot.speed_2d, "speed_err_mps": shot.speed_2d_err}).encode()
        req = urllib.request.Request(args.publish.rstrip("/") + "/shot", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=3)
            print(f"published to {args.publish}")
        except OSError as e:
            print(f"could not publish to {args.publish}: {e}")


if __name__ == "__main__":
    main()
