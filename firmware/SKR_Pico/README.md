# SortIQ sorter firmware — SKR Pico

The CS7.2 SortIQ fork, ported to the BigTreeTech SKR Pico (RP2040 with four
TMC2209s on board). Same serial protocol, function for function, so the
SortIQ app sees a fork board and needs no changes beyond pointing at the
new serial port. The board wiring it assumes is in
[`hardware/pico-controller/wiring.html`](../../hardware/pico-controller/wiring.html).

**Status: experiment.** Phase 1 = the stock feed-wheel + sort-disc layout
on the new controller, for validation. Phase 2 (the gated-wheel carousel)
is a design in progress and not in this firmware yet.

## What the new board adds

- **StallGuard jam detection** on both motors — the drivers' own load
  sensing, read at cruise speed. A stall aborts the move and answers
  `error:feed stall detected` / `error:sort stall detected`, which the app
  already treats as a jam (home + retry). A sort stall also cancels the
  feed queued behind it, since the arm's position is then unknown.
- **Motor power as a state.** The Pi stays on while the machine's 12 V is
  switched, so the drivers come and go under a running board. The firmware
  polls their UART presence when idle, re-applies every driver setting
  when power returns (and re-homes, positions being unknown), and refuses
  motion with `error:motor power off` while it's gone. `info:motor power
  on/off` lines announce the transitions; the app ignores `info:` lines.
- **WS2812 camera ring** on the RGB header (GPIO 24): `cameraledlevel:` sets the
  level exactly as before, `ledcolor:r,g,b` sets the mix. Driven by the
  RP2040's PIO — updates never disturb step timing.
- **Hardware UART to the drivers** — the Uno bit-banged it; the Pico asks
  the drivers real questions (`status`, `sgstats`).
- **Two hosts:** the Pi header UART (`Serial1`, 9600 baud) is the machine
  link; USB-C speaks the same protocol for bench work from a PC. Replies go
  to whichever port sent the command.

## Build and flash

```
pio run                       # needs PlatformIO; first run fetches the RP2040 toolchain
```

Flash: hold **BOOT** on the Pico, plug USB-C into any computer, release.
It mounts as a drive named `RPI-RP2`; copy `.pio/build/skr_pico/firmware.uf2`
onto it and it reboots into the new firmware. (`pio run -t upload` does the
same with the board already in BOOT mode.)

## The Pi side

On the Pi this board serves, the only changes are configuration:

1. Enable the GPIO UART (`enable_uart=1` in the boot config, serial console
   off) so `/dev/ttyAMA0` exists.
2. In SortIQ's `config.json`, set `serial.port` to `/dev/ttyAMA0`.

That's it — the app's firmware detection reads the version string and
treats this as a fork (SS2) board.

## Commands beyond the fork's

| Command | Does |
|---|---|
| `sgstats` | live `SG_RESULT` and DIAG levels, thresholds, stall counters, motor power |
| `sgfeed:<0-255>` / `sgsort:<0-255>` | stall thresholds — higher is more sensitive |
| `sg:0` / `sg:1` | StallGuard master switch |
| `sgprobe:1` | bench tuning: samples `SG_RESULT` mid-move, prints `sg:feed:min=..,avg=..` per move; `sgprobe:0` before sorting |
| `sgtcool:<n>` | driver-side speed gate (`TCOOLTHRS`); default always-armed, the software cruise gate does the work |
| `ledcolor:r,g,b` | the ring's color mix (white is `255,255,255`) |

`feedstats` gains `FeedStalls`/`SortStalls`; `getconfig` gains the stall
thresholds, `MotorPower`, and `"Board":"SKR_PICO"`.

## Tuning StallGuard

Start with `sgprobe:1` and a few test feeds and sorts to learn the healthy
`min` — the load of a normal move. Hold the feed wheel gently during a
test feed and watch `min` collapse. The threshold belongs between the
two with room on the healthy side; set it with `sgfeed:`/`sgsort:`, run a
few dozen cycles with `sgprobe:0`, confirm `sgstats` shows no false trips,
then bake the values into `board_skr_pico.h`.

## Layout

- `src/board_skr_pico.h` — pins and machine defaults (the only file the
  wiring page and this firmware both depend on)
- `src/main.cpp` — the fork, ported; lines tagged `[PICO]` mark where
  behavior had to differ from the Uno
