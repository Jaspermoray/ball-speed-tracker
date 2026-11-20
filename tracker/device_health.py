"""Poll phone battery / temperature / thermal status over adb in the background.

Android thermal status: 0 none, 1 light, 2 moderate, 3 severe, 4 critical,
5 emergency, 6 shutdown. At >= 2 the camera HAL may start lowering frame rate.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass

THERMAL_NAMES = ["none", "light", "moderate", "severe", "critical", "emergency", "shutdown"]

_CMD = "dumpsys battery; echo @@; dumpsys thermalservice | grep -m1 'Thermal Status'"


@dataclass
class Health:
    t: float                 # time.monotonic() of the sample
    level: int | None        # battery %
    charging: bool | None    # battery status == charging
    plugged: str | None      # "ac", "usb", "wireless", "dock" or None
    temp_c: float | None     # battery temperature
    thermal: int | None      # Android thermal status 0..6

    def short(self) -> str:
        parts = []
        if self.level is not None:
            parts.append(f"batt {self.level}%{'+' if self.charging else '-'}")
        if self.temp_c is not None:
            parts.append(f"{self.temp_c:.1f}C")
        if self.thermal is not None:
            parts.append(f"thermal={THERMAL_NAMES[self.thermal] if self.thermal < 7 else self.thermal}")
        return " ".join(parts)


def parse(out: str, t: float) -> Health:
    def grab(pat, cast=int):
        m = re.search(pat, out, re.M)
        return cast(m.group(1)) if m else None

    plugged = None
    for name in ("AC", "USB", "Wireless", "Dock"):
        if re.search(rf"^\s*{name} powered: true", out, re.M):
            plugged = name.lower()
            break
    status = grab(r"^\s*status: (\d+)")
    temp = grab(r"^\s*temperature: (-?\d+)")
    return Health(
        t=t,
        level=grab(r"^\s*level: (\d+)"),
        charging=None if status is None else status in (2, 5),   # 2 charging, 5 full
        plugged=plugged,
        temp_c=None if temp is None else temp / 10.0,
        thermal=grab(r"Thermal Status: (\d+)"),
    )


class HealthMonitor:
    def __init__(self, adb: str, serial: str | None, interval_s: float = 5.0):
        self.cmd = [adb] + (["-s", serial] if serial else []) + ["shell", _CMD]
        self.interval = interval_s
        self.samples: list[Health] = []
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "HealthMonitor":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def latest(self) -> Health | None:
        with self.lock:
            return self.samples[-1] if self.samples else None

    def _run(self):
        while not self._stop.is_set():
            try:
                r = subprocess.run(self.cmd, capture_output=True, text=True, timeout=10)
                h = parse(r.stdout, time.monotonic())
                with self.lock:
                    self.samples.append(h)
            except (subprocess.SubprocessError, OSError):
                pass
            self._stop.wait(self.interval)

    def summary(self) -> str | None:
        with self.lock:
            s = list(self.samples)
        if not s:
            return None
        first, last = s[0], s[-1]
        mins = (last.t - first.t) / 60
        temps = [h.temp_c for h in s if h.temp_c is not None]
        therm = [h.thermal for h in s if h.thermal is not None]
        out = []
        if first.level is not None and last.level is not None:
            d = last.level - first.level
            rate = f" ({d / mins:+.1f}%/min)" if mins >= 1 else ""
            out.append(f"battery {first.level}% -> {last.level}%{rate}, "
                       f"power: {last.plugged or 'none'}, charging: {last.charging}")
        if temps:
            out.append(f"temperature {temps[0]:.1f}C -> {temps[-1]:.1f}C (max {max(temps):.1f}C)")
        if therm:
            mx = max(therm)
            out.append(f"thermal status max: {THERMAL_NAMES[mx] if mx < 7 else mx}"
                       + ("  << phone was throttling" if mx >= 2 else ""))
        return "\n".join(out)
