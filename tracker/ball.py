"""Classical tennis-ball detection and speed estimation for a fixed camera.

Detection, per frame:
  1. foreground = pixels that differ from a static background (median of the clip)
  2. AND ball-coloured (optic yellow in HSV)
  3. largest plausible blob near the predicted position
  4. ellipse fit to its contour: the centre is sub-pixel; the minor axis is the
     true ball diameter (motion blur stretches only the major axis)

Scale: a tennis ball is 6.7 cm across, so each frame carries its own ruler
(metres per pixel = 0.067 / minor-axis pixels). With an assumed camera field of
view, the apparent size also gives depth, so the track can be lifted to 3D.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

BALL_DIAMETER_M = 0.067

# OpenCV HSV: H 0-180. Optic yellow sits around H 25-40. Brightness floor is
# low because high-speed capture (exposure < 1 frame) makes a ball in a dim room
# dark olive (V ~ 50); the hue gate and the motion tests do the discriminating.
HSV_LO = (22, 60, 30)
HSV_HI = (52, 255, 255)


@dataclass
class Detection:
    frame: int
    t: float            # seconds, real time
    x: float            # centre, px (sub-pixel)
    y: float
    d_minor: float      # px, ~true diameter
    d_major: float      # px, diameter + blur streak
    angle: float        # ellipse orientation, degrees
    area: int           # blob pixel count


def background_median(frames: list[np.ndarray], step: int = 4) -> np.ndarray:
    """Per-pixel median over a subsample of frames; a moving ball vanishes."""
    sample = np.stack(frames[::step])
    return np.median(sample, axis=0).astype(np.uint8)


# The colour mask loses the soft rim of the ball (edge pixels are part ball,
# part background), so ellipse axes come out about 1 px short. Measured on
# synthetic clips with known size (28 and 36 px balls); without this, speeds
# read 3-4% high because the ball is the ruler.
EDGE_PX = 1.0

RECENT_S = 0.015          # "moved recently" = differs from the frame ~15 ms earlier
CAMERA_MOTION_FRAC = 0.25  # if this much of the frame changed, the camera moved: skip


def smooth(bgr: np.ndarray) -> np.ndarray:
    """Light blur so sensor noise (dark scenes, short high-speed exposures)
    doesn't pass the colour and difference tests pixel by pixel."""
    return cv2.GaussianBlur(bgr, (5, 5), 0)


def _changed(a: np.ndarray, b: np.ndarray, thr: int) -> np.ndarray:
    """255 where any colour channel differs by more than thr. (OpenCV per-channel
    max is ~10x faster than numpy's max over the interleaved channel axis.)"""
    d0, d1, d2 = cv2.split(cv2.absdiff(a, b))
    _, m = cv2.threshold(cv2.max(cv2.max(d0, d1), d2), thr, 255, cv2.THRESH_BINARY)
    return m


def ball_mask(bgr: np.ndarray, bg: np.ndarray, diff_thr: int = 30) -> np.ndarray:
    """Ball-coloured pixels that differ from the static background."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    colour = cv2.inRange(hsv, HSV_LO, HSV_HI)
    m = cv2.bitwise_and(colour, _changed(bgr, bg, diff_thr))
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, k)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    return m


def detect(bgr: np.ndarray, bg: np.ndarray, frame: int, t: float,
           predicted: tuple[float, float] | None = None, max_jump: float = 200.0,
           min_area: int = 150, recent: np.ndarray | None = None,
           min_recent_frac: float = 0.15) -> Detection | None:
    """bgr and bg should already be smoothed. recent: mask of pixels that
    changed in the last ~15 ms; blobs with too little of it are static objects
    that only differ from the background through lighting drift or noise."""
    m = ball_mask(bgr, bg)
    contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    H, W = m.shape
    best, best_score = None, -1.0
    for c in contours:
        area = cv2.contourArea(c)
        if area < min_area or len(c) < 5:
            continue
        bx, by, bw, bh = cv2.boundingRect(c)
        if bx <= 2 or by <= 2 or bx + bw >= W - 2 or by + bh >= H - 2:
            continue                              # clipped by the frame edge: size unreliable
        (cx, cy), (a1, a2), ang = cv2.fitEllipse(c)
        dmin, dmaj = min(a1, a2), max(a1, a2)
        if dmin < 6 or dmaj / dmin > 4:          # not ball-shaped
            continue
        fill = area / (math.pi * a1 * a2 / 4)    # ellipse fill ratio, ~1 for a ball
        if fill < 0.6:
            continue
        if recent is not None:
            blob = np.zeros((bh, bw), np.uint8)
            cv2.drawContours(blob, [c - (bx, by)], -1, 255, -1)
            n_blob = int(np.count_nonzero(blob))
            n_recent = int(np.count_nonzero(cv2.bitwise_and(blob, recent[by:by + bh, bx:bx + bw])))
            if n_blob == 0 or n_recent / n_blob < min_recent_frac:
                continue
        score = area * fill
        if predicted is not None:
            dist = math.hypot(cx - predicted[0], cy - predicted[1])
            if dist > max_jump:
                continue
            score /= 1 + dist / 50
        if score > best_score:
            best_score = score
            best = Detection(frame, t, cx, cy, dmin + EDGE_PX, dmaj + EDGE_PX, ang, int(area))
    return best


def track(frames, bg: np.ndarray, max_jump: float = 200.0, diff_thr: int = 30,
          stats: dict | None = None) -> list[Detection]:
    """Detect the ball in every frame of an iterable of (index, t, bgr),
    gating each search on a constant-velocity prediction."""
    from collections import deque
    bg = smooth(bg)
    hist: deque = deque()                     # (t, smoothed frame)
    dets: list[Detection] = []
    skipped = 0
    for i, t, f in frames:
        f = smooth(f)
        hist.append((t, f))
        # reference frame ~RECENT_S earlier (at least one frame back)
        while len(hist) > 2 and t - hist[1][0] >= RECENT_S:
            hist.popleft()
        recent = _changed(f, hist[0][1], diff_thr) if len(hist) > 1 else None
        if recent is not None and np.count_nonzero(recent) > CAMERA_MOTION_FRAC * recent.size:
            skipped += 1                       # camera moving: nothing is trustworthy
            continue
        pred = None
        if dets and i - dets[-1].frame <= 3:
            a = dets[-1]
            if len(dets) >= 2 and a.frame - dets[-2].frame <= 3:
                b = dets[-2]
                k = (i - a.frame) / (a.frame - b.frame)
                pred = (a.x + (a.x - b.x) * k, a.y + (a.y - b.y) * k)
            else:
                pred = (a.x, a.y)
        d = detect(f, bg, i, t, pred, max_jump if pred else 1e9, recent=recent)
        if d is None and pred is not None:     # lost: retry without gating
            d = detect(f, bg, i, t, None, recent=recent)
            if d and dets and math.hypot(d.x - dets[-1].x, d.y - dets[-1].y) > 3 * max_jump:
                d = None
        if d:
            dets.append(d)
    if stats is not None:
        stats["camera_motion_frames"] = skipped
    return dets


# ---------------------------------------------------------------------------
# Geometry and speed
# ---------------------------------------------------------------------------

@dataclass
class Camera:
    width: int
    height: int
    hfov_deg: float = 66.0   # assumption: Pixel 9a main camera, 16:9 video

    @property
    def f_px(self) -> float:
        return (self.width / 2) / math.tan(math.radians(self.hfov_deg) / 2)

    def to_3d(self, d: Detection) -> tuple[float, float, float]:
        """Camera-frame position in metres from image centre and apparent size."""
        Z = self.f_px * BALL_DIAMETER_M / d.d_minor
        X = (d.x - self.width / 2) * Z / self.f_px
        Y = (d.y - self.height / 2) * Z / self.f_px
        return X, Y, Z


def drop_partials(dets: list[Detection], window: int = 8, frac: float = 0.7) -> list[Detection]:
    """Remove detections that caught only part of the ball. A partial blob is
    always smaller than the true ball (blur lengthens only the major axis), so
    compare each detection's minor axis with the 75th percentile of its
    neighbours within +-window frames, not with the median."""
    if len(dets) < 3:
        return dets
    frames = np.array([d.frame for d in dets])
    sizes = np.array([d.d_minor for d in dets])
    out = []
    for d in dets:
        near = sizes[np.abs(frames - d.frame) <= window]
        if d.d_minor >= frac * np.percentile(near, 75):
            out.append(d)
    return out


def split_segments(dets: list[Detection], angle_thr_deg: float = 35.0,
                   min_len: int = 5, max_gap: int = 3) -> list[list[Detection]]:
    """Split the track into free-flight segments at bounces/impacts (sharp
    changes in direction) and at detection gaps."""
    if len(dets) < 3:
        return [dets] if dets else []
    cuts = {0, len(dets)}
    for i in range(1, len(dets)):
        if dets[i].frame - dets[i - 1].frame > max_gap:
            cuts.add(i)
    for i in range(2, len(dets) - 2):
        v1 = (dets[i].x - dets[i - 2].x, dets[i].y - dets[i - 2].y)
        v2 = (dets[i + 2].x - dets[i].x, dets[i + 2].y - dets[i].y)
        n1, n2 = math.hypot(*v1), math.hypot(*v2)
        if n1 < 4 or n2 < 4:
            continue
        cosang = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
        if math.degrees(math.acos(max(-1, min(1, cosang)))) > angle_thr_deg:
            cuts.add(i)
    cuts = sorted(cuts)
    segs = [dets[a:b] for a, b in zip(cuts, cuts[1:])]
    # A bounce usually triggers several adjacent cuts; the slivers between them
    # fall below min_len and are dropped. Trim one frame off each end of the
    # rest, since frames touching an impact are deformed or mid-turn.
    return [s[1:-1] for s in segs if len(s) >= min_len + 2]


def clean_segment(seg: list[Detection], size_tol: float = 0.3, resid_k: float = 4.0,
                  min_travel_diam: float = 2.0) -> list[Detection] | None:
    """Drop partial detections (size far from the segment median, e.g. a ball
    fragment against a dark background) and position outliers from a smooth
    path fit. Return None if what is left is not a flight."""
    if len(seg) < 5:
        return None
    med = float(np.median([d.d_minor for d in seg]))
    seg = [d for d in seg if abs(d.d_minor - med) <= size_tol * med]
    for _ in range(2):
        if len(seg) < 5:
            return None
        t = np.array([d.t for d in seg]); t = t - t[0]
        keep = np.ones(len(seg), bool)
        for vals in ([d.x for d in seg], [d.y for d in seg]):
            v = np.array(vals)
            r = v - np.polyval(np.polyfit(t, v, 2), t)
            mad = np.median(np.abs(r - np.median(r))) * 1.4826 + 0.5   # px; floor for sub-pixel tracks
            keep &= np.abs(r) <= resid_k * mad
        if keep.all():
            break
        seg = [d for d, k in zip(seg, keep) if k]
    if len(seg) < 5:
        return None
    travel = math.hypot(seg[-1].x - seg[0].x, seg[-1].y - seg[0].y)
    if travel < min_travel_diam * float(np.median([d.d_minor for d in seg])):
        return None          # barely moved: flicker or a static object, not a flight
    return seg


@dataclass
class SegmentSpeed:
    first_frame: int
    last_frame: int
    n: int
    speed_2d: float          # m/s in the image plane (per-frame size scale)
    speed_2d_err: float
    speed_3d: float          # m/s including motion toward/away from camera
    speed_3d_err: float
    v_start_3d: float        # m/s at the segment's first frame (3D fit)
    depth_m: float           # median distance from camera
    blur_ratio: float        # median major/minor
    exposure_ms: float | None  # estimated from blur streak length
    gravity: float | None    # m/s^2 from the vertical fit; ~9.81 if time and scale are right


def _fit_velocity(t: np.ndarray, p: np.ndarray, quad: bool) -> tuple[np.ndarray, np.ndarray]:
    """Fit position vs time; return (velocity at each t, residual std)."""
    deg = 2 if quad and len(t) >= 6 else 1
    coef = np.polyfit(t, p, deg)
    fit = np.polyval(coef, t)
    resid = float(np.std(p - fit)) if len(t) > deg + 1 else 0.0
    vel = np.polyval(np.polyder(coef), t)
    return vel, np.array(resid)


def segment_speed(seg: list[Detection], cam: Camera) -> SegmentSpeed:
    t = np.array([d.t for d in seg])
    t0 = t - t[0]
    # 2D: pixel track, scaled by the per-segment median ball size
    px = np.array([d.x for d in seg])
    py = np.array([d.y for d in seg])
    dmin = np.median([d.d_minor for d in seg])
    m_per_px = BALL_DIAMETER_M / dmin
    vx, rx = _fit_velocity(t0, px, quad=False)
    vy, ry = _fit_velocity(t0, py, quad=True)        # gravity bends y
    s2 = np.hypot(vx, vy) * m_per_px
    # 3D: lift each detection with its own depth; fit each axis
    P = np.array([cam.to_3d(d) for d in seg])
    gravity = None
    if len(t0) >= 10:
        gravity = float(2 * np.polyfit(t0, P[:, 1], 2)[0])   # image y points down
    V = []
    R = []
    for k in range(3):
        v, r = _fit_velocity(t0, P[:, k], quad=(k == 1))
        V.append(v)
        R.append(float(r))
    V = np.array(V)
    s3 = np.linalg.norm(V, axis=0)
    # uncertainty: residual scatter propagated to a slope over the segment span
    span = max(t0[-1], 1e-6)
    n = len(t0)
    slope_err = lambda r: r * math.sqrt(12 / n) / span
    e2 = math.hypot(slope_err(float(rx)), slope_err(float(ry))) * m_per_px
    e3 = math.sqrt(sum(slope_err(r) ** 2 for r in R))
    blur = np.median([d.d_major / d.d_minor for d in seg])
    # streak length (px) / speed (px/s) = exposure time
    speed_px = float(np.mean(np.hypot(vx, vy)))
    streak = np.median([d.d_major - d.d_minor for d in seg])
    exposure = (streak / speed_px * 1000) if speed_px > 50 and streak > 1 else None
    return SegmentSpeed(seg[0].frame, seg[-1].frame, n,
                        float(np.mean(s2)), e2, float(np.mean(s3)), e3, float(s3[0]),
                        float(np.median(P[:, 2])), float(blur), exposure, gravity)
