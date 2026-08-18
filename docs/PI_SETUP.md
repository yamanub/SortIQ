# Raspberry Pi setup — from blank SD card to sorting

Total time: ~5 minutes of flashing + ~5 minutes of automated install.
Tested on a Pi 4 and a Pi 5 (the Pi only runs inference; training
happens on a PC, see [TRAINER_SETUP.md](TRAINER_SETUP.md)).

## Part A — flash the card (on your PC/Mac, ~5 min)

1. Open the **Raspberry Pi Imager** (download from raspberrypi.com/software).
2. Choose your Pi model → **Raspberry Pi OS Lite (64-bit)** (under
   "Raspberry Pi OS (other)") → your SD card.
   *Lite, not Desktop* — the UI lives in your browser; the Pi needs no screen.
3. When asked **"apply OS customisation?"** choose **Edit settings** and set:
   - hostname: `pisortiq`
   - username `pisortiq` + a password (or paste your SSH public key)
   - your Wi-Fi SSID + password and country
   - Services tab: **enable SSH**
4. Write the card, put it in the Pi, power on. First boot takes ~90 s
   (it resizes the filesystem and joins your Wi-Fi — ethernet also works,
   just plug it in, and it makes the trainer's first dataset sync much
   faster).

## Part B — install SortIQ (one command from your workstation)

The Pi never needs GitHub credentials — the code is pushed to it over
SSH from your machine (which also makes shipping updates a one-liner):

```bash
tools/pi_deploy.sh                  # defaults to pisortiq@pisortiq.local
# or: tools/pi_deploy.sh pisortiq@192.168.1.50
```

The script (safe to re-run any time — that's also how you ship updates):

- rsyncs the code to `~/SortIQ` on the Pi (never touching the Pi's own
  `config.json`, machine settings, or collected images)
- migrates any old ShellSorter-era install automatically (folder, service)
- creates the venv and installs the light inference stack
  (`ai-edge-litert` on current Pi OS — the `tflite-runtime` successor for
  Python 3.12+ — plus `opencv-python-headless`, `flask`, `pyserial`) —
  **no full TensorFlow**; training stays on the PC
- points `serial.port` at `/dev/ttyUSB0` (the CS7.2's CH340)
- installs the `sortiq` systemd service so the app starts on boot and
  restarts on crash, then reports whether the board and camera USB are
  detected

Then open **http://pisortiq.local:5000** from any browser on the network.
If `.local` doesn't resolve (mDNS can be moody, especially from macOS),
use the Pi's IP address instead — your router's device list has it.

## Part C — first-connect checklist

Wiring: camera USB → Pi, CS7.2 board USB → Pi (or a single cable to the
board's USB-C — its onboard hub carries both), light ring stays on the
board.

**Strongly recommended: a USB isolator on the board's serial line.**
Stepper motors are electrically noisy, and on this machine that noise
fed back into the Pi's USB as error storms that dropped the board link
mid-run (kernel `err -71` spam) — a cheap ADuM3160-based USB isolator
(~$10) between the Pi and the board ended it completely, field-proven.
Two rules when using one:

- The isolator goes on the **board's serial line only**: Pi → isolator →
  board USB. ADuM3160 boards are Full-Speed (12 Mbit/s) — plenty for the
  board's serial, **far too slow for the camera**. With an isolator you
  can't use the board's onboard hub for the camera; plug the camera into
  the Pi directly.
- Firmware flashing works straight through it (it passes the DTR reset),
  so nothing else changes.

**The camera deserves the same respect, differently:** it can't go
through an isolator, so give it a **shielded USB cable with ferrite
chokes** (or clip ferrites onto the existing cable) and route it away
from the stepper motor wiring. Field-proven here too: the camera
dropped mid-run with `uvcvideo -71` error storms until the cable got
ferrites and a better route — clean ever since. And feed the Pi from a
solid 5 V supply; brown-outs masquerade as USB gremlins.

If your link is rock-solid without any of this, you may not need it —
but if the board disconnects when motors run, this plus proper
grounding of the machine is the fix (a floating ground was the root
cause here).

1. **Machine tab → Target: Real CS7.2 board → Connect** (port
   `/dev/ttyUSB0`). Expect `Ready` in the protocol log.
2. **Read from board** and adopt its values before pushing anything —
   the official CS7.2 guide says Feed Cycle Steps should be 70 (with
   homing switch) / 80 (without).
3. **Camera setup** (Collect tab toggle): pick your camera in the
   the stock OV3660 is auto-detected and the page offers only controls
   that actually do something (light ring, digital zoom/pan, crop
   geometry). Aim/focus, then test the light-ring slider — if maximum
   looks dim, the board's 2 kΩ trimpot is limiting it (adjust it or fit
   the bypass jumper). Save your setup as a **preset** once it looks good.
4. **Test feed / Test sort** a few slots from the Machine tab; tune
   Notification Delay upward in +20 ms steps if captured images blur.
5. If you run the SortIQ **firmware fork**, calibrate per-slot arm
   positions over your funnels (Machine tab → Slot calibration; see
   [firmware/README.md](../firmware/README.md)).
6. Check the header network readout and do one Wi-Fi scan from its
   dialog (confirms nmcli permissions).

## Notes

- **Can't find the Pi on the network?** If the box boots somewhere its
  known Wi-Fi doesn't reach (new garage, changed router, mistyped
  password at imaging time), after ~2 minutes it raises its own
  hotspot: **SortIQ-\<hostname\>**, password **sortbrass**. Join it,
  open `http://10.42.0.1:5000`, and use the network dialog (header
  readout → Wi-Fi scan) to put the box on your real network — the
  hotspot stands down on its own once the join succeeds or an ethernet
  cable shows up.
- **The Pi never trains** — by design (inference-only runtime).
  The Train tab on the Pi points you to the trainer PC; set that up with
  [TRAINER_SETUP.md](TRAINER_SETUP.md). Freshly trained models install
  onto the Pi over the network and hot-reload — no restart, no file
  copying.
- **Camera settings lock** once the active model has collected images —
  changing optics mid-dataset silently ruins it. The Camera page explains
  and offers a warned override; the intended path for experiments is a
  new model profile.
- Storage: each collected case costs ~2 MB of raw image. A 256 GB card
  makes storage a non-issue; budget ~10 GB per 5,000 cases.
- Logs: `ssh pisortiq@pisortiq.local journalctl -u sortiq -f`
- Restart the app: `ssh pisortiq@pisortiq.local sudo systemctl restart sortiq`
