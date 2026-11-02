# SortIQ AutoUndefined

This fork extends [yamanub/SortIQ](https://github.com/yamanub/SortIQ) v1.6.0 with automatic identification of unidentified crops, optional automatic filing, and reversible review. In the standard web app, open **Dataset → Caliber & model** to configure both independent switches and your own OpenAI API key. Both switches start off. The key input is always blank; no key or personal dataset ships with this repository.

See [AutoUndefined setup and recovery](docs/AUTOUNDEFINED.md) and the [changelog](CHANGELOG.md). The original SortIQ documentation follows.

## SortIQ — vision-powered brass sorting
**A remix of an awesome DIY project.** SortIQ is a fork/remix of [Seth Hahner's](https://github.com/sjseth) brilliant [AI Case Sorter CS7.2](https://github.com/sjseth/AI-Case-Sorter-CS7.2) — the machine itself (mechanics, electronics, stock firmware) is his design, and none of this would exist without it. SortIQ replaces the desktop software side with a self-contained Raspberry Pi application (on-device TFLite inference, an open-set embedding classifier that says "I don't know" instead of guessing, in-browser dataset collection and machine control, LAN-based training) and carries a [firmware fork](firmware/README.md) with per-slot calibration and motion profiles. If you want the original experience, go build his — it's great.

**License:** [GPL-3.0](LICENSE), same as upstream. **No warranty — use at your own risk** (see [Disclaimer](#disclaimer) below). The firmware fork derives from Seth's GPL-3.0 code; the rest is original but ships under the same license to keep the whole program unambiguous.

SortIQ sorts fired pistol brass by **headstamp**. A camera photographs each
case head as the CS7.2 machine feeds it past; an embedding network matches
the stamp against a gallery of known examples and the sorter arm drops the
case into its bin, with everything the model isn't sure about routed to an
**unmatched** bin instead of guessed. The whole thing is driven from a
browser — collection, training, calibration, and live sorting runs.

## The pieces

| Piece | What it is |
|---|---|
| **The machine** | Seth's CS7.2: feed wheel, camera tube + light ring, 8-slot sorter — driven by its Arduino Uno board (2× TMC2209) over USB serial. Runs the stock firmware or the [SortIQ fork](firmware/README.md) (per-slot µstep calibration, true motion profiles, homing telemetry). |
| **The brain** | A Raspberry Pi (4 or 5; tested on both) running the SortIQ app as a service (`sortiq.service`, port 5000). Inference-only by design — TFLite runtime, no TensorFlow. ~100 ms per verdict on a Pi 5, ~200 ms on a Pi 4. A Pi 3B (1 GB) runs the full app and classifies just as accurately at ~740 ms per verdict — it can sort in a pinch at roughly a third of the pace, but isn't recommended as the machine's brain. |
| **The camera** | The stock OV3660 USB module or the Suyin HD (50° lens) module, auto-detected, with in-app controls for the light ring, digital zoom/pan, crop geometry (primer mask, rim adjust) and, on the Suyin, the camera's own image controls and a white-balance calibration. |
| **The trainer** | Any Mac or Windows PC on the network, running this same app. It mirrors the machine's dataset over HTTP (incremental after the first sync) and trains new embedding generations that install back over the network — the Pi hot-reloads. See [docs/TRAINER_SETUP.md](docs/TRAINER_SETUP.md). |
| **The servo carousel** | A second machine design on an SKR Pico: a geared 12-pocket wheel and eight servo gate doors, with an optional case feeder run from the same board. Renders, parts list and screws in [hardware/carousel](hardware/carousel/), the controller case in [hardware/pico-controller](hardware/pico-controller/), setup in [docs/PICO_MACHINE_SETUP.md](docs/PICO_MACHINE_SETUP.md). |
| **The touchscreen** | Optional: a 7" HDMI touchscreen on the machine's post, plugged into the Pi, runs sorting, capture, reject review and the machine controls without a phone or PC. The installer sets it up when it finds one plugged in. Parts and the printed mount in [hardware/touchscreen](hardware/touchscreen/). |
| **Printable parts** | [hardware/manifold](hardware/manifold/) — an experimental replacement sorter base (1" hose or 3/4" PEX per port), sort pipes, and 9mm drop-in funnels. |

## How a case gets sorted

Capture (multi-frame, steady-head) → find the case head → crop the headstamp
ring → a MobileNetV2 **embedding network** (distilled from a large teacher at
480 px) turns the crop into a vector → cosine match against a **gallery of
exemplars** (a handful of representative photos per class, picked by coverage)
→ three gates decide: sharpness floor, similarity over that **class's own
bar**, and a clear **margin over the runner-up** — fail any and the case goes
to the unmatched bin with a plain-language reason ("Too close to call —
SPEER 91% vs BLAZER 89%") instead of a guess. Every decision is logged to
`runs/` and each run ends with a reviewable report where any case can be
refiled into the dataset.

The open-set design is the point: a headstamp the model has never seen
doesn't *resemble* anything strongly enough to clear the gates, so strangers
end up in the unmatched bin and, from there, in a **set-aside tray** that
clusters look-alikes together — name the cluster once and it becomes a new
class, no retraining required (the gallery picks up new classes from as few
as 3 photos).

## Quick start

- **New here? Start with [docs/QUICK_START.md](docs/QUICK_START.md)** —
  the whole path from a blank SD card to your first sorting run, step by
  step on one page, including the recommended-hardware shopping list.
- **Using the app:** [docs/USER_MANUAL.md](docs/USER_MANUAL.md) — the
  illustrated user manual, tab by tab, with screenshots from a live machine.
- **Getting to 99%+:** [docs/TRAINING_GUIDE.md](docs/TRAINING_GUIDE.md) —
  how to structure classes so the model sorts with confidence: split
  visual variants, group them with families for the bins, keep the
  dataset clean with the scan loop, and know when to retrain.
- **Set up the machine (Pi):** [docs/PI_SETUP.md](docs/PI_SETUP.md) — flash
  a card, run `tools/pi_deploy.sh`, plug in the board and camera.
- **Set up the trainer PC:** [docs/TRAINER_SETUP.md](docs/TRAINER_SETUP.md)
  — install [uv](https://docs.astral.sh/uv/), one `uv run`, point it at the
  machine (or click *Find machine* — it scans the network), *Pull dataset*.
- **No hardware yet?** The app runs anywhere uv does:

  ```bash
  uv run webui/server.py        # http://localhost:5000
  ```

  The Machine tab can connect a **simulated machine** (a virtual board and
  a synthetic camera, so runs can be exercised with no hardware), and
  the Collect tab falls back to file upload or synthetic rendered brass, so
  the full collect → train → sort loop works on a bare laptop.

## The app, tab by tab

- **Collect** — live preview with head-detect overlay, one-key chained
  capture (feed → photograph → label → repeat), model pre-labeling, a
  full-speed **batch capture** mode that photographs a whole hopper (a
  **mixed lot** reviewed as grouped confirm-cards, or **one class**
  checked and filed automatically), and camera setup (zoom, light ring,
  crop geometry).
- **Train** — dataset health strip and per-class readiness bars, the
  installed embedding generation with its gallery stats, **Train
  models…** to drive training on your PC, a dataset copy to USB, and
  restorable archives of every model generation.
- **Dataset** — class cards with readiness bars, full-page image browser,
  an **Exemplars** card showing exactly which photos do the matching (pin
  or exclude any), variant **families**, the **set-aside tray** for
  unknown stamps, a mislabel scan that second-guesses every stored label,
  and crop/gallery rebuilds.
- **Test** — run any image through the live decider and see every gate,
  the closest gallery matches, the crop the model saw, and the
  destination bin.
- **Sort** — live sorting on slot cards: assignment by class, family, or
  auto-assign, an optional **OVERFLOW** bin so the catch-all holds only
  true rejects, live per-bin dashboards with capacity-calibrated fill
  bars, end-of-brass flush that empties the wheel into the correct bins,
  per-bin reports, pause/resume with jam and no-brass prompts, the case
  feeder strip, and reject review that turns mistakes into training
  data.
- **Machine** — board connection and console, machine settings for the
  hardware detected (CS7.2 stock or fork, SortIQ-Pico sort arm, PicoServo
  carousel), slot enable/disable, per-slot position calibration, the
  carousel's sort doors, board power through an optional IoT relay (also
  from a header icon), Home wheel / Test feed, and the optional case
  feeder. The network dialog lives behind the header's Wi-Fi icon.
- **Fleet** — every SortIQ machine on the network: model, camera and
  board health, live run status, build, and one-click update.
- **Docs** — full user documentation served in-app.

The optional 7" touchscreen carries the machine-side half of this:
**Sort**, **Capture** (single cases or a whole hopper), **Review** of
rejects and sorted cases, **Stats** with the thinnest classes and a
copy-to-USB, and the **Machine** controls, plus the network and Pi
controls behind its gear icon.

## Repo map

```
webui/            the app: Flask server + single-page UI + in-app docs
sorter/           pipeline: camera, imaging (crops), embedding classifier,
                  board serial transport, carousel gates, simulated
                  machine, profiles
tools/            pi_deploy.sh and the Pi installer, the embedding
                  training pipeline (teacher bench, student distillation,
                  gallery build, GPU job runner), the trainer's Windows
                  autostart
firmware/         CS72_SortIQ — the CS7.2 Arduino firmware fork (stock
                  firmware lives in Seth's repo — that's also the rollback)
                  SortIQ_Pico — the SKR Pico sort-arm build
                  SortIQ_PicoServo — the SKR Pico servo-carousel build
hardware/         build files that never ship to a machine: the servo
                  carousel (renders, parts list), the Pico controller
                  case, the 7" touchscreen mount, and the CS7.2
                  manifold STLs
calibers/         per-caliber/model profiles: dataset, crops, trained models
                  (a blank 9mm/Default template ships; data stays local)
docs/             QUICK_START.md, USER_MANUAL.md, PI_SETUP.md,
                  PICO_MACHINE_SETUP.md, TRAINER_SETUP.md,
                  TRAINING_GUIDE.md, COMMUNITY_HEADSTAMPS.md
config.json       global settings (camera, serial, active profile pointer)
pyproject.toml    dependencies + uv.lock are authoritative (uv run / uv sync);
uv.lock           requirements.txt is a plain-pip fallback kept in step —
requirements.txt  whoever bumps one bumps both
```

## Security model — read before exposing anything

SortIQ trusts your LAN completely, **by design**: the app has no logins,
and its update endpoint (`/api/code/update`) lets any device on your
network push code to the machine — that's what makes the one-click
trainer/machine sync work in a home shop. The flip side is absolute:
**never port-forward, tunnel, or otherwise expose a SortIQ machine or
trainer to the internet.** Anyone who can reach port 5000 can run code
on it. On a shared or untrusted network, treat every SortIQ box as open
to everyone on that network.

## Disclaimer

SortIQ is an **experimental hobby project**, provided **as-is, with no
warranty of any kind** — see sections 15 and 16 of the
[GPL-3.0 license](LICENSE), which legally govern. In plain English:

- This software **controls physical machinery**: it spins motors, drives
  current through stepper drivers, switches lights, and changes settings
  on your CS7.2 board. It is possible to jam, wear, or otherwise damage
  your machine, and by running this software **you accept that risk
  entirely**. Nobody who wrote or contributed to this project is liable
  for damage to your machine, electronics, media, brass, or anything
  else.
- **Flashing firmware carries its own risk.** A failed or interrupted
  flash can leave the board unresponsive. The stock firmware in
  [Seth's repo](https://github.com/sjseth/AI-Case-Sorter-CS7.2) is the
  flash-back rollback, but you perform any flash at your own risk.
- **Sort fired brass only.** Never put live ammunition, primed cases, or
  anything you have not personally inspected into the machine. You are
  solely responsible for the safe handling of ammunition components and
  for compliance with the laws that apply to you.
- This project is an independent remix and is **not affiliated with or
  endorsed by** the AI Case Sorter project or its author.

If any of that is not acceptable, do not use this software.
