# Ball Speed Tracker

Measure tennis ball **speed and spin (RPM)** live with an Android phone's **240 fps** slow-motion camera and a laptop. The readings show up on a big full-screen display you can read from across the court, a moment after each shot.

- **No app to install on the phone.** The video streams over USB or a long Ethernet cable using [scrcpy](https://github.com/Genymobile/scrcpy).
- **Uses the phone's real frame timestamps,** so dropped or uneven frames can't skew the speed.
- **Calibrates itself from the ball,** which is 6.7 cm across, so there's no setup measuring.
- **Built-in physics check:** for each flight it reports the measured gravity, which should come out near 9.81 m/s².
- **Spin from the ball's own seams and logo,** with no marked ball needed. It gives the full 3D spin, split into topspin/backspin, sidespin and rifle spin. When the footage is too blurred to be sure, it says so instead of guessing.

> **Status:** speed and spin both work. Spin needs **bright light**: indoors the ball is blurred, and the spin is usually flagged unreliable and shown as "—". On real footage, spin has so far only been checked against a rolling ball (physics predicts 250–360 RPM; measured 268), not a radar or launch monitor.

---

## What you need

| Item | Notes |
|---|---|
| Android phone with a 240 fps high-speed camera | Tested on a **Pixel 9a** (Android 17). Other phones work if their camera exposes high-speed 1080p to apps; `scrcpy --list-camera-sizes` tells you. |
| Computer | Tested on an **Apple Silicon Mac**. Intel Macs and x86-64 Linux should work but are untested. |
| USB-C data cable | For setup, and for short-range use. |
| Optic-yellow tennis ball | Detection is colour-based. |
| *Optional, to put the camera across the court:* two USB-C Ethernet adapters and an Ethernet cable | Use an adapter with **USB-C charging pass-through** on the phone end, plus a power bank (30 W+). Phone Wi-Fi hotspots are too slow; see [why](#why-not-wi-fi). |

---

## 1. Install (once)

You need Python 3.10 or newer ([python.org](https://www.python.org/downloads/)).

```bash
git clone https://github.com/Jaspermoray/ball-speed-tracker.git
cd ball-speed-tracker
./setup.sh
```

`setup.sh` creates a Python environment in `.venv/` and downloads scrcpy 5.0.1, which includes `adb`, into `tools/`. It checks the download's checksum, and it's safe to run again.

## 2. Phone setup (once)

1. **Turn on Developer options:** Settings → About phone → tap **Build number** 7 times.
2. **Turn on USB debugging:** Settings → System → Developer options → **USB debugging**.
3. **Turn on Stay awake** in the same menu, so the screen stays on while charging. Without it the phone locks, and on the lock screen **Face Unlock takes over the camera** and the tracker can't open it.
4. Plug the phone into the computer and unlock it. When asked *"Allow USB debugging?"*, tick **Always allow from this computer** and tap **Allow**.

## 3. Run it

```bash
./start.sh
```

This finds the phone, checks it's unlocked, starts the camera, and opens the display in your browser. Press **F** in the browser for full screen. Throw or hit a ball through the view, and the speed appears a moment after the ball leaves the frame or stops. Press **Ctrl+C** in the terminal to stop.

Wait about 2 seconds after starting before the first shot; the tracker needs a moment to learn the empty background.

---

## Putting the camera across the court (Ethernet)

USB cables only reach a few metres. For longer distances, run Ethernet: `phone → USB-C Ethernet adapter → cable → USB-C Ethernet adapter → Mac`.

**Once per phone restart**, while the phone is on USB:
```bash
./start.sh --enable-network
```
This switches adb to network mode. It stays on until the phone restarts.

**On the Mac, turn on Internet Sharing** so the Mac gives the phone an address:
1. System Settings → General → Sharing → click **ⓘ** next to **Internet Sharing**.
2. *Share your connection from:* **Wi-Fi**.
3. *To devices using:* tick **your USB Ethernet adapter**. It's often listed by its chip name, such as *AX88179A* or *RTL8153*, not "Ethernet Adapter (enX)". Untick everything else.
4. Click Done and switch Internet Sharing **on**.

**Then:** unplug the USB cable, plug the Ethernet adapter into the phone (power bank into the adapter first, if it has a charging port), connect the cable, and run:
```bash
./start.sh
```
It finds the phone on the network by itself and remembers its address for next time. If it can't, unplug and replug the Ethernet cable at the phone end (this makes the phone request an address), or pass the address shown under Settings → About phone → IP address: `./start.sh --ip 192.168.3.2`.

### Why not Wi-Fi?
The stream needs about 100 Mbps, sustained. In testing, the phone's hotspot carried only 25–30 Mbps: the video fell 15 seconds behind and the phone dropped frames. Over Ethernet it ran at a steady 238 fps with no dropped frames.

---

## Filming tips (these matter most for accuracy)

- **Side-on.** Speed is measured across the picture, so put the camera at right angles to the ball's path. Motion toward or away from the camera is under-counted.
- **3–5 m from the ball's path.** At that distance the ball stays in view for many frames and is large enough to measure. Too close, and it crosses the frame in a few frames.
- **Bright light.** At 240 fps each exposure is very short. Outdoors in daylight is ideal; dim rooms make the ball dark and grainy. **Spin especially needs it:** the seams must stay sharp, which means a short exposure (under about 1 ms), and that only happens in strong light.
- **Steady camera** (a tripod or a solid surface), **landscape** orientation.
- **Plain background** if you can. Keep other yellow-green moving things out of the frame.

---

## The display

Open it at `http://localhost:8765/`; `./start.sh` opens it for you. Other devices on the same network (a TV, an iPad) can open `http://<computer-ip>:8765/` too; the address is printed when it starts.

| Key | Action |
|---|---|
| **F** | Full screen |
| **U** | Switch mph and km/h |
| **V** | Show or hide the live camera preview (bottom-right) |
| **H** | Hide the history row |
| **D** | Show a fake reading (to check readability from a distance) |
| **?** | Help |

The newest reading turns yellow for a few seconds. The camera preview draws a ring on the ball while it's being tracked, and the last shot's path for a few seconds afterwards, which helps with aiming.

### What gets reported
Each shot prints a line in the terminal, such as:
```
[shot 2] 9.0 mph ±0.0 (3D 9.1; frames 833-886, n=54, gravity 11.7; all segments: 9.0, 9.4, 9.4; ...)
```
- The **headline speed** is the ball's first flight, before any bounce. That's what goes to the display, straight away.
- A second line follows a second or two later with the **spin**, which the display adds to the same reading:
  ```
  [shot 2] spin: 2933 rpm (2514 topspin, 1508 side, 88 rifle; match 0.34, margin 0.30)
  ```
  *match* is how well the ball's surface pattern lines up between frames, and *margin* is how much better the answer fits than the next-best one. If either is too low, or the error is too large, the spin is marked **UNRELIABLE** and the display shows "—" for RPM rather than a doubtful number.
- **gravity** is the measured downward acceleration. Near 9.81 means the camera was level and side-on, and the measurement is consistent. A value far off usually means the ball was moving toward or away from the camera.
- **3D** is a rough cross-check from the ball's changing apparent size. It's noisy; trust the headline speed when the camera is side-on.
- Shots under 3 mph (rolls, nudges) aren't sent to the display; change this with `./start.sh --min-mph 5`.

---

## Analysing recordings

You can also measure saved videos. They're useful for checking a setup, or for clips filmed with the phone's own camera app.

```bash
# record 240 fps through scrcpy (live view on the Mac; Ctrl+C to stop)
tools/scrcpy/scrcpy --video-source=camera --camera-facing=back --camera-size=1920x1080 \
  --camera-fps=240 --camera-high-speed --no-audio --video-codec=h264 \
  --video-bit-rate=100M --record=clip.mp4

# measure it (also works on the phone camera app's slow-motion videos)
.venv/bin/python analyze_clip.py clip.mp4

# or replay it through the live tracker and display
./start.sh --replay clip.mp4
```

`analyze_clip.py` writes `analysis/<clip>/overlay.jpg`, which shows the tracked path with each flight's speed, plus `crops.jpg` (the fitted ball in each frame) and `detections.csv`.

Phone slow-motion files store 120 or 240 fps footage with 30 fps timestamps. The analysis reads the real capture rate from the file and converts back. For the Pixel camera app, choose **1/8×** slow motion for 240 fps.

> Phone videos can contain your GPS location in their metadata. `*.mp4` files are git-ignored here, so be careful when sharing clips elsewhere.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Device is unauthorized` / "asking for permission" | Unlock the phone and accept the USB debugging prompt (tick *Always allow*). |
| "camera limit reached" / `Too many cameras already open` | Something else has the camera, usually **Face Unlock** on the lock screen, or the Camera app. Unlock the phone, close the Camera app, and turn on Stay awake. |
| "no video from the camera" | The phone is asleep or locked. Unlock it. |
| "no phone found" on Ethernet | Check Internet Sharing is on for the **right** adapter. Replug the cable at the phone end. After a phone restart, run `./start.sh --enable-network` once over USB. |
| `behind=` keeps growing, `<< FALLING BEHIND` | The link is too slow (Wi-Fi), or the computer is overloaded. Use USB or Ethernet. |
| `gaps>6ms` above 0 during a session | The phone dropped frames. Check `thermal=` in `receiver.py` output, and keep the phone cool and powered. |
| Speeds look too high | Check `overlay.jpg`: the ball should be fully outlined. Dark scenes give partial detections, so add light. |
| No shots detected | Use an optic-yellow ball in good light, and keep the camera steady. Wait 2 s after starting. |
| RPM always "—" (spin UNRELIABLE) | The ball is too blurred to see its seams. Add light (outdoors in sun), or move the camera so the ball is bigger in the frame (closer, but keep it side-on). |

Diagnostic tools:
- `.venv/bin/python receiver.py --serial <serial>` streams and prints frame-rate, bit-rate, lag and phone battery and temperature, with no detection.
- `.venv/bin/python link_setup.py` checks and sets up the Ethernet link step by step.

---

## How it works

1. **Transport.** `start.sh` launches scrcpy's server on the phone through adb, and the Python client reads its H.264 stream directly. That keeps each frame's **device timestamp**, which speed depends on.
2. **Trigger.** Every frame is checked at ¼ resolution (480×270) for an optic-yellow blob that differs from the background *and* moved in the last ~15 ms. This takes under 1 ms per frame.
3. **Measurement.** During a shot, full-resolution crops around the ball are kept. When the ball leaves the frame or stops, an ellipse is fitted to the ball in each crop. Its centre is the position, at sub-pixel accuracy. Its short axis is the true diameter, because motion blur only stretches the long axis, and that gives the scale in each frame.
4. **Speed.** The track is split at bounces, and each flight is fitted against time: straight-line motion horizontally, a parabola vertically for gravity. Positions are converted to metres using the 6.7 cm ball diameter.
5. **Spin.** Each frame's ball image is mapped onto a sphere. Parts that don't turn with the ball are divided out: the lighting, the darker rim, and the motion-blur smear. The estimator then searches every spin axis and rate up to 6,000 RPM for the steady spin that best carries the seams and logo from each frame to the frames 1, 2 and 3 later. A coarse 3D grid search is followed by refinement. Checking several frame gaps at once rejects look-alike answers (a tennis seam looks the same after a half turn about some axes). When two answers fit about equally, the slower one wins.

**Accuracy and limits.**
- **Speed** is measured two-dimensionally, across the picture, so it's accurate only when the camera is side-on to the flight. On synthetic clips with a known answer it's within 2% (usually under 1%). On real clips, measured gravity came out within 1–10% of 9.81 m/s².
- **Spin**, on synthetic clips with a 0.5 ms exposure, is within 2% for topspin, backspin, sidespin, kick-serve mixtures, 300–3,600 RPM, and balls 28 px across. With an indoor-like 1.5 ms exposure it's either right or flagged unreliable. The practical limit at 240 fps is about 4,000 RPM.
- **Neither has been checked against a radar gun or launch monitor yet.**
- **Motion blur is the main risk:** shoot in bright light.

**Tests.** `.venv/bin/python tests/test_spin.py` renders synthetic 240 fps clips of a spinning tennis ball, with the real seam curve, a logo, lighting, motion blur, noise and H.264 compression. It then runs the full pipeline on them and checks speed and spin against the truth. It takes about a minute; use `--quick` for 4 cases. `tests/synthetic_ball.py` renders single clips, for example `--topspin=2500 --side=1500 --exposure=0.5`.

## Project layout

| Path | What it is |
|---|---|
| `setup.sh`, `start.sh` (`start.py`) | Install; find the phone and run |
| `live.py` | Live tracker: stream → shot detection → display |
| `analyze_clip.py` | Measure a recorded video |
| `tracker/scrcpy_client.py` | Minimal scrcpy client (protocol for scrcpy 5.0.1) |
| `tracker/ball.py` | Ball detection, tracking, flight fitting |
| `tracker/live_shots.py` | Two-stage live shot detector |
| `tracker/spin.py` | Spin (RPM and axis) from the ball's surface pattern |
| `tests/` | Synthetic spinning-ball renderer and the end-to-end speed and spin test |
| `tracker/display.py`, `display/index.html` | Display server (live updates and camera preview) and page |
| `receiver.py`, `link_setup.py`, `analyze_pts.py`, `display_server.py`, `send_shot.py` | Diagnostics and testing tools |
| `TENNIS_TRACKER_HANDOFF.md` | Development notes: design decisions, measurements, gotchas |

## Credits and licence

MIT; see [LICENSE](LICENSE).

This project downloads and runs [scrcpy](https://github.com/Genymobile/scrcpy) by Genymobile (Apache-2.0) to stream the phone camera. scrcpy isn't redistributed here; `setup.sh` fetches the official release.
