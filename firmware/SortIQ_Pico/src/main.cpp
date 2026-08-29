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
  // sort ihold was 16 (50% hold) for StallGuard-at-rest experiments; SG is
  // telemetry-only now and the motor sat warm at idle. 8 = same as feed.
  sortDrv.rms_current(sortCurrent); sortDrv.ihold(8);
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

void feedHardStop();
void sortHardStop();
void motorsWake();

// ---------------- stats ----------------
uint32_t feedCycles = 0, feedStalls = 0, sortStalls = 0, sortSkips = 0;
uint32_t powerFlaps = 0, restOffTab = 0;
long lastSeek = 0, maxSeek = 0;
uint32_t sgLastN = 0, sgLastSum = 0; uint16_t sgLastMin = 1023;

// =====================================================================
// SORT axis — flag-relative state machine
// =====================================================================
enum SortState { S_IDLE, S_UNHOMED, S_SEEK, S_BACKOFF_LEAVE, S_BACKOFF_RUN,
                 S_APPROACH, S_HOME_SETTLE, S_MOVING, S_SEEK_ARM };
SortState sortState = S_UNHOMED;
long sortFlagPos = 0;                    // absolute ustep position of the flag edge
bool sortHomed = false;
int  sortSlot = 0;                       // logical slot the arm sits at / heads to
uint32_t sortT0 = 0;                     // state timer
uint32_t lastSortArrive = 0;
bool sortMoveQueuedDone = false;         // a cycle waits on this arm move

// Mid-position flag support: the tab may be mounted anywhere along the
// slot arc. sortFlagOffset = usteps from SLOT 0 up to the flag's upper
// edge (0 = flag at slot 0, the classic geometry — all crossing checks
// are inert at 0). With the flag mid-range, every move crossing its zone
// becomes a checkpoint: the flag must appear inside the zone (presence),
// must NOT appear elsewhere (two-sided audit), and a crossing move that
// never sees it fails loudly (absence check — catches lost steps AND a
// dead sensor). Downward crossings measure the true upper edge by ISR;
// small drift re-anchors the frame silently, large drift aborts.
int sortFlagOffset = 0;       // usteps, slot 0 -> flag upper edge
int sortFlagWidth = 240;      // usteps, tab width (zone below upper edge)
volatile bool sortEdgeArm = false, sortEdgeSeen = false;
volatile uint32_t sortEdgeUs = 0;
bool sortEdgeCalced = false;
bool sortCrossExpect = false, sortSawFlag = false, sortMovingDown = false;
uint32_t sortCrossChecks = 0, sortReanchors = 0;
long sortLastDrift = 0;
void sortHomeIsr() {
  if (sortEdgeArm && !sortEdgeSeen) { sortEdgeUs = micros(); sortEdgeSeen = true; }
}

long sortTargetAbs(int slot) {
  return sortFlagPos + (long)sortHomingOffset * USTEP - (long)sortFlagOffset
         + slotPosTab[slot];
}

void sortApplyMotion() {
  sortSt->setSpeedInHz(speedToHz(sortSpeedSet));
  sortSt->setAcceleration(accFToSS2(sortAccF));    // FAS: one accel per move;
}                                                  // decel factor -> future FAS api

void sortStartHoming() {
  if (!sortAxis || !motorPower) { sortState = S_IDLE; sortHomed = sortAxis ? false : true; return; }
  motorsWake();
  if (sortSt->isRunning()) sortHardStop();    // re-home over any motion
  sortHomed = false;
  sortSt->setAcceleration(accFToSS2(sortAccF));
  // Homing geometry (machine-verified): the flag lies BELOW slot 0 in the
  // count frame. Seek runs DOWN onto the flag; the edge is then measured
  // by leaving it UPWARD (toward the slots) and creeping back DOWN onto
  // it — so the frame anchor is the repeatable upper edge, and the flag
  // zone lies strictly below flagPos (which is what the audit assumes).
  // Parked ON the flag already (normal after a reboot): skip the seek.
  if (digitalRead(SORT_HOME) == LOW) {
    sortT0 = millis();
    sortState = S_BACKOFF_LEAVE;
    return;
  }
  // FAS swallows a motion command issued in the same breath as a
  // forceStop (async stop processing, same family as its async position
  // writes — spike-documented, then forgotten here: the seek 'ran' with
  // the motor frozen for its whole timeout). Arm the seek lazily.
  sortT0 = millis();
  sortState = S_SEEK_ARM;
}

void sortHomingFailed() {
  sortHardStop();
  sortState = S_IDLE; sortHomed = false;
  host.println(F("error:sort homing failed"));
}

void sortSkipAbort() {                             // steps were lost: frame dead
  sortHardStop();
  sortStalls++; sortSkips++;
  sortEdgeArm = false; sortCrossExpect = false;
  sortState = S_IDLE; sortHomed = false;
  host.println(F("error:sort stall detected"));
  sortStartHoming();                               // self-heal like the fork
}

void sortGoTo(int slot) {                          // called only when S_IDLE+homed
  if (slot < 0) slot = 0; if (slot >= MAX_SLOTS) slot = MAX_SLOTS - 1;
  uint32_t since = millis() - lastSortArrive;      // settle: rapid commands
  // 1.x parity: AirDrop REPLACES the baseline rest (blasted cases clear
  // the chute faster); Arm dwell is extra margin on top in both modes
  int base = airDrop ? airPost : slotDropDelay;
  uint32_t dwell = (uint32_t)(base > 0 ? base : 150) + (uint32_t)armDwellMs;
  if (since < dwell) delay(dwell - since);         // bounded, protocol-visible
  sortSlot = slot;
  sortApplyMotion();
  sgLastN = 0; sgLastSum = 0; sgLastMin = 1023;
  long start = (long)sortSt->getCurrentPosition();
  long target = sortTargetAbs(slot);
  long zLow = sortFlagPos - sortFlagWidth, zHigh = sortFlagPos;
  sortCrossExpect = sortAxis && sortFlagOffset > 0 &&
                    ((start < zLow - 64 && target > zHigh + 64) ||
                     (start > zHigh + 64 && target < zLow - 64));
  sortMovingDown = target < start;
  sortSawFlag = false; sortEdgeCalced = false; sortEdgeSeen = false;
  sortEdgeArm = sortCrossExpect;
  if (sortCrossExpect) sortCrossChecks++;
  sortSt->moveTo(target);
  sortState = S_MOVING;
}

void sortService() {
  switch (sortState) {
    case S_IDLE: case S_UNHOMED: return;
    case S_SEEK_ARM:                               // standstill + settle first:
      if (!sortSt->isRunning() && millis() - sortT0 > 150) {
        sortSt->setSpeedInHz(4000);
        sortSt->setAcceleration(accFToSS2(sortAccF));
        sortSt->runBackward();
        sortT0 = millis();
        sortState = S_SEEK;
      } else if (millis() - sortT0 > 4000) sortHomingFailed();
      return;
    case S_SEEK:
      if (digitalRead(SORT_HOME) == LOW) {
        sortHardStop(); sortT0 = millis(); sortState = S_BACKOFF_LEAVE;
      } else if (millis() - sortT0 > 8000) sortHomingFailed();
      return;
    case S_BACKOFF_LEAVE:                          // wait for standstill
      if (!sortSt->isRunning() && millis() - sortT0 > 120) {
        sortSt->setSpeedInHz(1500);
        sortSt->runForward();                      // leave the flag UPWARD
        sortT0 = millis(); sortState = S_BACKOFF_RUN;
      }
      return;
    case S_BACKOFF_RUN:
      if (digitalRead(SORT_HOME) != LOW) {         // flag released: margin, stop
        sortSt->move((long)sortHomeBackoff);
        sortT0 = millis(); sortState = S_APPROACH;
      } else if (millis() - sortT0 > 5000) sortHomingFailed();
      return;
    case S_APPROACH:
      if (!sortSt->isRunning()) {
        long us = sortHomeSlow > 0 ? sortHomeSlow : 1400;   // creep us/ustep
        sortSt->setSpeedInHz((uint32_t)(1000000L / us));
        sortSt->runBackward();                     // creep DOWN onto the edge
        sortT0 = millis(); sortState = S_HOME_SETTLE;
      } else if (millis() - sortT0 > 4000) sortHomingFailed();
      return;
    case S_HOME_SETTLE:
      if (digitalRead(SORT_HOME) == LOW) {         // the repeatable edge
        sortHardStop();
        delay(80);                                  // let queued steps drain
        sortFlagPos = (long)sortSt->getCurrentPosition();
        sortHomed = true; sortSlot = 0;
        lastSortArrive = millis();
        sortApplyMotion();
        // rest at slot 0 (with a mid-range flag this is a DOWN move)
        sortSt->moveTo(sortTargetAbs(0));
        sortState = S_IDLE;
      } else if (millis() - sortT0 > 6000) sortHomingFailed();
      return;
    case S_MOVING: {
      long pos = (long)sortSt->getCurrentPosition();
      long zLow = sortFlagPos - sortFlagWidth, zHigh = sortFlagPos;
      // ISR-latched entry edge on a crossing move: measure frame drift.
      // Moving DOWN enters at the upper edge — the true homing anchor —
      // so small drift re-anchors the frame (continuous self-calibration).
      // Moving UP enters at the lower edge, whose position inherits the
      // width setting's uncertainty: audited with a generous window only.
      if (sortEdgeSeen && !sortEdgeCalced) {
        uint32_t lateUs = micros() - sortEdgeUs;
        int32_t vm = sortSt->getCurrentSpeedInMilliHz(); if (vm < 0) vm = -vm;
        long lateSteps = (long)((uint64_t)lateUs * (uint32_t)(vm / 1000) / 1000000ULL);
        long meas = sortMovingDown ? pos + lateSteps : pos - lateSteps;
        sortEdgeCalced = true; sortSawFlag = true;
        long drift = meas - (sortMovingDown ? zHigh : zLow);
        sortLastDrift = drift;
        if (sortMovingDown) {
          if (drift >= -48 && drift <= 48) {
            sortFlagPos += drift; sortReanchors++;
          } else { sortSkipAbort(); return; }
        } else if (drift < -160 || drift > 160) { sortSkipAbort(); return; }
      }
      // polled two-sided audit: the flag seen far outside its zone = lost
      // steps (also marks legitimate in-zone sightings for the absence check)
      if (digitalRead(SORT_HOME) == LOW) {
        if (pos > zLow - 240 && pos < zHigh + 240) sortSawFlag = true;
        else { sortSkipAbort(); return; }
      }
      // SG telemetry (blocking UART read costs the motion nothing on PIO)
      uint16_t r = sortDrv.SG_RESULT();
      if (r == 0) r = sortDrv.SG_RESULT();
      if (r > 0) { sgLastN++; sgLastSum += r; if (r < sgLastMin) sgLastMin = r; }
      if (!sortSt->isRunning()) {
        // absence check: a crossing move that never saw the flag lost
        // steps somewhere (or the sensor is dead) — same recovery
        if (sortCrossExpect && !sortSawFlag) { sortSkipAbort(); return; }
        sortEdgeArm = false; sortCrossExpect = false;
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
                 F_SEEK, F_OFFSET, F_NOTIFY, F_HOME, F_LAUNCH };
FeedState feedState = F_IDLE;
bool forceFeed = false;
bool slotQueued = true;
int qSlot = 0;
uint32_t feedT0 = 0, waitMsgT = 0;
long feedSeekStart = 0;
int feedSgHits = 0, feedSgGate = 0;
int feedSortHomeTries = 0;
bool feedHomeResume = false;      // F_HOME returns to the brass wait
bool feedRealigned = false;       // one realign per wait
uint32_t feedWaitStart = 0;
long feedSgArmPos = -1;
long feedSgEvalPos = LONG_MIN;

// Flag edge by INTERRUPT, not loop polling: the PIO keeps stepping while the
// CPU services serial or TMC UART, so a polled edge is seen late (or a narrow
// tab is missed outright — bench-measured 100+ usteps of jitter). The ISR
// timestamps the true edge; the service latency-corrects the position with
// elapsed-time * current-rate.
//
// ONLY a falling edge counts — never a level read. A level read on a wheel
// that stopped inside the tab yields a mid-tab pseudo-edge, and that error
// carries into the next cycle's start with nothing to reset it (bench:
// cycles alternating between instant stop, a barely-move, and a full extra
// pitch). A true leading edge each cycle is an absolute reference.
//
// The ISR is NOT armed at cycle start: a cycle begins parked at/near the
// tab edge, and launch vibration chatters the opto — a bogus falling edge
// in the first steps anchors the cycle to the start position and the wheel
// stops short of the sensor. Arm only after the service has CONFIRMED the
// wheel clear of the tab (pin HIGH, 10+ full steps into the cycle); from a
// confirmed-clear wheel, the next falling edge is a real tab entry.
volatile bool feedEdgeArm = false;
volatile bool feedEdgeSeen = false;
volatile uint32_t feedEdgeUs = 0;
bool feedEdgeCalced = false;
long feedEdgeAbs = 0;
bool feedEdgeInBlind = false;
long feedCycleStart = 0;
long feedClearStart = 0;
bool feedDebug = false;
long feedArmAt = 0;
uint32_t feedEdgeLatUs = 0;
// Predictive creep: braking from cruise inside the small offset is
// physically impossible (bench: ~25 full steps of forward rotor slip at
// speed 96 — 1.x commanded the same stop and simply never knew). 2.0
// learns the tab pitch, ramps down to a creep BEFORE the predicted edge,
// and takes the edge at creep speed: an exact, slip-free stop.
long feedPrevEdge = LONG_MIN;
long feedPitchEst = 0;                             // EMA of tab pitch, usteps
long feedCreepAt = 0;
bool feedCreeping = false;
#define FEED_CREEP_HZ 4000
#define FEED_CREEP_RAMP 200000
// lead covers the FAS step queue (~300 usteps at cruise), the ramp-down
// distance, and tab placement jitter
#define FEED_CREEP_LEAD 450
volatile uint32_t feedFallCount = 0;               // every falling edge, all cycle
void feedHomeIsr() {
  feedFallCount++;
  if (feedEdgeArm && !feedEdgeSeen) { feedEdgeUs = micros(); feedEdgeSeen = true; }
}

static void feedEdgeMaybeArm() {
  if (feedEdgeArm || feedEdgeCalced) return;
  long pos = (long)feedSt->getCurrentPosition();
  // the opto chatters at BOTH tab boundaries; a single HIGH sample can be
  // exit chatter. Demand 4 full steps of uninterrupted clear before arming.
  if (digitalRead(FEED_HOME) == LOW) { feedClearStart = pos; return; }
  if (pos - feedClearStart >= 64 && pos > feedCycleStart + 160) {
    feedEdgeSeen = false;
    feedEdgeArm = true;
    feedArmAt = pos;
  }
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
  feedEdgeLatUs = lateUs;
  feedEdgeCalced = true;
}

void feedApplyMotion() {
  feedSt->setSpeedInHz(speedToHz(feedSpeedSet));
  feedSt->setAcceleration(accFToSS2(feedAccF));
}

// The feed stepper must NEVER forceStop(): with its short forward-planning
// queue (set for the predictive creep), FAS's forceStop wedges the queue —
// isRunning() sticks true until reboot and every later cycle dies in
// F_LAUNCH (bench-bisected: any feed forceStop killed the wheel for the
// rest of the boot; the sort, on default planning, is unaffected). A
// max-decel stopMove() is the same near-dead stop through the sane path.
void feedHardStop() {
  feedSt->setAcceleration(4000000);
  feedSt->stopMove();
}

// same disease on the sort axis (field trace: the homing dance's runForward
// swallowed after a forceStop, position frozen through every retry): the
// sort never forceStops either.
void sortHardStop() {
  sortSt->setAcceleration(4000000);
  sortSt->stopMove();
}

// Motor standby (AutoMotorStandbyTimeout, seconds; 0 = never): after the
// machine sits idle that long, both drivers de-energize so the motors run
// cool. Any motion request wakes them; the sort frame is treated as lost
// (a limp arm can be nudged), and the existing self-heal re-homes it.
uint32_t lastMotionMs = 0;
bool motorsInStandby = false;
void motorsWake() {
  lastMotionMs = millis();
  if (!motorsInStandby) return;
  motorsInStandby = false;
  digitalWrite(FEED_EN, LOW);
  digitalWrite(SORT_EN, LOW);
  delay(60);
  sortHomed = false; sortState = S_UNHOMED;
  host.println(F("info:motors wake"));
}
void checkMotorStandby() {
  bool busy = feedState != F_IDLE ||
              (sortState != S_IDLE && sortState != S_UNHOMED) ||
              feedSt->isRunning() || sortSt->isRunning();
  if (busy) { lastMotionMs = millis(); return; }
  if (motorsInStandby || motorStandby <= 0 || !motorPower) return;
  if (millis() - lastMotionMs > (uint32_t)motorStandby * 1000UL) {
    motorsInStandby = true;
    digitalWrite(FEED_EN, HIGH);
    digitalWrite(SORT_EN, HIGH);
    sortHomed = false; sortState = S_UNHOMED;
    host.println(F("info:motor standby"));
  }
}

void feedAbort(const __FlashStringHelper *err) {
  feedHardStop();
  feedEdgeArm = false;
  feedPrevEdge = LONG_MIN;   // edge chain broken: don't learn a fake pitch
  feedState = F_IDLE; forceFeed = false;
  host.println(err);
}

// bare feed home: slow seek to the tab edge, stop AT it, no offset.
// A wheel already resting on the tab is home (the feed frame re-anchors
// every cycle; it does not need the absolute edge at rest).
// deliberate=true (the Home feeder button): an on-tab wheel is NOT trusted
// as aligned — a stop that slipped under brass load can rest misaligned yet
// still on the wide tab (field-hit: rehome was a silent no-op until the
// operator dragged the wheel off the tab by hand). A deliberate home rides
// forward out of the tab and anchors on the NEXT tab's true leading edge —
// forward-only, advancing one pocket. Automatic homes (boot, run-end, the
// mid-run realign) stay deliberate=false: on-tab is a no-op so they can
// never advance a pocket / drop a case uncommanded.
void feedStartManualHome(bool deliberate) {
  if (feedState != F_IDLE) return;               // busy — a cycle owns the wheel
  motorsWake();
  bool onTab = digitalRead(FEED_HOME) == LOW;
  if (onTab && !deliberate) return;
  feedApplyMotion();
  feedSt->setSpeedInHz(2500);                    // 1.x homefeeder pace (400 us)
  feedEdgeSeen = false; feedEdgeCalced = false; feedEdgeInBlind = false;
  feedEdgeArm = false;
  feedSeekStart = (long)feedSt->getCurrentPosition();
  if (onTab) {
    // must clear this tab before an edge can be trusted: no credits —
    // the arm logic waits for 4 steps of clean HIGH after the exit
    host.println(F("info:on tab - advancing to the next tab edge"));
    feedCycleStart = feedSeekStart;
    feedClearStart = feedSeekStart;
  } else {
    feedCycleStart = feedSeekStart - 160;        // pin verified HIGH at rest:
    feedClearStart = feedSeekStart - 64;         // credit the standstill, arm at once
  }
  feedT0 = millis();
  feedSt->runForward();
  feedState = F_HOME;
}

// tab edge in hand (feedEdgeAbs): ONE deceleration to edge + offset,
// forward-only. 1.x parity: crisp mode crosses the offset at cruise and
// dead-stops; decel-over-offset shapes the whole offset down to the
// feeddecel end speed. Never reverse across the drop port — an overrun
// stops just past home (bounded; the next cycle re-anchors on its edge).
void feedFinishHome() {
  feedEdgeArm = false;
  long pos = (long)feedSt->getCurrentPosition();
  long stopAt = feedEdgeAbs + (long)feedHomingOffset * USTEP;
  lastSeek = feedEdgeAbs - feedCycleStart - (long)feedSteps * USTEP;
  if (lastSeek < 0) lastSeek = 0;
  if (lastSeek > maxSeek) maxSeek = lastSeek;
  long remain = stopAt - pos;
  int32_t vm = feedSt->getCurrentSpeedInMilliHz();
  if (vm < 0) vm = -vm;
  uint32_t v = (uint32_t)(vm / 1000);
  // learn the pitch ONLY from slip-free edges (taken at creep speed) —
  // a cruise-taken edge follows a slipped stop and reads short
  if (feedPrevEdge != LONG_MIN && v <= FEED_CREEP_HZ + 800) {
    long dp = feedEdgeAbs - feedPrevEdge;
    if (dp > 600 && dp < 3000)
      feedPitchEst = feedPitchEst > 0 ? (feedPitchEst * 3 + dp) / 4 : dp;
  }
  feedPrevEdge = feedEdgeAbs;
  if (remain < 16) {
    feedHardStop();                          // at/past the mark: dead stop
  } else if (!feedDecelOverOffset) {
    feedSt->setAcceleration(4000000);        // cruise the offset, dead stop
    feedSt->moveTo(stopAt);
  } else {
    uint32_t vend = (feedDecF > 0) ? (1000000UL / (uint32_t)feedDecF) : 0;
    uint64_t vv = (uint64_t)v * v;
    uint64_t ee = (uint64_t)vend * vend;
    uint64_t a = (vv > ee) ? (vv - ee) / (uint64_t)(2L * remain) : 5000;
    if (a < 5000) a = 5000;
    if (a > 4000000ULL) a = 4000000ULL;
    feedSt->setAcceleration((uint32_t)a);
    feedSt->moveTo(stopAt);
  }
  if (feedDebug) {
    host.print(F("fdbg start=")); host.print(feedCycleStart);
    host.print(F(" arm=")); host.print(feedArmAt);
    host.print(F(" edge=")); host.print(feedEdgeAbs);
    host.print(F(" lat=")); host.print(feedEdgeLatUs);
    host.print(F(" v=")); host.print(v);
    host.print(F(" nfall=")); host.print(feedFallCount);
    host.print(F(" pitch=")); host.print(feedPitchEst);
    host.print(F(" stop=")); host.println(stopAt);
  }
  feedState = F_OFFSET;
}

void feedStartCycle() {                            // after the arm is parked
  feedApplyMotion();
  feedEdgeSeen = false; feedEdgeCalced = false; feedEdgeInBlind = false;
  feedEdgeArm = false;                             // armed once confirmed clear
  feedCycleStart = (long)feedSt->getCurrentPosition();
  feedClearStart = feedCycleStart;
  feedSeekStart = feedCycleStart;
  feedSgHits = 0; feedSgGate = 0; feedSgArmPos = -1; feedSgEvalPos = LONG_MIN;
  sgLastN = 0; sgLastSum = 0; sgLastMin = 1023;
  // ONE continuous run to the flag, 1.x-style. The port's blind-move /
  // stop / re-launch / seek / stop shape put three violent transitions
  // where 1.x has one, and the motor slipped steps at them (bench: pitch
  // readings wandering 685-1316 on a wheel proven even to +/-2 steps).
  // "Blind" is now only a distance during which the edge is not yet
  // trusted, not a separate move.
  feedFallCount = 0;
  feedCreeping = false;
  long blindEnd = feedCycleStart + (long)feedSteps * USTEP;
  long bootstrap = blindEnd - FEED_CREEP_LEAD;   // unlearned: creep the seek
  if (feedPitchEst > 0 && feedPrevEdge != LONG_MIN)
    feedCreepAt = feedPrevEdge + feedPitchEst - FEED_CREEP_LEAD;
  else
    feedCreepAt = bootstrap;
  if (feedCreepAt < feedCycleStart + 64 || feedCreepAt > blindEnd + 6400)
    feedCreepAt = bootstrap;                       // stale frame: be safe
  if (feedCreepAt < feedCycleStart + 64) feedCreepAt = feedCycleStart + 64;
  feedT0 = millis();
  feedState = F_LAUNCH;                            // same forceStop-swallow
}                                                  // guard as the sort seek

void feedService() {
  switch (feedState) {
    case F_IDLE: return;
    case F_WAIT_SORT:
      if (sortState == S_IDLE && (sortHomed || !sortAxis)) {
        feedStartCycle();
      } else if ((sortState == S_IDLE || sortState == S_UNHOMED) &&
                 sortAxis && !sortHomed) {
        // arm idle but unhomed (a stopped run, a failed home): self-heal —
        // home it now and keep waiting; the cycle proceeds once the frame
        // is real. Feeding over an unhomed arm mis-bins every case.
        if (++feedSortHomeTries > 2) {             // physically blocked arm:
          feedAbort(F("error:sort homing failed"));// don't grind forever
          return;
        }
        sortStartHoming();
        host.println(F("info:sort arm re-homing"));
      }
      return;
    case F_WAIT_BRASS:
      if (waitMsgT == 0) { waitMsgT = millis(); feedWaitStart = millis(); }
      if (forceFeed || digitalRead(PROX_PIN) == HIGH) {
        feedT0 = millis();
        feedState = forceFeed ? F_WAIT_SORT : F_DEBOUNCE;
        return;
      }
      // a slip event that misaligns the pocket kills brass STAGING, and
      // with no cycles running nothing re-anchors — the wheel starves
      // until the app's end-of-brass flush (field-hit). If the wait drags
      // and the wheel is NOT resting on its tab, re-home it once: a
      // misaligned pocket realigns (brass usually stages right after); a
      // truly dry hopper pays nothing (on-tab home is a no-op).
      if (!feedRealigned && millis() - feedWaitStart > 3000 &&
          digitalRead(FEED_HOME) == HIGH) {
        feedRealigned = true;
        feedHomeResume = true;
        host.println(F("info:feed wheel re-aligning"));
        feedApplyMotion();
        feedSt->setSpeedInHz(2500);
        feedEdgeSeen = false; feedEdgeCalced = false; feedEdgeInBlind = false;
        feedEdgeArm = false;
        feedSeekStart = (long)feedSt->getCurrentPosition();
        feedCycleStart = feedSeekStart - 160;
        feedClearStart = feedSeekStart - 64;
        feedT0 = millis();
        feedSt->runForward();
        feedState = F_HOME;
        return;
      }
      if (millis() - waitMsgT > 1000) {
        host.println(F("waiting for brass"));
        waitMsgT = millis();
      }
      return;
    case F_DEBOUNCE:
      if (digitalRead(PROX_PIN) != HIGH) { feedState = F_WAIT_BRASS; waitMsgT = 0; return; }
      if (millis() - feedT0 >= (uint32_t)debounceTime) feedState = F_WAIT_SORT;
      return;
    case F_LAUNCH:                                 // standstill + settle before
      if (!feedSt->isRunning() && millis() - feedT0 > 150) {
        feedSt->runForward();                      // motion: FAS swallows a run
        feedT0 = millis();                         // issued right on a forceStop
        feedState = F_BLIND;
      } else if (millis() - feedT0 > 4000) {
        feedAbort(F("error:feed overtravel detected"));
      }
      return;
    case F_BLIND: {
      // feed jam detection, the 1.x two-stage detector: DIAG is a free
      // tripwire (asserts when SG_RESULT < 2*SGTHRS); only when it
      // accumulates do we pay for ONE UART confirm read. Continuous UART
      // sampling both false-trips on transient dips 1.x never saw and
      // blocks the loop ~10 ms per read.
      feedEdgeMaybeArm();
      if (!feedCreeping &&
          (long)feedSt->getCurrentPosition() >= feedCreepAt) {
        feedSt->setAcceleration(FEED_CREEP_RAMP);
        feedSt->setSpeedInHz(FEED_CREEP_HZ);
        feedSt->applySpeedAcceleration();          // ramp down while running
        feedCreeping = true;
      }
      if (!feedCreeping && sgEnabled && feedSgThrs > 0 &&
          (feedSt->rampState() & RAMP_STATE_COAST)) {
        // 1.x SG_ARM_STEPS parity: SG_RESULT reads ~0 for the first ~8 full
        // steps after a standstill and DIAG asserts through the settle.
        // The gate advances per USTEP, not per loop pass — 1.x evaluated
        // DIAG once per step pulse, and the loop spins ~10x faster than
        // the step train, which made the gate ~10x too twitchy.
        long pn = (long)feedSt->getCurrentPosition();
        if (feedSgArmPos < 0) feedSgArmPos = pn + 256;
        bool sgArmed = pn >= feedSgArmPos && pn != feedSgEvalPos;
        if (sgArmed) feedSgEvalPos = pn;
        if (sgArmed && digitalRead(FEED_DIAG) == HIGH) { feedSgGate++; }
        else if (sgArmed && feedSgGate > 0) { feedSgGate--; }
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
      feedEdgeResolve();
      if (feedEdgeCalced) { feedFinishHome(); return; }
      // overtravel: two pitches past the blind with no tab = jammed wheel
      // skipping under the wedge (the SG tripwire is creep-blind by design)
      long budget = feedPitchEst > 0 ? 2 * feedPitchEst : 3200;
      long trav = (long)feedSt->getCurrentPosition() - feedCycleStart;
      if (trav > (long)feedSteps * USTEP + budget ||
          millis() - feedT0 > 8000) {
        feedAbort(F("error:feed overtravel detected"));
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
        if (digitalRead(FEED_HOME) == HIGH) restOffTab++;  // stop slipped off tab
        feedState = F_IDLE; forceFeed = false;
        host.println(F("done"));
      }
      return;
    case F_HOME: {                               // bare home: slow seek, stop
      feedEdgeMaybeArm();                        // AT the edge, no offset
      feedEdgeResolve();
      if (feedEdgeCalced) {
        feedEdgeArm = false;
        long pos = (long)feedSt->getCurrentPosition();
        // park at the OPERATING rest (edge + offset), exactly where a
        // cycle parks — a bare home used to stop at edge+0, leaving the
        // wheel 7 steps short of the tuned alignment after every
        // run-end/boot home ("off home" on inspection, field-hit)
        long stopAt = feedEdgeAbs + (long)feedHomingOffset * USTEP;
        if (stopAt < pos + 4) stopAt = pos + 4;  // forward-only
        feedSt->setAcceleration(500000);
        feedSt->moveTo(stopAt);
        feedPrevEdge = feedEdgeAbs;              // seed the pitch predictor
        if (feedHomeResume) {
          feedHomeResume = false;
          feedState = F_WAIT_BRASS;              // back to the brass wait
          waitMsgT = millis(); feedWaitStart = millis();
        } else {
          feedState = F_IDLE;
        }
        return;
      }
      long trav = (long)feedSt->getCurrentPosition() - feedSeekStart;
      if (trav > 400L * USTEP || millis() - feedT0 > 8000) {
        feedAbort(F("error:feed overtravel detected"));
      }
      return;
    }
  }
}

void startPipelinedFeed(int slot, bool force) {    // pf / xf:N
  motorsWake();
  if (feedState != F_IDLE) {
    // recovery paths overlap: the app's HOME (a seconds-long homing dance)
    // is chased by its re-prime xf within ~1s, and blindly overwriting the
    // state machine left the wheel running into a mangled cycle -> error ->
    // HOME again, forever (the first field run died in this loop). Yield
    // whatever is in flight — every cycle re-anchors on its own edge.
    feedHardStop();
    feedEdgeArm = false;
    feedPrevEdge = LONG_MIN; // edge chain broken: don't learn a fake pitch
    feedState = F_IDLE;
  }
  forceFeed = force;
  feedSortHomeTries = 0;
  feedRealigned = false; feedHomeResume = false;
  if (sortAxis && sortHomed) sortGoTo(slot);       // arm first (dwell inside)
  qSlot = slot;
  // brass wait + debounce run CONCURRENTLY with the arm move (pure sensor
  // time, no motion); the wheel's MOTION gates on the parked arm in
  // F_WAIT_SORT afterwards — brass still cannot drop before the arm is
  // under the port, but the arm no longer sits through the debounce.
  feedState = F_WAIT_BRASS; waitMsgT = 0;
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
  // poll only at true rest: a glitched TMC read mid-homing used to declare
  // power lost and strand the arm S_UNHOMED (S_UNHOMED itself must still
  // poll — it IS the power-off resting state)
  if (feedState != F_IDLE ||
      (sortState != S_IDLE && sortState != S_UNHOMED)) return;
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
    feedStartManualHome(false);                    // both axes, 1.x boot parity
  } else {
    powerFlaps++;
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
  host.print(F(",\"SortFlagOffset\":")); host.print(sortFlagOffset);
  host.print(F(",\"SortFlagWidth\":")); host.print(sortFlagWidth);
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
    feedHardStop(); sortHardStop();
    feedEdgeArm = false;
    feedPrevEdge = LONG_MIN; // edge chain broken: don't learn a fake pitch
    feedState = F_IDLE; forceFeed = false;
    if (sortState == S_MOVING) {
      sortState = S_IDLE;                          // position frame intact
    } else if (sortState != S_IDLE && sortState != S_UNHOMED) {
      // stop landed mid-HOMING: the frame is not established. Leaving the
      // homing state running with a dead motor let its timeout fire
      // 'sort homing failed' seconds later and strand the arm unhomed
      // (field-hit on the first real sort run's stop/restart).
      sortState = S_UNHOMED;
      sortHomed = false;
    }
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
    host.print(F(" feedEdge=")); host.print(feedEdgeAbs);
    host.print(F(" fpos=")); host.print((long)feedSt->getCurrentPosition());
    host.print(F(" flaps=")); host.print(powerFlaps);
    host.print(F(" xchk=")); host.print(sortCrossChecks);
    host.print(F(" reanch=")); host.print(sortReanchors);
    host.print(F(" drift=")); host.print(sortLastDrift);
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
    host.print(F("{\"RestOffTab\":")); host.print(restOffTab);
  host.print(F(",\"PowerFlaps\":")); host.print(powerFlaps);
  host.print(F(",\"LastHomingSteps\":")); host.print(lastSeek);
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

  if (input == "bootloader") {
    // drop to the UF2 bootloader (RPI-RP2 drive) — reachable over the Pi
    // UART link so the Pi can flash the board without anyone touching it
    host.println(F("ok:rebooting to bootloader"));
    outPi.pump(); outUsb.pump();
    delay(120);                                    // let the ack leave the wire
    rp2040.rebootToBootloader();
    return;
  }
  if (input.startsWith(F("feeddebug:"))) {
    feedDebug = input.substring(10).toInt() != 0;
    host.println(F("ok"));
    return;
  }
  if (input.startsWith(F("armfree:"))) {
    // guided-setup helper: release ONLY the sorter arm so the operator can
    // position it by hand (feed stays held; fans, ring, board untouched).
    // armfree:0 re-energizes and re-homes — the frame is unknown after
    // hand moves by definition.
    bool freeArm = input.substring(8).toInt() != 0;
    host.println(F("ok"));
    motorsWake();
    if (freeArm) {
      sortHardStop();
      digitalWrite(SORT_EN, HIGH);
      sortHomed = false; sortState = S_UNHOMED;
    } else {
      digitalWrite(SORT_EN, LOW);
      delay(50);
      sortStartHoming();
    }
    return;
  }
  if (input == "homefeeder" || input == "homefeeder:soft") {
    if (!requireMotorPower()) return;
    host.println(F("ok"));
    // bare "homefeeder" = the operator's button: on-tab advances to a
    // true edge. ":soft" = automatic callers (run end): on-tab is a
    // no-op so a run's end never advances a pocket uncommanded.
    feedStartManualHome(input == "homefeeder");
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
  if (input.startsWith(F("sorttest:"))) {          // 1.x parity: random slots
    if (!requireMotorPower()) return;
    host.println(F("testing started"));
    int n = clampi(input.substring(9).toInt(), 1, 100);
    for (int i = 0; i < n && sortHomed; i++) {
      int slot = (int)(rp2040.hwrand32() % 8);
      host.print(i); host.print(F(" - Sorting to: ")); host.println(slot);
      sortGoTo(slot);
      while (sortState == S_MOVING) { sortService(); outPi.pump(); outUsb.pump(); }
    }
    if (sortHomed) {
      sortGoTo(0);
      while (sortState == S_MOVING) { sortService(); outPi.pump(); outUsb.pump(); }
    }
    host.println(F("Sort Test Completed"));
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
  SET_INT("sortflagoffset:", sortFlagOffset, 0, 3520, );
  SET_INT("sortflagwidth:", sortFlagWidth, 32, 1000, );
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
  attachInterrupt(digitalPinToInterrupt(SORT_HOME), sortHomeIsr, FALLING);
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
  if (feedSt) {
    feedSt->setDirectionPin(FEED_DIR, true);
    // short step queue: a mid-run speed change (the pre-edge creep) must
    // take effect fast — the default 20 ms of pre-planned steps is ~200
    // usteps of lag at cruise
    feedSt->setForwardPlanningTimeInMs(5);
    feedApplyMotion();
  }
  // sort polarity: count-up = toward the slots (machine-verified). The
  // HOMING SEEK runs the other way — down onto the flag from the slot
  // side — handled by the homing state machine, not the polarity.
  if (sortSt) { sortSt->setDirectionPin(SORT_DIR, true); sortApplyMotion(); }
  // FastAccelStepper's PIO claim stomps GPIO 0's pin mux (UART0 TX = the
  // Pi machine link) on this core — bisected on the bench: TX died at the
  // first stepperConnectToPin, RX untouched. Hand the pins back to UART.
  gpio_set_function(0, GPIO_FUNC_UART);
  gpio_set_function(1, GPIO_FUNC_UART);

  host.println(F("Ready"));
  if (!motorPower) {
    host.println(F("info:motor power off"));
  } else {
    sortStartHoming();
    feedStartManualHome(false);
  }
}

void loop() {
  if (recvFrom(Serial1, inPi) || recvFrom(Serial, inUsb)) {
    if (cmdReady) { cmdReady = false; handleCommand(); }
  }
  sortService();
  feedService();
  checkMotorPower();
  checkMotorStandby();
  outPi.pump();
  outUsb.pump();
}
