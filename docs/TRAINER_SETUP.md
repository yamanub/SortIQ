# Trainer PC setup

The sorting machine (the Pi) deliberately does **not** train — it runs an
inference-only runtime. Training runs on a PC on the same network: the PC
mirrors the machine's dataset over HTTP, retrains the **embedding model**
from that snapshot, and installs the new generation (model + exemplar
gallery) back on the machine, which hot-reloads it. Day-to-day dataset
growth doesn't even need that — new classes and photos take effect with a
gallery rebuild, no training at all.

Since you already drive the machine from a browser on this PC, the whole
workflow is two tabs: the machine's app for collecting and sorting, and
`http://localhost:5000` for training.

## One-time setup

### Windows 11

1. Install **[uv](https://docs.astral.sh/uv/)** — it manages Python itself,
   so you don't hand-pick a version:
   ```bat
   winget install --id=astral-sh.uv -e
   ```
   (No winget? `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`.)
   Close and reopen your terminal afterwards so `uv` is on PATH.
2. Get the SortIQ repo (Git or a source zip). A GitHub zip extracts to a
   folder named `SortIQ-master` — and Windows "Extract All" often nests it
   (`SortIQ-master\SortIQ-master`). Open a terminal **in the folder where
   `dir` shows `pyproject.toml`**; every command below runs from there.
3. ```bat
   uv run webui\server.py --port 5000
   ```
   One command does the whole install: uv fetches Python 3.12 (TensorFlow
   ships wheels for a limited range of Python versions, so uv pins to the
   one `pyproject.toml` asks for instead of whatever's newest), creates
   `.venv`, installs the locked dependencies — a few hundred MB, give it a
   few minutes the first time — then starts the app.

   The first launch may trigger a Windows Firewall prompt — allow access
   on private networks so the trainer can reach the machine.

   Windows must also count your home network as **Private**: Settings →
   Network & internet → your connection → **Network profile type →
   Private network**. On a Public network Windows blocks the machines from
   reaching the trainer, and the trainer shows a warning until it's
   changed.

   (To just install without starting the server — e.g. before step 5 below
   — run `uv sync` instead.)
4. Browse to `http://localhost:5000` → **Train**.
5. **Recommended — make it hands-free:** in File Explorer, open the
   `tools` folder inside the SortIQ folder and double-click
   **`trainer_autostart_windows.bat`**. The trainer server then starts
   silently every time you log in (no console window), so steps 3–4
   never happen again: the machine's Train page simply finds the trainer
   whenever this PC is on. Undo any time by running the same file from a
   terminal with `remove`:
   `tools\trainer_autostart_windows.bat remove`.
6. **Also recommended — the watchdog:** double-click
   **`trainer_watchdog_windows.bat`** in the same folder. It installs a
   scheduled task that checks every 2 minutes and silently relaunches
   the trainer if it has stopped (background servers do occasionally
   die without a trace). Each revival is noted in `watchdog.log`, and
   the same file with `remove` uninstalls it. Autostart covers login;
   the watchdog covers the rest of the day.

Note on GPUs: recent TensorFlow has no native Windows GPU support, so
the trainer offers two ways to run a full retraining. **CPU** works out
of the box — an overnight job at low priority, the PC stays usable.
**GPU** cuts that to about an hour but needs a one-time WSL2 (Ubuntu)
setup with uv at `/opt/sortiq-gpu312`:
```sh
uv venv --python 3.12 /opt/sortiq-gpu312
uv pip install --python /opt/sortiq-gpu312 "tensorflow[and-cuda]"
```
once present, the Train page probes it at startup and offers it as a
choice. Day-to-day dataset growth needs neither — gallery rebuilds run
on CPU.

### macOS / Linux

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh   # or: brew install uv
uv run webui/server.py --port 5000                # uv handles Python 3.12 + deps
```

## Using it

1. On the Train page, set **Sorting machine** to the machine's address
   (e.g. `http://pisortiq.local:5000`) — or click **Find machine…**, which
   scans the local network and sets it in one click (immune to mDNS
   moods). "machine reachable ✓" confirms the link.
2. Click **Pull dataset**. Phases: dataset pull (incremental — only new
   or changed images transfer after the first run), then crop rebuild.
   Progress shows throughout. The first pull over Wi-Fi is slow; to seed
   it, use **Copy dataset to USB stick…** on the machine's Train tab, then
   copy the stick's `SortIQ/calibers` folder into this SortIQ folder, and
   the pull only verifies.
3. To retrain, pick a device under **Train a new model** — GPU (about an
   hour, if the WSL sandbox is set up) or CPU (overnight, low priority) —
   and press **Start training**. The trainer needs **100+ training crops
   across the whole dataset** to start a run at all (a dataset-wide
   floor, separate from any single class's 10-image training threshold);
   below that, **Start training** refuses with an error naming the crop
   count — pull more of the dataset or collect more brass first. The run
   trains a large teacher network on every crop, then distills the fast
   student the machine runs. The
   result is staged as a **candidate** with its bench numbers; press
   **Install** to archive the current generation (model and gallery
   together — restorable from the Train page) and push the new pair to
   the machine. A GPU failure is never papered over: the app asks
   whether to retry the GPU or run on CPU instead.

The trainer keeps a local mirror of the machine's dataset under
`calibers/<caliber>/<model>/` — deletions and renames on the machine
propagate on the next pull, so the mirror never resurrects removed classes.

## Keeping the trainer up to date

The trainer PC never needs the git repo after the first install — it
updates **from the machine**. Every `tools/pi_deploy.sh` run puts fresh
code on the machine; both installs expose a manifest of their deployable
code files (per-file SHA-256 rolled up into one digest — that digest is
the version, no git involved), and the two Train UIs compare them:

- the machine's **Train models…** window and the trainer's own Train page
  both warn when the trainer's code doesn't match the machine's, and
  **training is blocked until it does** — a stale trainer rebuilds crops
  with drifted imaging code and silently trains models the machine can't
  reproduce at sort time;
- one click — **Update trainer from machine** — pulls exactly the files
  that differ, deletes what the machine no longer ships, verifies every
  byte against the manifest, and restarts the trainer into the new code.
  Config, the dataset mirror, trained models, logs, and the venv are
  never touched.

Three things to know:

- If the update pulls a changed `uv.lock` (a dependency version moved),
  `uv run` picks that up on its own next launch — a trainer started with
  `uv run webui\server.py` (or the autostart shortcut, which launches
  `.venv`'s Python directly) needs one manual `uv sync` in the SortIQ
  folder to install the new packages before restarting.

- The first check on a fresh trainer may report that *every* file
  differs — line endings (a Windows zip vs the machine's
  files) count as differences. One click normalizes everything; after
  that, updates are only ever the files that really changed.
- A trainer that is a **git checkout** (a dev box) refuses self-update on
  purpose: the machine must never overwrite uncommitted work. Update it
  with `git pull` instead.
