#!/usr/bin/env python3
"""Render a synthetic 240 fps clip of a spinning tennis ball with known speed
and spin, for testing the measurement pipeline end to end.

  .venv/bin/python tests/synthetic_ball.py out.mp4 --topspin 2400 --side 600

The ball has the real tennis-ball seam curve, a small dark logo, felt texture,
Lambert shading, motion blur integrated over the exposure, sensor noise and
H.264 compression. Coordinates: x right, y down, z toward the camera.
Topspin (positive) means the top of the ball moves along the flight direction.
"""
from __future__ import annotations

import argparse
import math
import sys
from fractions import Fraction
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tracker  # noqa: F401,E402  (quiet av+cv2 load on macOS)
import av  # noqa: E402
import cv2  # noqa: E402

FPS = 240
BALL_D_M = 0.067


def seam_points(n: int = 600, a: float = 0.75) -> np.ndarray:
    """Tennis-ball seam: x = a cos t + b cos 3t, y = a sin t - b sin 3t,
    z = 2 sqrt(ab) sin 2t, with a + b = 1 (lies exactly on the unit sphere)."""
    b = 1 - a
    t = np.linspace(0, 2 * np.pi, n, endpoint=False)
    P = np.stack([a * np.cos(t) + b * np.cos(3 * t),
                  a * np.sin(t) - b * np.sin(3 * t),
                  2 * math.sqrt(a * b) * np.sin(2 * t)], 1)
    return P / np.linalg.norm(P, axis=1, keepdims=True)


def rotate(P: np.ndarray, w: np.ndarray) -> np.ndarray:
    th = np.linalg.norm(w)
    if th < 1e-12:
        return P.copy()
    k = w / th
    return (P * math.cos(th) + np.cross(k, P) * math.sin(th)
            + np.outer(P @ k, k) * (1 - math.cos(th)))


class Ball:
    def __init__(self, seed: int = 1):
        rng = np.random.default_rng(seed)
        self.seam = seam_points()
        self.logo = np.array([0.3, -0.5, 0.81])
        self.logo /= np.linalg.norm(self.logo)
        self.fk = rng.normal(size=(12, 3)) * 9           # felt texture frequencies
        self.fp = rng.uniform(0, 2 * np.pi, 12)
        self.light = np.array([-0.4, -0.6, 0.7])
        self.light /= np.linalg.norm(self.light)

    def shade(self, n_cam: np.ndarray, m_ball: np.ndarray) -> np.ndarray:
        """BGR colour (N x 3, 0-255) for camera-frame normals n_cam whose
        ball-frame directions are m_ball."""
        felt = np.array([40.0, 228.0, 205.0])
        seam_c = np.array([205.0, 238.0, 238.0])
        logo_c = np.array([40.0, 70.0, 70.0])
        col = np.tile(felt, (len(n_cam), 1))
        col *= (1 + 0.05 * np.sin(m_ball @ self.fk.T + self.fp).mean(1))[:, None]
        seam_cos = (m_ball @ self.seam.T).max(1)
        sw = np.clip((seam_cos - math.cos(0.075)) / (math.cos(0.045) - math.cos(0.075)), 0, 1)
        col = col * (1 - sw[:, None]) + seam_c * sw[:, None]
        lw = (m_ball @ self.logo > math.cos(0.16)).astype(float)
        col = col * (1 - lw[:, None]) + logo_c * lw[:, None]
        lam = 0.45 + 0.55 * np.clip(n_cam @ self.light, 0, 1)
        return col * lam[:, None]


def background(w: int, h: int, seed: int = 2) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    bg = np.stack([90 + 40 * xx / w, 100 + 30 * yy / h, 110 + 20 * (1 - xx / w)], -1)
    for _ in range(14):
        x0, y0 = rng.integers(0, w - 200), rng.integers(0, h - 150)
        bg[y0:y0 + rng.integers(40, 150), x0:x0 + rng.integers(40, 200)] = rng.integers(40, 200, 3)
    return bg.astype(np.float32)


def render(out: str, speed_mps: float, topspin: float, side: float, rifle: float,
           diameter_px: float = 36, exposure_ms: float = 1.5, angle_deg: float = 0.0,
           width: int = 1920, height: int = 1080, noise: float = 2.0, seconds: float | None = None,
           lead_s: float = 0.0):
    """lead_s: seconds of empty background before the ball enters (the live
    tracker needs ~1 s of background before it can detect a shot)."""
    r = diameter_px / 2
    px_per_m = diameter_px / BALL_D_M
    g_px = 9.81 * px_per_m
    a = math.radians(angle_deg)
    v = np.array([math.cos(a), -math.sin(a)]) * speed_mps * px_per_m   # px/s, flying right
    f = np.array([v[0], v[1], 0.0]) / np.linalg.norm(v)
    c = np.array([0.0, 0.0, 1.0])
    p = np.cross(c, f)
    up = np.array([0.0, -1.0, 0.0]); up -= f * (up @ f); up /= np.linalg.norm(up)
    top_sign = np.sign(np.cross(c, up) @ f)
    w = (topspin * top_sign * c + side * p + rifle * f) * 2 * math.pi / 60   # rad/s
    x0, y0 = 60.0, height * 0.45
    dur = seconds or (width - 120) / abs(v[0])
    lead = int(lead_s * FPS)
    nframes = int(dur * FPS) + lead
    ball, bg = Ball(), background(width, height)
    rng = np.random.default_rng(3)
    sub = 8

    c_out = av.open(out, "w")
    st = c_out.add_stream("libx264" if "libx264" in av.codecs_available else "h264", rate=FPS)
    st.width, st.height, st.pix_fmt = width, height, "yuv420p"
    st.time_base = Fraction(1, 90000)
    st.options = {"crf": "14"}
    for k in range(nframes):
        acc = np.zeros_like(bg)
        for s in range(sub):
            t = (k - lead) / FPS + (s + 0.5) / sub * exposure_ms / 1000
            cx = x0 + v[0] * t
            cy = y0 + v[1] * t + 0.5 * g_px * t * t
            img = bg.copy()
            xa, xb = int(cx - r - 2), int(cx + r + 3)
            ya, yb = int(cy - r - 2), int(cy + r + 3)
            if t >= 0 and xb > 0 and xa < width and yb > 0 and ya < height:
                xa, ya = max(xa, 0), max(ya, 0)
                xb, yb = min(xb, width), min(yb, height)
                yy, xx = np.mgrid[ya:yb, xa:xb].astype(np.float32)
                u, vv = (xx + 0.5 - cx) / r, (yy + 0.5 - cy) / r
                rho2 = u * u + vv * vv
                alpha = np.clip((1 - np.sqrt(rho2)) * r + 0.5, 0, 1)
                sel = alpha > 0
                n = np.stack([u[sel], vv[sel], np.sqrt(np.clip(1 - rho2[sel], 0, 1))], 1)
                m = rotate(n, -w * t)                         # camera -> ball frame
                col = ball.shade(n, m)
                patch = img[ya:yb, xa:xb]
                al = alpha[sel][:, None]
                patch[sel] = patch[sel] * (1 - al) + col * al
            acc += img
        frame = acc / sub + rng.normal(0, noise, acc.shape)
        vf = av.VideoFrame.from_ndarray(np.clip(frame, 0, 255).astype(np.uint8), format="bgr24")
        vf.pts = round(k * 90000 / FPS)
        vf.time_base = st.time_base
        for pkt in st.encode(vf):
            c_out.mux(pkt)
    for pkt in st.encode():
        c_out.mux(pkt)
    c_out.close()
    total = math.hypot(math.hypot(topspin, side), rifle)
    return {"frames": nframes, "speed_mph": speed_mps * 2.2369363, "rpm": total,
            "topspin": topspin, "side": side, "rifle": rifle}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--speed", type=float, default=20.0, help="m/s")
    ap.add_argument("--topspin", type=float, default=2400, help="rpm (+top, -back)")
    ap.add_argument("--side", type=float, default=0, help="rpm")
    ap.add_argument("--rifle", type=float, default=0, help="rpm")
    ap.add_argument("--diameter", type=float, default=36, help="ball diameter, px")
    ap.add_argument("--exposure", type=float, default=1.5, help="ms")
    ap.add_argument("--angle", type=float, default=0, help="launch angle, degrees up")
    ap.add_argument("--lead", type=float, default=0, help="seconds of empty background first")
    args = ap.parse_args()
    info = render(args.out, args.speed, args.topspin, args.side, args.rifle,
                  args.diameter, args.exposure, args.angle, lead_s=args.lead)
    print(info)


if __name__ == "__main__":
    main()
