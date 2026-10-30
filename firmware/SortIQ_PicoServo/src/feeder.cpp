// SortIQ-PicoServo — case feeder module. See feeder.h.
//
// The motion, beam and jam logic is the Case Feeder Pro controller's case
// feeder, unchanged in behavior: the same speeds, ramps, StallGuard bands,
// beam debounce, jam reverse, empty warning and idle shutoff, all validated
// on that machine with the 51:1 gearbox at 0.95 A.
//
// Step pulses come from a hardware PWM slice on the STEP pin, so they never
// touch the wheel's PIO stepper and keep running steadily whatever the loop
// is doing.

#include "feeder.h"
#include <TMCStepper.h>
#include "hardware/pwm.h"
#include "hardware/clocks.h"
#include "hardware/gpio.h"

extern bool motorPower;              // main.cpp: the drivers have power

// ---------------- pins (SKR Pico) ----------------
static constexpr uint8_t PIN_STEP = 14;   // E driver
static constexpr uint8_t PIN_DIR  = 13;
static constexpr uint8_t PIN_EN   = 15;
static constexpr uint8_t PIN_DIAG = 16;   // E0-STOP via the E0-DIAG jumper
static constexpr uint8_t PIN_BEAM = 25;   // Z-STOP, Z-DIAG removed
static constexpr uint8_t DRV_ADDR = 3;

// ---------------- driver ----------------
static constexpr float    R_SENSE           = 0.110f;
static constexpr uint16_t MICROSTEPS        = 16;
static constexpr uint16_t RMS_CURRENT_MA    = 950;   // the jam bands are set at this current
static constexpr uint32_t TCOOLTHRS_VALUE   = 0xFFFFF;

// ---------------- tuning (Case Feeder Pro, case feeder, 51:1) ----------------
static constexpr uint32_t SPEED_MIN_HZ      = 7868;
static constexpr uint32_t SPEED_MAX_HZ      = 12875;
static constexpr uint32_t ACCEL_HZ_S        = 143051;
static constexpr uint32_t STARTUP_ACCEL_HZ_S = 35763;
static constexpr uint32_t DECEL_HZ_S        = 28610;  // gentle: a hard stop dumps regen into VM
static constexpr uint32_t JAM_REVERSE_HZ    = 10729;
static constexpr uint32_t JAM_REVERSE_MS    = 2300;
static constexpr uint32_t JAM_MIN_SPEED_HZ  = 4292;
static constexpr uint32_t JAM_PAUSE_MS      = 400;
static constexpr uint16_t JAM_REPEAT_MS     = 2000;   // a jam this soon after a recovery = FAULT
static constexpr uint16_t STALL_CONFIRM_MS  = 450;
static constexpr uint16_t STALL_SETTLE_MS   = 300;
static constexpr uint32_t STALL_CLEAR_MS    = 150;
static constexpr uint32_t STEALTH_CALIBRATE_MS = 150; // StealthChop settles before stepping
static constexpr uint32_t RAMP_PERIOD_MS    = 10;
static constexpr uint16_t BEAM_CONFIRM_MS   = 250;
static constexpr uint16_t BEAM_CLEAR_MS     = 1500;
static constexpr uint16_t COUNT_CLEAR_MS    = 30;
static constexpr uint8_t  BEAM_RESET_BREAKS = 2;      // two quick breaks resume a stop
static constexpr uint32_t BEAM_RESET_TIMEOUT_MS = 2000;
static constexpr uint32_t SG_SAMPLE_MS      = 100;
static constexpr uint32_t DRIVER_RECHECK_MS = 2000;  // health check, at rest only
static constexpr uint32_t TEMP_SAMPLE_MS    = 5000;
static constexpr uint8_t  DEFAULT_SPEED_PCT = 60;

struct StallBand { uint32_t speedMinHz; uint8_t threshold; };
// The TMC2209 flags a stall when SG_RESULT drops below 2 x SGTHRS; the
// reading rises with speed, so each speed range has its own threshold.
static StallBand bands[3] = { { 0, 140 }, { 9298, 150 }, { 10729, 160 } };
static constexpr uint8_t NUM_BANDS = 3;

enum FState : uint8_t { S_STOPPED, S_RUNNING, S_HOLD, S_JAM_REVERSE, S_JAM_PAUSE,
                        S_FAULT, S_IDLE_SHUTOFF };
static const char *stateName(FState s) {
  switch (s) {
    case S_STOPPED:      return "STOPPED";
    case S_RUNNING:      return "RUNNING";
    case S_HOLD:         return "HOLD";
    case S_JAM_REVERSE:  return "JAM_REVERSE";
    case S_JAM_PAUSE:    return "JAM_PAUSE";
    case S_FAULT:        return "FAULT";
    case S_IDLE_SHUTOFF: return "IDLE_SHUTOFF";
  }
  return "?";
}

static TMC2209Stepper drv(&Serial2, R_SENSE, DRV_ADDR);

// ---------------- state ----------------
static bool     enabled = false;          // the app says a feeder is fitted
static FState   state = S_STOPPED, reportedState = S_STOPPED;
static uint32_t stateSince = 0;
static bool     runRequested = false;
static uint8_t  speedPct = DEFAULT_SPEED_PCT;
static bool     motorReversed = false, beamActiveLow = true, jamDetect = true;
static uint32_t idleWarnMs = 30000, idleShutoffMs = 120000;

static bool     driverReady = false, motorPowered = false, useSlowStart = true;
static bool     diagUsable = false, offlineLogged = false;
static uint32_t appliedHz = 0, targetHz = 0;
static int8_t   appliedDirLevel = -1;
static uint32_t motorEnabledAt = 0, lastRampAt = 0;
static uint     stepSlice = 0, stepChannel = 0;

static bool     beamBlocked = false, rawBeam = false;
static uint32_t beamBrokenSince = 0, beamClearedSince = 0, beamClearSince = 0;
static bool     countArmed = true, prevBeamRunning = false;
static uint32_t count = 0, lastFedTime = 0;
static bool     idleWarnActive = false;
static uint8_t  beamResetBreaks = 0;
static uint32_t beamResetStart = 0;
static bool     prevBeamState = false;

static bool     stallActive = false;
static uint32_t stallTimer = 0, lastStallEvent = 0, stallSettleStart = 0, lastRecoveryEnd = 0;
static uint16_t sgResult = 0;
static int8_t   currentBand = -1;
static uint8_t  appliedThr = 0;
static uint32_t jams = 0, faults = 0;

static uint32_t lastDriverCheck = 0, lastSgRead = 0, lastTempAt = 0;
static uint8_t  resetReads = 0;
static uint8_t  tempLevel = 0, tempPending = 0;   // 0 ok .. 5 overtemp shutdown

// ============================================================
//  motor: step pulses from a hardware PWM slice
// ============================================================
static void stepPwmInit() {
  gpio_set_function(PIN_STEP, GPIO_FUNC_PWM);
  stepSlice = pwm_gpio_to_slice_num(PIN_STEP);
  stepChannel = pwm_gpio_to_channel(PIN_STEP);
  pwm_set_chan_level(stepSlice, stepChannel, 0);
  pwm_set_enabled(stepSlice, true);
}

static void setStepFrequency(uint32_t hz) {
  if (hz == appliedHz) return;
  appliedHz = hz;
  if (hz == 0) { pwm_set_chan_level(stepSlice, stepChannel, 0); return; }
  const uint32_t sysHz = clock_get_hz(clk_sys);
  uint32_t div = sysHz / (hz * 65536UL) + 1;
  if (div > 255) div = 255;
  uint32_t top = sysHz / (div * hz);
  if (top < 2) top = 2;
  if (top > 65536) top = 65536;
  pwm_set_clkdiv_int_frac(stepSlice, (uint8_t)div, 0);
  pwm_set_wrap(stepSlice, (uint16_t)(top - 1));
  pwm_set_chan_level(stepSlice, stepChannel, (uint16_t)(top / 2));
}

static void motorEnable(bool on) {
  if (on) {
    if (!motorPowered) {
      motorPowered = true;
      motorEnabledAt = lastRampAt = millis();
      setStepFrequency(0);
      digitalWrite(PIN_EN, LOW);
    }
  } else {
    targetHz = 0;
    setStepFrequency(0);
    digitalWrite(PIN_EN, HIGH);
    motorPowered = false;
  }
}

static void stopImmediately() { motorEnable(false); useSlowStart = true; }

static void setDirection(bool forward) {
  const int8_t level = (forward != motorReversed) ? HIGH : LOW;
  if (level == appliedDirLevel) return;
  appliedDirLevel = level;
  digitalWrite(PIN_DIR, level);
}

static uint32_t selectedStepHz() {
  return SPEED_MIN_HZ + ((SPEED_MAX_HZ - SPEED_MIN_HZ) * speedPct) / 100U;
}

// walk the step rate toward the target: a gentle ramp from a standstill,
// the normal ramp otherwise, and a gentle deceleration
static void serviceRamp() {
  if (!motorPowered) return;
  const uint32_t now = millis();
  if (now - motorEnabledAt < STEALTH_CALIBRATE_MS) { lastRampAt = now; return; }
  if (appliedHz == targetHz) { lastRampAt = now; return; }
  uint32_t dt = now - lastRampAt;
  if (dt < RAMP_PERIOD_MS) return;
  if (dt > 100) dt = 100;
  lastRampAt = now;
  const bool decel = targetHz < appliedHz;
  const uint32_t rate = decel ? DECEL_HZ_S : (useSlowStart ? STARTUP_ACCEL_HZ_S : ACCEL_HZ_S);
  uint32_t delta = (uint32_t)(((uint64_t)rate * dt) / 1000U);
  if (delta == 0) delta = 1;
  const uint32_t prevHz = appliedHz;
  uint32_t next = appliedHz;
  if (next < targetHz) next += min(delta, targetHz - next);
  else                 next -= min(delta, next - targetHz);
  setStepFrequency(next);
  if (prevHz <= JAM_MIN_SPEED_HZ && appliedHz > JAM_MIN_SPEED_HZ) stallSettleStart = now;
  if (useSlowStart && appliedHz >= targetHz) useSlowStart = false;
}

static void serviceStallThreshold() {
  if (!driverReady) return;
  int8_t band = 0;
  for (int8_t i = NUM_BANDS - 1; i >= 0; i--) {
    if (appliedHz >= bands[i].speedMinHz) { band = i; break; }
  }
  if (band == currentBand && bands[band].threshold == appliedThr) return;
  currentBand = band;
  appliedThr = bands[band].threshold;
  drv.SGTHRS(appliedThr);
}

// a stall shows on the DIAG line, instantly and with no bus traffic; the
// polled reading is only the fallback for a board without the jumper
static bool isStalled() {
  if (diagUsable) return digitalRead(PIN_DIAG) == HIGH;
  const uint16_t trip = (uint16_t)appliedThr * 2U;
  return sgResult > 0 && sgResult < trip;
}

// ============================================================
//  beam
// ============================================================
static bool readBeamRaw() {
  const bool low = digitalRead(PIN_BEAM) == LOW;
  return beamActiveLow ? low : !low;
}

// two quick breaks resume a fault or an idle shutoff from the machine
static bool beamResetGesture() {
  const bool blocked = readBeamRaw();
  bool done = false;
  if (blocked && !prevBeamState) {
    if (beamResetBreaks == 0) beamResetStart = millis();
    if (++beamResetBreaks >= BEAM_RESET_BREAKS) { beamResetBreaks = 0; done = true; }
  }
  prevBeamState = blocked;
  if (beamResetBreaks > 0 && millis() - beamResetStart > BEAM_RESET_TIMEOUT_MS) beamResetBreaks = 0;
  return done;
}

static void serviceBeam() {
  const bool blocked = readBeamRaw();
  rawBeam = blocked;
  // count each case once, on the leading edge of a break
  if (blocked && !prevBeamRunning) {
    lastFedTime = millis();
    idleWarnActive = false;
    if (countArmed) { countArmed = false; count++; }
  }
  if (!blocked) {
    if (beamClearSince == 0) beamClearSince = millis();
    if (!countArmed && millis() - beamClearSince >= COUNT_CLEAR_MS) countArmed = true;
  } else {
    beamClearSince = 0;
  }
  prevBeamRunning = blocked;
  // HOLD only after a sustained break, released only after a sustained clear
  if (blocked) {
    beamClearedSince = 0;
    if (beamBrokenSince == 0) beamBrokenSince = millis();
    else if (!beamBlocked && millis() - beamBrokenSince >= BEAM_CONFIRM_MS) beamBlocked = true;
  } else {
    beamBrokenSince = 0;
    if (beamBlocked) {
      if (beamClearedSince == 0) beamClearedSince = millis();
      else if (millis() - beamClearedSince >= BEAM_CLEAR_MS) { beamBlocked = false; beamClearedSince = 0; }
    }
  }
}

// ============================================================
//  driver (TMC2209 on the shared UART)
// ============================================================
static bool initDriver() {
  driverReady = false;
  drv.begin();
  if (drv.test_connection() != 0) return false;
  drv.pdn_disable(true);
  drv.mstep_reg_select(true);
  drv.I_scale_analog(false);
  drv.rms_current(RMS_CURRENT_MA, 1.0f);
  drv.microsteps(MICROSTEPS);
  drv.en_spreadCycle(false);          // StealthChop: StallGuard4 only works here
  drv.pwm_autoscale(true);
  drv.pwm_autograd(true);
  drv.TCOOLTHRS(TCOOLTHRS_VALUE);
  drv.semin(0);
  drv.toff(3);
  appliedThr = bands[0].threshold;
  drv.SGTHRS(appliedThr);
  currentBand = 0;
  delay(20);
  if (drv.SGTHRS() != appliedThr) return false;
  drv.GSTAT(0x07);
  // DIAG idles LOW; HIGH at standstill = the E0-DIAG jumper is missing, and
  // jam detection falls back on the polled reading alone
  diagUsable = digitalRead(PIN_DIAG) == LOW;
  driverReady = true;
  return true;
}

// reconfigure a driver that lost its settings (GSTAT reset flag, read twice
// to be sure) or never answered
static void checkDriverHealth() {
  if (millis() - lastDriverCheck < DRIVER_RECHECK_MS) return;
  lastDriverCheck = millis();
  uint8_t gstat = 0;
  if (driverReady) gstat = drv.GSTAT();
  if (driverReady && (gstat & 0x01)) {
    if (++resetReads < 2) return;
  } else {
    resetReads = 0;
  }
  if ((gstat & 0x01) || !driverReady) {
    resetReads = 0;
    if (driverReady) feederEmit(F("feeder:info:driver reset - reconfiguring"));
    stopImmediately();
    driverReady = false;
    if (initDriver()) {
      offlineLogged = false;
    } else if (!offlineLogged) {
      offlineLogged = true;
      feederEmit(F("feeder:alert:driver"));
    }
  }
}

// the TMC2209 reports temperature thresholds only; a change must read the
// same twice before it counts
static void serviceDriverTemp() {
  if (!driverReady || millis() - lastTempAt < TEMP_SAMPLE_MS) return;
  lastTempAt = millis();
  const uint32_t ds = drv.DRV_STATUS();
  uint8_t level = 0;
  if      (ds & (1UL << 1))                level = 5;   // overtemp shutdown
  else if (ds & (1UL << 11))               level = 4;   // >=157 C
  else if (ds & (1UL << 10))               level = 3;   // >=150 C
  else if (ds & (1UL << 9))                level = 2;   // >=143 C
  else if (ds & ((1UL << 8) | (1UL << 0))) level = 1;   // >=120 C pre-warning
  if (level == tempLevel) { tempPending = level; return; }
  if (level != tempPending) { tempPending = level; return; }
  tempLevel = level;
  if (level >= 2) feederEmit(String(F("feeder:alert:hot:")) + level);
}

// ============================================================
//  state machine
// ============================================================
static void enterState(FState next) {
  if (state == next) return;
  state = next;
  stateSince = millis();
}

static void goFault() {
  stopImmediately();
  beamResetBreaks = 0;
  prevBeamState = readBeamRaw();
  stallActive = false; stallTimer = 0;
  faults++;
  enterState(S_FAULT);
  feederEmit(F("feeder:alert:jam"));
}

static void startJamReverse() {
  stopImmediately();
  setDirection(false);
  motorEnable(true);
  useSlowStart = false;
  targetHz = JAM_REVERSE_HZ;
  stallActive = false; stallTimer = 0;
  jams++;
  enterState(S_JAM_REVERSE);
}

static void resumeFromStop(bool clearRecovery) {
  if (clearRecovery) lastRecoveryEnd = 0;
  stallActive = false; stallTimer = 0;
  lastFedTime = millis();
  idleWarnActive = false;
  useSlowStart = true;
  enterState(runRequested ? S_RUNNING : S_STOPPED);
}

static void handleRunning() {
  if (!runRequested) { stopImmediately(); enterState(S_STOPPED); return; }
  if (!driverReady)  { stopImmediately(); return; }
  // a case waiting at the beam: hold until it is taken
  if (beamBlocked) {
    stopImmediately();
    stallActive = false; stallTimer = 0;
    enterState(S_HOLD);
    return;
  }
  setDirection(true);
  motorEnable(true);
  targetHz = selectedStepHz();

  if (idleWarnMs > 0 && lastFedTime != 0 && !idleWarnActive &&
      millis() - lastFedTime >= idleWarnMs) {
    idleWarnActive = true;
    feederEmit(F("feeder:alert:empty"));
  }
  if (idleShutoffMs > 0 && lastFedTime != 0 && millis() - lastFedTime >= idleShutoffMs) {
    stopImmediately();
    beamResetBreaks = 0;
    prevBeamState = readBeamRaw();
    stallActive = false; stallTimer = 0;
    idleWarnActive = false;
    enterState(S_IDLE_SHUTOFF);
    feederEmit(F("feeder:alert:shutoff"));
    return;
  }
  // jam detection: above the minimum speed and after the motor settles; a
  // stall must persist STALL_CONFIRM_MS, and a second jam within
  // JAM_REPEAT_MS of a recovery is a FAULT
  if (jamDetect && targetHz > JAM_MIN_SPEED_HZ && appliedHz > JAM_MIN_SPEED_HZ &&
      millis() - stallSettleStart >= STALL_SETTLE_MS) {
    if (isStalled()) {
      if (!stallActive) {
        stallActive = true;
        stallTimer = lastStallEvent = millis();
      } else {
        lastStallEvent = millis();
        if (millis() - stallTimer >= STALL_CONFIRM_MS) {
          if (lastRecoveryEnd != 0 && millis() - lastRecoveryEnd < JAM_REPEAT_MS) {
            goFault();
            return;
          }
          startJamReverse();
          return;
        }
      }
    } else if (stallActive && millis() - lastStallEvent >= STALL_CLEAR_MS) {
      stallActive = false; stallTimer = 0;
    }
  } else {
    stallActive = false; stallTimer = 0;
  }
}

static void serviceStateMachine() {
  switch (state) {
    case S_RUNNING: handleRunning(); break;
    case S_HOLD:
      if (!beamBlocked) {
        if (!runRequested) { enterState(S_STOPPED); break; }
        lastRecoveryEnd = 0;
        lastFedTime = millis();
        idleWarnActive = false;
        useSlowStart = true;
        enterState(S_RUNNING);
      } else if (!runRequested) {
        enterState(S_STOPPED);
      }
      break;
    case S_JAM_REVERSE:
      if (millis() - stateSince >= JAM_REVERSE_MS) { stopImmediately(); enterState(S_JAM_PAUSE); }
      break;
    case S_JAM_PAUSE:
      if (millis() - stateSince >= JAM_PAUSE_MS) {
        lastRecoveryEnd = lastFedTime = millis();
        idleWarnActive = false;
        useSlowStart = false;
        setDirection(true);
        enterState(S_RUNNING);
      }
      break;
    case S_FAULT:
      stopImmediately();
      if (beamResetGesture()) resumeFromStop(true);
      break;
    case S_IDLE_SHUTOFF:
      stopImmediately();
      if (beamResetGesture()) resumeFromStop(false);
      break;
    case S_STOPPED:
      stopImmediately();
      if (runRequested && driverReady) {
        lastFedTime = millis();
        idleWarnActive = false;
        useSlowStart = true;
        enterState(beamBlocked ? S_HOLD : S_RUNNING);
      }
      break;
  }
}

// ============================================================
//  status + protocol
// ============================================================
static void sendStatus() {
  String s = F("feeder:status {\"enabled\":");
  s += enabled ? 1 : 0;
  s += F(",\"state\":\""); s += stateName(state);
  s += F("\",\"run\":"); s += runRequested ? 1 : 0;
  s += F(",\"beam\":"); s += beamBlocked ? 1 : 0;
  s += F(",\"raw\":"); s += rawBeam ? 1 : 0;
  s += F(",\"speed\":"); s += speedPct;
  s += F(",\"hz\":"); s += appliedHz;
  s += F(",\"count\":"); s += count;
  s += F(",\"since_fed_ms\":"); s += lastFedTime ? (millis() - lastFedTime) : 0;
  s += F(",\"in_state_ms\":"); s += millis() - stateSince;
  s += F(",\"empty\":"); s += idleWarnActive ? 1 : 0;
  s += F(",\"jams\":"); s += jams;
  s += F(",\"faults\":"); s += faults;
  s += F(",\"sg\":"); s += sgResult;
  s += F(",\"sg_trip\":"); s += (unsigned)appliedThr * 2U;
  s += F(",\"driver\":"); s += driverReady ? 1 : 0;
  s += F(",\"diag\":"); s += diagUsable ? 1 : 0;
  s += F(",\"temp\":"); s += tempLevel;
  s += F(",\"warn_s\":"); s += idleWarnMs / 1000;
  s += F(",\"shutoff_s\":"); s += idleShutoffMs / 1000;
  s += F(",\"reversed\":"); s += motorReversed ? 1 : 0;
  s += F(",\"beam_high\":"); s += beamActiveLow ? 0 : 1;
  s += F(",\"jam_detect\":"); s += jamDetect ? 1 : 0;
  s += F("}");
  feederEmit(s);
}

static void setEnabled(bool on) {
  if (on == enabled) return;
  runRequested = false;
  stopImmediately();
  stallActive = false; stallTimer = 0;
  state = reportedState = S_STOPPED;
  stateSince = millis();
  enabled = on;
  if (on) {
    offlineLogged = false;
    if (motorPower) initDriver();
    rawBeam = prevBeamRunning = prevBeamState = readBeamRaw();
    beamBlocked = false;
    beamBrokenSince = beamClearedSince = 0;
    lastFedTime = millis();
  } else {
    driverReady = false;
  }
}

static bool onOff(const String &v) { return v == "1" || v == "on" || v == "true"; }

bool feederCommand(const String &line) {
  if (!line.startsWith(F("feeder:"))) return false;
  if (line == F("feeder:status")) { sendStatus(); return true; }
  int c = line.indexOf(':', 7);
  String key = c > 0 ? line.substring(7, c) : line.substring(7);
  String val = c > 0 ? line.substring(c + 1) : String();

  if (key == F("enable")) {
    setEnabled(onOff(val));
  } else if (!enabled) {
    feederEmit(F("feeder:error:not enabled"));
    return true;
  } else if (key == F("run")) {
    runRequested = onOff(val);
    if (!runRequested) { stopImmediately(); enterState(S_STOPPED); }
    else if (state == S_IDLE_SHUTOFF) lastFedTime = millis();
  } else if (key == F("speed")) {
    long v = val.toInt();
    if (v < 1 || v > 100) { feederEmit(F("feeder:error:speed is 1-100")); return true; }
    speedPct = (uint8_t)v;
  } else if (key == F("clear")) {
    if (state == S_FAULT || state == S_IDLE_SHUTOFF) resumeFromStop(true);
  } else if (key == F("warn")) {
    long v = val.toInt(); if (v < 0) v = 0; if (v > 3600) v = 3600;
    idleWarnMs = (uint32_t)v * 1000UL; idleWarnActive = false; lastFedTime = millis();
  } else if (key == F("shutoff")) {
    long v = val.toInt(); if (v < 0) v = 0; if (v > 3600) v = 3600;
    idleShutoffMs = (uint32_t)v * 1000UL; lastFedTime = millis();
  } else if (key == F("reversed")) {
    motorReversed = onOff(val); appliedDirLevel = -1; setDirection(true);
  } else if (key == F("beamhigh")) {
    beamActiveLow = !onOff(val);
    rawBeam = prevBeamRunning = prevBeamState = readBeamRaw();
    beamBlocked = false; beamBrokenSince = beamClearedSince = 0;
  } else if (key == F("jamdetect")) {
    jamDetect = onOff(val);
  } else if (key == F("band")) {                       // feeder:band:<i>:<sgthrs>
    int c2 = val.indexOf(':');
    long i = c2 > 0 ? val.substring(0, c2).toInt() : -1;
    long t = c2 > 0 ? val.substring(c2 + 1).toInt() : -1;
    if (i < 0 || i >= NUM_BANDS || t < 0 || t > 255) {
      feederEmit(F("feeder:error:band wants <0-2>:<0-255>")); return true;
    }
    bands[i].threshold = (uint8_t)t; currentBand = -1;
  } else {
    feederEmit(F("feeder:error:unknown command"));
    return true;
  }
  feederEmit(F("feeder:ok"));
  return true;
}

// ============================================================
void feederSetup() {
  // every feeder output safe at boot, whether or not one is fitted
  pinMode(PIN_EN, OUTPUT);   digitalWrite(PIN_EN, HIGH);
  pinMode(PIN_STEP, OUTPUT); digitalWrite(PIN_STEP, LOW);
  pinMode(PIN_DIR, OUTPUT);  digitalWrite(PIN_DIR, HIGH);
  pinMode(PIN_DIAG, INPUT);
  pinMode(PIN_BEAM, INPUT);
  stepPwmInit();
  setDirection(true);
}

void feederLoop() {
  if (!enabled) return;
  serviceBeam();
  // every driver read or write waits for the wheel to stop: the feeder
  // itself needs no bus traffic to run (beam, step pulses and the DIAG
  // jam line are all plain pins)
  const bool busFree = wheelAtRest();
  if (!motorPower) {                                   // 24 V off: nothing answers
    if (driverReady) { stopImmediately(); driverReady = false; }
  } else if (busFree) {
    checkDriverHealth();
  }
  serviceStateMachine();
  serviceRamp();
  if (busFree) serviceStallThreshold();
  if (busFree && !diagUsable && driverReady && state == S_RUNNING &&
      millis() - lastSgRead >= SG_SAMPLE_MS) {         // no jumper: poll
    lastSgRead = millis();
    sgResult = drv.SG_RESULT();
  }
  if (busFree) serviceDriverTemp();
  if (state != reportedState) {
    reportedState = state;
    feederEmit(String(F("feeder:state:")) + stateName(state));
  }
}
