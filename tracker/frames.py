"""Decoding, the in-memory ring buffer, and the on-disk recording format.

Recording format (a directory):
  stream.h264   raw Annex-B H.264 exactly as received (config packets included)
  packets.csv   one row per packet, in stream order:
                pts_us,config,key,size,arrival_ns
                (pts_us is empty for config packets)
Use iter_recording() to get (pts_us, gray_frame) back from a recording.
"""
from __future__ import annotations

import csv
import threading
from pathlib import Path
from typing import Iterator

import av
import numpy as np


def y_plane(frame: av.VideoFrame) -> np.ndarray:
    """Copy the luma plane of a decoded frame as an (h, w) uint8 array."""
    p = frame.planes[0]
    a = np.frombuffer(p, np.uint8).reshape(frame.height, p.line_size)
    return a[:, : frame.width].copy()


class Decoder:
    """Wraps a PyAV codec context. Config packets are held and prepended to the
    next media packet, as the scrcpy client does."""

    def __init__(self, codec: str = "h264", threads: int = 0):
        """threads: 0 = FFmpeg default (all cores, highest throughput but
        frame threading adds about one frame of latency per thread);
        1 = single-threaded; N = N threads."""
        self.ctx = av.CodecContext.create(codec, "r")
        if threads != 1:
            self.ctx.thread_type = "AUTO"
            self.ctx.thread_count = threads
        self._pending_config = b""

    def decode(self, data: bytes, pts_us: int | None, config: bool) -> list[av.VideoFrame]:
        if config:
            self._pending_config += data
            return []
        if self._pending_config:
            data = self._pending_config + data
            self._pending_config = b""
        pkt = av.Packet(data)
        pkt.pts = pts_us
        return list(self.ctx.decode(pkt))

    def flush(self) -> list[av.VideoFrame]:
        try:
            return list(self.ctx.decode(None))
        except av.error.EOFError:
            return []

    def close(self):
        """Flush (if not already) and release the codec context."""
        if self.ctx is not None:
            self.flush()
            self.ctx = None


class RingBuffer:
    """Fixed-size buffer of the most recent gray frames and their PTS."""

    def __init__(self, capacity: int, height: int, width: int):
        self.capacity = capacity
        self.frames = np.empty((capacity, height, width), np.uint8)
        self.pts_us = np.full(capacity, -1, np.int64)
        self.count = 0          # total frames ever written
        self.lock = threading.Lock()

    @property
    def shape(self):
        return self.frames.shape[1:]

    def push(self, pts_us: int, gray: np.ndarray):
        i = self.count % self.capacity
        with self.lock:
            self.frames[i] = gray
            self.pts_us[i] = pts_us
            self.count += 1

    def latest(self) -> tuple[int, np.ndarray] | None:
        with self.lock:
            if self.count == 0:
                return None
            i = (self.count - 1) % self.capacity
            return int(self.pts_us[i]), self.frames[i].copy()

    def window(self, t0_us: int, t1_us: int) -> tuple[np.ndarray, np.ndarray]:
        """Copies of all buffered frames with t0 <= pts <= t1, in PTS order."""
        with self.lock:
            n = min(self.count, self.capacity)
            idx = (np.arange(self.count - n, self.count)) % self.capacity
            pts = self.pts_us[idx]
            sel = idx[(pts >= t0_us) & (pts <= t1_us)]
            return self.pts_us[sel].copy(), self.frames[sel].copy()


class Recorder:
    def __init__(self, out_dir: Path):
        self.dir = Path(out_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.h264 = open(self.dir / "stream.h264", "wb")
        self._csv_f = open(self.dir / "packets.csv", "w", newline="")
        self.csv = csv.writer(self._csv_f)
        self.csv.writerow(["pts_us", "config", "key", "size", "arrival_ns"])

    def write(self, pts_us, config, key, data, arrival_ns):
        self.h264.write(data)
        self.csv.writerow(["" if pts_us is None else pts_us, int(config), int(key),
                           len(data), arrival_ns])

    def close(self):
        self.h264.close()
        self._csv_f.close()


def read_packets_csv(rec_dir: Path) -> list[dict]:
    with open(Path(rec_dir) / "packets.csv", newline="") as f:
        return list(csv.DictReader(f))


def iter_recording(rec_dir: Path) -> Iterator[tuple[int, np.ndarray]]:
    """Yield (pts_us, gray_frame) from a directory written by Recorder."""
    rec_dir = Path(rec_dir)
    rows = read_packets_csv(rec_dir)
    dec = Decoder()
    with open(rec_dir / "stream.h264", "rb") as f:
        for r in rows:
            data = f.read(int(r["size"]))
            cfg = r["config"] == "1"
            pts = None if cfg else int(r["pts_us"])
            for fr in dec.decode(data, pts, cfg):
                yield fr.pts, y_plane(fr)
    for fr in dec.flush():
        yield fr.pts, y_plane(fr)
