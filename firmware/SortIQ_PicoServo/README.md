# SortIQ-PicoServo 0.9.2

Firmware for the SKR Pico servo-carousel machine: a 12-station wheel on the
feed stepper, geared through a 25T pinion on the wheel's 72T ring gear
(9216 microsteps per wheel turn), and eight servo gate doors on a PCA9685
that the app drives over the Pi's I²C bus. It also runs the optional case
feeder on the same board. The sort-arm machine's firmware is
[SortIQ_Pico](../SortIQ_Pico/); both are supported.

**Ready to flash:** `SortIQ-PicoServo-0.9.2.uf2` in this folder. On the Pi,
`~/SortIQ/tools/flash_pico.sh carousel` flashes it; a new board needs the
BOOT jumper for its first flash. Building from source (PlatformIO,
`pio run`) is only needed to change the firmware, and the
[SortIQ_Pico library caveats](../SortIQ_Pico/README.md#library-caveats)
apply, including the `claimed_pios` local patch in `.pio/libdeps` (marker:
"SORTIQ LOCAL PATCH"), reapplied after any package refresh.

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

## Hardware map
- **Feed stepper:** X driver (step 11 / dir 10 / en 12), direction flag
  `true` for the geared mount. Wheel steps per turn are a setting
  (`feedwheelrev:`, app default 9216) kept in EEPROM.
- **Tab opto:** X-STOP (GPIO 4). 12 tabs on a 30° grid, 768 microsteps apart.
- **Case sensor:** WD-DET (GPIO 22) at the load station. Fit the P_S·IO22
  "Proximity Switch" jumper; remove the NPN jumper for a PNP sensor.
- **Gate doors:** PCA9685 at 0x40 on the **Pi's** I²C bus (SDA pin 3,
  SCL pin 5, VCC from the Pi's 3.3 V). The servo rail runs from its own 5 V
  supply into the PCA9685's V+ terminals, with a common ground. The app
  drives the doors; this firmware never touches them.
- **Doors:** servo channels 0–7 are gate stations 2–9. Each 180° servo turns
  a pinion that slides a rack-and-pinion door. The app's defaults are
  **900 µs = closed** and **2150 µs = open**; per-door values are a machine
  setting (`gate_pulses`), tuned on the Machine tab. A door is driven to its
  position and then released, and the geared linkage holds it, so no servo
  carries current at rest.
- **LED ring:** driven by the **Pi** (GPIO 18 hardware PWM, 20 kHz,
  `dtoverlay=pwm` plus `ring-pwm.service` to re-arm it at boot), not this
  board. `cameraledlevel:` is accepted and does nothing.
- **Sort axis:** none, and the empty Y driver socket is never touched. The
  sort-arm setters are still accepted and echoed by `getconfig`, because the
  app pushes one setter list to every board.
- **Case feeder (optional):** motor on the **E** driver (step 14 / dir 13 /
  en 15, TMC address 3, 950 mA, StealthChop). Fit the **E0-DIAG** jumper:
  the driver's stall flag reaches GPIO 16 through it, for jam detection.
  The feeder's break-beam receiver goes on **Z-STOP** (GPIO 25) with the
  **Z-DIAG** jumper removed. Every feeder output stays off until the app
  says a feeder is fitted, so a board without one is unaffected.
- **Fans:** FAN1 (GPIO 17) and FAN2 (GPIO 18) cool the controller case,
  FAN3 (GPIO 20) is the camera fan. All three run at full speed from boot.
- **AirDrop mod (optional):** HE0, GPIO 23, off at boot.
- **Board link:** the TFT header's UART (GPIO 0 / 1) at 9600 to the Pi; the
  USB port mirrors it at 9600. The onboard drivers are on UART2.

Door testing and tuning live in the app (Machine tab, Gate doors).

## Bench commands

Typed in the app's Console drawer, or over the USB serial port. None of
them is needed to run the machine.

| Command | What it does |
|---|---|
| `ping` | Answers `ok` |
| `version` | Firmware version |
| `getconfig` | Every setting as JSON |
| `diag` | One line of state: wheel position and grid anchor, fault code, link counters |
| `sensors` | Tab opto and case sensor readings, plus motor power |
| `status` | The feed driver's microstepping, current and chopper mode |
| `feedstats` | Feed cycle and homing counters |
| `feeddebug:1` / `feeddebug:0` | One trace line per feed cycle, on or off |
| `feedjog:<usteps>` | Open-loop wheel jog, no sensors: tape-mark the wheel and jog until the mark closes to measure wheel steps per turn |
| `debounceTime:<ms>` | Case-sensor confirm time (default 100) |
| `launchsettle:<ms>` | Pause before each feed move (default 0) |
| `fan:<0-100>` | FAN2 speed (full speed from boot) |
| `homefeeder` | Home the wheel |
| `homefeeder:soft` | Home the wheel only if it isn't already on a tab |
| `stop` | Stop the wheel (a feed in motion finishes at its rest position first) |
| `feedwheelrev:<usteps>` | Wheel microsteps per turn (2000–20000), kept in EEPROM |
| `feedhomingoffset:<steps>` | Feed homing offset (0–30), kept in EEPROM |
| `faultclear` | Clear a fault after fixing its cause |
| `bootloader` | Reboot into the UF2 bootloader (what `flash_pico.sh` uses) |

Every other command is a setting the app sends on connect; `getconfig`
shows them all.

## Case feeder

The feeder runs on its own: it feeds until a case backs up to the beam,
holds while the case waits, and starts again when the case is taken. A
jam it senses reverses the plate to clear it, and an empty bowl stops it.
The app switches it on and off, sets the speed, and sends its settings on
connect (Machine tab, Hardware → **Case feeder**, then the Case feeder
card).

- **Hold:** the beam blocked for 250 ms holds the feeder; 1.5 s clear
  starts it again.
- **Jam:** a stall lasting 450 ms reverses the plate for 2.3 s, pauses
  0.4 s and feeds again. A second jam within 2 s of recovering is a fault:
  the feeder stops until it is cleared.
- **Clearing a fault or a shutoff** from the feeder: break the beam twice
  within 2 s.
- **Board off:** with the 24 V off the driver drops out, and the feeder
  status shows `"driver":0`.
- **E0-DIAG jumper missing:** the status shows `"diag":0`, and jam
  detection falls back to polling the driver's stall reading.

Every feeder line, in or out, starts with `feeder:`. A command replies
`feeder:ok` unless noted; for
on/off values `1`, `on` and `true` mean on.

| Command | What it does |
|---|---|
| `feeder:status` | One line, `feeder:status {json}`. Answers even with the feeder disabled |
| `feeder:enable:<0\|1>` | Tells the board a feeder is fitted. Until it is on, every other command answers `feeder:error:not enabled` |
| `feeder:run:<0\|1>` | Start or stop feeding |
| `feeder:speed:<1-100>` | Feed speed, percent |
| `feeder:clear` | Clear a fault or a shutoff |
| `feeder:warn:<s>` | Seconds with no case past the beam before `feeder:alert:empty` (0 = off, default 30) |
| `feeder:shutoff:<s>` | Seconds with no case before the feeder stops, `feeder:alert:shutoff` (0 = never, default 120) |
| `feeder:reversed:<0\|1>` | Reverse the motor direction |
| `feeder:beamhigh:<0\|1>` | 1 for a beam receiver that reads HIGH with a case in it (default 0: LOW = blocked) |
| `feeder:jamdetect:<0\|1>` | Jam detection on or off (default on) |
| `feeder:band:<0-2>:<0-255>` | Stall threshold for one speed band (defaults 140, 150, 160) |

The board also sends `feeder:state:<STOPPED|RUNNING|HOLD|JAM_REVERSE|JAM_PAUSE|FAULT|IDLE_SHUTOFF>`
on every change, and `feeder:alert:<empty|shutoff|jam|driver|hot:n>` when
something needs attention.
