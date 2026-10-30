#!/usr/bin/env python
"""Distill the ConvNeXt embedder into a Pi-sized MobileNetV2@224 student.

Teacher: tools/embedding_bench2_convnext_224_full.keras (95.4 closed /
83.8 fewshot; 882ms on the Pi — too slow to ship). Student: MNV2@224
(36ms on the Pi, measured). The student learns to (a) reproduce the
teacher's embedding for each training crop (cosine loss; teacher runs
once on clean images, student sees augmented views — the mismatch
doubles as consistency regularization) and (b) keep classes separable
via the same ArcFace head used across the experiment.

Evaluates on the identical tiers/splits as embedding_bench2 --holdout 0
so numbers are directly comparable. Saves .keras + .tflite + JSON.

Run:  python tools/distill_student.py [--epochs 20] [--ft-epochs 15]
"""
import argparse
import json
import random
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = 7


def profile_dir():
    cfg = json.loads((ROOT / "config.json").read_text())
    act = cfg.get("active", {})
    return ROOT / "calibers" / act["cartridge"] / act["model"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=20)
    ap.add_argument("--ft-epochs", type=int, default=15)
    ap.add_argument("--gallery", type=int, default=10)
    ap.add_argument("--split", default="random", choices=["random", "index"],
                    help="held-out queries: 'random' (seeded shuffle) or "
                         "'index' (newest 20%% of each class by capture "
                         "index — see embedding_bench2 --split)")
    ap.add_argument("--img-size", type=int, default=224)
    ap.add_argument("--embed-dim", type=int, default=128)
    ap.add_argument("--margin", type=float, default=0.3)
    ap.add_argument("--scale", type=float, default=30.0)
    ap.add_argument("--distill-w", type=float, default=1.0)
    ap.add_argument("--arc-w", type=float, default=0.5)
    ap.add_argument("--teacher", default="embedding_bench2_convnext_224_full.keras")
    ap.add_argument("--teacher-npz", default=None,
                    help="precomputed teacher embeddings (paths CLASS/FILE, "
                         "vecs) — e.g. features extracted from the community "
                         "convnext_small; skips loading a keras teacher and "
                         "sets the student's embed dim to the target dim")
    ap.add_argument("--embed-batch", type=int, default=64,
                    help="teacher/student embed batch; 8-16 on an 8GB GPU")
    ap.add_argument("--shuffle-buf", type=int, default=0,
                    help="cap the shuffle buffer (0 = whole dataset)")
    ap.add_argument("--out-prefix", default="shadow_embed",
                    help="artifact basename in models/ — pass candidate_* "
                         "to stage a run without touching the live "
                         "shadow_embed pair (in-app training does)")
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
    trained = sorted(c for c in counts
                     if c in labels and c not in benched and counts[c] >= 10)
    fewshot = sorted(c for c in counts
                     if c in benched and counts[c] >= args.gallery + 2)
    unknown = sorted(c for c in counts
                     if c not in trained and c not in fewshot and counts[c] >= 3)
    print(f"trained {len(trained)} | few-shot {fewshot} | unknown {unknown}")

    rng = random.Random(SEED)
    files = {c: sorted((crops / c).glob("*.png")) + sorted((crops / c).glob("*.jpg"))
             for c in counts}
    for c in files:
        if args.split == "index":       # newest session first = the queries
            files[c].sort(key=lambda p: p.name, reverse=True)
        else:
            rng.shuffle(files[c])

    train_paths, train_y = [], []
    queries, gallery = [], []
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
    for c in unknown:
        for f in files[c][:30]:
            queries.append((f, c, "unknown"))

    size = args.img_size
    n_cls = len(trained)

    def load(path, sz=None):
        img = tf.io.decode_image(tf.io.read_file(path), channels=3,
                                 expand_animations=False)
        sz = sz or size
        return tf.image.resize(img, (sz, sz)) / 255.0

    def embed_with(model, paths):
        # feed the model at ITS input size — a 224 teacher can't eat the
        # student's 480 view (same crops, its own preferred resolution)
        t_sz = model.input_shape[1] or size
        arr = []
        bs = args.embed_batch
        for i in range(0, len(paths), bs):
            imgs = [load(str(p), t_sz) for p in paths[i:i + bs]]
            k = len(imgs)
            imgs += [imgs[-1]] * (bs - k)
            arr.append(model(tf.stack(imgs), training=False).numpy()[:k])
        v = np.concatenate(arr)
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    if args.teacher_npz:
        # cross-modal distillation: targets were computed by a different
        # network on ITS preferred view of the same crops (e.g. the
        # community convnext_small judging color at 480px); the student
        # learns to reproduce those judgments from the production view
        z = np.load(args.teacher_npz, allow_pickle=False)
        # hoist the arrays ONCE: indexing z["vecs"][i] in a loop
        # re-decompresses the whole array every access — 40GB of churn
        # at 768-d, which read as a "GPU OOM" for three straight runs
        zv, zp = z["vecs"], z["paths"]
        tv = {str(p): zv[i] for i, p in enumerate(zp)}
        key = lambda p: f"{Path(p).parent.name}/{Path(p).name}"
        missing = [p for p in train_paths if key(p) not in tv]
        if missing:
            raise SystemExit(f"{len(missing)} train crops missing from "
                             f"{args.teacher_npz} (e.g. {missing[:2]})")
        t_emb = np.stack([tv[key(p)] for p in train_paths]).astype("float32")
        args.embed_dim = t_emb.shape[1]
        print(f"teacher targets from {args.teacher_npz}: dim {args.embed_dim}")
    else:
        print("precomputing teacher embeddings for", len(train_paths), "crops")
        teacher = tf.keras.models.load_model(pdir / "models" / args.teacher)
        t_emb = embed_with(teacher, train_paths).astype("float32")
        del teacher

    buf = args.shuffle_buf or len(train_paths)
    ds = (tf.data.Dataset.from_tensor_slices((train_paths, train_y, t_emb))
          .shuffle(buf, seed=SEED)
          .map(lambda p, y, t: ((load(p), y), (t, y)),
               num_parallel_calls=tf.data.AUTOTUNE)
          .batch(32).prefetch(2))

    L = tf.keras.layers
    aug = tf.keras.Sequential([
        L.RandomRotation(0.5, fill_mode="constant", fill_value=0.0),
        L.RandomTranslation(0.06, 0.06, fill_mode="constant", fill_value=0.0),
        L.RandomBrightness(0.3, value_range=(0, 1)),
        L.RandomContrast(0.15)])
    base = tf.keras.applications.MobileNetV2(
        input_shape=(size, size, 3), include_top=False, weights="imagenet")
    base.trainable = False
    inp = tf.keras.Input((size, size, 3))
    x = aug(inp)
    x = L.Rescaling(2.0, offset=-1.0)(x)
    x = base(x, training=False)
    x = L.GlobalAveragePooling2D()(x)
    x = L.Dropout(0.2)(x)
    e = L.Dense(args.embed_dim, use_bias=False)(x)
    emb = L.BatchNormalization(name="embedding")(e)

    class ArcFace(tf.keras.layers.Layer):
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
    model = tf.keras.Model([inp, lab], [emb, arc(emb, lab)])

    def cos_loss(y_true, y_pred):
        return 1.0 + tf.keras.losses.cosine_similarity(y_true, y_pred)

    def smoothed_cce(y_true, y_pred):
        y = tf.one_hot(tf.cast(tf.reshape(y_true, [-1]), tf.int32), n_cls)
        return tf.keras.losses.categorical_crossentropy(
            y, y_pred, from_logits=True, label_smoothing=0.1)

    warm = [tf.keras.callbacks.LambdaCallback(
        on_epoch_begin=lambda ep, _:
            arc.m.assign(0.0 if ep < 5 else args.margin))]
    model.compile("adam", [cos_loss, smoothed_cce],
                  loss_weights=[args.distill_w, args.arc_w])
    model.fit(ds, epochs=args.epochs, verbose=2, callbacks=warm)

    base.trainable = True
    for l in base.layers[:len(base.layers) * 2 // 3]:
        l.trainable = False
    model.compile(tf.keras.optimizers.Adam(1e-4), [cos_loss, smoothed_cce],
                  loss_weights=[args.distill_w, args.arc_w])
    model.fit(ds, epochs=args.ft_epochs, verbose=2)

    student = tf.keras.Model(inp, emb)
    out = pdir / "models" / args.out_prefix  # ships as shadow_embed
    student.save(out.with_suffix(".keras"))
    tfl = tf.lite.TFLiteConverter.from_keras_model(student).convert()
    out.with_suffix(".tflite").write_bytes(tfl)
    print("saved:", out.with_suffix(".keras"), f"tflite {len(tfl)/1e6:.1f}MB")

    g_vec = embed_with(student, [p for p, _ in gallery])
    g_cls = np.array([c for _, c in gallery])
    q_vec = embed_with(student, [p for p, _, _ in queries])
    sims = q_vec @ g_vec.T
    classes = sorted(set(g_cls))
    per_cls = np.stack([sims[:, g_cls == c].max(axis=1) for c in classes],
                       axis=1)
    i1 = per_cls.argmax(axis=1)
    recs = []
    for qi, (path, true, tier) in enumerate(queries):
        recs.append({"true": true, "tier": tier, "path": str(path),
                     "pred": classes[i1[qi]],
                     "sim": round(float(per_cls[qi, i1[qi]]), 4)})

    rep = {"tag": "distill_student_mnv2_224", "split": args.split,
           "epochs": args.epochs,
           "ft_epochs": args.ft_epochs, "distill_w": args.distill_w,
           "arc_w": args.arc_w, "teacher": args.teacher}
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
    known = [r for r in recs if r["tier"] != "unknown"]
    unk = [r for r in recs if r["tier"] == "unknown"]
    rep["open_set"] = {}
    scores = sorted(r["sim"] for r in known)
    for accept in (0.95, 0.90):
        tau = scores[int(len(scores) * (1 - accept))]
        rep["open_set"][f"known_accept_{accept}"] = {
            "tau": round(float(tau), 4),
            "unknown_accepted":
                f"{sum(1 for r in unk if r['sim'] >= tau)}/{len(unk)}",
            "known_wrong_identity_accepted":
                f"{sum(1 for r in known if r['sim'] >= tau and r['pred'] != r['true'])}"
                f"/{len(known)}"}
    rep["records"] = recs
    np.savez_compressed(out.with_suffix(".npz"), q_vec=q_vec, g_vec=g_vec,
                        g_cls=g_cls,
                        q_true=np.array([t for _, t, _ in queries]),
                        q_tier=np.array([t for _, _, t in queries]))
    out.with_suffix(".json").write_text(json.dumps(rep, indent=2))
    print(json.dumps({k: v for k, v in rep.items() if k != "records"},
                     indent=2))


if __name__ == "__main__":
    main()
