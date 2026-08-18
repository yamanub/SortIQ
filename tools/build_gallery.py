#!/usr/bin/env python
"""Build the shadow-mode exemplar gallery from the active profile's crops.

For every class with enough images, embed up to --exemplars crops with
the student model and store the vectors in shadow_gallery.npz. Classes
below the trainer's 10-image minimum still get a gallery — few-shot
recognition of thin classes is the architecture's whole selling point.

Run on the trainer PC after (re)training a student:
    python tools/build_gallery.py [--model tools/distill_student_mnv2_224.tflite]
                                  [--exemplars 10] [--tau 0.85]
Writes shadow_gallery.npz beside the model. Install both to the machine
with the Train page's shadow controls (or POST /api/shadow/push).
"""
import argparse
import hashlib
import json
import random
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = 7                      # same shuffle as the benches — exemplars
                              # come from what training saw, never from
                              # the benches' held-out query slices


def main():
    cfg0 = json.loads((ROOT / "config.json").read_text())
    act0 = cfg0.get("active", {})
    models_dir = (ROOT / "calibers" / act0.get("cartridge", "")
                  / act0.get("model", "") / "models")
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=str(models_dir / "shadow_embed.tflite"))
    ap.add_argument("--exemplars", type=int, default=10)
    ap.add_argument("--min-images", type=int, default=3)
    ap.add_argument("--tau", type=float, default=0.85)
    ap.add_argument("--margin-floor", type=float, default=0.03)
    args = ap.parse_args()

    import numpy as np
    import cv2

    import sys
    sys.path.insert(0, str(ROOT))
    from sorter.embed_classifier import EmbedClassifier  # noqa: E402
    from sorter.classifier import _load_interpreter      # noqa: E402

    cfg = json.loads((ROOT / "config.json").read_text())
    act = cfg.get("active", {})
    crops = ROOT / "calibers" / act["cartridge"] / act["model"] / "data" / "stamp"

    interp = _load_interpreter(args.model)
    inp = interp.get_input_details()[0]
    out = interp.get_output_details()[0]
    h, w = inp["shape"][1], inp["shape"][2]

    # the previous gallery already banks a vector for EVERY crop (the
    # novelty gate needs them), and the embedder is deterministic — so an
    # unchanged file's old vector IS its new vector. Reuse them and only
    # embed what's new: moving one image no longer costs a full
    # re-embed of the dataset. A different model digest (retrain) or a
    # changed file signature (crop regeneration) invalidates naturally.
    model_digest = hashlib.md5(Path(args.model).read_bytes()).hexdigest()
    out_path = Path(args.model).with_name("shadow_gallery.npz")
    cache = {}
    # cross-machine amnesty: a gallery built on ANOTHER machine (trainer
    # build installed onto the Pi) can never match size:mtime — its sigs
    # carry the builder's file times. Same gated fallback the live
    # classifier's bank_vec uses: trust the banked vector when the crop
    # PREDATES the gallery, its SIZE half still matches (crops are
    # deterministic re-encodes, so byte size survives machines), and the
    # imaging signature the bank was built under matches this machine's
    # — an imaging reshape between build and install stays a full price.
    amnesty_sig = None
    old_gal_mtime = 0.0
    if out_path.is_file():
        try:
            old = np.load(out_path, allow_pickle=False)
            om = json.loads(str(old["meta"]))
            if om.get("model_digest") == model_digest and "g_all_sig" in old:
                cache = {p: (v, s) for p, v, s in
                         zip(old["g_all_path"], old["g_all_vec"],
                             old["g_all_sig"])}
                old_gal_mtime = out_path.stat().st_mtime
                want = om.get("crops_sig")
                sig_p = crops / ".crops_sig"
                if want and sig_p.is_file() and sig_p.read_text() == want:
                    amnesty_sig = want
        except Exception as e:
            print(f"cache from previous gallery unusable ({e}) — full build")
    n_new = n_cached = 0

    def sig(path):
        st = path.stat()
        return f"{st.st_size}:{int(st.st_mtime)}"

    def embed(path, rel):
        nonlocal n_new, n_cached
        hit = cache.get(rel)
        if hit is not None and hit[1] == sig(path):
            n_cached += 1
            return hit[0]
        if hit is not None and amnesty_sig is not None:
            st = path.stat()
            if (st.st_mtime < old_gal_mtime
                    and str(hit[1]).split(":")[0] == str(st.st_size)):
                n_cached += 1
                return hit[0]
        img = cv2.imread(str(path))
        if img is None:
            return None                  # corrupt/empty file: caller skips
        x = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        x = cv2.resize(x, (w, h)).astype(np.float32) / 255.0
        interp.set_tensor(inp["index"], x[None, ...])
        interp.invoke()
        v = interp.get_tensor(out["index"])[0].astype(np.float32)
        n_new += 1
        return v / (np.linalg.norm(v) or 1.0)

    # user pins from the profile: {class: [crop filenames]} — a pinned
    # image is always an exemplar; k-center diversifies AROUND the pins
    mj = json.loads((crops.parent.parent / "model.json").read_text())
    pins = mj.get("gallery_pinned") or {}
    # excluded images never become exemplars: k-center loves outliers,
    # and a blown-out capture from a retired camera IS a maximal
    # outlier — banned from matching duty, still fine for training
    excl = mj.get("gallery_excluded") or {}

    rng = random.Random(SEED)
    vecs, cls, paths = [], [], []
    all_vecs, all_cls = [], []      # every crop's vector, for the dedupe gate
    all_paths = []                  # crop paths so the gate can pixel-verify
    all_sigs = []                   # size:mtime per crop — the reuse key
    skipped = []
    n_pinned = 0
    for d in sorted(crops.iterdir()):
        if not d.is_dir():
            continue
        files = sorted(d.glob("*.png")) + sorted(d.glob("*.jpg"))
        if len(files) < args.min_images:
            skipped.append(f"{d.name} ({len(files)})")
            continue
        rng.shuffle(files)
        # embed EVERY readable crop first: the full class bank must hold
        # them all — the novelty gate can only convict a re-filed case
        # against images it can see. The bench holdout and exclusions
        # below shape exemplar CHOICE only (previously the bank
        # was built from the exemplar pool, which silently dropped the
        # ~30-image holdout — a re-run WIN case slid past the gate
        # because its original hid there).
        embedded = {f.name: embed(f, f"{d.name}/{f.name}") for f in files}
        bad = [n for n, v in embedded.items() if v is None]
        if bad:
            print(f"  {d.name}: skipped {len(bad)} unreadable "
                  f"file(s): {', '.join(bad[:4])}")
        files = [f for f in files if embedded[f.name] is not None]
        if len(files) < args.min_images:
            skipped.append(f"{d.name} (unreadable)")
            continue
        all_vecs.append(np.stack([embedded[f.name] for f in files]))
        all_cls += [d.name] * len(files)
        all_paths += [f"{d.name}/{f.name}" for f in files]
        all_sigs += [sig(f) for f in files]
        # mirror the benches' split: the first 20% (capped) were query
        # material there; exemplars come from the remainder so gallery
        # vectors were never used to grade the model. Pinned files are
        # exempt — an explicit user choice outranks the split.
        n_q = max(2, min(30, int(len(files) * 0.2)))
        pin_names = set(pins.get(d.name, []))
        excl_names = set(excl.get(d.name, []))
        efiles = [f for f in files if f.name not in excl_names] or files
        pool = [f for f in efiles[n_q:] if f.name not in pin_names] or efiles
        pool = [f for f in efiles if f.name in pin_names] + \
               [f for f in pool if f.name not in pin_names]
        # exemplars are chosen for COVERAGE, not at random: greedy
        # k-center over the pool — start at the most central image,
        # repeatedly add the one farthest from the chosen set.
        # A variant filed into its parent class (FC with a center dot,
        # 24WIN) is visually far from the originals, so it is guaranteed
        # an exemplar slot instead of hiding behind 10 look-alike picks.
        pe = np.stack([embedded[f.name] for f in pool])
        # big classes carry more natural variation (glare angles, die
        # generations) than 10 points can span — measured on live run
        # 221247, where correct BLAZERs (~400 imgs) sat at sim 0.75-0.85
        # against a 10-exemplar gallery. One extra slot per 40 images,
        # capped at 2x the base.
        quota = min(args.exemplars + len(pool) // 40, args.exemplars * 2)
        k = min(quota, len(pool))
        chosen = [i for i, f in enumerate(pool) if f.name in pin_names][:k]
        n_pinned += len(chosen)
        if not chosen:
            chosen = [int(np.argmax(pe @ pe.mean(axis=0)))]
        while len(chosen) < k:
            best_sim = np.max(pe @ pe[chosen].T, axis=1)
            best_sim[chosen] = np.inf          # never re-pick
            chosen.append(int(np.argmin(best_sim)))
        for i in chosen:
            vecs.append(pe[i])
            cls.append(d.name)
            paths.append(f"{d.name}/{pool[i].name}")
        pin_note = f", {len(pin_names & {f.name for f in pool})} pinned" \
            if pin_names else ""
        print(f"{d.name}: {k} exemplars (k-center over {len(pool)}{pin_note})")
    if skipped:
        print("skipped (too few images):", ", ".join(skipped))

    print(f"vectors: {n_cached} reused from the previous gallery, "
          f"{n_new} freshly embedded")
    # the imaging signature the crops were shaped by, carried along so a
    # consumer on ANOTHER machine can tell whether banked vectors still
    # describe its own crops (see EmbedClassifier.bank_vec)
    crops_sig_p = crops / ".crops_sig"
    meta = {"tau": args.tau, "margin_floor": args.margin_floor,
            "exemplars": args.exemplars, "pinned": n_pinned,
            "model": Path(args.model).name, "model_digest": model_digest,
            "crops_sig": (crops_sig_p.read_text()
                          if crops_sig_p.is_file() else None),
            "built_at": time.strftime("%Y-%m-%d %H:%M"),
            "classes": len(set(cls)), "vectors": len(vecs)}
    np.savez_compressed(out_path, g_vec=np.stack(vecs),
                        g_cls=np.array(cls), g_path=np.array(paths),
                        g_all_vec=np.concatenate(all_vecs).astype(np.float32),
                        g_all_cls=np.array(all_cls),
                        g_all_path=np.array(all_paths),
                        g_all_sig=np.array(all_sigs),
                        meta=json.dumps(meta))
    print(f"written: {out_path}  ({meta['classes']} classes, "
          f"{meta['vectors']} vectors, tau={args.tau})")

    # self-check: the classifier must load what we just wrote
    ec = EmbedClassifier(args.model, out_path)
    r = ec.predict(cv2.imread(str(next(crops.glob("*/*.png")))))
    print("self-check predict:", r)


if __name__ == "__main__":
    main()
