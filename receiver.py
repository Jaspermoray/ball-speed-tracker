#!/usr/bin/env python3
"""Live 240 fps camera receiver for the Pixel 9a (handoff step 3).

Launches scrcpy-server directly, keeps per-frame device PTS, decodes to gray
into a ring buffer, prints live stats, and optionally records the raw stream.

Examples:
  .venv/bin/python receiver.py                     # stats only
  .venv/bin/python receiver.py --preview           # + live preview window
  .venv/bin/python receiver.py --save recordings/  # + raw H.264 and packets.csv
  .venv/bin/python receiver.py --duration 10       # stop after 10 s

Press Ctrl+C (or q / Esc in the preview window) to stop.
"""
from __future__ import annotations

import tracker  # noqa: F401  (first: loads PyAV + OpenCV quietly on macOS)
import argparse
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from tracker.device_health import HealthMonitor
from tracker.frames import Decoder, Recorder, RingBuffer, y_plane
from tracker.scrcpy_client import CameraConfig, Packet, ScrcpyCamera, Session

GAP_WARN_MS = 6.0


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.bytes = 0
        self.pkt_pts: list[int] = []      # media packet PTS, this interval
        self.lag_ms: list[float] = []     # arrival -> decoded, this interval
        self.all_pts: list[int] = []
        self.all_lag: list[float] = []
        self.total_bytes = 0
        self.session: Session | None = None
        self.last_pts: int | None = None
        self.decode_errors = 0
        # Backlog: how far the newest frame lags real time, relative to the
        # quickest-arriving frame seen so far (phone and Mac clocks differ, so
        # only changes in (arrival - pts) are meaningful).
        self.min_offset_ms: float | None = None
        self.behind_ms = 0.0               # latest value
        self.behind_max_ms = 0.0           # max this interval
        self.behind_max_all = 0.0

    def take(self):
        with self.lock:
            b, p, l, bm = self.bytes, self.pkt_pts, self.lag_ms, self.behind_max_ms
            self.bytes, self.pkt_pts, self.lag_ms, self.behind_max_ms = 0, [], [], 0.0
        return b, p, l, bm


def reader_loop(cam: ScrcpyCamera, q: queue.Queue, stats: Stats, rec: Recorder | None,
                stop: threading.Event):
    try:
        for item in cam.packets():
            if isinstance(item, Session):
                print(f"[client] session {item.width}x{item.height}", flush=True)
                stats.session = item
                continue
            if rec:
                rec.write(item.pts_us, item.config, item.key, item.data, item.arrival_ns)
            with stats.lock:
                stats.bytes += len(item.data) + 12
                stats.total_bytes += len(item.data) + 12
                if not item.config:
                    stats.pkt_pts.append(item.pts_us)
                    stats.all_pts.append(item.pts_us)
                    off = item.arrival_ns / 1e6 - item.pts_us / 1e3
                    if stats.min_offset_ms is None or off < stats.min_offset_ms:
                        stats.min_offset_ms = off
                    stats.behind_ms = off - stats.min_offset_ms
                    stats.behind_max_ms = max(stats.behind_max_ms, stats.behind_ms)
                    stats.behind_max_all = max(stats.behind_max_all, stats.behind_ms)
            q.put(item)
            if stop.is_set():
                break
    except (EOFError, OSError) as e:
        if not stop.is_set():
            print(f"[client] stream ended: {e}", flush=True)
    finally:
        q.put(None)
        stop.set()


def decoder_loop(codec: str, q: queue.Queue, stats: Stats, ring_holder: dict,
                 buffer_seconds: float, fps: int, threads: int):
    dec = Decoder(codec, threads=threads)
    arrival: dict[int, int] = {}
    while True:
        item: Packet | None = q.get()
        if item is None:
            # Drain the frame threads before the codec context is freed; freeing
            # an unflushed multi-threaded context can deadlock on shutdown.
            frames = dec.flush()
        else:
            if not item.config:
                arrival[item.pts_us] = item.arrival_ns
            try:
                frames = dec.decode(item.data, item.pts_us, item.config)
            except Exception as e:  # corrupt packet: count and keep going
                stats.decode_errors += 1
                print(f"[decoder] error: {e}", flush=True)
                continue
        now = time.monotonic_ns()
        for fr in frames:
            ring = ring_holder.get("ring")
            if ring is None:
                cap = max(1, int(buffer_seconds * fps))
                ring = RingBuffer(cap, fr.height, fr.width)
                ring_holder["ring"] = ring
                print(f"[client] ring buffer: {cap} frames ({cap * fr.width * fr.height / 1e9:.2f} GB)",
                      flush=True)
            ring.push(fr.pts, y_plane(fr))
            t_arr = arrival.pop(fr.pts, None)
            if t_arr is not None:
                lag = (now - t_arr) / 1e6
                with stats.lock:
                    stats.lag_ms.append(lag)
                    stats.all_lag.append(lag)
        if item is None:
            break
    dec.close()


def gap_summary(pts_us: list[int]) -> str:
    if len(pts_us) < 2:
        return "n/a"
    g = np.diff(np.asarray(pts_us, np.int64)) / 1000.0
    span = (pts_us[-1] - pts_us[0]) / 1e6
    fps = (len(pts_us) - 1) / span if span > 0 else float("nan")
    return (f"fps={fps:6.1f} gap med={np.median(g):.3f}ms max={g.max():5.1f}ms "
            f">{GAP_WARN_MS:.0f}ms={int((g > GAP_WARN_MS).sum())}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--fps", type=int, default=240)
    ap.add_argument("--no-high-speed", action="store_true")
    ap.add_argument("--bit-rate", default="100M", help="e.g. 100M, 50M, 20000000")
    ap.add_argument("--buffer-seconds", type=float, default=2.0)
    ap.add_argument("--save", metavar="DIR", help="record raw stream into DIR/<timestamp>/")
    ap.add_argument("--preview", action="store_true", help="show a live preview window")
    ap.add_argument("--duration", type=float, help="stop after this many seconds")
    ap.add_argument("--serial", help="adb device serial")
    ap.add_argument("--no-health", action="store_true", help="don't poll phone battery/temperature")
    ap.add_argument("--decode-threads", type=int, default=4,
                    help="decoder threads; more = higher throughput but more latency (0 = all cores)")
    args = ap.parse_args()

    br = args.bit_rate.upper()
    bit_rate = int(float(br[:-1]) * 1e6) if br.endswith("M") else int(br)
    cfg = CameraConfig(size=args.size, fps=args.fps, high_speed=not args.no_high_speed,
                       bit_rate=bit_rate)

    rec = None
    if args.save:
        out = Path(args.save) / datetime.now().strftime("%Y%m%d-%H%M%S")
        rec = Recorder(out)
        print(f"[client] recording to {out}/", flush=True)

    cam = ScrcpyCamera(cfg, serial=args.serial)
    stats = Stats()
    stop = threading.Event()
    q: queue.Queue = queue.Queue()
    ring_holder: dict = {}

    health = None if args.no_health else HealthMonitor(cam.adb, args.serial)

    preview = None
    if args.preview:
        import cv2
        preview = cv2
        rot_codes = [None, cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE]
        rot = 0
        cv2.namedWindow("receiver", cv2.WINDOW_NORMAL)

    try:
        cam.start()
        t_start = time.monotonic()
        rt = threading.Thread(target=reader_loop, args=(cam, q, stats, rec, stop), daemon=True)
        dt = threading.Thread(target=decoder_loop,
                              args=(cam.codec, q, stats, ring_holder, args.buffer_seconds, args.fps,
                                    args.decode_threads),
                              daemon=True)
        rt.start()
        dt.start()
        if health:
            health.start()

        next_report = time.monotonic() + 1.0
        reports = 0
        while not stop.is_set():
            now = time.monotonic()
            if args.duration and now - t_start >= args.duration:
                break
            if now >= next_report:
                interval = now - next_report + 1.0
                next_report = now + 1.0
                b, pts, lags, behind_max = stats.take()
                # include previous interval's last PTS so boundary gaps are counted
                seq = ([stats.last_pts] if stats.last_pts is not None else []) + pts
                if pts:
                    stats.last_pts = pts[-1]
                lag_s = (f"lag p50={np.median(lags):5.1f}ms max={max(lags):5.1f}ms"
                         if lags else "lag n/a")
                warn = "  << FALLING BEHIND" if behind_max > 250 else ""
                print(f"[{now - t_start:6.1f}s] rx={len(pts) / interval:5.1f}/s {gap_summary(seq)}  "
                      f"{b * 8 / interval / 1e6:6.1f} Mbps  behind={behind_max:6.0f}ms  "
                      f"decode {lag_s}  queue={q.qsize()}{warn}", flush=True)
                h = health.latest() if health else None
                reports += 1
                if h and (reports == 3 or reports % 10 == 0):
                    print(f"         phone: {h.short()}", flush=True)
            if preview:
                ring = ring_holder.get("ring")
                latest = ring.latest() if ring else None
                if latest is not None:
                    img = latest[1][::2, ::2]
                    if rot_codes[rot] is not None:
                        img = preview.rotate(img, rot_codes[rot])
                    preview.imshow("receiver", img)
                k = preview.waitKey(30) & 0xFF
                if k in (ord("q"), 27):
                    break
                if k == ord("r"):
                    rot = (rot + 1) % 4
            else:
                time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if health:
            health.stop()
        cam.stop()   # closes the socket -> reader exits and queues the sentinel
        for t in (rt, dt) if "dt" in locals() else ():
            t.join(timeout=5)
            if t.is_alive():
                print(f"[client] warning: {t.name} did not exit", flush=True)
        if rec:
            rec.close()
        if preview:
            preview.destroyAllWindows()

    # ---- summary ----
    with stats.lock:
        pts = list(stats.all_pts)
        lags = list(stats.all_lag)
    print("\n=== summary ===")
    print(f"media packets: {len(pts)}   decode errors: {stats.decode_errors}")
    if len(pts) > 1:
        dur = (pts[-1] - pts[0]) / 1e6
        print(f"duration (PTS): {dur:.3f}s   avg bitrate: {stats.total_bytes * 8 / dur / 1e6:.1f} Mbps")
        print(f"overall: {gap_summary(pts)}")
        print(f"backlog: max {stats.behind_max_all:.0f} ms behind real time "
              f"(ended {stats.behind_ms:.0f} ms behind)")
        g = np.diff(np.asarray(pts, np.int64)) / 1000.0
        big = np.nonzero(g > GAP_WARN_MS)[0]
        for i in big[:10]:
            print(f"  gap of {g[i]:.1f} ms after frame {i} (t={(pts[i] - pts[0]) / 1e6:.3f}s)")
        if np.any(g <= 0):
            print(f"  WARNING: {int((g <= 0).sum())} non-increasing PTS")
    if lags:
        print(f"decode lag: p50={np.percentile(lags, 50):.1f}ms p95={np.percentile(lags, 95):.1f}ms "
              f"max={max(lags):.1f}ms")
    hs = health.summary() if health else None
    if hs:
        print(hs)
    if rec:
        print(f"saved: {rec.dir}/")


if __name__ == "__main__":
    main()
