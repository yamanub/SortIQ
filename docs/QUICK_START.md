# Quick start — zero to sorting

The whole path, in order, on one page. Each step links to the deeper doc
if you want the details. Rough budget: an afternoon to a running machine,
then one evening of collecting + one training run to your first real sort.

**You need:** an assembled [CS7.2 machine](https://github.com/sjseth/AI-Case-Sorter-CS7.2),
a Raspberry Pi 4 or 5 (5 recommended — twice the inference speed) with a
64 GB+ SD card, and a Mac or Windows PC on the same network for training.
No screen or keyboard for the Pi — everything runs in your browser.
(A Pi 3B does run everything — same accuracy, ~3× slower verdicts — and
will sort in a pinch, but it's not recommended as the machine's brain.)

### Recommended hardware (cheap, field-proven)

Stepper motors are electrically noisy, and USB is how that noise reaches
the Pi. Every item below earned its place by fixing a real mid-run failure:

| Item | Why |
|---|---|
| **USB isolator** (ADuM3160-based, ~$10) on the board's serial line | Ends board-link drops when motors run. Serial only — too slow for the camera. |
| **Shielded USB cables with ferrite chokes** — one for the board link, one for the camera | The camera is on USB too and gets the same interference; a ferrite-choked cable ended camera dropouts mid-run. Clip-on ferrites onto existing cables also work. |
| **Cable routing** | Keep both USB runs away from the stepper motor wiring. |
| **A solid 5 V supply** for the Pi | Brown-outs masquerade as USB gremlins. Use the official PSU or equivalent. |

---

## Step 1 — Flash the Pi (~5 min)

Raspberry Pi Imager → **Raspberry Pi OS Lite (64-bit)**. In the
customisation dialog set: hostname `pisortiq`, a username + password (or
SSH key), your Wi-Fi, and **enable SSH**. Boot the Pi.

Details: [PI_SETUP.md](PI_SETUP.md) Part A.

## Step 2 — Install SortIQ (one command, ~5 min)

From this repo on your PC/Mac:

```bash
tools/pi_deploy.sh                  # defaults to pisortiq@pisortiq.local
```

It pushes the code, builds the Pi's environment, and installs the
service. When it finishes, open **http://pisortiq.local:5000** (or the
Pi's IP) in any browser. Re-running the same command later is also how
you ship updates. Details: [PI_SETUP.md](PI_SETUP.md) Part B.

## Step 3 — Connect the machine (~10 min)

1. **Machine tab → Connect** (port `/dev/ttyUSB0`). Expect `Ready` in
   the Console drawer's protocol log. **Read from board** and adopt its
   values.
2. **Collect tab → Camera setup**: aim and focus the camera using the
   live sharpness number, set the light ring, and **save a preset**.
   Push **digital Zoom** up until the headstamp fills most of the frame
   (a small stamp in a wide shot starves the model of pixels), use
   **Pan** to re-center it, and set **Brightness** so the stamped
   letters read as crisp dark marks with no washed-out glare or murky
   shadow — check the "what the model sees" preview after each tweak.
3. **Test feed** a few cases from the Machine tab. If captures blur,
   nudge Notification Delay up in +20 ms steps.

Details + wiring advice: [PI_SETUP.md](PI_SETUP.md) Part C.

## Step 4 — Collect your first classes (~1 evening)

A fresh install has no trained model yet, so the first collection is
named by hand — it goes faster than it sounds, and every session after
this one becomes confirm-clicks. (A downloadable community starter
recognizer is planned for a future release; when published, the Train
tab will offer it to fresh installs automatically.)

1. Sort a coffee can of brass **by hand** into 3–6 piles of your most
   common headstamps (FC, WIN, BLAZER…).
2. **Collect tab → Batch capture**: pour in one pile, press start — the
   machine photographs every case at full speed. When it finishes, the
   review shows your cases grouped by looks; type the headstamp name on
   the group and file it. Repeat per pile.
3. Aim for **25–50 images per class** to start (10+ is the per-class
   training minimum, 3+ makes a class sortable at all). More is better;
   you'll grow them for weeks. Training a model also needs **100+
   images across the whole dataset** — a handful of classes at 25–50
   each clears that easily, but a single thin class won't.

## Step 5 — Set up the trainer + first training (~15 min setup, ~1 h training)

1. On your PC/Mac: [TRAINER_SETUP.md](TRAINER_SETUP.md) (install uv, then one
   `uv run`; optional WSL GPU setup makes training ~12× faster).
2. On the **machine's** Train tab, press **Train models…** — the window
   finds the trainer on the PC you're browsing from and walks the whole
   flow: **sync → pick GPU or CPU → progress → bench numbers → Install**.
   Leave the machine idle while it syncs and trains.
3. **Install** archives nothing you'll miss (it's your first model) and
   pushes the model to the machine, which hot-reloads.

From now on the model pre-labels everything — collection becomes
confirm-clicks, and brand-new headstamps sort from as few as 3 photos
with **no retraining** (they cluster in review; name the cluster once).

## Step 6 — First sort (~5 min)

1. **Sort tab**: type headstamps onto the bin cards (**+ add** on a
   card — the picker filters as you type), or enable **auto-assign**
   and let stamps claim slots as they appear. Anything uncertain goes to
   the **UNMATCHED** card — the machine never guesses.
2. **Start**. When the hopper runs dry the run ends itself, flushes the
   feed wheel, and shows a report — every case reviewable, any mistake
   one click from becoming training data.

## Step 7 — The growth loop

This is the ongoing rhythm that makes the model sharper every week:

- **Capture** new brass in batches; the review's confirm-cards do the
  labeling. Classes with 500+ images automatically keep only photos that
  add something new ("File novel") — no dataset bloat.
- After a big session: **Scan for duplicates** (Dataset tab) → confirm
  deletions → **Rebuild gallery**.
- **Retrain** every few hundred new images via the same Train models…
  flow. Bench numbers before you install; one-click restore if a
  generation disappoints.
- Check the Dataset tab's readiness bars: **red** classes (under 10)
  need photos, **green** (500+) are well-fed — feed the reds.

That's the whole system. Deeper reading: the illustrated
[USER_MANUAL.md](USER_MANUAL.md), and the in-app **Docs** tab for the
decision flow, protocol reference, and troubleshooting.
