"""Embedding + gallery classifier — the sorting brain.

The trained embedding network (a distilled MobileNetV2) maps a head crop
to a 128-d vector; classification is nearest-neighbor against a gallery
of labeled exemplar vectors, and "none of the above" is a first-class
answer: below the cosine threshold the verdict is a reject, not a forced
pick. New classes are added by adding gallery exemplars — no retraining.

The pair shadow_embed.tflite + shadow_gallery.npz in the profile's
models dir IS the decider (the "shadow" name survives from the campaign
that proved it as a logged second opinion before it became the
decider). See tools/build_gallery.py for the gallery format.
"""
import hashlib
import json
import os
from dataclasses import dataclass, field

import numpy as np

from . import imaging
from .classifier import _load_interpreter


@dataclass
class Decision:
    """One case's verdict: destination bin + a machine-readable reason.
    Every path that can't produce a confident answer routes to the
    unmatched bin, and the reason string is the audit trail the run
    report and reject review are built on."""
    bin_id: int
    stamp: str | None = None
    stamp_conf: float = 0.0
    reason: str = "ok"
    extras: dict = field(default_factory=dict)

    @property
    def category(self):
        return "unmatched" if self.reason != "ok" else self.stamp


class EmbedClassifier:
    """kNN-over-gallery on top of a tflite embedding model."""

    def __init__(self, model_path, gallery_path):
        self.interp = _load_interpreter(model_path)
        self.inp = self.interp.get_input_details()[0]
        self.out = self.interp.get_output_details()[0]
        # ONE interpreter serves every consumer — the run loop, dataset
        # scans, batch-review clustering, the novelty gate, the Test
        # page. Concurrent invokes trip tflite's internal-reference
        # check (field-hit twice), so the invoke is serialized here at
        # the source. Endpoint-level guards still refuse the heavy
        # overlaps for UX; this lock is the correctness backstop for
        # every path they can't cover.
        import threading
        self._invoke_lock = threading.Lock()
        g = np.load(gallery_path, allow_pickle=False)
        vec = g["g_vec"].astype(np.float32)
        self.g_vec = vec / np.linalg.norm(vec, axis=1, keepdims=True)
        self.g_cls = [str(c) for c in g["g_cls"]]
        # exemplar crop paths: lets predict() exclude a query's own
        # gallery seat (leave-one-out) so an image can never vouch for
        # itself during dataset scans
        self.g_path = ([str(p) for p in g["g_path"]]
                       if "g_path" in g else None)
        # full class bank (every crop's vector) — used by the novelty
        # gate for dedupe; matching stays exemplar-only. Absent in
        # older galleries.
        if "g_all_vec" in g:
            av = g["g_all_vec"].astype(np.float32)
            self.all_vec = av / np.linalg.norm(av, axis=1, keepdims=True)
            self.all_cls = [str(c) for c in g["g_all_cls"]]
        else:
            self.all_vec = None
            self.all_cls = None
        # crop paths for the bank (newer galleries): lets the
        # novelty gate pixel-verify "same physical case" instead of
        # trusting cosine, which this embedding saturates within a class
        self.all_path = ([str(p) for p in g["g_all_path"]]
                         if "g_all_path" in g else None)
        # bank reuse plumbing: every crop's vector is in the bank, so
        # dataset scans can skip the ~110ms embed per image when the
        # bank is trustworthy — same model (digest) and unchanged file
        self.all_sig = ([str(s) for s in g["g_all_sig"]]
                        if "g_all_sig" in g else None)
        self.all_idx = ({p: i for i, p in enumerate(self.all_path)}
                        if self.all_path else {})
        self._crops_sig_cache = {}
        self.gallery_mtime = os.path.getmtime(gallery_path)
        try:
            with open(model_path, "rb") as f:
                self.model_md5 = hashlib.md5(f.read()).hexdigest()
        except OSError:
            self.model_md5 = None
        self.meta = json.loads(str(g["meta"])) if "meta" in g else {}
        self.bank_reusable = (self.model_md5 is not None
                              and self.model_md5
                              == self.meta.get("model_digest"))
        self.tau = float(self.meta.get("tau", 0.85))
        # near-tie guard: BLAZER/SPEER share a font and differ only in
        # four letters — an untrained-on look-alike can edge the true
        # class by ~0.01-0.03 cosine. Measured over 3 live runs: correct
        # calls carry median margin 0.174, wrong-identity accepts 0.004-
        # 0.05, so a small floor rejects the coin-flips almost for free.
        self.margin_floor = float(self.meta.get("margin_floor", 0.03))
        self.classes = sorted(set(self.g_cls))
        # per-class column mask, computed once: predict() reduces a
        # query's exemplar sims to one best-sim per class
        self._masks = [np.array([c == k for c in self.g_cls])
                       for k in self.classes]
        # per-class acceptance bar from NEAREST-NEIGHBOR cohesion: a
        # query matches its closest exemplar, so the bar for class c is
        # what c's own exemplars score against their nearest sibling.
        # Thin classes whose few exemplars are near-duplicates demand a
        # tighter match (fixes true unknowns accepted as STV/WMA at
        # 0.87, live run 221247); the bar never drops below the global
        # tau (k-center exemplars are deliberately spread — mean
        # pairwise cohesion would HALVE big classes' bars) and never
        # rises more than 0.05 above it (a 3-photo class of one case
        # type must stay reachable by its 4th photo — few-shot is the
        # product).
        self.class_tau = {}
        for k, m in zip(self.classes, self._masks):
            v = self.g_vec[m]
            if len(v) >= 3:
                pair = v @ v.T
                np.fill_diagonal(pair, -1.0)
                nn = pair.max(axis=1)
                bar = float(nn.mean() - nn.std())
                self.class_tau[k] = min(max(self.tau, bar), self.tau + 0.05)
            else:
                self.class_tau[k] = self.tau

    def embed(self, img_bgr):
        import cv2
        h, w = self.inp["shape"][1], self.inp["shape"][2]
        x = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        x = cv2.resize(x, (w, h)).astype(np.float32) / 255.0
        with self._invoke_lock:
            self.interp.set_tensor(self.inp["index"], x[None, ...])
            self.interp.invoke()
            v = self.interp.get_tensor(self.out["index"])[0].astype(np.float32)
        return v / (np.linalg.norm(v) or 1.0)

    def predict(self, img_bgr, exclude_path=None):
        """-> dict(stamp, sim, margin, accept). stamp is the nearest
        gallery class regardless of accept, so disagreement analysis can
        see what a reject WOULD have been called.

        exclude_path (a "CLASS/crop_name.png" gallery path) masks that
        exemplar out of the vote — leave-one-out for dataset scans. The
        k-center picker loves outliers, and a mislabeled image is its
        class's biggest outlier, so mislabels tend to BE exemplars and
        would otherwise self-match at ~1.0 and hide from the scan."""
        return self.predict_vec(self.embed(img_bgr), exclude_path)

    def bank_vec(self, rel_path, crop_file=None):
        """The banked vector for a dataset crop ("CLASS/name.png"), or
        None when it can't be trusted: bank built by a different model,
        path unknown, or the file changed since the gallery was built.
        A miss costs the caller one live embed — never correctness."""
        if not (self.bank_reusable and self.all_vec is not None):
            return None
        i = self.all_idx.get(rel_path)
        if i is None:
            return None
        if crop_file is not None:
            try:
                st = crop_file.stat()
            except OSError:
                return None
            sig = f"{st.st_size}:{int(st.st_mtime)}"
            if not (self.all_sig is not None and self.all_sig[i] == sig):
                # not the build that banked it. A gallery built on
                # another machine (trainer push) is still trusted for
                # crops that PREDATE it — but only when the imaging
                # signature it was built under matches this machine's
                # (an imaging reshape between build and install would
                # otherwise validate vectors of the old-look crops)
                if st.st_mtime > self.gallery_mtime:
                    return None
                want = self.meta.get("crops_sig")
                if not want:
                    return None
                sig_p = crop_file.parent.parent / ".crops_sig"
                cur = self._crops_sig_cache.get(sig_p)
                if cur is None:
                    try:
                        cur = sig_p.read_text()
                        self._crops_sig_cache[sig_p] = cur
                    except OSError:
                        cur = ""   # missing file: DON'T cache the miss —
                        #             it may be seeded while we're loaded
                if cur != want:
                    return None
        return self.all_vec[i]

    def predict_vec(self, q, exclude_path=None):
        """predict() minus the embed — for callers that already hold the
        query vector (dataset scans reading the gallery bank)."""
        sims = self.g_vec @ q
        if exclude_path is not None and self.g_path is not None:
            for k, pth in enumerate(self.g_path):
                if pth == exclude_path:
                    sims[k] = -1.0
        per_cls = np.array([sims[m].max() for m in self._masks])
        order = np.argsort(per_cls)
        best, second = order[-1], order[-2] if len(order) > 1 else order[-1]
        s1 = float(per_cls[best])
        margin = s1 - float(per_cls[second])
        cls = self.classes[best]
        ctau = self.class_tau.get(cls, self.tau)
        return {"stamp": cls, "sim": round(s1, 4),
                "margin": round(margin, 4),
                "runner": self.classes[second],
                "runner_sim": round(float(per_cls[second]), 4),
                "class_tau": round(ctau, 4),
                "accept": s1 >= ctau and margin >= self.margin_floor}


class EmbedDecider:
    """EmbedClassifier wrapped in the run loop's Decision vocabulary:
    image-quality gate, per-class acceptance bar (plus the Sort page's
    user offset), margin floor, bin lookup.

    Promoted to sole decider after ~1,000 live head-to-head cases with
    zero known-brand misfiles while sorting ~3x more of the previous
    classifier's reject pile."""

    def __init__(self, config, embed_clf):
        self.cfg = config
        self.ec = embed_clf

    def classify(self, frame, explain=False, center=None, probe=True):
        """probe=False skips the rotation-flicker evidence pass (3 extra
        embeds, ~350ms on the Pi) — batch capture uses it: the pre-label
        doesn't gate anything, and the shorter think time lets the cycle
        re-time against the real feed ack. Sorting runs keep the probe:
        its agree/alts log is the dataset the future review-flag policy
        gets designed from."""
        f = self.cfg.floors
        rej = self.cfg.unmatched_bin
        ex = {}

        def done(d):
            if explain:
                d.extras.update(ex)
            return d

        if frame is None:
            return done(Decision(rej, reason="capture_failed"))
        ex["sharpness"] = round(imaging.sharpness(frame), 1)
        if ex["sharpness"] < f["sharpness"]:
            return done(Decision(rej, reason="blurry"))

        if self.ec is None:          # no model installed: capture runs
            return done(Decision(rej, reason="no_model"))

        head = imaging.crop_head(frame, center=center)
        r = self.ec.predict(head)
        stamp, sim = r["stamp"], r["sim"]
        # user acceptance (Sort page slider): shifts every class's
        # self-tuned bar by a common offset. 0.85 = neutral (the gallery
        # floor), higher = stricter everywhere, lower = more permissive.
        user = float(f.get("stamp_confidence") or 0.85)
        if user != 0.85:
            r["class_tau"] = round(
                min(max(r["class_tau"] + (user - 0.85), 0.55), 0.99), 4)
        ex["embed"] = r
        # who crowded whom and what bar applied — rides into the run log
        # so reject review can explain itself ("GFL 89% vs PPU 87%") and
        # ambiguous rejects map which class pairs need photos next
        det = {"runner": r["runner"], "runner_sim": r["runner_sim"],
               "bar": r["class_tau"]}

        def dd(*a, **kw):
            d = Decision(*a, **kw)
            d.extras["embed"] = det
            return done(d)
        if sim < r["class_tau"]:
            return dd(rej, stamp=stamp, stamp_conf=sim,
                      reason="stamp_low_conf")
        if r["margin"] < self.ec.margin_floor:
            # family-aware margin: a coin-flip between two variants that
            # share a family AND a physical bin changes nothing — the
            # case lands in the same chute whichever way it falls, so
            # the ambiguity is not actionable and rejecting it would
            # only tax the unmatched bin. Capture mode has no bin map,
            # so labeling keeps the strict gate (a variant flip DOES
            # matter for review cards), and variants routed to
            # different bins keep it too.
            runner = r["runner"]
            same_family = any(stamp in m and runner in m
                              for m in getattr(self.cfg, "families",
                                               {}).values())
            wb = self.cfg.bin_map.get((stamp, None))
            same_bin = (wb is not None
                        and wb == self.cfg.bin_map.get((runner, None)))
            if not (same_family and same_bin):
                return dd(rej, stamp=stamp, stamp_conf=sim,
                          reason="stamp_ambiguous")
            det["family_tie"] = True     # audit trail: accepted because
                                         # the tie is intra-family
        # rotation flicker probe (LOGGED, never gates): a real member of
        # a class reads the same from every angle; strangers riding an
        # accidental resemblance often flip class between views.
        # Measured: strangers flicker 2.5x more, but few-shot
        # classes also flicker while RIGHT (21/61) — so this collects
        # live evidence for a future review-flag, and does not decide.
        views = None
        if probe:
            try:
                names = [stamp]
                for k in (1, 2, 3):
                    v = np.ascontiguousarray(np.rot90(head, k))
                    names.append(self.ec.predict(v)["stamp"])
                views = {"agree": names.count(stamp),
                         "alts": sorted(set(names) - {stamp})}
            except Exception:
                pass
        d_extras = {"embed": det}
        if views:
            d_extras["views"] = views
        bin_id = self.cfg.bin_map.get((stamp, None))
        if bin_id is None:
            # a confident read with nowhere to go: the OVERFLOW bin if the
            # user assigned one, else the unmatched catch-all (legacy).
            # Either way the reason stays no_bin_mapping — reports keep
            # telling the truth about WHY it landed there.
            over = getattr(self.cfg, "overflow_bin", None)
            d = Decision(over if over is not None else rej,
                         stamp=stamp, stamp_conf=sim,
                         reason="no_bin_mapping")
            d.extras.update(d_extras)
            return done(d)
        d = Decision(bin_id, stamp=stamp, stamp_conf=sim)
        d.extras.update(d_extras)
        return done(d)
