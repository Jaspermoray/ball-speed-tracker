"""Ball Speed Tracker package.

On macOS, PyAV and OpenCV each bundle their own copy of FFmpeg's libavdevice,
and loading both makes the Objective-C runtime print an alarming
"Class AVFFrameReceiver is implemented in both ..." warning. It only concerns
FFmpeg's AVFoundation *capture* device, which this project never uses (video
comes from scrcpy), so it is harmless. Load both libraries here once, with
stderr muted for just that moment; entry scripts import `tracker` first.
"""
import os
import sys


def _quiet_load():
    if sys.platform != "darwin" or "cv2" in sys.modules:
        return
    try:
        saved = os.dup(2)
    except OSError:
        return
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 2)
        os.close(devnull)
        import av  # noqa: F401
        import cv2  # noqa: F401
    except ImportError:
        pass        # surfaced properly by the real imports later
    finally:
        os.dup2(saved, 2)
        os.close(saved)


_quiet_load()
