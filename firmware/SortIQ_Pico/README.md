# SortIQ-Pico 2.0

The rewrite: FastAccelStepper (PIO step generation) under the same serial
protocol as the CS7.2 fork / 1.x port. The app is unchanged.

Build: PlatformIO, `pio run`. Flash: BOOT jumper + UF2 to the RPI-RP2 drive,
or the 1200-baud touch on the USB CDC port.

## Architecture
- Motion on the RP2040 PIO via FastAccelStepper: the CPU never bit-bangs;
  serial, sensors and TMC UART reads cannot disturb step timing.
- Flag-relative coordinates: homing MEASURES each flag's absolute position;
  the library position counter is never written (its writes are async).
- Sort jam detection: the FLAG AUDIT — seeing the home flag far from where
  the frame says it belongs aborts the move and re-homes (bench-proven to
  catch jams StallGuard reads straight through). StallGuard on the sort is
  telemetry only.
- Feed jam detection: the 1.x two-stage detector — the driver's DIAG pin is
  a free tripwire (asserts when SG_RESULT < 2*SGTHRS); 24 accumulated highs
  buy ONE UART confirm read, three low confirms = jam — plus the flag-seek
  overtravel budget. Continuous UART sampling is wrong twice: it false-trips
  on transient dips, and each read blocks the loop ~10 ms.
- Feed flag edge by INTERRUPT with latency-corrected position (edge =
  pos_now − elapsed_µs × cruise rate), and host TX is ring-buffered and
  pumped from loop(). Both exist for the same reason, the core lesson of
  the rewrite: **the PIO keeps stepping while the CPU blocks.** On the 1.x
  core a blocked loop also froze the motor, so polled sensing stayed in
  lockstep with motion; here a 9600-baud print to the Pi (100+ ms once the
  FIFO fills) or a TMC read lets the wheel sail past the flag. Bench
  signature: seeks of ~920 µsteps with ±100 jitter instead of the true
  0–150. Nothing may write a host port directly, and no polled edge may be
  trusted without the ISR timestamp.
- The feed stop never reverses: if the configured decel cannot rest within
  what remains of the offset, braking is raised to fit (FastAccelStepper
  reverses on moveTo overshoot — unacceptable across the drop port).

## Library caveats (bench-bisected)
1. **FastAccelStepper 0.31 rp2040: `claimed_pios` is never written.** The
   first stepper's PIO claim is not recorded, so a second stepper demands a
   whole fresh PIO (and the NeoPixel ring holds the other one) and fails to
   attach. Local patch required in
   `.pio/libdeps/skr_pico/FastAccelStepper/src/StepperISR_rp_pico.cpp`,
   fresh-claim success path:

   ```c
   if (rc) {
     engine->pio[engine->claimed_pios] = pio;
     engine->claimed_pios++;
   }
   ```

   Reapply after any `pio pkg` refresh. Worth an upstream issue/PR.
2. **The PIO claim stomps GPIO 0's pin mux** (UART0 TX — the Pi machine
   link). RX stays alive, TX dies silently at the first
   `stepperConnectToPin`. Our `setup()` re-muxes GPIO 0/1 back to UART
   after the steppers attach — do not remove that block.
3. FAS position writes (`setCurrentPosition`, `forceStopAndNewPosition`)
   apply asynchronously and unreliably at bench timing — hence the
   flag-relative frame. Measure positions, never write them.
4. Needs `-D__FREERTOS=1` (the rp2040 backend runs its engine task on
   FreeRTOS) and the maxgerhardt platform (the standard pico platform's PIO
   support does not work with this library).
