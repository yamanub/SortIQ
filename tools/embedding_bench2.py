#!/usr/bin/env python
"""Instrumented embedding bench — v3 of the embedding+gallery experiment.

What's new over embedding_bench.py (kept for the v1/v2 baselines):
  * per-query records saved (true, pred, sims, runner-up) so open-set
    failures can be inspected instead of guessed at
  * synthetic unknowns: a few well-populated trained classes are held out
    of training AND gallery, giving the unknown tier real statistics
    (the true unknowns are 4 classes / 25 queries — too thin to measure
    a reject rate that must be ~0)
  * two accept rules scored side by side: raw top-1 similarity vs
    margin-to-runner-up (top1 − best other-class sim)
  * embeddings dumped to .npz so new accept rules can be tried offline
    in seconds without retraining
  * selectable backbone (mobilenetv2 / convnext_tiny / effnetv2b0)

Run:  python tools/embedding_bench2.py --tag <name> [--backbone mobilenetv2]
      [--epochs 40] [--ft-epochs 15] [--margin 0.3] [--embed-dim 128]
Writes embedding_bench2_<tag>.json and embedding_bench2_<tag>.npz here.
"""
import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = 7


def profile_dir():
    cfg = json.loads((ROOT / "config.json").read_text())
    act = cfg.get("active", {})
    return ROOT / "calibers" / act["cartridge"] / act["model"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--backbone", default="mobilenetv2",
                    choices=["mobilenetv2", "convnext_tiny", "effnetv2b0"])
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--ft-epochs", type=int, default=15)
    ap.add_argument("--gallery", type=int, default=10)
    ap.add_argument("--img-size", type=int, default=160)
    ap.add_argument("--embed-dim", type=int, default=128)
    ap.add_argument("--margin", type=float, default=0.3)
    ap.add_argument("--scale", type=float, default=30.0)
    ap.add_argument("--holdout", type=int, default=4,
                    help="trained classes to hold out as synthetic unknowns")
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--ft-lr", type=float, default=1e-4)
    ap.add_argument("--embed-batch", type=int, default=64,
                    help="eval-pass batch; drop to 8-16 on an 8GB GPU")
    ap.add_argument("--eval-only", default=None, metavar="CKPT",
                    help="skip training, evaluate a saved embedder .keras")
    ap.add_argument("--split", default="random", choices=["random", "index"],
                    help="which images become held-out queries: 'random' "
                         "(seeded shuffle, the historical bench) or 'index' "
                         "(the NEWEST 20%% of each class by capture index — "
                         "a session the model never saw; same-case re-photos "
                         "and same-session lighting cannot leak into the "
                         "score, so this is the honest cross-session number)")
    ap.add_argument("--outlier-exposure", action="store_true",
                    help="the stranger lesson: half of each sub-floor "
                         "class (3-9 images) trains with a repulsion "
                         "loss — 'this belongs near nothing' — the "
                         "other half becomes honest unknown queries")
    args = ap.parse_args()

    import numpy as np
    import tensorflow as tf
    tf.keras.utils.set_random_seed(SEED)

    pdir = profile_dir()
    mj = json.loads((pdir / "model.json").read_text())
    crops = pdir / "data" / "stamp"
    labels = set(mj["stamp_labels"])
    benched = set(mj.get("train_disabled") or [])

    counts = {d.name: len(list(d.glob("*.png"))) + len(list(d.glob("*.jpg")))
              for d in sorted(crops.iterdir()) if d.is_dir()}
    eligible = sorted(c for c in counts
                      if c in labels and c not in benched and counts[c] >= 10)

    # synthetic unknowns: classes with enough images to measure a reject
    # rate, picked nearest the median count so training keeps its anchors
    # and the holdouts aren't starved either. Deterministic by sort.
    rich = sorted((c for c in eligible if counts[c] >= 45),
                  key=lambda c: counts[c])
    med = rich[len(rich) // 2] if rich else None
    holdout = sorted(sorted(rich, key=lambda c: abs(counts[c] - counts[med]))
                     [:args.holdout]) if med else []
    trained = [c for c in eligible if c not in holdout]
    fewshot = sorted(c for c in counts
                     if c in benched and counts[c] >= args.gallery + 2)
    unknown = sorted(c for c in counts
                     if c not in trained and c not in fewshot
                     and c not in holdout and counts[c] >= 3)
    print(f"trained {len(trained)} | few-shot {fewshot}\n"
          f"holdout-unknown {holdout} | true-unknown {unknown}")

    rng = random.Random(SEED)
    files = {c: sorted((crops / c).glob("*.png")) + sorted((crops / c).glob("*.jpg"))
             for c in counts}
    for c in files:
        if args.split == "index":
            # crops are numbered in capture order (indices are stable —
            # moves and deletes never renumber), so the highest indices
            # are the latest session. Put them FIRST: the query slice
            # below takes the head of the list.
            files[c].sort(key=lambda p: p.name, reverse=True)
        else:
            rng.shuffle(files[c])

    train_paths, train_y = [], []
    queries = []                       # (path, true_class, tier)
    gallery = []                       # (path, class)
    for i, c in enumerate(trained):
        fs = files[c]
        n_q = max(2, min(30, int(len(fs) * 0.2)))
        for f in fs[:n_q]:
            queries.append((f, c, "closed"))
        pool = fs[n_q:]
        for f in pool:
            train_paths.append(str(f))
            train_y.append(i)
        for f in pool[:args.gallery]:
            gallery.append((f, c))
    for c in fewshot:
        fs = files[c]
        for f in fs[:args.gallery]:
            gallery.append((f, c))
        for f in fs[args.gallery:args.gallery + 30]:
            queries.append((f, c, "fewshot"))
    outlier_paths = []
    if args.outlier_exposure:
        # sub-floor classes: first half feeds the repulsion loss, the
        # rest stays an UNSEEN unknown query — thin but honest
        for c in unknown:
            fs = files[c]
            cut = max(len(fs) // 2, 1)
            outlier_paths += [str(f) for f in fs[:cut]]
            for f in fs[cut:cut + 30]:
                queries.append((f, c, "unknown"))
    else:
        for c in unknown:
            for f in files[c][:30]:
                queries.append((f, c, "unknown"))
    for c in holdout:
        for f in files[c][:30]:
            queries.append((f, c, "holdout"))

    size = args.img_size
    n_cls = len(trained)

    def load(path, y):
        img = tf.io.decode_image(tf.io.read_file(path), channels=3,
                                 expand_animations=False)
        img = tf.image.resize(img, (size, size)) / 255.0
        return img, y

    if args.outlier_exposure and outlier_paths:
        print(f"outlier exposure: {len(outlier_paths)} stranger samples")
        train_paths = train_paths + outlier_paths
        train_y = train_y + [-1] * len(outlier_paths)
    ds = (tf.data.Dataset.from_tensor_slices((train_paths, train_y))
          .shuffle(len(train_paths), seed=SEED)
          .map(load, num_parallel_calls=tf.data.AUTOTUNE)
          .batch(args.batch).prefetch(2)
          .map(lambda x, y: ((x, y), y)))

    outdir = profile_dir() / "models"      # manifest-safe artifact home
    if args.eval_only:
        # evaluation crashed once (embed-pass GPU OOM with the training
        # graph still resident) — this path resumes from the checkpoint
        # the training run saves before evaluating
        embedder = tf.keras.models.load_model(args.eval_only)
        print("loaded embedder:", args.eval_only)
    else:
        L = tf.keras.layers
        aug = tf.keras.Sequential([
            L.RandomRotation(0.5, fill_mode="constant", fill_value=0.0),
            L.RandomTranslation(0.06, 0.06, fill_mode="constant",
                                fill_value=0.0),
            L.RandomBrightness(0.3, value_range=(0, 1)),
            L.RandomContrast(0.15)])

        # convnext/effnetv2 keras models carry their own preprocessing and
        # want [0,255]; mobilenetv2 wants [-1,1]
        if args.backbone == "mobilenetv2":
            base = tf.keras.applications.MobileNetV2(
                input_shape=(size, size, 3), include_top=False,
                weights="imagenet")
            pre = L.Rescaling(2.0, offset=-1.0)
        elif args.backbone == "convnext_tiny":
            base = tf.keras.applications.ConvNeXtTiny(
                input_shape=(size, size, 3), include_top=False,
                weights="imagenet")
            pre = L.Rescaling(255.0)
        else:
            base = tf.keras.applications.EfficientNetV2B0(
                input_shape=(size, size, 3), include_top=False,
                weights="imagenet")
            pre = L.Rescaling(255.0)
        base.trainable = False

        inp = tf.keras.Input((size, size, 3))
        x = aug(inp)
        x = pre(x)
        x = base(x, training=False)
        x = L.GlobalAveragePooling2D()(x)
        x = L.Dropout(0.2)(x)
        e = L.Dense(args.embed_dim, use_bias=False)(x)
        emb = L.BatchNormalization(name="embedding")(e)

        class ArcFace(tf.keras.layers.Layer):
            """s·cos(θ+m) on the true class (insightface recipe); m in a
            variable so warmup can hold it at 0."""
            def __init__(self, n, s, m):
                super().__init__()
                self.n, self.s = n, s
                self.m = tf.Variable(m, trainable=False, dtype=tf.float32)

            def build(self, _):
                self.W = self.add_weight(name="W",
                                         shape=(args.embed_dim, self.n),
                                         initializer="glorot_uniform")

            def call(self, z, y):
                zn = tf.nn.l2_normalize(z, axis=1)
                W = tf.nn.l2_normalize(self.W, axis=0)
                cos = tf.clip_by_value(zn @ W, -1 + 1e-7, 1 - 1e-7)
                sin = tf.sqrt(1.0 - cos * cos)
                tgt = tf.where(cos > tf.cos(3.14159265 - self.m),
                               cos * tf.cos(self.m) - sin * tf.sin(self.m),
                               cos - self.m * tf.sin(self.m))
                hot = tf.one_hot(tf.cast(y, tf.int32), self.n)
                return self.s * (hot * tgt + (1 - hot) * cos)

        lab = tf.keras.Input((), dtype="int32")
        arc = ArcFace(n_cls, args.scale, args.margin)
        model = tf.keras.Model([inp, lab], arc(emb, lab))
        OUTLIER_EXPOSURE = args.outlier_exposure
        SCALE_S = args.scale

        def smoothed_cce(y_true, y_pred):
            yt = tf.cast(tf.reshape(y_true, [-1]), tf.int32)
            y = tf.one_hot(yt, n_cls)
            ce = tf.keras.losses.categorical_crossentropy(
                y, y_pred, from_logits=True, label_smoothing=0.1)
            if not OUTLIER_EXPOSURE:
                return ce
            # strangers (label -1): push every class cosine below 0.1 —
            # "you belong near nothing". one_hot(-1)=zeros, so their
            # logits are s*cos untouched by the margin.
            cos = y_pred / SCALE_S
            rep = tf.reduce_mean(tf.nn.relu(cos - 0.1), axis=-1)
            is_out = tf.cast(tf.equal(yt, -1), ce.dtype)
            # balance weights live HERE, not in fit(class_weight=...):
            # Keras applies class_weight by gathering weight[label] inside
            # the input pipeline, where label -1 is out of range
            w = tf.gather(CLS_W, tf.maximum(yt, 0))
            return (1.0 - is_out) * w * ce + is_out * 2.0 * rep

        model.compile("adam", smoothed_cce,
                      metrics=["sparse_categorical_accuracy"])
        n_real = sum(1 for y in train_y if y >= 0)
        weight = {i: round(min(n_real / (n_cls * max(train_y.count(i), 1)),
                               10.0), 3) for i in range(n_cls)}
        if args.outlier_exposure:
            CLS_W = tf.constant([weight[i] for i in range(n_cls)],
                                dtype=tf.float32)
            fit_weight = None    # weighting happens in the loss (see above)
        else:
            fit_weight = weight
        warm = [tf.keras.callbacks.LambdaCallback(
            on_epoch_begin=lambda ep, _:
                arc.m.assign(0.0 if ep < 5 else args.margin))]
        model.fit(ds, epochs=args.epochs, verbose=2, class_weight=fit_weight,
                  callbacks=warm)

        if args.ft_epochs:
            base.trainable = True
            for l in base.layers[:len(base.layers) * 2 // 3]:
                l.trainable = False
            model.compile(tf.keras.optimizers.Adam(args.ft_lr), smoothed_cce,
                          metrics=["sparse_categorical_accuracy"])
            model.fit(ds, epochs=args.ft_epochs, verbose=2,
                      class_weight=fit_weight)

        embedder = tf.keras.Model(inp, emb)
        # save BEFORE the embedding pass: a crash below must not cost the
        # hours of training above (it did once — exit 9 mid-embed)
        ckpt = outdir / f"embedding_bench2_{args.tag}.keras"
        embedder.save(ckpt)
        print("embedder saved:", ckpt)

    def embed(paths):
        arr = []
        bs = args.embed_batch
        for i in range(0, len(paths), bs):
            imgs = [load(str(p), 0)[0] for p in paths[i:i + bs]]
            k = len(imgs)
            # pad to a fixed batch shape — ragged final batches retrace
            # the compiled graph and leak memory
            imgs += [imgs[-1]] * (bs - k)
            arr.append(embedder(tf.stack(imgs), training=False).numpy()[:k])
        v = np.concatenate(arr)
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    g_vec = embed([p for p, _ in gallery])
    g_cls = np.array([c for _, c in gallery])
    q_vec = embed([p for p, _, _ in queries])

    sims = q_vec @ g_vec.T
    classes = sorted(set(g_cls))
    # best sim per gallery class -> top1 + best other-class runner-up
    per_cls = np.stack([sims[:, g_cls == c].max(axis=1) for c in classes],
                       axis=1)
    order = per_cls.argsort(axis=1)
    top1_i, top2_i = order[:, -1], order[:, -2]
    recs = []
    for qi, (path, true, tier) in enumerate(queries):
        s1 = float(per_cls[qi, top1_i[qi]])
        s2 = float(per_cls[qi, top2_i[qi]])
        recs.append({"true": true, "tier": tier, "path": str(path),
                     "pred": classes[top1_i[qi]], "sim": round(s1, 4),
                     "runner": classes[top2_i[qi]], "runner_sim": round(s2, 4),
                     "margin": round(s1 - s2, 4)})

    rep = {"tag": args.tag, "backbone": args.backbone, "split": args.split,
           "trained_classes": n_cls, "holdout": holdout,
           "gallery_per_class": args.gallery, "epochs": args.epochs,
           "ft_epochs": args.ft_epochs, "img_size": size,
           "embed_dim": args.embed_dim, "margin_m": args.margin,
           "scale_s": args.scale}
    for tier in ("closed", "fewshot"):
        rs = [r for r in recs if r["tier"] == tier]
        ok = sum(1 for r in rs if r["pred"] == r["true"])
        rep[tier] = {"n": len(rs), "top1": round(ok / max(len(rs), 1), 4)}
    per = defaultdict(lambda: [0, 0])
    for r in recs:
        if r["tier"] == "fewshot":
            per[r["true"]][0] += r["pred"] == r["true"]
            per[r["true"]][1] += 1
    rep["fewshot_per_class"] = {c: f"{v[0]}/{v[1]}" for c, v in sorted(per.items())}

    known = [r for r in recs if r["tier"] in ("closed", "fewshot")]
    unk_true = [r for r in recs if r["tier"] == "unknown"]
    unk_hold = [r for r in recs if r["tier"] == "holdout"]
    rep["open_set"] = {}
    for rule in ("sim", "margin"):
        scores = sorted(r[rule] for r in known)
        block = {}
        for accept in (0.95, 0.90):
            tau = scores[int(len(scores) * (1 - accept))]
            def acc(rs):
                return sum(1 for r in rs if r[rule] >= tau)
            wrong = sum(1 for r in known
                        if r[rule] >= tau and r["pred"] != r["true"])
            right = sum(1 for r in known
                        if r[rule] >= tau and r["pred"] == r["true"])
            block[f"known_accept_{accept}"] = {
                "tau": round(float(tau), 4),
                "true_unknown_accepted": f"{acc(unk_true)}/{len(unk_true)}",
                "holdout_unknown_accepted": f"{acc(unk_hold)}/{len(unk_hold)}",
                "known_wrong_identity_accepted": f"{wrong}/{len(known)}",
                "known_right_accepted": f"{right}/{len(known)}"}
        rep["open_set"][rule] = block

    # the diagnostic v1/v2 couldn't answer: what do accepted unknowns
    # match? (label-granularity look-alikes vs genuine embedding failure)
    tau95 = sorted(r["sim"] for r in known)[int(len(known) * 0.05)]
    match = defaultdict(Counter)
    for r in unk_true + unk_hold:
        if r["sim"] >= tau95:
            match[r["true"]][r["pred"]] += 1
    rep["accepted_unknowns_matched"] = {
        c: dict(m.most_common(3)) for c, m in sorted(match.items())}

    out = outdir / f"embedding_bench2_{args.tag}"
    np.savez_compressed(
        out.with_suffix(".npz"),
        q_vec=q_vec, g_vec=g_vec, g_cls=g_cls,
        q_true=np.array([t for _, t, _ in queries]),
        q_tier=np.array([t for _, _, t in queries]),
        q_path=np.array([str(p) for p, _, _ in queries]))
    rep["records"] = recs
    out.with_suffix(".json").write_text(json.dumps(rep, indent=2))
    slim = {k: v for k, v in rep.items() if k != "records"}
    print(json.dumps(slim, indent=2))
    print("written:", out.with_suffix(".json"))


if __name__ == "__main__":
    main()
