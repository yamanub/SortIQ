// SortIQ sorter on the BigTreeTech SKR Pico — board pin map and machine
// defaults. GPIO numbers are BTT's published map (mirrors Klipper's
// generic-bigtreetech-skr-pico config). The wiring these assume is in
// hardware/pico-controller/wiring.html.
#pragma once

#define FIRMWARE_VERSION "7.2.250925.6.3-SS2-PICO"

// ---- host link -------------------------------------------------------------
// Serial1 = UART0 on GPIO0 (TX) / GPIO1 (RX) = the board's "Raspberry Pi"
// header. 9600 baud keeps parity with the stock firmware (cs72.py FW_BAUD).
// USB-C (Serial) carries the same protocol for bench work from a PC.
#define HOST_BAUD 9600

// ---- stepper drivers (onboard TMC2209, one shared UART) --------------------
// Serial2 = UART1 on GPIO8 (TX) / GPIO9 (RX): the board's driver bus.
#define DRIVER_BAUD 115200
#define R_SENSE 0.110f
#define FEED_DRIVER_ADDR 0b00        // X socket
#define SORT_DRIVER_ADDR 0b10        // Y socket

#define FEED_STEPPIN 11              // X
#define FEED_DIRPIN 10
#define FEED_ENABLE 12
#define FEED_DIAG_PIN 4              // X-STOP (DIAG jumper set)

#define SORT_STEPPIN 6               // Y
#define SORT_DIRPIN 5
#define SORT_ENABLE 7
#define SORT_DIAG_PIN 3              // Y-STOP (DIAG jumper set)

#define FEED_CURRENT 900             // mA RMS
#define SORT_CURRENT 900
#define FEED_MICROSTEPS 16
#define SORT_MICROSTEPS 16
#define FEED_IN_REVERSE false
#define SORT_IN_REVERSE false

// ---- sensors ---------------------------------------------------------------
#define FEED_HOMING_SENSOR 16        // E0-STOP — slotted optical, powered 3.3V
#define FEED_HOMING_SENSOR_TYPE 0    // 0 = NC (optical) reads LOW when homed
#define SORT_HOMING_SENSOR 25        // Z-STOP
#define SORT_HOMING_SENSOR_TYPE 0
#define FEED_SENSOR 22               // WD-DET header: the 12/24V PNP prox, DIRECT. The board
                                     // conditions the probe line itself: set the "Jumper for
                                     // Proximity Switch" (Vcc = VIN), REMOVE the NPN jumper (/BY = PNP).
#define FEEDSENSOR_ENABLED true
#define FEEDSENSOR_TYPE 1            // direct PNP: brass present pulls the line HIGH.
                                     // (runtime feedsensortype: flips it if the bench says otherwise)
#define FEED_HOMING_ENABLED true
#define SORT_HOMING_ENABLED true

// ---- outputs ---------------------------------------------------------------
#define CASEFAN_PWM 18               // FAN2 (VIN, MOSFET) — the 60 mm case fan
#define AUX_FAN 17                   // FAN1 (VIN, MOSFET) — Pi/Pico cooling fan 1, on at boot
#define AUX_FAN2 20                  // FAN3 (VIN, MOSFET) — Pi/Pico cooling fan 2, on at boot
#define CASEFAN_LEVEL 100            // 0-100
#define CASEFAN_SW_CTRL false
#define FEED_DONE_SIGNAL 23          // HE0 — AirDrop solenoid (optional mod)
#define LED_RING_PIN 24              // RGB header — WS2812 data (5V + GND on the same plug)
// phase-2 I2C1 for the PCA9685: SDA = GPIO26 (TH0), SCL = GPIO27 (THB) — both exposed
// analog headers, unused here, with on-board pull-ups to 3.3V (made for this)
#define LED_RING_COUNT 12
#define CAMERA_LED_LEVEL 200         // 0-255 white level at boot

// ---- machine defaults (same names/values as the fork) ----------------------
#define FEED_HOMING_OFFSET_STEPS 5
#define FEED_STEPS 60
#define FEED_OVERSTEP_THRESHOLD 140
#define FEED_MOTOR_SPEED 90
#define FEED_ACC_SLOPE 32
#define ACC_FEED_ENABLED false
#define SORT_HOMING_OFFSET_STEPS 0
#define SORT_MOTOR_SPEED 94
#define ACC_SORT_ENABLED true
#define ACC_FACTOR 1200
#define SORT_ACC_SLOPE 64
#define AIR_DROP_ENABLED false
#define AUTO_MOTORSTANDBY_TIMEOUT 0
#define SORTER_CHUTE_SEPERATION 20
#define MAX_SLOTS 12
#define SORT_HOME_BACKOFF 160
#define SORT_HOME_SLOW_DELAY 1400
#define FEED_LAUNCH_STEPS 48
#define FEED_DECEL_OVER_OFFSET true
#define FEED_CYCLE_COMPLETE_SIGNALTIME 100
#define FEED_CYCLE_COMPLETE_PRESIGNALDELAY 30
#define FEED_CYCLE_NOTIFICATION_DELAY 120
#define FEED_CYCLE_COMPLETE_POSTDELAY 100
#define SLOT_DROP_DELAY 550
#define DEBOUNCE_TIMEOUT 300
#define DEBOUNCE_PAUSE_TIME 500

// ---- StallGuard jam detection ---------------------------------------------
#define STALLGUARD_ENABLED true
#define FEED_SGTHRS 40               // 0-255, higher = more sensitive; tune with sgprobe
#define SORT_SGTHRS 40
#define SG_TCOOLTHRS 0xFFFFF
#define SG_TRIP_HITS 64               // DIAG must stay high for 4 full steps (~10 ms at cruise)
#define SG_ARM_STEPS 128              // 8 full steps of cruise: SG_RESULT reads ~0 that long after a standstill

// ---- motor power as a state ------------------------------------------------
// The Pi stays on while the 12V is switched: the drivers come and go under
// a running board. Poll their UART presence when idle and re-apply config
// when power returns.
#define SORT_HOME_SEEK_US 400        // us/ustep on homing seeks: the Uno's loop
                                     // overhead used to ramp this implicitly; at real
                                     // cruise speed from standstill the arm stalls
#define POWER_POLL_MS 1000
