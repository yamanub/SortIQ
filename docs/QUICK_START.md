# Quick start — zero to sorting

The whole path, in order, on one page. Each step links to the deeper doc
if you want the details. Rough budget: an afternoon to a running machine,
then one evening of collecting + one training run to your first real sort.

**You need:** an assembled [CS7.2 machine](https://github.com/sjseth/AI-Case-Sorter-CS7.2)
or a SortIQ Pico machine (the servo carousel or the sort arm, built with
[PICO_MACHINE_SETUP.md](PICO_MACHINE_SETUP.md), which uses these same
steps), a Raspberry Pi 4 or 5 (5 recommended — twice the inference speed) with a
64 GB+ SD card, and a Mac or Windows PC on the same network for training.
No keyboard for the Pi — everything runs in your browser. A 7"
touchscreen on the machine is optional
([hardware/touchscreen](../hardware/touchscreen/)); plug it into the Pi
before Step 2 and the installer sets it up.
(A Pi 3B does run everything — same accuracy, ~3× slower verdicts — and
will sort in a pinch, but it's not recommended as the machine's brain.)

### Recommended hardware (cheap)

Stepper motors are electrically noisy, and USB is how that noise reaches
the Pi. Each item below prevents a mid-run failure:

| Item | Why |
|---|---|
| **USB isolator** (ADuM3160-based, ~$10) on the board's serial line | Ends board-link drops when motors run. Serial only — too slow for the camera. |
| **Shielded USB cables with ferrite chokes** — one for the board link, one for the camera | The camera is on USB too and gets the same interference; a ferrite-choked cable prevents camera dropouts mid-run. Clip-on ferrites onto existing cables also work. |
| **Cable routing** | Keep both USB runs away from the stepper motor wiring. |
| **A solid 5 V supply** for the Pi | Brown-outs masquerade as USB gremlins. Use the official PSU or equivalent. |

---

## Step 1 — Flash the Pi (~5 min)

Raspberry Pi Imager → **Raspberry Pi OS Lite (64-bit)**. In the
customisation dialog set: hostname `pisortiq`, a username + password (or
SSH key), your Wi-Fi, and **enable SSH**. Boot the Pi.

Details: [PI_SETUP.md](PI_SETUP.md) Part A.

## Step 2 — Install SortIQ (one command, ~5 min)

First time only — get the code onto your PC/Mac and set up SSH:

```bash
git clone https://github.com/yamanub/SortIQ.git
cd SortIQ
ssh-copy-id pisortiq@pisortiq.local   # asks for the Pi password once
```

(No git? Use GitHub's **Code → Download ZIP** and unzip it instead. The
`ssh-copy-id` step is what lets the installer log in without a password —
skip it only if you pasted this machine's SSH key into the Imager.)

Then, from the repo folder:

```bash
tools/pi_deploy.sh                  # defaults to pisortiq@pisortiq.local
```

It pushes the code, builds the Pi's environment, and installs the
service. When it finishes, open **http://pisortiq.local:5000** (or the
Pi's IP) in any browser. With a touchscreen plugged in, the Pi reboots
once and the screen opens the machine's touchscreen page. Re-running the
same command later is also how you ship updates. Details:
[PI_SETUP.md](PI_SETUP.md) Part B.

## Step 3 — Connect the machine (~10 min)

1. **Power the machine on.** The app finds the board by itself (a CS7.2
   on USB, an SKR Pico build on the Pi's header UART) and the machine bar
   reads CONNECTED. On a CS7.2, **Machine tab → Read from board**, then
   **Save**, to adopt its values. A Pico build needs nothing here: the
   app applies its reference settings on first connect. If an IoT relay
   switches the board's power, tick **Relay power** on the Machine tab's
   Hardware card and use **Board power**. With a case feeder on a
   carousel, tick **Case feeder** there too and leave **Start and stop
   with sorting** on.
2. **Collect tab → Camera setup**: aim and focus the camera using the
   live sharpness number, set the light ring, and press **Save camera
   settings** (and **Save as…** a preset if you want to come back to it).
   Push **digital Zoom** up until the headstamp fills most of the frame
   (a small stamp in a wide shot starves the model of pixels), use
   **Pan** to re-center it, and set **Brightness** so the stamped
   letters read as crisp dark marks with no washed-out glare or murky
   shadow — check the "what the model sees" preview after each tweak.
3. **Test feed** a few cases from the Machine tab (the Hardware card on
   the carousel, the Slots card on a CS7.2 or sort arm). On a CS7.2 or
   sort arm, if captures blur, raise **Notification delay** in +20 ms
   steps.

Details + wiring advice: [PI_SETUP.md](PI_SETUP.md) Part C.

## Step 4 — Collect your first classes (~1 evening)

A fresh install has no trained model yet, so the first collection is
named by hand — it goes faster than it sounds, and every session after
this one becomes confirm-clicks. (When a release publishes a community
starter recognizer, the Train tab offers it to a fresh install.)

1. Sort a coffee can of brass **by hand** into 3–6 piles of your most
   common headstamps (FC, WIN, BLAZER…).
2. **Collect tab → Batch capture**: create the pile's class first (type
   it in the Capture view's filter box and press Enter, or **Dataset →
   New class…**), set **Hopper** to **One class** and pick it, pour in
   that pile and press **▶ Start batch capture**. The machine photographs
   every case at full speed; when the hopper empties it files the cases
   that look like that class and holds the rest for you in **Review**.
   Repeat per pile. (For a mixed pile, use **Mixed lot**: the review
   groups the cases by looks, and you name and file each group.)
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

1. **Sort tab**: put headstamps on the bin cards (**+ add** on a card
   opens a checklist of families and classes — tick what the bin should
   hold and Save), or enable **auto-assign** and let stamps claim slots
   as they appear. Anything uncertain goes to
   the **UNMATCHED** card — the machine never guesses.
2. **Start**. When brass stops arriving, a 10-second **No brass
   arriving** countdown asks whether a case is stuck (**Resume** if one
   is); then the run finishes, flushes the feed wheel, and shows a
   report — every case reviewable, any mistake one click from becoming
   training data.

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
