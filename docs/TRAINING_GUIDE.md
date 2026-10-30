# Training Guide — building a model that sorts with confidence

SortIQ identifies brass by **look, not by name**: every photo maps to a
point in an embedding space, and a case is called by whichever class's
gallery examples sit closest. That one fact drives everything in this
guide. A class whose images all look alike produces tight, confident
matches; a class that quietly mixes two different looks produces thin
margins, hesitant calls, and rejects that should have been sorts.

The workflow below takes a dataset from "a pile of labeled photos" to a
model that identifies at 99%+ with margins to spare.

---

## 1 · Split classes by look, not by brand

Most headstamp names cover several physically different dies. "FC"
brass arrives plain, with dots, with close-set dots, with numbers, with
tiny diamonds. "WIN" arrives plain, with numbers, as +P. To a human
they're all one brand; to the matcher they are **different looks**, and
lumping them into one class forces its gallery to stretch across all of
them — which drags the whole class's match quality down and blurs the
line against genuinely different brands.

**The rule: if you can see the difference in the app's crop view, give
it its own class.** `FC` and `FC DOTS` and `FC DIAMONDS`, not one big
`FC`. A mark of a few pixels — a small diamond between two words — is
enough to justify a split.

What a split buys you, concretely: a variant living inside its parent
class typically matches with margins of +0.01–0.03 (coin-flip
territory, constant near-rejects). After splitting and retraining, the
same images match their own class at +0.10–0.30 — decisive, every time.

Don't worry about splitting "too small". A class is trainable at 10
images and usable in the gallery even below that — few-shot
recognition of thin classes is what the gallery architecture is for.
Feed thin classes more brass as it shows up; they harden with every
image.

## 2 · Rejoin the variants with a family

Splitting improves *recognition* — but you rarely want six FC bins.
That's what **families** are for (Dataset page → Families): group
`FC`, `FC DOTS`, `FC DIAMONDS`, … into family `FC`, then assign the
*family* to a bin. Members sort into one bin as one unit, while
staying separate classes underneath.

Families also make close calls safe instead of wasteful. When the top
two candidates for a case are members of the same family headed to the
same bin, the machine accepts the call instead of rejecting it as
ambiguous — the case lands in the right bin either way, so hesitating
between siblings costs nothing. Without the family, that same near-tie
kicks a perfectly sortable case to the Unmatched bin.

Families follow class changes: renaming a class renames it in its
families, merging or deleting one takes it out, and a family left with a
single class is removed, that class keeping the family's bin.

The pattern to remember: **split for the model, family for the bin.**

## 3 · Keep classes clean — the scan loop

A mislabeled image doesn't just sit there quietly: the gallery's
exemplar picker favors unusual-looking images, so a wrong-class photo
tends to become an *exemplar* — actively vouching for the wrong class.
The Dataset page has two fast tools for this:

- **Scan for mislabels** — every image is checked against the whole
  gallery with its own vote masked out, and images that read as a
  different class are flagged for review. The scan covers the full
  dataset in seconds, so run it freely.
- **Scan for duplicates** — finds the same physical case photographed more
  than once (re-run brass), pixel-verified so look-alike distinct
  cases never flag.

Run the loop after any big dataset session: **scan → fix what's
genuinely wrong → rebuild gallery → scan again.** Mislabels can shelter
each other (two wrong images vouching for each other), so a second
pass after fixes sometimes surfaces stragglers the first pass couldn't
see. The loop converges quickly — a clean dataset scans clean twice.

Two flags that are *not* mislabels, and what to do instead:

- **Worn-but-real stamps** (correct label, heavily worn die): keep
  them — they teach the model what worn examples look like — but mark
  them **Excluded** (in the class window, click the photo's badge until
  it shows 🚫) so they never
  become matching anchors. Deletion is for bad *photos* (ghosted,
  smeared), not bad *brass*.
- **Thin-class flags**: a 4-image class losing one image to a
  look-alike neighbor isn't mislabeled, it's hungry. More brass is the
  fix.

## 4 · Capture quality in, quality out

- Respect the **sharpness floor** — it exists to keep unusable photos
  out of training. If a batch capture reports a wave of blurry
  rejects, check the camera before filing anything: a soft batch is
  disposable (discard and re-run the same brass), a soft *dataset* is
  forever.
- **Zoom and pan onto the headstamp before you collect a single
  image.** Push the digital Zoom slider up until the headstamp covers
  the majority of the frame — a stamp shrunk into a wide shot of the
  whole case head hands the model too few pixels to tell close dies
  apart. Use Pan left/right and up/down to re-center the primer once
  zoomed (pan does nothing below 1× zoom). Set the light ring so the
  stamped letters read as clear, dark marks against the brass: too
  bright washes them out in glare, too dim buries them in shadow.
  Nickel and other reflective cases need the light diffused rather
  than aimed straight on. Judge it by the "what the model sees" crop
  preview, not the raw camera view.
- Keep **imaging settings** (zoom, pan, crop geometry, camera look)
  stable across a dataset. If you change them, capture a fresh batch
  and rebuild crops and the gallery so training and sorting see the
  same picture.
- A **training run** needs 100+ crops across the *whole dataset* to
  start at all — a floor on top of, and separate from, any single
  class's 10-image training threshold. A dataset with only a couple of
  thin classes can clear every per-class gate and still be too small
  to start a run; the error names the shortfall.
- The batch intake filter keeps well-fed classes (500+) from bloating
  with routine look-alikes — it only files what adds information.
  Trust it; hand-picked additions bypass it by design.

## 5 · Retrain when the labels outgrow the model

The gallery updates instantly — new classes and new images work for
sorting as soon as you rebuild it. **Training** is what bakes the
distinctions into the embedding itself, and it's worth a run when:

- you've split variants out of a parent class (the big one — margins
  between the new siblings stay thin until a retrain teaches the
  model to see the difference),
- the dataset has grown substantially since the last run, or
- the bench/margins say a pair of classes keeps crowding each other.

After a training run, read the bench like this: closed-set accuracy is
the headline, **wrong-identity count is the one that must be zero**
(a reject costs a re-run; a wrong bin costs a mixed batch), and the
strangers number tells you how the model treats brass it has never
seen. Install, rebuild the gallery, and spot-check a freshly split
class's margins — healed margins are the receipt that the split took.

---

**The short version:** split every visible variant into its own class,
family them back together for the bins, scan-and-fix until clean,
retrain after splits, and keep the camera honest. Each step is minutes
of work, and together they're the difference between a sorter that
hesitates and one that just sorts.
