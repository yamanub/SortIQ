"""Image-quality gate and region-of-interest crops."""
import cv2
import numpy as np

# Crop parameters. Config() calls configure() on load, so every entry point
# (server, CLI tools) shares the values from config.json.
# pocket_frac is the boundary circle at the primer's edge: outside it the
# donut crop keeps the lettering ring, inside it is blacked out so primer
# state can't leak into headstamp identity (primer_only models invert this).
POCKET_FRAC = 0.42     # boundary radius as a fraction of the detected head radius
POCKET_CIRCLE = True   # circular mask on the pocket crop
HEAD_DONUT = True      # black out the pocket in the donut representation
HEAD_MARGIN = 1.08     # head crop half-size as a fraction of head radius
# Per-model crop mode:
#   "normal"      headstamp reader — donut ring; pocket_frac is the inner
#                 edge, i.e. how much of the center (primer) is excluded.
#   "primer_only" round primer crop for verifying primer seating/
#                 orientation; pocket_frac is how much of the primer to show.
CROP_MODE = "normal"
# Per-model look controls, applied to every finished crop so capture,
# training, and sorting always see identical processing. Exists because
# some cameras (the project's OV3660 module) ignore their UVC exposure/
# gain controls entirely — the look is created here instead:
#   CLAHE - local contrast equalization STRENGTH (0 = off). Makes stamped
#           lettering pop with dark borders; strong values also amplify
#           glare artifacts on shiny nickel, so it's a per-model dial —
#           deep clean stamps tolerate 3-5, glare-prone brass wants 1-2.
CLAHE = 0.0
ENHANCE = "none"       # "none" | "clahe" | "blackhat" | "divnorm"
ENHANCE_SIZE = 13      # blackhat kernel px / divnorm blur scale
# Per-model trim on the DETECTED rim radius. Lighting can bias the circle
# fit consistently a hair large or small; the operator aligns the drawn
# ring onto the physical rim once and every crop inherits the correction.
RIM_SCALE = 1.0


def _clahe_strength(v):
    """Config value -> clip limit; tolerates the older boolean form."""
    if isinstance(v, bool):
        return 3.0 if v else 0.0
    return max(0.0, min(float(v or 0), 6.0))


def configure(imaging_cfg):
    # polar + gray imaging are retired: the polar strip was the old
    # twin's geometry, gray benched worse for the distilled student —
    # stale keys in old model.json files are simply ignored here
    global POCKET_FRAC, POCKET_CIRCLE, HEAD_DONUT, CROP_MODE, CLAHE
    global ENHANCE, ENHANCE_SIZE
    POCKET_FRAC = float(imaging_cfg.get("pocket_frac", POCKET_FRAC))
    POCKET_CIRCLE = bool(imaging_cfg.get("pocket_circle", POCKET_CIRCLE))
    HEAD_DONUT = bool(imaging_cfg.get("head_donut", HEAD_DONUT))
    CROP_MODE = imaging_cfg.get("crop_mode", "normal")
    CLAHE = _clahe_strength(imaging_cfg.get("clahe", 0))
    ENHANCE = _enhance_mode(imaging_cfg.get("enhance"), CLAHE)
    ENHANCE_SIZE = _enhance_size(imaging_cfg.get("enhance_size", 13))
    global RIM_SCALE
    RIM_SCALE = min(max(float(imaging_cfg.get("rim_scale", 1.0)), 0.8), 1.2)


def _rim(r, rim_scale=None):
    s = RIM_SCALE if rim_scale is None else min(max(float(rim_scale), 0.8), 1.2)
    return max(int(r * s), 8)


def _enhance(img, clahe=None, enhance=None, enhance_size=None):
    """Apply the configured look to a finished crop (3-channel either way).

    enhance picks the contrast treatment:
      "clahe"    - local histogram equalization (strength = the clahe dial).
                   Rescues dim/flat imaging but amplifies noise with it.
      "blackhat" - morphological black-hat: extracts dark stamped features
                   narrower than enhance_size px and erases the background
                   (glare gradients included). Output = lettering bright on
                   black; surface/plating cues are deliberately discarded.
      "divnorm"  - divide by a blurred copy (sigma = enhance_size / 3):
                   flattens illumination gently, mild on noise.
      "none"     - plain crop.
    Legacy configs without an "enhance" key keep meaning what they meant:
    clahe > 0 behaves as "clahe", otherwise "none".

    All parameters override the process globals — training must be able to
    build crops fully self-described, because the globals get reconfigured
    by every web request (a real concurrency race)."""
    clahe_v = CLAHE if clahe is None else _clahe_strength(clahe)
    mode = ENHANCE if enhance is None else _enhance_mode(enhance, clahe_v)
    size = ENHANCE_SIZE if enhance_size is None else _enhance_size(enhance_size)
    if mode == "clahe" and clahe_v > 0:
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        l = cv2.createCLAHE(clipLimit=clahe_v, tileGridSize=(8, 8)).apply(l)
        img = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)
    elif mode == "blackhat":
        g = cv2.medianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 3)
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
        bh = cv2.morphologyEx(g, cv2.MORPH_BLACKHAT, k)
        bh = cv2.normalize(bh, None, 0, 255, cv2.NORM_MINMAX)
        return cv2.cvtColor(bh, cv2.COLOR_GRAY2BGR)      # inherently gray
    elif mode == "divnorm":
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype("float32") + 1.0
        bg = cv2.GaussianBlur(g, (0, 0), max(size / 3.0, 3.0))
        dn = cv2.convertScaleAbs(g / bg * 128.0)
        return cv2.cvtColor(dn, cv2.COLOR_GRAY2BGR)      # inherently gray
    return img


def _enhance_mode(value, clahe_v):
    v = str(value or "").lower()
    if v in ("clahe", "blackhat", "divnorm", "none"):
        return v
    return "clahe" if clahe_v > 0 else "none"    # legacy configs


def _enhance_size(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 13
    n = min(max(n, 3), 41)
    return n if n % 2 else n + 1                 # morphology wants odd


def sharpness(img):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return cv2.Laplacian(g, cv2.CV_64F).var()


def _head_contour(img):
    """The bright round case-sized contour, or None if the scene lacks one.

    This is the primary rim detector, and its None doubles as the
    empty-nest signal: an unlit empty nest has no bright round blob, so a
    force-feed that skipped a slot is detectable before anything is saved.
    """
    h, w = img.shape[:2]
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    g = cv2.GaussianBlur(g, (5, 5), 0)

    _, bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    contours, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    r_min, r_max = min(h, w) / 8, min(h, w) / 2 * 1.05
    for c in contours:
        (x, y), r = cv2.minEnclosingCircle(c)
        if not r_min <= r <= r_max:
            continue
        circularity = cv2.contourArea(c) / (np.pi * r * r + 1e-9)
        if circularity < 0.55:          # hands, glare streaks, desk edges
            continue
        if best is None or r > best[2]:
            best = (int(x), int(y), int(r))
    return best


CASE_MIN_DISC_BRIGHTNESS = 40    # empty-nest phantom discs read ~12; the
                                 # darkest real case in a 250-frame sweep
                                 # read 66 (median 114) — 40 splits both
                                 # ways with margin


def case_present(img):
    """True when a case-head-sized bright disc is in the scene.

    Otsu is relative, so a sharply focused EMPTY nest can still yield a
    round contour (the pocket itself) — which produced blank phantom
    captures at run start and end. A real case head is lit metal: the
    disc must also be absolutely bright, not just the brightest thing
    in a dark scene. find_head stays permissive on purpose — cropping a
    real case must never get stricter."""
    best = _head_contour(img)
    if best is None:
        return False
    x, y, r = best
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = np.zeros(g.shape, np.uint8)
    cv2.circle(mask, (x, y), max(int(r * 0.8), 1), 255, -1)
    return float(cv2.mean(g, mask)[0]) >= CASE_MIN_DISC_BRIGHTNESS


def find_head(img):
    """Locate the case head -> (cx, cy, r).

    Primary method: the case rim is the strongest high-contrast edge in the
    scene and cases are round, so threshold the bright case against the
    background, take the roundest large contour, and fit its minimum
    enclosing circle — that circle IS the outer rim, and it is far more
    stable frame-to-frame than Hough voting. Hough remains as a fallback,
    then the image center, so a miss degrades instead of crashing.
    """
    h, w = img.shape[:2]
    best = _head_contour(img)
    if best:
        return best

    # Hough fallback on a small copy: at full 1080p this search costs ~26 s
    # on a Pi 4 (the whole live preview stalls whenever the pocket is empty);
    # at 360 px tall it is well under a second and the rim is still found
    # to within a couple of pixels once scaled back.
    scale = min(1.0, 360.0 / h)
    small = cv2.resize(img, (int(w * scale), int(h * scale))) if scale < 1.0 else img
    sh = small.shape[0]
    g2 = cv2.medianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), 5)
    circles = cv2.HoughCircles(g2, cv2.HOUGH_GRADIENT, dp=2, minDist=sh,
                               param1=120, param2=40,
                               minRadius=sh // 6, maxRadius=sh // 2)
    if circles is not None:
        x, y, r = circles[0][0]
        return int(x / scale), int(y / scale), int(r / scale)
    return w // 2, h // 2, min(h, w) // 2 - 4


def crop_head(img, donut=None, pocket_frac=None, center=None,
              crop_mode=None, rim_scale=None, clahe=None,
              enhance=None, enhance_size=None):
    """Case-head crop — the headstamp model's input.

    Normal mode: CS7-style donut ring with background and primer pocket
    blacked out (pocket_frac sets the inner boundary). Field-proven over
    the alternatives — the polar-strip unwrap retired with the softmax
    twins whose second geometry it was.

    primer_only mode: the round primer alone, ring masked away.
    """
    do = HEAD_DONUT if donut is None else bool(donut)
    frac = POCKET_FRAC if pocket_frac is None else float(pocket_frac)
    mode = CROP_MODE if crop_mode is None else crop_mode
    cx, cy, r = center if center is not None else find_head(img)
    r = _rim(r, rim_scale)
    # primer-verification model: the round primer only, ring masked away
    if mode == "primer_only":
        return _enhance(crop_pocket(img, pocket_frac=frac, circle=True,
                                    center=(cx, cy, r)), clahe,
                        enhance, enhance_size)
    crop = _square(img, cx, cy, int(r * HEAD_MARGIN))
    if not do:
        return _enhance(crop, clahe, enhance, enhance_size)
    h, w = crop.shape[:2]
    outer = min(h, w) // 2
    inner = max(int(outer * frac / HEAD_MARGIN), 4)
    mask = np.zeros((h, w), np.uint8)
    cv2.circle(mask, (w // 2, h // 2), outer, 255, -1, cv2.LINE_AA)
    cv2.circle(mask, (w // 2, h // 2), inner, 0, -1, cv2.LINE_AA)
    out = crop.copy()
    out[mask == 0] = 0
    return _enhance(out, clahe, enhance, enhance_size)


def head_view(img, center=None, rim_scale=None):
    """Plain square crop of the case head for the capture UI — the same
    framing as mask_indicator but with nothing drawn or dimmed, so the
    operator can read the stamp. Display only, never a model input (no
    enhancement, no masking)."""
    cx, cy, r = center if center is not None else find_head(img)
    r = _rim(r, rim_scale)
    return _square(img, cx, cy, int(r * HEAD_MARGIN))


def crop_pocket(img, pocket_frac=None, circle=None, center=None):
    """Tight crop around the primer pocket — primer_only models' input.

    pocket_frac: crop radius as a fraction of head radius — covers the
    primer, crimp ring/stakes, and a little surrounding brass. circle=True
    masks the square's corners to black so only the circular pocket region
    remains. Defaults come from config.json via configure(). center may
    carry a precomputed (cx, cy, r) so both crops share one detection.
    """
    frac = POCKET_FRAC if pocket_frac is None else float(pocket_frac)
    circ = POCKET_CIRCLE if circle is None else bool(circle)
    cx, cy, r = center if center is not None else find_head(img)
    crop = _square(img, cx, cy, max(int(r * frac), 24))
    return _circle_mask(crop) if circ else crop


def _circle_mask(crop):
    h, w = crop.shape[:2]
    mask = np.zeros((h, w), np.uint8)
    cv2.circle(mask, (w // 2, h // 2), min(h, w) // 2, 255, -1, cv2.LINE_AA)
    out = crop.copy()
    out[mask == 0] = 0
    return out


def _square(img, cx, cy, half):
    h, w = img.shape[:2]
    x0, x1 = max(cx - half, 0), min(cx + half, w)
    y0, y1 = max(cy - half, 0), min(cy + half, h)
    crop = img[y0:y1, x0:x1]
    if crop.size == 0:
        return img
    return crop


def mask_indicator(img, pocket_frac=None, crop_mode=None, center=None,
                   rim_scale=None):
    """The round head with a circle at the primer boundary, for the capture UI.

    normal: the inner (primer) region is dimmed — that's what the donut
    crop blacks out. primer_only: the inner region is kept bright
    and the ring dimmed — that's what the model will see.
    """
    frac = POCKET_FRAC if pocket_frac is None else float(pocket_frac)
    mode = CROP_MODE if crop_mode is None else crop_mode
    cx, cy, r = center if center is not None else find_head(img)
    r = _rim(r, rim_scale)
    crop = _square(img, cx, cy, int(r * HEAD_MARGIN)).copy()
    h, w = crop.shape[:2]
    ctr = (w // 2, h // 2)
    rr = int(min(h, w) // 2 * frac / HEAD_MARGIN)      # boundary radius in px
    inside = np.zeros((h, w), np.uint8)
    cv2.circle(inside, ctr, rr, 255, -1, cv2.LINE_AA)
    dim = (crop * 0.35).astype(np.uint8)
    if mode == "primer_only":
        out = np.where(inside[..., None] > 0, crop, dim)   # keep the primer
        col = (0, 220, 0)
    else:
        out = np.where(inside[..., None] > 0, dim, crop)   # exclude the primer
        col = (60, 180, 255)
    out = np.ascontiguousarray(out)
    cv2.circle(out, ctr, rr, col, 2, cv2.LINE_AA)
    if mode != "primer_only":
        # the crop's outer boundary — the tuning target:
        # adjust rim_scale until this ring hugs the physical case rim
        orr = int(min(h, w) // 2 / HEAD_MARGIN)
        cv2.circle(out, ctr, orr, (0, 220, 0), 2, cv2.LINE_AA)
    return out
