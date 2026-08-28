"""Browser-based trainer / tester for the case sorter.

Run on the PC (or later the Pi) and open http://<host>:5000 —
  Collect : capture or upload labeled A/B frame pairs into the dataset
  Train   : background training with live progress + confusion matrix
  Test    : run any image through the real pipeline; see every gate,
            per-class probabilities, and the destination bin on the disc
  Classes : routing, near-twin group, acceptance level; bin map auto-computed

Usage: python webui/server.py [--port 5000] [--data data/real]
"""
import argparse
import base64
import functools
import itertools
import json
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from flask import (Flask, jsonify, request, send_file, send_from_directory,
                   Response)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# pythonw (the Windows autostart) runs with NO console: sys.stdout/stderr
# are None, and the first write — TensorFlow's import banner, Keras's
# progress bar, any stray print — dies with "'NoneType' object has no
# attribute 'write'", which killed the first autostart-hosted training.
# Give the process a real sink; a log file beats devnull for debugging.
if sys.stdout is None or sys.stderr is None:
    _sink = open(ROOT / "trainer.log", "a", buffering=1, encoding="utf-8",
                 errors="replace")
    sys.stdout = sys.stdout or _sink
    sys.stderr = sys.stderr or _sink

from sorter import codesync, imaging, synth, profiles
from sorter.camera import (CAMERA_PROPS, apply_camera_controls, apply_zoom,
                           auto_exposure_value)
from sorter.config import Config
from sorter.dataset import (dataset_counts, delete_label, delete_pair,
                            move_pair, parse_label, raw_counts, rebuild_crops,
                            rename_label)

app = Flask(__name__, static_folder="static")

# The machine's Train-models modal drives the trainer app on the VIEWER'S
# PC straight from the browser (the page at http://<machine>:5000 fetches
# http://localhost:5000) — cross-origin, so the trainer must answer CORS.
# Only private/LAN origins are reflected; public websites get nothing.
# Allow-Private-Network satisfies Chrome's local-network preflight.
_PRIVATE_ORIGIN = re.compile(
    r"^https?://(localhost|127\.0\.0\.1|\[::1\]"
    r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|[\w-]+\.local)(:\d+)?$")


@app.after_request
def _cors_private(resp):
    origin = request.headers.get("Origin", "")
    if _PRIVATE_ORIGIN.match(origin):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Private-Network"] = "true"
    return resp

CONFIG_PATH = ROOT / "config.json"


def active_dirs():
    """(data_dir, models_dir) for the currently active caliber/model."""
    c = Config(CONFIG_PATH)          # first call migrates a legacy flat config
    return c.data_dir, c.models_dir


# Repointed whenever the active model changes; every route reads these globals.
DATA_DIR, MODELS_DIR = active_dirs()

state_lock = threading.Lock()
train_status = {"running": False, "stage": None, "epoch": 0, "epochs": 0,
                "acc": 0, "val_acc": 0, "error": None, "result": None}


# ---------------------------------------------------------------- config ---
# Requests run concurrently (threaded=True); every read-modify-write of
# config.json must hold this lock or concurrent edits lose one of the writes.
_config_lock = threading.Lock()


def load_cfg():
    return Config(CONFIG_PATH)


def write_cfg_raw(raw):
    """Atomic replace, so a crash mid-write can't leave a truncated config."""
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text(json.dumps(raw, indent=2))
    os.replace(tmp, CONFIG_PATH)


def clean_bins(raw):
    """Normalize bins to lists-of-stamps and drop stamps no longer configured.

    Slots hold LISTS so several variants of one make can share a chute
    (see sorter.config.normalize_bin); legacy single-string entries are
    normalized here on the next write."""
    from sorter.config import normalize_bin
    stamps = set(raw["stamp_labels"])
    fams = set((raw.get("families") or {}).keys())
    raw["bins"] = [[s for s in normalize_bin(b)
                    if s in ("UNMATCHED", "OVERFLOW") or s in stamps
                    or (s.startswith("family:")
                        and s[len("family:"):] in fams)]
                   for b in raw.get("bins", [])]


# ---- active model (labels / bins / floors / imaging live in model.json) ----
def active_model_raw():
    """The active model's model.json as a mutable dict (model-scoped config)."""
    a = json.loads(CONFIG_PATH.read_text())["active"]
    return profiles.read_model(ROOT, a["cartridge"], a["model"])


# retired legacy keys: shed from model.json on
# the next write so old profiles converge to the current schema themselves
_RETIRED_MODEL_KEYS = ("specialist_groups", "specialist_classes", "decider",
                       "tta_views", "train_img_size")
_RETIRED_SUBKEYS = {"imaging": ("polar", "gray"), "floors": ("top2_margin",)}


def write_active_model(mj):
    for k in _RETIRED_MODEL_KEYS:
        mj.pop(k, None)
    for sub, keys in _RETIRED_SUBKEYS.items():
        d = mj.get(sub)
        if isinstance(d, dict):
            for k in keys:
                d.pop(k, None)
    profiles.write_model(ROOT, mj["cartridge"], mj["name"], mj)


def set_active_model(cartridge, model):
    """Repoint the app at a different caliber/model and reload its dirs/models."""
    global DATA_DIR, MODELS_DIR
    profiles.set_active(CONFIG_PATH, cartridge, model)   # raises if it's missing
    DATA_DIR, MODELS_DIR = active_dirs()
    with state_lock:                # force an embed-classifier reload
        _shadow.update(clf=None, mtimes=None)


# The embedding decider (shadow_embed.tflite + shadow_gallery.npz in the
# profile's models dir) is the ONLY classifier — the softmax pipeline
# (stamp/stamp_alt twins + rival referees) is retired, and any
# old stamp*.tflite files on disk are inert. The "shadow" name survives
# from the campaign that proved the embedding as a logged second opinion.
_shadow = {"clf": None, "mtimes": None}


def get_shadow():
    m_path = MODELS_DIR / "shadow_embed.tflite"
    g_path = MODELS_DIR / "shadow_gallery.npz"
    if not (m_path.exists() and g_path.exists()):
        _shadow["clf"] = None
        _shadow["mtimes"] = None
        return None
    mt = (m_path.stat().st_mtime, g_path.stat().st_mtime)
    if _shadow["mtimes"] != mt:
        try:
            from sorter.embed_classifier import EmbedClassifier
            _shadow["clf"] = EmbedClassifier(m_path, g_path)
            _shadow["mtimes"] = mt
        except Exception as e:                       # bad file must not kill runs
            print(f"shadow classifier load failed: {e}", flush=True)
            _shadow["clf"] = None
            _shadow["mtimes"] = mt
    return _shadow["clf"]


# ---------------------------------------------------------------- camera ---
_camera = {"cap": None, "lock": threading.Lock(), "error": None, "zoom": None,
           "frame": None, "frame_t": 0.0, "pump_stop": None,
           # STREAM-ONLY zoom override while the camera page tunes: the
           # operator sees the slider live, but captures keep using the
           # SAVED zoom until an explicit Save. Auto-expires so an
           # abandoned tab can't leave the view lying forever.
           "zoom_preview": None}

FRAME_STALE_S = 2.0   # slowest camera mode is ~2.6 fps; older means it died


def _capture_device(index):
    """Resolve a camera index to a stable /dev/v4l/by-id path when possible.

    /dev/videoN numbering is not stable: when the USB link drops and the
    camera re-enumerates (the EMI storms take the whole hub down), the
    camera can come back as video1 while the config still says video0.
    The by-id symlink follows the physical device across re-enumeration.
    Off-Pi there is no by-id dir and the numeric index passes through.
    """
    byid = Path("/dev/v4l/by-id")
    if byid.is_dir():
        paths = sorted(str(p) for p in byid.iterdir()
                       if p.name.endswith("-video-index0"))
        if 0 <= index < len(paths):
            return paths[index]
    return index


def _ensure_recovery():
    """Start the (single) background reopen loop. Caller holds the lock."""
    if _camera.get("recovering"):
        return
    _camera["recovering"] = True
    threading.Thread(target=_recovery_loop, daemon=True).start()


def _recovery_loop():
    """Retry the camera open every few seconds until it comes back.

    Covers both failure shapes seen in the field: the pump dying mid-run
    (USB re-enumeration after an EMI hit) and an open that fails outright
    (device briefly held or still enumerating). Stops as soon as any path
    — this loop, a settings change, a preset apply — lands a working open."""
    try:
        while True:
            time.sleep(3.0)
            with _camera["lock"]:
                if _camera["cap"] is None:
                    open_camera_locked()
                if _camera["cap"] is not None:
                    _camera["recovering"] = False
                    return               # working open (ours or someone's)
                _camera["error"] = "camera lost — reconnecting"
    except BaseException:
        with _camera["lock"]:
            _camera["recovering"] = False
        raise


def _pump(cap, stop):
    """Continuously hold the newest camera frame in memory.

    The camera is slow (2.6-10 fps depending on mode); if every consumer
    (stream, live preview, capture) did its own buffered reads, latency
    stacks up to seconds. One reader thread means everyone else gets the
    freshest frame instantly.
    """
    failures = 0
    while not stop.is_set():
        with _camera["lock"]:
            if _camera["cap"] is not cap:
                return                      # camera was reopened; retire
        try:
            # OUTSIDE the lock: this read blocks ~a frame interval, and
            # holding the lock through it made every settings call and
            # /api/state poll queue behind the camera — the app felt
            # sluggish everywhere. A reopen mid-read just fails the read
            # (caught below); the next loop notices the new cap and retires.
            ok, frame = cap.read()
        except Exception:                   # a USB hiccup mid-read throws —
            ok, frame = False, None         # count it, don't kill the thread
        if ok:
            _camera["frame"] = frame
            _camera["frame_t"] = time.monotonic()
            failures = 0
        else:
            failures += 1
            if failures > 25:
                with _camera["lock"]:
                    if _camera["cap"] is cap:   # nobody reopened meanwhile
                        cap.release()
                        _camera["cap"] = None
                        _camera["error"] = "camera lost — reconnecting"
                        _ensure_recovery()
                return
            time.sleep(0.05)

# UI metadata for the hardware controls. Ranges are generic; unsupported
# properties are detected per camera and disabled in the browser.
CONTROL_META = [
    {"name": "autofocus",          "label": "Autofocus",          "type": "bool"},
    {"name": "focus",              "label": "Focus",              "type": "range", "min": 0,    "max": 255,  "step": 5},
    {"name": "auto_exposure",      "label": "Auto exposure",      "type": "bool"},
    {"name": "exposure",           "label": "Exposure",           "type": "range", "min": -13,  "max": 0,    "step": 1},
    {"name": "gain",               "label": "Gain",               "type": "range", "min": 0,    "max": 255,  "step": 1},
    {"name": "brightness",         "label": "Brightness",         "type": "range", "min": 0,    "max": 255,  "step": 1},
    {"name": "contrast",           "label": "Contrast",           "type": "range", "min": 0,    "max": 255,  "step": 1},
    {"name": "saturation",         "label": "Saturation",         "type": "range", "min": 0,    "max": 255,  "step": 1},
    {"name": "sharpness",          "label": "Sharpness",          "type": "range", "min": 0,    "max": 255,  "step": 1},
    {"name": "gamma",              "label": "Gamma",              "type": "range", "min": 100,  "max": 300,  "step": 1},
    {"name": "white_balance_auto", "label": "Auto white balance", "type": "bool"},
    {"name": "white_balance_temp", "label": "WB temperature",     "type": "range", "min": 2800, "max": 6500, "step": 100},
]


def cam_cfg_raw():
    return json.loads(CONFIG_PATH.read_text())["camera"]


def save_cam_cfg(update):
    with _config_lock:
        raw = json.loads(CONFIG_PATH.read_text())
        raw["camera"].update(update)
        write_cfg_raw(raw)
    return raw["camera"]


def open_camera_locked():
    """(Re)open the capture device. Caller holds _camera['lock']."""
    if _camera["pump_stop"] is not None:
        _camera["pump_stop"].set()
    if _camera["cap"] is not None:
        _camera["cap"].release()
        _camera["cap"] = None
    _camera["error"] = None
    _camera["frame"] = None
    _camera["frame_t"] = 0.0
    c = cam_cfg_raw()
    dev = _capture_device(c["index"])
    if isinstance(dev, str):
        cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
    else:
        flag = cv2.CAP_DSHOW if sys.platform == "win32" else 0
        cap = cv2.VideoCapture(dev, flag)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, c["width"])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, c["height"])
    if cap.isOpened():
        apply_camera_controls(cap, c.get("controls"))
        if sys.platform.startswith("linux"):
            # UVC controls OpenCV can't reach, and that reset at reboot:
            # the bridge re-enables dynamic framerate on open, which
            # stretches exposures in the dim nest into motion blur (the
            # camera honors no manual exposure — bounding the frame
            # period is the only shutter control we have). Best-effort:
            # cameras without these controls just say no.
            dev = _capture_device(c["index"])
            node = dev if isinstance(dev, str) else f"/dev/video{dev}"

            def _pin_ctrls():
                try:
                    subprocess.run(
                        ("v4l2-ctl", "-d", node, "--set-ctrl",
                         "exposure_dynamic_framerate=0,"
                         "power_line_frequency=2"),
                        capture_output=True, timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            # off-thread: a slow v4l2-ctl must never sit inside the
            # camera lock (a mid-run watchdog reopen would stall the
            # run loop's captures behind it)
            threading.Thread(target=_pin_ctrls, daemon=True).start()
        _camera["cap"] = cap
        _camera["opened_t"] = time.monotonic()
        _camera["zoom"] = c.get("zoom") or {"factor": 1.0, "x": 0, "y": 0}
        _camera["head_prev"] = None
        stop = threading.Event()
        _camera["pump_stop"] = stop
        threading.Thread(target=_pump, args=(cap, stop), daemon=True).start()
    else:
        cap.release()
        _camera["error"] = "no camera found"
        _ensure_recovery()


def get_camera():
    if _camera["cap"] is not None:
        return _camera["cap"]     # fast path, no lock: state polls and
                                  # stream starts must not wait on reads
    with _camera["lock"]:
        if _camera["cap"] is None and _camera["error"] is None:
            open_camera_locked()
        return _camera["cap"]


def smoothed_head(frame):
    """Temporally-stabilized head circle for the live preview and capture.

    Small frame-to-frame detection wobble is blended away (EMA); a large
    jump means a different case was seated, so snap to it immediately.
    """
    det = imaging.find_head(frame)
    prev = _camera.get("head_prev")
    if prev is not None:
        drift = max(abs(det[0] - prev[0]), abs(det[1] - prev[1]),
                    abs(det[2] - prev[2]))
        if drift <= max(prev[2] * 0.15, 4):
            det = tuple(int(0.7 * p + 0.3 * d) for p, d in zip(prev, det))
    _camera["head_prev"] = det
    return det


def steady_head(n=4, budget_s=1.0):
    """(frame, (cx, cy, r)) — noise-averaged frame + median head circle
    over several DISTINCT fresh frames.

    Single-frame detections wobble with sensor noise and AE flicker; an
    off-center circle offsets the head crop and misplaces the primer
    mask, so train and sort quietly see different geometry. The median
    over a few frames is what retaking manually converges to — done
    automatically, per capture. (The original symptom was "warped
    polar strips" — that crop retired with the twins, but the wobble it
    exposed is universal.)

    The returned frame is the per-pixel mean of the sampled frames: the
    case is stationary in the nest, so averaging is pure sensor-noise
    reduction (~halved grain at n=4) with no motion blur. It's the
    software stand-in for the exposure/gain control this camera module
    doesn't honor — CLAHE downstream then sharpens lettering instead of
    amplifying grain. budget_s caps the wait so a slow camera mode
    degrades to fewer frames instead of stalling the feed cycle.
    Returns (None, None) when no camera."""
    dets, frames = [], []
    last_t = 0.0
    deadline = time.monotonic() + budget_s
    while len(frames) < n:
        f = read_frame()
        if f is None:
            break
        t = _camera["frame_t"]
        if t != last_t:                    # genuinely new frame from the pump
            last_t = t
            frames.append(f)
            dets.append(imaging.find_head(f))
        if len(frames) < n:
            if time.monotonic() > deadline:
                break
            time.sleep(0.04)
    if not frames:
        return None, None
    mid = len(dets) // 2
    center = tuple(int(sorted(d[k] for d in dets)[mid]) for k in range(3))
    frame = frames[0] if len(frames) == 1 else \
        np.mean(np.stack(frames), axis=0).astype(np.uint8)
    return frame, center


def read_frame(zoomed=True):
    """Newest frame from the pump (never blocks on the slow camera).

    Deliberately lock-free after initialization: the pump holds the camera
    lock for ~a frame interval per read, so any request that took the lock
    would queue behind the camera and lag by seconds.

    A frame older than FRAME_STALE_S counts as missing — if the pump dies,
    callers must see "no camera", not an endless replay of the last good
    frame (which would classify every case identically).
    """
    if _camera["cap"] is None:
        if _camera["error"] or get_camera() is None:
            return None
    deadline = time.monotonic() + 3.0     # camera warm-up after (re)open
    while _camera["frame"] is None or \
            time.monotonic() - _camera["frame_t"] > FRAME_STALE_S:
        if time.monotonic() > deadline or _camera["error"]:
            return None
        time.sleep(0.05)
    frame = _camera["frame"]
    return apply_zoom(frame, _camera["zoom"]) if zoomed else frame


# ---------------------------------------------------------------- helpers ---
def b64_jpg(img, max_side=360):
    h, w = img.shape[:2]
    scale = max_side / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (int(w * scale), int(h * scale)))
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return "data:image/jpeg;base64," + base64.b64encode(buf).decode()


def decode_upload(file_or_b64):
    if file_or_b64 is None:
        return None
    if isinstance(file_or_b64, str):  # data URL from canvas capture
        raw = base64.b64decode(file_or_b64.split(",", 1)[1])
    else:
        raw = file_or_b64.read()
    arr = np.frombuffer(raw, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def next_index(folder):
    existing = sorted(folder.glob("*_A.png"))
    return int(existing[-1].name.split("_")[0]) + 1 if existing else 0


# ----------------------------------------------------------------- routes ---
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


_embed_state_cache = {}


def _embed_state():
    """Embedding decider status for /api/state. Gallery stats cached by
    mtime — the npz decompresses on read, too heavy for every poll."""
    emb_m = MODELS_DIR / "shadow_embed.tflite"
    emb_g = MODELS_DIR / "shadow_gallery.npz"
    out = {"exists": emb_m.exists() and emb_g.exists()}
    if not out["exists"]:
        return out
    out["trained"] = time.strftime(
        "%Y-%m-%d %H:%M", time.localtime(emb_m.stat().st_mtime))
    key = (str(emb_g), emb_g.stat().st_mtime)
    if _embed_state_cache.get("key") != key:
        try:
            with np.load(emb_g, allow_pickle=False) as z:
                _embed_state_cache["stats"] = {
                    "gallery_vectors": int(z["g_vec"].shape[0]),
                    "gallery_classes": len(set(z["g_cls"].tolist()))}
            _embed_state_cache["key"] = key
        except Exception:
            _embed_state_cache["stats"] = {}
            _embed_state_cache["key"] = key
    out.update(_embed_state_cache.get("stats") or {})
    return out


def _identity(raw=None):
    """Who this install is: friendly name (config `machine_name`, falls
    back to hostname) + role. With several machines on one bench, every
    page must say whose page it is."""
    import socket as _s
    if raw is None:
        raw = json.loads(CONFIG_PATH.read_text())
    host = _s.gethostname()
    return {"name": (raw.get("machine_name") or "").strip() or host,
            "host": host, "trainer": _tf_available()}


@app.post("/api/machine/name")
def api_machine_name():
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
    if len(name) > 40:
        return jsonify({"error": "keep the name under 40 characters"}), 400
    with _config_lock:
        raw = json.loads(CONFIG_PATH.read_text())
        if name:
            raw["machine_name"] = name
        else:
            raw.pop("machine_name", None)     # back to the hostname
        write_cfg_raw(raw)
    return jsonify({"ok": True, "identity": _identity()})


# --------- dataset-count cache: the UI refreshes /api/state after every
# save/move/delete, and each call used to walk every class directory
# three times over. Counts are now cached; mutating handlers call
# _counts_dirty(), and a 10s TTL backstops any path that forgets.
_counts_cache = {}
_counts_lock = threading.Lock()


def _counts(kind, root):
    key = (kind, str(root))
    now = time.time()
    with _counts_lock:
        hit = _counts_cache.get(key)
        if hit and now - hit[1] < 10:
            return hit[0]
    val = (raw_counts(root) if kind == "raw"
           else dataset_counts(root / "stamp"))
    with _counts_lock:
        _counts_cache[key] = (val, now)
    return val


def _counts_dirty():
    with _counts_lock:
        _counts_cache.clear()


def _crop_total():
    return sum(_counts("stamp", DATA_DIR).values())


@app.get("/api/state")
def api_state():
    cfg = load_cfg()
    raw = json.loads(CONFIG_PATH.read_text())
    models = {"embed": _embed_state()}
    datasets = {}
    for name, root in (("real", DATA_DIR), ("synth", ROOT / "data" / "synth")):
        datasets[name] = {"stamp": _counts("stamp", root)}
    datasets["real"]["raw"] = _counts("raw", DATA_DIR)
    identity = _identity(raw)
    # only the machine (Linux/Pi) opens the camera eagerly to keep the
    # header truthful and the device warm for sorting. A trainer PC must
    # NOT light its webcam just because a page polled /api/state — there
    # the camera opens on first actual use (stream, capture, camera
    # settings). camera.eager in config overrides either way.
    if cam_cfg_raw().get("eager", sys.platform.startswith("linux")):
        get_camera()
    return jsonify({
        "cartridge": cfg.cartridge,
        "model": cfg.model_name,
        "profiles": profiles.list_profiles(ROOT),
        "stamp_labels": cfg.stamp_labels,
        "families": cfg.families,
        "bins": cfg.bins,
        "bin_count": cfg.bin_count,
        "bin_sizes": (raw.get("machine") or {}).get("bin_sizes") or [],
        "bin_colors": (raw.get("machine") or {}).get("bin_colors") or [],
        "slots_enabled": cfg.slots_enabled,
        "unmatched_bin": cfg.unmatched_bin,
        "floors": cfg.floors,
        "imaging": cfg.imaging,
        "train_disabled": cfg.train_disabled,
        "models": models,
        "datasets": datasets,
        "identity": identity,
        # "idle" = not opened (lazy trainer) — never a false "ok"
        "camera": ("ok" if _camera["cap"] is not None
                   else _camera["error"] or "idle"),
        "train": train_status,
    })


@app.post("/api/model/switch")
def api_model_switch():
    """Change the active caliber/model. Its dataset, crops, and weights swap in."""
    b = request.get_json() or {}
    try:
        set_active_model(b["cartridge"], b["model"])
    except (KeyError, ValueError, FileNotFoundError) as e:
        return jsonify({"error": f"can't switch: {e}"}), 400
    return jsonify({"ok": True, "cartridge": b["cartridge"], "model": b["model"]})


@app.post("/api/model/create")
def api_model_create():
    """Create a model (optionally seeded from another model's images) and,
    unless activate=False, switch to it."""
    b = request.get_json() or {}
    cart = (b.get("cartridge") or "").strip()
    name = (b.get("model") or "").strip()
    if not cart or not name:
        return jsonify({"error": "cartridge and model name are required"}), 400
    seed = None
    if b.get("seed_from"):
        seed = (b["seed_from"]["cartridge"], b["seed_from"]["model"])
    try:
        profiles.create_model(ROOT, cart, name, seed_from=seed,
                              imaging=b.get("imaging"))
        seeded_embed = False
        if b.get("seed_embed"):
            # borrow the ACTIVE model's trained recognizer so the new
            # profile can classify from day one — the embedding
            # generalizes to unseen brass (few-shot is the product), so
            # a new caliber can collect and sort before its first
            # training run. The bench sidecar deliberately does NOT
            # travel (its numbers describe the source dataset — stale
            # provenance is worse than none), and the gallery is built
            # from the new profile's own photos, so classes never leak
            # across profiles.
            import shutil
            act = json.loads(CONFIG_PATH.read_text())["active"]
            src = (profiles.model_dir(ROOT, act["cartridge"], act["model"])
                   / "models" / "shadow_embed.tflite")
            if src.is_file():
                dst_dir = profiles.model_dir(ROOT, cart, name) / "models"
                dst_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst_dir / "shadow_embed.tflite")
                mj_path = profiles.model_dir(ROOT, cart, name) / "model.json"
                mj = json.loads(mj_path.read_text())
                mj["embed_seeded_from"] = (f"{act['cartridge']}/{act['model']}"
                                           f" {time.strftime('%Y-%m-%d')}")
                mj_path.write_text(json.dumps(mj, indent=2))
                seeded_embed = True
        if b.get("activate", True):
            set_active_model(cart, name)
    except (ValueError, KeyError, FileNotFoundError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "cartridge": cart, "model": name,
                    "seeded_embed": seeded_embed})


@app.post("/api/model/merge")
def api_model_merge():
    """COPY another model's raw images into the ACTIVE model, per class,
    renumbered after the existing pairs — cross-camera dataset
    consolidation (image diversity trains better; per-rig specificity
    lives in the gallery, not the dataset). Classes the active model has
    never heard of are created and added to its class list, and reported
    so the operator can review/rename. The source model is left intact —
    delete it separately once the merge is verified."""
    b = request.get_json() or {}
    cart, src = b.get("cartridge"), b.get("model")
    profs = profiles.list_profiles(ROOT)
    if src not in profs.get(cart, []):
        return jsonify({"error": f"no such model {cart}/{src}"}), 404
    active = json.loads(CONFIG_PATH.read_text())["active"]
    if (cart, src) == (active["cartridge"], active["model"]):
        return jsonify({"error": "that IS the active model"}), 400
    src_raw = profiles.model_dir(ROOT, cart, src) / "data" / "raw"
    if not src_raw.is_dir():
        return jsonify({"error": f"{src} has no images"}), 400
    import shutil
    copied, new_classes = {}, []
    with _config_lock:
        raw_cfg = active_model_raw()
        labels = set(raw_cfg["stamp_labels"])
        for d in sorted(p for p in src_raw.iterdir() if p.is_dir()):
            label = d.name
            dst = DATA_DIR / "raw" / label
            dst.mkdir(parents=True, exist_ok=True)
            existing = sorted(dst.glob("*_A.png"))
            nxt = int(existing[-1].name.split("_")[0]) + 1 if existing else 0
            n = 0
            for a in sorted(d.glob("*_A.png")):
                idx = a.name.split("_")[0]
                for suffix in ("A", "B"):
                    p = d / f"{idx}_{suffix}.png"
                    if p.exists():
                        shutil.copy2(p, dst / f"{nxt:04d}_{suffix}.png")
                nxt += 1
                n += 1
            copied[label] = n
            base_cls = parse_label(label)[0]
            if base_cls not in labels:
                raw_cfg["stamp_labels"].append(base_cls)
                labels.add(base_cls)
                new_classes.append(base_cls)
        if new_classes:
            write_active_model(raw_cfg)
    load_cfg()
    n_crops, _ = rebuild_crops(DATA_DIR, incremental=True)
    _counts_dirty()
    return jsonify({"ok": True, "from": f"{cart}/{src}",
                    "pairs_copied": sum(copied.values()),
                    "per_class": copied, "new_classes": new_classes,
                    "crops": n_crops})


@app.post("/api/families")
def api_families():
    """Create, update, or delete a family — a named group of classes
    that a slot can hold as one unit (stored in bins as a
    "family:NAME" token, expanded at config load). Members are
    validated against the class list; deleting a family also pulls its
    token out of any slot so no orphan tokens linger."""
    from sorter.config import normalize_bin
    body = request.get_json() or {}
    name = (body.get("name") or "").strip().upper()
    if not name or any(c in name for c in "/\\:"):
        return jsonify({"error": "a family needs a simple name "
                        "(no slashes or colons)"}), 400
    with _config_lock:
        raw = active_model_raw()
        fams = raw.setdefault("families", {})
        if body.get("delete"):
            fams.pop(name, None)
            raw["bins"] = [[s for s in normalize_bin(b)
                            if s != f"family:{name}"]
                           for b in raw.get("bins", [])]
        else:
            known = set(raw["stamp_labels"])
            members = sorted({str(s) for s in (body.get("members") or [])
                              if s in known})
            if not members:
                return jsonify({"error": "pick at least one member "
                                "class"}), 400
            fams[name] = members
        write_active_model(raw)
    load_cfg()
    return jsonify({"ok": True, "families": raw.get("families", {})})


@app.post("/api/model/delete")
def api_model_delete():
    """Delete a model (its dataset + weights + config). Can't delete the last
    one; deleting the active model switches to another first."""
    b = request.get_json() or {}
    cart, model = b.get("cartridge"), b.get("model")
    profs = profiles.list_profiles(ROOT)
    if sum(len(v) for v in profs.values()) <= 1:
        return jsonify({"error": "can't delete the only model — create another first"}), 400
    if model not in profs.get(cart, []):
        return jsonify({"error": f"no such model {cart}/{model}"}), 404
    active = json.loads(CONFIG_PATH.read_text()).get("active", {})
    if active.get("cartridge") == cart and active.get("model") == model:
        # switch to any other remaining model before deleting the active one
        other = next(((c, m) for c, ms in profs.items() for m in ms
                      if (c, m) != (cart, model)), None)
        set_active_model(*other)
    profiles.delete_model(ROOT, cart, model)
    a = json.loads(CONFIG_PATH.read_text())["active"]
    return jsonify({"ok": True, "active": a})


_BAD_STAMP_CHARS = set('<>:"/\\|?*')


def _valid_stamp(name):
    return name and name != "OTHER" and ".." not in name and \
        not (set(name) & _BAD_STAMP_CHARS)


@app.post("/api/stamps/add")
def api_stamps_add():
    """Create a new headstamp (Collect tab). Routing follows automatically."""
    name = (request.get_json() or {}).get("name", "").strip().upper()
    if not _valid_stamp(name):
        return jsonify({"error": f"invalid headstamp name {name!r}"}), 400
    with _config_lock:
        raw = active_model_raw()
        if name in raw["stamp_labels"]:
            return jsonify({"error": f"{name} already exists"}), 409
        raw["stamp_labels"].append(name)
        write_active_model(raw)
    load_cfg()
    return jsonify({"ok": True, "added": name})


@app.post("/api/stamps/remove")
def api_stamps_remove():
    """Remove a headstamp from sorting; optionally delete its images too."""
    body = request.get_json() or {}
    name = body.get("name", "").strip().upper()
    with _config_lock:
        raw = active_model_raw()
        if name not in raw["stamp_labels"]:
            return jsonify({"error": f"{name} is not a configured headstamp"}), 404
        if len(raw["stamp_labels"]) == 1:
            return jsonify({"error": "can't remove the last headstamp"}), 400
        raw["stamp_labels"].remove(name)
        raw["train_disabled"] = [s for s in raw.get("train_disabled", [])
                                 if s != name]
        clean_bins(raw)
        write_active_model(raw)
    load_cfg()
    deleted = 0
    if body.get("delete_data"):
        try:
            deleted = delete_label(DATA_DIR, name)
        except ValueError:
            pass
        rebuild_crops(DATA_DIR, incremental=True)
        _counts_dirty()
    return jsonify({"ok": True, "removed": name, "images_deleted": deleted})


@app.post("/api/classes")
def api_classes():
    body = request.get_json()
    with _config_lock:
        raw = active_model_raw()
        stamps = raw["stamp_labels"]
        if "train_disabled" in body:
            dis = [str(s) for s in body["train_disabled"]]
            if any(s not in stamps for s in dis):
                return jsonify({"error": "train_disabled names an unknown "
                                         "headstamp"}), 400
            raw["train_disabled"] = sorted(set(dis))
        if "bins" in body:
            from sorter.config import normalize_bin
            total = int(machine_settings()["slots_total"])
            bins = [normalize_bin(b) for b in body["bins"]][:total]
            named = [s for g in bins for s in g
                     if s not in ("UNMATCHED", "OVERFLOW")]
            if sum("UNMATCHED" in g for g in bins) != 1:
                return jsonify({"error": "exactly one bin must be UNMATCHED"}), 400
            if sum("OVERFLOW" in g for g in bins) > 1:
                return jsonify({"error": "at most one bin can be OVERFLOW"}), 400
            if len(named) != len(set(named)):
                return jsonify({"error": "a headstamp can only occupy one bin"}), 400
            fams = set((raw.get("families") or {}).keys())
            if any(s not in stamps
                   and not (s.startswith("family:")
                            and s[len("family:"):] in fams)
                   for s in named):
                return jsonify({"error": "bin assigned to an unknown headstamp"}), 400
            raw["bins"] = bins
        if "floors" in body:
            for k in raw["floors"]:
                if k in body["floors"]:
                    raw["floors"][k] = float(body["floors"][k])
        crops_rebuilt = False
        if "imaging" in body:
            old = dict(raw.get("imaging", {}))
            raw.setdefault("imaging", {})
            raw["imaging"]["pocket_frac"] = round(float(body["imaging"].get(
                "pocket_frac", old.get("pocket_frac", 0.42))), 3)
            raw["imaging"]["pocket_circle"] = bool(body["imaging"].get(
                "pocket_circle", old.get("pocket_circle", True)))
            raw["imaging"]["head_donut"] = bool(body["imaging"].get(
                "head_donut", old.get("head_donut", True)))
            mode = body["imaging"].get("crop_mode", old.get("crop_mode", "normal"))
            raw["imaging"]["crop_mode"] = mode if mode in ("normal", "primer_only") else "normal"
            # polar + gray retired with the twins (benched worse for the
            # distilled student): stored values pass through untouched,
            # the API no longer accepts changes
            raw["imaging"]["clahe"] = imaging._clahe_strength(
                body["imaging"].get("clahe", old.get("clahe", 0)))
            raw["imaging"]["enhance"] = imaging._enhance_mode(
                body["imaging"].get("enhance", old.get("enhance")),
                raw["imaging"]["clahe"])
            raw["imaging"]["enhance_size"] = imaging._enhance_size(
                body["imaging"].get("enhance_size", old.get("enhance_size", 13)))
            raw["imaging"]["rim_scale"] = round(min(max(float(
                body["imaging"].get("rim_scale", old.get("rim_scale", 1.0))),
                0.8), 1.2), 3)
            crops_rebuilt = raw["imaging"] != old
        write_active_model(raw)
    load_cfg()  # validates + applies imaging params process-wide
    if crops_rebuilt:
        rebuild_crops(DATA_DIR)  # dataset must match what the pipeline will see
    return jsonify({"ok": True, "crops_rebuilt": crops_rebuilt})


@app.get("/api/stream")
def api_stream():
    # ?fast=1: raw frames only — no circle fit, no sharpness, no overlay.
    # A third of the CPU and visibly lower latency; made for hardware/
    # lighting sessions where the operator just needs to SEE the camera.
    fast = request.args.get("fast") == "1"

    def gen():
        misses = 0
        while True:
            frame = read_frame(zoomed=False)
            if frame is not None:
                zp = _camera.get("zoom_preview")
                if zp is not None and time.monotonic() - zp["t"] > 120:
                    _camera["zoom_preview"] = zp = None   # abandoned tune-up
                elif zp is not None:
                    # someone is WATCHING this tune-up: keep it alive. The
                    # expiry now only fires when no stream has served the
                    # preview for 2 min (the real abandoned case) — it used
                    # to snap the view back mid-focus-session.
                    zp["t"] = time.monotonic()
                z = zp or _camera["zoom"]
                if z:
                    frame = apply_zoom(frame, z)
            if frame is None:
                # a camera reopen (resolution change, preset apply) starves
                # the pump for a few seconds — ride it out instead of dying,
                # or every open browser keeps a silently frozen "live" view
                misses += 1
                if misses > 80:            # ~8s of nothing: camera truly gone
                    break
                time.sleep(0.1)
                continue
            misses = 0
            if fast:
                view = frame
            else:
                cx, cy, r = smoothed_head(frame)
                view = frame.copy()
                cv2.circle(view, (cx, cy), imaging._rim(r), (0, 255, 0), 2)
                cv2.putText(view, f"sharpness {imaging.sharpness(frame):.0f}",
                            (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
            # right-size for VIEWING: full-res frames at ~9fps saturate the
            # Pi's Wi-Fi (~9 of its ~12 Mbit/s) and the stream falls seconds
            # behind while starving every other request. The browser shows
            # this at ~500px anyway; captures stay full resolution.
            h, w = view.shape[:2]
            if w > 800:
                view = cv2.resize(view, (800, int(h * 800 / w)))
            ok, buf = cv2.imencode(".jpg", view, [cv2.IMWRITE_JPEG_QUALITY, 65])
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n")
            time.sleep(0.10)
    if get_camera() is None:
        return jsonify({"error": "no camera"}), 404
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


# our control name -> the V4L2 control name (for real slider ranges on Linux)
_V4L2_NAME = {"brightness": "brightness", "contrast": "contrast",
              "saturation": "saturation", "sharpness": "sharpness",
              "gain": "gain", "gamma": "gamma",
              "exposure": "exposure_time_absolute",
              "focus": "focus_absolute",
              "white_balance_temp": "white_balance_temperature"}


def _v4l2_ranges(index):
    """Ask the driver for each control's REAL min/max/step (Linux only).

    The static CONTROL_META ranges follow Windows/DirectShow conventions;
    UVC modules differ wildly (this project's OV3660 wants brightness
    -64..64, gain 0..100, sharpness 0..6, exposure 1..5000) — sliders with
    the wrong range silently clamp into uselessness."""
    try:
        dev = _capture_device(int(index))
        if not isinstance(dev, str):
            dev = f"/dev/video{dev}"
        out = subprocess.run(("v4l2-ctl", "-d", dev, "-l"),
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return {}
    import re
    ranges = {}
    for line in out.splitlines():
        m = re.match(r"\s*(\w+)\s+0x\w+\s+\(int\)\s*:\s*"
                     r"min=(-?\d+)\s+max=(-?\d+)\s+step=(\d+)", line)
        if m:
            ranges[m.group(1)] = {"min": int(m.group(2)), "max": int(m.group(3)),
                                  "step": int(m.group(4))}
    return ranges


# ------------------------------------------------------- camera identity ---
# Which physical camera is attached decides WHICH controls are real: both
# drivers happily enumerate knobs that do nothing (or worse). The user's
# choice lives in config camera.model; USB detection is shown beside the
# selector and warns on a mismatch but never overrides the user.
CAMERA_MODELS = {
    "ov3660": {
        "label": "Stock OV3660",
        "usb": ("0c45", "6366"),
        # the classic UVC set the stock camera has always shown; exposure
        # and gain are known-decorative in MJPG mode, but the shipped
        # "collection" recipe pushes them, so they stay visible
        "controls": ["auto_exposure", "exposure", "gain", "brightness",
                     "contrast", "saturation", "sharpness",
                     "white_balance_auto", "white_balance_temp"],
    },
}


def detect_camera_model(index):
    """USB-ID sniff of /dev/video<index> via sysfs. Linux only; returns the
    CAMERA_MODELS key, or None when unknown / not detectable (dev Mac)."""
    try:
        dev = _capture_device(int(index))
        node = dev if isinstance(dev, str) else f"/dev/video{dev}"
        # _capture_device prefers the /dev/v4l/by-id symlink; sysfs wants
        # the real videoN name, so resolve the link first
        name = Path(node).resolve().name
        p = (Path("/sys/class/video4linux") / name / "device").resolve()
        for _ in range(5):        # climb to the USB device that carries the ids
            vid, pid = p / "idVendor", p / "idProduct"
            if vid.exists() and pid.exists():
                ids = (vid.read_text().strip().lower(),
                       pid.read_text().strip().lower())
                return next((k for k, m in CAMERA_MODELS.items()
                             if m["usb"] == ids), None)
            p = p.parent
    except Exception:
        pass
    return None


def _migrate_camera_config():
    """One-time: the flat global camera_presets dict becomes per-camera
    blocks under `cameras`, and camera.model records which device is in
    use. Existing presets are assigned by name ("imx" anywhere -> imx415);
    the initial selection honors USB detection so a machine that's already
    running the IMX415 doesn't get flipped to the stock default."""
    with _config_lock:
        raw = json.loads(CONFIG_PATH.read_text())
        if "cameras" in raw:
            return
        old = raw.pop("camera_presets", {}) or {}
        cams = {k: {"presets": {}, "saved": None} for k in CAMERA_MODELS}
        for name, p in old.items():
            key = "imx415" if "imx" in name.lower() else "ov3660"
            cams.setdefault(key, {"presets": {}, "saved": None})
            cams[key]["presets"][name] = p
        raw["cameras"] = cams
        cam = raw.setdefault("camera", {})
        cam["model"] = detect_camera_model(cam.get("index", 0)) or "ov3660"
        write_cfg_raw(raw)


_migrate_camera_config()


def _cam_presets(raw):
    """The ACTIVE camera's preset dict (mutable view into `raw`)."""
    cur = raw.get("camera", {}).get("model") or "ov3660"
    return (raw.setdefault("cameras", {})
               .setdefault(cur, {"presets": {}, "saved": None})
               .setdefault("presets", {}))


# camera lock: RETIRED. Per-rig galleries absorb capture changes and the
# dataset keeps every era's images for training — locking raw settings
# protected a softmax-era invariant that no longer exists.
def camera_lock_status():
    return {"locked": False, "override": False}


def _camera_locked_resp():
    return None


@app.get("/api/camera/model")
def api_camera_model():
    raw = json.loads(CONFIG_PATH.read_text())
    cam = raw.get("camera", {})
    return jsonify({"model": cam.get("model") or "ov3660",
                    "detected": detect_camera_model(cam.get("index", 0)),
                    "models": {k: m["label"] for k, m in CAMERA_MODELS.items()}})


@app.post("/api/camera/model")
def api_camera_model_post():
    locked = _camera_locked_resp()
    if locked:
        return locked
    key = ((request.get_json() or {}).get("model") or "").strip()
    if key not in CAMERA_MODELS:
        return jsonify({"error": f"unknown camera {key!r}"}), 400
    with _config_lock:
        raw = json.loads(CONFIG_PATH.read_text())
        cam = raw.setdefault("camera", {})
        old = cam.get("model") or "ov3660"
        if key == old:
            return jsonify({"ok": True, "model": key})
        cams = raw.setdefault("cameras", {})
        # stash the leaving camera's live look, restore the arriving one's
        cams.setdefault(old, {"presets": {}})["saved"] = {
            "width": cam.get("width"), "height": cam.get("height"),
            "zoom": cam.get("zoom"), "controls": cam.get("controls", {}),
            "active_preset": cam.get("active_preset"),
            "camera_led": machine_settings()["camera_led"],
            "led_color": machine_settings().get("led_color", "#ffffff")}
        cam["model"] = key
        s = (cams.get(key) or {}).get("saved") or {}
        for k2 in ("width", "height", "zoom", "controls"):
            if s.get(k2) is not None:
                cam[k2] = s[k2]
        if not s:
            cam["controls"] = {}          # fresh camera: driver defaults
        cam["active_preset"] = s.get("active_preset")
        write_cfg_raw(raw)
    with _camera["lock"]:
        open_camera_locked()
    led_pushed = False
    if s.get("led_color"):
        save_machine_settings({"led_color": s["led_color"]})
        if _console["transport"] is not None:
            _apply_machine_settings({"led_color":
                                     machine_settings()["led_color"]})
    if s.get("camera_led") is not None:
        m = save_machine_settings({"camera_led": s["camera_led"]})
        if _console["transport"] is not None:
            led_pushed = bool(_apply_machine_settings({"camera_led": m["camera_led"]}))
    return jsonify({"ok": True, "model": key,
                    "camera": _camera["error"] or "ok", "led_pushed": led_pushed})


@app.get("/api/camera/settings")
def api_camera_settings():
    cap = get_camera()
    if cap is None:
        return jsonify({"error": "no camera"}), 404
    c = cam_cfg_raw()
    saved = c.get("controls", {})
    controls = []
    v4l = _v4l2_ranges(c["index"])
    with _camera["lock"]:
        for meta in CONTROL_META:
            prop = CAMERA_PROPS[meta["name"]]
            current = cap.get(prop)
            item = dict(meta)
            item["supported"] = current != -1.0
            r = v4l.get(_V4L2_NAME.get(meta["name"], ""))
            if r and item.get("type") == "range":
                item.update(r)                 # honest min/max/step from the driver
            elif v4l and item.get("type") == "range" \
                    and _V4L2_NAME.get(meta["name"]) is not None:
                item["supported"] = False      # driver enumerates, control absent
            if meta["name"] == "auto_exposure":
                item["value"] = bool(saved.get("auto_exposure",
                                               current >= auto_exposure_value(True)))
            elif meta["type"] == "bool":
                item["value"] = bool(saved.get(meta["name"], current > 0))
            else:
                item["value"] = saved.get(meta["name"], current)
            controls.append(item)
    with _camera["lock"]:
        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    # curate to the selected camera's REAL knobs: both drivers enumerate
    # controls that do nothing (or don't exist) - the whitelist is measured
    # ground truth per camera, not driver claims
    cur_model = c.get("model") or "ov3660"
    allowed = CAMERA_MODELS.get(cur_model, {}).get("controls")
    if allowed is not None:
        controls = [i for i in controls if i["name"] in allowed]
    return jsonify({"index": c["index"],
                    "camera_model": cur_model,
                    "detected": detect_camera_model(c["index"]),
                    "model_labels": {k: m["label"] for k, m in CAMERA_MODELS.items()},
                    "lock": camera_lock_status(),
                    "resolution": {"width": c.get("width"), "height": c.get("height"),
                                   "actual_width": actual_w, "actual_height": actual_h},
                    "zoom": _camera["zoom"] or {"factor": 1.0, "x": 0, "y": 0},
                    "controls": controls})


@app.post("/api/camera/settings")
def api_camera_settings_post():
    locked = _camera_locked_resp()     # every branch changes the raw image
    if locked:
        return locked
    body = request.get_json()
    cap = get_camera()

    if body.get("reset"):
        save_cam_cfg({"controls": {}, "zoom": {"factor": 1.0, "x": 0, "y": 0}})
        with _camera["lock"]:
            open_camera_locked()  # driver defaults come back on a fresh open
        return jsonify({"ok": True})

    if "index" in body:
        save_cam_cfg({"index": int(body["index"])})
        with _camera["lock"]:
            open_camera_locked()
        if _camera["error"]:
            return jsonify({"error": f"camera {body['index']}: not found"}), 404
        return jsonify({"ok": True})

    if "resolution" in body:
        w, h = int(body["resolution"]["width"]), int(body["resolution"]["height"])
        if not (160 <= w <= 4096 and 120 <= h <= 4096):
            return jsonify({"error": "resolution out of range"}), 400
        save_cam_cfg({"width": w, "height": h})
        with _camera["lock"]:
            open_camera_locked()   # the driver snaps to the nearest mode it has
            if _camera["error"]:
                return jsonify({"error": _camera["error"]}), 404
            actual = {"width": int(_camera["cap"].get(cv2.CAP_PROP_FRAME_WIDTH)),
                      "height": int(_camera["cap"].get(cv2.CAP_PROP_FRAME_HEIGHT))}
        return jsonify({"ok": True, "actual": actual})

    if body.get("zoom_preview_clear"):
        _camera["zoom_preview"] = None
        return jsonify({"ok": True})

    if "zoom" in body:
        z = {"factor": min(max(float(body["zoom"].get("factor", 1.0)), 1.0), 8.0),
             "x": min(max(float(body["zoom"].get("x", 0.0)), -1.0), 1.0),
             "y": min(max(float(body["zoom"].get("y", 0.0)), -1.0), 1.0)}
        if body.get("preview"):
            # live view only — captures keep the saved zoom until Save
            _camera["zoom_preview"] = {**z, "t": time.monotonic()}
            return jsonify({"ok": True, "preview": True})
        _camera["zoom"] = z
        _camera["zoom_preview"] = None
        save_cam_cfg({"zoom": z})
        return jsonify({"ok": True, "zoom": z})

    name, value = body.get("name"), body.get("value")
    if name not in CAMERA_PROPS:
        return jsonify({"error": f"unknown control {name!r}"}), 400
    if cap is None:
        return jsonify({"error": "no camera"}), 404
    with _camera["lock"]:
        raw_value = auto_exposure_value(bool(value)) if name == "auto_exposure" \
            else float(value)
        cap.set(CAMERA_PROPS[name], raw_value)
        actual = cap.get(CAMERA_PROPS[name])
    with _config_lock:
        raw = json.loads(CONFIG_PATH.read_text())
        raw["camera"].setdefault("controls", {})[name] = value
        write_cfg_raw(raw)
    return jsonify({"ok": True, "actual": actual})


# ------------------------------------------------------- camera presets ---
# A preset is a named snapshot of the image look: resolution, digital zoom,
# hardware controls, and light-ring brightness. The camera index is NOT part
# of a preset — that's which physical device, not how the picture looks.
def _preset_snapshot():
    c = cam_cfg_raw()
    return {"width": c.get("width"), "height": c.get("height"),
            "zoom": c.get("zoom") or {"factor": 1.0, "x": 0, "y": 0},
            "controls": c.get("controls", {}),
            "camera_led": machine_settings()["camera_led"],
            "led_color": machine_settings().get("led_color", "#ffffff")}


@app.get("/api/camera/presets")
def api_camera_presets():
    raw = json.loads(CONFIG_PATH.read_text())
    presets = _cam_presets(raw)        # only the ACTIVE camera's presets
    # active = last preset applied or saved; "modified" is computed fresh by
    # comparing the live snapshot, so tweaking any dial shows up honestly
    active = raw.get("camera", {}).get("active_preset")
    if active not in presets:
        active = None
    modified = bool(active) and _preset_snapshot() != presets[active]
    return jsonify({"presets": presets, "active": active, "modified": modified})


@app.post("/api/camera/presets")
def api_camera_presets_save():
    name = ((request.get_json() or {}).get("name") or "").strip()
    if not name or len(name) > 40:
        return jsonify({"error": "preset needs a name (max 40 chars)"}), 400
    snap = _preset_snapshot()
    with _config_lock:
        raw = json.loads(CONFIG_PATH.read_text())
        _cam_presets(raw)[name] = snap
        raw["camera"]["active_preset"] = name   # saving current == running it
        write_cfg_raw(raw)
    return jsonify({"ok": True, "saved": name})


@app.post("/api/camera/presets/apply")
def api_camera_presets_apply():
    locked = _camera_locked_resp()     # a preset rewrites the whole look
    if locked:
        return locked
    name = ((request.get_json() or {}).get("name") or "").strip()
    p = _cam_presets(json.loads(CONFIG_PATH.read_text())).get(name)
    if p is None:
        return jsonify({"error": f"no preset named {name!r}"}), 404
    update = {k: p[k] for k in ("width", "height", "zoom", "controls")
              if p.get(k) is not None}
    update["active_preset"] = name
    save_cam_cfg(update)
    with _camera["lock"]:
        open_camera_locked()   # reopen applies resolution + saved controls
    led_pushed = False
    if p.get("led_color"):
        save_machine_settings({"led_color": p["led_color"]})
        if _console["transport"] is not None:
            _apply_machine_settings({"led_color":
                                     machine_settings()["led_color"]})
    if p.get("camera_led") is not None:
        m = save_machine_settings({"camera_led": p["camera_led"]})
        if _console["transport"] is not None:
            led_pushed = bool(_apply_machine_settings({"camera_led": m["camera_led"]}))
    return jsonify({"ok": True, "camera": _camera["error"] or "ok",
                    "led_pushed": led_pushed})


@app.post("/api/camera/presets/delete")
def api_camera_presets_delete():
    name = ((request.get_json() or {}).get("name") or "").strip()
    with _config_lock:
        raw = json.loads(CONFIG_PATH.read_text())
        presets = _cam_presets(raw)
        if name not in presets:
            return jsonify({"error": f"no preset named {name!r}"}), 404
        del presets[name]
        write_cfg_raw(raw)
    return jsonify({"ok": True, "deleted": name})


@app.post("/api/capture")
def api_capture():
    """Freeze one frame; returns the full frame plus the exact crops that
    will be saved, so the user can review before committing."""
    frame, center = steady_head()
    if frame is None:
        return jsonify({"error": "no camera"}), 404
    return jsonify({"image": b64_jpg(frame, max_side=1600),
                    "sharpness": round(float(imaging.sharpness(frame)), 1),
                    "center": list(center),
                    "head": b64_jpg(imaging.crop_head(frame, center=center), max_side=320)})


def save_labeled(stamp, frame, center=None):
    """File a labeled case-head photo into the dataset. Returns (label, index).

    `center` carries the capture-time (median-stabilized) head circle so the
    saved training crop is EXACTLY the strip the operator approved — a fresh
    single-frame re-detection here could disagree and save a warped strip."""
    raw_dir = DATA_DIR / "raw" / stamp
    raw_dir.mkdir(parents=True, exist_ok=True)
    idx = next_index(raw_dir)
    cv2.imwrite(str(raw_dir / f"{idx:04d}_A.png"), frame)
    stamp_dir = DATA_DIR / "stamp" / stamp
    stamp_dir.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(stamp_dir / f"{stamp}_{idx:04d}_A.png"),
                imaging.crop_head(frame, center=center))
    _counts_dirty()
    return stamp, idx


@app.post("/api/save_pair")
def api_save_pair():
    stamp = request.form.get("stamp", "").strip().upper()
    force = request.form.get("force") == "1"
    frame = decode_upload(request.files.get("frameA") or request.form.get("frameA_b64"))
    center = None
    try:
        if request.form.get("cx") is not None:
            center = (int(request.form["cx"]), int(request.form["cy"]),
                      int(request.form["cr"]))
    except (KeyError, ValueError):
        center = None
    if not stamp:
        return jsonify({"error": "stamp is required"}), 400
    if frame is None:
        return jsonify({"error": "an image is required"}), 400
    cfg = load_cfg()

    # blur guard: keep unusable images out of the training data
    if not force:
        floor = cfg.floors["sharpness"]
        s = imaging.sharpness(frame)
        if s < floor:
            return jsonify({"error": f"image looks blurry "
                            f"(sharpness {s:.0f} < floor {floor:.0f})",
                            "blurry": True, "sharpness": round(s, 1)}), 400
        # novelty gate — same rule as batch filing: pixel-verified "this
        # exact case is already filed". Save anyway overrides (a
        # deliberate capture is refused softly, never silently dropped).
        sh = get_shadow()
        n_imgs = _counts("stamp", DATA_DIR).get(stamp, 0)
        if sh is not None and stamp != "UNKNOWN" \
                and n_imgs >= NOVELTY_MIN_IMAGES:
            dup, det, _ = _same_case_in_bank(
                sh, stamp, imaging.crop_head(frame, center=center))
            if dup:
                return jsonify({
                    "error": f"this case is already filed as {det['match']} "
                             f"({det['corr']:.0%} pixel match) — "
                             "Save anyway to keep it",
                    "duplicate": True, **det}), 400

    label, idx = save_labeled(stamp, frame, center)
    _counts_dirty()
    return jsonify({"ok": True, "saved": label, "index": idx,
                    "counts": _counts("stamp", DATA_DIR)})


# ----------------------------------------------------- chained collection ---
# Let a freshly seated case stop vibrating first. Measured on the real
# machine (stream probe over live feeds): sharpness is back
# to baseline 100-160ms after the firmware's done/SEATED — 0.15 keeps
# margin. Used by run AND collect captures, so the train/serve imaging
# distributions move together.
COLLECT_SETTLE_S = 0.15


def _collect_predict(frame, center=None):
    """Top-1 embedding read for the Collect loop; None when no embedding
    model is installed or the frame produced no usable read. `confident`
    mirrors whether the decider would have accepted the read (so the UI
    can pre-select). center: the capture flow's smoothed head circle, so
    the prediction sees the exact crop that a save would store."""
    sh = get_shadow()
    if sh is None:
        return None
    from sorter.embed_classifier import EmbedDecider
    d = EmbedDecider(load_cfg(), sh).classify(frame, center=center)
    if not d.stamp:
        return None
    return {"stamp": d.stamp, "conf": round(d.stamp_conf, 4),
            "confident": d.reason in ("ok", "no_bin_mapping")}


@app.post("/api/collect/advance")
def api_collect_advance():
    """One step of the chained capture loop: optionally feed the next case
    (xf:0 — one CS7.2 `done` means the sort finished AND the next case is
    seated), settle, then freeze + classify a frame for labeling."""
    body = request.get_json() or {}
    fed = False
    if body.get("feed"):
        if _console["transport"] is None:
            return jsonify({"error": "connect to the machine first"}), 409
        # sort-while-training: drop the case just labeled into its stamp's
        # bin (unassigned stamps and skips go to the UNMATCHED slot);
        # otherwise everything parks in the firmware home slot 0
        slot = 0
        if body.get("sort"):
            c = load_cfg()
            stamp = (body.get("stamp") or "").strip().upper()
            slot = c.bin_map.get((stamp, None), c.unmatched_bin)
        # a sorted feed adds real arm travel + the slot drop delay before
        # the feed even starts — 8s (fine for xf:0) times out on far bins
        r = _console_request(f"xf:{slot}", lambda l: l.strip() in
                             ("done", "error:feed overtravel detected"),
                             timeout=25.0)
        if r is None:
            return jsonify({"error": "no reply from the board (timeout)"}), 504
        if r.strip() != "done":
            return jsonify({"error": r.strip(), "jam": True}), 409
        fed = True
        time.sleep(COLLECT_SETTLE_S)
    frame, center = steady_head()
    source = "camera"
    if frame is None and _console["mode"] == "sim":
        # off-hardware testing: the fake board feeds, a synthetic case appears
        spec = synth.CaseSpec.random(load_cfg())
        frame = synth.render(spec, "A", seed=random.randrange(1 << 30))
        center = imaging.find_head(frame)
        source = "synthetic"
    if frame is None:
        return jsonify({"error": "no camera"}), 404
    floor = load_cfg().floors["sharpness"]
    if source == "camera" and imaging.sharpness(frame) < floor:
        # blur auto-retake: one fresh grab before bothering the user
        time.sleep(0.15)
        again, c2 = steady_head()
        if again is not None and imaging.sharpness(again) > imaging.sharpness(frame):
            frame, center = again, c2
    sharp = round(float(imaging.sharpness(frame)), 1)
    # predict=false skips inference entirely — faster feeds, and an early
    # model's guesses are noise the operator asked not to see
    predict = body.get("predict", True)
    # empty-nest guard: a force-feed skips the proximity sensor, so a missed
    # slot shows the camera a bare nest — flag it so the UI can refuse a save
    present = imaging.case_present(frame) if source == "camera" else True
    return jsonify({"image": b64_jpg(frame, max_side=1600),
                    # the operator reads the stamp off this one — keep it sharp
                    "crop": b64_jpg(imaging.head_view(frame, center=center),
                                    max_side=640),
                    "head": b64_jpg(imaging.crop_head(frame, center=center), max_side=320),
                    "sharpness": sharp, "blurry": sharp < floor,
                    "fed": fed, "source": source, "center": list(center),
                    "case_present": present,
                    "prediction": _collect_predict(frame, center)
                                  if predict and present else None})


@app.get("/api/imaging/preview")
def api_imaging_preview():
    """Donut-crop preview for candidate settings (nothing is saved).
    Uses a live camera frame when available, else a synthetic case."""
    try:
        frac = float(request.args.get("frac", 0.42))
        rim = float(request.args.get("rim", imaging.RIM_SCALE))
    except ValueError:
        return jsonify({"error": "bad parameters"}), 400
    mode = request.args.get("mode", "normal")
    if mode not in ("normal", "primer_only"):
        mode = "normal"
    frame = read_frame()
    source = "camera"
    center = None
    if frame is None:
        cfg = load_cfg()
        spec = synth.CaseSpec((cfg.stamp_labels[0][:3] if cfg.stamp_labels else "ABC"),
                              crimped=False)
        frame = synth.render(spec, "A", seed=42)
        source = "synthetic"
    else:
        center = smoothed_head(frame)
    head = imaging.crop_head(frame, pocket_frac=frac, center=center,
                             crop_mode=mode, rim_scale=rim)
    indicator = imaging.mask_indicator(frame, pocket_frac=frac, crop_mode=mode,
                                       center=center, rim_scale=rim)
    resp = {"head": b64_jpg(head, max_side=280),
            "indicator": b64_jpg(indicator, max_side=280),
            "sharpness": round(imaging.sharpness(frame), 1),
            "source": source}
    return jsonify(resp)


def _archive_stage(name):
    """'stamp_alt_20260714_2143.tflite' -> ('stamp_alt', '20260714_2143');
    'specialist_1_20260716_0031.tflite' -> ('specialist_1', ...).
    A bare split('_')[0] read the twin's archives as stage 'stamp' — which
    made Restore overwrite the DONUT model with the POLAR one. The referee
    index is 1-3 digits so 'specialist_20260716...' (legacy, no index)
    still parses as stage 'specialist'."""
    m = re.match(r"(shadow_embed|stamp_alt|stamp|specialist(?:_\d{1,3})?)"
                 r"_(\d{8}_.+)$", Path(name).stem)
    return (m.group(1), m.group(2)) if m else (None, None)


@app.get("/api/models/archive")
def api_models_archive():
    """Archived models grouped by training session (timestamp): the stamp
    twins archive and restore as a PAIR, so they list as one row."""
    arch = MODELS_DIR / "archive"
    sessions = {}
    if arch.is_dir():
        for p in sorted(arch.glob("*.tflite"), reverse=True):
            stage, ts = _archive_stage(p.name)
            if stage is None:
                continue
            sessions.setdefault(ts, {})[stage] = {
                "file": p.name, "kb": p.stat().st_size // 1024}
    items = [{"when": ts.replace("_", " "), "stages": stages}
             for ts, stages in sorted(sessions.items(), reverse=True)]
    return jsonify({"archive": items[:8]})     # newest 8 sessions


@app.get("/api/models/details")
def api_models_details():
    """Everything knowable about one model file (current or archived):
    the metadata sidecar written at training time (accuracy, epochs,
    dataset size, geometry, ...), the label list, and file facts. Models
    trained before the sidecar existed still get the file facts."""
    name = Path(request.args.get("file", "")).name        # no path tricks
    cand = [MODELS_DIR / name, MODELS_DIR / "archive" / name]
    path = next((c for c in cand if c.exists()), None)
    if path is None or not name.endswith(".tflite"):
        return jsonify({"error": f"no model {name!r}"}), 404
    stem = path.stem
    if re.fullmatch(r"stamp|stamp_alt|shadow_embed|specialist(_\d{1,3})?",
                    stem):
        stage, ts = stem, None
    else:
        stage, ts = _archive_stage(name)
    base = str(path)[:-len(".tflite")]
    labels_p, meta_p = Path(base + "_labels.json"), Path(base + "_meta.json")
    d = {"file": name, "stage": stage,
         "archived_as": (ts or "").replace("_", " ") or None,
         "size_kb": path.stat().st_size // 1024,
         "modified": time.strftime("%Y-%m-%d %H:%M",
                                   time.localtime(path.stat().st_mtime)),
         "labels": json.loads(labels_p.read_text()) if labels_p.exists() else [],
         "meta": json.loads(meta_p.read_text()) if meta_p.exists() else None}
    try:                                # input geometry from the file itself
        from sorter.classifier import _load_interpreter
        inp = _load_interpreter(path).get_input_details()[0]
        d["input_shape"] = [int(x) for x in inp["shape"]]
    except Exception:
        pass
    if stage == "shadow_embed":
        # the embedding decider's real story: its training bench (the
        # sidecar travels with the model) and the
        # gallery it matches against
        side = Path(base + ".json")
        if side.exists():
            try:
                d["bench"] = {k: v for k, v in
                              json.loads(side.read_text()).items()
                              if k != "records"}
            except (OSError, ValueError):
                pass
        gal = path.parent / ("shadow_gallery.npz" if ts is None
                             else f"shadow_gallery_{ts}.npz")
        if gal.exists():
            try:
                with np.load(gal, allow_pickle=False) as z:
                    gm = json.loads(str(z["meta"])) if "meta" in z else {}
                    cls = [str(c) for c in z["g_cls"]]
                    d["gallery"] = {**gm, "classes": len(set(cls)),
                                    "vectors": len(cls)}
                    if not d["labels"]:
                        d["labels"] = sorted(set(cls))
            except (OSError, ValueError, KeyError):
                pass
    return jsonify(d)


@app.post("/api/models/restore")
def api_models_restore():
    import shutil
    body = request.get_json()
    name = Path(body.get("file", "")).name          # no path tricks
    src = MODELS_DIR / "archive" / name
    if not src.exists() or not name.endswith(".tflite"):
        return jsonify({"error": f"no archived model {name!r}"}), 404
    stage, ts = _archive_stage(name)
    if stage is None:
        return jsonify({"error": f"can't infer stage from {name!r}"}), 400

    if stage == "shadow_embed":
        # the student restores as model+gallery pair (+ bench sidecar):
        # embeddings and exemplar vectors come from the same network or
        # match nothing. get_shadow() hot-reloads on mtime, so this takes
        # effect on the next classify — no restart.
        now = time.strftime("%Y%m%d_%H%M%S")
        pair = (("shadow_embed", ".tflite"), ("shadow_gallery", ".npz"),
                ("shadow_embed", ".json"))
        for base, suffix in pair:
            cur = MODELS_DIR / f"{base}{suffix}"
            if cur.exists():
                shutil.copy2(cur,
                             MODELS_DIR / "archive" / f"{base}_{now}{suffix}")
        restored = []
        for base, suffix in pair:
            src_p = MODELS_DIR / "archive" / f"{base}_{ts}{suffix}"
            if src_p.exists():
                shutil.copy2(src_p, MODELS_DIR / f"{base}{suffix}")
                restored.append(base)
        # a generation without a bench sidecar must not inherit the
        # previous one's — stale provenance is worse than none
        if not (MODELS_DIR / "archive" / f"shadow_embed_{ts}.json").exists():
            (MODELS_DIR / "shadow_embed.json").unlink(missing_ok=True)
        return jsonify({"ok": True, "restored": name, "stages": restored})

    # the stamp twins live and die as a PAIR — and their generation's
    # referees ride along too: restoring one geometry alone desyncs the
    # label lists and silently disables the live ensemble, and a restored
    # pair judged by another generation's referees is almost as bad
    stages = [stage]
    if stage in ("stamp", "stamp_alt"):
        other = "stamp_alt" if stage == "stamp" else "stamp"
        if (MODELS_DIR / "archive" / f"{other}_{ts}.tflite").exists():
            stages.append(other)
        arch_dir = MODELS_DIR / "archive"
        stages += sorted(
            p.stem[:-len(ts) - 1] for p in arch_dir.glob(f"specialist*_{ts}.tflite"))

    now = time.strftime("%Y%m%d_%H%M%S")
    restored = []
    if stage in ("stamp", "stamp_alt"):
        # archive + drop the CURRENT referees wholesale: the restored
        # generation may have fewer (or none), and a leftover would keep
        # arbitrating against a pair it wasn't trained with
        import shutil as _sh
        for p in sorted(MODELS_DIR.glob("specialist*.tflite")):
            _sh.copy2(p, MODELS_DIR / "archive" / f"{p.stem}_{now}.tflite")
            for kind in ("_labels.json", "_meta.json"):
                side = MODELS_DIR / f"{p.stem}{kind}"
                if side.exists():
                    _sh.copy2(side, MODELS_DIR / "archive" / f"{p.stem}_{now}{kind}")
        for p in MODELS_DIR.glob("specialist*"):
            p.unlink()
    for st in stages:
        src_st = MODELS_DIR / "archive" / f"{st}_{ts}.tflite"
        cur = MODELS_DIR / f"{st}.tflite"
        cur_sidecar = MODELS_DIR / f"{st}_labels.json"
        # archive the current model first so a restore is itself reversible
        if cur.exists():
            shutil.copy2(cur, MODELS_DIR / "archive" / f"{st}_{now}.tflite")
            for kind in ("_labels.json", "_meta.json"):
                side = MODELS_DIR / f"{st}{kind}"
                if side.exists():
                    shutil.copy2(side,
                                 MODELS_DIR / "archive" / f"{st}_{now}{kind}")
        shutil.copy2(src_st, cur)
        for kind in ("_labels.json", "_meta.json"):
            side = src_st.with_name(src_st.stem + kind)
            if side.exists():
                shutil.copy2(side, MODELS_DIR / f"{st}{kind}")
        restored.append(st)
    return jsonify({"ok": True, "restored": name, "stages": restored})


@app.get("/api/export")
def api_export():
    """Zip download: what=deploy (config+models, for the Pi) or what=all
    (adds the full real dataset — your backup)."""
    import io
    import zipfile
    what = request.args.get("what", "deploy")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(CONFIG_PATH, "config.json")
        if MODELS_DIR.is_dir():
            for p in MODELS_DIR.iterdir():   # current models only, not archive/
                if p.is_file():
                    z.write(p, f"models/{p.name}")
        if what == "all" and DATA_DIR.is_dir():
            for p in DATA_DIR.rglob("*.png"):
                z.write(p, f"data/real/{p.relative_to(DATA_DIR).as_posix()}")
    buf.seek(0)
    name = f"sortiq_{what}_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(buf, download_name=name, as_attachment=True,
                     mimetype="application/zip")


# which crop files are in the live gallery, keyed by class — cached on
# the gallery file's mtime so browsing the Dataset page costs nothing
_gal_ex = {"mtime": None, "byclass": {}}


def _gallery_exemplars():
    g = MODELS_DIR / "shadow_gallery.npz"
    if not g.exists():
        return {}
    mt = g.stat().st_mtime
    if _gal_ex["mtime"] != mt:
        byc = {}
        try:
            with np.load(g, allow_pickle=False) as z:
                if "g_path" in z.files:
                    for p in z["g_path"]:
                        c, _, name = str(p).partition("/")
                        byc.setdefault(c, set()).add(name)
        except Exception:
            pass
        _gal_ex.update(mtime=mt, byclass=byc)
    return _gal_ex["byclass"]


@app.get("/api/dataset/images")
def api_dataset_images():
    label = request.args.get("label", "")
    # optional read-only peek into ANOTHER profile's dataset (same
    # cartridge) — e.g. comparing camera generations across profiles
    prof = request.args.get("profile", "")
    base = DATA_DIR
    if prof:
        if any(c in prof for c in "/\\") or ".." in prof:
            return jsonify({"error": "bad profile"}), 400
        cart = json.loads(CONFIG_PATH.read_text())["active"]["cartridge"]
        base = ROOT / "calibers" / cart / prof / "data"
        if not base.is_dir():
            return jsonify({"error": f"no profile {prof!r}"}), 404
    d = base / "raw" / label
    if any(c in label for c in "/\\") or ".." in label or not d.is_dir():
        return jsonify({"error": f"unknown label {label!r}"}), 404
    try:
        offset = max(int(request.args.get("offset", 0)), 0)
        limit = min(max(int(request.args.get("limit", 23)), 1), 100)
    except ValueError:
        return jsonify({"error": "bad offset/limit"}), 400
    stamp_cls = parse_label(label)[0]
    ex_names = _gallery_exemplars().get(stamp_cls, set())
    with _config_lock:
        _raw = active_model_raw()
        pin_names = set((_raw.get("gallery_pinned") or {})
                        .get(stamp_cls, []))
        excl_names = set((_raw.get("gallery_excluded") or {})
                         .get(stamp_cls, []))

    def crop_name(a_path):
        return f"{label}_{a_path.name.split('_')[0]}_A.png"
    all_a = sorted(d.glob("*_A.png"))
    idx_arg = request.args.get("indices")
    if idx_arg:                        # duplicate-scan group cards: exact set
        want = {f"{int(i):04d}" for i in idx_arg.split(",")
                if i.strip().isdigit()}
        all_a = [p for p in all_a if p.name.split("_")[0] in want]
    if request.args.get("exemplars"):
        # the Exemplars card: just the images doing the matching
        # (auto-picked + pinned), all of them, no pagination games
        all_a = [p for p in all_a
                 if crop_name(p) in ex_names or crop_name(p) in pin_names]
    if request.args.get("list"):
        # navigation support (the detail modal's prev/next): the whole
        # class's ordered index list, no thumbnails, effectively free
        return jsonify({"label": label,
                        "indices": [int(p.name.split("_")[0])
                                    for p in all_a]})
    items = []
    # gallery shows the training CROP (what the model sees), not the raw
    # frame; raw is the fallback when the crop hasn't been rebuilt yet
    stamp_dir = base / "stamp" / stamp_cls
    for a_path in all_a[offset:offset + limit]:
        idx = a_path.name.split("_")[0]
        cname = f"{label}_{idx}_A.png"
        crop = stamp_dir / cname
        img = cv2.imread(str(crop)) if crop.is_file() else None
        if img is None:
            img = cv2.imread(str(a_path))
        if img is None:
            continue
        items.append({"index": int(idx),
                      "exemplar": cname in ex_names,
                      "pinned": cname in pin_names,
                      "excluded": cname in excl_names,
                      "px": list(img.shape[:2]),
                      "thumb": b64_jpg(img, max_side=150)})
    return jsonify({"label": label, "total": len(all_a),
                    "offset": offset, "images": items,
                    "gallery_n": len(ex_names)})


_tray_cache = {}          # name -> (mtime, model_md5, vec, thumb)
_tray_lock = threading.Lock()


@app.get("/api/tray")
def api_tray():
    """The set-aside pool ("identify later"), self-clustered by
    embedding similarity so five copies of one mystery stamp arrive as
    a single nameable group. Unknown is a STATE, not a category — this
    tray exists to be emptied."""
    tray = DATA_DIR / "unknown"
    files = sorted(tray.glob("*_A.png")) if tray.is_dir() else []
    sh = get_shadow()
    md5 = sh.model_md5 if sh is not None else None
    # the tray is append-only between visits: embed + thumbnail each
    # image once (keyed by mtime + model) instead of re-doing the whole
    # pool on every view — 200 set-aside cases was ~1 min per open
    items, live = [], set()
    for f in files:
        name = f.name
        try:
            mt = f.stat().st_mtime
        except OSError:
            continue
        live.add(name)
        with _tray_lock:
            hit = _tray_cache.get(name)
        if hit and hit[0] == mt and hit[1] == md5:
            items.append({"n": int(name.split("_")[0]),
                          "vec": hit[2], "thumb": hit[3]})
            continue
        img = cv2.imread(str(f))
        if img is None:
            continue
        vec = sh.embed(imaging.crop_head(img)) if sh is not None else None
        thumb = b64_jpg(imaging.head_view(img), max_side=200)
        with _tray_lock:
            _tray_cache[name] = (mt, md5, vec, thumb)
        items.append({"n": int(name.split("_")[0]),
                      "vec": vec, "thumb": thumb})
    with _tray_lock:
        for k in [k for k in _tray_cache if k not in live]:
            del _tray_cache[k]
    groups = []
    if items and sh is not None:
        vecs = [it["vec"] for it in items]
        used = [False] * len(items)
        for i in range(len(items)):
            if used[i]:
                continue
            members = [i]
            used[i] = True
            for j in range(i + 1, len(items)):
                if not used[j] and float(np.dot(vecs[i], vecs[j])) >= 0.90:
                    members.append(j)
                    used[j] = True
            groups.append(members)
    elif items:
        groups = [[i] for i in range(len(items))]
    out = []
    for g in sorted(groups, key=len, reverse=True):
        out.append({"cases": [
            {"n": items[i]["n"], "thumb": items[i]["thumb"]} for i in g]})
    return jsonify({"total": len(items), "groups": out})


@app.post("/api/tray/resolve")
def api_tray_resolve():
    """Graduate tray photos into a class (create allowed), or discard."""
    body = request.get_json() or {}
    ns = [int(n) for n in (body.get("ns") or [])]
    tray = DATA_DIR / "unknown"
    if body.get("action") == "discard":
        gone = 0
        for n in ns:
            p = tray / f"{n:04d}_A.png"
            if p.exists():
                p.unlink()
                gone += 1
        return jsonify({"ok": True, "discarded": gone})
    stamp = (body.get("stamp") or "").strip().upper()
    if not stamp or stamp == "UNKNOWN":
        return jsonify({"error": "a real headstamp name is required"}), 400
    if body.get("create"):
        if not _valid_stamp(stamp):
            return jsonify({"error": f"invalid headstamp name {stamp!r}"}), 400
        with _config_lock:
            raw = active_model_raw()
            if stamp not in raw["stamp_labels"]:
                raw["stamp_labels"].append(stamp)
                write_active_model(raw)
        load_cfg()
    saved = 0
    for n in ns:
        p = tray / f"{n:04d}_A.png"
        if p.exists():
            frame = cv2.imread(str(p))
            if frame is not None:
                save_labeled(stamp, frame)
                saved += 1
            p.unlink()
    return jsonify({"ok": True, "stamp": stamp, "saved": saved})


@app.post("/api/gallery/pin")
def api_gallery_pin():
    """Pin/unpin a crop as a forced gallery exemplar (stored per class in
    the profile). Takes effect at the next gallery rebuild."""
    body = request.get_json() or {}
    label = (body.get("label") or "").strip()
    idx = body.get("index")
    # state: "pinned" (always an exemplar), "excluded" (never one),
    # "none" (k-center decides). Legacy pinned:bool still accepted.
    state = body.get("state")
    if state is None:
        state = "pinned" if body.get("pinned") else "none"
    if state not in ("pinned", "excluded", "none"):
        return jsonify({"error": "bad state"}), 400
    if any(c in label for c in "/\\") or ".." in label or idx is None:
        return jsonify({"error": "label and index required"}), 400
    stamp_cls = parse_label(label)[0]
    cname = f"{label}_{int(idx):04d}_A.png"
    with _config_lock:
        raw = active_model_raw()
        for key, want in (("gallery_pinned", "pinned"),
                          ("gallery_excluded", "excluded")):
            m = raw.setdefault(key, {})
            cur = set(m.get(stamp_cls, []))
            (cur.add if state == want else cur.discard)(cname)
            if cur:
                m[stamp_cls] = sorted(cur)
            else:
                m.pop(stamp_cls, None)
        write_active_model(raw)
    return jsonify({"ok": True, "state": state, "class": stamp_cls})


_gal_build = {"running": False, "error": None, "done": None, "log": ""}
# batch-review clustering in flight (api_run_groups embedding fresh
# unknowns) — a run must not start under it, and it must not start
# under a run: same interpreter, and on a Pi the same CPU budget
_groups_busy = {"n": 0}


@app.post("/api/gallery/rebuild")
def api_gallery_rebuild():
    """Rebuild the exemplar gallery from the local dataset, in place.
    Runs tools/build_gallery.py (tflite-only — works on the Pi too, where
    the live gallery and canonical dataset already are: no pull/push)."""
    if _gal_build["running"]:
        return jsonify({"error": "rebuild already running"}), 409
    # the finished rebuild swaps shadow_gallery.npz, which hot-reloads
    # into whatever is using it — never under a live run, and not under
    # a scan mid-read either
    if run_mgr.status().get("running"):
        return jsonify({"error": "a run is active — the rebuilt gallery "
                        "would swap in mid-run; rebuild when it ends"}), 409
    if _scan_status["running"] or _dup_status["running"]:
        return jsonify({"error": "a dataset scan is running — let it "
                        "finish, then rebuild"}), 409

    def work():
        try:
            r = subprocess.run(
                (sys.executable, str(ROOT / "tools" / "build_gallery.py")),
                capture_output=True, text=True, timeout=5400, cwd=ROOT)
            tail = (r.stdout or "").strip().splitlines()[-2:]
            _gal_build.update(error=None if r.returncode == 0 else
                              (r.stderr or "build failed").strip()[-400:],
                              log=" · ".join(tail),
                              done=time.strftime("%H:%M:%S"))
        except Exception as e:
            _gal_build.update(error=str(e), done=time.strftime("%H:%M:%S"))
        finally:
            _gal_build["running"] = False

    _gal_build.update(running=True, error=None, done=None, log="")
    threading.Thread(target=work, daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/gallery/rebuild")
def api_gallery_rebuild_status():
    return jsonify(_gal_build)


@app.post("/api/dataset/bulk")
def api_dataset_bulk():
    """Delete or move many pairs in one call. Crops travel or die with
    their raws — no rebuild unless a crop turns out to be missing."""
    body = request.get_json() or {}
    label = body.get("label", "")
    indices = body.get("indices", [])
    action = body.get("action")
    if action not in ("delete", "move") or not indices:
        return jsonify({"error": "need action delete|move and indices"}), 400
    processed = 0
    stale = set()
    try:
        for i in indices:
            if action == "delete":
                if delete_pair(DATA_DIR, label, int(i)):
                    processed += 1
                    _crop_path(label, int(i)).unlink(missing_ok=True)
            else:
                new_idx = move_pair(DATA_DIR, label, int(i), body.get("to", ""))
                processed += 1
                if not _carry_crop(label, int(i), body["to"], new_idx):
                    stale.add(body["to"])
    except ValueError as e:
        return jsonify({"error": str(e), "processed": processed}), 400
    if stale:
        n_stamp, _ = rebuild_crops(DATA_DIR, labels=stale)
        _counts_dirty()
    else:
        _sweep_empty_class_dir(label)
        _counts_dirty()
        n_stamp = _crop_total()
    return jsonify({"ok": True, "processed": processed, "crops": n_stamp})


@app.get("/api/dataset/pair")
def api_dataset_pair():
    """Full-size A/B frames of one pair, for the review detail view."""
    label = request.args.get("label", "")
    if any(c in label for c in "/\\") or ".." in label:
        return jsonify({"error": "bad label"}), 400
    try:
        index = int(request.args.get("index", -1))
    except ValueError:
        return jsonify({"error": "bad index"}), 400
    d = DATA_DIR / "raw" / label
    a_path = d / f"{index:04d}_A.png"
    b_path = d / f"{index:04d}_B.png"
    if not a_path.exists() and not b_path.exists():
        return jsonify({"error": "no such pair"}), 404
    resp = {"label": label, "index": index, "a": None, "b": None}
    for key, p in (("a", a_path), ("b", b_path)):
        if p.exists():
            img = cv2.imread(str(p))
            if img is not None:
                resp[key] = b64_jpg(img, max_side=640)
                resp[key + "_sharpness"] = round(imaging.sharpness(img), 1)
                # the crop is what actually trains — show it next to the raw
                if key == "a":
                    resp["head_crop"] = b64_jpg(imaging.crop_head(img), max_side=320)
    return jsonify(resp)


def _crop_path(label, index):
    return (DATA_DIR / "stamp" / parse_label(label)[0]
            / f"{label}_{int(index):04d}_A.png")


def _sweep_empty_class_dir(label):
    try:
        (DATA_DIR / "stamp" / parse_label(label)[0]).rmdir()
    except OSError:
        pass                       # not empty (or locked) — fine


def _carry_crop(label, index, to, new_idx):
    """A relabel doesn't change pixels — move the existing crop instead
    of re-cropping whole classes (that re-decoded every image in both
    and stalled the UI for seconds per move). False if no crop to move."""
    old = _crop_path(label, index)
    if not old.exists():
        return False
    new_dir = DATA_DIR / "stamp" / parse_label(to)[0]
    new_dir.mkdir(parents=True, exist_ok=True)
    old.rename(new_dir / f"{to}_{new_idx:04d}_A.png")
    return True


@app.post("/api/dataset/move")
def api_dataset_move():
    body = request.get_json()
    try:
        new_idx = move_pair(DATA_DIR, body["label"], body["index"], body["to"])
    except (ValueError, KeyError) as e:
        return jsonify({"error": str(e)}), 400
    _counts_dirty()
    if _carry_crop(body["label"], body["index"], body["to"], new_idx):
        _sweep_empty_class_dir(body["label"])
        n_stamp = _crop_total()
    else:                          # crop never built — make just the dest
        n_stamp, _ = rebuild_crops(DATA_DIR, labels={body["to"]})
    return jsonify({"ok": True, "moved_to": f"{body['to']} #{new_idx}",
                    "crops": {"stamp": n_stamp, "crimp": 0}})


@app.post("/api/dataset/delete")
def api_dataset_delete():
    """The Dataset page is the source of truth for classes: deleting a whole
    dataset also removes its headstamp from the class list and its bin."""
    body = request.get_json()
    class_removed = False
    try:
        if body.get("index") is not None:
            removed = delete_pair(DATA_DIR, body["label"], body["index"])
            # the crop dies with its raw — no class-wide rebuild
            _crop_path(body["label"], body["index"]).unlink(missing_ok=True)
            _sweep_empty_class_dir(body["label"])
            _counts_dirty()
            n_stamp = _crop_total()
            return jsonify({"ok": True, "removed": removed,
                            "class_removed": False,
                            "crops": {"stamp": n_stamp}})
        removed = delete_label(DATA_DIR, body["label"])
        _counts_dirty()
        with _config_lock:
            raw = active_model_raw()
            if body["label"] in raw["stamp_labels"] and len(raw["stamp_labels"]) > 1:
                raw["stamp_labels"].remove(body["label"])
                clean_bins(raw)
                write_active_model(raw)
                class_removed = True
        if class_removed:
            load_cfg()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    n_stamp, _ = rebuild_crops(DATA_DIR, labels={body["label"]})
    return jsonify({"ok": True, "removed": removed, "class_removed": class_removed,
                    "crops": {"stamp": n_stamp}})


@app.post("/api/dataset/rename")
def api_dataset_rename():
    """Renaming a dataset renames its class everywhere (list, near-twin
    group, bin slot); renaming onto an existing class merges into it."""
    body = request.get_json()
    src, dst = body.get("from", ""), (body.get("to") or "").strip().upper()
    if not _valid_stamp(dst):
        return jsonify({"error": f"invalid name {dst!r}"}), 400
    try:
        moved = rename_label(DATA_DIR, src, dst)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    renamed = False
    with _config_lock:
        raw = active_model_raw()
        if src in raw["stamp_labels"]:
            if dst in raw["stamp_labels"]:        # merge: src class disappears
                raw["stamp_labels"].remove(src)
                # the surviving class keeps ITS OWN training-enabled state
                raw["train_disabled"] = [s for s in raw.get("train_disabled", [])
                                         if s != src]
            else:                                 # plain rename everywhere
                raw["stamp_labels"] = [dst if s == src else s for s in raw["stamp_labels"]]
                from sorter.config import normalize_bin
                raw["bins"] = [[dst if s == src else s for s in normalize_bin(b)]
                               for b in raw.get("bins", [])]
                raw["train_disabled"] = sorted({dst if s == src else s
                                                for s in raw.get("train_disabled", [])})
            clean_bins(raw)
            write_active_model(raw)
            renamed = True
    if renamed:
        load_cfg()
    n_stamp, _ = rebuild_crops(DATA_DIR, labels={src, dst})
    _counts_dirty()
    return jsonify({"ok": True, "moved": moved, "crops": {"stamp": n_stamp}})


# ------------------------------------------------------- mislabel scan ---
# The embedding decider audits its own training data: a crop whose
# nearest-exemplar read is a DIFFERENT class with a margin the live
# decider would accept is almost always a save that landed in the wrong
# folder (this method has caught real mid-session strays).
# Read-only; results link into the Dataset detail
# dialog where Move/Delete already live.
_scan_status = {"running": False, "cancel": False, "done": 0, "total": 0,
                "flagged": [], "error": None, "finished": None, "label": None}


@app.post("/api/dataset/scan")
def api_dataset_scan():
    if _scan_status["running"]:
        return jsonify({"error": "scan already running"}), 409
    if run_mgr.status().get("running"):
        return jsonify({"error": "a sorting run is active — the scan "
                        "shares the recognizer; wait for the run to "
                        "end"}), 409
    if _gal_build["running"]:
        return jsonify({"error": "the gallery is rebuilding — the scan "
                        "reads it; wait for the rebuild to finish"}), 409
    if get_shadow() is None:
        return jsonify({"error": "no embedding model installed — the scan "
                        "uses it to second-guess the labels"}), 400
    body = request.get_json(silent=True) or {}
    only = (body.get("label") or "").strip()   # one class, or "" for all
    if only:
        if any(c in only for c in "/\\") or ".." in only \
                or not (DATA_DIR / "raw" / only).is_dir():
            return jsonify({"error": f"unknown label {only!r}"}), 404
    _scan_status.update(running=True, cancel=False, done=0, total=0,
                        flagged=[], error=None, finished=None,
                        label=only or None)

    def run():
        try:
            sh = get_shadow()
            known = set(str(c) for c in sh.g_cls)
            work = []       # only classes the gallery can vote on
            raw = DATA_DIR / "raw"
            if raw.is_dir():
                for d in sorted(p for p in raw.iterdir() if p.is_dir()):
                    if only and d.name != only:
                        continue
                    if parse_label(d.name)[0] in known:
                        work += [(d.name, p) for p in sorted(d.glob("*_A.png"))]
            _scan_status["total"] = len(work)
            flagged = []
            for label, p in work:
                if _scan_status["cancel"]:
                    return
                cls = parse_label(label)[0]
                own = f"{cls}/{label}_{p.name.split('_')[0]}_A.png"
                # the gallery bank already holds this crop's vector —
                # reusing it turns a ~110ms embed (plus a full-frame
                # decode) into a dict lookup; a bank miss (new image,
                # model changed, crop regenerated) embeds live as before
                q = sh.bank_vec(own, DATA_DIR / "stamp" / own)
                if q is None:
                    crop_p = DATA_DIR / "stamp" / own
                    if crop_p.is_file():        # crop beats raw: no find_head
                        img = cv2.imread(str(crop_p))
                    else:
                        frame = cv2.imread(str(p))
                        img = (imaging.crop_head(frame)
                               if frame is not None else None)
                    if img is None:
                        _scan_status["done"] += 1
                        continue
                    q = sh.embed(img)
                # leave-one-out: if this image is serving as an exemplar,
                # mask its own gallery seat so it can't vouch for itself
                r = sh.predict_vec(q, exclude_path=own)
                # flag only reads live sorting would ACT on: wrong class,
                # over that class's bar, clear of the runner-up
                if (r["stamp"] != cls and r["accept"]):
                    flagged.append({
                        "label": label, "index": int(p.name.split("_")[0]),
                        "pred": r["stamp"], "conf": round(r["sim"], 3)})
                    flagged.sort(key=lambda f: -f["conf"])
                    _scan_status["flagged"] = flagged
                _scan_status["done"] += 1
            _scan_status["finished"] = time.strftime("%H:%M")
        except Exception as e:
            _scan_status["error"] = f"{type(e).__name__}: {e}"
        finally:
            _scan_status["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/dataset/scan")
def api_dataset_scan_status():
    return jsonify({k: v for k, v in _scan_status.items() if k != "cancel"})


@app.post("/api/dataset/scan/cancel")
def api_dataset_scan_cancel():
    _scan_status["cancel"] = True
    return jsonify({"ok": True})


# ------------------------------------------------------ duplicate scan ---
# The historical complement to the save-time novelty gate: repeats of
# the same PHYSICAL case (early sessions, re-run brass) inflate class
# counts without adding variety. Two stages, both calibrated on real
# data:
#   1. embedding cosine >= tau (0.97) nominates candidate pairs — cheap,
#      but the ArcFace space is TRAINED to make classmates identical, so
#      alone it flagged 60% of the dataset (a smooth 0.97-0.99 continuum
#      with no same-case gap);
#   2. pixel truth: rotation-searched normalized correlation of the gray
#      crops. Distinct cases of one brand top out ~0.93 (different
#      scratch patterns); the same case re-photographed scores 0.95+
#      (the three real duplicates found sat at 0.996-1.000, all
#      consecutive indices — back-to-back double-saves).
# A HUMAN still confirms every group; nothing deletes automatically.
_dup_status = {"running": False, "cancel": False, "done": 0, "total": 0,
               "stage": "embed", "groups": [], "error": None,
               "finished": None, "tau": 0.97, "pix": 0.95}

_DUP_SIZE, _DUP_ROTS = 96, 60


def _dup_gray(path):
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    img = cv2.resize(img, (_DUP_SIZE, _DUP_SIZE)).astype(np.float32)
    img -= img.mean()
    return (img / (np.linalg.norm(img) or 1.0)).ravel()


def _crop_sharp(path):
    """Full-resolution sharpness for one crop — same scale as the
    capture-time floor (never computed on a resized image)."""
    img = cv2.imread(str(path))
    return float(imaging.sharpness(img)) if img is not None else 0.0


@functools.lru_cache(maxsize=1024)               # ~36KB/entry, ~37MB cap
def _dup_gray_cached(path_str, mtime):
    """mtime-keyed _dup_gray: the novelty gate's nominees repeat heavily
    across a bulk file of same-class cases."""
    return _dup_gray(Path(path_str))


def _rotstack_gray(img):
    img = cv2.resize(img, (_DUP_SIZE, _DUP_SIZE)).astype(np.float32)
    c = (_DUP_SIZE / 2 - 0.5, _DUP_SIZE / 2 - 0.5)
    out = np.empty((_DUP_ROTS, _DUP_SIZE * _DUP_SIZE), np.float32)
    for k in range(_DUP_ROTS):
        M = cv2.getRotationMatrix2D(c, k * 360.0 / _DUP_ROTS, 1.0)
        r = cv2.warpAffine(img, M, (_DUP_SIZE, _DUP_SIZE))
        r -= r.mean()
        out[k] = (r / (np.linalg.norm(r) or 1.0)).ravel()
    return out


def _dup_rotstack(path):
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    return None if img is None else _rotstack_gray(img)


# ------------------------------------------------- novelty gate (saves) ---
# Once a class holds NOVELTY_MIN_IMAGES, new saves must add variety.
# The embedding alone cannot judge that: measured on the
# self-trained model, 86% of DISTINCT cases in the big classes read
# >= 0.97 against their own class (S&B 100%, FC 98%) — a cosine gate is
# frozen shut. So cosine only NOMINATES the closest bank images, and the
# duplicate scan's rotation-searched pixel correlation convicts: >= 0.95
# means the same scratch pattern, i.e. the same physical case re-filed.
NOVELTY_MIN_IMAGES = 300
_NOVELTY_EMBED = 0.97          # nominator floor, not a verdict
_NOVELTY_PIX = 0.95            # same-scratches bar (calibrated on real data)
_WELLFED_MIN_IMAGES = 500      # intake bar for batch filing: past this size
                               # a class needs VARIETY, not volume — a
                               # confidently classified case whose nearest
                               # class-bank neighbor reads >= _NOVELTY_EMBED
                               # is the model saying "thoroughly seen this
                               # look" and is skipped as routine (File all
                               # overrides). Only confident class groups are
                               # filtered: below-bar and cluster cases are
                               # the hard examples — always worth filing.
_NOVELTY_TOPK = 32             # nominees pixel-checked per save. Cosine can
                               # nominate but not RANK inside a big class
                               # (classmates crowd 0.97-0.99): measured
                               # live, re-filed cases ranked 6-23
                               # by cosine, so a top-4 window missed every
                               # one; 32 caught each in-bank repeat with zero
                               # false skips across that day's 220 filings
                               # (~0.2s a case, early exit on conviction).


def _same_case_in_bank(sh, stamp, crop):
    """(dup, detail, nn_sim) — is this crop a re-photograph of a case
    already filed under `stamp`? nn_sim is the crop's nearest-neighbor
    cosine against the class bank (None when the bank can't say), which
    the batch intake filter reuses. Inactive (False) until the gallery
    carries bank paths (newer galleries)."""
    if sh.all_vec is None or sh.all_path is None:
        return False, None, None
    # class->rows index, built once per gallery load (the attribute dies
    # with the classifier on hot-reload): a 300-case group confirm was
    # paying a full 8,000-entry scan per case
    by = getattr(sh, "_bank_by_cls", None)
    if by is None:
        by = {}
        for i, c in enumerate(sh.all_cls):
            by.setdefault(c, []).append(i)
        sh._bank_by_cls = by
    idx = by.get(stamp)
    if not idx:
        return False, None, None
    v = sh.embed(crop)
    sims = sh.all_vec[np.array(idx)] @ v
    nn_sim = float(sims.max())
    stack = None
    for o in np.argsort(-sims)[:_NOVELTY_TOPK]:  # top nominees
        sim = float(sims[o])
        if sim < _NOVELTY_EMBED:
            break
        p = DATA_DIR / "stamp" / sh.all_path[idx[int(o)]]
        try:                          # nominees repeat across a bulk file:
            cand = _dup_gray_cached(str(p), p.stat().st_mtime)
        except OSError:
            cand = None
        if cand is None:
            continue                             # bank image since deleted
        if stack is None:
            stack = _rotstack_gray(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY))
        corr = float((stack @ cand).max())
        if corr >= _NOVELTY_PIX:
            return True, {"sim": round(sim, 4), "corr": round(corr, 4),
                          "match": sh.all_path[idx[int(o)]]}, nn_sim
    return False, None, nn_sim


@app.post("/api/dataset/dupscan")
def api_dataset_dupscan():
    if _dup_status["running"] or _scan_status["running"]:
        return jsonify({"error": "a scan is already running"}), 409
    if run_mgr.status().get("running"):
        return jsonify({"error": "a sorting run is active — the scan "
                        "shares the recognizer; wait for the run to "
                        "end"}), 409
    if _gal_build["running"]:
        return jsonify({"error": "the gallery is rebuilding — the scan "
                        "reads it; wait for the rebuild to finish"}), 409
    if get_shadow() is None:
        return jsonify({"error": "no embedding model installed — the scan "
                        "uses it to compare images"}), 400
    body = request.get_json(silent=True) or {}
    try:
        tau = min(max(float(body.get("tau", 0.97)), 0.90), 0.999)
    except (TypeError, ValueError):
        tau = 0.97
    try:
        pix = min(max(float(body.get("pix", 0.95)), 0.85), 0.999)
    except (TypeError, ValueError):
        pix = 0.95
    only = (body.get("label") or "").strip()   # one class, or "" for all
    if only:
        if any(c in only for c in "/\\") or ".." in only \
                or not (DATA_DIR / "raw" / only).is_dir():
            return jsonify({"error": f"unknown label {only!r}"}), 404
    _dup_status.update(running=True, cancel=False, done=0, total=0,
                       stage="embed", groups=[], error=None, finished=None,
                       tau=tau, pix=pix, label=only or None)

    def run():
        try:
            sh = get_shadow()
            work = []                              # (label, idx, crop_path)
            raw = DATA_DIR / "raw"
            if raw.is_dir():
                for d in sorted(p for p in raw.iterdir() if p.is_dir()):
                    if only and d.name != only:
                        continue
                    cls = parse_label(d.name)[0]
                    for p in sorted(d.glob("*_A.png")):
                        idx = p.name.split("_")[0]
                        rel = f"{cls}/{d.name}_{idx}_A.png"
                        crop = DATA_DIR / "stamp" / rel
                        work.append((d.name, int(idx),
                                     crop if crop.is_file() else p,
                                     rel if crop.is_file() else None))
            _dup_status["total"] = len(work)
            # sharpness is only consumed for images that land in a
            # VERIFIED group (a handful), so it's computed there — with
            # banked vectors, stage 1 needs no image decode at all
            by_label = {}                          # label -> [(idx, emb, None, path)]
            for label, idx, path, rel in work:
                if _dup_status["cancel"]:
                    return
                emb = sh.bank_vec(rel, path) if rel else None
                if emb is None:
                    img = cv2.imread(str(path))
                    emb = sh.embed(img) if img is not None else None
                if emb is not None:
                    by_label.setdefault(label, []).append(
                        (idx, emb, None, path))
                _dup_status["done"] += 1

            # stage 2: pixel-verify every embedding-nominated pair
            cand_pairs = {}                        # label -> [(i, j)]
            for label, rows in by_label.items():
                if len(rows) < 2:
                    continue
                vec = np.stack([r[1] for r in rows])
                sims = vec @ vec.T
                ii, jj = np.nonzero(np.triu(sims >= tau, k=1))
                if len(ii):
                    cand_pairs[label] = list(zip(ii.tolist(), jj.tolist()))
            _dup_status.update(stage="verify", done=0,
                               total=sum(len(v) for v in cand_pairs.values()))

            # pairs the human already ruled DIFFERENT cases ("keep all"):
            # persisted in the profile so they never re-flag
            with _config_lock:
                dd_all = active_model_raw().get("dup_distinct") or {}
            groups = []
            for label, pairs in cand_pairs.items():
                rows = by_label[label]
                ruled = {tuple(p) for p in dd_all.get(label, [])}
                involved = sorted({i for p in pairs for i in p})
                plain = {i: _dup_gray(rows[i][3]) for i in involved}
                by_i = {}
                for i, j in pairs:
                    by_i.setdefault(i, []).append(j)
                verified = []                      # (i, j, corr)
                for i, js in by_i.items():
                    if _dup_status["cancel"]:
                        return
                    stack = _dup_rotstack(rows[i][3])
                    for j in js:
                        if tuple(sorted((rows[i][0], rows[j][0]))) in ruled:
                            _dup_status["done"] += 1
                            continue               # human overrules the pixels
                        if stack is not None and plain.get(j) is not None:
                            corr = float((stack @ plain[j]).max())
                            if corr >= pix:
                                verified.append((i, j, corr))
                        _dup_status["done"] += 1
                if not verified:
                    continue
                # union-find over VERIFIED links only — pixel-level
                # same-case identity is genuinely transitive
                parent = {}

                def find(x):
                    parent.setdefault(x, x)
                    while parent[x] != x:
                        parent[x] = parent[parent[x]]
                        x = parent[x]
                    return x

                link_corr = {}
                for i, j, corr in verified:
                    parent[find(i)] = find(j)
                    link_corr.setdefault(i, []).append(corr)
                    link_corr.setdefault(j, []).append(corr)
                comp = {}
                for x in list(parent):
                    comp.setdefault(find(x), []).append(x)
                for members in comp.values():
                    if len(members) < 2:
                        continue
                    sharp = {i: _crop_sharp(rows[i][3]) for i in members}
                    ms = sorted(members, key=lambda i: -sharp[i])
                    tight = min(c for i in members
                                for c in link_corr.get(i, [1.0]))
                    groups.append({
                        "label": label,
                        "keep": rows[ms[0]][0],       # sharpest member
                        "members": [{"index": rows[i][0],
                                     "sharp": round(float(sharp[i]), 1)}
                                    for i in ms],
                        "sim": round(tight, 3)})
            groups.sort(key=lambda g: (-len(g["members"]), g["label"]))
            _dup_status["groups"] = groups
            _dup_status["finished"] = time.strftime("%H:%M")
        except Exception as e:
            _dup_status["error"] = f"{type(e).__name__}: {e}"
        finally:
            _dup_status["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/dataset/dupscan")
def api_dataset_dupscan_status():
    out = {k: v for k, v in _dup_status.items() if k != "cancel"}
    # the result is a snapshot from scan time — re-check against disk so
    # groups the user already thinned (here or in the viewer) drop out
    # instead of re-listing as ghosts after a reload
    if not out["running"] and out["groups"]:
        raw = DATA_DIR / "raw"
        live = []
        for g in out["groups"]:
            members = [m for m in g["members"]
                       if (raw / g["label"]
                           / f"{m['index']:04d}_A.png").exists()]
            if len(members) >= 2:
                live.append({**g, "members": members,
                             "keep": g["keep"]
                             if any(m["index"] == g["keep"] for m in members)
                             else members[0]["index"]})   # sharpest left
        out["groups"] = live
    return jsonify(out)


@app.post("/api/dataset/dupscan/cancel")
def api_dataset_dupscan_cancel():
    _dup_status["cancel"] = True
    return jsonify({"ok": True})


@app.post("/api/dataset/dupscan/ignore")
def api_dataset_dupscan_ignore():
    """"Different cases — keep all": the human overrules the pixel test
    for this group. Persisted to the profile (model.json dup_distinct,
    as index pairs) so neither a page refresh nor any future scan
    re-flags brass that's already been adjudicated."""
    body = request.get_json() or {}
    label = (body.get("label") or "").strip()
    try:
        ids = sorted({int(i) for i in (body.get("indices") or [])})
    except (TypeError, ValueError):
        ids = []
    if not label or len(ids) < 2:
        return jsonify({"error": "label and 2+ indices required"}), 400
    pairs = [(a, b) for k, a in enumerate(ids) for b in ids[k + 1:]]
    with _config_lock:
        raw = active_model_raw()
        dd = raw.setdefault("dup_distinct", {})
        cur = {tuple(p) for p in dd.get(label, [])}
        cur.update(pairs)
        dd[label] = sorted(list(p) for p in cur)
        write_active_model(raw)
    # drop the group from the live results too, so a reload stays clean
    gs = _dup_status.get("groups") or []
    _dup_status["groups"] = [
        g for g in gs
        if not (g["label"] == label
                and {m["index"] for m in g["members"]} <= set(ids))]
    return jsonify({"ok": True, "pairs": len(pairs)})


@app.post("/api/dataset/rebuild")
def api_dataset_rebuild():
    """Manual crop rebuild from raw/ — the escape hatch for datasets that
    changed outside the app (rsync from another machine, sync-tool lock
    leftovers). Idempotent: raw is the source of truth. Synchronous; the
    UI shows a busy state (~20-30s for a 1,400-image dataset on the Pi)."""
    # a full reshape is a CPU storm on a Pi and rewrites the crops the
    # scans pixel-verify against — not under a run, not under a scan
    if run_mgr.status().get("running"):
        return jsonify({"error": "a run is active — rebuild crops when "
                        "it ends"}), 409
    if _scan_status["running"] or _dup_status["running"]:
        return jsonify({"error": "a dataset scan is running — let it "
                        "finish, then rebuild"}), 409
    # the escape hatch stays an escape hatch: outside changes (rsync
    # with preserved mtimes) can defeat the freshness check, so the
    # manual button does the full reshape unless asked not to
    n_stamp, _ = rebuild_crops(
        DATA_DIR, incremental=bool((request.get_json(silent=True)
                                    or {}).get("incremental")))
    _counts_dirty()
    return jsonify({"ok": True, "crops": {"stamp": n_stamp}})


# ---------------------------------------- remote training (machine side) ---
# The Pi can't train (inference-only runtime). A trainer PC mirrors the
# dataset over these endpoints, trains the embedding pipeline, and pushes
# the model+gallery pair back — trainer-initiated, because the machine is
# the always-on node with the stable address.
@app.get("/api/dataset/manifest")
def api_dataset_manifest():
    """Inventory for the trainer's incremental pull: the active model's
    raw/ images (size+mtime) plus model.json (labels + imaging config).
    Refused while a run is live: thousands of SD-card reads under a
    sorting loop stutter the machine, and the modal that "prevents"
    this is only client-side — a page refresh walks straight past it.
    The trainer surfaces this message in its sync status."""
    if run_mgr.status().get("running"):
        return jsonify({"error": "the machine is running — pull the "
                        "dataset when the run ends"}), 409
    files = []
    raw_dir = DATA_DIR / "raw"
    if raw_dir.is_dir():
        for p in sorted(raw_dir.rglob("*.png")):
            st = p.stat()
            files.append({"path": p.relative_to(DATA_DIR).as_posix(),
                          "size": st.st_size, "mtime": int(st.st_mtime)})
    a = json.loads(CONFIG_PATH.read_text())["active"]
    return jsonify({"cartridge": a["cartridge"], "model": a["model"],
                    "model_json": active_model_raw(), "files": files})


@app.get("/api/dataset/file")
def api_dataset_file():
    rel = request.args.get("path", "")
    p = (DATA_DIR / rel).resolve()
    if not str(p).startswith(str(DATA_DIR.resolve()) + os.sep) or not p.is_file():
        return jsonify({"error": f"no such dataset file {rel!r}"}), 404
    return send_file(p)


@app.post("/api/dataset/files")
def api_dataset_files():
    """Batch download: an uncompressed tar of the requested paths. A first
    pull is ~1,400 images; per-request overhead on individual GETs measured
    ~3s/file over the Pi's Wi-Fi — batching collapses it. Capped per call
    so the in-memory tar stays small on the 2GB Pi; the trainer chunks."""
    import io
    import tarfile
    # a pull that was mid-flight when a run started stops at the next
    # chunk — same reason the manifest refuses
    if run_mgr.status().get("running"):
        return jsonify({"error": "the machine is running — pull the "
                        "dataset when the run ends"}), 409
    paths = ((request.get_json() or {}).get("paths") or [])[:50]
    buf = io.BytesIO()
    root = str(DATA_DIR.resolve()) + os.sep
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel in paths:
            p = (DATA_DIR / rel).resolve()
            if str(p).startswith(root) and p.is_file():
                tar.add(p, arcname=rel)
    buf.seek(0)
    return send_file(buf, mimetype="application/x-tar",
                     download_name="dataset.tar")


# ------------------------------------------------------------- code sync ---
# The machine gets fresh code on every pi_deploy; a trainer PC deliberately
# holds no git credentials, so it updates FROM the machine instead — same
# manifest-and-pull pattern as the dataset, opposite direction (see
# sorter/codesync.py for why drift actually corrupts models). This is the
# serving side; the pulling side lives with the trainer endpoints below.
@app.get("/api/code/manifest")
def api_code_manifest():
    """This install's code inventory. ?digest=1 returns just the rolled-up
    digest — the "version" the UIs compare — without the file list."""
    if request.args.get("digest"):
        try:
            ver = (ROOT / "VERSION").read_text().strip()
        except OSError:
            ver = "dev"
        return jsonify({"digest": codesync.digest(ROOT), "version": ver,
                        "is_git": codesync.is_git_checkout(ROOT)})
    man = codesync.manifest(ROOT)
    man["is_git"] = codesync.is_git_checkout(ROOT)
    return jsonify(man)


@app.post("/api/code/files")
def api_code_files():
    """Batch download, same shape as /api/dataset/files: an uncompressed tar
    of the requested paths. The manifest is the whitelist — nothing outside
    it (config.json, calibers/, logs) can ever be served."""
    import io
    import tarfile
    paths = ((request.get_json() or {}).get("paths") or [])[:50]
    ok = {f["path"] for f in codesync.manifest(ROOT)["files"]}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel in paths:
            if rel in ok:
                tar.add(ROOT / rel, arcname=rel)
    buf.seek(0)
    return send_file(buf, mimetype="application/x-tar",
                     download_name="code.tar")


@app.post("/api/models/install")
def api_models_install():
    """Receive a freshly trained generation from the trainer: the
    embedding pair (shadow_embed.tflite + shadow_gallery.npz), archived
    as one generation, hot-reloaded by get_shadow's mtime watcher.
    (Legacy twins installs are retired — a trainer old enough to push
    stamp*.tflite can't exist behind the code-sync digest gate.)"""
    import shutil as _sh
    # a model landing mid-run hot-reloads into the sorting loop — the
    # brain (and every class's bar) would swap between two cases. The
    # trainer's Install surfaces this message; retry when the run ends.
    if run_mgr.status().get("running"):
        return jsonify({"error": "the machine is running — installing "
                        "would swap the model mid-run; install when "
                        "it ends"}), 409
    got = {k: f for k, f in request.files.items()
           if k in ("shadow_embed.tflite", "shadow_gallery.npz",
                    "shadow_embed.json")}
    if not got:
        return jsonify({"error": "need shadow_embed.tflite and/or "
                                 "shadow_gallery.npz"}), 400
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    arch_dir = MODELS_DIR / "archive"
    arch_dir.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    # archive the outgoing generation (model + gallery + bench sidecar)
    # under one timestamp so the Train page can restore it as a unit
    archived = False
    cur_m = MODELS_DIR / "shadow_embed.tflite"
    if cur_m.exists() and "shadow_embed.tflite" in got:
        _sh.copy2(cur_m, arch_dir / f"shadow_embed_{ts}.tflite")
        for name, arch_name in (("shadow_gallery.npz",
                                 f"shadow_gallery_{ts}.npz"),
                                ("shadow_embed.json",
                                 f"shadow_embed_{ts}.json")):
            cur = MODELS_DIR / name
            if cur.exists():
                _sh.copy2(cur, arch_dir / arch_name)
        archived = True
    for name, f in got.items():
        f.save(MODELS_DIR / name)
    # a new model without a bench sidecar must not keep the old one's
    if "shadow_embed.tflite" in got and "shadow_embed.json" not in got:
        (MODELS_DIR / "shadow_embed.json").unlink(missing_ok=True)
    return jsonify({"ok": True, "installed": sorted(got),
                    "archived_as": ts if archived else None})


@app.post("/api/shadow/push")
def api_shadow_push():
    """Trainer-side: install the embedding decider (model + gallery)
    onto the machine (default) or this install ("target": "local").
    This IS the sorting brain now — DELETE removes the pair, which
    leaves the machine unable to start runs until one is reinstalled."""
    import urllib.request
    body = request.get_json(silent=True) or {}
    model = MODELS_DIR / str(body.get("model", "shadow_embed.tflite"))
    gal = MODELS_DIR / str(body.get("gallery", "shadow_gallery.npz"))
    if not model.is_file() or not gal.is_file():
        return jsonify({"error": f"missing {model.name} or {gal.name} in the "
                                 "profile models dir — train with "
                                 "tools/distill_student.py, then "
                                 "tools/build_gallery.py"}), 400
    files = {"shadow_embed.tflite": model.read_bytes(),
             "shadow_gallery.npz": gal.read_bytes()}
    if body.get("target") == "local":
        if run_mgr.status().get("running"):
            return jsonify({"error": "a run is active — installing would "
                            "swap the model mid-run"}), 409
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (MODELS_DIR / name).write_bytes(data)
        return jsonify({"ok": True, "installed": sorted(files), "target": "local"})
    url = _machine_url()
    if not url:
        return jsonify({"error": "set the sorting machine URL first"}), 400
    data, ctype = _multipart(files)
    req = urllib.request.Request(url + "/api/models/install", data=data,
                                 method="POST", headers={"Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=120) as r:
        reply = json.loads(r.read())
    return jsonify({"ok": True, "machine": url, "reply": reply})


@app.delete("/api/shadow/push")
def api_shadow_remove():
    """Remove the shadow files (machine by default, "target": "local")
    — the off switch."""
    import urllib.request
    body = request.get_json(silent=True) or {}
    if body.get("target") == "local":
        if run_mgr.status().get("running"):
            return jsonify({"error": "a run is active — removing the "
                            "model would blind it mid-run"}), 409
        removed = []
        for n in ("shadow_embed.tflite", "shadow_gallery.npz"):
            p = MODELS_DIR / n
            if p.exists():
                p.unlink()
                removed.append(n)
        return jsonify({"ok": True, "removed": removed, "target": "local"})
    url = _machine_url()
    if not url:
        return jsonify({"error": "set the sorting machine URL first"}), 400
    req = urllib.request.Request(url + "/api/shadow/push",
                                 data=json.dumps({"target": "local"}).encode(),
                                 method="DELETE",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return jsonify(json.loads(r.read()))


# ------------------------------------------------ community starter model ---
# A trained recognizer + gallery published as a release asset, so a fresh
# install can SORT before its owner has collected a single image. Pinned
# by URL and SHA-256: the download must hash to exactly the published
# digest or nothing is installed. Re-pinned each time a new starter ships.
STARTER_URL = ("https://github.com/yamanub/SortIQ/releases/download/"
               "v1.4/starter_9mm.tar.gz")
# UNPINNED for the v1.4 release (owner's call — held for later). The v5
# pair (99.9% closed-set, 86 classes, packaged 2026-08-18) lives at
# Documents\SortIQ-release-assets\starter_9mm.tar.gz with digest
# e40d5abec51e4649c44790cf177d8f4ae70617deb33ec8540a25bf53dee9ba9c —
# re-pin that digest here and attach the asset to a release to publish.
# With no pin the Train tab hides the starter card entirely.
STARTER_SHA256 = ""


@app.get("/api/models/starter")
def api_models_starter_info():
    return jsonify({"available": bool(STARTER_SHA256), "url": STARTER_URL,
                    "installed": get_shadow() is not None})


@app.post("/api/models/starter")
def api_models_starter():
    """Download the pinned starter asset, verify its digest, and install
    the pair into the active profile — the same landing spot a trainer
    push uses, so get_shadow() hot-reloads it the same way. "url"/"sha256"
    in the body override the pin (the docs' drop-a-file path, and how the
    asset is verified against a local server before it's ever published);
    an override URL still gets its digest echoed back so it can be pinned."""
    import hashlib
    import io
    import shutil as _sh
    import tarfile
    import urllib.request
    if run_mgr.status().get("running"):
        return jsonify({"error": "a run is active — install after it ends"}), 409
    body = request.get_json(silent=True) or {}
    url = body.get("url") or STARTER_URL
    sha = body.get("sha256") if "url" in body else STARTER_SHA256
    if "url" not in body and not STARTER_SHA256:
        return jsonify({"error": "no starter is pinned for this build"}), 400
    try:
        with urllib.request.urlopen(url, timeout=300) as r:
            blob = r.read()
    except Exception as e:
        return jsonify({"error": f"download failed: {e}"}), 502
    got_sha = hashlib.sha256(blob).hexdigest()
    if sha and got_sha != sha:
        return jsonify({"error": "digest mismatch — the download is not the "
                                 f"published starter (got {got_sha[:16]}…)"}), 400
    want = {"shadow_embed.tflite", "shadow_gallery.npz"}
    files = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tar:
            for m in tar.getmembers():
                base = Path(m.name).name
                if m.isfile() and base in want:
                    files[base] = tar.extractfile(m).read()
    except tarfile.TarError as e:
        return jsonify({"error": f"not a readable tar archive: {e}"}), 400
    if set(files) != want:
        return jsonify({"error": "asset must carry shadow_embed.tflite "
                                 "and shadow_gallery.npz"}), 400
    # the pair must agree with EACH OTHER: the gallery was built by
    # embedding crops through one exact model, and a digest mismatch
    # means every stored vector is in a different space
    import numpy as np
    try:
        gal = np.load(io.BytesIO(files["shadow_gallery.npz"]),
                      allow_pickle=False)
        meta = json.loads(str(gal["meta"]))
    except Exception as e:
        return jsonify({"error": f"gallery unreadable: {e}"}), 400
    model_digest = hashlib.md5(files["shadow_embed.tflite"]).hexdigest()
    if meta.get("model_digest") != model_digest:
        return jsonify({"error": "gallery/model mismatch inside the asset"}), 400
    # same landing as /api/models/install: archive the outgoing
    # generation first, then write — the mtime watcher does the reload
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    arch_dir = MODELS_DIR / "archive"
    arch_dir.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    archived = False
    if (MODELS_DIR / "shadow_embed.tflite").exists():
        for name, arch in (("shadow_embed.tflite", f"shadow_embed_{ts}.tflite"),
                           ("shadow_gallery.npz", f"shadow_gallery_{ts}.npz"),
                           ("shadow_embed.json", f"shadow_embed_{ts}.json")):
            cur = MODELS_DIR / name
            if cur.exists():
                _sh.copy2(cur, arch_dir / arch)
        archived = True
    for name, data in files.items():
        (MODELS_DIR / name).write_bytes(data)
    # a community model carries no bench sidecar for THIS dataset
    (MODELS_DIR / "shadow_embed.json").unlink(missing_ok=True)
    return jsonify({"ok": True, "classes": meta.get("classes"),
                    "vectors": meta.get("vectors"),
                    "built_at": meta.get("built_at"),
                    "sha256": got_sha,
                    "archived_as": ts if archived else None})


# ------------------------------------------------------------- USB export ---
# "Insert a stick, click the button, walk it to the PC": the alternative to
# the network pull for the big first sync. Exports every profile's raw
# images + model.json in the exact calibers/ layout the trainer expects —
# crops and models are skipped (both regenerate). Incremental by size, so a
# re-export only copies what's new.
_usb_status = {"running": False, "phase": None, "copied": 0, "total": 0,
               "error": None, "result": None, "mounted": False,
               "device": None, "mountpoint": None}


def _usb_partitions():
    """Removable USB partitions with a filesystem, via lsblk (Linux only)."""
    try:
        out = subprocess.run(("lsblk", "-J", "-o",
                              "PATH,TYPE,TRAN,RM,SIZE,FSTYPE,MOUNTPOINT,LABEL"),
                             capture_output=True, text=True, timeout=5).stdout
        devs = json.loads(out).get("blockdevices", [])
    except Exception:
        return []

    # some lsblk builds nest partitions under their disk, others emit a
    # FLAT list where the partition carries tran=null (bench-found on the
    # Pi) — flatten first, then match partitions to a USB parent disk by
    # device-path prefix
    flat = []

    def walk(nodes, tran):
        for n in nodes:
            n = dict(n)
            n["tran"] = n.get("tran") or tran
            flat.append(n)
            walk(n.get("children") or [], n["tran"])

    walk(devs, None)
    usb_disks = {n["path"] for n in flat
                 if n.get("type") == "disk" and n.get("tran") == "usb"}
    parts = []
    for n in flat:
        if n.get("type") != "part" or not n.get("fstype"):
            continue
        if n.get("tran") == "usb" \
                or any(n["path"].startswith(d) for d in usb_disks):
            parts.append({"path": n["path"], "size": n.get("size"),
                          "fstype": n.get("fstype"),
                          "label": n.get("label"),
                          "mountpoint": n.get("mountpoint")})
    return parts


@app.get("/api/usb/status")
def api_usb_status():
    return jsonify({**_usb_status, "sticks": _usb_partitions()})


@app.post("/api/usb/export")
def api_usb_export():
    if _usb_status["running"]:
        return jsonify({"error": "USB export already running"}), 409
    parts = _usb_partitions()
    if not parts:
        return jsonify({"error": "no USB stick detected — insert one and "
                                 "try again"}), 404
    dev = parts[0]
    _usb_status.update(running=True, phase="mount", copied=0, total=0,
                       error=None, result=None, mounted=False,
                       device=None, mountpoint=None)

    def run():
        import shutil
        mounted_here = False
        mp = dev.get("mountpoint")
        try:
            if not mp:
                # plain mount at a fixed path — systemd-mount creates a lazy
                # AUTOMOUNT unit (no mountpoint until first access) and its
                # unit lingers across attempts (bench-found). FAT-family
                # sticks get uid/gid options so the service user can write.
                subprocess.run(("sudo", "systemd-umount", dev["path"]),
                               capture_output=True, timeout=15)  # clear strays
                mp = "/run/sortiq-usb"
                subprocess.run(("sudo", "mkdir", "-p", mp),
                               capture_output=True, timeout=10)
                cmd = ["sudo", "mount"]
                if (dev.get("fstype") or "").lower() in ("vfat", "exfat", "ntfs"):
                    cmd += ["-o", f"uid={os.getuid()},gid={os.getgid()}"]
                cmd += [dev["path"], mp]
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=30)
                if r.returncode != 0:
                    raise RuntimeError(f"mount failed: {(r.stderr or '').strip()}")
                mounted_here = True

            # ACTIVE model only — that's all the trainer's pull ever reads,
            # and it keeps retired profiles from tripling the stick size
            pdir = DATA_DIR.parent
            files = ([pdir / "model.json"]
                     if (pdir / "model.json").exists() else [])
            files += sorted(p for p in (DATA_DIR / "raw").rglob("*")
                            if p.is_file())
            dst_root = Path(mp) / "SortIQ"
            # refuse cleanly BEFORE filling the stick: only what's missing
            # or changed actually needs room (re-exports are incremental)
            need = sum(src.stat().st_size for src in files
                       if not (dst_root / src.relative_to(ROOT)).exists()
                       or (dst_root / src.relative_to(ROOT)).stat().st_size
                       != src.stat().st_size)
            free = shutil.disk_usage(mp).free
            if need > free * 0.97:
                raise RuntimeError(
                    f"stick is too small: needs {need // 2**20} MB free, "
                    f"{dev.get('label') or dev['path']} has "
                    f"{free // 2**20} MB — use a larger stick")
            _usb_status.update(phase="copy", total=len(files))
            for n, src in enumerate(files, 1):
                dst = dst_root / src.relative_to(ROOT)
                if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dst)   # copyfile: FAT keeps no metadata
                _usb_status["copied"] = n
            _usb_status["phase"] = "flush"
            os.sync()                            # everything on the stick before
            _usb_status["result"] = {             # we call it done
                "files": len(files),
                "stick": dev.get("label") or dev["path"],
                "dest": str(dst_root)}
            # the stick STAYS mounted on success — the modal's Close &
            # eject is what unmounts it (POST /api/usb/eject), so the
            # user decides when it comes out
            _usb_status.update(mounted=True, device=dev["path"],
                               mountpoint=mp)
        except Exception as e:
            _usb_status["error"] = str(e)
            if mounted_here:                     # failure: leave nothing behind
                subprocess.run(("sudo", "umount", mp),
                               capture_output=True, timeout=60)
        finally:
            _usb_status["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "stick": dev})


@app.post("/api/usb/eject")
def api_usb_eject():
    """The USB modal's Close & eject: flush, unmount, safe to remove."""
    if _usb_status["running"]:
        return jsonify({"error": "copy in progress — wait for it to finish"}), 409
    target = _usb_status.get("mountpoint")
    if not target:
        # nothing of ours mounted; eject any mounted USB partition anyway
        target = next((p["mountpoint"] for p in _usb_partitions()
                       if p.get("mountpoint")), None)
    if not target:
        _usb_status.update(mounted=False, device=None, mountpoint=None)
        return jsonify({"ok": True, "ejected": None})
    os.sync()
    r = subprocess.run(("sudo", "umount", target),
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return jsonify({"error": f"eject failed: {(r.stderr or '').strip()}"}), 500
    _usb_status.update(mounted=False, device=None, mountpoint=None)
    return jsonify({"ok": True, "ejected": target})


@app.post("/api/train")
def api_train():
    """Start an embedding-pipeline training run on the chosen device.

    One run = the proven chain:
    ConvNeXt-tiny teacher @224 over every crop, then a 480px MNV2
    student distilled from it. Artifacts land beside the live pair as
    candidate_app.* — installing is its own explicit step
    (/api/train/install), so a bad run can never touch the decider.

    HARD RULE (user): when the GPU path fails, the failure is surfaced
    with the choice to retry on GPU or train on CPU. Never fall back
    silently — a 20-hour CPU run nobody asked for is not a recovery.
    """
    body = request.get_json(silent=True) or {}
    device = body.get("device")
    if device not in ("gpu", "cpu"):
        return jsonify({"error": "pick a device: gpu or cpu"}), 400
    if device == "cpu" and not _tf_available():
        return jsonify({"error": "no TensorFlow on this host — CPU "
                                 "training needs the trainer PC"}), 400
    if device == "gpu" and not _gpu_supported():
        return jsonify({"error": "no WSL GPU sandbox on this host"}), 400
    n = _crop_count()
    if n < 100:
        return jsonify({"error": f"only {n} training crops here — pull "
                                 "the dataset from the machine first"}), 400
    # same gate as the dataset pull: models must be built by the same
    # imaging code the machine sorts with (None = can't check, don't block)
    url = _machine_url()
    mdig = _machine_code_digest(url) if url else None
    if mdig and mdig != codesync.digest(ROOT):
        return jsonify({"error": "trainer code doesn't match the machine — "
                                 "use “Update trainer from machine” on the "
                                 "Train page, then retry"}), 409
    with state_lock:
        if train_status["running"]:
            return jsonify({"error": "training already running"}), 409
        train_status.update(running=True, device=device, stage="start",
                            epoch=0, epochs=0, acc=0, val_acc=0,
                            error=None, failed_device=None, result=None)
    threading.Thread(target=_train_thread, args=(device,),
                     daemon=True).start()
    return jsonify({"ok": True, "device": device, "est": _train_estimate()})


@app.get("/api/train/status")
def api_train_status():
    return jsonify(train_status)


# ---------------------------------------- remote training (trainer side) ---
def _machine_url():
    return (json.loads(CONFIG_PATH.read_text()).get("machine_url") or "").rstrip("/")


@app.get("/api/train/machine")
def api_train_machine():
    ok = False
    url = _machine_url()
    if url:
        try:
            import urllib.request
            with urllib.request.urlopen(url + "/api/train/status", timeout=5) as r:
                ok = r.status == 200
        except Exception:
            ok = False
    n_raw, raw_bytes = 0, 0
    raw_dir = DATA_DIR / "raw"
    if raw_dir.is_dir():
        for p in raw_dir.rglob("*.png"):
            n_raw += 1
            raw_bytes += p.stat().st_size
    code = {"local": codesync.digest(ROOT),
            "is_git": codesync.is_git_checkout(ROOT),
            "machine": None, "match": None}
    if url:
        code["machine"] = _machine_code_digest(url)
        if code["machine"]:
            code["match"] = code["machine"] == code["local"]
    return jsonify({"machine_url": url, "reachable": ok,
                    "can_train": _tf_available(), "code": code,
                    "dataset": {"images": n_raw, "bytes": raw_bytes}})


@app.post("/api/train/machine")
def api_train_machine_post():
    url = ((request.get_json() or {}).get("machine_url") or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "http://" + url
    with _config_lock:
        raw = json.loads(CONFIG_PATH.read_text())
        raw["machine_url"] = url
        write_cfg_raw(raw)
    return jsonify({"ok": True, "machine_url": url})


def _machine_hello(url):
    """The machine's identity beacon, or None when it can't say."""
    import urllib.request
    try:
        with urllib.request.urlopen(url + "/api/hello", timeout=5) as r:
            j = json.loads(r.read())
        return j if j.get("app") == "sortiq" else None
    except Exception:
        return None


def _machine_code_digest(url):
    """The machine's code digest, or None when it can't say — an older
    machine without the endpoint, or unreachable. Callers treat None as
    "can't check", never as a mismatch, so old machines keep working."""
    import urllib.request
    try:
        with urllib.request.urlopen(url + "/api/code/manifest?digest=1",
                                    timeout=5) as r:
            return json.loads(r.read()).get("digest")
    except Exception:
        return None


@app.post("/api/code/update")
def api_code_update():
    """One-click trainer update: make this install byte-identical to the
    machine's deployable code, then restart into it. Every byte is pulled
    and verified before anything on disk is touched."""
    # the restart at the end kills whatever this instance is doing — a
    # live sorting run died at case 92 to a docs deploy before this
    # guard existed. Busy means not now.
    busy = ("a sorting run" if run_mgr.status().get("running")
            else "training" if train_status.get("running")
            else "a gallery rebuild" if _gal_build.get("running")
            # scans hold results in memory — the restart wiped a finished
            # scan's flags once (an auto-deploy raced the user to them)
            else "a mislabel scan" if _scan_status.get("running")
            else "a duplicate scan" if _dup_status.get("running") else None)
    if busy:
        return jsonify({"error": f"{busy} is active — updating restarts "
                                 "the app and would kill it; retry when "
                                 "it finishes"}), 409
    import io
    import tarfile
    import urllib.request
    body = request.get_json(silent=True) or {}
    url = (body.get("machine_url") or _machine_url() or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "http://" + url
    if codesync.is_git_checkout(ROOT):
        return jsonify({"error": "this install is a git checkout — update it "
                                 "with git, not from the machine"}), 400
    if not url:
        return jsonify({"error": "set the sorting machine URL first"}), 400
    with state_lock:
        if train_status["running"]:
            return jsonify({"error": "training is running — wait for it to "
                                     "finish"}), 409
    try:
        with urllib.request.urlopen(url + "/api/code/manifest", timeout=15) as r:
            remote = json.loads(r.read())
    except Exception as e:
        return jsonify({"error": f"machine didn't answer the code manifest "
                                 f"({e}) — is it running current code?"}), 502
    fetch, delete = codesync.plan(ROOT, remote["files"])
    if not fetch and not delete:
        return jsonify({"ok": True, "updated": 0, "deleted": 0,
                        "restarting": False})
    got = {}
    for i in range(0, len(fetch), 50):
        chunk = fetch[i:i + 50]
        req = urllib.request.Request(
            url + "/api/code/files",
            data=json.dumps({"paths": chunk}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            blob = r.read()
        with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
            want = set(chunk)
            for m in tar.getmembers():
                if m.isfile() and m.name in want:
                    got[m.name] = tar.extractfile(m).read()
    missing = [p for p in fetch if p not in got]
    if missing:
        return jsonify({"error": f"machine didn't send {len(missing)} file(s) "
                                 f"(first: {missing[0]}) — nothing was "
                                 f"changed"}), 502
    for rel in fetch:
        codesync.write_file(ROOT, rel, got[rel])
    for rel in delete:
        codesync.delete_file(ROOT, rel)
    # the pull may have replaced codesync itself (new manifest rules);
    # verify with the freshly written module, not the copy this process
    # imported at startup — otherwise a rules change can never pass its
    # own verification and the update wedges one restart short
    try:
        import importlib
        verifier = importlib.reload(codesync)
    except Exception:
        verifier = codesync
    if verifier.digest(ROOT) != remote["digest"]:
        return jsonify({"error": "install doesn't match the machine after "
                                 "the pull (files changed mid-update?) — "
                                 "retry"}), 500
    print(f"code update: {len(fetch)} file(s) pulled, {len(delete)} deleted, "
          f"now at {remote['digest'][:12]} from {url}; restarting")
    threading.Thread(target=_relaunch_self, daemon=True).start()
    return jsonify({"ok": True, "updated": len(fetch), "deleted": len(delete),
                    "restarting": True})


# ------------------------------------------------ release self-update ---
# "Check for updates" in the System dialog: compare the local VERSION
# against the newest GitHub RELEASE (tagged versions only — users ride
# stable points, never master), and on the user's click make this
# install byte-identical to that release through the same plan/write/
# verify/restart path a trainer pull uses. Click-driven by design: the
# app never phones home on its own.
GH_REPO = "yamanub/SortIQ"


def _ver_tuple(s):
    return tuple(int(x) for x in re.findall(r"\d+", str(s))[:3]) or (0,)


def _local_version():
    try:
        return (ROOT / "VERSION").read_text().strip()
    except OSError:
        return "0"


def _latest_release():
    """Newest published version: the latest GitHub Release when one
    exists, else the highest-versioned bare tag (a maintainer who only
    tags still counts as releasing). Returns (tag, name, notes) or
    raises."""
    import urllib.request
    import urllib.error
    hdrs = {"Accept": "application/vnd.github+json"}
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GH_REPO}/releases/latest",
            headers=hdrs)
        with urllib.request.urlopen(req, timeout=15) as r:
            rel = json.loads(r.read())
        tag = rel.get("tag_name") or ""
        if tag:
            return tag, rel.get("name") or tag, (rel.get("body") or "")[:4000]
    except urllib.error.HTTPError as e:
        if e.code != 404:            # 404 = no Release objects, try tags
            raise
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GH_REPO}/tags?per_page=30",
        headers=hdrs)
    with urllib.request.urlopen(req, timeout=15) as r:
        tags = [t.get("name") or "" for t in json.loads(r.read())]
    tag = max((t for t in tags if _ver_tuple(t) > (0,)),
              key=_ver_tuple, default="")
    return tag, tag, ""


@app.get("/api/update/check")
def api_update_check():
    try:
        tag, name, notes = _latest_release()
    except Exception as e:
        return jsonify({"error": f"couldn't reach GitHub ({e})"}), 502
    if not tag:
        return jsonify({"error": "no releases or tags found"}), 502
    cur = _local_version()
    return jsonify({
        "current": cur, "latest": tag, "name": name, "notes": notes,
        "update_available": _ver_tuple(tag) > _ver_tuple(cur),
        "git_checkout": codesync.is_git_checkout(ROOT)})


@app.post("/api/update/apply")
def api_update_apply():
    """Update this install to the given release tag (default: latest).
    Same rules as a trainer pull: refused while anything is running,
    refused on git checkouts, every byte verified before restart."""
    import hashlib
    import io
    import tarfile
    import urllib.request
    busy = ("a sorting run" if run_mgr.status().get("running")
            else "training" if train_status.get("running")
            else "a gallery rebuild" if _gal_build.get("running")
            else "a mislabel scan" if _scan_status.get("running")
            else "a duplicate scan" if _dup_status.get("running") else None)
    if busy:
        return jsonify({"error": f"{busy} is active — updating restarts "
                                 "the app and would kill it; retry when "
                                 "it finishes"}), 409
    if codesync.is_git_checkout(ROOT):
        return jsonify({"error": "this install is a git checkout — update "
                                 "it with git"}), 400
    body = request.get_json(silent=True) or {}
    tag = (body.get("tag") or "").strip()
    if not tag:
        try:
            tag, _, _ = _latest_release()
        except Exception as e:
            return jsonify({"error": f"couldn't reach GitHub ({e})"}), 502
    if not tag:
        return jsonify({"error": "no release found"}), 502
    if (_ver_tuple(tag) <= _ver_tuple(_local_version())
            and not body.get("force")):
        return jsonify({"error": f"already on v{_local_version()} — "
                                 f"{tag} isn't newer"}), 400
    try:
        url = f"https://github.com/{GH_REPO}/archive/refs/tags/{tag}.tar.gz"
        with urllib.request.urlopen(url, timeout=300) as r:
            blob = r.read()
    except Exception as e:
        return jsonify({"error": f"release download failed: {e}"}), 502
    # the release tarball, filtered through the same rules the manifest
    # lives by, IS a remote manifest — the rest is the proven pull path
    skip_sfx = codesync._SKIP_SUFFIXES
    skip_dirs = codesync._SKIP_DIRS
    files, data = [], {}
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tar:
            for m in tar.getmembers():
                if not m.isfile():
                    continue
                parts = m.name.split("/")[1:]        # strip repo-tag prefix
                if not parts:
                    continue
                rel = "/".join(parts)
                tracked = (
                    (len(parts) == 1 and parts[0] in codesync.CODE_FILES)
                    or (parts[0] in codesync.CODE_DIRS
                        and not any(p in skip_dirs for p in parts)
                        and Path(parts[-1]).suffix.lower() not in skip_sfx))
                if not tracked:
                    continue
                raw = tar.extractfile(m).read()
                data[rel] = raw
                files.append({"path": rel, "size": len(raw),
                              "sha256": hashlib.sha256(raw).hexdigest()})
    except tarfile.TarError as e:
        return jsonify({"error": f"release tarball unreadable: {e}"}), 502
    if not files:
        return jsonify({"error": "release carries no tracked code files"}), 502
    fetch, delete = codesync.plan(ROOT, files)
    if not fetch and not delete:
        return jsonify({"ok": True, "updated": 0, "deleted": 0, "tag": tag,
                        "restarting": False})
    for rel in fetch:
        codesync.write_file(ROOT, rel, data[rel])
    for rel in delete:
        codesync.delete_file(ROOT, rel)
    # verify with the freshly written codesync (its rules may have
    # changed in this very release), comparing file-by-file against
    # what the tarball carried
    try:
        import importlib
        verifier = importlib.reload(codesync)
    except Exception:
        verifier = codesync
    now = {f["path"]: f["sha256"] for f in verifier.manifest(ROOT)["files"]}
    want = {f["path"]: f["sha256"] for f in files}
    if now != want:
        return jsonify({"error": "install doesn't match the release after "
                                 "the update (files changed mid-write?) — "
                                 "retry"}), 500
    print(f"release update: {tag} — {len(fetch)} file(s) written, "
          f"{len(delete)} deleted; restarting", flush=True)
    threading.Thread(target=_relaunch_self, daemon=True).start()
    return jsonify({"ok": True, "updated": len(fetch), "deleted": len(delete),
                    "tag": tag, "restarting": True})


def _relaunch_self():
    """Restart this server into freshly written code. The sleep lets the
    HTTP response flush.

    Under systemd (the Pi's sortiq.service) the trainer-style handoff
    inverts: any replacement we spawn dies with our control group when
    the main process exits, and exit(0) reads as success — which
    Restart=on-failure politely ignores, leaving the service dead. So on
    a supervised install, exit NONZERO and let the supervisor do the
    restart; that's what it's for. This is what makes the Pi safe to
    update over HTTP from an in-sync trainer.

    Everywhere else (the Windows/mac trainer — no supervisor) spawn a
    fully detached copy of ourselves and step aside; __main__'s
    bind-retry rides out the beat where both hold the port."""
    time.sleep(1.5)
    if os.environ.get("INVOCATION_ID"):   # stamped by systemd on services
        os._exit(1)
    # the child must NOT inherit this process's std handles: a detached
    # pythonw parent passes down stale ones, so the child's stdout is a
    # broken file object instead of the None the trainer.log shim checks
    # for — its first write (werkzeug's banner) then dies with no working
    # stderr to report to. Two field failures before this was caught.
    # Hand it the log file directly: valid handles, and startup output
    # lands where the shim would have put it anyway.
    sink = open(ROOT / "trainer.log", "ab")
    kw = dict(cwd=str(ROOT), stdin=subprocess.DEVNULL,
              stdout=sink, stderr=subprocess.STDOUT)
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kw["creationflags"] = 0x00000008 | 0x00000200
    else:
        kw["start_new_session"] = True
    subprocess.Popen([sys.executable] + sys.argv, **kw)
    os._exit(0)


@functools.lru_cache(maxsize=1)      # installed-or-not never changes mid-run
def _tf_available():
    """Whether this host can train at all (the Pi deliberately can't:
    2GB RAM, inference-only runtime)."""
    import importlib.util
    return importlib.util.find_spec("tensorflow") is not None


# ---------------------------------------- in-app embedding training ---
# The Train page's CPU/GPU selector. GPU jobs go through tools/gpu_run.py
# and nothing else — its preflight (venv seal, TF-sees-GPU sentinel),
# detached launch, and file markers each exist because a run died without
# them. The CPU path runs the same two tools locally at below-normal
# priority so a day-long run doesn't hobble the trainer.

GPU_JOB = "app_train"          # gpu_run job name; also its single-job lock

gpu_status = {"supported": False, "state": "unknown", "checks": [],
              "ts": None}


def _gpu_supported():
    import shutil as _sh
    return os.name == "nt" and _sh.which("wsl") is not None


def _gpu_run():
    """tools/gpu_run.py as a module (it's a script, not a package)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "gpu_run", ROOT / "tools" / "gpu_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _gpu_job_live():
    """A GPU job with a running.marker and no exit.marker is (or may
    be) alive inside the WSL VM — e.g. after a trainer crash + watchdog
    revival, the detached job survives this process."""
    jobs = ROOT / "gpu_jobs"
    if not jobs.is_dir():
        return False
    return any((d / "running.marker").exists()
               and not (d / "exit.marker").exists()
               for d in jobs.iterdir() if d.is_dir())


def _gpu_probe():
    """Full gpu_run preflight (boots the WSL VM, imports TF under the
    job env). Takes up to ~2 minutes cold — always called on a thread.

    The preflight's recovery step is `wsl --shutdown`, which would
    execute any job living in the VM — so recovery is only permitted
    when nothing is (or might be) running: a probe against a busy VM
    that times out must report failure, never amputate."""
    gpu_status["state"] = "probing"
    safe_to_recover = not train_status["running"] and not _gpu_job_live()
    try:
        good, checks = _gpu_run().preflight(recover=safe_to_recover)
        gpu_status.update(state="ready" if good else "failed",
                          checks=checks)
    except Exception as e:
        gpu_status.update(state="failed", checks=[f"FAIL probe: {e}"])
    gpu_status["ts"] = time.strftime("%H:%M:%S")


def _crop_count():
    return _crop_total()


def _train_estimate():
    """Minutes, scaled from a measured reference chain:
    teacher 46min + distill 53min on the GPU at ~7k crops. The one
    observed CPU fallback ran ~12x slower (655s/epoch vs 56)."""
    n = _crop_count()
    gpu_min = max(int(100 * n / 7000), 10)
    return {"crops": n, "gpu_min": gpu_min, "cpu_min": gpu_min * 12}


@app.get("/api/gpu")
def api_gpu():
    return jsonify({**gpu_status, "cpu_ok": _tf_available(),
                    "est": _train_estimate()})


@app.post("/api/gpu/probe")
def api_gpu_probe():
    if not gpu_status["supported"]:
        return jsonify({"error": "no WSL GPU sandbox on this host"}), 400
    if train_status["running"]:
        return jsonify({"error": "training is running — probing now "
                                 "could disturb the job; re-check after "
                                 "it finishes"}), 409
    if gpu_status["state"] != "probing":
        threading.Thread(target=_gpu_probe, daemon=True).start()
    return jsonify({"ok": True, "state": "probing"})


# one keras line per epoch (every fit here runs verbose=2)
_EPOCH_RE = re.compile(r"^Epoch (\d+)/(\d+)", re.M)
# epochs + ft-epochs per stage, at the tools' defaults
_STAGE_EPOCHS = {"teacher": 40 + 15, "distill": 20 + 15}


def _log_progress(text):
    """(stage, epochs_seen, epochs_total) from a chain log. Counting
    epoch lines since the stage marker rides out each stage's two fits
    (main + fine-tune) without caring where one ends."""
    t_i, d_i = text.rfind("STAGE_TEACHER"), text.rfind("STAGE_DISTILL")
    if d_i < 0 and t_i < 0:
        return "start", 0, 0
    stage = "distill" if d_i > t_i else "teacher"
    seen = len(_EPOCH_RE.findall(text[max(t_i, d_i):]))
    return stage, seen, _STAGE_EPOCHS[stage]


def _teacher_cmd(python):
    return [python, "-u", "tools/embedding_bench2.py",
            "--tag", "app_teacher", "--backbone", "convnext_tiny",
            "--img-size", "224", "--holdout", "0"]


def _distill_cmd(python):
    return [python, "-u", "tools/distill_student.py",
            "--teacher", "embedding_bench2_app_teacher.keras",
            "--img-size", "480", "--out-prefix", "candidate_app"]


class _TrainFail(Exception):
    pass


def _keep_awake():
    """Reset Windows' idle-sleep timer — called on every training poll
    tick. Modern Standby kills the WSL VM (and with it a running GPU
    job) even though ordinary processes survive, so an unattended
    overnight training dies silently ~20 minutes after the operator
    walks away. Ticking ES_SYSTEM_REQUIRED holds the machine up only
    while a job is actually running; once polling stops, normal power
    behavior resumes on its own."""
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x00000001)


def _train_gpu():
    """Submit the teacher->distill chain via gpu_run and babysit its file
    markers. The job script mirrors the proven overnight scripts: stage
    profile + tools to the WSL disk (/mnt/c IO starved a run once), run
    both stages there, copy artifacts back as candidate_app.*."""
    mod = _gpu_run()
    cfg = json.loads(CONFIG_PATH.read_text())
    act = cfg.get("active", {})
    prof = f"calibers/{act['cartridge']}/{act['model']}"
    src = mod._win_to_wsl(ROOT)
    py = f"{mod.VENV}/bin/python"
    script = "\n".join([
        "set -e",
        "W=~/sortiq-work",
        f'SRC="{src}"',
        f'P="{prof}"',
        'mkdir -p "$W/tools" "$W/$P/models" "$W/$P/data"',
        'cp "$SRC/config.json" "$W/"',
        'cp "$SRC/$P/model.json" "$W/$P/"',
        'rm -rf "$W/$P/data/stamp"',
        'cp -r "$SRC/$P/data/stamp" "$W/$P/data/"',
        'cp "$SRC/tools/embedding_bench2.py" '
        '"$SRC/tools/distill_student.py" "$W/tools/"',
        'echo "staged: $(find $W/$P/data/stamp -name \'*.png\' | wc -l)'
        ' crops"',
        'cd "$W"',
        "echo STAGE_TEACHER",
        " ".join(_teacher_cmd(py) + ["--embed-batch", "8"]),
        "echo STAGE_DISTILL",
        " ".join(_distill_cmd(py) + ["--embed-batch", "8"]),
        'for ext in tflite keras json; do',
        '  cp "$W/$P/models/candidate_app.$ext" "$SRC/$P/models/"',
        'done',
        'cp "$W/$P/models/embedding_bench2_app_teacher.json" '
        '"$SRC/$P/models/" || true',
        "echo APP_TRAIN_DONE",
        ""])
    mod.JOBS.mkdir(parents=True, exist_ok=True)
    script_path = mod.JOBS / f"{GPU_JOB}.sh"
    script_path.write_bytes(script.replace("\r\n", "\n").encode())
    job = mod.JOBS / GPU_JOB
    rc = mod.submit(GPU_JOB, script_path)
    if rc == 2:
        raise _TrainFail("a GPU job is already running — reap it with "
                         f"'python tools/gpu_run.py kill {GPU_JOB}' if "
                         "it's hung")
    if rc:
        checks = ""
        pf = job / "preflight.txt"
        if pf.exists():
            fails = [l for l in pf.read_text().splitlines()
                     if l.startswith("FAIL")]
            checks = "; ".join(fails) or pf.read_text()[-200:]
        raise _TrainFail(f"GPU preflight failed: {checks}")
    log, marker = job / "run.log", job / "exit.marker"
    t0 = time.time()
    while time.time() - t0 < 8 * 3600:
        _keep_awake()
        time.sleep(15)
        if log.exists():
            text = log.read_text(encoding="utf-8", errors="replace")
            stage, seen, total = _log_progress(text)
            train_status.update(stage=stage, epoch=seen, epochs=total)
        if marker.exists():
            rc = marker.read_text().strip()
            if rc != "0":
                tail = ""
                if log.exists():
                    lines = log.read_text(encoding="utf-8",
                                          errors="replace").splitlines()
                    tail = " · ".join(lines[-3:])
                raise _TrainFail(f"GPU job died (exit {rc}): {tail}")
            return
    raise _TrainFail("GPU job still running after 8h — check it with "
                     f"'python tools/gpu_run.py status {GPU_JOB}'")


def _train_cpu():
    """The same chain, run locally. Below-normal priority: the trainer
    stays usable under a run that can take most of a day."""
    job = ROOT / "gpu_jobs" / f"{GPU_JOB}_cpu"
    job.mkdir(parents=True, exist_ok=True)
    log_path = job / "run.log"
    log_path.write_bytes(b"")
    # BELOW_NORMAL_PRIORITY | CREATE_NO_WINDOW — low priority so the PC
    # stays usable, windowless so no console pops over the desktop
    flags = (0x00004000 | 0x08000000) if os.name == "nt" else 0
    for stage, cmd in (("teacher", _teacher_cmd(sys.executable)),
                       ("distill", _distill_cmd(sys.executable))):
        train_status.update(stage=stage, epoch=0,
                            epochs=_STAGE_EPOCHS[stage])
        offset = log_path.stat().st_size
        with open(log_path, "ab") as sink:
            sink.write(f"STAGE_{stage.upper()}\n".encode())
            p = subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                 stdout=sink, stderr=subprocess.STDOUT,
                                 creationflags=flags)
        while p.poll() is None:
            _keep_awake()
            time.sleep(10)
            with open(log_path, "rb") as f:
                f.seek(offset)
                text = f.read().decode("utf-8", errors="replace")
            seen = len(_EPOCH_RE.findall(text))
            train_status["epoch"] = seen
        if p.returncode != 0:
            tail = " · ".join(log_path.read_text(
                encoding="utf-8", errors="replace").splitlines()[-3:])
            raise _TrainFail(f"{stage} failed (exit {p.returncode}): {tail}")


def _train_thread(device):
    t0 = time.time()
    try:
        (_train_gpu if device == "gpu" else _train_cpu)()
        rep = json.loads((MODELS_DIR / "candidate_app.json").read_text())
        o90 = rep.get("open_set", {}).get("known_accept_0.9", {})
        train_status["result"] = {
            "kind": "embedding", "device": device,
            "minutes": int((time.time() - t0) / 60),
            "closed": rep["closed"]["top1"], "closed_n": rep["closed"]["n"],
            "fewshot": rep["fewshot"]["top1"],
            "fewshot_n": rep["fewshot"]["n"],
            "unknown_accepted": o90.get("unknown_accepted"),
            "wrong_id": o90.get("known_wrong_identity_accepted")}
        print(f"embedding training done on {device} in "
              f"{train_status['result']['minutes']}min", flush=True)
    except _TrainFail as e:
        train_status.update(error=str(e), failed_device=device)
    except Exception as e:
        train_status.update(error=f"{type(e).__name__}: {e}",
                            failed_device=device)
    finally:
        train_status.update(running=False, stage=None)


@app.post("/api/train/install")
def api_train_install():
    """Promote the freshly trained candidate to the live decider pair:
    archive the current pair, build the candidate's own gallery in a
    staging dir (the pair must never be mismatched in place), swap both
    in, and push the pair to the machine. Explicitly user-triggered —
    training never installs anything by itself."""
    import shutil as _sh
    import urllib.request
    body = request.get_json(silent=True) or {}
    push = bool(body.get("push", True))
    cand = MODELS_DIR / "candidate_app.tflite"
    if not cand.is_file():
        return jsonify({"error": "no candidate_app.tflite here — train "
                                 "first"}), 400
    if _gal_build["running"]:
        return jsonify({"error": "a gallery rebuild is running — wait for "
                                 "it to finish"}), 409
    url = _machine_url()
    if push and not url:
        return jsonify({"error": "set the sorting machine URL first"}), 400
    if push:
        # the pair must land on the machine this profile mirrors — with
        # several machines, pushing A's model to B is one stale URL away
        src_p = MODELS_DIR.parent / "source.json"
        hello = _machine_hello(url) or {}
        if src_p.exists() and hello.get("host"):
            known = json.loads(src_p.read_text())
            if known.get("host") and known["host"] != hello["host"]:
                return jsonify({"error":
                    f"this profile mirrors machine "
                    f"“{known.get('name') or known['host']}” but the "
                    f"configured machine is "
                    f"“{hello.get('name') or hello['host']}” — fix the "
                    "machine URL (or switch profiles) before "
                    "installing"}), 409
    with state_lock:
        if train_status["running"]:
            return jsonify({"error": "training is running"}), 409
        train_status.update(running=True, device=None, stage="gallery",
                            epoch=0, epochs=0, error=None,
                            failed_device=None, result=None)

    def work():
        stage_dir = MODELS_DIR / "_install_stage"
        try:
            if stage_dir.exists():
                _sh.rmtree(stage_dir)
            stage_dir.mkdir(parents=True)
            _sh.copy2(cand, stage_dir / "shadow_embed.tflite")
            # the bench report becomes the model's provenance sidecar —
            # it rides along to the machine and into the archive, so
            # Model details can always say what this generation scored
            cand_rep = MODELS_DIR / "candidate_app.json"
            if cand_rep.exists():
                _sh.copy2(cand_rep, stage_dir / "shadow_embed.json")
            r = subprocess.run(
                (sys.executable, str(ROOT / "tools" / "build_gallery.py"),
                 "--model", str(stage_dir / "shadow_embed.tflite")),
                capture_output=True, text=True, timeout=5400, cwd=ROOT)
            if r.returncode != 0:
                raise _TrainFail("gallery build failed: "
                                 + (r.stderr or r.stdout or "").strip()[-300:])
            gal = stage_dir / "shadow_gallery.npz"
            if not gal.is_file():
                raise _TrainFail("gallery build wrote nothing")
            arch_dir = MODELS_DIR / "archive"
            arch_dir.mkdir(exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            archived = False
            pair = ("shadow_embed.tflite", "shadow_gallery.npz",
                    "shadow_embed.json")
            for name in pair:
                cur = MODELS_DIR / name
                if cur.exists():
                    stem, suf = cur.stem, cur.suffix
                    _sh.copy2(cur, arch_dir / f"{stem}_{ts}{suf}")
                    archived = archived or name.endswith(".tflite")
            for name in pair:
                if (stage_dir / name).exists():
                    os.replace(stage_dir / name, MODELS_DIR / name)
                elif name.endswith(".json"):     # no sidecar this time:
                    (MODELS_DIR / name).unlink(missing_ok=True)
            reply = None
            if push:
                train_status["stage"] = "install"
                files = {n: (MODELS_DIR / n).read_bytes()
                         for n in pair if (MODELS_DIR / n).exists()}
                data, ctype = _multipart(files)
                req = urllib.request.Request(
                    url + "/api/models/install", data=data, method="POST",
                    headers={"Content-Type": ctype})
                with urllib.request.urlopen(req, timeout=300) as resp:
                    reply = json.loads(resp.read())
            train_status["result"] = {
                "kind": "embedding_install", "pushed": push,
                "machine": url if push else None,
                "archived_as": ts if archived else None,
                "machine_archived_as": (reply or {}).get("archived_as")}
        except _TrainFail as e:
            train_status["error"] = str(e)
        except Exception as e:
            train_status["error"] = f"{type(e).__name__}: {e}"
        finally:
            if stage_dir.exists():
                _sh.rmtree(stage_dir, ignore_errors=True)
            train_status.update(running=False, stage=None)

    threading.Thread(target=work, daemon=True).start()
    return jsonify({"ok": True, "push": push})


# ------------------------------------------------------------- discovery ---
@app.get("/api/hello")
def api_hello():
    """Tiny identity beacon so a subnet scan can tell SortIQ instances
    from whatever else answers on port 5000. mDNS names flake (observed
    dropping mid-run on Windows); scanning by IP + this endpoint is the
    name-free fallback."""
    cfg = load_cfg()
    ident = _identity()
    return jsonify({"app": "sortiq", "host": ident["host"],
                    "name": ident["name"],
                    "model": f"{cfg.cartridge}/{cfg.model_name}",
                    "can_train": _tf_available()})


@app.post("/api/machines/scan")
def api_machines_scan():
    """Sweep this host's /24 for SortIQ instances (port 5000 + hello).
    ~2s: 254 addresses probed on 64 threads, 0.3s connect timeout."""
    import socket as _s
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor
    s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
    try:                       # routing trick: no packet is actually sent
        s.connect(("8.8.8.8", 80))
        me = s.getsockname()[0]
    finally:
        s.close()
    base = me.rsplit(".", 1)[0]

    def probe(ip):
        try:
            _s.create_connection((ip, 5000), timeout=0.3).close()
        except OSError:
            return None
        try:
            with urllib.request.urlopen(f"http://{ip}:5000/api/hello",
                                        timeout=2) as r:
                j = json.loads(r.read())
        except Exception:
            return None
        if j.get("app") != "sortiq":
            return None
        j["ip"] = ip
        j["self"] = ip == me
        return j

    with ThreadPoolExecutor(max_workers=64) as ex:
        found = [r for r in ex.map(probe, (f"{base}.{i}"
                                           for i in range(1, 255))) if r]
    return jsonify({"subnet": f"{base}.0/24", "me": me, "found": found})


# ------------------------------------------------------------- fleet ---
# Every instance can show the whole fleet: the page you're browsing
# polls its peers SERVER-side (no browser cross-origin games), so the
# view works from any device against any machine — trainer optional.
# The status card is deliberately skeletal (no image work, no directory
# walks) so answering costs nothing mid-run.

def _own_lan_url():
    import socket as _s
    s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
    try:                       # routing trick: no packet is actually sent
        s.connect(("8.8.8.8", 80))
        return f"http://{s.getsockname()[0]}:5000"
    finally:
        s.close()


def _fleet_self_status():
    cfg = load_cfg()
    ident = _identity()
    rs = run_mgr.status()
    bc = rs.get("bin_counts") or {}
    try:
        ver = (ROOT / "VERSION").read_text().strip()
    except OSError:
        ver = "dev"
    return {"name": ident["name"], "host": ident["host"],
            "model": f"{cfg.cartridge}/{cfg.model_name}",
            "classes": len(cfg.stamp_labels),
            "camera": ("ok" if _camera["cap"] is not None
                       else _camera["error"] or "idle"),
            "board": _console["transport"] is not None,
            "run": {"running": bool(rs.get("running")),
                    "capture": bool(rs.get("capture")),
                    "cases": sum(bc.values()) if bc else 0},
            "version": ver, "digest": codesync.digest(ROOT),
            "can_train": _tf_available()}


@app.get("/api/fleet/status")
def api_fleet_status():
    return jsonify(_fleet_self_status())


@app.get("/api/fleet/peers")
def api_fleet_peers_get():
    raw = json.loads(CONFIG_PATH.read_text())
    return jsonify({"peers": raw.get("fleet_peers", [])})


@app.post("/api/fleet/peers")
def api_fleet_peers_set():
    body = request.get_json() or {}
    peers = [str(u).rstrip("/") for u in (body.get("peers") or [])
             if str(u).startswith("http")][:32]
    with _config_lock:
        raw = json.loads(CONFIG_PATH.read_text())
        raw["fleet_peers"] = sorted(set(peers))
        write_cfg_raw(raw)
    return jsonify({"ok": True, "peers": raw["fleet_peers"]})


@app.get("/api/fleet")
def api_fleet():
    """Everyone's status card, self first. Peers answer with a short
    timeout; the unreachable stay on the list, grayed — which doubles
    as the is-that-machine-frozen telltale."""
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor
    me = _fleet_self_status()
    me.update(url=None, reachable=True, same_build=True, me=True)
    peers = json.loads(CONFIG_PATH.read_text()).get("fleet_peers", [])

    def fetch(url):
        try:
            # generous budget: a freshly restarted peer's first status
            # answer hashes its whole code tree (seconds on a Pi's SD)
            with urllib.request.urlopen(url + "/api/fleet/status",
                                        timeout=6) as r:
                j = json.loads(r.read())
            j.update(url=url, reachable=True, me=False,
                     same_build=j.get("digest") == me["digest"])
            return j
        except Exception:
            return {"url": url, "reachable": False, "me": False}
    cards = [me]
    if peers:
        with ThreadPoolExecutor(max_workers=8) as ex:
            cards += list(ex.map(fetch, peers))
    return jsonify({"cards": cards})


@app.post("/api/fleet/update")
def api_fleet_update():
    """Tell a drifted peer to pull code from THIS instance — the
    direction /api/code/update already speaks, driven server-side so
    any browser can press the button."""
    import urllib.request
    peer = ((request.get_json() or {}).get("peer") or "").rstrip("/")
    if not peer.startswith("http"):
        return jsonify({"error": "peer url required"}), 400
    try:
        req = urllib.request.Request(
            peer + "/api/code/update",
            data=json.dumps({"machine_url": _own_lan_url()}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return jsonify(json.loads(r.read()))
    except Exception as e:
        return jsonify({"error": f"peer didn't take the update: {e}"}), 502


def _multipart(files):
    """Encode {name: bytes} as multipart/form-data (stdlib-only client)."""
    import uuid
    boundary = uuid.uuid4().hex
    out = bytearray()
    for name, data in files.items():
        out += (f'--{boundary}\r\nContent-Disposition: form-data; '
                f'name="{name}"; filename="{name}"\r\n'
                f'Content-Type: application/octet-stream\r\n\r\n').encode()
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


@app.post("/api/train/remote")
def api_train_remote():
    """Dataset mirror from the machine: pull the delta, rebuild crops.

    Softmax training is retired — this endpoint
    now only pulls (any body is treated as pull_only). Embedding training
    lands here with the Train-page GPU selector."""
    body = request.get_json() or {}
    pull_only = True
    url = _machine_url()
    if not url:
        return jsonify({"error": "set the sorting machine URL first"}), 400
    # a stale trainer must not touch the dataset or train: crops rebuilt
    # with drifted imaging code teach the models geometry the machine will
    # never produce at sort time. (An older machine without the code
    # endpoint returns None here — can't check, don't block.)
    mdig = _machine_code_digest(url)
    if mdig and mdig != codesync.digest(ROOT):
        return jsonify({"error": "trainer code doesn't match the machine — "
                                 "use “Update trainer from machine” "
                                 "on the Train page, then retry"}), 409
    epochs = int(body.get("epochs", 40))
    with state_lock:
        if train_status["running"]:
            return jsonify({"error": "training already running"}), 409
        train_status.update(running=True, stage="pull", epoch=0, epochs=epochs,
                            acc=0, val_acc=0, error=None, result=None,
                            phase="pull", pulled=0, total=0)

    def run():
        import urllib.error
        import urllib.parse
        import urllib.request

        def fetch_json(path):
            with urllib.request.urlopen(url + path, timeout=30) as r:
                return json.loads(r.read())

        try:
            man = fetch_json("/api/dataset/manifest")
            cart, model = man["cartridge"], man["model"]
            pdir = profiles.model_dir(ROOT, cart, model)
            # one mirror belongs to ONE machine: two machines running the
            # same profile name would silently mirror-delete each other's
            # datasets right here. The first pull stamps the source; a
            # different machine must rename its model first.
            hello = _machine_hello(url) or {}
            src_p = pdir / "source.json"
            if src_p.exists() and hello.get("host"):
                known = json.loads(src_p.read_text())
                if known.get("host") and known["host"] != hello["host"]:
                    raise RuntimeError(
                        f"profile {cart}/{model} mirrors machine "
                        f"“{known.get('name') or known['host']}” but this "
                        f"pull is from “{hello.get('name') or hello['host']}”"
                        " — give the new machine's model a unique name "
                        "(its Dataset page), then retry")
            (pdir / "data").mkdir(parents=True, exist_ok=True)
            (pdir / "models").mkdir(parents=True, exist_ok=True)
            if hello.get("host"):
                src_p.write_text(json.dumps(
                    {"host": hello["host"], "name": hello.get("name"),
                     "url": url}))
            profiles.write_model(ROOT, cart, model, man["model_json"])
            data_dir = pdir / "data"

            # mirror raw/ exactly: pull new/changed (by size — captures are
            # immutable), delete what the machine no longer has (renames,
            # merges, deletions must propagate or classes resurrect here)
            want = {f["path"]: f for f in man["files"]}
            train_status["total"] = len(want)
            raw_dir = data_dir / "raw"
            if raw_dir.is_dir():
                for p in list(raw_dir.rglob("*.png")):
                    if p.relative_to(data_dir).as_posix() not in want:
                        p.unlink()
                for d in sorted(raw_dir.glob("*/"), reverse=True):
                    if d.is_dir() and not any(d.iterdir()):
                        d.rmdir()
            need = [rel for rel, f in want.items()
                    if not (data_dir / rel).exists()
                    or (data_dir / rel).stat().st_size != f["size"]]
            train_status["pulled"] = len(want) - len(need)
            # batched tar pulls (50/call): per-file GETs measured ~3s each
            # over the machine's Wi-Fi - the batch endpoint is ~50x fewer
            # round trips. Falls back to single GETs on older machines.
            import io
            import tarfile
            root = str(data_dir.resolve()) + os.sep

            def pull_one(rel):
                q = urllib.parse.urlencode({"path": rel})
                with urllib.request.urlopen(
                        f"{url}/api/dataset/file?{q}", timeout=60) as r:
                    dst = data_dir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(r.read())

            import http.client
            for i in range(0, len(need), 50):
                chunk = need[i:i + 50]
                # a 390MB first pull over Wi-Fi WILL hit a blip eventually;
                # one dropped read must not abort the whole run
                for attempt in range(5):
                    try:
                        req = urllib.request.Request(
                            url + "/api/dataset/files",
                            data=json.dumps({"paths": chunk}).encode(),
                            headers={"Content-Type": "application/json"},
                            method="POST")
                        with urllib.request.urlopen(req, timeout=300) as r:
                            blob = io.BytesIO(r.read())
                        with tarfile.open(fileobj=blob) as tar:
                            for m in tar.getmembers():
                                dst = (data_dir / m.name).resolve()
                                if not m.isfile() or not str(dst).startswith(root):
                                    continue    # never extract outside data/
                                dst.parent.mkdir(parents=True, exist_ok=True)
                                dst.write_bytes(tar.extractfile(m).read())
                        break
                    except urllib.error.HTTPError:
                        for rel in chunk:       # machine predates the batch
                            pull_one(rel)       # endpoint - go one by one
                        break
                    except (OSError, http.client.HTTPException,
                            tarfile.ReadError):    # truncated body
                        if attempt == 4:
                            raise
                        time.sleep(2 * (attempt + 1))
                train_status["pulled"] += len(chunk)
                _keep_awake()          # a first full sync can outlast
                                       # the idle-sleep timeout

            # adopt the machine's profile locally (imaging config included),
            # then rebuild crops so they match the pulled raws exactly
            train_status.update(phase="crops", stage="crops")
            set_active_model(cart, model)
            img_cfg = man["model_json"].get("imaging") or {}
            imaging.configure(img_cfg)
            # incremental: only the pull delta gets decoded — the crop
            # signature (imaging settings + code) still forces a full
            # reshape when the machine's imaging changed since last pull
            n_crops, _ = rebuild_crops(data_dir, incremental=True)

            train_status["result"] = {
                "pull_only": True, "files": len(want),
                "crops": n_crops, "machine": url,
                "profile": f"{cart}/{model}"}
        except Exception as e:   # surfaced to the UI, not swallowed
            # include the type: a bare socket TimeoutError str()s to ""
            train_status["error"] = f"{type(e).__name__}: {e}".rstrip(": ")
        finally:
            train_status["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})


# ------------------------------------------------------------- run loop ---
class _ServerCamera:
    """Camera adapter for hardware runs: reuses the shared webcam + zoom.

    capture() returns the same settled, noise-averaged, circle-smoothed
    frame the Collect flow saves — run-time input must match the training
    distribution or run accuracy silently trails collection accuracy.

    settle_s exists because the constant's real meaning is ABSOLUTE time
    between the case seating and the first frame (~0.45s in production
    sorting, where the flicker probe's think time hides most of it and
    COLLECT_SETTLE_S tops it up). Batch capture skips the probe, so its
    feed ack arrives at the true seat time and the wait must be explicit
    — a longer settle keeps frames landing at the same post-seat instant
    as sorting, i.e. the same training distribution. Vibration measured
    on the reference rig: stable ~250-310ms after seat."""

    def __init__(self, settle_s=COLLECT_SETTLE_S):
        self.center = None       # smoothed (cx, cy, r) of the last capture
        self.settle_s = settle_s

    def capture(self, light):
        time.sleep(self.settle_s)
        frame, self.center = steady_head()
        return frame

    def release(self):
        pass


KEEP_RUNS = 5   # rolling window of run folders kept on disk — PER KIND:
                # the newest 5 sorting runs AND the newest 5 batch
                # captures, on separate shelves, so a capture spree can't
                # evict a sort report (or vice versa) before it's been
                # reviewed. Runs store a full frame for EVERY case
                # (~80MB per 400-case run) so any card in the report can
                # be filed into the dataset.


def _run_is_capture(d):
    try:
        return bool(json.loads((d / "run.json").read_text()).get("capture"))
    except (OSError, ValueError):
        return False              # unlabeled (pre-capture-flag) = sort run


def _prune_runs(keep=KEEP_RUNS):
    """Delete all but the newest `keep` run folders OF EACH KIND."""
    import shutil
    runs_dir = ROOT / "runs"
    if not runs_dir.is_dir():
        return
    dirs = sorted((p for p in runs_dir.iterdir() if p.is_dir()), reverse=True)
    seen = {True: 0, False: 0}
    for d in dirs:
        kind = _run_is_capture(d)
        seen[kind] += 1
        if seen[kind] > keep:
            shutil.rmtree(d, ignore_errors=True)


def _persist_bin(slot, stamp):
    """Add an auto-assigned headstamp to a slot's group in the active model,
    so it shows on the Run page bin map and carries into the next run."""
    from sorter.config import normalize_bin
    with _config_lock:
        mj = active_model_raw()
        bins = [normalize_bin(b) for b in (mj.get("bins") or [])]
        bins += [[] for _ in range(slot + 1 - len(bins))]
        if stamp not in bins[slot]:
            bins[slot].append(stamp)
        mj["bins"] = bins
        write_active_model(mj)


def _clear_bins():
    """Empty every non-UNMATCHED bin in the active model."""
    from sorter.config import normalize_bin
    with _config_lock:
        mj = active_model_raw()
        mj["bins"] = [["UNMATCHED"] if "UNMATCHED" in normalize_bin(b) else []
                      for b in (mj.get("bins") or [])]
        write_active_model(mj)


class RunManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()
        self._reset()

    def _reset(self):
        # auto-assign: fill empty trays with new headstamps as they appear;
        # unknown / low-confidence / overflow -> the catch-all slot (0).
        self.auto_assign = False
        self.slot_count = 8
        self.slots_enabled = list(range(8))
        self.catch_all = 0
        self.slot_stamp = {}            # slot:int -> [stamps] (mirrors the bin map)
        self.slot_stamp_inv = {}        # stamp -> slot:int
        self.state = {"running": False, "finished": False, "run_id": None,
                      "mode": None, "sorted": 0, "rejected": 0, "jams": 0,
                      "counts": {}, "bin_counts": {}, "bin_stamp_counts": {},
                      "recent": [],
                      "rate": 0.0, "error": None, "score": None,
                      "auto_assign": False, "slots": [],
                      "end_reason": None,   # "out_of_brass" | "stopped"
                      "flushed": 0,         # in-flight cases the end-of-brass
                                            # flush placed in their true slots
                      "feed_stats": None}   # fork firmware homing telemetry,
                                            # polled before the board handback

    def _slot_owner(self, s):
        """Display label for a slot — its stamps joined (multi-stamp bins)."""
        group = self.slot_stamp.get(s) or []
        return " · ".join(group) or None

    def _rebuild_slots(self):
        """state['slots'] = per-slot {slot, stamp, stamps, count} for the Run
        tab — stamp is the joined display label, stamps the raw group."""
        bc = self.state["bin_counts"]
        self.state["slots"] = [{"slot": s, "stamp": self._slot_owner(s),
                                "stamps": list(self.slot_stamp.get(s, [])),
                                "catch_all": s == self.catch_all,
                                "count": bc.get(str(s), 0)}
                               for s in self.slots_enabled]

    def _resolve_slot(self, d):
        """Destination slot for one decision (auto-assign aware). New
        assignments are persisted into the active model's bin map."""
        if not self.auto_assign:
            return d.bin_id
        # a confident read that merely lacks a bin (no_bin_mapping) is exactly
        # what auto-assign exists to place; only real rejects go to catch-all
        if d.reason not in ("ok", "no_bin_mapping") or not d.stamp:
            return self.catch_all
        with self.lock:
            if d.stamp in self.slot_stamp_inv:
                return self.slot_stamp_inv[d.stamp]
            used = set(self.slot_stamp) | {self.catch_all}
            free = next((s for s in self.slots_enabled if s not in used), None)
            if free is None:
                return self.catch_all                # trays full -> catch-all
            self.slot_stamp[free] = [d.stamp]
            self.slot_stamp_inv[d.stamp] = free
        _persist_bin(free, d.stamp)                  # -> Sorting page + next run
        return free

    def clear_slots(self):
        with self.lock:
            self.slot_stamp, self.slot_stamp_inv = {}, {}
            self._rebuild_slots()
        _clear_bins()                                # empty the model's bin map

    def reset_counters(self):
        with self.lock:
            self.state.update(sorted=0, rejected=0, jams=0, counts={},
                              bin_counts={}, bin_stamp_counts={})
            self._rebuild_slots()

    def status(self):
        with self.lock:
            s = {k: v for k, v in self.state.items() if k != "recent"}
            s["recent"] = list(self.state["recent"])[-10:]
            # nested dicts must be COPIED under the lock: jsonify iterates
            # them after release, racing the writer thread's inserts
            for k in ("counts", "bin_counts"):
                if isinstance(s.get(k), dict):
                    s[k] = dict(s[k])
            if isinstance(s.get("bin_stamp_counts"), dict):
                s["bin_stamp_counts"] = {k: dict(v) for k, v
                                         in s["bin_stamp_counts"].items()}
            # a pending stop is user-visible state: the loop only reads
            # the flag between case cycles (and finishes the end-of-brass
            # flush regardless), so the UI must say "stopping" instead of
            # letting the button look ignored — field-hit during a flush
            s["stopping"] = bool(s.get("running") and self.stop_evt.is_set())
            return s

    def start(self, params):
        # claim the running slot before the slow model load, or two
        # concurrent starts can both pass the check and drive the hardware
        with self.lock:
            if self.state["running"]:
                return "a run is already active"
            self.stop_evt.clear()
            self._reset()
            self.state["running"] = True
        # capture runs never consult the decider for a bin (every case goes
        # to the catch-all — see below), so a fresh install with no model
        # yet can still batch-capture its first dataset; only a real
        # sorting run needs one.
        if get_shadow() is None and not params.get("capture"):
            with self.lock:
                self.state["running"] = False
            return ("no embedding model installed (shadow_embed.tflite + "
                    "shadow_gallery.npz) — install one from the trainer first")
        threading.Thread(target=self._run, args=(params,), daemon=True).start()
        return None

    def stop(self):
        self.stop_evt.set()

    def _run(self, params):
        cfg = load_cfg()
        self.auto_assign = bool(params.get("auto_assign"))
        # batch-capture mode: the machine runs at full pipelined speed
        # purely to PHOTOGRAPH brass — every case is classified for the
        # pre-label but physically sent to the catch-all, and the whole
        # run reviews as grouped confirm-cards instead of a sort report
        self.capture = bool(params.get("capture"))
        self.slot_count = cfg.bin_count
        self.slots_enabled = list(cfg.slots_enabled)
        self.catch_all = cfg.unmatched_bin
        # seed from the Run page's bin map; auto-assign only fills the gaps.
        # The catch-all's own group may hold stamps too (explicitly routed
        # to the reject tray) — they keep their mapping, the sentinel is
        # display-only.
        self.slot_stamp = {i: [s for s in g if s != "UNMATCHED"]
                           for i, g in enumerate(cfg.bins)
                           if any(s != "UNMATCHED" for s in g)}
        self.slot_stamp_inv = {s: i for i, g in self.slot_stamp.items()
                               for s in g}
        with self.lock:
            self.state["auto_assign"] = self.auto_assign
            self._rebuild_slots()
        run_id = time.strftime("run_%Y%m%d_%H%M%S")
        run_dir = ROOT / "runs" / run_id
        rejects_dir = run_dir / "rejects"
        rejects_dir.mkdir(parents=True, exist_ok=True)
        thumbs_dir = run_dir / "thumbs"     # head crops of the GOOD cases,
        thumbs_dir.mkdir(exist_ok=True)     # for the end-of-run report
        frames_dir = run_dir / "frames"     # full frames of the GOOD cases
        frames_dir.mkdir(exist_ok=True)     # too: an accepted-but-wrong
        # stranger used to escape without evidence — now every card in
        # the report can be reassigned/filed. KEEP_RUNS bounds the disk.
        # so listings can label sim runs — synthetic cases in a report are
        # confusing next to real ones
        (run_dir / "run.json").write_text(json.dumps(
            {"mode": params.get("mode", "sim"), "capture": self.capture}))
        _prune_runs(KEEP_RUNS)          # keep only the newest N runs on disk
        transport = camera = None
        machine = None
        wq = writer = None   # persistence thread; created inside the run
        t0 = time.time()
        with self.lock:
            self.state.update(run_id=run_id, mode=params.get("mode", "sim"),
                              capture=self.capture)
        try:
            if params.get("mode") == "serial":
                from sorter.cs72 import Cs72Transport
                port = cfg.serial.get("port") or "/dev/ttyUSB0"
                # The run ADOPTS the console's live link instead of opening
                # the port a second time: two readers on one tty steal each
                # other's replies, and a fresh open DTR-resets the Uno,
                # wiping the pushed machine settings for the whole run.
                # Connecting via the console path first also guarantees the
                # Ready handshake + setter push have happened.
                with _console["lock"]:
                    have = _console["transport"] is not None
                if not have:
                    _console_connect("serial", port)   # waits Ready, pushes settings
                with _console["lock"]:
                    con = _console["transport"]
                    if con is None:
                        raise RuntimeError("board is not connected")
                    if _console["stop"]:
                        _console["stop"].set()          # retire the console reader
                    link = con.link                     # keep the port OPEN (no reset)
                    _console.update(transport=None, stop=None, mode=None)
                    _console["hold"] = True             # freeze auto-connect
                time.sleep(0.7)          # let the reader finish its last read tick
                # adopted link = board already booted; there is no Ready
                # coming, so don't sit out the full boot-banner timeout
                transport = Cs72Transport(link, ready_timeout=1.0)
                # re-assert the SAVED lighting on the adopted link: a board
                # reboot between the console's last push and this run leaves
                # firmware-default lighting (field-hit: first cases of a run
                # blown out white until the operator noticed)
                try:
                    ms = machine_settings()
                    if ms.get("camera_led") is not None:
                        link.write(f"cameraledlevel:{int(ms['camera_led'])}\n")
                    lc = (ms.get("led_color") or "").lstrip("#")
                    if len(lc) == 6:
                        r, g, b = (int(lc[i:i+2], 16) for i in (0, 2, 4))
                        link.write(f"ledcolor:{r},{g},{b}\n")
                except Exception:
                    pass
                # capture mode: no flicker probe -> the feed ack IS the
                # seat time, so the settle carries the whole post-seat
                # wait that think time covers during sorting (see
                # _ServerCamera). 280ms sat INSIDE the rig's measured
                # 250-310ms vibration band and averaged ~1% of frames
                # into motion ghosts (doubled/rotated headstamps, run
                # 20260802_203822 cases 148/237); 350ms clears the band
                # with margin at ~70ms/case.
                camera = _ServerCamera(settle_s=0.35 if self.capture
                                       else COLLECT_SETTLE_S)
                # SS2 fork: the mechanical cycle runs UNDER inference
                # (PFEED right after the photo, PSLOT when the slot is
                # known). Older firmware keeps the sequential SORT path.
                # ss2 AND pico speak the pipelined cycle (pf/ps:); the pico
                # kind postdates this gate and fell through to the stock
                # sequential path, which 2.0 doesn't speak — every sort
                # timed out, HOME, re-prime, forever (first field run).
                pipelined = machine_settings().get("firmware") in ("ss2", "pico")
            else:
                from sorter.esp32_sim import VirtualMachine, Esp32Sim
                from sorter.transport import SimTransport
                from sorter.camera import SyntheticCamera
                seed = params.get("seed")
                machine = VirtualMachine(cfg, n_cases=int(params.get("cases", 50)),
                                         jam_rate=float(params.get("jam_rate", 0)),
                                         seed=int(seed) if seed not in (None, "") else None)
                transport = SimTransport(Esp32Sim(machine))
                camera = SyntheticCamera(machine)
                pipelined = False

            with (run_dir / "log.jsonl").open("w") as log_f, \
                 (rejects_dir / "meta.jsonl").open("w") as meta_f:
                # Wheel geometry (bench truth): a case takes TWO
                # feeds to travel from the prox sensor to the camera, and
                # one more to the drop port — so at any moment up to three
                # cases sit past the sensor (the "~3 stragglers").
                #
                # Run START: the first photo comes up empty until the first
                # case has walked in; a small PRIME_FEEDS budget of forced
                # feeds covers the walk instead of declaring out-of-brass.
                #
                # Run END: when a SORT's reply is FLUSH_WAITS consecutive
                # "waiting for brass" lines, cancel_wait confirms the gate
                # is truly dry (or resumes if a feed won the race) and the
                # loop enters DRY mode: each sort is reissued as a forced
                # FLUSH:prev:slot, and whatever the feed walks into the
                # camera next is photographed, classified, and flushed to
                # its own slot too — including the tail case the run never
                # saw. An empty camera closes with one final FEED; every
                # case lands in its TRUE bin and the wheel ends empty.
                # The whole dance is proven against the ported-firmware
                # simulator (tools/cs72_flush_selftest.py), which also
                # reproduces the two field misplacements (#50/#52, reverted
                # in #53) as regression evidence. Idle-loop WAITINGs
                # (WAIT_LIMIT) still plain-stop: they only occur in states
                # (jam recovery, mid-prime) where the in-flight picture
                # isn't known, and blind flushing is the one thing this
                # machine must not do.
                from sorter.cs72 import cancel_wait
                WAIT_LIMIT, FLUSH_WAITS, EMPTY_FEEDS = 8, 6, 4
                # dry mode is no longer a one-way door: the wheel holds at
                # most ~3 cases past the sensor, so if this many CONSECUTIVE
                # flush cycles each deliver a real case, the tube is
                # provably still feeding — the end-of-brass call was a
                # false alarm (a delivery gap), and the run resumes full
                # speed instead of walking the rest of the bowl at flush
                # pace (field incident: 76 cases rode the slow tail)
                DRY_RESUME = 6
                waiting = 0
                prev_slot = 0        # slot of the previous sort/prime (the
                                     # prime force-feeds at the park slot 0)
                dry = False          # end-of-brass flush loop is running
                dry_present = 0      # consecutive flush cases with brass
                empty_left = EMPTY_FEEDS
                flushed = 0          # cases counted while in DRY mode
                sorted_n = 0         # local case counter; the writer
                                     # thread owns state["sorted"]

                # Persistence (PNG encodes, thumbs, log lines, state
                # counters) rides a single writer thread. Inline it ran
                # AFTER the machine finished its cycle — dead time that
                # was invisible while twins-era inference dominated the
                # feed window and became the long pole when the embedding
                # cut think time to ~40ms. One thread preserves log order
                # and monotonic counters; the bounded queue backpressures
                # the loop if the disk falls behind; jobs are enqueued
                # only after DONE, so a jammed case is still never logged
                # (same as inline).
                wq = queue.Queue(maxsize=8)

                def _write_jobs():
                    while True:
                        job = wq.get()
                        if job is None:
                            return
                        try:
                            job()
                        except Exception as e:      # a bad frame or full disk
                            print(f"run writer: {e}", flush=True)  # can't kill the run
                writer = threading.Thread(target=_write_jobs, daemon=True)
                writer.start()

                def note(msg):
                    # timestamped run-event breadcrumbs: when a run flips
                    # to flush mode early, events.log says exactly which
                    # gate reads led there — forensics we lacked when one
                    # entered 76 cases before true end of brass
                    try:
                        with open(run_dir / "events.log", "a") as f:
                            f.write(time.strftime("%H:%M:%S") + " "
                                    + msg + "\n")
                    except OSError:
                        pass

                def sort_reply():
                    """Await a sort/flush ack: DONE, JAM, WAITING (means
                    FLUSH_WAITS consecutive dry lines) or None (silence)."""
                    deadline = time.monotonic() + cfg.serial["sort_timeout_s"]
                    reply, waits = None, 0
                    while time.monotonic() < deadline:
                        reply = transport.readline(
                            timeout=max(deadline - time.monotonic(), 0.1))
                        if reply is None or reply in ("DONE", "JAM"):
                            return reply
                        if reply == "WAITING":
                            waits += 1
                            note(f"gate empty {waits}/{FLUSH_WAITS} (sort ack)")
                            if waits >= FLUSH_WAITS:
                                return reply
                    return reply

                def await_feed(timeout=12.0):
                    """Wait out a forced FEED: SEATED, JAM or None."""
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        line = transport.readline(
                            timeout=max(deadline - time.monotonic(), 0.1))
                        if line in ("SEATED", "JAM", None):
                            return line
                    return None

                while not self.stop_evt.is_set():
                    line = transport.readline(timeout=2.0)
                    if line is None:
                        continue                      # idle tick; re-check stop
                    if line == "EMPTY":
                        break
                    if line == "WAITING":
                        waiting += 1
                        note(f"gate empty {waiting}/{WAIT_LIMIT} (idle)")
                        if waiting >= WAIT_LIMIT:
                            note("run END: out_of_brass (idle waits)")
                            with self.lock:           # dry: end of brass
                                self.state["end_reason"] = "out_of_brass"
                            break
                        continue
                    if line == "JAM":
                        with self.lock:
                            self.state["jams"] += 1
                        transport.send("HOME")
                        prev_slot = 0     # HOME re-primes at the park slot
                        continue
                    if line != "SEATED":
                        continue
                    waiting = 0

                    t_cap = time.monotonic()
                    # 50ms light lead: a rolling-shutter frame that starts
                    # exposing across the LED transition comes out banded
                    # (half-dark wash, case 108 same run) — give the ring a
                    # full frame period to be ON before anything averages
                    transport.send("LIGHT:A"); time.sleep(0.05)
                    frame = camera.capture("A")
                    transport.send("LIGHT:OFF")
                    capture_ms = (time.monotonic() - t_cap) * 1000
                    present = frame is not None and imaging.case_present(frame)
                    if not present:
                        if dry:
                            # the flush walked every straggler through the
                            # camera: one final forced feed replays the
                            # queue's delayed arm move and drops the last
                            # (already counted) case at its slot. Wheel is
                            # now empty.
                            transport.send("FEED")
                            await_feed()
                            with self.lock:
                                self.state["end_reason"] = "out_of_brass"
                                self.state["flushed"] = flushed
                            break
                        if empty_left > 0:
                            # an empty pocket at the camera: the cold-start
                            # walk-in (the first case needs two feeds to
                            # reach the camera from the sensor) or a bubble
                            # left mid-wheel by a collator hiccup. A forced
                            # FEED handles both SAFELY: it executes the
                            # drop-port case's queued arm move (so it falls
                            # in its true slot) and the bubble rides on as
                            # a phantom sort-to-park where nothing falls —
                            # alignment self-heals. Budget-limited so a
                            # truly empty machine still stops.
                            empty_left -= 1
                            transport.send("FEED")
                            continue
                        with self.lock:
                            self.state["end_reason"] = "out_of_brass"
                        break
                    empty_left = EMPTY_FEEDS      # real case: refill budget
                    if dry:
                        dry_present += 1
                        if pipelined and dry_present >= DRY_RESUME:
                            # brass is provably still flowing — reverse the
                            # end-of-brass call. Safe re-entry: every flush
                            # cycle leaves the firmware idle with the
                            # previous case's slot self-queued, which is
                            # exactly the arm target the drop-port case
                            # needs on the next PFEED.
                            dry = False
                            waiting = 0
                            with self.lock:
                                self.state["flushed"] = flushed
                            note(f"brass still flowing ({dry_present} "
                                 f"consecutive flush cases) — false "
                                 f"end-of-brass, resuming full speed "
                                 f"after case {sorted_n}")
                    if pipelined and not dry:
                        # SS2 pipelined cycle: the photo is taken, so the
                        # machine can move — this feed's arm target is
                        # LAST cycle's PSLOT, and this case's slot (sent
                        # as PSLOT below) is first needed at the NEXT
                        # feed. Mechanics and inference overlap; the dry
                        # flush stays sequential (placement over speed at
                        # end of brass).
                        transport.send("PFEED")
                    # the embedding decider is the only brain (twins
                    # retired after ~1,700 head-to-head cases:
                    # zero known-brand misfiles, one-third the rejects)
                    sh = get_shadow()
                    ctr = getattr(camera, "center", None)
                    t1 = time.time()
                    from sorter.embed_classifier import EmbedDecider
                    d = EmbedDecider(load_cfg(), sh).classify(
                        frame, center=ctr, probe=not self.capture)
                    infer_ms = (time.time() - t1) * 1000

                    slot = (self.catch_all if self.capture
                            else self._resolve_slot(d))
                    t_feed = time.monotonic()
                    if pipelined and not dry:
                        transport.send(f"PSLOT:{slot}")
                    else:
                        # in DRY mode every sort is a forced FLUSH — same
                        # arm move a bare sort would make, but the feed
                        # can't wait on the (dry) proximity gate
                        transport.send(f"FLUSH:{prev_slot}:{slot}" if dry
                                       else f"SORT:{slot}")
                    reply = sort_reply()
                    if reply == "WAITING":
                        # a dry feeder answered the bare sort. One or two
                        # waits can be a case still falling from the
                        # collator (the feed then completes on its own,
                        # which cancel_wait catches as "resumed");
                        # FLUSH_WAITS in a row + a clean cancel is end of
                        # brass: reissue this sort forced and enter the
                        # flush loop.
                        outcome = cancel_wait(transport)
                        note(f"cancel_wait -> {outcome} (case {sorted_n})")
                        if outcome == "resumed":
                            reply = "DONE"
                        elif outcome == "clean":
                            dry = True
                            dry_present = 0
                            note(f"DRY/FLUSH MODE entered after case {sorted_n}")
                            transport.send(f"FLUSH:{prev_slot}:{slot}")
                            reply = sort_reply()
                        else:
                            reply = "JAM"
                    if reply != "DONE":
                        if reply == "JAM":
                            with self.lock:
                                self.state["jams"] += 1
                        if dry:
                            # never home-and-refeed blind at end of brass —
                            # remaining stragglers stay for the operator
                            with self.lock:
                                self.state["end_reason"] = "out_of_brass"
                                self.state["flushed"] = flushed
                            break
                        transport.send("HOME")
                        prev_slot = 0     # HOME re-primes at the park slot
                        continue
                    # ack wait = mechanical time NOT hidden under think
                    # time; with ts deltas this decomposes the cycle
                    feed_ms = (time.monotonic() - t_feed) * 1000
                    prev_slot = slot
                    if dry:
                        flushed += 1

                    sorted_n += 1
                    n = sorted_n
                    # "classified" = the model produced a headstamp, even if it
                    # has no bin (no_bin_mapping). Only a genuinely unidentifiable
                    # case is a REJECT worth photographing for review/retraining;
                    # a recognized-but-unbinned case is not. Capture runs are
                    # never rejects either — every case (no_model included)
                    # goes to the catch-all and gets reviewed as a normal card,
                    # not the reject pile.
                    classified = d.reason in ("ok", "no_bin_mapping")
                    is_reject = not classified and not self.capture
                    category = d.stamp if classified else "unmatched"
                    entry = {"n": n, "bin": slot, "category": category,
                             "stamp": d.stamp,
                             "stamp_conf": round(d.stamp_conf, 3),
                             "reason": d.reason, "infer_ms": round(infer_ms, 1),
                             # cycle decomposition: elapsed
                             # since run start + where this case's time
                             # went — ts deltas give the true marginal
                             # rate free of prime/flush overhead
                             "ts": round(time.time() - t0, 2),
                             "capture_ms": round(capture_ms, 1),
                             "feed_ms": round(feed_ms, 1)}
                    if d.extras.get("views"):
                        entry["views"] = d.extras["views"]
                    if d.extras.get("embed"):
                        entry["embed"] = d.extras["embed"]

                    # defaults bind now: frame/ctr belong to THIS capture
                    # (camera.center advances with the next case), and the
                    # writer runs while the machine feeds it in
                    def _persist(n=n, slot=slot, frame=frame, entry=entry,
                                 category=category, classified=classified,
                                 is_reject=is_reject, ctr=ctr):
                        log_f.write(json.dumps(entry) + "\n")
                        log_f.flush()
                        thumb = None
                        if frame is not None:
                            if is_reject:
                                cv2.imwrite(str(rejects_dir / f"{n:04d}_A.png"),
                                            frame)
                                meta_f.write(json.dumps(entry) + "\n")
                                meta_f.flush()
                            else:
                                cv2.imwrite(str(frames_dir / f"{n:04d}_A.png"),
                                            frame)
                            # the operator reads stamps off these — crop to
                            # the head like the Capture page, don't shrink
                            # the whole frame
                            crop = imaging.head_view(frame, center=ctr)
                            thumb = b64_jpg(crop, max_side=200)
                            if classified or self.capture:
                                # rejects keep full frames for review; good
                                # cases (and every capture-run case) get a
                                # small thumb for the review/report grid
                                h, w = crop.shape[:2]
                                sc = 200 / max(h, w, 1)
                                cv2.imwrite(str(thumbs_dir / f"{n:04d}.jpg"),
                                            cv2.resize(crop, (max(int(w * sc), 1),
                                                              max(int(h * sc), 1))))
                        with self.lock:
                            st = self.state
                            st["sorted"] = n
                            st["counts"][category] = st["counts"].get(category, 0) + 1
                            st["bin_counts"][str(slot)] = st["bin_counts"].get(str(slot), 0) + 1
                            if entry.get("stamp"):
                                bsc = st.setdefault("bin_stamp_counts", {})
                                k2 = str(slot)
                                bsc.setdefault(k2, {})
                                bsc[k2][entry["stamp"]] = bsc[k2].get(entry["stamp"], 0) + 1
                            if is_reject:
                                st["rejected"] += 1
                            st["recent"].append({**entry, "thumb": thumb})
                            st["recent"] = st["recent"][-10:]
                            st["rate"] = round(n / max(time.time() - t0, 0.01), 2)
                            self._rebuild_slots()
                    wq.put(_persist)

                # every queued job must land before the log files close
                # and the report/state read the run as finished
                wq.put(None)
                writer.join(timeout=30)

            if params.get("mode") == "serial" and transport is not None:
                # fork firmware telemetry — must happen NOW: the handback
                # below reopens the port, which resets the Uno and wipes
                # the counters. None on stock firmware; that's fine.
                from sorter.cs72 import read_feed_stats
                stats = read_feed_stats(transport)
                if stats:
                    with self.lock:
                        self.state["feed_stats"] = stats

            score = None
            if machine is not None and machine.results:
                correct = misfiled = rejected_known = 0
                for case, bin_id in machine.results:
                    if self.auto_assign:
                        # a case is correct if it landed in the slot its own
                        # headstamp owns; catch-all is a safe reject
                        if bin_id == self.catch_all:
                            rejected_known += 1
                        elif case.stamp in (self.slot_stamp.get(bin_id) or []):
                            correct += 1
                        else:
                            misfiled += 1
                        continue
                    true_bin = cfg.bin_map.get((case.stamp, None))
                    over = getattr(cfg, "overflow_bin", None)
                    if bin_id == true_bin:
                        correct += 1
                    elif bin_id == cfg.unmatched_bin:
                        rejected_known += 1
                    elif (true_bin is None and over is not None
                          and bin_id == over):
                        # a no-bin class routed to OVERFLOW is the designed
                        # behavior — safely set aside, never a misfile
                        rejected_known += 1
                    else:
                        misfiled += 1
                score = {"correct": correct, "rejected": rejected_known,
                         "misfiled": misfiled, "total": len(machine.results)}
            with self.lock:
                if not self.state["end_reason"]:
                    self.state["end_reason"] = \
                        "stopped" if self.stop_evt.is_set() else "complete"
                self.state.update(running=False, finished=True, score=score)
        except Exception as e:
            with self.lock:
                self.state.update(running=False, error=str(e))
        finally:
            if wq is not None:
                wq.put(None)          # crash path: normal runs already
                writer.join(timeout=15)  # drained — second sentinel is a no-op
            try:
                # duration lands next to mode in run.json — every ended run
                # gets one (stopped and crashed included; they sorted cases
                # too), and the report shows "N cases in M m S s"
                rj = run_dir / "run.json"
                meta = json.loads(rj.read_text())
                meta["duration_s"] = round(time.time() - t0, 1)
                rj.write_text(json.dumps(meta))
            except (OSError, ValueError):
                pass
            # every ended run (finished, stopped, crashed) leaves the sorter
            # arm re-homed: a jam late in a run can skew the arm's frame
            # without ever crossing the flag again, and the next run must
            # not inherit that. The feed re-home is a free no-op when the
            # wheel already rests on its tab.
            link = getattr(transport, "link", None)
            if link is not None:
                try:
                    link.write("homesorter\n")
                    link.write("homefeeder:soft\n")  # no on-tab pocket advance
                    time.sleep(0.3)      # let the lines hit the wire pre-close
                except Exception:
                    pass
            for closer in (transport, camera):
                try:
                    if closer:
                        closer.close() if closer is transport else closer.release()
                except Exception:
                    pass
            if params.get("mode") == "serial":
                # hand the board back: auto-connect reopens the port within
                # ~5s and re-pushes the machine settings (the reopen resets
                # the Uno, so the re-push is exactly what we want)
                with _console["lock"]:
                    _console["hold"] = False


run_mgr = RunManager()


@app.post("/api/run/start")
def api_run_start():
    # the run loop and the dataset jobs share ONE TFLite interpreter and
    # one gallery — a run started under any of them either fights for
    # the interpreter (field-hit: a sort started mid-mislabel-scan) or
    # gets its gallery swapped mid-run. Refuse with a reason. (The
    # interpreter itself is also lock-serialized as a backstop.)
    if _scan_status["running"]:
        return jsonify({"error": "the mislabel scan is re-reading the "
                        "dataset — let it finish (or cancel it on the "
                        "Dataset tab), then start the run"}), 409
    if _dup_status["running"]:
        return jsonify({"error": "the duplicate scan is running — let it "
                        "finish (or cancel it on the Dataset tab), then "
                        "start the run"}), 409
    if _gal_build["running"]:
        return jsonify({"error": "the gallery is rebuilding — a run "
                        "started now would have its matching gallery "
                        "swapped mid-run; wait for it to finish"}), 409
    if _groups_busy["n"] > 0:
        return jsonify({"error": "a batch review is clustering its "
                        "unknowns — give it a few seconds, then start "
                        "the run"}), 409
    if train_status.get("running"):
        return jsonify({"error": "training is running on this install — "
                        "wait for it to finish"}), 409
    err = run_mgr.start(request.get_json() or {})
    if err:
        return jsonify({"error": err}), 409
    return jsonify({"ok": True})


@app.post("/api/run/stop")
def api_run_stop():
    run_mgr.stop()
    return jsonify({"ok": True})


@app.get("/api/run/status")
def api_run_status():
    s = run_mgr.status()
    if request.args.get("brief"):        # counter-only pollers skip the
        s = dict(s, recent=[])           # ~200KB of inline thumbnails
    return jsonify(s)


@app.post("/api/run/clear_slots")
def api_run_clear_slots():
    run_mgr.clear_slots()
    return jsonify({"ok": True})


@app.post("/api/run/reset_counters")
def api_run_reset_counters():
    run_mgr.reset_counters()
    return jsonify({"ok": True})


# --------------------------------------------------------- reject review ---
@app.get("/api/runs")
def api_runs():
    runs = []
    runs_dir = ROOT / "runs"
    if runs_dir.is_dir():
        for d in sorted((p for p in runs_dir.iterdir() if p.is_dir()), reverse=True):
            n_rej = len(list((d / "rejects").glob("*_A.png"))) if (d / "rejects").is_dir() else 0
            # frames still on disk = cases awaiting review (resolving a
            # card — file or discard — deletes its frame)
            n_frames = len(list((d / "frames").glob("*_A.png"))) \
                if (d / "frames").is_dir() else 0
            mode = None
            capture = False
            try:
                rj = json.loads((d / "run.json").read_text())
                mode = rj.get("mode")
                capture = bool(rj.get("capture"))
            except (OSError, ValueError):
                pass
            # tally + duration off the log so the run list can describe
            # each run without anyone opening its full report
            total = dur = None
            try:
                lines = (d / "log.jsonl").read_text().splitlines()
                total = len(lines)
                if lines:
                    dur = json.loads(lines[-1]).get("ts")
            except (OSError, ValueError):
                pass
            runs.append({"run": d.name, "rejects": n_rej, "mode": mode,
                         "capture": capture, "cases_left": n_frames + n_rej,
                         "total": total, "dur_s": dur})
    return jsonify({"runs": runs[:20]})


def _run_dir(run_id):
    if any(c in run_id for c in "/\\") or ".." in run_id:
        return None
    d = ROOT / "runs" / run_id
    return d if d.is_dir() else None


@app.get("/api/runs/<run_id>/log")
def api_run_log(run_id):
    """The raw per-case log, every field included — the report and
    rejects endpoints project a fixed field set, which hides extras
    like the shadow-mode verdicts this endpoint exists to expose."""
    d = _run_dir(run_id)
    if d is None:
        return jsonify({"error": "unknown run"}), 404
    log = d / "log.jsonl"
    if not log.is_file():
        return jsonify({"error": "no log"}), 404
    entries = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    return jsonify({"run": run_id, "entries": entries})


@app.get("/api/runs/<run_id>/rejects")
def api_run_rejects(run_id):
    d = _run_dir(run_id)
    if d is None:
        return jsonify({"error": "unknown run"}), 404
    meta = {}
    meta_path = d / "rejects" / "meta.jsonl"
    if meta_path.exists():
        for line in meta_path.read_text().splitlines():
            try:
                e = json.loads(line)
                meta[e["n"]] = e
            except (ValueError, KeyError):
                pass
    items = []
    # thumbs ship as URLs (lazy-loaded client-side), so listing every
    # pending reject costs bytes, not image decodes
    pending = sorted((d / "rejects").glob("*_A.png"))
    for a_path in pending:
        n = int(a_path.name.split("_")[0])
        e = meta.get(n, {})
        items.append({"n": n, "reason": e.get("reason", "?"),
                      "stamp": e.get("stamp"),
                      "conf": e.get("stamp_conf"),
                      "embed": e.get("embed"),
                      "thumb": f"/api/runs/{run_id}/reject_thumb/{n}"})
    return jsonify({"run": run_id, "rejects": items, "total": len(pending)})


@app.get("/api/runs/<run_id>/thumb/<int:n>")
def api_run_thumb(run_id, n):
    """One report thumbnail as a plain image — the report JSON carries
    URLs, the browser lazy-loads only what scrolls into view."""
    d = _run_dir(run_id)
    if d is None:
        return jsonify({"error": "unknown run"}), 404
    tp = d / "thumbs" / f"{n:04d}.jpg"
    if not tp.is_file():
        return jsonify({"error": "no thumb"}), 404
    resp = send_file(tp, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


@app.get("/api/runs/<run_id>/frame/<int:n>")
def api_run_frame(run_id, n):
    """A classified case's saved frame at inspect size — the report
    modal's click-to-enlarge. 404s once filing consumed the frame; the
    client falls back to the stored thumb."""
    d = _run_dir(run_id)
    if d is None:
        return jsonify({"error": "unknown run"}), 404
    a_path = d / "frames" / f"{n:04d}_A.png"
    if not a_path.is_file():
        a_path = d / "rejects" / f"{n:04d}_A.png"
    if not a_path.is_file():
        return jsonify({"error": "frame consumed"}), 404
    img = cv2.imread(str(a_path))
    if img is None:
        return jsonify({"error": "unreadable"}), 404
    img = imaging.head_view(img)
    h, w = img.shape[:2]
    scale = 900 / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (int(w * scale), int(h * scale)))
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        return jsonify({"error": "encode failed"}), 500
    resp = app.response_class(buf.tobytes(), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@app.get("/api/runs/<run_id>/reject_thumb/<int:n>")
def api_run_reject_thumb(run_id, n):
    """A reject's review thumbnail, cropped to the readable head on
    demand. The stored file stays a FULL frame (the resolve flow feeds
    it back into the dataset, which needs raws)."""
    d = _run_dir(run_id)
    if d is None:
        return jsonify({"error": "unknown run"}), 404
    a_path = d / "rejects" / f"{n:04d}_A.png"
    if not a_path.is_file():
        return jsonify({"error": "no reject"}), 404
    img = cv2.imread(str(a_path))
    if img is None:
        return jsonify({"error": "unreadable"}), 404
    img = imaging.head_view(img)
    h, w = img.shape[:2]
    scale = 220 / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (int(w * scale), int(h * scale)))
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        return jsonify({"error": "encode failed"}), 500
    resp = app.response_class(buf.tobytes(), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


@app.get("/api/runs/<run_id>/report")
def api_run_report(run_id):
    """End-of-run report: aggregates + every classified case with its saved
    head-crop thumb. Rejects appear in the counts only — their images live
    in the reject review."""
    d = _run_dir(run_id)
    if d is None:
        return jsonify({"error": "unknown run"}), 404
    log = d / "log.jsonl"
    entries = []
    if log.exists():
        for line in log.read_text().splitlines():
            try:
                entries.append(json.loads(line))
            except ValueError:
                pass
    per_bin, reasons, stamps = {}, {}, {}
    cases = []
    for e in entries:
        b = per_bin.setdefault(e["bin"], {"count": 0, "confs": [], "stamps": {}})
        b["count"] += 1
        b["confs"].append(e.get("stamp_conf", 0))
        s = e.get("stamp") or "?"
        b["stamps"][s] = b["stamps"].get(s, 0) + 1
        reasons[e["reason"]] = reasons.get(e["reason"], 0) + 1
        stamps[s] = stamps.get(s, 0) + 1
        if e["reason"] in ("ok", "no_bin_mapping"):
            # filing a card consumes its saved frame; the thumb stays, so
            # the case remains ON the report — just no longer fileable
            filed = not (d / "frames" / f"{e['n']:04d}_A.png").exists()
            cases.append({"n": e["n"], "bin": e["bin"], "stamp": e.get("stamp"),
                          "conf": e.get("stamp_conf"), "reason": e["reason"],
                          "thumb": None, "filed": filed})
    bins_out = []
    for b, v in sorted(per_bin.items()):
        confs = sorted(v["confs"])
        bins_out.append({"bin": b, "count": v["count"],
                         "median_conf": confs[len(confs) // 2] if confs else 0,
                         "stamps": v["stamps"]})
    meta = {}
    try:
        meta = json.loads((d / "run.json").read_text())
    except (OSError, ValueError):
        pass
    duration = meta.get("duration_s")
    if duration is None and entries and log.exists():
        # runs from before durations were recorded: the folder name is the
        # start, the log's last write is the end — close enough to true
        try:
            start = time.mktime(time.strptime(run_id, "run_%Y%m%d_%H%M%S"))
            duration = round(max(log.stat().st_mtime - start, 0), 1) or None
        except ValueError:
            pass
    # thumbs ship as URLs — no encoding cost per case, so every case
    # ships and the client's pagination decides what actually loads
    thumbs = d / "thumbs"
    for c in cases:
        if (thumbs / f"{c['n']:04d}.jpg").exists():
            c["thumb"] = f"/api/runs/{run_id}/thumb/{c['n']}"
    return jsonify({"run": run_id, "mode": meta.get("mode"),
                    "total": len(entries), "duration_s": duration,
                    "bins": bins_out, "reasons": reasons, "stamps": stamps,
                    "cases": cases})


def _resolve_case_file(a_path, b_path, body):
    """Shared resolve: file a saved run frame into the dataset.

    action "save" files under `stamp`; `create: true` first adds the
    stamp to the class list (one-stop new-group creation — no more
    detouring through the Collect page and hunting the card again).
    stamp "UNKNOWN" files into the profile's data/unknown/ pool — not a
    class, never in galleries or ordinary training: it is the outlier-
    exposure corpus that teaches the model what "belongs to nobody"
    looks like. Any other action discards.
    """
    saved = None
    body = body or {}
    if body.get("action") == "save":
        stamp = body.get("stamp", "").strip().upper()
        if not stamp:
            return None, (jsonify({"error": "stamp required to save"}), 400)
        frame = cv2.imread(str(a_path))
        if stamp == "UNKNOWN":
            udir = DATA_DIR / "unknown"
            udir.mkdir(parents=True, exist_ok=True)
            existing = sorted(udir.glob("*_A.png"))
            idx = int(existing[-1].name.split("_")[0]) + 1 if existing else 0
            cv2.imwrite(str(udir / f"{idx:04d}_A.png"), frame)
            saved = f"UNKNOWN #{idx}"
        else:
            if body.get("create"):
                if not _valid_stamp(stamp):
                    return None, (jsonify(
                        {"error": f"invalid headstamp name {stamp!r}"}), 400)
                with _config_lock:
                    raw = active_model_raw()
                    if stamp not in raw["stamp_labels"]:
                        raw["stamp_labels"].append(stamp)
                        write_active_model(raw)
                load_cfg()
            label, idx = save_labeled(stamp, frame)
            saved = f"{label} #{idx}"
    a_path.unlink()
    if b_path.exists():
        b_path.unlink()
    return saved, None


@app.post("/api/runs/<run_id>/rejects/<int:n>/resolve")
def api_run_reject_resolve(run_id, n):
    d = _run_dir(run_id)
    if d is None:
        return jsonify({"error": "unknown run"}), 404
    a_path = d / "rejects" / f"{n:04d}_A.png"
    if not a_path.exists():
        return jsonify({"error": "already resolved"}), 404
    saved, err = _resolve_case_file(
        a_path, d / "rejects" / f"{n:04d}_B.png", request.get_json())
    if err:
        return err
    return jsonify({"ok": True, "saved": saved})


@app.post("/api/runs/<run_id>/cases/<int:n>/resolve")
def api_run_case_resolve(run_id, n):
    """Reassign/file a CLASSIFIED case from the run report — the escape
    hatch for accepted-but-wrong verdicts, which used to leave no
    fileable evidence behind."""
    d = _run_dir(run_id)
    if d is None:
        return jsonify({"error": "unknown run"}), 404
    a_path = d / "frames" / f"{n:04d}_A.png"
    if not a_path.exists():
        return jsonify({"error": "no saved frame for this case"}), 404
    saved, err = _resolve_case_file(
        a_path, d / "frames" / f"{n:04d}_B.png", request.get_json())
    if err:
        return err
    return jsonify({"ok": True, "saved": saved})


def _case_frame(d, n):
    """A case's saved full frame, wherever the run put it."""
    for sub in ("frames", "rejects"):
        p = d / sub / f"{n:04d}_A.png"
        if p.exists():
            return p
    return None


@app.get("/api/runs/<run_id>/groups")
def api_run_groups(run_id):
    """Grouped review for a capture run (works on any run with frames).

    Confident cases group by their predicted class — one confirm files
    the lot. Everything else is CLUSTERED by embedding similarity, so
    six copies of the same unknown stamp arrive as one nameable group
    instead of six scattered cards.
    """
    d = _run_dir(run_id)
    if d is None:
        return jsonify({"error": "unknown run"}), 404
    log = d / "log.jsonl"
    if not log.is_file():
        return jsonify({"error": "no log"}), 404
    entries = [json.loads(l) for l in log.read_text().splitlines()
               if l.strip()]
    sh = get_shadow()
    confident, murky = {}, []          # class -> cases | [(n, entry, path)]
    for e in entries:
        p = _case_frame(d, e["n"])
        if p is None:
            continue
        if e["reason"] in ("ok", "no_bin_mapping"):
            confident.setdefault(e["stamp"], []).append((e, p))
        else:
            murky.append((e, p))
    def card(e, p):
        # the run loop already saved a 200px head thumb for every
        # classified case — re-deriving it from the full frame took a
        # 300-case review to ~20s of server stall (starved the header
        # poll into a false "disconnected")
        t = d / "thumbs" / f"{e['n']:04d}.jpg"
        if t.is_file():
            thumb = ("data:image/jpeg;base64,"
                     + base64.b64encode(t.read_bytes()).decode())
        else:
            img = cv2.imread(str(p))
            thumb = b64_jpg(imaging.head_view(img), max_side=200)
        out = {"n": e["n"], "stamp": e.get("stamp"),
               "conf": e.get("stamp_conf"), "thumb": thumb}
        # the decider's audit trail (who crowded whom): lets the review
        # card explain a high-% case in the trouble pile — "FC 98% but
        # R-P at 94%, margin too thin" instead of a bare percentage
        em = e.get("embed") or {}
        if em.get("runner"):
            out["runner"] = em["runner"]
            out["runner_sim"] = em.get("runner_sim")
        return out
    counts = _counts("stamp", DATA_DIR)
    def wellfed(c):
        return counts.get(c, 0) >= _WELLFED_MIN_IMAGES
    groups = [{"kind": "class", "label": c, "well_fed": wellfed(c),
               "cases": [card(e, p) for e, p in sorted(cs,
                                                       key=lambda x: x[0]["n"])]}
              for c, cs in sorted(confident.items())]
    # below-the-bar cases with a decent best guess group BY that guess —
    # a pile of low-confidence GFLs is one "looks like GFL" card, not
    # shredded across blind clusters
    suggested, unknown = {}, []
    for e, p in murky:
        if e.get("stamp") and (e.get("stamp_conf") or 0) >= 0.70:
            suggested.setdefault(e["stamp"], []).append((e, p))
        else:
            unknown.append((e, p))
    groups += [{"kind": "suggest", "label": c,
                "cases": [card(e, p) for e, p in sorted(cs,
                                                        key=lambda x: x[0]["n"])]}
               for c, cs in sorted(suggested.items())]
    # cluster the remainder by embedding similarity — join against every
    # member (not just the seed), so chains of look-alikes stay together
    if unknown and sh is not None:
        # per-run vector cache: the first review computes each murky
        # case's embedding once; every later open of the page (and each
        # regroup after filing pulled-out cases) reads it back instead
        # of re-embedding the pile. Keyed to the model so an install
        # invalidates it.
        vp = d / "case_vecs.npz"
        vcache = {}
        if vp.is_file():
            try:
                with np.load(vp, allow_pickle=False) as z:
                    if str(z["model_md5"]) == (sh.model_md5 or ""):
                        vcache = dict(zip(z["ns"].tolist(), z["vecs"]))
            except Exception:      # torn/corrupt cache must never 500 the
                vcache = {}        # review page — it just recomputes
        # every uncached unknown costs an interpreter embed — behind the
        # invoke lock that means STALLING a live run's think time, so a
        # fresh clustering waits its turn instead
        need = [e["n"] for e, _ in unknown if e["n"] not in vcache]
        if need and run_mgr.status().get("running"):
            return jsonify({"error": "the machine is running — this "
                            "review needs to analyze new cases first; "
                            "open it when the run ends"}), 409
        vecs, fresh = [], False
        _groups_busy["n"] += 1
        try:
            for e, p in unknown:
                v = vcache.get(e["n"])
                if v is None:
                    img = cv2.imread(str(p))
                    v = sh.embed(imaging.crop_head(img))
                    vcache[e["n"]] = v
                    fresh = True
                vecs.append(v)
        finally:
            _groups_busy["n"] -= 1
        if fresh:
            try:
                tmp = vp.with_suffix(".tmp.npz")
                np.savez_compressed(
                    tmp, model_md5=sh.model_md5 or "",
                    ns=np.array(list(vcache), dtype=np.int64),
                    vecs=np.stack(list(vcache.values())))
                os.replace(tmp, vp)        # never leave a torn file behind
            except OSError:
                pass                       # cache is an optimization only
        used = [False] * len(unknown)
        for i in range(len(unknown)):
            if used[i]:
                continue
            members = [i]
            used[i] = True
            for j in range(i + 1, len(unknown)):
                if used[j]:
                    continue
                if max(float(np.dot(vecs[m], vecs[j]))
                       for m in members) >= 0.88:
                    members.append(j)
                    used[j] = True
            groups.append({"kind": "cluster",
                           "label": None,
                           "cases": [card(*unknown[k]) for k in members]})
    elif unknown:
        groups.append({"kind": "cluster", "label": None,
                       "cases": [card(e, p) for e, p in unknown]})
    # confident classes, then suggestions, then unnamed clusters;
    # biggest piles first inside each kind
    order = {"class": 0, "suggest": 1, "cluster": 2}
    groups.sort(key=lambda g: (order[g["kind"]], -len(g["cases"])))
    return jsonify({"run": run_id, "groups": groups,
                    "total": sum(len(g["cases"]) for g in groups)})


@app.post("/api/runs/<run_id>/cases/bulk_resolve")
def api_run_bulk_resolve(run_id):
    """File many cases under one stamp in a single call — the group
    confirm. Novelty gate: once a class holds NOVELTY_MIN_IMAGES, a
    photo is skipped only when it is pixel-verified as the SAME physical
    case already filed (re-run brass) — distinct cases always file.
    Intake filter: past _WELLFED_MIN_IMAGES the class needs variety, not
    volume — cases whose class-bank nearest neighbor reads at or above
    the nomination floor are skipped as routine unless `file_all` is
    set (the UI sets it for suggest/cluster/pulled-out filings, which
    are hard examples by definition, and for the explicit override).
    `create` adds the stamp first; stamp "UNKNOWN" -> set-aside pool.
    """
    d = _run_dir(run_id)
    if d is None:
        return jsonify({"error": "unknown run"}), 404
    body = request.get_json() or {}
    ns = body.get("ns") or []
    stamp = (body.get("stamp") or "").strip().upper()
    if body.get("action") == "discard":
        gone = 0
        for n in ns:
            p = _case_frame(d, int(n))
            if p:
                p.unlink()
                gone += 1
        return jsonify({"ok": True, "discarded": gone})
    if not stamp:
        return jsonify({"error": "stamp required"}), 400
    if body.get("create") and stamp != "UNKNOWN":
        if not _valid_stamp(stamp):
            return jsonify({"error": f"invalid headstamp name {stamp!r}"}), 400
        with _config_lock:
            raw = active_model_raw()
            if stamp not in raw["stamp_labels"]:
                raw["stamp_labels"].append(stamp)
                write_active_model(raw)
        load_cfg()
    sh = get_shadow()
    n_imgs = len(list((DATA_DIR / "stamp" / stamp).glob("*.png"))) \
        if (DATA_DIR / "stamp" / stamp).is_dir() else 0
    gate = (sh is not None and stamp != "UNKNOWN"
            and n_imgs >= NOVELTY_MIN_IMAGES)
    intake = (gate and n_imgs >= _WELLFED_MIN_IMAGES
              and not body.get("file_all"))
    kept, skipped, routine = 0, 0, 0
    for n in ns:
        p = _case_frame(d, int(n))
        if p is None:
            continue
        if gate:
            img = cv2.imread(str(p))
            dup, _, nn = _same_case_in_bank(sh, stamp,
                                            imaging.crop_head(img))
            if dup:
                p.unlink()          # this exact case is already filed
                skipped += 1
                continue
            if intake and nn is not None and nn >= _NOVELTY_EMBED:
                p.unlink()          # distinct case, but a look the class
                routine += 1        # already holds in depth
                continue
        saved, err = _resolve_case_file(
            p, p.with_name(p.name.replace("_A", "_B")),
            {"action": "save", "stamp": stamp})
        if err is None and saved:
            kept += 1
    return jsonify({"ok": True, "stamp": stamp, "kept": kept,
                    "skipped_duplicates": skipped,
                    "skipped_routine": routine})


# ------------------------------------------------------------- Pi network ---
# Wi-Fi management for the headless Pi, via NetworkManager (the default on
# Raspberry Pi OS Bookworm+). First-boot provisioning is the Raspberry Pi
# Imager's job (it bakes in SSID/password/hostname/SSH before the first
# boot); this panel is for changing networks later, from a browser over
# ethernet or the current Wi-Fi. On the dev box nmcli simply isn't there
# and the UI says so.
def _nmcli(*args, timeout=20):
    """Run nmcli; returns (ok, text). Never raises. Polkit allows a
    service-context user less than an interactive one — scans and joins
    pass on netdev membership, but hotspot creation (wifi.share.*) came
    back "Not authorized" from the live bench test. Denied calls retry
    once under the passwordless sudo the Pi image grants; if the retry
    fails too, the original polkit error is the one worth reporting."""
    def run(cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout)
        except FileNotFoundError:
            return None, "nmcli not found — Wi-Fi management runs on the Pi"
        except subprocess.TimeoutExpired:
            return None, "nmcli timed out"
        out = r.stdout if r.returncode == 0 else (r.stderr or r.stdout)
        return r.returncode == 0, out.strip()
    ok, out = run(("nmcli", *args))
    if ok is None:
        return False, out
    if not ok and "not authorized" in out.lower():
        ok2, out2 = run(("sudo", "-n", "nmcli", *args))
        if ok2:
            return True, out2
    return ok, out


@app.post("/api/system/restart")
def api_system_restart():
    """Restart the app service or reboot the Pi from the UI — the remedy
    for a wedged camera/USB stack without pulling power. Machine-only
    (Linux + systemd + passwordless sudo, as pi_deploy sets up); refused
    mid-run and mid-training so a restart can't eat a hopper run."""
    import shutil
    what = (request.get_json() or {}).get("what")
    if what not in ("app", "pi", "off"):
        return jsonify({"error": "what must be 'app', 'pi' or 'off'"}), 400
    if not (sys.platform.startswith("linux") and shutil.which("systemctl")):
        return jsonify({"error": "restart only works on the machine itself"}), 400
    if run_mgr.state.get("running"):
        return jsonify({"error": "a sorting run is active — stop it first"}), 409
    if train_status.get("running"):
        return jsonify({"error": "training is running — wait for it to finish"}), 409
    cmd = (("sudo", "-n", "systemctl", "restart", "sortiq") if what == "app"
           else ("sudo", "-n", "reboot") if what == "pi"
           else ("sudo", "-n", "poweroff"))
    # reply first, act a beat later — the browser needs the 200 to start
    # its reconnect countdown before this process dies
    threading.Timer(0.8, lambda: subprocess.Popen(cmd)).start()
    return jsonify({"ok": True, "restarting": what,
                    "eta_s": 12 if what == "app" else 55 if what == "pi" else 0})


_net_cache = {"t": 0.0, "resp": None}


@app.get("/api/network/status")
def api_network_status():
    # nmcli spawns are slow enough to stack up behind heavier requests
    # and paint a false "offline" in the header — one probe per 10s
    if _net_cache["resp"] is not None and time.time() - _net_cache["t"] < 10:
        return jsonify(_net_cache["resp"])
    ok, out = _nmcli("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device")
    if not ok:
        _net_cache.update(t=time.time(),
                          resp={"available": False, "reason": out})
        return jsonify(_net_cache["resp"])
    devices = []
    for line in out.splitlines():
        p = line.split(":")
        if len(p) >= 4 and p[1] in ("ethernet", "wifi"):
            devices.append({"device": p[0], "type": p[1], "state": p[2],
                            "connection": p[3] or None})
    for d in devices:
        ok2, out2 = _nmcli("-t", "-f", "IP4.ADDRESS", "device", "show", d["device"])
        d["ip"] = (out2.split(":", 1)[1].split("/")[0]
                   if ok2 and ":" in out2 else None)
    _net_cache.update(t=time.time(),
                      resp={"available": True, "devices": devices,
                            "hotspot": (_ap_ssid() if _ap["active"]
                                        else None)})
    return jsonify(_net_cache["resp"])


@app.get("/api/network/scan")
def api_network_scan():
    ok, out = _nmcli("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY",
                     "device", "wifi", "list", "--rescan", "yes", timeout=30)
    if not ok:
        return jsonify({"error": out}), 503
    seen = {}
    for line in out.splitlines():
        p = line.split(":")
        if len(p) < 4 or not p[1]:
            continue                       # hidden SSIDs
        e = {"ssid": p[1], "in_use": p[0] == "*",
             "signal": int(p[2] or 0), "security": p[3] or "open"}
        if p[1] not in seen or e["signal"] > seen[p[1]]["signal"]:
            seen[p[1]] = e                 # strongest AP per SSID
    ok2, out2 = _nmcli("-t", "-f", "NAME,TYPE", "connection", "show")
    saved = [l.rsplit(":", 1)[0] for l in out2.splitlines()
             if ok2 and l.endswith(":802-11-wireless")]
    return jsonify({"networks": sorted(seen.values(), key=lambda n: -n["signal"]),
                    "saved": saved})


@app.post("/api/network/connect")
def api_network_connect():
    body = request.get_json() or {}
    ssid = (body.get("ssid") or "").strip()
    password = body.get("password") or ""
    if not ssid:
        return jsonify({"error": "ssid is required"}), 400
    # one radio: an active fallback hotspot must stand down for the join —
    # and stand back up if the join fails, or a bad password strands the box
    was_ap = _ap["active"]
    if was_ap:
        _ap_down()
        # the radio just left AP mode with an empty beacon cache — give a
        # rescan a moment, or the connect below can't infer the target's
        # security type (bench-test failure: "key-mgmt property is missing")
        _nmcli("device", "wifi", "rescan")
        time.sleep(4)

    def join():
        # a saved profile for this SSID (Imager/netplan-provisioned
        # included) beats creating a duplicate: update its secret if a
        # new one was typed, then bring it up by name
        okc, outc = _nmcli("-t", "-f", "NAME,TYPE", "connection", "show")
        for line in (outc.splitlines() if okc else []):
            name = line.rsplit(":", 1)[0]
            if not line.endswith(":802-11-wireless"):
                continue
            oks, outs = _nmcli("-g", "802-11-wireless.ssid",
                               "connection", "show", name)
            if not (oks and outs == ssid):
                continue
            if password:
                _nmcli("connection", "modify", name,
                       "wifi-sec.key-mgmt", "wpa-psk",
                       "wifi-sec.psk", password)
            return _nmcli("connection", "up", name, timeout=60)
        args = ["device", "wifi", "connect", ssid]
        if password:
            args += ["password", password]
        ok1, out1 = _nmcli(*args, timeout=60)   # DHCP can take a while
        if not ok1 and "key-mgmt" in out1 and password:
            # target still unseen (fresh out of AP mode): the half-made
            # profile is broken — replace it with an explicit WPA-PSK one
            _nmcli("connection", "delete", "id", ssid)
            oka, outa = _nmcli("connection", "add", "type", "wifi",
                               "con-name", ssid, "ssid", ssid,
                               "wifi-sec.key-mgmt", "wpa-psk",
                               "wifi-sec.psk", password)
            if oka:
                return _nmcli("connection", "up", ssid, timeout=60)
        return ok1, out1

    ok, out = join()
    if not ok:
        if was_ap:
            _ap_up()
        return jsonify({"error": out}), 400
    return jsonify({"ok": True, "detail": out})


@app.post("/api/network/forget")
def api_network_forget():
    name = ((request.get_json() or {}).get("ssid") or "").strip()
    if not name:
        return jsonify({"error": "ssid is required"}), 400
    ok, out = _nmcli("connection", "delete", "id", name)
    if not ok:
        return jsonify({"error": out}), 400
    return jsonify({"ok": True})


# ---- AP-mode fallback: a Pi that can't find any known network raises its
# own. New garage, moved router, password mistyped at imaging time —
# without this the headless box is unreachable and the fix is pulling the
# SD card. A watchdog (machine only: Linux + nmcli) waits out the boot
# autoconnect window, and if nothing has an IP it starts a WPA2 hotspot
# named after the hostname; the operator joins it, browses to
# http://10.42.0.1:5000, and uses the Wi-Fi panel to put the box on a
# real network. The join tears the hotspot down first (one radio) and
# raises it again if the join fails — a bad password must not strand the
# box twice.
AP_CON = "sortiq-ap"
AP_PASSWORD = "sortbrass"       # WPA2 wants 8+; in the docs next to the SSID
_ap = {"active": False}


def _ap_ssid():
    import socket
    return f"SortIQ-{socket.gethostname()}"


def _net_has_link():
    """True if any ethernet/wifi device is connected to something that
    isn't our own hotspot."""
    ok, out = _nmcli("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device")
    if not ok:
        return True          # can't tell — never raise an AP on a guess
    for line in out.splitlines():
        p = line.split(":")
        if (len(p) >= 4 and p[1] in ("ethernet", "wifi")
                and p[2] == "connected" and p[3] != AP_CON):
            return True
    return False


def _ap_up():
    # a stale profile from an earlier hotspot (interrupted teardown,
    # reboot mid-AP) blocks creation under the same name — clear it
    # first so bring-up is idempotent (bench-test failure: the AP never
    # rose again after a reboot left sortiq-ap on disk)
    _nmcli("connection", "delete", AP_CON)
    ok, out = _nmcli("device", "wifi", "hotspot", "con-name", AP_CON,
                     "ssid", _ap_ssid(), "password", AP_PASSWORD,
                     timeout=30)
    _ap["active"] = ok
    _net_cache["resp"] = None            # header state changed right now
    print(f"AP fallback: hotspot {_ap_ssid()} "
          f"{'up' if ok else 'FAILED: ' + out}", flush=True)
    return ok


def _ap_down():
    if not _ap["active"]:
        return
    _nmcli("connection", "down", AP_CON)
    _nmcli("connection", "delete", AP_CON)   # or autoconnect resurrects it
    _ap["active"] = False
    _net_cache["resp"] = None


def _ap_watchdog():
    time.sleep(75)                   # NetworkManager's own autoconnect window
    misses = 0
    while True:
        if _net_has_link():
            misses = 0
            _ap_down()               # a cable showed up: real network wins
        elif not _ap["active"]:
            misses += 1
            if misses >= 2:          # two sightings 30s apart — not a blip
                _ap_up()
        time.sleep(30)


# --------------------------------------------------------------- console ---
_console = {"transport": None, "log": [], "stop": None, "mode": None,
            "hold": False, "lock": threading.Lock()}

# Monotonic id per log entry. Replies are matched by id, never by list
# index: the log is trimmed to its last 300 entries as it grows, and an
# index recorded before a trim points past the entries that shifted down
# (a request would then wait out its timeout while the board's reply sat
# in the log, which is exactly what broke feeding once a long collection
# session crossed 300 console lines).
_console_seq = itertools.count(1)


def _console_log_append(dir_, line):
    """Append a console log entry (caller holds the lock). Returns its id."""
    n = next(_console_seq)
    _console["log"].append({"n": n, "t": time.strftime("%H:%M:%S"),
                            "dir": dir_, "line": line})
    _console["log"] = _console["log"][-300:]
    return n


def _console_disconnect_locked():
    if _console["stop"]:
        _console["stop"].set()
    if _console["transport"]:
        try:
            _console["transport"].close()
        except Exception:
            pass
    _console.update(transport=None, stop=None, mode=None)


def _console_connect(mode, port=None):
    """Open the console link. Shared by the endpoint and auto-connect.
    Raises on failure (port missing, busy, ...)."""
    from sorter.cs72 import (RawCs72Console, FakeCs72Link,
                             SerialCs72Link, FW_BAUD)
    # An active run OWNS the board: it adopted the console's link, and
    # opening the port a second time pulses DTR — rebooting the Uno
    # mid-run (settings, queue state and wheel position gone, mid-motion).
    # Refuse every console connect until the run ends; auto-connect
    # retries on its own a few seconds after the handback.
    if run_mgr.state.get("running"):
        raise RuntimeError("a run owns the board — stop the run first")
    if mode == "serial" and _power_get() is False:
        raise RuntimeError("board power is off — press the Power button first")
    with _console["lock"]:
        _console_disconnect_locked()
        _console["log"] = []
        if mode == "serial":
            port = port or load_cfg().serial["port"]
            transport = RawCs72Console(SerialCs72Link(port, FW_BAUD))
        else:
            transport = RawCs72Console(FakeCs72Link())
        _console.update(transport=transport, mode=mode, hold=False,
                        port=(port if mode == "serial" else None))
        stop = threading.Event()
        _console["stop"] = stop

        def reader():
            while not stop.is_set():
                try:
                    line = transport.readline(timeout=0.5)
                except Exception:
                    break                     # the port itself died
                if line:
                    with _console["lock"]:
                        _console_log_append("<", line)
            # ZOMBIE FIX: a USB re-enumeration kills the port under us. If
            # we're still the active transport, mark the console properly
            # disconnected so the UI tells the truth and auto-connect can
            # bring the board back the moment it reappears.
            if not stop.is_set():
                with _console["lock"]:
                    if _console["transport"] is transport:
                        _console_log_append("<", "[link lost — port died]")
                        _console_disconnect_locked()
        threading.Thread(target=reader, daemon=True).start()

    if mode == "serial":
        # the Uno auto-resets when the port opens; wait for its Ready so
        # pushed settings don't land in the bootloader void
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            if any(l["dir"] == "<" and l["line"].strip() == "Ready"
                   for l in list(_console["log"])):
                break
            time.sleep(0.2)
    # firmware auto-detect: the version reply says which world we're in
    # (stock "7.2.x" vs the fork's "-SS" suffix) — recorded so the Machine
    # page shows only the knobs this board actually has
    ver = _console_request("version", lambda l: "7.2" in l, timeout=3.0)
    if ver:
        # SS1 is retired (every fork board runs SS2 now): a pre-SS2 fork
        # version is treated as stock — its extra knobs stay hidden, and
        # stock handling is safe on it (setters answer "ok", no pf/ps).
        save_machine_settings({"firmware": "pico" if "-PICO" in ver
                               else "ss2" if "-SS2" in ver
                               else "stock",
                               "board": "SKR Pico" if "-PICO" in ver
                               else "CS7.2"})
    # push the saved motor settings on connect, if the user asked to
    if machine_settings().get("init_on_startup"):
        _apply_machine_settings(machine_settings())


@app.post("/api/console/connect")
def api_console_connect():
    body = request.get_json() or {}
    mode = body.get("mode", "sim")
    try:
        _console_connect(mode, body.get("port"))
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "mode": mode})


def _auto_connect_loop():
    """Appliance behavior: if a real board is on the configured port and the
    console isn't connected (fresh boot, USB drop, board power-cycle),
    connect automatically — unless the user explicitly disconnected."""
    while True:
        time.sleep(5)
        try:
            if _console["transport"] is not None or _console["hold"]:
                continue
            serial_cfg = load_cfg().serial
            if not serial_cfg.get("auto_connect", True):
                continue
            port = serial_cfg.get("port") or ""
            if not port.startswith("/dev/") or not os.path.exists(port):
                continue
            # the Pi-header UART exists whether or not the board has power:
            # with the relay pin readable, a known-off board is not a target
            if _power_get() is False:
                continue
            _console_connect("serial", port)
        except Exception:
            pass                              # board mid-boot etc.: retry later


threading.Thread(target=_auto_connect_loop, daemon=True).start()


def _frame_watchdog():
    """Reopen a WEDGED camera: device open, no error, frames just stopped
    (seen twice on the Sunplus bridge — a died pump or a silently stalled
    UVC stream). The pump's own failure path catches reads that FAIL;
    this catches reads that never happen."""
    while True:
        time.sleep(5)
        try:
            with _camera["lock"]:
                if _camera["cap"] is None:
                    continue
                alive_t = max(_camera["frame_t"], _camera.get("opened_t", 0.0))
                if time.monotonic() - alive_t > 12:
                    _camera["error"] = "camera wedged — reopening"
                    open_camera_locked()
        except Exception:
            pass


threading.Thread(target=_frame_watchdog, daemon=True).start()


@app.post("/api/console/send")
def api_console_send():
    line = (request.get_json() or {}).get("line", "").strip()
    with _console["lock"]:
        if _console["transport"] is None:
            return jsonify({"error": "not connected"}), 409
        if line:
            _console["transport"].send(line)
            _console_log_append(">", line)
    return jsonify({"ok": True})


# ---- sorter board power: a GPIO drives an IoT Relay (normally-off outlet
# feeding the 24V PSU). GPIO 25 = physical pin 22, its neighbor pin 20 is
# ground — the control pair the relay wants. State is read back from the
# pin itself, so the button can never lie about what the relay sees.
POWER_GPIO = 25
_power_cache = {"t": 0.0, "on": None}


def _power_init():
    if not machine_settings().get("power_relay"):
        return
    """At app start: if the pin has never been driven (still an input),
    pull it firmly low so the relay can't float on. If it's already an
    output, leave it ALONE — an app restart mid-session must never cut
    board power."""
    if not sys.platform.startswith("linux"):
        return
    tool = _power_tool()
    if tool is None:
        return
    try:
        r = subprocess.run((tool, "get", str(POWER_GPIO)),
                           capture_output=True, text=True, timeout=3)
        out = r.stdout.lower()
        if ("op" in out) or ("func=output" in out):
            return                      # someone (us, earlier) owns it — hands off
        subprocess.run((tool, "set", str(POWER_GPIO), "ip", "pd"),
                       capture_output=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _power_tool():
    import shutil
    for t in ("pinctrl", "raspi-gpio"):
        if shutil.which(t):
            return t
    return None


def _power_get(fresh=False):
    if not machine_settings().get("power_relay"):
        return None
    if not sys.platform.startswith("linux"):
        return None
    now = time.monotonic()
    if not fresh and now - _power_cache["t"] < 2.0:
        return _power_cache["on"]
    tool = _power_tool()
    if tool is None:
        return None
    try:
        r = subprocess.run((tool, "get", str(POWER_GPIO)),
                           capture_output=True, text=True, timeout=3)
        out = r.stdout.lower()
        on = ("hi" in out) or ("level=1" in out)
    except (OSError, subprocess.TimeoutExpired):
        on = None
    _power_cache.update(t=now, on=on)
    return on


@app.get("/api/power")
def api_power_get():
    return jsonify({"on": _power_get(fresh=True), "gpio": POWER_GPIO})


@app.post("/api/power")
def api_power_post():
    on = bool((request.get_json() or {}).get("on"))
    if not machine_settings().get("power_relay"):
        return jsonify({"error": "no power relay configured on this machine"}), 400
    tool = _power_tool()
    if not (sys.platform.startswith("linux") and tool):
        return jsonify({"error": "power control only works on the machine"}), 400
    if not on:
        if run_mgr.state.get("running"):
            return jsonify({"error": "a sorting run is active — stop it first"}), 409
        if train_status.get("running"):
            return jsonify({"error": "training is running — wait for it"}), 409
        # drop the serial link cleanly; auto-connect resumes when power returns
        try:
            with _console["lock"]:
                _console_disconnect_locked()
        except Exception:
            pass
    args = ((tool, "set", str(POWER_GPIO), "op", "dh" if on else "dl")
            if tool == "pinctrl"
            else (tool, "set", str(POWER_GPIO), "op", "dh" if on else "dl"))
    try:
        subprocess.run(args, capture_output=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired) as e:
        return jsonify({"error": f"gpio set failed: {e}"}), 500
    _power_cache.update(t=0.0, on=None)      # next poll reads the real pin
    return jsonify({"ok": True, "on": _power_get(fresh=True)})


@app.get("/api/console/log")
def api_console_log():
    return jsonify({"connected": _console["transport"] is not None,
                    "mode": _console["mode"], "log": _console["log"][-200:],
                    "port": _console.get("port"),
                    "board": machine_settings().get("board") or "",
                    "power": _power_get()})


@app.post("/api/console/disconnect")
def api_console_disconnect():
    with _console["lock"]:
        _console_disconnect_locked()
        _console["hold"] = True    # explicit user intent: no auto-reconnect
    return jsonify({"ok": True})


# --------------------------------------------------------- machine settings ---
# The CS7.2 firmware has no persistence (no EEPROM), so the Pi is the source of
# truth: settings live in config.json and are pushed to the board with the
# firmware's setter commands, optionally re-applied on every connect.
# defaults mirror a stock CS7.2 board (read via getconfig);
# motor_standby is idle SECONDS before power-down, 0 = stock (disabled)
MACHINE_DEFAULTS = {"feed_speed": 94, "feed_steps": 60, "sort_speed": 94,
                    "sort_steps": 20, "feed_current": 1000, "sort_current": 1000,
                    "feed_homing_offset": 5, "sort_homing_offset": 0,
                    "slot_drop_delay": 300, "notification_delay": 120,
                    # AirDrop mod (stock firmware feature, both firmwares):
                    # an air blast on pin 12 ejects brass at the drop port.
                    # Default OFF — the upstream app defaults it ON, which
                    # taxes every unmodded machine. Timing defaults mirror
                    # the firmware defines; post_delay REPLACES
                    # slot_drop_delay while the mod is on.
                    "air_drop": False, "air_drop_pre_delay": 30,
                    "air_drop_signal_ms": 100, "air_drop_post_delay": 100,
                    "motor_standby": 0, "camera_led": 200,
                    "led_color": "#ffffff",   # WS2812 ring mix (SKR Pico only)
                    "slots_total": 8, "slots_enabled": None,   # None = all
                    # per-slot physical capacity in cases; None/0 entries =
                    # uncalibrated (fill bars fall back to relative widths)
                    "bin_sizes": None,
                    # per-slot badge color (hex from the fixed palette;
                    # None/"" = brass default). Machine-scoped like
                    # bin_sizes: it describes the tray on the table
                    "bin_colors": None,
                    "init_on_startup": False,
                    # SortIQ firmware fork (7.2.250925.6.1-SS1) knobs.
                    # Stock firmware answers "ok" to the setters and ignores
                    # them, so pushing these is harmless on either firmware.
                    # Defaults mirror the fork's baked-in boot values.
                    "board": "",           # "CS7.2" | "SKR Pico" — from the version line on connect
                    "power_relay": False,  # an IoT Relay on POWER_GPIO switches the board's
                                           # mains — dev/Pico builds only; hides the UI when off
                    "firmware": "stock",   # "stock" | "ss2" (SS1 retired);
                                           # auto-detected from the version
                                           # reply on connect
                    "sort_accel": 1200, "sort_home_backoff": 160,
                    "sort_decel": 1200, "feed_accel": 1200,
                    "feed_decel_rate": 1200,
                    "stall_guard": True, "feed_stall_threshold": 40,
                    "sort_home_slow": 1400, "feed_launch": 48,
                    "feed_decel": True,
                    # extra hold before every arm move (fork v6.2+): brass-
                    # clearance for slow tumblers in the arm tube. Additive
                    # and cycle-independent — unlike slot_drop_delay, whose
                    # remainder logic is a no-op below the app's natural
                    # arm-move spacing (~820ms in sorting). Zero-travel
                    # moves skip it, so batch capture pays nothing.
                    "arm_dwell": 0,
                    "slot_positions": None}   # None = the firmware's default
                                              # grid (i * sort_steps * 16)
_MACHINE_BOOLS = ("init_on_startup", "feed_decel", "air_drop", "stall_guard", "power_relay")
MAX_SLOTS = 12                # matches the fork's slot table size

# the six user-pickable bin-badge colors. Fixed on purpose: every entry
# keeps dark badge text readable, and red/blue never appear — they stay
# the reserved meanings (UNMATCHED / OVERFLOW)
BIN_PALETTE = ("#e8d44d", "#4cc46a", "#a78bfa",
               "#f08c3a", "#3fc8c8", "#e879b9")

# (min, max) per numeric setting — values outside are CLAMPED on save, so
# no amount of Machine-tab experimentation can push the board somewhere
# harmful. The firmware clamps some of these too, but its current ceiling
# (1800 mA) is generous enough to cook a StepStick driver run continuously;
# the app's ceilings are the ones a user can actually reach. Ranges cover
# every sensible bench value with margin (the bench-tuned defaults sit
# comfortably inside).
MACHINE_BOUNDS = {
    "feed_speed": (1, 100),          # firmware maps 1-100 to step delay
    "sort_speed": (1, 100),
    "feed_steps": (30, 200),         # official guide: 70/80; ours: 60
    "sort_steps": (5, 50),           # 8-slot disc = 20
    "feed_current": (300, 1200),     # mA; CS7.2 StepSticks top out at 1.2A —
    "sort_current": (300, 1200),     # the Pico's onboard drivers get 1.6A (below)
    "feed_homing_offset": (0, 30),   # full steps past the sensor edge
    "sort_homing_offset": (0, 20),
    "slot_drop_delay": (0, 3000),    # ms
    "notification_delay": (0, 1000), # ms
    "motor_standby": (0, 3600),      # s, 0 = off
    "camera_led": (0, 255),
    "slots_total": (1, MAX_SLOTS),   # the firmware slot table's size
    "sort_accel": (100, 5000),       # µs start/stop delay
    "sort_decel": (100, 5000),       # µs landing-end delay (Pico)
    "feed_accel": (100, 5000),       # µs launch start delay (Pico)
    "feed_decel_rate": (100, 5000),  # µs stop-shaping slow end (Pico)
    "feed_stall_threshold": (0, 255),
    "sort_home_backoff": (0, 200),   # µsteps
    "sort_home_slow": (100, 5000),   # µs/µstep
    "feed_launch": (0, 200),         # µsteps
    "arm_dwell": (0, 1000),          # ms, matches the firmware clamp
    # AirDrop timings — the firmware setters have NO clamps (raw toInt),
    # so these bounds are the only guard
    "air_drop_pre_delay": (0, 500),  # ms before the blast fires
    "air_drop_signal_ms": (0, 500),  # blast length
    "air_drop_post_delay": (0, 3000),  # replaces slot_drop_delay when on
}
SLOTPOS_MAX = 4000  # µsteps; > a full revolution past the fork grid's end


def _clamp_machine(key, value):
    lo, hi = MACHINE_BOUNDS.get(key, (None, None))
    if lo is None:
        return int(value)
    return min(max(int(value), lo), hi)
_SETTER_CMD = {"feed_speed": "feedspeed", "feed_steps": "feedsteps",
               "sort_speed": "sortspeed", "sort_steps": "sortsteps",
               "feed_current": "feedmotorcurrent", "sort_current": "sortmotorcurrent",
               "feed_homing_offset": "feedhomingoffset",
               "sort_homing_offset": "sorthomingoffset",
               "slot_drop_delay": "slotdropdelay",
               "notification_delay": "notificationdelay",
               "motor_standby": "automotorstandbytimeout",
               "camera_led": "cameraledlevel",
               "sort_accel": "sortaccel",
               "sort_decel": "sortdecel",
               "feed_accel": "feedaccel",
               "feed_decel_rate": "feeddec",
               "stall_guard": "sg",
               "feed_stall_threshold": "sgfeed",
               "sort_home_backoff": "sorthomebackoff",
               "sort_home_slow": "sorthomeslow",
               "feed_launch": "feedlaunch",
               "feed_decel": "feeddecel",
               "arm_dwell": "armdwell",
               "air_drop": "airdropenabled",
               "air_drop_pre_delay": "airdroppredelay",
               # sic: the firmware's command name carries a typo — match it
               "air_drop_signal_ms": "airdropdsignalduration",
               "air_drop_post_delay": "airdroppostdelay"}
_GETCONFIG_KEY = {"feed_speed": "FeedMotorSpeed", "feed_steps": "FeedCycleSteps",
                  "sort_speed": "SortMotorSpeed", "sort_steps": "SortSteps",
                  "feed_current": "FeedMotorCurrent", "sort_current": "SortMotorCurrent",
                  "feed_homing_offset": "FeedHomingOffset",
                  "sort_homing_offset": "SortHomingOffset",
                  "slot_drop_delay": "SlotDropDelay",
                  "notification_delay": "NotificationDelay",
                  "motor_standby": "AutoMotorStandbyTimeout",
                  "camera_led": "CameraLEDLevel",
                  "sort_accel": "SortAccelFactor",
                  "sort_decel": "SortDecelFactor",
                  "feed_accel": "FeedAccelFactor",
                  "feed_decel_rate": "FeedDecelFactor",
                  "stall_guard": "StallGuardEnabled",
                  "feed_stall_threshold": "FeedStallThreshold",
                  "sort_home_backoff": "SortHomeBackoff",
                  "sort_home_slow": "SortHomeSlowDelay",
                  "feed_launch": "FeedLaunchSteps",
                  "feed_decel": "FeedDecelOverOffset",
                  "arm_dwell": "ArmDwellMs",
                  "air_drop": "AirDropEnabled",
                  "air_drop_pre_delay": "AirDropPreDelay",
                  "air_drop_signal_ms": "AirDropSignalTime",
                  "air_drop_post_delay": "AirDropPostDelay"}


def effective_slot_positions(m):
    """The slot table as the board should have it: the firmware default
    grid (i * sort_steps * 16 microsteps) with any saved overrides on top."""
    grid = [i * int(m["sort_steps"]) * 16 for i in range(MAX_SLOTS)]
    for i, v in enumerate((m.get("slot_positions") or [])[:MAX_SLOTS]):
        if v is not None:
            grid[i] = int(v)
    return grid


def machine_settings():
    m = {**MACHINE_DEFAULTS, **json.loads(CONFIG_PATH.read_text()).get("machine", {})}
    if not m.get("slots_enabled"):
        m["slots_enabled"] = list(range(int(m["slots_total"])))
    return m


def save_machine_settings(update):
    with _config_lock:
        raw = json.loads(CONFIG_PATH.read_text())
        m = {**MACHINE_DEFAULTS, **raw.get("machine", {})}
        for k in MACHINE_DEFAULTS:
            if k not in update:
                continue
            if k == "firmware":
                m[k] = (update[k] if update[k] in ("stock", "ss2", "pico")
                        else "stock")
            elif k == "board":
                m[k] = str(update[k] or "")[:40]
            elif k == "led_color":
                c = str(update[k] or "").strip().lstrip("#").lower()
                m[k] = "#" + c if len(c) == 6 and all(
                    ch in "0123456789abcdef" for ch in c) else "#ffffff"
            elif k in _MACHINE_BOOLS:
                m[k] = bool(update[k])
            elif k == "slots_enabled":
                m[k] = sorted({int(s) for s in (update[k] or [])})
            elif k in ("feed_current", "sort_current"):
                lo, hi = MACHINE_BOUNDS[k]
                if m.get("firmware") == "pico" or update.get("firmware") == "pico":
                    hi = 1600            # onboard TMC2209s, actively cooled
                m[k] = min(max(int(update[k]), lo), hi)
            elif k == "slot_positions":
                m[k] = ([min(max(int(v), 0), SLOTPOS_MAX)
                         for v in update[k]][:MAX_SLOTS]
                        if update[k] else None)
            elif k == "bin_sizes":
                # per-slot capacity in cases; garbage entries clear the
                # slot rather than 500 the save, 100k caps the fantasy
                def _cap(v):
                    try:
                        return min(max(int(v or 0), 0), 100_000) or None
                    except (TypeError, ValueError):
                        return None
                m[k] = ([_cap(v) for v in update[k]][:MAX_SLOTS]
                        if isinstance(update[k], list) else None)
            elif k == "bin_colors":
                # only the fixed palette is legal — red/blue stay the
                # reserved meanings and never arrive here from the UI
                m[k] = ([(v if v in BIN_PALETTE else None)
                         for v in update[k]][:MAX_SLOTS]
                        if isinstance(update[k], list) else None)
            else:
                m[k] = _clamp_machine(k, update[k])
        # slots sanity: enabled slots must exist; at least one must remain
        total = max(int(m["slots_total"]), 1)
        m["slots_total"] = total
        if m.get("slots_enabled"):
            m["slots_enabled"] = [s for s in m["slots_enabled"] if 0 <= s < total]
        if not m.get("slots_enabled"):
            m["slots_enabled"] = list(range(total))
        raw["machine"] = m
        write_cfg_raw(raw)
    return m


def _console_request(line, predicate, timeout=3.0):
    """Send a raw line over the console connection and wait for the next
    received line that satisfies `predicate` (or None on timeout)."""
    with _console["lock"]:
        if _console["transport"] is None:
            return None
        _console["transport"].send(line)
        sent = _console_log_append(">", line)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for e in list(_console["log"]):
            if e["n"] > sent and e["dir"] == "<" and predicate(e["line"]):
                return e["line"]
        time.sleep(0.05)
    return None


def _apply_machine_settings(settings):
    applied = 0
    fw = str(machine_settings().get("firmware", "stock"))
    pico_only = {"sort_decel", "feed_accel", "feed_decel_rate",
                 "stall_guard", "feed_stall_threshold"}
    fork_only = {"sort_accel", "sort_home_backoff", "sort_home_slow",
                 "feed_launch", "feed_decel", "arm_dwell", "slot_positions"}
    on_stock = not (fw.startswith("ss") or fw == "pico")
    for key, cmd in _SETTER_CMD.items():
        if on_stock and key in fork_only:
            continue
        if fw != "pico" and key in pico_only:
            continue
        if key in settings and settings[key] is not None and _console_request(
                f"{cmd}:{int(settings[key])}", lambda l: l.strip() == "ok", timeout=2.0):
            applied += 1
    # the ring color is a Pico-only command; the fork board has no RGB
    if settings.get("led_color") and             machine_settings().get("board") == "SKR Pico":
        c = str(settings["led_color"]).lstrip("#")
        try:
            r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            r = g = b = 255
        if _console_request(f"ledcolor:{r},{g},{b}",
                            lambda l: l.strip() == "ok", timeout=2.0):
            applied += 1
    # the per-slot table goes LAST: the firmware's sortsteps setter (pushed
    # above) refills the whole table, clobbering any earlier slotpos
    if settings.get("slot_positions") and not on_stock:
        for i, v in enumerate(settings["slot_positions"][:MAX_SLOTS]):
            if v is not None and _console_request(
                    f"slotpos:{i}:{int(v)}", lambda l: l.strip() == "ok",
                    timeout=2.0):
                applied += 1
    return applied


@app.get("/api/machine/settings")
def api_machine_settings_get():
    return jsonify({"settings": machine_settings(),
                    "defaults": MACHINE_DEFAULTS,
                    "bounds": MACHINE_BOUNDS,
                    "connected": _console["transport"] is not None,
                    "mode": _console["mode"]})


def _sanitize_bins_for_slots(enabled):
    """Keep the active model's bin map consistent with the enabled slots:
    stamps on now-disabled slots lose their bin, and UNMATCHED relocates to
    the first enabled slot if its slot was disabled. Returns what moved."""
    from sorter.config import normalize_bin
    en = set(enabled)
    cleared, moved = [], False
    with _config_lock:
        raw = active_model_raw()
        bins = [normalize_bin(b) for b in (raw.get("bins") or [])]
        for i, g in enumerate(bins):
            # OVERFLOW is a routing token, not a class: a disabled slot
            # still sheds it, but it never shows up as an "unbinned" stamp
            stamps = [s for s in g if s not in ("UNMATCHED", "OVERFLOW")]
            if i not in en and (stamps or "OVERFLOW" in g):
                bins[i] = ["UNMATCHED"] if "UNMATCHED" in g else []
                cleared.extend(stamps)
        um = next((i for i, g in enumerate(bins) if "UNMATCHED" in g), None)
        if um is None or um not in en:
            if um is not None:
                bins[um] = [s for s in bins[um] if s != "UNMATCHED"]
            tgt = enabled[0]
            bins += [[] for _ in range(tgt + 1 - len(bins))]
            cleared.extend(s for s in bins[tgt]
                           if s not in ("UNMATCHED", "OVERFLOW"))
            bins[tgt] = ["UNMATCHED"]
            moved = True
        if cleared or moved:
            raw["bins"] = bins
            write_active_model(raw)
    return cleared, moved


@app.post("/api/machine/settings")
def api_machine_settings_post():
    body = request.get_json() or {}
    if body.get("preview"):
        # push to the board WITHOUT persisting — the camera page's live
        # LED tuning; the saved value comes back on Save/Revert/reconnect
        push = {}
        if body.get("camera_led") is not None:
            push["camera_led"] = min(max(int(body["camera_led"]), 0), 255)
        if body.get("led_color") is not None:
            push["led_color"] = str(body["led_color"])
        if not push:
            return jsonify({"error":
                            "preview supports camera_led / led_color"}), 400
        applied = (_apply_machine_settings(push)
                   if _console["transport"] is not None else 0)
        return jsonify({"ok": True, "preview": True, "applied": applied})
    m = save_machine_settings(body)
    cleared, moved = [], False
    if "slots_enabled" in body or "slots_total" in body:
        cleared, moved = _sanitize_bins_for_slots(m["slots_enabled"])
    # push only what changed — a slider drag shouldn't re-send every setter
    push = {k: m[k] for k in body if k in _SETTER_CMD}
    if "led_color" in body:
        push["led_color"] = m["led_color"]
    if "air_drop" in body:
        # the mod needs ~30ms of notification delay (brass starts falling
        # before the blast); without the mod that delay is pure dead time
        # (measured: -120ms/feed). Auto-manage only between the
        # two automatic values so a hand-tuned figure is never clobbered.
        want = 30 if m["air_drop"] else 0
        if m["notification_delay"] in (0, 30) and m["notification_delay"] != want:
            m = save_machine_settings({"notification_delay": want})
            push["notification_delay"] = want
    if "slot_positions" in body or "sort_steps" in body:
        # sortsteps refills the firmware's whole table; re-push overrides
        push["slot_positions"] = m.get("slot_positions")
        if push["slot_positions"] and "sort_steps" in body:
            push.setdefault("sort_steps", m["sort_steps"])
    applied = _apply_machine_settings(push) if _console["transport"] is not None else 0
    return jsonify({"ok": True, "settings": m, "applied": applied,
                    "bins_cleared": cleared, "unmatched_moved": moved})


@app.post("/api/machine/slotpos")
def api_machine_slotpos():
    """Calibration jog: set one slot's table position (µsteps from home),
    persist it, push it, and re-seat the arm on the slot so the operator
    SEES the new position. The re-seat bounces via slot 0 because the
    firmware computes moves from the current table — an arm already parked
    on the slot would otherwise not move at all."""
    body = request.get_json() or {}
    m = machine_settings()
    slot = int(body.get("slot", -1))
    limit = min(int(m["slots_total"]), MAX_SLOTS)
    if not (0 <= slot < limit):
        return jsonify({"error": f"slot must be 0..{limit - 1}"}), 400
    grid = effective_slot_positions(m)
    try:
        pos = int(body["position"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "position (µsteps) required"}), 400
    grid[slot] = max(0, min(pos, SLOTPOS_MAX))
    m = save_machine_settings({"slot_positions": grid})
    pushed = seated = False
    if _console["transport"] is not None:
        pushed = bool(_console_request(f"slotpos:{slot}:{grid[slot]}",
                                       lambda l: l.strip() == "ok", timeout=2.0))
        if pushed and body.get("seat"):
            _console_request("sortto:0", lambda l: l.strip() == "ok", timeout=6.0)
            seated = bool(_console_request(f"sortto:{slot}",
                                           lambda l: l.strip() == "ok", timeout=8.0))
    return jsonify({"ok": True, "slot": slot, "position": grid[slot],
                    "slot_positions": m["slot_positions"],
                    "pushed": pushed, "seated": seated})


@app.post("/api/machine/slotpos/reset")
def api_machine_slotpos_reset():
    """Drop all per-slot overrides back to the sort_steps grid and push."""
    m = save_machine_settings({"slot_positions": None})
    applied = 0
    if _console["transport"] is not None:
        # re-pushing sortsteps refills the firmware table to the grid
        applied = _apply_machine_settings({"sort_steps": m["sort_steps"]})
    return jsonify({"ok": True, "slot_positions": None, "applied": applied})


@app.post("/api/machine/read")
def api_machine_read():
    """Read the board's live config (getconfig) mapped to our setting keys."""
    if _console["transport"] is None:
        return jsonify({"error": "connect to the machine first"}), 409
    line = _console_request("getconfig", lambda l: l.strip().startswith("{"), timeout=3.0)
    if not line:
        return jsonify({"error": "no config reply from the board"}), 504
    try:
        board = json.loads(line)
    except ValueError:
        return jsonify({"error": "unreadable config reply"}), 502
    out = {k: board[j] for k, j in _GETCONFIG_KEY.items() if j in board}
    if isinstance(board.get("SlotPositions"), str):     # fork firmware only
        try:
            out["slot_positions"] = [int(v) for v in
                                     board["SlotPositions"].split(",")]
        except ValueError:
            pass
    return jsonify({"ok": True, "board": out})


@app.post("/api/machine/test")
def api_machine_test():
    """Test Feed (xf:) or Test Sort to a slot (sortto:)."""
    if _console["transport"] is None:
        return jsonify({"error": "connect to the machine first"}), 409
    body = request.get_json() or {}
    if body.get("action") == "feed":
        # xf:N sorts the current case to slot N and feeds the next, so a
        # test feed can keep dropping cases at the operator's selected
        # slot instead of snapping the arm back to 0 on every press
        slot = int(body.get("slot", 0))
        r = _console_request(f"xf:{slot}", lambda l: l.strip() in
                             ("done", "error:feed overtravel detected"),
                             timeout=25.0)
        return jsonify({"ok": True, "result": r or "timeout"})
    if body.get("action") == "sort":
        slot = int(body.get("slot", 0))
        r = _console_request(f"sortto:{slot}", lambda l: l.strip() == "ok", timeout=5.0)
        return jsonify({"ok": True, "result": r or "timeout"})
    return jsonify({"error": "action must be feed or sort"}), 400


@app.post("/api/test")
def api_test():
    # same shared-interpreter rule as the run/scan guards: a classify
    # here during a live run or scan would collide with their invokes
    if run_mgr.status().get("running"):
        return jsonify({"error": "a sorting run is active — it owns the "
                        "recognizer; test after it ends"}), 409
    if _scan_status["running"] or _dup_status["running"]:
        return jsonify({"error": "a dataset scan is running — it shares "
                        "the recognizer; test after it finishes"}), 409
    if request.form.get("synthetic"):
        cfg = load_cfg()
        spec = synth.CaseSpec.random(cfg)
        frame = synth.render(spec, "A", seed=random.randrange(1 << 30))
        truth = {"stamp": spec.stamp}
    else:
        frame = decode_upload(request.files.get("frameA") or request.form.get("frameA_b64"))
        truth = None
        if frame is None:
            return jsonify({"error": "an image is required"}), 400

    # the Test page exercises the one brain that sorts: the embedding
    sh = get_shadow()
    if sh is None:
        return jsonify({"error": "no embedding model installed "
                        "(shadow_embed.tflite + shadow_gallery.npz)"}), 409
    from sorter.embed_classifier import EmbedDecider
    d = EmbedDecider(load_cfg(), sh).classify(frame, explain=True)
    return jsonify({
        "bin": d.bin_id, "category": d.category, "stamp": d.stamp,
        "stamp_conf": round(d.stamp_conf, 4), "reason": d.reason,
        "extras": d.extras, "truth": truth,
        "decider": "embedding",
        "crop_head": b64_jpg(imaging.crop_head(frame)),
        "frame_a": b64_jpg(frame),
    })


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--host", default="0.0.0.0")  # reachable on the LAN, like the Pi will be
    args = ap.parse_args()
    # probe the GPU sandbox once per boot so the Train page can show its
    # state up front (trainer PCs only — the Pi has no WSL)
    if _gpu_supported():
        gpu_status["supported"] = True
        threading.Thread(target=_gpu_probe, daemon=True).start()
    # AP-mode fallback watchdog (machine only — needs nmcli): a box that
    # can't find any known network raises its own so it stays reachable
    import shutil as _sh_main
    if sys.platform.startswith("linux") and _sh_main.which("nmcli"):
        threading.Thread(target=_ap_watchdog, daemon=True).start()
    _power_init()   # relay pin: firmly low on first boot, untouched after
    # warm the code-digest cache off the critical path: the first digest
    # after a restart hashes the whole tree (seconds from a Pi's SD),
    # and that cold hit used to land on whoever asked first — a fleet
    # probe's timeout budget would blow and gray the card as unreachable
    threading.Thread(target=lambda: codesync.digest(ROOT),
                     daemon=True).start()
    # dataset location now follows the active caliber/model (see calibers/)
    # bind-retry: after a code-update self-restart the outgoing process can
    # hold the port for a beat — ride it out instead of dying silently
    for attempt in range(10):
        try:
            app.run(host=args.host, port=args.port, threaded=True)
            break
        except OSError as e:
            if attempt == 9:
                raise
            print(f"port {args.port} busy ({e}); retrying")
            time.sleep(1.5)
