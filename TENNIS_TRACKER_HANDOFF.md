# Tennis ball speed + RPM tracker: session handoff

Read this first, then start at "Next steps". Written 2026-11-18.

## Goal

Stream live 240 fps video from a **Pixel 9a** to a **MacBook (macOS)** and analyze it with custom software to extract tennis ball **speed** and, ideally, **spin (RPM)**.

## Current status (updated 2026-11-18)

- **Steps 1–3 are done and tested on the real phone.** Next is step 4 (speed).
- Pixel 9a (Android 17), back camera `0`: the only high-speed size is `1920x1080` at 120 or 240 fps. It runs at a steady **238.2 fps** (median gap 4.198 ms), not exactly 240, so always use PTS.
- scrcpy is **v5.0.1**, not v4. Its stream layout matches the v4.0 notes below, checked against the v5.0.1 `Streamer.java`, `DesktopConnection.java` and `Options.java`. PTS values are in microseconds.
- No Homebrew. scrcpy and adb come from the GitHub release in `tools/scrcpy-macos-aarch64-v5.0.1/`, and Python packages are in `.venv/` (av, numpy, pillow, opencv-python).
- Frames arrive rotated 90° (sensor orientation). Indoor test frames show motion blur, so shutter speed is still the top risk.

### Packaging (2026-11-20)

- The repo is set up for open source: `README.md` (user directions), MIT `LICENSE` (copyright name still a placeholder), `requirements.txt` and `.gitignore`. The `.gitignore` excludes videos (Pixel clips carry GPS metadata), recordings, analysis output, `.venv` and `tools/`.
- `setup.sh`: Python 3.10+ check, `.venv`, pip install, and download of scrcpy 5.0.1 for macOS arm64/x86_64 or Linux x86_64 with SHA-256 check, into `tools/scrcpy`. Idempotent; verified from a clean copy.
- `start.sh` → `start.py`: finds the phone (network device, then USB, then `.last_phone_ip`, then macOS Internet Sharing leases in `/var/db/dhcpd_leases`), handles an unauthorized device, warns if Stay awake is off, waits for unlock (Face Unlock), then execs `live.py --open --serial ...`. Also `--enable-network` (adb tcpip over USB) and `--replay clip.mp4`. Unknown flags pass through to live.py.
- `tracker/__init__.py` loads PyAV and OpenCV with stderr muted, to hide the harmless macOS duplicate libavdevice objc warning; entry scripts `import tracker` first. opencv-python-headless bundles the same dylib, so switching packages doesn't help.
- `scrcpy_client.find_scrcpy_dir()` looks in `tools/scrcpy` first, then `tools/scrcpy-*`.
- git was initialized (branch main); nothing committed yet.

### Code

| File | What it does |
|---|---|
| `receiver.py` | Live client: stats every second, `--preview` (r rotates, q quits), `--save DIR`, `--duration`, `--decode-threads` |
| `tracker/scrcpy_client.py` | Launches the server, opens the forward tunnel, parses the protocol, yields packets with PTS |
| `tracker/frames.py` | PyAV decoder, gray `RingBuffer` (`window(t0_us, t1_us)`), `Recorder`, `iter_recording()` for offline work |
| `analyze_pts.py` | Frame-gap report for an MP4 or a recording folder |
| `link_setup.py` | Network link setup: finds the phone's `eth*` IP over USB, runs `adb tcpip 5555`, pings, connects and verifies. Diagnoses missing hub, missing DHCP and blocked traffic. `--iface ncm0` for other links, `--ip` when there's no USB |
| `tracker/device_health.py` | Polls the phone's battery %, charging, temperature and Android thermal status every 5 s; receiver.py prints them and summarizes at the end (`--no-health` to disable) |
| `tracker/ball.py` | Classical ball detector: static-background difference AND optic-yellow HSV mask, ellipse fit (sub-pixel centre; minor axis = true diameter, major = diameter + blur), edge-clipped blobs rejected, gated tracking, bounce splitting, per-segment speed fits, gravity check |
| `analyze_clip.py` | Runs the detector on an MP4 (handles phone slow-motion files via `com.android.capture.fps`) and writes `analysis/<clip>/`: detections.csv, overlay.jpg, crops.jpg. `--publish` sends the speed to the display |
| `live.py` | **Live tracker:** phone stream → shot detection → speed on the display. `--serial` for Ethernet, `--file X --fast` replays a clip as if live, `--min-mph` stops rolls being published |
| `tracker/live_shots.py` | `ShotDetector`: a per-frame trigger at 1/4 scale (480x270; starts on 3 of 5 frames with a moving ball), full-res crops around the ball during a shot (cached BGR conversion, plus a reference frame ~15 ms earlier and the background), and a worker thread that runs the full detector on the crops when the ball is gone or still for 0.3 s (cap 3 s) |
| Live preview | `live.py` encodes the newest frame as a 640x360 JPEG at about 15 fps (its own thread; about 33 KB per frame, 3.6 Mbps), drawing the trigger's ball ring, a SHOT tag, and the last shot's path for 4 s. `DisplayServer.set_preview()` serves it at `/video.mjpg`. The page shows it bottom-right beside the RPM row (16vw on screens 3:2 or narrower; hidden in portrait). V toggles it; it auto-retries every 3 s, so it appears when the tracker starts. `--no-preview` turns it off |

**First real-ball test (2026-11-20, `PXL_20261120_161336298.mp4`):** Pixel camera-app slow motion, indoors: 120 fps capture stored with 30 fps timestamps, ball about 1.7 m away and about 60 px across.
- The ball was found in all 145 in-view frames, with ellipse fits tight even next to a hand.
- The toss measured about 7.0 mph (first arc) and about 4.7 mph (after the bounce).
- **Gravity check passes in the image plane:** 8.84 and 9.73 m/s² against 9.81, confirming the timing conversion and the ball-diameter scale.
- **Depth from apparent size is too noisy for acceleration.** It implies about 9 m/s² of spurious Z acceleration, so trust the 2D (in-plane) speed and keep the camera side-on. The 3D figure is only a cross-check.
- **Exposure estimated from blur:** about 4–6 ms indoors. At serve speed (about 45 m/s) that would smear the ball 18–28 cm, so bright sun or shutter control is still needed.
- **Second test (`throw240.mp4`, scrcpy 1080p at 239.5 fps, dim room):** the pipeline needed hardening. High-speed exposure (about 2 ms) makes the ball dark olive (V about 50), so the HSV brightness floor was lowered to 30. Static yellow objects passed through brightness drift, so blobs must now also have changed in the last ~15 ms. Frames where more than 25% of the picture changed are skipped as camera motion. Partial blobs are dropped against the local 75th-percentile size (fragments are only ever smaller). Segments must travel at least 2 ball diameters, and outliers are rejected from a robust path fit. Result: **17.9 mph in the image plane** over 7 whole-ball frames at about 0.8 m (3D cross-check 22 ±9 mph; the ball was also moving away from the camera). The slow-mo clip still gives 6.4 mph, so nothing regressed.
- **Third test (new `throw240.mp4`, bright room, ball about 2.5–2.9 m away, overwrote the second clip):** this was the first clean result at 240 fps. The ball was tracked through the throw (9.2 mph), the floor bounce (5.4 mph), three small bounces and a roll, with no false tracks. Gravity on the two long arcs came out at 9.11 and 10.17 m/s². Arcs moving toward the camera read low in 2D (2.2–2.4 mph against 3.7–4.4 in 3D) and their gravity reads high (10.8–12.7), because a per-segment median scale can't follow a changing depth. That shows directly why the camera must be side-on. The roll shows gravity ≈ 0, as it should.
- **The headline speed is now 2D** (in the image plane). 3D from apparent size is only a cross-check.
- **Shooting guidance:** keep the ball 3–5 m from the camera and side-on. At 0.8 m it crossed the frame in about 25 frames, half of them against dark clutter, which left only 7 usable frames.
- **Live path built (`live.py`):** replays of both clips match the offline analysis (9.1 against 9.2 mph; 6.4 mph). Per-frame cost is 0.3–0.6 ms idle and about 2.2 ms during a shot, against a 4.2 ms budget. Shot analysis takes about 50–150 ms after the shot ends. PyAV's bgr24 conversion is fast (about 0.3 ms full-res) and gets full-range colour right; OpenCV's I420 conversion assumes limited range and shifts saturation, so don't use it. `_changed` uses OpenCV per-channel max, 16× faster than numpy's max over the channel axis. Known limit: the result is published only when the whole sequence ends, including bounces and rolls.
- cv2 and PyAV both bundle libavdevice (objc duplicate-class warning at import); harmless so far.
| `tracker/display.py` + `display/index.html` | Big-number display (speed, RPM) for 30–40 ft away. Local web server that pushes readings to the page; any device on the LAN can open it. Call `DisplayServer().start().publish(speed_mps=..., rpm=...)` |
| `display_server.py`, `send_shot.py` | Run the display on its own (`--open`, `--demo SEC`); push a test reading (`--mph 112 --rpm 2850`) |

The tracker doesn't publish to the display yet; that waits on step 4. Keys in the display: F full screen, U mph or km/h, D local demo reading, H hide footer.

**Long-range link (camera across the court):** about 112 Mbps is needed, and device PTS makes link jitter harmless.
- **Phone hotspot Wi-Fi fails.** It carried only about 25–30 Mbps; the stream fell about 15 s behind and the phone dropped frames (gaps of 9, 17 and 33 frames).
- **adb over a wired network works at full rate.** Tested over USB tethering (`ncm0`): 238.2 fps, no gaps over 6 ms, 107 Mbps, about 50 ms behind real time. So the limit was the Wi-Fi radio, not adb's network mode.
- **Plan: Ethernet.** Phone: a USB-C Ethernet adapter with PD pass-through plus a power bank. Mac: a UGREEN adapter, already owned, plus a long Cat6 cable. The phone needs DHCP from Mac Internet Sharing or a small router, because Android 17 has no `cmd ethernet` to set a fixed IP. IPv6 link-local is a fallback.
- **Ethernet verified (2026-11-20):** USB-C Ethernet adapters at both ends plus a long cable; Mac Internet Sharing from Wi-Fi to **AX88179A** (en7; not the "Ethernet Adapter (en3/4/5)" entries); the phone got 192.168.3.2 after replugging the cable. adb tcpip had survived since Nov 18 (no reboot). The 30 s receiver test showed 238.2 fps, 0 gaps over 6 ms, 102 Mbps, 0 decode errors, usually 40–90 ms behind with brief spikes to 225 ms that caught up. The phone reported AC power but not charging; battery held at 70% over 30 s, so check a longer session.
- **Setup:** run `adb tcpip 5555` over USB once per phone restart, then `adb connect <ip>:5555` and `receiver.py --serial <ip>:5555`. Watch `rx` (frames received per second) and `behind` (lag behind real time) in the stats.

### Measured performance (M5 Pro)

- About 112 Mbps over USB.
- Decoding to gray reaches about 830 fps with 4 threads, or about 1,900 with all cores.
- Lag from packet arrival to a frame in the ring buffer: about 12 ms median and 50 ms at p95 with 4 threads, versus about 63 ms median with all cores (frame threading adds latency).
- A 2 s ring buffer takes 1 GB.

### Gotchas found

- **Phone asleep or locked:** the camera never starts. The client now times out with a message.
- **Face Unlock blocks the camera:** on the lock screen, Pixel's face service opens the front camera, and the phone allows only one camera at a time, so scrcpy is denied ("Too many cameras already open"). Unlock first and use Stay awake while charging. `adb shell dumpsys media.camera` shows the event log.
- **Back-to-back sessions:** the server on the phone holds the camera until it notices the socket closed. `stop()` waits for that session's server process (matched by SCID) to exit, and `start()` retries.
- **Shutdown deadlock:** freeing an unflushed multi-threaded PyAV decoder can deadlock while holding the GIL. Always flush or close the `Decoder`.

## Decisions so far

| Topic | Decision | Why |
|---|---|---|
| Transport | scrcpy over USB-C | No custom Android app needed; supports camera capture and high-speed mode |
| Receiver | Custom Python client that launches `scrcpy-server` directly | Keeps per-frame device timestamps, which speed depends on |
| Time base | Device PTS, never an assumed 240 fps | Dropped or uneven frames would otherwise bias speed |
| Processing | Near-real-time: results shortly after each shot | 4.17 ms per frame is too short to run a neural net on every frame |
| Spin method | Dots drawn on the ball, tracked frame to frame | Most practical route; no turnkey tennis RPM tool was found |
| Order of work | Transport, then speed, then spin | Each stage depends on the one before |

## Next steps

### 1. Install and check 240 fps support

```bash
brew install scrcpy ffmpeg
brew install --cask android-platform-tools
scrcpy --version
adb devices
scrcpy --list-camera-sizes
```

Enable USB debugging on the phone first. Look for a high-speed entry for `1920x1080` at 240 fps on the main back camera.

### 2. Measure real frame timing

```bash
scrcpy --video-source=camera --camera-facing=back \
  --camera-size=1920x1080 --camera-fps=240 --camera-high-speed \
  --no-audio --video-codec=h264 --video-bit-rate=100M --record=test.mp4

ffprobe -v error -select_streams v:0 -show_entries packet=pts_time \
  -of csv=p=0 test.mp4 > pts.csv
```

Stop recording with Ctrl+C so the MP4 closes cleanly. Use MP4 rather than MKV here: Matroska timestamps usually have 1 ms resolution, too coarse to judge 4.17 ms gaps.

**Pass:** median gap is about 4.17 ms, with few gaps above 6 ms.
**If it fails:** try a lower bit rate, then 720p if listed, then 120 fps.

Listed sizes are only what the device declares, so some may not work in practice.

### 3. Build the receiver

Suggested file: `receiver.py`.

1. Read the installed version from `scrcpy --version`.
2. Push `scrcpy-server` to `/data/local/tmp/`. On Homebrew it is probably at `$(brew --prefix scrcpy)/share/scrcpy/scrcpy-server` (unverified).
3. Set up `adb forward tcp:<port> localabstract:scrcpy_<SCID>`, where SCID is a random 31-bit number written as 8 hex digits.
4. Launch the server through `adb shell`, passing the exact version as the first argument.
5. Connect, parse the stream, and decode it.
6. Push `(pts, gray_frame)` into a ring buffer holding a few seconds of frames.
7. Print live stats: fps, gap outliers, Mbps, decode lag.
8. Add an option to save raw H.264 plus a PTS CSV, so analysis can be developed offline.

Launch template:

```bash
adb shell CLASSPATH=/data/local/tmp/scrcpy-server-manual.jar \
  app_process / com.genymobile.scrcpy.Server <VERSION> \
  scid=<SCID> log_level=info tunnel_forward=true \
  audio=false control=false cleanup=false send_device_meta=false \
  video_source=camera camera_facing=back camera_size=1920x1080 \
  camera_fps=240 camera_high_speed=true \
  video_codec=h264 video_bit_rate=100000000
```

**Verify these option names** against `Options.java` for the installed version. The camera and codec keys above were written from memory, not confirmed in the docs.

### 4. Speed

1. Run cheap motion detection on every frame (frame differencing on a downscaled image).
2. When motion looks like a shot, pull that window from the ring buffer.
3. Locate the ball with sub-pixel accuracy on crops. Try classical methods first; use a TrackNet-style model if they are not robust enough.
4. Calibrate pixels to metres in the plane of the ball's flight.
5. Fit position against PTS across the track and report speed with an uncertainty.

Validate against a known reference, such as a radar gun or a ball machine's stated speed.

### 5. Spin

1. Stabilize crops around the ball.
2. Detect the dots and fit their 3D rotation on a sphere between frames.
3. Fit a constant spin axis and rate across the track.

Use an asymmetric dot pattern so the rotation between frames is unambiguous. Start with slower ball-machine shots, close to the camera.

## scrcpy protocol notes

The protocol **changes between versions**, and the server refuses to run unless the client version matches exactly. Branch on the installed version.

The current docs describe **v4.0**. A third-party article gives v4.1 as the latest release (July 2026); check the installed version's `doc/develop.md`.

### v4.0 stream layout (from the official `doc/develop.md`)

With a forward tunnel, on the first socket:

1. 1 dummy byte
2. Device name, unless `send_device_meta=false` (the docs give no length, so disable it)
3. Codec ID: `u32`, big-endian (`h264` = `0x68323634`)
4. A 12-byte session packet, then media packets

Session packet (12 bytes), sent again when a capture session changes:

```text
bytes 0-3   MSB = 1 (session packet flag), LSB = client-resized flag
bytes 4-7   video width
bytes 8-11  video height
```

Media packet header (12 bytes), followed by the payload:

```text
bytes 0-7   bit 63 = 0 (media packet flag)
            bit 62 = config packet
            bit 61 = key frame
            bits 60-0 = PTS
bytes 8-11  packet size (u32)
```

Check the first bit of each 12-byte header to tell session packets from media packets. Config packets carry SPS/PPS and must reach the decoder, but they are not frames, so they get no timestamp entry.

### Older versions

Older protocol versions used a different layout, so do not reuse v4 parsing without checking. As far as I recall, v3.x sends codec ID, width and height up front, and uses bit 63 for config and bit 62 for key frame.

## Decoder options (undecided)

| Option | Pros | Cons |
|---|---|---|
| **PyAV** (preferred) | Packets carry PTS directly; clean API | Must confirm it keeps up at 1080p240 on this Mac |
| **ffmpeg subprocess** (`-f rawvideo -pix_fmt gray`) | Simple; can use VideoToolbox | PTS must be matched to frames by order; assumes no B-frames or dropped frames |

Benchmark decode speed early. Grayscale (the Y plane) is enough for detection and saves memory.

Memory check: 1080p gray is about 2 MB per frame, so about 500 MB/s at 240 fps. A 3-second buffer is about 1.5 GB. Consider storing encoded packets and decoding only the shot window.

## Physics and capture constraints

| Quantity | Value |
|---|---|
| Frame interval at 240 fps | 4.17 ms |
| 100 mph | 44.7 m/s, or about 18.6 cm per frame |
| Blur at 100 mph, 1/1000 s | about 4.5 cm (ball diameter is about 6.7 cm) |
| Blur at 100 mph, 1/4000 s | about 1.1 cm |
| 3,000 RPM at 240 fps | 75 degrees per frame |
| 5,000 RPM at 240 fps | 125 degrees per frame |
| Theoretical spin limit at 240 fps | 7,200 RPM (180 degrees per frame) |
| Ball width at 3 m | about 30 px (estimate; assumes about 70 degree horizontal view) |
| Ball width at 1.5 m | about 60 px (estimate) |

### Known risks

1. **Shutter speed is the biggest risk.** scrcpy has no shutter control, and Android's high-speed mode may not allow manual exposure (unconfirmed). Film in bright sun. If blur is still too high, a small custom Android app may be needed.
2. **The 240 fps stream may be unavailable or unstable** through the camera API or hardware encoder.
3. **Compression can erase dots.** Use a high bit rate and inspect real frames.
4. **Camera geometry affects speed.** Keep the camera side-on to the flight path. Motion toward or away from the camera will bias a single-camera estimate.
5. **The ball slows in flight**, so measured speed depends on where along the path it is taken.
6. **Rolling shutter** may skew a fast ball's apparent shape and position.

## References

**Transport**
- scrcpy: https://github.com/Genymobile/scrcpy
- Camera docs: https://github.com/Genymobile/scrcpy/blob/master/doc/camera.md
- Protocol docs: https://github.com/Genymobile/scrcpy/blob/master/doc/develop.md

**Ball tracking and speed**
- tennis_analysis (ball shot speed): https://github.com/abdullahtarek/tennis_analysis
- GridTrackNet (reports 116 FPS): https://gitee.com/tslnihao/GridTrackNet
- TrackNetV3 (badminton; about 25 FPS): https://github.com/qaz812345/TrackNetV3
- tennis-tracking: https://github.com/ArtLabss/tennis-tracking

**Spin**
- SpinDOE (dotted table tennis balls; 175 rev/s at 350 fps; code license not confirmed): https://github.com/cogsys-tuebingen/spindoe
- Paper: https://arxiv.org/abs/2303.03879
- Tennis spin validation from multi-camera tracking: https://pubmed.ncbi.nlm.nih.gov/31783720
