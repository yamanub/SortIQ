# SortIQ-Pico 2.8

Firmware for the SKR Pico sort-arm machine: a feed wheel and a stepper sort
arm, driven with FastAccelStepper (PIO step generation) under the same
serial protocol as the CS7.2 fork. The app drives it like any other board.

**Ready to flash:** `SortIQ-Pico-2.8.uf2` in this folder. On the Pi,
`~/SortIQ/tools/flash_pico.sh arm` flashes it; a new board needs the BOOT
jumper for its first flash. Building from source (PlatformIO, `pio run`) is
only needed to change the firmware, and needs the local library patch under
[Library caveats](#library-caveats).

**Flashing notes** (full procedure: [docs/PICO_MACHINE_SETUP.md](../../docs/PICO_MACHINE_SETUP.md))
- The board's logic runs off **24 V, not USB**. With the 24 V off (relay off,
  if fitted) it never enumerates, however the USB cable is plugged.
- A board already running SortIQ needs **no jumper**: `flash_pico.sh` asks it
  to reboot into the bootloader over the link. A blank board can't be asked:
  fit the BOOT jumper, power on, flash, then **remove the jumper**, or the
  post-flash reset lands back in the bootloader instead of the firmware.
- A fresh board's EEPROM is empty, so its first boot homes on the compile
  defaults and may throw a feed error. The app's connect push writes the real
  values, the firmware persists them, and later boots home correctly.

## Wiring & GPIO map

| Connection | SKR Pico port | GPIO | Signal |
|---|---|---|---|
| Feed wheel motor | **X** driver output | step 11 / dir 10 / en 12 | onboard TMC2209, UART addr 0 |
| Sort arm motor | **Y** driver output | step 6 / dir 5 / en 7 | onboard TMC2209, UART addr 2 |
| Feed homing opto | **X-STOP** | 4 | input, pull-up — LOW = blocked |
| Sort homing opto | **Y-STOP** | 3 | input, pull-up — LOW = blocked |
| Brass/case sensor | **WD-DET** | 22 | input, pull-up — HIGH = brass present; needs the P_S·IO22 "Proximity Switch" jumper fitted, NPN jumper out for a PNP sensor |
| LED ring MOSFET module — trigger | **the Pi's GPIO 18** | — | 20 kHz hardware PWM from the app, not this board |
| LED ring MOSFET module — power in | **E0-STOP** header, 5 V + GND | — | supply only |
| Controller case fans | **FAN1**, **FAN2** | 17, 18 | full speed from boot |
| Camera fan | **FAN3** | 20 | full speed from boot |
| AirDrop mod (optional) | **HE0** | 23 | pulse output, off by default |
| Pi machine link (UART, 9600) | **TFT** header | 0 (TX) / 1 (RX) | wire to Pi RXD/TXD + GND only — no 5 V to the Pi, it has its own supply |
| TMC driver telemetry (internal) | — | 8 / 9 | UART2 to the onboard drivers, 460800 |
| Unused | RGB, E0-STOP signal, Z-STOP | 24, 16, 25 | — |

The Pi drives the LED ring (`dtoverlay=pwm,pin=18,func=2` + `ring-pwm.service`,
see [docs/PICO_MACHINE_SETUP.md](../../docs/PICO_MACHINE_SETUP.md)), so the
LED ring is wired the same way on both builds. `cameraledlevel:` is
accepted and echoed by `getconfig` but drives nothing.

**Required:** pull BOTH the X and Y DIAG jumpers. StallGuard is telemetry
only, and a fitted DIAG jumper interferes with the endstop inputs the homing
optos share.

## How it works
- Motion runs on the RP2040 PIO via FastAccelStepper. The CPU never
  bit-bangs steps, so serial, sensors and TMC UART reads cannot disturb step
  timing.
- Flag-relative coordinates: homing MEASURES each flag's absolute position;
  the library's position counter is never written (its writes are async).
- Sort jam detection: the flag audit. Seeing the home flag far from where
  the frame says it belongs aborts the move and re-homes. StallGuard on the
  sort axis is telemetry only.
- Feed jam detection: the flag-seek overtravel budget. Two pitches past the
  blind with no tab is a jam.
- The feed flag edge is taken by INTERRUPT with a latency-corrected position
  (edge = pos_now − elapsed_µs × cruise rate), and host output is
  ring-buffered and pumped from `loop()`. The PIO keeps stepping while the
  CPU blocks, so a blocking 9600-baud print or a TMC read would let the wheel
  sail past the flag. Nothing may write a host port directly, and no polled
  edge is trusted without the interrupt timestamp.
- The feed stop never reverses: if the configured decel cannot rest within
  what remains of the offset, braking is raised to fit (FastAccelStepper
  reverses on a moveTo overshoot, which is unacceptable across the drop port).

## Bench commands

Typed in the app's Console drawer, or over the USB serial port. None of
them is needed to run the machine.

| Command | What it does |
|---|---|
| `version` | Firmware version |
| `getconfig` | Every setting as JSON |
| `diag` | One line of state: feed and sort positions, fault code, link counters |
| `sensors` | Feed opto, sort opto and case sensor readings, plus motor power |
| `status` | Both drivers' microstepping, current and chopper mode |
| `feedstats` | Feed cycle, homing and sort-stall counters |
| `feeddebug:1` / `feeddebug:0` | One trace line per feed cycle, on or off |
| `debounceTime:<ms>` | Case-sensor confirm time (default 100) |
| `launchsettle:<ms>` | Pause before each feed move (default 0) |
| `fan:<0-100>` | FAN2 speed (full speed from boot) |
| `homefeeder`, `homesorter` | Home the feed wheel or the sort arm |
| `faultclear` | Clear a fault after fixing its cause |
| `bootloader` | Reboot into the UF2 bootloader (what `flash_pico.sh` uses) |

## Library caveats
1. **FastAccelStepper 0.31 rp2040: `claimed_pios` is never written.** The
   first stepper's PIO claim is not recorded, so a second stepper demands a
   fresh PIO and can fail to attach. Local patch required in
   `.pio/libdeps/skr_pico/FastAccelStepper/src/StepperISR_rp_pico.cpp`,
   fresh-claim success path:

   ```c
   if (rc) {
     engine->pio[engine->claimed_pios] = pio;
     engine->claimed_pios++;
   }
   ```

   Reapply after any `pio pkg` refresh.
2. **The PIO claim overwrites GPIO 0's pin mux** (UART0 TX, the Pi machine
   link). RX stays alive, TX goes silent at the first
   `stepperConnectToPin`. `setup()` re-muxes GPIO 0/1 back to UART after the
   steppers attach; do not remove that block.
3. FastAccelStepper position writes (`setCurrentPosition`,
   `forceStopAndNewPosition`) apply asynchronously and unreliably, hence
   the flag-relative frame. Measure positions, never write them.
4. Needs `-D__FREERTOS=1` (the rp2040 backend runs its engine task on
   FreeRTOS) and the maxgerhardt platform (the standard pico platform's PIO
   support does not work with this library).
