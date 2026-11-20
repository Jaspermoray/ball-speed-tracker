"""Ball spin (RPM and axis) from the ball's surface pattern.

Model: the ball is a sphere seen orthographically (it is small relative to its
distance). In each frame the ball image is resampled into a normalised patch:
centre at the ellipse-fit centre, radius from the minor axis. Each patch pixel
(u, v) inside the disc is a point on the visible hemisphere,
p = (u, v, sqrt(1 - u^2 - v^2)), with x right, y down and z toward the camera.

A constant spin w (rad/s, a rotation vector) carries a surface point p at time
t_i to q = Rot(w * (t_j - t_i)) p at time t_j. So patch j sampled at q should
match patch i at p, wherever both are on the visible side. The estimator finds
the w that maximises the mean masked normalised cross-correlation over many
frame pairs (gaps of 1-3 frames). Mixing gaps rejects answers that only fit one
gap: aliasing, and the seam's own 180-degree symmetries.

The search is a coarse grid over all rotation vectors up to max_rpm, on small
patches, then a local grid refinement on larger patches.

The texture signal is high-passed luminance: seams are light, logos dark, and
the high-pass removes the shading, which stays fixed relative to the camera
rather than turning with the ball.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from tracker.ball import Detection

RPM_PER_RADS = 60 / (2 * math.pi)


# ---------------------------------------------------------------------------
# Patches
# ---------------------------------------------------------------------------

def ball_patch(bgr: np.ndarray, d: Detection, R: int, ox: float = 0.0, oy: float = 0.0) -> np.ndarray:
    """(2R+1)^2 float32 patch of the ball, ball radius = R pixels. (ox, oy) is
    the image's offset if bgr is a crop of the full frame."""
    r = d.d_minor / 2
    s = r / R
    cx, cy = d.x - ox, d.y - oy
    # patch (u, v) -> image (cx + (u - R) * s, cy + (v - R) * s)
    M = np.float32([[s, 0, cx - R * s], [0, s, cy - R * s]])
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    p = cv2.warpAffine(g, M, (2 * R + 1, 2 * R + 1),
                       flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                       borderMode=cv2.BORDER_REPLICATE).astype(np.float32)
    return p


def remove_static(patches: list[np.ndarray], R: int) -> list[np.ndarray]:
    """Divide out everything that does not turn with the ball.

    Shading (light from one side), the darker rim and the motion-blur smear stay
    fixed relative to the camera, and they dominate a naive frame-to-frame
    match, which then prefers "no spin". Over a flight the spinning surface
    pattern averages out, so the per-pixel mean of the patches is the static
    part, and image = albedo x shading means dividing by it leaves the
    rotating pattern."""
    yy, xx = np.mgrid[-R:R + 1, -R:R + 1] / R
    inside = xx ** 2 + yy ** 2 <= 0.9 ** 2
    norm = [p / max(float(np.median(p[inside])), 1.0) for p in patches]   # exposure changes
    static = np.mean(norm, axis=0)
    static = cv2.GaussianBlur(static, (0, 0), 0.7)
    return [n / np.maximum(static, 1e-3) for n in norm]


def texture(patch: np.ndarray, R: int) -> np.ndarray:
    """High-passed, normalised texture inside the disc (0 outside)."""
    sigma = max(1.0, R / 4)
    hp = patch - cv2.GaussianBlur(patch, (0, 0), sigma)
    yy, xx = np.mgrid[-R:R + 1, -R:R + 1] / R
    inside = xx ** 2 + yy ** 2 <= 0.9 ** 2
    v = hp[inside]
    hp = (hp - v.mean()) / (v.std() + 1e-6)
    hp[~inside] = 0
    return hp.astype(np.float32)


class _Grid:
    """Sphere points for the pixels of a (2R+1)^2 patch, within rho_max."""

    def __init__(self, R: int, rho_max: float = 0.85):
        self.R = R
        yy, xx = np.mgrid[-R:R + 1, -R:R + 1].astype(np.float32) / R
        rho2 = xx ** 2 + yy ** 2
        sel = rho2 <= rho_max ** 2
        self.idx = np.flatnonzero(sel)
        z = np.sqrt(np.clip(1 - rho2[sel], 0, None))
        self.P = np.stack([xx[sel], yy[sel], z], axis=1)   # N x 3


# ---------------------------------------------------------------------------
# Rotation + matching
# ---------------------------------------------------------------------------

def _rotate(P: np.ndarray, W: np.ndarray) -> np.ndarray:
    """Rotate points P (N x 3) by each rotation vector in W (B x 3) -> B x N x 3."""
    th = np.linalg.norm(W, axis=1)                         # B
    k = W / np.maximum(th, 1e-12)[:, None]                 # B x 3
    c, s = np.cos(th)[:, None, None], np.sin(th)[:, None, None]
    kP = k @ P.T                                           # B x N  (k . p)
    kxP = np.cross(k[:, None, :], P[None, :, :])           # B x N x 3
    return P[None] * c + kxP * s + k[:, None, :] * kP[..., None] * (1 - c)


def _sample(A: np.ndarray, X: np.ndarray, Y: np.ndarray) -> np.ndarray:
    """Bilinear sample of 2-D array A at float pixel coords."""
    h, w = A.shape
    X = np.clip(X, 0, w - 1.001)
    Y = np.clip(Y, 0, h - 1.001)
    x0 = X.astype(np.int32)
    y0 = Y.astype(np.int32)
    fx = X - x0
    fy = Y - y0
    a = A[y0, x0]
    b = A[y0, x0 + 1]
    c = A[y0 + 1, x0]
    d = A[y0 + 1, x0 + 1]
    return (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy


def _pair_scores_q(Ti: np.ndarray, Tj: np.ndarray, grid: _Grid, Q: np.ndarray,
                   valid: np.ndarray, min_valid: float = 0.25) -> tuple[np.ndarray, np.ndarray]:
    """Masked NCC between texture Ti at p and Tj at q (Q: B x N x 3 rotated
    points, valid: B x N visibility). Returns (ncc B, weight B)."""
    R = grid.R
    a = Ti.ravel()[grid.idx]                               # N
    b = _sample(Tj, Q[..., 0] * R + R, Q[..., 1] * R + R)  # B x N
    n = valid.sum(1)
    n_safe = np.maximum(n, 1)
    ma = (valid * a).sum(1) / n_safe
    mb = (valid * b).sum(1) / n_safe
    da = (a[None] - ma[:, None]) * valid
    db = (b - mb[:, None]) * valid
    ncc = (da * db).sum(1) / np.sqrt((da * da).sum(1) * (db * db).sum(1) + 1e-9)
    w = np.where(n >= min_valid * len(a), n, 0.0)
    return ncc, w


def _score(W: np.ndarray, pairs, grid: _Grid, batch: int = 2048) -> np.ndarray:
    """Weighted mean NCC over frame pairs for candidate spins W (rad/s).
    Pairs with (nearly) the same time gap share one rotation of the sphere
    points, which is the expensive part."""
    groups: list[tuple[float, list]] = []
    for Ti, Tj, dt in pairs:
        for g in groups:
            if abs(g[0] - dt) <= 0.01 * dt:
                g[1].append((Ti, Tj))
                break
        else:
            groups.append((dt, [(Ti, Tj)]))
    out = np.empty(len(W), np.float32)
    for s0 in range(0, len(W), batch):
        Wb = W[s0:s0 + batch]
        num = np.zeros(len(Wb))
        den = np.zeros(len(Wb))
        for dt, members in groups:
            Q = _rotate(grid.P, Wb * dt)                   # B x N x 3
            valid = (Q[..., 2] > 0.2).astype(np.float32)   # still on the visible side
            for Ti, Tj in members:
                ncc, w = _pair_scores_q(Ti, Tj, grid, Q, valid)
                num += ncc * w
                den += w
        out[s0:s0 + batch] = num / np.maximum(den, 1e-9)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class SpinResult:
    rpm: float
    rpm_err: float | None
    axis: tuple[float, float, float]   # unit vector, camera coords (x right, y down, z toward camera)
    topspin_rpm: float                 # + topspin, - backspin (axis across the flight, facing the camera)
    sidespin_rpm: float                # axis in the image plane, across the flight (e.g. slice)
    rifle_rpm: float                   # axis along the flight
    score: float                       # mean NCC at the best spin (texture match quality)
    margin: float                      # best minus the best clearly different alternative
    n_pairs: int
    reliable: bool

    def label(self) -> str:
        kind = "topspin" if self.topspin_rpm >= 0 else "backspin"
        return (f"{self.rpm:.0f} rpm ({abs(self.topspin_rpm):.0f} {kind}, "
                f"{abs(self.sidespin_rpm):.0f} side, {abs(self.rifle_rpm):.0f} rifle; "
                f"match {self.score:.2f}, margin {self.margin:.2f}"
                + ("" if self.reliable else ", UNRELIABLE") + ")")


def _make_pairs(textures: list[np.ndarray], frames: list[int], times: list[float],
                gaps=(1, 2, 3), max_pairs: int | None = None):
    pairs = []
    for i in range(len(textures)):
        for j in range(i + 1, min(len(textures), i + 4)):
            if frames[j] - frames[i] in gaps:
                pairs.append((textures[i], textures[j], times[j] - times[i]))
    if max_pairs and len(pairs) > max_pairs:
        sel = np.linspace(0, len(pairs) - 1, max_pairs).round().astype(int)
        pairs = [pairs[k] for k in sel]
    return pairs


def estimate_spin(patches: list[np.ndarray], dets: list[Detection], flight_vel: tuple[float, float],
                  max_rpm: float = 6000, coarse_R: int = 8, fine_R: int = 20,
                  min_score: float = 0.25, min_margin: float = 0.04) -> SpinResult | None:
    """patches: raw grayscale ball patches of radius fine_R (from ball_patch) for
    the detections in one flight segment, in time order. flight_vel: image-plane
    velocity (px/s), used to name the spin components."""
    if len(patches) < 4:
        return None
    frames = [d.frame for d in dets]
    times = [d.t for d in dets]
    dt_med = float(np.median(np.diff(times)))

    flat = remove_static(patches, fine_R)
    fine = [texture(p, fine_R) for p in flat]
    coarse = [texture(cv2.resize(p, (2 * coarse_R + 1, 2 * coarse_R + 1),
                                 interpolation=cv2.INTER_AREA), coarse_R) for p in flat]
    mid_R = 12
    mid = [texture(cv2.resize(p, (2 * mid_R + 1, 2 * mid_R + 1),
                              interpolation=cv2.INTER_AREA), mid_R) for p in flat]
    pairs_c = _make_pairs(coarse, frames, times, max_pairs=12)
    pairs_f = _make_pairs(fine, frames, times, max_pairs=40)
    if len(pairs_c) < 3:
        return None
    gc, gm, gf = _Grid(coarse_R), _Grid(mid_R), _Grid(fine_R)

    # coarse: lattice of per-frame rotation vectors within max_rpm
    wmax = max_rpm / RPM_PER_RADS * dt_med                 # rad per frame
    h = 0.15
    n = int(wmax / h)
    ax = np.arange(-n, n + 1) * h
    L = np.stack(np.meshgrid(ax, ax, ax, indexing="ij"), -1).reshape(-1, 3)
    L = L[np.linalg.norm(L, axis=1) <= wmax + 1e-9]
    W = L / dt_med                                         # rad/s
    sc = _score(W, pairs_c, gc)

    # keep several distinct coarse peaks...
    order = np.argsort(sc)[::-1]
    seeds = []
    for k in order:
        if all(np.linalg.norm(W[k] - W[s]) * dt_med > 0.45 for s in seeds):
            seeds.append(k)
        if len(seeds) == 8:
            break
    seed_w = [W[k] for k in seeds]
    # ...plus the best pure topspin/backspin (axis toward the camera): the most
    # common case, and a cheap 1-D scan on the fine patches that the coarse
    # 3-D grid can step over at high spin rates.
    zr = np.arange(-max_rpm, max_rpm + 1, 25.0) / RPM_PER_RADS
    zc = np.stack([np.zeros_like(zr), np.zeros_like(zr), zr], 1)
    pairs_mid = _make_pairs(mid, frames, times, max_pairs=16)    # mid-size patches: for seeds
    seed_w.append(zc[int(np.argmax(_score(zc, pairs_mid, gm)))])

    def refine(w0: np.ndarray, pairs, grid: _Grid, iters: int = 6,
               step0: float = h) -> tuple[np.ndarray, float]:
        best, best_s = w0, -2.0
        step = step0 / dt_med
        offs = np.stack(np.meshgrid(*[np.arange(-2, 3)] * 3, indexing="ij"), -1).reshape(-1, 3)
        for _ in range(iters):
            cand = best + offs * step / 2
            s = _score(cand, pairs, grid)
            k = int(np.argmax(s))
            best, best_s = cand[k], float(s[k])
            step /= 2
        return best, best_s

    wlim = max_rpm * 1.02 / RPM_PER_RADS
    refined = [r for r in (refine(w0, pairs_mid, gm, iters=4) for w0 in seed_w)
               if np.linalg.norm(r[0]) <= wlim]
    if not refined:
        return None
    # Look-alikes: around some axes the seam pattern repeats after a half turn,
    # so a faster spin can fit about as well as the true one. Rescore the
    # candidates on all pairs and, among near-ties, take the slowest.
    full = _score(np.array([v for v, _ in refined]), pairs_f, gf)
    near_best = [k for k in range(len(refined)) if full[k] >= full.max() - 0.02]
    pick = min(near_best, key=lambda k: np.linalg.norm(refined[k][0]))
    w, score = refine(refined[pick][0], pairs_f, gf, iters=3, step0=h / 8)
    if np.linalg.norm(w) > wlim:
        w, score = refined[pick][0], float(full[pick])
    # margin: best vs the best clearly different candidate, all scored on the same pairs
    others = np.array([v for k, (v, _) in enumerate(refined)
                       if k != pick and np.linalg.norm(v - w) * dt_med > 0.3])
    alt = float(_score(others, pairs_f, gf).max()) if len(others) else -1.0
    margin = score - alt

    # uncertainty: refine on two halves of the pairs and compare
    err = None
    if len(pairs_f) >= 8:
        a, _ = refine(w, pairs_f[0::2], gf, iters=3, step0=h / 4)
        b, _ = refine(w, pairs_f[1::2], gf, iters=3, step0=h / 4)
        err = float(np.linalg.norm(a - b) / 2 * RPM_PER_RADS)

    # name the components relative to the flight direction (image plane)
    rpm = float(np.linalg.norm(w) * RPM_PER_RADS)
    vx, vy = flight_vel
    nv = math.hypot(vx, vy) or 1.0
    f = np.array([vx / nv, vy / nv, 0.0])                  # flight direction
    c = np.array([0.0, 0.0, 1.0])                          # toward the camera
    p = np.cross(c, f)                                     # in image plane, across the flight
    up = np.array([0.0, -1.0, 0.0])
    up = up - f * (up @ f)
    up /= np.linalg.norm(up) or 1.0
    # topspin: the top of the ball (spin velocity w x up) moves along the flight
    top_sign = float(np.sign(np.cross(c, up) @ f)) or 1.0
    topspin = float(w @ c) * top_sign * RPM_PER_RADS
    side = float(w @ p) * RPM_PER_RADS
    rifle = float(w @ f) * RPM_PER_RADS
    axis = tuple(float(x) for x in (w / (np.linalg.norm(w) or 1.0)))
    reliable = (score >= min_score and margin >= min_margin
                and (err is None or err <= max(100.0, 0.08 * rpm)))
    return SpinResult(rpm, err, axis, topspin, side, rifle, score, margin, len(pairs_f), reliable)
