"""Big-number shot display: a tiny local web server that pushes shot results
to a full-screen browser page (display/index.html) via Server-Sent Events.

In-process use (from the tracker):
    from tracker.display import DisplayServer
    disp = DisplayServer(port=8765).start()
    disp.publish(speed_mps=50.2, rpm=2800, speed_err_mps=0.4)

From another process / the shell:
    POST http://localhost:8765/shot  {"speed_mps": 50.2, "rpm": 2800}
    (see send_shot.py)

Any device on the same network can open http://<laptop-ip>:8765/ as a display.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "display" / "index.html"


class DisplayServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 8765):
        self.host, self.port = host, port
        self._clients: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._history: list[dict] = []   # most recent last
        self._next_id = 1
        self._httpd: ThreadingHTTPServer | None = None
        # live camera preview (motion JPEG)
        self._preview: bytes | None = None
        self._preview_seq = 0
        self._preview_cond = threading.Condition()

    # ---- public API ---------------------------------------------------------
    def start(self) -> "DisplayServer":
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep the console quiet
                pass

            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    body = PAGE.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/events":
                    server._serve_events(self)
                elif self.path.split("?")[0] == "/video.mjpg":
                    server._serve_mjpeg(self)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)

            def do_POST(self):
                if self.path != "/shot":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    data = json.loads(self.rfile.read(n) or b"{}")
                    shot = server.publish(**{k: data.get(k) for k in
                                             ("speed_mps", "rpm", "speed_err_mps", "rpm_err")})
                except (ValueError, TypeError) as e:
                    self.send_error(HTTPStatus.BAD_REQUEST, str(e))
                    return
                body = json.dumps(shot).encode()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        ThreadingHTTPServer.daemon_threads = True
        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def publish(self, speed_mps: float | None, rpm: float | None = None,
                speed_err_mps: float | None = None, rpm_err: float | None = None) -> dict:
        """Send a shot result to every open display. Speed in m/s, spin in RPM.
        Pass None for anything not measured."""
        def num(v):
            return None if v is None else float(v)
        with self._lock:
            shot = {"id": self._next_id, "t": time.time() * 1000,
                    "speed_mps": num(speed_mps), "speed_err_mps": num(speed_err_mps),
                    "rpm": num(rpm), "rpm_err": num(rpm_err)}
            self._next_id += 1
            self._history = (self._history + [shot])[-20:]
            for q in self._clients:
                q.put(shot)
        return shot

    def set_preview(self, jpeg: bytes):
        """Publish the latest camera preview frame (JPEG bytes)."""
        with self._preview_cond:
            self._preview = jpeg
            self._preview_seq += 1
            self._preview_cond.notify_all()

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()

    # ---- MJPEG ------------------------------------------------------------------
    def _serve_mjpeg(self, h: BaseHTTPRequestHandler):
        if self._preview is None:
            h.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "no camera preview")
            return
        h.send_response(HTTPStatus.OK)
        h.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        seen = -1
        try:
            while True:
                with self._preview_cond:
                    self._preview_cond.wait_for(lambda: self._preview_seq != seen, timeout=5)
                    if self._preview_seq == seen:
                        return            # source stopped; the page will retry
                    seen, jpg = self._preview_seq, self._preview
                h.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                              + f"Content-Length: {len(jpg)}\r\n\r\n".encode() + jpg + b"\r\n")
                h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ---- SSE ------------------------------------------------------------------
    def _serve_events(self, h: BaseHTTPRequestHandler):
        h.send_response(HTTPStatus.OK)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-store")
        h.send_header("Connection", "keep-alive")
        h.end_headers()
        q: queue.Queue = queue.Queue()
        with self._lock:
            backlog = list(self._history)
            self._clients.add(q)
        try:
            # Replay recent shots so a freshly opened page shows the history.
            h.wfile.write(f"event: history\ndata: {json.dumps(backlog)}\n\n".encode())
            h.wfile.flush()
            while True:
                try:
                    shot = q.get(timeout=15)
                    h.wfile.write(f"event: shot\ndata: {json.dumps(shot)}\n\n".encode())
                except queue.Empty:
                    h.wfile.write(b": keepalive\n\n")
                h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with self._lock:
                self._clients.discard(q)
