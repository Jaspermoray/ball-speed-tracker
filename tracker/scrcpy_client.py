"""Minimal scrcpy camera client: launches scrcpy-server directly and yields
encoded video packets with their device PTS.

Protocol verified against scrcpy v5.0.1 (server/.../device/Streamer.java,
DesktopConnection.java, Options.java). The server refuses to run unless the
version argument matches its own exactly, so the version is read from the
bundled scrcpy binary.

Wire format (forward tunnel, video only, send_device_meta=false):
  1 dummy byte
  u32 codec id
  then a sequence of 12-byte headers:
    session packet: u32 flags (MSB=1), u32 width, u32 height
    media packet:   u64 (bit63=0, bit62=config, bit61=key, bits60-0=PTS us), u32 size, payload
"""
from __future__ import annotations

import os
import random
import re
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parent.parent
SCRCPY_VERSION = "5.0.1"   # protocol verified against this release; setup.sh installs it


def find_scrcpy_dir() -> Path:
    """tools/scrcpy (installed by setup.sh), else any tools/scrcpy-* folder
    containing scrcpy, adb and scrcpy-server."""
    tools = ROOT / "tools"
    candidates = [tools / "scrcpy"] + sorted(tools.glob("scrcpy-*"), reverse=True)
    for d in candidates:
        if all((d / n).exists() for n in ("scrcpy", "adb", "scrcpy-server")):
            return d
    return tools / "scrcpy"     # missing: ScrcpyCamera reports it


DEFAULT_SCRCPY_DIR = find_scrcpy_dir()
DEVICE_SERVER_PATH = "/data/local/tmp/scrcpy-server-manual.jar"

CODEC_IDS = {
    0x68323634: "h264",
    0x68323635: "hevc",
    0x00617631: "av1",
}
FLAG_SESSION = 1 << 63
FLAG_CONFIG = 1 << 62
FLAG_KEY = 1 << 61
PTS_MASK = FLAG_KEY - 1


@dataclass
class Session:
    width: int
    height: int


@dataclass
class Packet:
    pts_us: int | None     # None for config packets (SPS/PPS)
    config: bool
    key: bool
    data: bytes
    arrival_ns: int         # time.monotonic_ns() when the payload was fully read


@dataclass
class CameraConfig:
    size: str = "1920x1080"
    fps: int = 240
    high_speed: bool = True
    facing: str = "back"
    codec: str = "h264"
    bit_rate: int = 100_000_000
    log_level: str = "info"


class ScrcpyError(RuntimeError):
    pass


class _StartupClosed(ScrcpyError):
    """Server closed the stream before any video (often: camera still busy)."""


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        r = sock.recv_into(view[got:], n - got)
        if r == 0:
            raise EOFError("stream closed by device")
        got += r
    return bytes(buf)


class ScrcpyCamera:
    def __init__(self, cfg: CameraConfig, scrcpy_dir: Path = DEFAULT_SCRCPY_DIR,
                 serial: str | None = None, verbose: bool = True):
        self.cfg = cfg
        self.dir = Path(scrcpy_dir)
        self.adb = str(self.dir / "adb")
        self.serial = serial
        self.verbose = verbose
        self.scid = random.getrandbits(31)
        self.port: int | None = None
        self.proc: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self.codec: str | None = None
        self._first_hdr: bytes | None = None
        self.version = self._scrcpy_version()

    # ---- adb helpers -------------------------------------------------------
    def _adb(self, *args: str, **kw) -> subprocess.CompletedProcess:
        cmd = [self.adb] + (["-s", self.serial] if self.serial else []) + list(args)
        r = subprocess.run(cmd, capture_output=True, text=True, **kw)
        if r.returncode != 0:
            msg = (r.stderr or r.stdout).strip()
            hint = ""
            if self.serial and ":" in self.serial and "connect failed" in msg:
                hint = ("\n  Wireless adb connected but traffic is blocked. Check both are on the same "
                        "network (try `ping <phone-ip>`); campus/office Wi-Fi often isolates clients.")
            raise ScrcpyError(f"adb {' '.join(args[:1])} failed: {msg}{hint}")
        return r

    def _scrcpy_version(self) -> str:
        if not (self.dir / "scrcpy").exists():
            raise ScrcpyError(f"scrcpy not found in {self.dir}. Run ./setup.sh first.")
        out = subprocess.run([str(self.dir / "scrcpy"), "--version"],
                             capture_output=True, text=True).stdout
        m = re.search(r"scrcpy (\d+\.\d+(?:\.\d+)?)", out)
        if not m:
            raise ScrcpyError(f"could not read scrcpy version from: {out!r}")
        return m.group(1)

    def _log(self, msg: str):
        if self.verbose:
            print(msg, flush=True)

    # ---- lifecycle ---------------------------------------------------------
    def start(self, connect_timeout_s: float = 10.0, attempts: int = 3):
        """Start the server and connect. Retries if the server closes the
        stream during startup, which happens when the camera from a previous
        session has not been released yet."""
        for attempt in range(1, attempts + 1):
            try:
                self._start(connect_timeout_s)
                return
            except _StartupClosed as e:
                self.stop()
                if attempt == attempts:
                    raise ScrcpyError(str(e)) from None
                self._log(f"[client] startup failed (attempt {attempt}), retrying...")
                self.scid = random.getrandbits(31)
                time.sleep(1.5)
            except BaseException:
                self.stop()   # don't leave a server process or adb forward behind
                raise

    def _start(self, connect_timeout_s: float):
        devs = self._adb("devices").stdout
        if "\tdevice" not in devs:
            raise ScrcpyError(f"no authorized device:\n{devs}")

        self._adb("push", str(self.dir / "scrcpy-server"), DEVICE_SERVER_PATH)
        name = f"scrcpy_{self.scid:08x}"
        self.port = int(self._adb("forward", "tcp:0", f"localabstract:{name}").stdout.strip())

        c = self.cfg
        server_args = [
            f"scid={self.scid:08x}", f"log_level={c.log_level}", "tunnel_forward=true",
            "audio=false", "control=false", "cleanup=false", "send_device_meta=false",
            "video_source=camera", f"camera_facing={c.facing}", f"camera_size={c.size}",
            f"camera_fps={c.fps}", f"camera_high_speed={'true' if c.high_speed else 'false'}",
            f"video_codec={c.codec}", f"video_bit_rate={c.bit_rate}",
        ]
        shell_cmd = (f"CLASSPATH={DEVICE_SERVER_PATH} app_process / "
                     f"com.genymobile.scrcpy.Server {self.version} " + " ".join(server_args))
        cmd = [self.adb] + (["-s", self.serial] if self.serial else []) + ["shell", shell_cmd]
        self._log(f"[client] scrcpy {self.version}, scid={self.scid:08x}, port={self.port}")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1)
        threading.Thread(target=self._pump_server_log, daemon=True).start()

        # With a forward tunnel, connect() succeeds even if nothing listens on the
        # device; the dummy byte confirms the server is really there.
        deadline = time.monotonic() + connect_timeout_s
        while True:
            if self.proc.poll() is not None:
                raise ScrcpyError(f"server exited early (code {self.proc.returncode})")
            s = socket.create_connection(("127.0.0.1", self.port))
            try:
                s.settimeout(2.0)
                if s.recv(1) == b"\x00":
                    break
            except (OSError, socket.timeout):
                pass
            s.close()
            if time.monotonic() > deadline:
                raise ScrcpyError("timed out waiting for scrcpy server")
            time.sleep(0.1)

        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        self.sock = s
        # If the phone is asleep or locked, opening the camera stalls and the
        # stream never starts, so time out with a useful message.
        s.settimeout(connect_timeout_s)
        try:
            (codec_id,) = struct.unpack(">I", _recv_exact(s, 4))
            if codec_id == 0:
                raise ScrcpyError("device disabled the video stream")
            if codec_id == 1:
                raise ScrcpyError("device reported a configuration error (see server log)")
            self.codec = CODEC_IDS.get(codec_id)
            if self.codec is None:
                raise ScrcpyError(f"unknown codec id 0x{codec_id:08x}")
            self._first_hdr = _recv_exact(s, 12)
        except socket.timeout:
            raise ScrcpyError("no video from the camera. Is the phone awake and unlocked?") from None
        except EOFError:
            raise _StartupClosed("server closed the stream during startup (see [server] log above; "
                              "'camera limit reached' means something else holds a camera: the Camera app, another session, or Face Unlock on the lock screen. Unlock the phone and retry)") from None
        s.settimeout(None)
        self._log(f"[client] connected, codec={self.codec}")

    def _pump_server_log(self):
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.rstrip()
            if line:
                self._log(line if line.startswith("[server]") else f"[server] {line}")

    def packets(self) -> Iterator[Session | Packet]:
        """Yield Session and Packet objects until the stream closes."""
        assert self.sock is not None, "call start() first"
        s = self.sock
        hdr = self._first_hdr
        while True:
            if hdr is None:
                hdr = _recv_exact(s, 12)
            (first,) = struct.unpack_from(">Q", hdr, 0)
            if first & FLAG_SESSION:
                _, w, h = struct.unpack(">III", hdr)
                hdr = None
                yield Session(w, h)
                continue
            (size,) = struct.unpack_from(">I", hdr, 8)
            hdr = None
            data = _recv_exact(s, size)
            now = time.monotonic_ns()
            config = bool(first & FLAG_CONFIG)
            yield Packet(
                pts_us=None if config else (first & PTS_MASK),
                config=config,
                key=bool(first & FLAG_KEY),
                data=data,
                arrival_ns=now,
            )

    def stop(self):
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)  # wakes a blocked recv
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.proc:
            self._wait_device_server_exit()
            self.proc = None
        if self.port:
            try:
                self._adb("forward", "--remove", f"tcp:{self.port}")
            except ScrcpyError:
                pass
            self.port = None

    def _wait_device_server_exit(self, timeout_s: float = 3.0):
        """The device-side server keeps the camera open until it notices the
        socket closed. Wait for it (matched by our scid) so the next session can
        open the camera; kill it if it lingers."""
        # "[c]" keeps the regex from matching the shell running pgrep itself.
        pattern = f"'[c]om.genymobile.scrcpy.Server.*scid={self.scid:08x}'"
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            r = subprocess.run([self.adb] + (["-s", self.serial] if self.serial else [])
                               + ["shell", f"pgrep -f {pattern}"], capture_output=True, text=True)
            if not r.stdout.strip():
                return
            time.sleep(0.1)
        subprocess.run([self.adb] + (["-s", self.serial] if self.serial else [])
                       + ["shell", f"pkill -f {pattern}"], capture_output=True)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
