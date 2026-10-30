# Raspberry Pi setup — from blank SD card to sorting

Total time: ~5 minutes of flashing + ~5 minutes of automated install.
Tested on a Pi 3B, 4, and 5 (the Pi only runs inference; training
happens on a PC, see [TRAINER_SETUP.md](TRAINER_SETUP.md)). A 3B runs
the whole app with the same accuracy but ~740 ms verdicts — fine for
collection, review, and fleet duty, workable for sorting in a pinch;
use a 4 or 5 as the machine's brain.

> **Building a Pico machine (sort arm or carousel)?** The board link is
> the Pi's header UART, not USB. The installer sets the Pi up for it;
> follow [PICO_MACHINE_SETUP.md](PICO_MACHINE_SETUP.md) alongside this
> page for the wiring and flashing.

## Part A — flash the card (on your PC/Mac, ~5 min)

1. Open the **Raspberry Pi Imager** (download from raspberrypi.com/software).
2. Choose your Pi model → **Raspberry Pi OS Lite (64-bit)** (under
   "Raspberry Pi OS (other)") → your SD card.
   *Lite, not Desktop* — the UI lives in your browser. The optional 7"
   touchscreen needs no desktop either: the installer gives it a kiosk of
   its own ([hardware/touchscreen](../hardware/touchscreen/)).
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
SSH from your machine (which also makes shipping updates a one-liner).

**First time only:** clone this repository to your PC/Mac (`git clone
https://github.com/yamanub/SortIQ.git`, or GitHub's **Code → Download
ZIP**), and give this machine passwordless SSH to the Pi:

```bash
ssh-copy-id pisortiq@pisortiq.local   # asks for the Pi password once
```

Skip `ssh-copy-id` only if you pasted this machine's SSH public key into
the Imager (the Imager then disables password login, so `ssh-copy-id`
from a *different* machine won't work — run it from the machine whose
key you pasted, or log in from that machine and add the new key to
`~/.ssh/authorized_keys` on the Pi).

The installer also needs passwordless `sudo` on the Pi. An Imager-created
user has it already; if yours doesn't (the script detects this), it sets
it up for you, asking for the Pi password once.

Then, from the repo folder:

```bash
tools/pi_deploy.sh                  # defaults to pisortiq@pisortiq.local
# or: tools/pi_deploy.sh pisortiq@192.168.1.50
```

The script (safe to re-run any time — that's also how you ship updates):

- rsyncs the code to `~/SortIQ` on the Pi (never touching the Pi's own
  `config.json`, machine settings, or collected images)
- creates the venv and installs the light inference stack
  (`ai-edge-litert` on current Pi OS — the `tflite-runtime` successor for
  Python 3.12+ — plus `opencv-python-headless`, `flask`, `pyserial`) —
  **no full TensorFlow**; training stays on the PC
- installs the `sortiq` systemd service so the app starts on boot and
  restarts on crash, then reports whether the camera is detected
- sets the Pi up for the SKR Pico builds too (header UART, LED ring PWM,
  I²C, a persistent system log) and reboots the Pi if that changed a boot
  setting; none of it affects a CS7.2 on USB
- sets up the 7" touchscreen when one is plugged into the Pi's HDMI: a
  kiosk that opens the machine's touchscreen page at every boot, the
  screen's mode, and a hidden mouse pointer. With no screen plugged in it
  installs nothing for it; plug one in later and re-run the script

Then open **http://pisortiq.local:5000** from any browser on the network.
If `.local` doesn't resolve (mDNS can be moody, especially from macOS),
use the Pi's IP address instead — your router's device list has it.

## Optional: boot from an NVMe drive (Pi 5)

An SD card is the standard boot drive, and everything in this guide works
on one. A Pi 5 can boot from an NVMe drive on a PCIe hat instead: faster,
and far more robust than a card under the machine's constant writes. The
controller case has its own parts for the hat
([hardware/pico-controller](../hardware/pico-controller/README.md#nvme-on-a-pi-5)).

1. **Tell the Pi to look for the drive.** Boot the Pi once from an SD card
   (Part A's card is fine), then on the Pi:

   ```bash
   sudo rpi-eeprom-config --edit
   ```

   Set `BOOT_ORDER=0xf416` (NVMe first, then the SD card, then USB) and,
   for a hat that is not HAT+ (most M.2 hats), add a line `PCIE_PROBE=1`.
   Save and shut the Pi down.
2. **Write Raspberry Pi OS Lite to the drive** with the Imager, exactly as
   in Part A but choosing the drive (in a USB M.2 adapter) instead of a
   card.
3. **Turn the PCIe port on.** Still on your computer, open the drive's
   `bootfs` partition and add this line at the end of `config.txt`:

   ```
   dtparam=pciex1
   ```

4. Fit the drive in the hat, take the SD card out, and power on. Then
   carry on with Part B. `lsblk` on the Pi shows the root filesystem on
   `nvme0n1p2`.

## Part C — first-connect checklist

This part is for a CS7.2 machine. A Pico build is found on the Pi's
header UART and gets its build's reference settings on first connect:
follow [PICO_MACHINE_SETUP.md](PICO_MACHINE_SETUP.md) instead of steps 1
and 2 below.

Wiring: camera USB → Pi, CS7.2 board USB → Pi (or a single cable to the
board's USB-C — its onboard hub carries both), light ring stays on the
board.

**Strongly recommended: a USB isolator on the board's serial line.**
Stepper motors are electrically noisy, and that noise can reach the
Pi's USB as error storms that drop the board link mid-run (kernel
`err -71` spam). A cheap ADuM3160-based USB isolator (~$10) between the
Pi and the board stops it.
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
from the stepper motor wiring. A camera that drops mid-run with
`uvcvideo -71` error storms usually needs exactly this. And feed the Pi from a
solid 5 V supply; brown-outs masquerade as USB gremlins.

If your link is rock-solid without any of this, you may not need it —
but if the board disconnects when motors run, this plus proper
grounding of the machine is the fix.

1. **Power the machine on.** The app finds the board on USB by itself and
   connects; expect `Ready` in the Console drawer's protocol log.
2. **Read from board**, then **Save**, to adopt its values before pushing
   anything — the official CS7.2 guide says Feed Cycle Steps should be 70
   (with homing switch) / 80 (without).
3. **Camera setup** (Collect tab toggle): pick your camera model at the
   top of the page — the stock OV3660 and the Suyin HD are auto-detected
   — and the page offers only controls
   that actually do something (light ring, digital zoom/pan, crop
   geometry). Aim/focus, then test the light-ring slider — if maximum
   looks dim, the board's 2 kΩ trimpot is limiting it (adjust it or fit
   the bypass jumper). Press **Save camera settings** once it looks good
   (and **Save as…** a preset if you want to come back to it).
4. **Test feed / Test sort** a few slots from the Machine tab; tune
   Notification Delay upward in +20 ms steps if captured images blur.
5. If you run the SortIQ **firmware fork**, calibrate per-slot arm
   positions over your funnels (Machine tab → Slot calibration; see
   [firmware/README.md](../firmware/README.md)).
6. Click the Wi-Fi icon in the header and do one Wi-Fi scan from its
   dialog (confirms nmcli permissions).

## Notes

- **New router?** The trainer and the machines find each other again by
  name: within a few minutes of a machine coming back on a new address,
  every saved link to it is updated. Reserve each machine's address in
  the router's settings to keep them fixed.
- **Can't find the Pi on the network?** If the box boots somewhere its
  known Wi-Fi doesn't reach (new garage, changed router, mistyped
  password at imaging time), it raises its own hotspot after ~2 minutes
  (5 if it lost a network it was on): **SortIQ-\<hostname\>**, password **sortbrass**. Join it,
  open `http://10.42.0.1:5000`, and use the network dialog (header
  readout → Wi-Fi scan) to put the box on your real network — the
  hotspot stands down on its own once the join succeeds or an ethernet
  cable shows up.
- **NVMe drive on a Pi 5 with Wi-Fi:** the drive's PCIe link can drown
  the Pi's 2.4 GHz Wi-Fi, and forced to Gen 3 it drowns 5 GHz too. Leave
  the link at its default (no `dtparam=pciex1_gen=3`), join a 5 GHz
  network, and keep the ribbon cable flat and away from the antenna end
  of the board — or use ethernet.
- **The Pi never trains** — by design (inference-only runtime).
  The Train tab on the Pi points you to the trainer PC; set that up with
  [TRAINER_SETUP.md](TRAINER_SETUP.md). Freshly trained models install
  onto the Pi over the network and hot-reload — no restart, no file
  copying.
- **Camera changes are Save-gated.** No slider applies until you press
  Save; leaving the page with unsaved changes asks whether to save or
  revert, and Save warns that new photos will differ from the existing
  dataset. Changing optics mid-dataset silently weakens matches — the
  intended path for experiments is a new model profile.
- Storage: each collected case costs ~2 MB of raw image. A 256 GB card
  makes storage a non-issue; budget ~10 GB per 5,000 cases.
- Logs: `ssh pisortiq@pisortiq.local journalctl -u sortiq -f`
- Restart the app: `ssh pisortiq@pisortiq.local sudo systemctl restart sortiq`
