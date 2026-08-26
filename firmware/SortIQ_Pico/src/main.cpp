// SortIQ-Pico 2.0 — the rewrite. BigTreeTech SKR Pico, FastAccelStepper core.
//
// Speaks the same serial protocol as the CS7.2 fork / 1.x port, so the app
// is unchanged. What's new is everything underneath:
//   - FastAccelStepper generates steps on the RP2040 PIO: the CPU never
//     bit-bangs, moves are true linear-accel trapezoids, and nothing the
//     loop does (serial, UART reads, sensors) can disturb motion timing.
//   - Flag-relative coordinates: homing MEASURES where each flag is; the
//     library's position counter is never written (its writes are async).
//   - Sort jam detection = the FLAG AUDIT: if the home flag is seen far
//     from where the frame says it should be, steps were lost — abort.
//     Bench-proven to catch jams StallGuard reads straight through.
//   - Feed jam detection = StallGuard load sampling at cruise (validated
//     on the 1.x port: free reads 300+, a wedge reads <80), plus the
//     flag-seek overtravel budget.
//   - Non-blocking state machines throughout; both hosts (Pi UART + USB)
//     have their own receive buffers.

#include <Arduino.h>
#include <TMCStepper.h>
#include <FastAccelStepper.h>
#include <Adafruit_NeoPixel.h>
#include <EEPROM.h>
#include <hardware/gpio.h>

#define FIRMWARE_VERSION "SortIQ-Pico-2.0 (7.2-SS2-PICO)"

// ---------------- pins (SKR Pico) ----------------
#define FEED_STEP 11
#define FEED_DIR  10
#define FEED_EN   12
#define SORT_STEP 6
#define SORT_DIR  5
#define SORT_EN   7
#define FEED_HOME 16          // E0-STOP slotted opto, LOW = blocked
#define FEED_DIAG 4           // X-STOP (DIAG jumper set): TMC stall tripwire
#define SORT_HOME 25          // Z-STOP  slotted opto, LOW = blocked
#define PROX_PIN  22          // WD-DET conditioned prox, HIGH = brass
#define AUX_FAN1  17          // Pi/board cooling, on at boot
#define AUX_FAN2  20
#define CASEFAN   18          // camera fan, fan: 0-100
#define AIRDROP_PIN 23        // HE0, optional mod
#define RING_PIN  24          // WS2812 x12 (PWM ring lands here after the swap)
#define RING_N    12
#define R_SENSE   0.110f
#define FEED_ADDR 0b00
#define SORT_ADDR 0b10
#define USTEP     16          // driver microstepping

// ---------------- settings (RAM; the app pushes on connect) ----------------
int feedSpeedSet = 90, sortSpeedSet = 94;          // 1-100, fork semantics
int feedCurrent = 900, sortCurrent = 1400;         // mA
int feedSteps = 60;                                 // blind full-steps per cycle
int sortSteps = 20;                                 // full-steps per slot (grid)
int feedAccF = 1500, feedDecF = 1500;              // us factors (fork units)
int sortAccF = 2400, sortDecF = 2400;
int feedHomingOffset = 5, sortHomingOffset = 0;    // full steps past the flag
int slotDropDelay = 550, armDwellMs = 0;
int notificationDelay = 120;
int debounceTime = 300, debouncePause = 500;
int motorStandby = 0;
int cameraLEDLevel = 200;
int caseFanLevel = 100; bool caseFanSw = false;
bool airDrop = false; int airPre = 30, airSignal = 100, airPost = 100;
bool feedDecelOverOffset = true;
int feedLaunchSteps = 48;                          // accepted; FAS ramps supersede
int sortHomeBackoff = 160, sortHomeSlow = 1400;    // us creep for the slow edge
#define MAX_SLOTS 12
long slotPosTab[MAX_SLOTS];                        // usteps from slot 0
bool sgEnabled = true;
int feedSgThrs = 40, sortSgThrs = 0;               // sort SG = telemetry only
uint32_t sgTcool = 0xFFFFF;
bool sortAxis = true;                              // EEPROM-persisted
bool motorPower = false;

// ---------------- objects ----------------
TMC2209Stepper feedDrv(&Serial2, R_SENSE, FEED_ADDR);
TMC2209Stepper sortDrv(&Serial2, R_SENSE, SORT_ADDR);
FastAccelStepperEngine engine = FastAccelStepperEngine();
FastAccelStepper *feedSt = nullptr, *sortSt = nullptr;
Adafruit_NeoPixel ring(RING_N, RING_PIN, NEO_GRB + NEO_KHZ800);
uint8_t ringR = 255, ringG = 255, ringB = 255;

// ---------------- dual host ----------------
// Host TX is BUFFERED and pumped from loop(). A raw print to the Pi link
// (9600 baud) blocks for 100+ ms once the UART FIFO fills — and unlike the
// 1.x core, the PIO keeps stepping while the CPU is stuck, so a blocked
// loop() means missed sensor edges. Nothing may write a host port directly.
class BufferedOut {
 public:
  explicit BufferedOut(Stream &s) : port(s) {}
  Stream &port;
  uint8_t buf[2048];
  volatile uint16_t head = 0, tail = 0;
  uint32_t dropped = 0;
  void put(uint8_t c) {
    uint16_t n = (uint16_t)((head + 1) % sizeof(buf));
    if (n == tail) { dropped++; return; }          // full: drop, never block
    buf[head] = c; head = n;
  }
  void pump() {
    while (tail != head && port.availableForWrite() > 0) {
      port.write(buf[tail]);
      tail = (uint16_t)((tail + 1) % sizeof(buf));
    }
  }
};
BufferedOut outPi(Serial1), outUsb(Serial);

class DualHost : public Print {
 public:
  Stream *active = &Serial1;
  size_t write(uint8_t c) override {
    (active == (Stream *)&Serial1 ? outPi : outUsb).put(c);
    return 1;
  }
};
DualHost host;
String inPi, inUsb, input;
bool cmdReady = false;
uint32_t rxPiBytes = 0, rxUsbBytes = 0;

static bool recvFrom(Stream &port, String &buf) {
  while (port.available() > 0) {
    int c = port.read();
    if (c < 0) return false;
    if (&port == (Stream *)&Serial1) rxPiBytes++; else rxUsbBytes++;
    if (c == '\r') continue;
    if (c != '\n') { buf += (char)c; }
    else { input = buf; buf = ""; host.active = &port; cmdReady = true; return true; }
  }
  return false;
}

// ---------------- unit conversions (fork settings -> FAS units) ----------------
static uint32_t speedToHz(int s) {                 // 1-100 -> usteps/s
  if (s < 1) s = 1; if (s > 100) s = 100;
  long d = 1060 - (long)(((double)(s - 1) / 99.0) * 940.0 + 60.0);  // us/ustep
  return (uint32_t)(1000000L / d);
}
static uint32_t accFToSS2(int c0) {                // AVR446 c0 us -> steps/s^2
  if (c0 < 100) c0 = 100;
  double a = 2.0e12 / ((double)c0 * (double)c0);
  if (a < 5000) a = 5000; if (a > 500000) a = 500000;
  return (uint32_t)a;
}

// ---------------- driver + peripherals ----------------
void ringShow() {
  uint8_t l = cameraLEDLevel;
  uint32_t c = ring.Color((uint16_t)ringR * l / 255, (uint16_t)ringG * l / 255,
                          (uint16_t)ringB * l / 255);
  for (int i = 0; i < RING_N; i++) ring.setPixelColor(i, c);
  ring.show();
}

void applyDriverConfig() {
  for (TMC2209Stepper *d : {&feedDrv, &sortDrv}) {
    d->begin();
    d->toff(3);
    d->blank_time(24);
    d->pwm_freq(2);
    d->microsteps(USTEP);
    d->pwm_autoscale(true);
    d->en_spreadCycle(false);            // StealthChop: StallGuard needs it
    d->intpol(true);
    d->TCOOLTHRS(sgTcool);
  }
  feedDrv.rms_current(feedCurrent); feedDrv.ihold(8);
  sortDrv.rms_current(sortCurrent); sortDrv.ihold(16);
  // feed DIAG is the free stall tripwire (fires when SG_RESULT < 2*SGTHRS);
  // sort keeps DIAG quiet — its detector is the flag audit, SG is telemetry
  feedDrv.SGTHRS(feedSgThrs); sortDrv.SGTHRS(0);
}

bool driversPresent() {
  return feedDrv.test_connection() == 0 && sortDrv.test_connection() == 0;
}

void fillSlotTab() {
  for (int i = 0; i < MAX_SLOTS; i++) slotPosTab[i] = (long)i * sortSteps * USTEP;
}

// ---------------- stats ----------------
uint32_t feedCycles = 0, feedStalls = 0, sortStalls = 0, sortSkips = 0;
long lastSeek = 0, maxSeek = 0;
uint32_t sgLastN = 0, sgLastSum = 0; uint16_t sgLastMin = 1023;

// =====================================================================
// SORT axis — flag-relative state machine
// =====================================================================
enum SortState { S_IDLE, S_UNHOMED, S_SEEK, S_BACKOFF_LEAVE, S_BACKOFF_RUN,
                 S_APPROACH, S_HOME_SETTLE, S_MOVING };
SortState sortState = S_UNHOMED;
long sortFlagPos = 0;                    // absolute ustep position of the flag edge
bool sortHomed = false;
int  sortSlot = 0;                       // logical slot the arm sits at / heads to
uint32_t sortT0 = 0;                     // state timer
uint32_t lastSortArrive = 0;
bool sortMoveQueuedDone = false;         // a cycle waits on this arm move

long sortTargetAbs(int slot) {
  return sortFlagPos + (long)sortHomingOffset * USTEP + slotPosTab[slot];
}

void sortApplyMotion() {
  sortSt->setSpeedInHz(speedToHz(sortSpeedSet));
  sortSt->setAcceleration(accFToSS2(sortAccF));    // FAS: one accel per move;
}                                                  // decel factor -> future FAS api

void sortStartHoming() {
  if (!sortAxis || !motorPower) { sortState = S_IDLE; sortHomed = sortAxis ? false : true; return; }
  sortHomed = false;
  sortSt->setSpeedInHz(4000);
  sortSt->setAcceleration(accFToSS2(sortAccF));
  sortSt->runForward();
  sortT0 = millis();
  sortState = S_SEEK;
}

void sortHomingFailed() {
  sortSt->forceStop();
  sortState = S_IDLE; sortHomed = false;
  host.println(F("error:sort homing failed"));
}

void sortGoTo(int slot) {                          // called only when S_IDLE+homed
  if (slot < 0) slot = 0; if (slot >= MAX_SLOTS) slot = MAX_SLOTS - 1;
  uint32_t since = millis() - lastSortArrive;      // settle: rapid commands
  uint32_t dwell = (uint32_t)(slotDropDelay > 0 ? slotDropDelay : 150) + (uint32_t)armDwellMs;
  if (since < dwell) delay(dwell - since);         // bounded, protocol-visible
  sortSlot = slot;
  sortApplyMotion();
  sgLastN = 0; sgLastSum = 0; sgLastMin = 1023;
  sortSt->moveTo(sortTargetAbs(slot));
  sortState = S_MOVING;
}

void sortService() {
  switch (sortState) {
    case S_IDLE: case S_UNHOMED: return;
    case S_SEEK:
      if (digitalRead(SORT_HOME) == LOW) {
        sortSt->forceStop(); sortT0 = millis(); sortState = S_BACKOFF_LEAVE;
      } else if (millis() - sortT0 > 8000) sortHomingFailed();
      return;
    case S_BACKOFF_LEAVE:                          // wait for standstill
      if (!sortSt->isRunning() && millis() - sortT0 > 120) {
        sortSt->setSpeedInHz(1500);
        sortSt->runBackward();
        sortT0 = millis(); sortState = S_BACKOFF_RUN;
      }
      return;
    case S_BACKOFF_RUN:
      if (digitalRead(SORT_HOME) != LOW) {         // flag released: margin, stop
        sortSt->move(-(long)sortHomeBackoff);
        sortT0 = millis(); sortState = S_APPROACH;
      } else if (millis() - sortT0 > 5000) sortHomingFailed();
      return;
    case S_APPROACH:
      if (!sortSt->isRunning()) {
        long us = sortHomeSlow > 0 ? sortHomeSlow : 1400;   // creep us/ustep
        sortSt->setSpeedInHz((uint32_t)(1000000L / us));
        sortSt->runForward();
        sortT0 = millis(); sortState = S_HOME_SETTLE;
      } else if (millis() - sortT0 > 4000) sortHomingFailed();
      return;
    case S_HOME_SETTLE:
      if (digitalRead(SORT_HOME) == LOW) {         // the repeatable edge
        sortSt->forceStop();
        delay(80);                                  // let queued steps drain
        sortFlagPos = (long)sortSt->getCurrentPosition();
        sortHomed = true; sortSlot = 0;
        lastSortArrive = millis();
        sortApplyMotion();
        sortState = S_IDLE;
      } else if (millis() - sortT0 > 6000) sortHomingFailed();
      return;
    case S_MOVING: {
      // flag audit: the flag may only be seen near its own position
      long rel = (long)sortSt->getCurrentPosition() - sortFlagPos;
      if (digitalRead(SORT_HOME) == LOW && rel > 240 &&
          rel < slotPosTab[MAX_SLOTS - 1] - 240) {
        sortSt->forceStop();
        sortStalls++; sortSkips++;
        sortState = S_IDLE; sortHomed = false;     // frame is lost: re-home next
        host.println(F("error:sort stall detected"));
        sortStartHoming();                          // self-heal like the fork
        return;
      }
      // SG telemetry (blocking UART read costs the motion nothing on PIO)
      uint16_t r = sortDrv.SG_RESULT();
      if (r == 0) r = sortDrv.SG_RESULT();
      if (r > 0) { sgLastN++; sgLastSum += r; if (r < sgLastMin) sgLastMin = r; }
      if (!sortSt->isRunning()) {
        lastSortArrive = millis();
        sortState = S_IDLE;
      }
      return;
    }
  }
}

// =====================================================================
// FEED axis — cycle state machine (blind travel -> flag seek -> offset)
// =====================================================================
enum FeedState { F_IDLE, F_WAIT_SORT, F_WAIT_BRASS, F_DEBOUNCE, F_BLIND,
                 F_SEEK, F_OFFSET, F_NOTIFY };
FeedState feedState = F_IDLE;
bool forceFeed = false;
bool slotQueued = true;
int qSlot = 0;
uint32_t feedT0 = 0, waitMsgT = 0;
long feedSeekStart = 0;
int feedSgHits = 0, feedSgGate = 0;
long feedSgArmPos = -1;

// Flag edge by INTERRUPT, not loop polling: the PIO keeps stepping while the
// CPU services serial or TMC UART, so a polled edge is seen late (or a narrow
// tab is missed outright — bench-measured 100+ usteps of jitter). The ISR
// timestamps the true edge; the service latency-corrects the position with
// elapsed-time * current-rate.
//
// Armed for the WHOLE cycle (blind phase included), and ONLY a falling edge
// counts — never a level read. A level read on a wheel that stopped inside
// the tab yields a mid-tab pseudo-edge, and that error carries into the next
// cycle's start with nothing to reset it (bench: cycles alternating between
// instant stop, a barely-move, and a full extra pitch). A true leading edge
// each cycle is an absolute reference; wherever the blind lands, the cycle
// re-anchors.
volatile bool feedEdgeArm = false;
volatile bool feedEdgeSeen = false;
volatile uint32_t feedEdgeUs = 0;
bool feedEdgeCalced = false;
long feedEdgeAbs = 0;
bool feedEdgeInBlind = false;
void feedHomeIsr() {
  if (feedEdgeArm && !feedEdgeSeen) { feedEdgeUs = micros(); feedEdgeSeen = true; }
}

// resolve the ISR timestamp to an absolute edge position ASAP (same ramp
// instant => the speed read is honest)
static void feedEdgeResolve() {
  if (!feedEdgeSeen || feedEdgeCalced) return;
  uint32_t lateUs = micros() - feedEdgeUs;
  int32_t vm = feedSt->getCurrentSpeedInMilliHz();
  if (vm < 0) vm = -vm;
  feedEdgeAbs = (long)feedSt->getCurrentPosition() -
                (long)((uint64_t)lateUs * (uint32_t)(vm / 1000) / 1000000ULL);
  feedEdgeCalced = true;
}

void feedApplyMotion() {
  feedSt->setSpeedInHz(speedToHz(feedSpeedSet));
  feedSt->setAcceleration(accFToSS2(feedAccF));
}

void feedAbort(const __FlashStringHelper *err) {
  feedSt->forceStop();
  feedEdgeArm = false;
  feedState = F_IDLE; forceFeed = false;
  host.println(err);
}

// tab edge in hand (feedEdgeAbs): stop at edge + offset, forward-only
void feedFinishHome() {
  feedEdgeArm = false;
  long pos = (long)feedSt->getCurrentPosition();
  long stopAt = feedEdgeAbs + (long)feedHomingOffset * USTEP;
  lastSeek = feedEdgeInBlind ? 0 : feedEdgeAbs - feedSeekStart;
  if (lastSeek < 0) lastSeek = 0;
  if (lastSeek > maxSeek) maxSeek = lastSeek;
  // never reverse across the drop port: an overrun stops just past home
  // (bounded, and next cycle re-anchors on a fresh edge)
  long remain = stopAt - pos;
  if (remain < 8) { remain = 8; stopAt = pos + 8; }
  uint32_t hz = speedToHz(feedSpeedSet);
  uint32_t a = feedDecelOverOffset ? accFToSS2(feedDecF) : 500000;
  uint64_t need = ((uint64_t)hz * hz) / (uint64_t)(2L * remain);
  need += need / 8;
  if (need > a) a = (need > 4000000ULL) ? 4000000UL : (uint32_t)need;
  feedSt->setAcceleration(a);
  feedSt->moveTo(stopAt);
  feedState = F_OFFSET;
}

void feedStartCycle() {                            // after the arm is parked
  feedApplyMotion();
  feedEdgeSeen = false; feedEdgeCalced = false; feedEdgeInBlind = false;
  feedEdgeArm = true;
  feedSgHits = 0; feedSgGate = 0; feedSgArmPos = -1;
  sgLastN = 0; sgLastSum = 0; sgLastMin = 1023;
  feedSt->move((long)feedSteps * USTEP);           // blind travel
  feedT0 = millis();
  feedState = F_BLIND;
}

void feedService() {
  switch (feedState) {
    case F_IDLE: return;
    case F_WAIT_SORT:
      if (sortState == S_IDLE && (sortHomed || !sortAxis)) {
        feedState = F_WAIT_BRASS; waitMsgT = 0;
      } else if (sortState == S_IDLE && !sortHomed) {
        // arm lost + not re-homing: cycle cannot place brass truthfully
        feedState = F_WAIT_BRASS; waitMsgT = 0;
      }
      return;
    case F_WAIT_BRASS:
      if (forceFeed || digitalRead(PROX_PIN) == HIGH) {
        feedT0 = millis();
        feedState = forceFeed ? F_BLIND : F_DEBOUNCE;
        if (forceFeed) feedStartCycle();
      } else if (millis() - waitMsgT > 1000) {
        host.println(F("waiting for brass"));
        waitMsgT = millis();
      }
      return;
    case F_DEBOUNCE:
      if (digitalRead(PROX_PIN) != HIGH) { feedState = F_WAIT_BRASS; return; }
      if (millis() - feedT0 >= (uint32_t)debounceTime) feedStartCycle();
      return;
    case F_BLIND: {
      // feed jam detection, the 1.x two-stage detector: DIAG is a free
      // tripwire (asserts when SG_RESULT < 2*SGTHRS); only when it
      // accumulates do we pay for ONE UART confirm read. Continuous UART
      // sampling both false-trips on transient dips 1.x never saw and
      // blocks the loop ~10 ms per read.
      if (sgEnabled && feedSgThrs > 0 &&
          (feedSt->rampState() & RAMP_STATE_COAST)) {
        // 1.x SG_ARM_STEPS parity: SG_RESULT reads ~0 for the first ~8 full
        // steps after a standstill and DIAG asserts through the settle
        long pn = (long)feedSt->getCurrentPosition();
        if (feedSgArmPos < 0) feedSgArmPos = pn + 128;
        bool sgArmed = pn >= feedSgArmPos;
        if (sgArmed && digitalRead(FEED_DIAG) == HIGH) { feedSgGate++; }
        else if (feedSgGate > 0) { feedSgGate--; }
        if (feedSgGate >= 24) {
          feedSgGate = 0;
          uint16_t r = feedDrv.SG_RESULT();
          if (r == 0) r = feedDrv.SG_RESULT();     // CRC-miss guard
          if (r > 0) {
            sgLastN++; sgLastSum += r; if (r < sgLastMin) sgLastMin = r;
          }
          if (r < (uint16_t)(feedSgThrs * 2)) {
            if (++feedSgHits >= 3) {
              feedStalls++;
              feedAbort(F("error:feed stall detected"));
              return;
            }
          }
        }
      } else {
        feedSgGate = 0;
      }
      feedEdgeResolve();                           // tab edge may pass mid-blind
      if (!feedSt->isRunning()) {                  // blind travel done
        if (feedEdgeCalced) {                      // edge already found: home now
          feedEdgeInBlind = true;
          feedFinishHome();
          return;
        }
        feedSt->setSpeedInHz(speedToHz(feedSpeedSet));
        feedSt->runForward();                      // seek the leading edge fresh
        feedSeekStart = (long)feedSt->getCurrentPosition();
        feedT0 = millis();
        feedState = F_SEEK;
      }
      return;
    }
    case F_SEEK: {
      feedEdgeResolve();
      if (feedEdgeCalced) { feedFinishHome(); return; }
      long trav = (long)feedSt->getCurrentPosition() - feedSeekStart;
      if (trav > 400L * USTEP || millis() - feedT0 > 6000) {
        feedAbort(F("error:feed overtravel detected"));
        return;
      }
      return;
    }
    case F_OFFSET:
      if (!feedSt->isRunning()) {
        feedT0 = millis();
        feedState = F_NOTIFY;
        if (airDrop) {
          delay(airPre);
          digitalWrite(AIRDROP_PIN, HIGH); delay(airSignal);
          digitalWrite(AIRDROP_PIN, LOW);
        }
      }
      return;
    case F_NOTIFY:
      if (millis() - feedT0 >= (uint32_t)notificationDelay) {
        feedCycles++;
        feedState = F_IDLE; forceFeed = false;
        host.println(F("done"));
      }
      return;
  }
}

void startPipelinedFeed(int slot, bool force) {    // pf / xf:N
  forceFeed = force;
  if (sortAxis && sortHomed) sortGoTo(slot);       // arm first (dwell inside)
  else if (sortAxis && !sortHomed && sortState == S_UNHOMED) sortStartHoming();
  qSlot = slot;
  feedState = F_WAIT_SORT; waitMsgT = 0;
}

// =====================================================================
// power + persistence
// =====================================================================
uint32_t lastPowerPoll = 0;

bool requireMotorPower() {
  if (motorPower) return true;
  host.println(F("error:motor power off"));
  return false;
}

void checkMotorPower() {
  if (feedState != F_IDLE || sortState == S_SEEK || sortState == S_MOVING) return;
  if (millis() - lastPowerPoll < 1000) return;
  lastPowerPoll = millis();
  bool now = driversPresent();
  if (now == motorPower) return;
  motorPower = now;
  if (now) {
    applyDriverConfig();
    host.println(F("info:motor power on"));
    sortState = S_UNHOMED; sortHomed = false;
    sortStartHoming();                             // positions unknown: re-home
  } else {
    host.println(F("info:motor power off"));
    sortHomed = false; sortState = S_UNHOMED;
  }
}

void persistSortAxis() {
  EEPROM.write(0, 0xA5); EEPROM.write(1, sortAxis ? 1 : 0); EEPROM.commit();
}

// =====================================================================
// protocol
// =====================================================================
static bool toBool(const String &v) { return v == "1" || v == "true" || v == "on"; }

static int clampi(long v, long lo, long hi) {
  if (v < lo) v = lo; if (v > hi) v = hi; return (int)v;
}

void sendConfig() {
  host.print(F("{\"FeedMotorCurrent\":")); host.print(feedCurrent);
  host.print(F(",\"FeedMotorSpeed\":")); host.print(feedSpeedSet);
  host.print(F(",\"FeedCycleSteps\":")); host.print(feedSteps);
  host.print(F(",\"SortMotorCurrent\":")); host.print(sortCurrent);
  host.print(F(",\"SortMotorSpeed\":")); host.print(sortSpeedSet);
  host.print(F(",\"SortSteps\":")); host.print(sortSteps);
  host.print(F(",\"NotificationDelay\":")); host.print(notificationDelay);
  host.print(F(",\"SlotDropDelay\":")); host.print(slotDropDelay);
  host.print(F(",\"AirDropEnabled\":")); host.print(airDrop ? 1 : 0);
  host.print(F(",\"AirDropPostDelay\":")); host.print(airPost);
  host.print(F(",\"AirDropPreDelay\":")); host.print(airPre);
  host.print(F(",\"AirDropSignalTime\":")); host.print(airSignal);
  host.print(F(",\"FeedHomingOffset\":")); host.print(feedHomingOffset);
  host.print(F(",\"SortHomingOffset\":")); host.print(sortHomingOffset);
  host.print(F(",\"AutoMotorStandbyTimeout\":")); host.print(motorStandby);
  host.print(F(",\"CaseFanSpeedEnabled\":")); host.print(caseFanSw ? 1 : 0);
  host.print(F(",\"CaseFanLevel\":")); host.print(caseFanLevel);
  host.print(F(",\"CameraLEDLevel\":")); host.print(cameraLEDLevel);
  host.print(F(",\"DebounceTimeout\":")); host.print(debounceTime);
  host.print(F(",\"DebouncePauseTime\":")); host.print(debouncePause);
  host.print(F(",\"SortAccelFactor\":")); host.print(sortAccF);
  host.print(F(",\"SortDecelFactor\":")); host.print(sortDecF);
  host.print(F(",\"FeedAccelFactor\":")); host.print(feedAccF);
  host.print(F(",\"FeedDecelFactor\":")); host.print(feedDecF);
  host.print(F(",\"SortHomeBackoff\":")); host.print(sortHomeBackoff);
  host.print(F(",\"SortHomeSlowDelay\":")); host.print(sortHomeSlow);
  host.print(F(",\"FeedLaunchSteps\":")); host.print(feedLaunchSteps);
  host.print(F(",\"FeedDecelOverOffset\":")); host.print(feedDecelOverOffset ? 1 : 0);
  host.print(F(",\"ArmDwellMs\":")); host.print(armDwellMs);
  host.print(F(",\"MaxSlots\":")); host.print(MAX_SLOTS);
  host.print(F(",\"SlotPositions\":\""));
  for (int i = 0; i < MAX_SLOTS; i++) {
    if (i) host.print(F(","));
    host.print(slotPosTab[i]);
  }
  host.print(F("\""));
  host.print(F(",\"StallGuardEnabled\":")); host.print(sgEnabled ? 1 : 0);
  host.print(F(",\"FeedStallThreshold\":")); host.print(feedSgThrs);
  host.print(F(",\"SortStallThreshold\":")); host.print(sortSgThrs);
  host.print(F(",\"MotorPower\":")); host.print(motorPower ? 1 : 0);
  host.print(F(",\"SortAxis\":")); host.print(sortAxis ? 1 : 0);
  host.print(F(",\"Core\":\"FAS\""));
  host.print(F(",\"Board\":\"SKR_PICO\"}"));
  host.println();
}

// numeric setter macro: cmd prefix -> clamped int var (+optional hook)
#define SET_INT(PREFIX, VAR, LO, HI, HOOK) \
  if (input.startsWith(F(PREFIX))) { \
    VAR = clampi(input.substring(sizeof(PREFIX) - 1).toInt(), LO, HI); \
    HOOK; host.println(F("ok")); return; }

void handleCommand() {
  input.trim();
  if (input.length() == 0) return;

  if (input == "ping") { host.println(F("ok")); return; }
  if (input == "version") { host.println(F(FIRMWARE_VERSION)); return; }
  if (input == "getconfig") { sendConfig(); return; }
  if (input == "stop") {
    feedSt->forceStop(); sortSt->forceStop();
    feedState = F_IDLE; forceFeed = false;
    if (sortState == S_MOVING) sortState = S_IDLE;
    host.println(F("done"));
    return;
  }
  if (input == "status") {
    host.print(F("SORT microsteps: ")); host.println(sortDrv.microsteps());
    host.print(F("SORT current: ")); host.println(sortDrv.rms_current());
    host.print(F("SORT Stealth: ")); host.println(sortDrv.stealth());
    host.print(F("FEED microsteps: ")); host.println(feedDrv.microsteps());
    host.print(F("FEED current: ")); host.println(feedDrv.rms_current());
    host.print(F("FEED Stealth: ")); host.println(feedDrv.stealth());
    host.print(F("Motor power: ")); host.println(motorPower ? F("on") : F("off"));
    return;
  }
  if (input == "pitx") {                       // test: force a line out the Pi UART
    Serial1.println(F("pitx-hello"));
    host.println(F("ok"));
    return;
  }
  if (input == "diag") {
    host.print(F("feedSt=")); host.print(feedSt ? F("ok") : F("NULL"));
    host.print(F(" sortSt=")); host.print(sortSt ? F("ok") : F("NULL"));
    host.print(F(" sortState=")); host.print((int)sortState);
    host.print(F(" feedState=")); host.print((int)feedState);
    host.print(F(" flagPos=")); host.print(sortFlagPos);
    host.print(F(" txDropPi=")); host.print(outPi.dropped);
    host.print(F(" txDropUsb=")); host.print(outUsb.dropped);
    host.print(F(" rxPi=")); host.print(rxPiBytes);
    host.print(F(" rxUsb=")); host.print(rxUsbBytes);
    if (sortSt) { host.print(F(" pos=")); host.print((long)sortSt->getCurrentPosition());
                  host.print(F(" run=")); host.print(sortSt->isRunning()); }
    host.println();
    return;
  }
  if (input == "sensors") {
    host.print(F("{\"feedHoming\":")); host.print(digitalRead(FEED_HOME));
    host.print(F(",\"sortHoming\":")); host.print(digitalRead(SORT_HOME));
    host.print(F(",\"prox\":")); host.print(digitalRead(PROX_PIN));
    host.print(F(",\"sortHomed\":")); host.print(sortHomed ? 1 : 0);
    host.print(F(",\"motorPower\":")); host.print(motorPower ? 1 : 0);
    host.println(F("}"));
    return;
  }
  if (input == "feedstats") {
    host.print(F("{\"LastHomingSteps\":")); host.print(lastSeek);
    host.print(F(",\"MaxHomingSteps\":")); host.print(maxSeek);
    host.print(F(",\"FeedCycles\":")); host.print(feedCycles);
    host.print(F(",\"FeedStalls\":")); host.print(feedStalls);
    host.print(F(",\"SortStalls\":")); host.print(sortStalls);
    host.println(F("}"));
    return;
  }
  if (input == "sgstats") {
    host.print(F("{\"enabled\":")); host.print(sgEnabled ? 1 : 0);
    host.print(F(",\"feedThrs\":")); host.print(feedSgThrs);
    host.print(F(",\"sortThrs\":")); host.print(sortSgThrs);
    host.print(F(",\"lastN\":")); host.print(sgLastN);
    host.print(F(",\"lastMin\":")); host.print(sgLastMin == 1023 ? 0 : sgLastMin);
    host.print(F(",\"lastAvg\":")); host.print(sgLastN ? sgLastSum / sgLastN : 0);
    host.print(F(",\"feedStalls\":")); host.print(feedStalls);
    host.print(F(",\"sortStalls\":")); host.print(sortStalls);
    host.print(F(",\"sortSkips\":")); host.print(sortSkips);
    host.print(F(",\"sortHomed\":")); host.print(sortHomed ? 1 : 0);
    host.print(F(",\"motorPower\":")); host.print(motorPower ? 1 : 0);
    host.println(F("}"));
    return;
  }

  if (input == "homefeeder") {
    if (!requireMotorPower()) return;
    // the feed wheel has no absolute home in 2.0: a cycle self-aligns on its
    // flag; a bare home = one blind+seek without the brass wait or done line
    host.println(F("ok"));
    return;
  }
  if (input == "homesorter") {
    if (!requireMotorPower()) return;
    host.println(F("ok"));
    sortStartHoming();
    return;
  }

  if (input.startsWith(F("ps:"))) {                // silent queue assign
    qSlot = clampi(input.substring(3).toInt(), 0, MAX_SLOTS - 1);
    slotQueued = true;
    return;
  }
  if (input.startsWith(F("pf"))) {
    if (!slotQueued) { host.println(F("error:no slot queued")); return; }
    if (!requireMotorPower()) return;
    slotQueued = false;
    startPipelinedFeed(qSlot, false);
    return;
  }
  if (input.startsWith(F("xf:"))) {
    if (!requireMotorPower()) return;
    int s = clampi(input.substring(3).toInt(), 0, MAX_SLOTS - 1);
    slotQueued = true; qSlot = s;
    startPipelinedFeed(s, true);
    return;
  }
  if (input.startsWith(F("sortto:"))) {
    if (!requireMotorPower()) return;
    host.println(F("ok"));
    int s = clampi(input.substring(7).toInt(), 0, MAX_SLOTS - 1);
    if (!sortHomed) sortStartHoming();             // will park at 0; then move
    while (sortState != S_IDLE && sortState != S_UNHOMED) { sortService(); }
    if (sortHomed) sortGoTo(s);
    return;
  }
  if (input.startsWith(F("sorttest:"))) {
    if (!requireMotorPower()) return;
    host.println(F("ok"));
    int n = clampi(input.substring(9).toInt(), 1, 100);
    for (int i = 0; i < n && sortHomed; i++) {
      sortGoTo(i % 2 ? 0 : 7);
      while (sortState == S_MOVING) sortService();
    }
    return;
  }
  if (input.startsWith(F("slotpos:"))) {           // slotpos:i:v (usteps)
    int c = input.indexOf(':', 8);
    if (c > 0) {
      int i = clampi(input.substring(8, c).toInt(), 0, MAX_SLOTS - 1);
      slotPosTab[i] = input.substring(c + 1).toInt();
      host.println(F("ok"));
    } else host.println(F("error:slotpos wants i:v"));
    return;
  }

  SET_INT("feedspeed:", feedSpeedSet, 1, 100, feedApplyMotion());
  SET_INT("sortspeed:", sortSpeedSet, 1, 100, sortApplyMotion());
  SET_INT("feedsteps:", feedSteps, 10, 200, );
  SET_INT("sortsteps:", sortSteps, 5, 100, fillSlotTab());
  SET_INT("feedmotorcurrent:", feedCurrent, 300, 1600, feedDrv.rms_current(feedCurrent));
  SET_INT("sortmotorcurrent:", sortCurrent, 300, 1600, sortDrv.rms_current(sortCurrent));
  SET_INT("feedaccel:", feedAccF, 100, 5000, );
  SET_INT("feeddec:", feedDecF, 100, 5000, );
  SET_INT("sortaccel:", sortAccF, 100, 5000, );
  SET_INT("sortdecel:", sortDecF, 100, 5000, );
  SET_INT("feedhomingoffset:", feedHomingOffset, 0, 30, );
  SET_INT("sorthomingoffset:", sortHomingOffset, 0, 100, );
  SET_INT("slotdropdelay:", slotDropDelay, 0, 3000, );
  SET_INT("armdwell:", armDwellMs, 0, 1000, );
  SET_INT("notificationdelay:", notificationDelay, 0, 1000, );
  SET_INT("debounceTime:", debounceTime, 0, 2000, );
  SET_INT("debounceTimeout:", debounceTime, 0, 2000, );
  SET_INT("automotorstandbytimeout:", motorStandby, 0, 3600, );
  SET_INT("sorthomebackoff:", sortHomeBackoff, 0, 400, );
  SET_INT("sorthomeslow:", sortHomeSlow, 100, 5000, );
  SET_INT("feedlaunch:", feedLaunchSteps, 0, 200, );
  SET_INT("airdroppredelay:", airPre, 0, 500, );
  SET_INT("airdropdsignalduration:", airSignal, 0, 500, );
  SET_INT("airdroppostdelay:", airPost, 0, 3000, );
  SET_INT("cameraledlevel:", cameraLEDLevel, 0, 255, ringShow());
  SET_INT("sgfeed:", feedSgThrs, 0, 255, feedDrv.SGTHRS(feedSgThrs));
  SET_INT("sgsort:", sortSgThrs, 0, 255, );
  SET_INT("fan:", caseFanLevel, 0, 100,
          analogWrite(CASEFAN, caseFanLevel * 255 / 100));

  if (input.startsWith(F("feeddecel:"))) {
    feedDecelOverOffset = toBool(input.substring(10));
    host.println(F("ok")); return;
  }
  if (input.startsWith(F("airdropenabled:"))) {
    airDrop = toBool(input.substring(15));
    host.println(F("ok")); return;
  }
  if (input.startsWith(F("sg:"))) {
    sgEnabled = toBool(input.substring(3));
    host.println(F("ok")); return;
  }
  if (input.startsWith(F("sgtcool:"))) {
    sgTcool = (uint32_t)input.substring(8).toInt();
    feedDrv.TCOOLTHRS(sgTcool); sortDrv.TCOOLTHRS(sgTcool);
    host.println(F("ok")); return;
  }
  if (input.startsWith(F("sgprobe:"))) {           // 2.0: telemetry always on
    host.println(F("ok")); return;
  }
  if (input.startsWith(F("sortaxis:"))) {
    sortAxis = toBool(input.substring(9));
    persistSortAxis();
    if (!sortAxis) { sortState = S_IDLE; sortHomed = true; }
    else { sortState = S_UNHOMED; sortHomed = false; }
    host.println(F("ok")); return;
  }
  if (input.startsWith(F("ledcolor:"))) {          // r,g,b (WS2812 era)
    int c1 = input.indexOf(',', 9), c2 = input.indexOf(',', c1 + 1);
    if (c1 > 0 && c2 > c1) {
      ringR = clampi(input.substring(9, c1).toInt(), 0, 255);
      ringG = clampi(input.substring(c1 + 1, c2).toInt(), 0, 255);
      ringB = clampi(input.substring(c2 + 1).toInt(), 0, 255);
      ringShow();
      host.println(F("ok"));
    } else host.println(F("error:ledcolor wants r,g,b"));
    return;
  }
  if (input.startsWith(F("chop:"))) {              // toff,tbl,pwmfreq (sort)
    int c1 = input.indexOf(',', 5), c2 = input.indexOf(',', c1 + 1);
    if (c1 > 0 && c2 > c1) {
      int t = clampi(input.substring(5, c1).toInt(), 2, 15);
      int b = clampi(input.substring(c1 + 1, c2).toInt(), 0, 3);
      int p = clampi(input.substring(c2 + 1).toInt(), 0, 3);
      sortDrv.toff(t);
      sortDrv.blank_time(b == 0 ? 16 : b == 1 ? 24 : b == 2 ? 36 : 54);
      sortDrv.pwm_freq(p);
      host.println(F("ok"));
    } else host.println(F("error:chop wants toff,tbl,pwmfreq"));
    return;
  }
  host.println(F("?"));
}

// =====================================================================
void setup() {
  Serial1.setFIFOSize(256);
  Serial1.begin(9600);                  // Pi header UART — the machine link
  Serial.begin(9600);                   // USB mirror
  Serial2.begin(115200);                // TMC bus
  EEPROM.begin(64);
  if (EEPROM.read(0) == 0xA5) sortAxis = EEPROM.read(1) != 0;

  pinMode(FEED_HOME, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(FEED_HOME), feedHomeIsr, FALLING);
  pinMode(FEED_DIAG, INPUT);
  pinMode(SORT_HOME, INPUT_PULLUP);
  pinMode(PROX_PIN, INPUT_PULLUP);
  pinMode(FEED_EN, OUTPUT); digitalWrite(FEED_EN, LOW);
  pinMode(SORT_EN, OUTPUT); digitalWrite(SORT_EN, LOW);
  pinMode(AUX_FAN1, OUTPUT); digitalWrite(AUX_FAN1, HIGH);
  pinMode(AUX_FAN2, OUTPUT); digitalWrite(AUX_FAN2, HIGH);
  pinMode(CASEFAN, OUTPUT); analogWrite(CASEFAN, caseFanLevel * 255 / 100);
  pinMode(AIRDROP_PIN, OUTPUT); digitalWrite(AIRDROP_PIN, LOW);

  ring.begin();
  ringShow();
  fillSlotTab();
  delay(400);

  motorPower = driversPresent();
  if (motorPower) applyDriverConfig();

  engine.init();
  feedSt = engine.stepperConnectToPin(FEED_STEP);
  sortSt = engine.stepperConnectToPin(SORT_STEP);
  if (feedSt) { feedSt->setDirectionPin(FEED_DIR, true); feedApplyMotion(); }
  if (sortSt) { sortSt->setDirectionPin(SORT_DIR, true); sortApplyMotion(); }
  // FastAccelStepper's PIO claim stomps GPIO 0's pin mux (UART0 TX = the
  // Pi machine link) on this core — bisected on the bench: TX died at the
  // first stepperConnectToPin, RX untouched. Hand the pins back to UART.
  gpio_set_function(0, GPIO_FUNC_UART);
  gpio_set_function(1, GPIO_FUNC_UART);

  host.println(F("Ready"));
  if (!motorPower) host.println(F("info:motor power off"));
  else sortStartHoming();
}

void loop() {
  if (recvFrom(Serial1, inPi) || recvFrom(Serial, inUsb)) {
    if (cmdReady) { cmdReady = false; handleCommand(); }
  }
  sortService();
  feedService();
  checkMotorPower();
  outPi.pump();
  outUsb.pump();
}
