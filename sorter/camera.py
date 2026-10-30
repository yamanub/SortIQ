"""Camera backends. All expose capture(light) -> BGR ndarray | None.

  SyntheticCamera - renders the case currently seated in the VirtualMachine
  WebcamCamera    - any USB webcam / USB microscope on the PC (or the Pi)
  FolderCamera    - replays saved A/B frame pairs from a dataset directory
"""
import random
import sys
from pathlib import Path

import cv2

from . import synth

# Hardware properties we try to drive. Support varies wildly by camera/driver;
# callers should verify with cap.get() after setting.
CAMERA_PROPS = {
    "brightness":         cv2.CAP_PROP_BRIGHTNESS,
    "contrast":           cv2.CAP_PROP_CONTRAST,
    "saturation":         cv2.CAP_PROP_SATURATION,
    "sharpness":          cv2.CAP_PROP_SHARPNESS,
    "gamma":              cv2.CAP_PROP_GAMMA,
    "gain":               cv2.CAP_PROP_GAIN,
    "exposure":           cv2.CAP_PROP_EXPOSURE,
    "auto_exposure":      cv2.CAP_PROP_AUTO_EXPOSURE,
    "focus":              cv2.CAP_PROP_FOCUS,
    "autofocus":          cv2.CAP_PROP_AUTOFOCUS,
    "white_balance_auto": cv2.CAP_PROP_AUTO_WB,
    "white_balance_temp": cv2.CAP_PROP_WB_TEMPERATURE,
}


def auto_exposure_value(on):
    """Driver-specific encoding: DirectShow wants 0.75/0.25, V4L2 wants 3/1."""
    if sys.platform == "win32":
        return 0.75 if on else 0.25
    return 3 if on else 1


def apply_camera_controls(cap, controls):
    """Apply saved {name: value} controls; returns {name: actual_value_now}."""
    applied = {}
    # order matters on a UVC module: a manual value written while its auto
    # is on is dropped by the driver, and a module fresh from a power cycle
    # boots in auto. So: autos being switched OFF first, then the manual
    # values, then autos being switched ON — never dict insertion order.
    autos = ("auto_exposure", "white_balance_auto")
    def rank(kv):
        name, value = kv
        return 0 if name in autos and not value else 2 if name in autos else 1
    for name, value in sorted((controls or {}).items(), key=rank):
        prop = CAMERA_PROPS.get(name)
        if prop is None:
            continue
        if name == "auto_exposure":
            value = auto_exposure_value(bool(value))
        cap.set(prop, float(value))
        applied[name] = cap.get(prop)
    return applied


def apply_zoom(frame, zoom):
    """Digital zoom: center crop by `factor`, panned by x/y in [-1, 1]."""
    if frame is None or not zoom:
        return frame
    f = max(float(zoom.get("factor", 1.0)), 1.0)
    if f <= 1.001:
        return frame
    h, w = frame.shape[:2]
    cw, ch = max(int(w / f), 16), max(int(h / f), 16)
    x = min(max(float(zoom.get("x", 0.0)), -1.0), 1.0)
    y = min(max(float(zoom.get("y", 0.0)), -1.0), 1.0)
    x0 = int((w - cw) / 2 * (1 + x))
    y0 = int((h - ch) / 2 * (1 + y))
    return frame[y0:y0 + ch, x0:x0 + cw]


class SyntheticCamera:
    def __init__(self, machine):
        self.m = machine

    def capture(self, light):
        if self.m.current_case is None:
            return None
        return synth.render(self.m.current_case, light=light,
                            seed=random.randrange(1 << 30))

    def release(self):
        pass


class WebcamCamera:
    """The real thing. On the PC: a USB microscope over your brass jig.

    OpenCV buffers frames internally, so after a light change the next
    cam.read() can return a frame captured BEFORE the switch. We flush
    `settle_frames` frames on every capture to guarantee freshness.
    """

    def __init__(self, cam_cfg):
        flag = cv2.CAP_DSHOW if sys.platform == "win32" else 0
        self.cap = cv2.VideoCapture(cam_cfg["index"], flag)
        # MJPG before resolution: at 3 MP over USB 2.0 (e.g. the CS7.2 OV3660)
        # uncompressed YUY2 is only a few fps; MJPG restores usable frame rate.
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, cam_cfg["width"])
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cam_cfg["height"])
        self.settle = cam_cfg.get("settle_frames", 4)
        self.zoom = cam_cfg.get("zoom") or {}
        apply_camera_controls(self.cap, cam_cfg.get("controls"))
        if not self.cap.isOpened():
            raise RuntimeError(f"cannot open camera index {cam_cfg['index']}")

    def capture(self, light):
        for _ in range(self.settle):
            self.cap.grab()
        ok, frame = self.cap.read()
        return apply_zoom(frame, self.zoom) if ok else None

    def release(self):
        self.cap.release()


class FolderCamera:
    """Replays a collected dataset: <root>/**/xxx_A.png + xxx_B.png pairs.

    Lets you rerun the full sort loop against real photos you already
    collected, e.g. to compare model versions on identical input.
    """

    def __init__(self, root):
        self.pairs = sorted(Path(root).rglob("*_A.png"))
        if not self.pairs:
            raise RuntimeError(f"no *_A.png frames under {root}")
        self.i = -1

    def advance(self):
        self.i += 1
        return self.i < len(self.pairs)

    def capture(self, light):
        if not (0 <= self.i < len(self.pairs)):
            return None
        p = self.pairs[self.i]
        if light == "B":
            p = p.with_name(p.name.replace("_A.png", "_B.png"))
        img = cv2.imread(str(p))
        return img

    def release(self):
        pass
