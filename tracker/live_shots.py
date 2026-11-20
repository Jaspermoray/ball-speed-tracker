"""Live shot detection on a stream of decoded frames.

Two stages, so the per-frame cost stays far below the 4.2 ms frame interval:

1. Every frame: the ball detector on a 1/4-scale copy (480x270). A shot starts
   when the ball is found, moving, in 3 of the last 5 frames.
2. During a shot: crop a full-resolution square around the ball in each frame
   (plus the same square from the frame ~15 ms earlier and from the background).
   When the ball has been gone (or still) for a moment, a worker thread runs the
   full-resolution detector on the crops, fits the flight and reports the speed.

Feed it with ShotDetector.feed(frame, t) where frame is an av.VideoFrame and t
is real time in seconds (device PTS for live streams).
"""
from __future__ import annotations

import math
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import av
import numpy as np

from tracker.ball import (RECENT_S, Camera, Detection, SegmentSpeed, _changed, clean_segment,
                          detect, drop_partials, segment_speed, smooth, split_segments)
from tracker.spin import SpinResult, ball_patch, estimate_spin

SPIN_R = 20

SCALE = 4                 # trigger works at 1/4 resolution
BG_EVERY_S = 0.5          # background snapshot interval while idle
BG_SNAPSHOTS = 7          # background = per-pixel median of this many snapshots


@dataclass
class Crop:
    frame: int
    t: float
    x0: int
    y0: int
    cur: np.ndarray       # smoothed BGR crop, current frame
    raw: np.ndarray       # unsmoothed crop (spin needs the fine surface detail)
    ref: np.ndarray | None  # same region, frame ~15 ms earlier
    bg: np.ndarray        # same region, background


@dataclass
class ShotResult:
    index: int
    t_start: float
    t_end: float
    detections: list[Detection]
    segments: list[SegmentSpeed]
    analysis_ms: float
    crops: int
    spin: SpinResult | None = None
    spin_ms: float = 0.0

    @property
    def shot(self) -> SegmentSpeed | None:
        return self.segments[0] if self.segments else None


@dataclass
class _Active:
    crops: list[Crop] = field(default_factory=list)
    last_seen_t: float = 0.0
    start_t: float = 0.0
    last_pos: tuple[float, float] | None = None
    still_since: float | None = None


class ShotDetector:
    def __init__(self, on_shot: Callable[[ShotResult], None], hfov_deg: float = 66.0,
                 spin: bool = True, max_rpm: float = 6000,
                 on_spin: Callable[[ShotResult], None] | None = None,
                 end_gap_s: float = 0.15, max_shot_s: float = 3.0, still_s: float = 0.3,
                 log: Callable[[str], None] = print):
        self.on_shot = on_shot
        self.hfov = hfov_deg
        self.spin = spin
        self.max_rpm = max_rpm
        self.on_spin = on_spin
        self.end_gap_s = end_gap_s
        self.max_shot_s = max_shot_s
        self.still_s = still_s
        self.log = log

        self.n = 0
        self.width = self.height = 0
        self.small_hist: deque = deque()            # (t, smoothed small bgr) for "recent"
        self.full_hist: deque = deque(maxlen=12)    # (i, t, frame, small_det) for pre-roll / ref
        self._bgr_cache: dict[int, np.ndarray] = {}  # full-res BGR of frames still in full_hist
        self.small_bg_snaps: deque = deque(maxlen=BG_SNAPSHOTS)
        self.full_bg_snaps: deque = deque(maxlen=BG_SNAPSHOTS)
        self.small_bg: np.ndarray | None = None
        self.full_bg: np.ndarray | None = None
        self.last_bg_t = -1e9
        self.recent_hits: deque = deque(maxlen=5)   # small detections (or None), last 5 frames
        self.active: _Active | None = None
        self.shots = 0
        self.busy_ms: deque = deque(maxlen=240)     # per-frame processing time

        self._q: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._work, daemon=True)
        self._worker.start()

    # ------------------------------------------------------------------ feed
    def feed(self, frame: av.VideoFrame, t: float):
        t0 = time.perf_counter()
        i = self.n
        self.n += 1
        if not self.width:
            self.width, self.height = frame.width, frame.height
        sw, sh = self.width // SCALE, self.height // SCALE
        small = smooth(frame.reformat(width=sw, height=sh, format="bgr24").to_ndarray())

        # background snapshots (only while no shot is running)
        if self.active is None and t - self.last_bg_t >= BG_EVERY_S:
            self.last_bg_t = t
            self.small_bg_snaps.append(small)
            self.full_bg_snaps.append(smooth(frame.to_ndarray(format="bgr24")))
            self.small_bg = np.median(np.stack(self.small_bg_snaps), axis=0).astype(np.uint8)
            self.full_bg = None                     # rebuilt lazily when a shot needs it

        # "changed in the last ~15 ms" mask at small scale
        self.small_hist.append((t, small))
        while len(self.small_hist) > 2 and t - self.small_hist[1][0] >= RECENT_S:
            self.small_hist.popleft()
        recent = _changed(small, self.small_hist[0][1], 30) if len(self.small_hist) > 1 else None

        sdet = None
        if (self.small_bg is not None and len(self.small_bg_snaps) >= 3 and recent is not None
                and np.count_nonzero(recent) < 0.25 * recent.size):
            pred = None
            if self.active and self.active.last_pos:
                pred = self.active.last_pos
            sdet = detect(small, self.small_bg, i, t, pred, 80.0 if pred else 1e9,
                          min_area=8, recent=recent)
            if sdet is None and pred is not None:
                sdet = detect(small, self.small_bg, i, t, None, min_area=8, recent=recent)
        self.recent_hits.append(sdet)
        if len(self.full_hist) == self.full_hist.maxlen:
            self._bgr_cache.pop(self.full_hist[0][0], None)
        self.full_hist.append((i, t, frame, sdet))

        if self.active is None:
            hits = [d for d in self.recent_hits if d is not None]
            if len(hits) >= 3 and math.hypot(hits[-1].x - hits[0].x,
                                             hits[-1].y - hits[0].y) >= 0.5 * hits[-1].d_minor:
                self._start(t)
        else:
            a = self.active
            if sdet is not None:
                self._add_crop(i, t, frame, sdet)
                if a.last_pos and math.hypot(sdet.x - a.last_pos[0], sdet.y - a.last_pos[1]) < 0.5:
                    a.still_since = a.still_since or t
                else:
                    a.still_since = None
                a.last_pos = (sdet.x, sdet.y)
                a.last_seen_t = t
            ended = (t - a.last_seen_t > self.end_gap_s
                     or t - a.start_t > self.max_shot_s
                     or (a.still_since is not None and t - a.still_since > self.still_s))
            if ended:
                self._finish()
        self.busy_ms.append((time.perf_counter() - t0) * 1000)

    def flush(self):
        """Finish a shot in progress (end of file / stream) and wait for results."""
        if self.active:
            self._finish()
        self._q.join()

    # --------------------------------------------------------------- shots
    def _ensure_full_bg(self):
        if self.full_bg is None and self.full_bg_snaps:
            self.full_bg = np.median(np.stack(self.full_bg_snaps), axis=0).astype(np.uint8)

    def _start(self, t: float):
        self._ensure_full_bg()
        self.active = _Active(start_t=t, last_seen_t=t)
        # pre-roll: crop the recent frames where the small detector saw the ball
        for i, ti, fr, sd in list(self.full_hist):
            if sd is not None:
                self._add_crop(i, ti, fr, sd)
                self.active.last_pos = (sd.x, sd.y)

    def _bgr(self, i: int, frame: av.VideoFrame) -> np.ndarray:
        """Full-res BGR of a history frame, converted once and cached."""
        a = self._bgr_cache.get(i)
        if a is None:
            a = self._bgr_cache[i] = frame.to_ndarray(format="bgr24")
        return a

    def _ref_frame(self, t: float):
        """(index, frame) about RECENT_S before t, from the short full-res history."""
        best = None
        for i, ti, fr, _ in self.full_hist:
            if t - ti >= RECENT_S:
                best = (i, fr)
        return best

    def _add_crop(self, i: int, t: float, frame: av.VideoFrame, sd: Detection):
        a = self.active
        if a.crops and a.crops[-1].frame == i:
            return
        cx, cy = sd.x * SCALE, sd.y * SCALE
        half = int(max(80, 1.6 * sd.d_major * SCALE + 40))
        x0 = max(0, int(cx) - half)
        y0 = max(0, int(cy) - half)
        x1 = min(self.width, int(cx) + half)
        y1 = min(self.height, int(cy) + half)
        raw = self._bgr(i, frame)[y0:y1, x0:x1].copy()
        cur = smooth(raw)
        rf = self._ref_frame(t)
        ref = smooth(self._bgr(*rf)[y0:y1, x0:x1].copy()) if rf is not None else None
        a.crops.append(Crop(i, t, x0, y0, cur, raw, ref, self.full_bg[y0:y1, x0:x1].copy()))

    def _finish(self):
        a, self.active = self.active, None
        self.recent_hits.clear()
        if len(a.crops) >= 5:
            self.shots += 1
            self._q.put((self.shots, a))

    # -------------------------------------------------------------- worker
    def _work(self):
        while True:
            idx, a = self._q.get()
            try:
                res, seg0 = self._analyse(idx, a)
                self.on_shot(res)                      # speed: report straight away
                if self.spin and seg0:
                    self._add_spin(res, a, seg0)       # spin: a second or two later
                    if self.on_spin:
                        self.on_spin(res)
            except Exception as e:  # never let one bad shot kill the worker
                self.log(f"[shots] analysis of shot {idx} failed: {e}")
            finally:
                self._q.task_done()

    def _analyse(self, idx: int, a: _Active) -> tuple[ShotResult, list[Detection] | None]:
        t0 = time.perf_counter()
        dets: list[Detection] = []
        for c in a.crops:
            recent = _changed(c.cur, c.ref, 30) if c.ref is not None else None
            d = detect(c.cur, c.bg, c.frame, c.t, None, recent=recent)
            if d is None:
                continue
            # a blob touching the crop edge (but not the image edge) is cut off: skip it
            dets.append(Detection(d.frame, d.t, d.x + c.x0, d.y + c.y0, d.d_minor, d.d_major,
                                  d.angle, d.area))
        whole = drop_partials(dets)
        segs = [s for s in (clean_segment(s) for s in split_segments(whole)) if s]
        cam = Camera(self.width, self.height, self.hfov)
        results = [segment_speed(s, cam) for s in segs]
        res = ShotResult(idx, a.start_t, a.crops[-1].t, dets, results,
                         (time.perf_counter() - t0) * 1000, len(a.crops))
        return res, (segs[0] if segs else None)

    def _add_spin(self, res: ShotResult, a: _Active, s: list[Detection]):
        t1 = time.perf_counter()
        by_frame = {c.frame: c for c in a.crops}
        patches = [ball_patch(by_frame[d.frame].raw, d, SPIN_R,
                              by_frame[d.frame].x0, by_frame[d.frame].y0) for d in s]
        dur = s[-1].t - s[0].t
        vel = ((s[-1].x - s[0].x) / dur, (s[-1].y - s[0].y) / dur) if dur > 0 else (1.0, 0.0)
        res.spin = estimate_spin(patches, s, vel, max_rpm=self.max_rpm)
        res.spin_ms = (time.perf_counter() - t1) * 1000
