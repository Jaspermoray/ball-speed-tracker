#!/usr/bin/env python3
"""Live shot tracker: phone camera -> shot detection -> speed on the big display.

  .venv/bin/python live.py                         # phone on USB, display at :8765
  .venv/bin/python live.py --serial 192.168.2.2:5555
  .venv/bin/python live.py --file throw240.mp4     # replay a clip as if it were live
  .venv/bin/python live.py --file clip.mp4 --fast  # replay as fast as possible

Open http://localhost:8765/ for the display (or pass --open).
"""
from __future__ import annotations

import argparse
import queue
import threading
import time
import webbrowser
from pathlib import Path

import tracker  # noqa: F401  (first: loads PyAV + OpenCV quietly on macOS)
import av
import cv2
import numpy as np

from tracker.display import DisplayServer
from tracker.frames import Decoder
from tracker.live_shots import SCALE, ShotDetector, ShotResult
from tracker.scrcpy_client import CameraConfig, Packet, ScrcpyCamera, Session

MPH = 2.2369363


PREVIEW_W, PREVIEW_H, PREVIEW_FPS = 640, 360, 15
TRAIL_S = 4.0            # how long the last shot's path stays drawn on the preview


def preview_loop(disp: DisplayServer, latest: dict, det: ShotDetector, stop: threading.Event):
    """Encode the newest frame as a small JPEG ~15 times a second, with the ball
    the trigger currently sees and the path of the last shot drawn on it."""
    last_id = None
    while not stop.is_set():
        t0 = time.monotonic()
        fr = latest.get("frame")
        if fr is not None and id(fr) != last_id:
            last_id = id(fr)
            img = fr.reformat(width=PREVIEW_W, height=PREVIEW_H, format="bgr24").to_ndarray()
            k = PREVIEW_W / fr.width
            shot = latest.get("shot")
            if shot and time.monotonic() - shot[0] < TRAIL_S:
                pts = np.array([[d.x * k, d.y * k] for d in shot[1].detections], np.int32)
                if len(pts) > 1:
                    cv2.polylines(img, [pts], False, (0, 255, 212), 2, cv2.LINE_AA)
            sd = det.recent_hits[-1] if det.recent_hits else None
            if sd is not None:
                ks = k * SCALE
                cv2.circle(img, (int(sd.x * ks), int(sd.y * ks)),
                           max(6, int(sd.d_major * ks * 0.7)), (0, 255, 212), 2, cv2.LINE_AA)
            if det.active:
                cv2.putText(img, "SHOT", (PREVIEW_W - 90, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                            (0, 255, 212), 2, cv2.LINE_AA)
            ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                disp.set_preview(jpg.tobytes())
        stop.wait(max(0.0, 1 / PREVIEW_FPS - (time.monotonic() - t0)))


def report(res: ShotResult, disp: DisplayServer | None, min_mph: float, latest: dict):
    latest["shot"] = (time.monotonic(), res)
    s = res.shot
    dur = res.t_end - res.t_start
    if s is None:
        print(f"[shot {res.index}] {len(res.detections)} detections over {dur:.2f}s, "
              f"no clean flight (analysis {res.analysis_ms:.0f} ms)", flush=True)
        return
    mph = s.speed_2d * MPH
    segs = ", ".join(f"{r.speed_2d * MPH:.1f}" for r in res.segments)
    grav = f", gravity {s.gravity:.1f}" if s.gravity is not None else ""
    print(f"[shot {res.index}] {mph:.1f} mph ±{s.speed_2d_err * MPH:.1f} "
          f"(3D {s.speed_3d * MPH:.1f}; frames {s.first_frame}-{s.last_frame}, n={s.n}{grav}; "
          f"all segments: {segs}; analysis {res.analysis_ms:.0f} ms)", flush=True)
    if disp and mph >= min_mph:
        disp.publish(speed_mps=s.speed_2d, speed_err_mps=s.speed_2d_err)


# ------------------------------------------------------------------ sources
def file_frames(path: str, realtime: bool, stop: threading.Event):
    """Yield (frame, t_real) from a file, honouring slow-motion metadata."""
    from analyze_clip import time_scale
    scale, desc = time_scale(path, None)
    print(f"[file] {desc}", flush=True)
    c = av.open(path)
    s = c.streams.video[0]
    s.thread_type = "AUTO"
    t0 = None
    wall0 = time.monotonic()
    for f in c.decode(s):
        if stop.is_set():
            break
        t = float(f.pts * s.time_base) * scale
        t0 = t if t0 is None else t0
        if realtime:
            lag = (t - t0) - (time.monotonic() - wall0)
            if lag > 0:
                time.sleep(lag)
        yield f, t - t0
    c.close()


def live_frames(args, stop: threading.Event, stats: dict):
    br = args.bit_rate.upper()
    bit_rate = int(float(br[:-1]) * 1e6) if br.endswith("M") else int(br)
    cam = ScrcpyCamera(CameraConfig(bit_rate=bit_rate), serial=args.serial)
    cam.start()
    stats["cam"] = cam
    q: queue.Queue = queue.Queue()

    def reader():
        try:
            for item in cam.packets():
                if isinstance(item, Session):
                    continue
                q.put(item)
                if stop.is_set():
                    break
        except (EOFError, OSError) as e:
            if not stop.is_set():
                print(f"[client] stream ended: {e}", flush=True)
        finally:
            q.put(None)

    threading.Thread(target=reader, daemon=True).start()
    dec = Decoder(cam.codec, threads=args.decode_threads)
    min_off = None
    try:
        while True:
            item: Packet | None = q.get()
            if item is None:
                break
            stats["qsize"] = q.qsize()
            if not item.config:
                off = item.arrival_ns / 1e6 - item.pts_us / 1e3
                min_off = off if min_off is None else min(min_off, off)
                stats["behind_ms"] = max(stats.get("behind_ms", 0), off - min_off)
            for fr in dec.decode(item.data, item.pts_us, item.config):
                yield fr, fr.pts / 1e6
        for fr in dec.flush():
            yield fr, fr.pts / 1e6
    finally:
        dec.close()
        cam.stop()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", help="replay this video instead of the live camera")
    ap.add_argument("--fast", action="store_true", help="with --file: don't pace to real time")
    ap.add_argument("--serial", help="adb serial, e.g. 192.168.2.2:5555 for Ethernet")
    ap.add_argument("--bit-rate", default="100M")
    ap.add_argument("--decode-threads", type=int, default=4)
    ap.add_argument("--hfov", type=float, default=66.0)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-display", action="store_true")
    ap.add_argument("--no-preview", action="store_true", help="don't stream the camera to the display")
    ap.add_argument("--open", action="store_true", help="open the display in the browser")
    ap.add_argument("--min-mph", type=float, default=3.0,
                    help="don't publish shots slower than this (rolls, nudges)")
    ap.add_argument("--duration", type=float, help="stop after this many seconds")
    args = ap.parse_args()

    disp = None
    if not args.no_display:
        disp = DisplayServer(port=args.port).start()
        print(f"[display] http://localhost:{args.port}/", flush=True)
        if args.open:
            webbrowser.open(f"http://localhost:{args.port}/")

    stop = threading.Event()
    stats: dict = {}
    latest: dict = {}
    det = ShotDetector(lambda r: report(r, disp, args.min_mph, latest))
    if disp and not args.no_preview:
        threading.Thread(target=preview_loop, args=(disp, latest, det, stop), daemon=True).start()
    src = (file_frames(args.file, not args.fast, stop) if args.file
           else live_frames(args, stop, stats))

    t_wall0 = time.monotonic()
    next_report = t_wall0 + 1.0
    n = n_last = 0
    pts_last = None
    gaps = 0
    try:
        for fr, t in src:
            if pts_last is not None and t - pts_last > 0.006:
                gaps += 1
            pts_last = t
            det.feed(fr, t)
            latest["frame"] = fr
            n += 1
            now = time.monotonic()
            if args.duration and now - t_wall0 >= args.duration:
                break
            if now >= next_report:
                busy = np.array(det.busy_ms) if det.busy_ms else np.zeros(1)
                state = "SHOT" if det.active else "idle"
                behind = stats.pop("behind_ms", None)
                print(f"[{now - t_wall0:6.1f}s] {n - n_last:4d} fps  detector "
                      f"p50={np.median(busy):.2f}ms p99={np.percentile(busy, 99):.2f}ms  "
                      f"gaps>6ms={gaps}  "
                      + (f"behind={behind:.0f}ms  " if behind is not None else "")
                      + f"{state}  shots={det.shots}", flush=True)
                n_last = n
                gaps = 0
                next_report = now + 1.0
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        det.flush()
        if hasattr(src, "close"):
            src.close()
        if disp and args.file:
            print("[display] still serving; Ctrl+C to quit", flush=True)
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
