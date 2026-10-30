// SortIQ-PicoServo — optional case feeder on the same SKR Pico.
//
// The case feeder (a bowl plate on a 51:1 geared NEMA 17) runs on the E
// driver; its break-beam sits at the feeder's outlet. It feeds until a case
// backs up to the beam (HOLD), resumes when the case is taken, reverses to
// clear a StallGuard jam, and stops on an empty bowl. All of that runs here;
// the app only switches it on and off and sets the speed.
//
// Wiring: motor on E (STEP 14, DIR 13, EN 15, TMC address 3), E0-DIAG
// jumper fitted (stall flag on GPIO 16), beam receiver on Z-STOP (GPIO 25)
// with Z-DIAG removed.
//
// Protocol: every line in or out starts with "feeder:", so a run's line
// reader can route them aside and never mistakes one for a sort reply.
#pragma once
#include <Arduino.h>

void feederSetup();
void feederLoop();
// true when `line` was a feeder command (handled, reply sent)
bool feederCommand(const String &line);

// provided by main.cpp: one line to the app (and the USB mirror)
void feederEmit(const String &line);
// provided by main.cpp: true while the wheel is stopped. The feeder only
// talks to its driver then; the shared driver bus stays quiet mid-move.
bool wheelAtRest();
