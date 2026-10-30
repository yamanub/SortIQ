# Firmware

The app drives four hardware configurations. It reads the board's version
reply on connect and shows the settings for that build.

| Hardware | Firmware | In this repo | How to flash |
|---|---|---|---|
| CS7.2, stock | the stock CS7.2 firmware | [Seth's repo](https://github.com/sjseth/AI-Case-Sorter-CS7.2) | as upstream |
| CS7.2, SortIQ fork | `7.2.250925.6.2-SS2` | [CS72_SortIQ](CS72_SortIQ/) (source) | arduino-cli over USB, below |
| SKR Pico sort arm | `SortIQ-Pico-2.8` | [SortIQ_Pico](SortIQ_Pico/README.md) | `~/SortIQ/tools/flash_pico.sh arm` |
| SKR Pico carousel, with the optional case feeder | `SortIQ-PicoServo-0.9.2` | [SortIQ_PicoServo](SortIQ_PicoServo/README.md) | `~/SortIQ/tools/flash_pico.sh carousel` |

The SKR Pico images ship ready to flash; the full procedure is in
[docs/PICO_MACHINE_SETUP.md](../docs/PICO_MACHINE_SETUP.md#flashing-the-skr-pico).

## CS7.2 firmware — SortIQ fork

`CS72_SortIQ/` is a fork of the stock CS7.2 firmware the machine ships
with (upstream version **7.2.250925.6.1**). Current fork version string:
**`7.2.250925.6.2-SS2`**.

**Provenance & license:** the fork derives from
[Seth Hahner's AI-Case-Sorter-CS7.2](https://github.com/sjseth/AI-Case-Sorter-CS7.2),
licensed **GPL-3.0**; it stays GPL-3.0 (LICENSE at the repo root), and
the header of `CS72_SortIQ.ino` carries the statement of modifications.
The **stock firmware itself lives in Seth's repo** — that's the
canonical copy and the flash-back rollback; this repo ships only the
fork.

The protocol is a **strict superset** of stock: every stock command,
reply, and ack is unchanged, so the SortIQ app (and the original
Windows app) drive either firmware identically.

### What the fork adds

| Feature | Commands | Why |
|---|---|---|
| Per-slot position table (µsteps, 12 slots) | `slotpos:<i>:<µsteps>`, reported in `getconfig` (`SlotPositions`, `MaxSlots`) | Exact drop centering on custom output funnels; non-uniform spacing; more slots on the same disc. Defaults reproduce the stock `index × SortSteps × 16` grid, so flashing alone changes nothing. **Push `sortsteps:` before `slotpos:` — the former refills the whole table.** |
| True trapezoid sorter accel | `sortaccel:<µs>` (start/stop delay, default 1200) | Smooth constant-acceleration ramps (AVR446 integer method) instead of the stock linear delay slope; slot-to-slot is also ~30% faster. |
| Two-stage sorter homing | `sorthomebackoff:<µsteps>` (default 160), `sorthomeslow:<µs>` (default 1400) | Fast seek → back off past the sensor → slow re-approach: the trigger edge is taken at a low constant speed every time. The disc re-homes every trip through slot 0, so this edge is the accuracy floor for the whole table. |
| Feed launch profile | `feedlaunch:<µsteps>` (default 48) | Short launch ramp, then full cruise across the open camera port — the case must cross the port fast or it slips in before the tensioner grips it. No end-of-travel decel; the stop is shaped by the homing offset. |
| Offset-as-decel toggle | `feeddecel:0|1` (default 1) | The feed homing offset (5 steps by default) runs as a decel ramp (identical stop dynamics every cycle) or stock-style flat with a dead stop. Tune on brass. |
| Arm dwell | `armdwell:<ms>` (default 0, clamp 0–1000), reported in `getconfig` (`ArmDwellMs`) | Unconditional extra hold immediately before every arm move — clearance time for brass that tumbles slowly down the arm tube (a slow faller can exit exactly as the arm swings, and the fight skips sort steps silently). Additive and cycle-independent, unlike `slotdropdelay`, whose remainder logic never fires once the app's think time exceeds it. Zero-travel moves skip it, so batch capture (arm parked) pays nothing. |
| Pipelined cycle (SS2) | `pf` (pipelined feed), `ps:<slot>` (pipelined slot) | The mechanical cycle runs *under* the app's inference instead of after it: the photo is taken, `pf` starts the next feed immediately, and `ps` delivers the slot as soon as the model decides. Real cases-per-minute, protocol still a strict superset of stock. |
| Feed homing telemetry | `feedstats` → `{"LastHomingSteps":n,"MaxHomingSteps":n,"FeedCycles":n}` | Homing seek length per cycle = drift/jam predictor. Poll from the app between cycles; the `done` line is untouched. |
| TMC2209 `intpol(true)` | — | 256-µstep interpolation on both drivers (missing from the stock build). |

New `getconfig` keys: `SortAccelFactor`, `SortHomeBackoff`,
`SortHomeSlowDelay`, `FeedLaunchSteps`, `FeedDecelOverOffset`,
`ArmDwellMs`, `MaxSlots`, `SlotPositions`.

### Building and flashing from the Pi

The fork ships as source. The installer does not put the Arduino tools on
the Pi, so install them once: get `arduino-cli` from the
[Arduino CLI installation page](https://arduino.github.io/arduino-cli/latest/installation/),
then add the board core and the driver library:

```sh
arduino-cli core install arduino:avr
arduino-cli lib install "TMCStepper@0.7.3"
```

The board's Uno is reachable on `/dev/ttyUSB0`, through the USB isolator
if one is fitted (the ADuM3160 passes the DTR auto-reset, so avrdude
works through it). From `~/SortIQ`:

```sh
sudo systemctl stop sortiq        # frees the serial port
arduino-cli compile --fqbn arduino:avr:uno firmware/CS72_SortIQ \
  --upload -p /dev/ttyUSB0
sudo systemctl start sortiq       # auto-connect re-pushes settings
```

Verify: Machine tab → console → `version` should answer with an
`-SS2` suffix; `getconfig` should show the new keys.

**Rollback**: fetch the stock `CS72_Firmware.ino` from
[Seth's repo](https://github.com/sjseth/AI-Case-Sorter-CS7.2) and flash
it the same way.

### Bench-tuning order (first session on the fork)

1. Flash; confirm `version` + `getconfig`; run a normal 10-case sort —
   behavior must be indistinguishable from stock (defaults are stock).
2. Sorter feel: tune `sortaccel` (bigger = gentler), watch for skipped
   steps at aggressive values.
3. Homing repeatability: mark the disc, run `sorttest:` cycles, tune
   `sorthomebackoff`/`sorthomeslow` until the mark returns exactly.
4. Feed: tune `feedlaunch` so the case is at cruise well before the port
   arc; compare `feeddecel:1` and `0` for how still the case sits under
   the camera (the Collect page's sharpness and center numbers).
5. Watch `feedstats` across a hopper: `LastHomingSteps` creeping up means
   the wheel is drifting toward a jam.
6. Then per-slot centering: jog each chute over its funnel with the
   Machine tab's **Slot calibration** UI (each press moves the real arm).
