"""Multi-caliber model profiles.

The sorter handles one caliber per run. Each *caliber* (cartridge) holds one
or more *models* — e.g. a proven "Default" and an "Experimental" you're
testing — and every model is fully self-contained (its own dataset, crop
settings, bin map, and trained weights). That isolation is deliberate: an
experimental model often means different images (new camera settings, a
re-shoot), so models must not share data.

On disk:

    calibers/<cartridge>/<model>/
        model.json                       labels, bins, floors, imaging (crop mode)
        data/raw/<LABEL>/*.png           this model's captured frames
        data/stamp/<LABEL>/*.png         crops regenerated from raw/ per crop mode
        models/stamp.tflite + _labels.json + archive/

The app-level config.json keeps only GLOBAL settings (camera, serial, model
input size) plus an `active` pointer {cartridge, model}. Switching models is
just repointing `active` and reloading — nothing caliber-specific lives there.
"""
import json
import shutil
from pathlib import Path

CALIBERS = "calibers"

DEFAULT_IMAGING = {"pocket_frac": 0.42, "pocket_circle": True, "head_donut": True,
                   "crop_mode": "normal", "clahe": 0, "rim_scale": 1.0}
# stamp_confidence = the Sort page's Acceptance offset (0.85 neutral);
# sharpness = the photo-quality gate. (top2_margin retired with the twins —
# the embedding's runner-up margin lives in the gallery, not the floors.)
# donut_sharpness: the blur gate, scored on the case-head donut at model
# input scale (imaging.donut_sharpness). Calibrated on real frames: they
# ran 934+ (two machines: 71 and 450 frames); a blur sweep read
# 23/24 at ~400 and 5/24 at ~80, with ZERO misreads in 336 degraded reads —
# the model fails safe, so the floor only drops hopeless frames early.
# "sharpness" is the retired whole-frame floor, kept so model.json
# round-trips; nothing gates on it.
DEFAULT_FLOORS = {"stamp_confidence": 0.85, "sharpness": 20.0,
                  "donut_sharpness": 250.0}

# crop_mode -> imaging flags. "normal" = full head ring; "hide_primer" =
# black out the primer pocket (donut); "primer_only" would crop the pocket.
CROP_MODES = ("normal", "hide_primer", "primer_only")


def safe_name(name):
    """Reject anything that could escape the calibers/ tree or break a path."""
    if not name or any(c in name for c in '/\\:<>"|?*') or ".." in name:
        raise ValueError(f"invalid name {name!r}")
    return name


def calibers_root(root):
    return Path(root) / CALIBERS


def model_dir(root, cartridge, model):
    return calibers_root(root) / safe_name(cartridge) / safe_name(model)


def list_profiles(root):
    """{cartridge: [model, ...]} discovered on disk (a model has a model.json)."""
    out = {}
    cr = calibers_root(root)
    if cr.is_dir():
        for cart in sorted(p for p in cr.iterdir() if p.is_dir()):
            models = sorted(m.name for m in cart.iterdir()
                            if m.is_dir() and (m / "model.json").exists())
            if models:
                out[cart.name] = models
    return out


def read_model(root, cartridge, model):
    return json.loads((model_dir(root, cartridge, model) / "model.json").read_text())


def write_model(root, cartridge, model, data):
    d = model_dir(root, cartridge, model)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "model.json.tmp"
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(d / "model.json")


def _blank_model(cartridge, name):
    return {"cartridge": cartridge, "name": name, "stamp_labels": [],
            "bins": ["UNMATCHED"], "bin_count": 8,
            "floors": dict(DEFAULT_FLOORS), "imaging": dict(DEFAULT_IMAGING)}


def create_model(root, cartridge, name, seed_from=None, imaging=None):
    """Create a new model. seed_from=(cartridge, model) copies that model's
    raw images and settings as a starting point (an isolated copy — the two
    models then diverge). Returns the new model.json dict."""
    cartridge, name = safe_name(cartridge), safe_name(name)
    d = model_dir(root, cartridge, name)
    if (d / "model.json").exists():
        raise ValueError(f"model {cartridge}/{name} already exists")
    (d / "data" / "raw").mkdir(parents=True, exist_ok=True)
    (d / "data" / "stamp").mkdir(parents=True, exist_ok=True)
    (d / "models").mkdir(parents=True, exist_ok=True)
    if seed_from:
        sc, sm = seed_from
        mj = read_model(root, sc, sm)
        mj["cartridge"], mj["name"] = cartridge, name
        src_raw = model_dir(root, sc, sm) / "data" / "raw"
        if src_raw.is_dir():
            shutil.copytree(src_raw, d / "data" / "raw", dirs_exist_ok=True)
    else:
        mj = _blank_model(cartridge, name)
    if imaging:
        mj["imaging"] = {**DEFAULT_IMAGING, **mj.get("imaging", {}), **imaging}
    write_model(root, cartridge, name, mj)
    return mj


def delete_model(root, cartridge, model):
    d = model_dir(root, cartridge, model)
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
    # drop the cartridge dir if now empty
    cart = calibers_root(root) / safe_name(cartridge)
    if cart.is_dir() and not any(cart.iterdir()):
        cart.rmdir()


def ensure_migrated(root, config_path):
    """Bring the on-disk layout up to the multi-caliber structure. Idempotent.

    Two things, each a no-op once done:
      1. A legacy flat config.json (stamp_labels/bins/etc at the top level) is
         converted to global-settings-only + an `active` pointer, and its model
         fields move into calibers/9mm/Default/model.json.
      2. Any leftover top-level models/ or data/real (e.g. on a machine that
         pulled the committed layout but still has its own data) is absorbed
         into the active model — so nobody's dataset gets orphaned.
    Returns the (global) config dict.
    """
    root = Path(root)
    config_path = Path(config_path)
    cfg = json.loads(config_path.read_text()) if config_path.exists() else {}

    if "stamp_labels" in cfg and not cfg.get("active"):
        cfg = _migrate_flat_config(root, config_path, cfg)

    if cfg.get("active"):
        a = cfg["active"]
        if (model_dir(root, a["cartridge"], a["model"]) / "model.json").exists():
            _absorb_legacy_dirs(root, a["cartridge"], a["model"])
    return cfg


def _migrate_flat_config(root, config_path, cfg):
    cart, name = "9mm", "Default"
    d = model_dir(root, cart, name)
    (d / "data").mkdir(parents=True, exist_ok=True)
    (d / "models").mkdir(parents=True, exist_ok=True)
    write_model(root, cart, name, {
        "cartridge": cart, "name": name,
        "stamp_labels": cfg.get("stamp_labels", []),
        "bins": cfg.get("bins", ["UNMATCHED"]),
        "bin_count": cfg.get("bin_count", 8),
        "floors": {**DEFAULT_FLOORS, **cfg.get("floors", {})},
        "imaging": {**DEFAULT_IMAGING, **cfg.get("imaging", {})},
    })
    new_cfg = {
        "active": {"cartridge": cart, "model": name},
        "camera": cfg.get("camera", {}),
        "serial": cfg.get("serial", {}),
        "models": {"input_size": cfg.get("models", {}).get("input_size", 160)},
    }
    tmp = config_path.with_name(config_path.name + ".tmp")
    tmp.write_text(json.dumps(new_cfg, indent=2))
    tmp.replace(config_path)
    return new_cfg


def _absorb_legacy_dirs(root, cartridge, model):
    """Move a leftover top-level models/ and data/real into the active model
    (only when the model's own dir is still empty, so nothing is overwritten)."""
    d = model_dir(root, cartridge, model)
    legacy_models = Path(root) / "models"
    dst_models = d / "models"
    if legacy_models.is_dir() and not any(dst_models.glob("*.tflite")):
        dst_models.mkdir(parents=True, exist_ok=True)
        for p in legacy_models.iterdir():
            shutil.move(str(p), str(dst_models / p.name))
        legacy_models.rmdir()
    legacy_data = Path(root) / "data" / "real"
    if legacy_data.is_dir():
        for sub in ("raw", "stamp"):
            s, dst = legacy_data / sub, d / "data" / sub
            if s.is_dir() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(s), str(dst))


def set_active(config_path, cartridge, model):
    """Point config.json's `active` at a different model (validated to exist)."""
    config_path = Path(config_path)
    root = config_path.parent
    read_model(root, cartridge, model)               # raises if missing
    cfg = json.loads(config_path.read_text())
    cfg["active"] = {"cartridge": cartridge, "model": model}
    tmp = config_path.with_name(config_path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(config_path)
    return cfg
