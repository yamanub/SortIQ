// SortIQ-PicoServo — firmware for the SortIQ servo carousel on a
// BigTreeTech SKR Pico (FastAccelStepper core).
//
// One stepper (the feed motor on the X driver) turns a 12-pocket wheel
// through a 25T pinion on the wheel's 72T ring gear. The eight gate doors
// are on a PCA9685 that the Raspberry Pi drives over its own I2C bus; this
// board never touches them. The serial protocol is the CS7.2 fork's, so
// the app drives it like any other board.
//   - Steps come from the RP2040 PIO: nothing the loop does (serial, TMC
//     UART reads, sensors) can disturb motion timing.
//   - Grid indexing: a home anchors one tab edge, every cycle is an exact
//     move to the next station, and the tab opto verifies each crossing.
//   - Feed jam detection: the grid checkpoint and the overtravel budget.
//   - Non-blocking state machines; both hosts (Pi UART + USB) have their
//     own receive buffers.

#include <Arduino.h>
#include <TMCStepper.h>
#include <FastAccelStepper.h>
#include <EEPROM.h>
#include <hardware/gpio.h>
#include "feeder.h"

#define FIRMWARE_VERSION "SortIQ-PicoServo-0.9.2 (7.2-SS2-PICO-SERVO)"

// ---------------- pins (SKR Pico) ----------------
#define FEED_STEP 11
#define FEED_DIR  10
#define FEED_EN   12
// DIAG jumper PULLED: the endstop input carries the homing opto.
#define FEED_HOME 4           // X-STOP slotted opto, LOW = blocked
#define PROX_PIN  22          // WD-DET conditioned prox, HIGH = brass
#define AUX_FAN1  17          // FAN1: controller case fan, full at boot
#define AUX_FAN2  20          // FAN3: camera fan, full at boot
#define CASEFAN   18          // FAN2: controller case fan, full at boot
                              // (fan: 0-100 exists; the app never sends it)
#define AIRDROP_PIN 23        // HE0, optional mod
// The LED ring's MOSFET is triggered by the Pi's GPIO 18, not this board;
// cameraledlevel: is accepted and echoed only.
#define R_SENSE   0.110f
#define FEED_ADDR 0b00
#define USTEP     16          // driver microstepping

// ---------------- settings (RAM; the app pushes on connect) ----------------
// The sort-arm settings (sort*, slot*, armDwell, airPost) do nothing on this
// build, but the app pushes the same setter list to every board and reads
// them back, so they are accepted, stored and echoed by getconfig.
int feedSpeedSet = 90, sortSpeedSet = 94;          // 1-100, fork semantics
int feedCurrent = 900, sortCurrent = 1400;         // mA
int feedSteps = 8;                                  // blind full-steps per cycle
                                                    // (lost-frame seek only)
int sortSteps = 20;
int feedAccF = 1500, feedDecF = 1500;              // us factors (fork units)
int sortAccF = 2400, sortDecF = 2400;
int feedHomingOffset = 5, sortHomingOffset = 0;    // full steps past the flag
int slotDropDelay = 550, armDwellMs = 0;
int notificationDelay = 120;
// debounce 100: a case settles well under 50 ms, 100 keeps 2x margin.
// launchSettle is the feed's standstill pause before each wheel move
// (F_LAUNCH); 0 because the !isRunning() standstill check is the guard.
// Both are settable from the console (debounceTime:, launchsettle:).
int debounceTime = 100;
int launchSettle = 0;
int motorStandby = 0;
int cameraLEDLevel = 200;
int caseFanLevel = 100;
bool airDrop = false; int airPre = 30, airSignal = 100, airPost = 100;
bool feedDecelOverOffset = true;
int feedLaunchSteps = 48;                          // accepted; FAS ramps supersede
int sortHomeBackoff = 160, sortHomeSlow = 1400;    // us creep for the slow edge
int sortFlagOffset = 0, sortFlagWidth = 240;
#define MAX_SLOTS 12
long slotPosTab[MAX_SLOTS];                        // usteps from slot 0
bool sgEnabled = true;                             // accepted and echoed only
int feedSgThrs = 40;
bool motorPower = false;

// ---------------- objects ----------------
TMC2209Stepper feedDrv(&Serial2, R_SENSE, FEED_ADDR);
FastAccelStepperEngine engine = FastAccelStepperEngine();
FastAccelStepper *feedSt = nullptr;

// ---------------- dual host ----------------
// Host TX is BUFFERED and pumped from loop(). A raw print to the Pi link
// (9600 baud) blocks for 100+ ms once the UART FIFO fills, and the PIO keeps
// stepping while the CPU is stuck, so a blocked loop() means missed edges.
// Nothing may write a host port directly.
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
uint32_t rxPiBytes = 0, rxUsbBytes = 0;

// feeder lines (state changes, alerts, status) go to the Pi link whoever
// owns the cycle, mirrored on USB for the bench
void feederEmit(const String &line) {
  for (unsigned i = 0; i < line.length(); i++) { outPi.put(line[i]); outUsb.put(line[i]); }
  outPi.put('\r'); outPi.put('\n'); outUsb.put('\r'); outUsb.put('\n');
}

static bool recvFrom(Stream &port, String &buf) {
  while (port.available() > 0) {
    int c = port.read();
    if (c < 0) return false;
    if (&port == (Stream *)&Serial1) rxPiBytes++; else rxUsbBytes++;
    if (c == '\r') continue;
    if (c != '\n') {
      // cap the line: a break or noise burst without a newline used to
      // grow the String until the heap ran out, then glue garbage onto
      // the next real command. Past the cap the line is discarded whole.
      if (buf.length() < 120) buf += (char)c;
      else buf = "\x01";                          // marker: overflowed
    } else {
      if (buf.length() && buf[0] == '\x01') { buf = ""; return false; }
      input = buf; buf = ""; host.active = &port; return true;
    }
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

// ---------------- driver ----------------
void applyDriverConfig() {
  feedDrv.begin();
  feedDrv.toff(3);
  feedDrv.blank_time(24);
  feedDrv.pwm_freq(2);
  feedDrv.microsteps(USTEP);
  feedDrv.pwm_autoscale(true);
  // SpreadCycle: StealthChop's torque deficit stalled the loaded wheel
  feedDrv.en_spreadCycle(true);
  feedDrv.intpol(true);
  feedDrv.TCOOLTHRS(0xFFFFF);
  // 50% hold: the disc's inertia needs real detent authority at rest
  feedDrv.rms_current(feedCurrent); feedDrv.ihold(16);
  feedDrv.SGTHRS(feedSgThrs);
}

bool driversPresent() {
  // the feed driver only: the Y driver socket is empty on this build
  return feedDrv.test_connection() == 0;
}

void fillSlotTab() {
  for (int i = 0; i < MAX_SLOTS; i++) slotPosTab[i] = (long)i * sortSteps * USTEP;
}

void feedStartCycle();     // fwd: the deliberate home reuses it

// ---------------- stats ----------------
uint32_t feedCycles = 0;
uint32_t powerFlaps = 0, restOffTab = 0;
long lastSeek = 0, maxSeek = 0;

// =====================================================================
// FEED axis — cycle state machine (blind travel -> flag seek -> offset)
// =====================================================================
enum FeedState { F_IDLE, F_WAIT_BRASS, F_DEBOUNCE, F_BLIND,
                 F_OFFSET, F_NOTIFY, F_HOME, F_LAUNCH, F_GRID };
FeedState feedState = F_IDLE;

// the shared TMC bus is the feeder's only while the wheel rests: a driver
// read mid-move holds the loop, and the tab edge is then resolved late at
// the wrong speed (a real edge read as "lost position")
bool wheelAtRest() {
  return feedState == F_IDLE && feedSt && !feedSt->isRunning();
}
bool forceFeed = false;
bool slotQueued = true;
int qSlot = 0;
uint32_t feedT0 = 0, waitMsgT = 0;
long feedSeekStart = 0;
Stream *cycleHost = &Serial1;     // who started the cycle: gets its done/errors
bool stopPending = false;         // stop received mid-motion: ack when landed
int feedCruiseCatch = 0;

// Flag edge by INTERRUPT, not loop polling: the PIO keeps stepping while the
// CPU services serial or TMC UART, so a polled edge is seen late (or a narrow
// tab is missed outright). The ISR
// timestamps the true edge; the service latency-corrects the position with
// elapsed-time * current-rate.
//
// ONLY a falling edge counts — never a level read. A level read on a wheel
// that stopped inside the tab yields a mid-tab pseudo-edge, and that error
// carries into the next cycle's start with nothing to reset it. A true
// leading edge each cycle is an absolute reference.
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
long feedCycleStart = 0;
long feedClearStart = 0;
bool feedDebug = false;
long feedArmAt = 0;
uint32_t feedEdgeLatUs = 0;
// Predictive creep (lost-frame seek): braking from cruise inside the small
// offset is physically impossible, so the seek learns the tab pitch, ramps
// down to a creep BEFORE the predicted edge, and takes the edge at creep
// speed: an exact, slip-free stop.
long feedPrevEdge = LONG_MIN;
// The pitch is geometric: 9216 usteps per wheel turn through the 72T:25T
// mesh (3200 x 72/25), 12 tabs on a 30° grid = 768. The estimate is seeded
// with that and the EMA only trims within print tolerance. Wheel usteps
// per turn is a setting (feedwheelrev:, kept in EEPROM); a different gear
// sets its measured ratio (tape-mark the wheel, feedjog: until the mark
// closes). Non-integer station pitches are fine: gridEdgeAt rounds per
// station and the anchor absorbs the residue.
long feedWheelRev = 9216;                          // 3200 x 72/25
#define FEED_PITCH_NOM ((feedWheelRev + 6) / 12)
long feedPitchEst = 9216 / 12 + 1;                 // EMA of tab pitch, usteps
                                                   // (re-seeded by the setter)

// GRID INDEXING — the carousel's motion model. A home anchors one tab
// edge; every cycle is an EXACT moveTo on the feedWheelRev/12 grid with
// symmetric ramps, and the tab sensor VERIFIES the crossing: <=48u off
// predicted = silent re-anchor, beyond = abort (position not trusted), two
// cycles with no tab at all = jam.
long feedAnchor = LONG_MIN;      // absolute ustep pos of the station-0 tab edge
long feedIdxN = 0;               // stations advanced since the anchor
long feedGridExpect = 0;         // this cycle's predicted tab edge
long feedGridTarget = 0;         // this cycle's rest position
bool feedGridWild = false;       // edge seen far off the grid
bool feedGridVerified = false;   // this cycle's crossing confirmed
int  feedGridMisses = 0;         // consecutive cycles with no tab seen
uint32_t feedGridChecks = 0, feedGridReanchors = 0;
long feedGridDrift = 0;
long feedGridErr = 0;      // last check's raw error (feeddebug: output)
// OFF-GRID RECOVERY. A tab edge seen inside a quarter station of where it
// was due (one clean sensor edge) is the same tab, just early or late:
// re-anchor on it and carry on, instead of dropping the frame. A dropped
// frame meant a lost-frame home - a creep seek that binds with brass
// loaded and ended the run more often than not (field 2026-10-21).
#define GRID_RECOVER_MAX 192      // usteps: a quarter of the 768 pitch
long feedReanchorSlack = 0;       // next cycle tolerates a rest this far off
uint32_t feedGridRecovers = 0;
bool feedGridQuiet = false;    // grid move as a HOME: end silent in
                               // F_IDLE (no 'done', no cycle counters)
bool feedGridStarted = false;  // moveTo is ASYNC: isRunning() stays false
                               // for a few ms after the command, so
                               // completion needs the move to have STARTED
                               // or the counter to sit at the target.
static long gridEdgeAt(long k) { return (k * feedWheelRev + 6) / 12; }

// re-anchor on this cycle's edge (err = seen - due). The move is still
// running toward the OLD target: a late edge (err > 0, the wheel is behind)
// extends it forward to the corrected rest, always safe; an early edge
// would mean pulling the target back, possibly into a reversal across the
// drop port, so the rest stays put and the NEXT cycle is allowed to start
// that far off grid (it lands exactly, on the corrected frame).
static void gridReanchor(long err) {
  feedAnchor += err;
  if (err > 0) {
    feedGridTarget += err;
    feedSt->moveTo(feedGridTarget);
  } else if (err < 0) {
    feedReanchorSlack = -err + 8;
  }
}
long feedCreepAt = 0;
bool feedCreeping = false;
#define FEED_CREEP_HZ 1800    // the disc's inertia: a faster creep stops hot
#define FEED_CREEP_RAMP 200000
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
  // the 64-ustep clear guard owns chatter; the 88-ustep floor only
  // debounces a start parked near a tab
  if (pos - feedClearStart >= 64 && pos > feedCycleStart + 88) {
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
// queue, FAS's forceStop wedges the queue (isRunning() sticks true until
// reboot). A max-decel stopMove() is the same near-dead stop, safely.
void feedHardStop() {
  feedSt->setAcceleration(4000000);
  feedSt->stopMove();
}

// Motor standby (AutoMotorStandbyTimeout, seconds; 0 = never): after the
// machine sits idle that long, the feed driver de-energizes so the motor
// runs cool. Any motion request wakes it.
uint32_t lastMotionMs = 0;
bool motorsInStandby = false;
void motorsWake() {
  lastMotionMs = millis();
  if (!motorsInStandby) return;
  motorsInStandby = false;
  digitalWrite(FEED_EN, LOW);
  delay(60);
  host.println(F("info:motors wake"));
}
void checkMotorStandby() {
  bool busy = feedState != F_IDLE || feedSt->isRunning();
  if (busy) { lastMotionMs = millis(); return; }
  if (motorsInStandby || motorStandby <= 0 || !motorPower) return;
  if (millis() - lastMotionMs > (uint32_t)motorStandby * 1000UL) {
    motorsInStandby = true;
    digitalWrite(FEED_EN, HIGH);
    host.println(F("info:motor standby"));
  }
}

// FAULT STOP: a feed abort gets ONE immediate retry; the same failure
// back-to-back means an unclearable condition (a real jam, a dead sensor),
// so stop the machine and say so instead of grinding through retries.
uint8_t faultCode = 0;                 // 0 = none, 1 = feed
uint8_t feedAbortStreak = 0;
String abortDetail;               // appended to the next abort's error line

void enterFault(uint8_t code, const __FlashStringHelper *msg) {
  if (faultCode) return;
  faultCode = code;
  feedHardStop();
  feedEdgeArm = false;
  feedState = F_IDLE; forceFeed = false;
  feedGridQuiet = false;                 // a silent home that died is over
  host.print(F("fault:")); host.println(msg);
  if (stopPending) { stopPending = false; host.println(F("done")); }
}

void feedAbort(const __FlashStringHelper *err) {
  feedHardStop();
  feedEdgeArm = false;
  feedPrevEdge = LONG_MIN;   // edge chain broken: don't learn a fake pitch
  feedState = F_IDLE; forceFeed = false;
  // the silent-home flag belongs to the cycle that just died. Left set,
  // it rode into the NEXT real cycle, which then ended without 'done' or
  // a gate: the app timed out, called it a jam and homed
  feedGridQuiet = false;
  host.print(err);
  if (abortDetail.length()) { host.print(abortDetail); abortDetail = ""; }
  host.println();
  if (stopPending) { stopPending = false; host.println(F("done")); }
  if (++feedAbortStreak >= 2)
    enterFault(1, F("feed jam - wheel cannot reach its sensor"));
}

// bare feed home: slow seek to the tab edge, stop AT it, no offset.
// A wheel already resting on the tab is home (the feed frame re-anchors
// every cycle; it does not need the absolute edge at rest).
// deliberate=true (the Home feeder button): an on-tab wheel is NOT trusted
// as aligned — a stop that slipped under brass load can rest misaligned yet
// still on the tab. With a live grid
// anchor (see below) a deliberate on-grid home is ONE silent grid index —
// a pocket passes the port exactly as a feed cycle would (a staged case
// drops, no gate, no 'done') — and a soft on-grid home is a no-op. With
// the frame lost, both seek forward to the next tab edge; that path
// advances a pocket whether deliberate or not, which is why the app only
// homes the feeder with the wheel known empty or on the operator's word.
void feedStartManualHome(bool deliberate) {
  if (feedState != F_IDLE) return;               // busy — a cycle owns the wheel
  motorsWake();
  cycleHost = host.active;
  // GRID-AWARE HOMES: with a live anchor and the wheel resting ON grid,
  // a SOFT home is a NO-OP (zero motion, zero staged-case risk) and a
  // DELIBERATE home is a SINGLE GRID INDEX — one pocket passes the port
  // at cruise, exactly like a feed cycle, and the tab edge re-verifies
  // and re-anchors in flight ("single feed to home"). The two-pitch
  // seek below survives only for a truly lost frame.
  if (feedAnchor != LONG_MIN) {
    long rel = (long)feedSt->getCurrentPosition()
               - (long)feedHomingOffset * USTEP - feedAnchor;
    long k = (rel + FEED_PITCH_NOM / 2) / FEED_PITCH_NOM;
    long err = rel - gridEdgeAt(k);
    if (err >= -8 && err <= 8) {
      if (!deliberate) return;                   // soft home, on-grid: no-op
      // deliberate home on a live frame = the CERTIFIED cycle path, run
      // silent: one grid index (one pocket past the port, edge re-verified
      // in flight, +/-8 landing) that ends in IDLE with no 'done'/counters
      feedGridQuiet = true;
      feedStartCycle();
      return;
    }
  }
  // LOST-FRAME HOME: with brass staged, a case starts falling into ANY
  // pocket that fully passes the port, at any speed. The only safe motion
  // is <= ONE PITCH ending at port alignment, like every index. So: seek
  // at CREEP and land DIRECTLY at the first edge + offset; nothing ever
  // passes through. A full seek is ~1 s worst case.
  bool onTab = digitalRead(FEED_HOME) == LOW;
  feedApplyMotion();
  feedSt->setSpeedInHz(FEED_CREEP_HZ);
  feedSt->setAcceleration(FEED_CREEP_RAMP);
  feedEdgeSeen = false; feedEdgeCalced = false;
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
  // bounded travel: worst phase (just past a tab) + a clearing margin —
  // the edge MUST appear inside it or the home aborts
  feedSt->moveTo(feedSeekStart + 2 * FEED_PITCH_NOM + 320);
  feedState = F_HOME;
}

// tab edge in hand (feedEdgeAbs): ONE deceleration to edge + offset,
// forward-only. Crisp mode crosses the offset at cruise and dead-stops;
// decel-over-offset shapes the whole offset down to the feeddecel end
// speed. Never reverse across the drop port — an overrun
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
  // a cruise-taken edge follows a slipped stop and reads short.
  // ESCAPE HATCH: a poisoned estimate (e.g. a hand-jammed cycle learned a
  // fake pitch at creep speed) misplaces the creep point so edges land at
  // cruise forever, and the gate then blocks all re-learning — a deadlock.
  // Two consecutive cruise-caught edges reset the estimate: the next
  // cycle bootstraps its creep from the blind length and re-learns clean.
  if (v > FEED_CREEP_HZ + 800) {
    if (++feedCruiseCatch >= 2) { feedPitchEst = FEED_PITCH_NOM; feedCruiseCatch = 0; }
  } else feedCruiseCatch = 0;
  if (feedPrevEdge != LONG_MIN && v <= FEED_CREEP_HZ + 800) {
    long dp = feedEdgeAbs - feedPrevEdge;
    if (dp > FEED_PITCH_NOM - 40 && dp < FEED_PITCH_NOM + 40)
      feedPitchEst = (feedPitchEst * 3 + dp) / 4;
  }
  feedPrevEdge = feedEdgeAbs;
  if (feedAnchor == LONG_MIN && v <= FEED_CREEP_HZ + 800) {
    // a cycle that had to seek (anchor lost, or a cycle started before
    // the boot home finished) just took a slip-free edge: that IS an
    // anchor. Without this every later cycle stayed in seek mode — no
    // grid target, no checkpoint — until someone homed by hand.
    feedAnchor = feedEdgeAbs; feedIdxN = 0; feedGridMisses = 0;
  }
  if (remain < 16) {
    if (v > FEED_CREEP_HZ + 800) {
      feedHardStop();          // cruise-caught anomaly: take no reversal risk
    } else {
      // land EXACTLY like the manual home does: moveTo lets the planner
      // hit the target, instead of a dead stop that drains the 5ms step
      // queue past it (~20 usteps at creep — the systematic 'a hair too
      // far' at offset 0). Any micro-correction is sub-full-step.
      long t = pos + 4;
      if (stopAt > t) t = stopAt;
      // 400k, not a 4M slam: a max-decel stop slides the disc forward.
      // From 1800 Hz creep this still lands within ~8u, no reversal.
      feedSt->setAcceleration(400000);
      feedSt->moveTo(t);
    }
  } else if (!feedDecelOverOffset) {
    feedSt->setAcceleration(400000);         // cruise the offset, firm stop
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

void feedStartCycle() {
  feedApplyMotion();
  if (feedAnchor != LONG_MIN) {                    // GRID MODE
    feedEdgeSeen = false; feedEdgeCalced = false;
    feedEdgeArm = false;
    feedFallCount = 0;
    feedCycleStart = (long)feedSt->getCurrentPosition();
    feedClearStart = feedCycleStart;
    // derive the station index from POSITION, not the counter: a yielded
    // cycle (startPipelinedFeed hard-stops whatever is in flight) leaves
    // the counter stale, and a counter-derived target the wheel already
    // sits at is a 3-ustep move that crosses nothing: a phantom "miss".
    {
      long rel = (long)feedSt->getCurrentPosition()
                 - (long)feedHomingOffset * USTEP - feedAnchor;
      feedIdxN = (rel + FEED_PITCH_NOM / 2) / FEED_PITCH_NOM;
      long off = rel - gridEdgeAt(feedIdxN);
      const long tol = 32 + feedReanchorSlack;     // a recovered frame rests
      feedReanchorSlack = 0;                       // up to this far off, once
      if (off < -tol || off > tol) {
        // resting between stations (a stop mid-move, a jog): a "nearest
        // station" target from here can be a pitch and a half of travel
        // with a pocket passing the port. The frame is not trusted —
        // fall through to the seek path, which re-anchors on the edge.
        feedAnchor = LONG_MIN;
        goto lost_frame;
      }
    }
    feedGridExpect = feedAnchor + gridEdgeAt(feedIdxN + 1);
    feedGridTarget = feedGridExpect + (long)feedHomingOffset * USTEP;
    feedGridWild = false; feedGridVerified = false;
    // large alignment offsets rest just shy of the NEXT tab — it can cross
    // within ~70u of launch, inside the arming floor. When the rest is
    // already clear of a tab there is nothing to debounce: arm now. (A
    // rest ON a tab, small offsets, still takes the clear-guard path.)
    if (digitalRead(FEED_HOME) == HIGH) { feedEdgeArm = true; feedArmAt = feedCycleStart; }
    if (feedDebug) {
      host.print(F("gdbg START n=")); host.print(feedIdxN + 1);
      host.print(F(" from=")); host.print(feedCycleStart);
      host.print(F(" target=")); host.print(feedGridTarget);
      host.print(F(" opto=")); host.println(digitalRead(FEED_HOME));
    }
    feedT0 = millis();
    feedState = F_LAUNCH;                          // same standstill guard
    return;
  }
lost_frame:
  feedEdgeSeen = false; feedEdgeCalced = false;
  feedEdgeArm = false;                             // armed once confirmed clear
  feedCycleStart = (long)feedSt->getCurrentPosition();
  feedClearStart = feedCycleStart;
  // ONE continuous run to the flag. "Blind" is only a distance during
  // which the edge is not yet trusted, not a separate move.
  feedFallCount = 0;
  feedCreeping = false;
  // the creep lead must fit the ACTUAL cruise speed: ramp-down distance
  // + the 5ms planning queue + jitter margin.
  uint32_t vc = speedToHz(feedSpeedSet);
  long lead = 100 + (long)(vc / 200) +
      (long)(((uint64_t)vc * vc - (uint64_t)FEED_CREEP_HZ * FEED_CREEP_HZ) /
             (2ULL * FEED_CREEP_RAMP));
  long blindEnd = feedCycleStart + (long)feedSteps * USTEP;
  long bootstrap = blindEnd - lead;              // unlearned: creep the seek
  if (feedPitchEst > 0 && feedPrevEdge != LONG_MIN)
    feedCreepAt = feedPrevEdge + feedPitchEst - lead;
  else
    feedCreepAt = bootstrap;
  if (feedCreepAt < feedCycleStart + 64 || feedCreepAt > blindEnd + 6400)
    feedCreepAt = bootstrap;                       // stale frame: be safe
  if (feedCreepAt < feedCycleStart + 64) feedCreepAt = feedCycleStart + 64;
  feedT0 = millis();
  feedState = F_LAUNCH;
}

void feedService() {
  switch (feedState) {
    case F_IDLE: return;
    case F_WAIT_BRASS:
      if (waitMsgT == 0) waitMsgT = millis();
      if (forceFeed) { feedStartCycle(); return; }
      if (digitalRead(PROX_PIN) == HIGH) {
        feedT0 = millis();
        feedState = F_DEBOUNCE;
        return;
      }
      // NO auto-realign: every pocket carries state, so an uncommanded
      // advance would desync every pocket from its verdict. The wheel moves
      // ONLY when a cycle is commanded; the ISR edge anchor is the position
      // truth, not the opto's rest reading (RestOffTab is telemetry only).
      if (millis() - waitMsgT > 1000) {
        host.println(F("waiting for brass"));
        waitMsgT = millis();
      }
      return;
    case F_DEBOUNCE:
      if (digitalRead(PROX_PIN) != HIGH) { feedState = F_WAIT_BRASS; waitMsgT = 0; return; }
      if (millis() - feedT0 >= (uint32_t)debounceTime) feedStartCycle();
      return;
    case F_LAUNCH:                                 // standstill + settle before
      if (!feedSt->isRunning() && millis() - feedT0 >= (uint32_t)launchSettle) {
        if (feedAnchor != LONG_MIN) {              // grid: one exact indexed
          feedSt->setAcceleration(200000);         // move, brisk symmetric
          feedSt->moveTo(feedGridTarget);          // ramps both ends
          feedGridStarted = false;
          feedT0 = millis();
          feedState = F_GRID;
        } else {
          feedSt->runForward();
          feedT0 = millis();
          feedState = F_BLIND;
        }
      } else if (millis() - feedT0 > 4000) {
        feedAbort(F("error:feed overtravel detected"));
      }
      return;
    case F_GRID: {
      if (feedSt->isRunning()) feedGridStarted = true;
      feedEdgeMaybeArm();
      feedEdgeResolve();                           // pure edge math, no stop
      if (feedEdgeCalced && !feedGridVerified && !feedGridWild) {
        long err = feedEdgeAbs - feedGridExpect;
        feedGridChecks++;
        feedGridErr = err;
        if (err >= -48 && err <= 48) {
          if (err && feedAnchor != LONG_MIN) { gridReanchor(err); feedGridReanchors++; feedGridDrift = err; }
          feedGridVerified = true;
          feedEdgeArm = false;                     // one crossing per cycle
        } else if (err >= -GRID_RECOVER_MAX && err <= GRID_RECOVER_MAX
                   && feedFallCount == 1 && feedAnchor != LONG_MIN) {
          // off grid, but one clean edge inside a quarter station: the same
          // tab. Recover in flight and say so (the app logs it).
          gridReanchor(err);
          feedGridRecovers++; feedGridDrift = err;
          feedGridVerified = true;
          feedEdgeArm = false;
          host.print(F("info:feed re-anchored off ")); host.print(err);
          host.print(F(" usteps (read ")); host.print(feedEdgeLatUs);
          host.println(F(" us late)"));
        } else {
          feedGridWild = true;
        }
      }
      if (millis() - feedT0 > 4000) {              // move never started or
        feedAnchor = LONG_MIN;                     // never finished: the
        feedAbort(F("error:feed overtravel detected"));  // one true guard
        return;
      }
      if (!feedGridStarted &&
          (long)feedSt->getCurrentPosition() < feedGridTarget - 4) {
        return;                                    // command still queued
      }
      if (!feedSt->isRunning()) {
        if (feedGridWild) {
          if (feedDebug) {
            host.print(F("gdbg WILD n=")); host.print(feedIdxN + 1);
            host.print(F(" err=")); host.print(feedGridErr);
            host.print(F(" edge=")); host.print(feedEdgeAbs);
            host.print(F(" expect=")); host.print(feedGridExpect);
            host.print(F(" nfall=")); host.println(feedFallCount);
          }
          feedAnchor = LONG_MIN;                   // frame no longer trusted:
          // the numbers that say WHY: how far the edge read from where it
          // was due, how many sensor edges the move saw, and how late the
          // loop got to the edge (a busy loop misreads a real edge)
          abortDetail = String(F(" (off ")) + feedGridErr + F(" usteps, ")
                        + feedFallCount + F(" sensor edge(s), read ")
                        + feedEdgeLatUs + F(" us late)");
          feedAbort(F("error:feed lost position at tab checkpoint"));
          return;                                  // next home re-anchors
        }
        if (feedGridVerified) feedGridMisses = 0;
        else if (++feedGridMisses >= 2) {
          feedAnchor = LONG_MIN;
          feedAbort(F("error:feed tab missing at checkpoint"));
          return;
        }
        if (feedDebug) {
          host.print(F("gdbg n=")); host.print(feedIdxN + 1);
          host.print(F(" expect=")); host.print(feedGridExpect);
          host.print(F(" edge=")); host.print(feedGridVerified ? feedEdgeAbs : -1);
          host.print(F(" drift=")); host.print(feedGridVerified ? feedEdgeAbs - feedGridExpect : 0);
          host.print(F(" nfall=")); host.print(feedFallCount);
          host.print(F(" arm=")); host.print(feedEdgeArm ? 1 : 0);
          host.print(F(" anchor=")); host.println(feedAnchor);
        }
        feedIdxN++;
        if (feedGridQuiet) {                     // this was a home, not a
          feedGridQuiet = false;                 // cycle: silent, no 'done'
          feedState = F_IDLE;
          if (stopPending) { stopPending = false; host.println(F("done")); }
          return;
        }
        feedT0 = millis();
        feedState = F_NOTIFY;
      }
      return;
    }
    case F_BLIND: {
      // feed jam detection is the overtravel budget below
      feedEdgeMaybeArm();
      if (!feedCreeping &&
          (long)feedSt->getCurrentPosition() >= feedCreepAt) {
        feedSt->setAcceleration(FEED_CREEP_RAMP);
        feedSt->setSpeedInHz(FEED_CREEP_HZ);
        feedSt->applySpeedAcceleration();          // ramp down while running
        feedCreeping = true;
      }
      feedEdgeResolve();
      if (feedEdgeCalced) { feedFinishHome(); return; }
      // overtravel: two pitches past the blind with no tab = jammed wheel
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
        feedAbortStreak = 0;                       // a clean cycle = healthy
        if (digitalRead(FEED_HOME) == HIGH) restOffTab++;  // stop slipped off tab
        feedState = F_IDLE; forceFeed = false;
        host.println(F("done"));
        if (stopPending) { stopPending = false; host.println(F("done")); }
      }
      return;
    case F_HOME: {                               // lost-frame home: creep seek,
      feedEdgeMaybeArm();                        // land at FIRST edge + offset
      feedEdgeResolve();
      if (feedEdgeCalced) {
        feedEdgeArm = false;
        long pos = (long)feedSt->getCurrentPosition();
        // park at the OPERATING rest (edge + offset), exactly where a
        // cycle parks, and at THIS edge, never a pitch further: this path
        // runs at every power-up, when the tube is not known clear. The
        // seek arrives at creep, so this is the cycle's own landing:
        // forward-only moveTo at 400k, never reversing across the port.
        long stopAt = feedEdgeAbs + (long)feedHomingOffset * USTEP;
        long t = pos + 4;
        if (stopAt > t) t = stopAt;
        feedSt->setAcceleration(400000);
        feedSt->moveTo(t);
        feedPrevEdge = feedEdgeAbs;              // seed the pitch predictor
        feedAnchor = feedEdgeAbs;                // grid mode anchors HERE
        feedIdxN = 0;                            // rest = the anchor's station
        feedGridMisses = 0;
        feedState = F_IDLE;
        return;
      }
      // the seek's travel is a BOUNDED moveTo: if it completes with
      // no edge captured, the sensor never fired inside two pitches —
      // abort (500ms floor rides out the async command start)
      if (millis() - feedT0 > 500 && !feedSt->isRunning()) {
        feedAbort(F("error:feed overtravel detected"));
        return;
      }
      if (millis() - feedT0 > 8000) {
        feedAbort(F("error:feed overtravel detected"));
      }
      return;
    }
  }
}

void startPipelinedFeed(int slot, bool force) {    // pf / xf:N
  motorsWake();
  cycleHost = host.active;                         // this host owns the cycle
  stopPending = false;                             // a new cycle supersedes
  if (feedState != F_IDLE) {
    // recovery paths overlap (the app's HOME is chased by its re-prime xf
    // within ~1 s): yield whatever is in flight — every cycle re-anchors
    // on its own edge.
    feedHardStop();
    feedEdgeArm = false;
    feedPrevEdge = LONG_MIN; // edge chain broken: don't learn a fake pitch
    feedState = F_IDLE;
    feedGridQuiet = false;   // a yielded silent home must not silence THIS cycle
  }
  forceFeed = force;
  qSlot = slot;                  // routing is the Pi's (gate doors); the slot
                                 // is still parsed and queued for the protocol
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
  // poll only at rest: a glitched TMC read mid-move must not read as
  // power lost
  if (feedState != F_IDLE) return;
  if (millis() - lastPowerPoll < 1000) return;
  lastPowerPoll = millis();
  bool now = driversPresent();
  if (now == motorPower) return;
  motorPower = now;
  if (now) {
    applyDriverConfig();
    host.println(F("info:motor power on"));
    feedStartManualHome(false);                    // position unknown: re-home
  } else {
    powerFlaps++;
    host.println(F("info:motor power off"));
    // a de-energized wheel turns by hand (clearing a jam): its frame is
    // gone too. Without this, power-on's soft home trusted the stale
    // anchor and no-op'd on a hand-moved wheel.
    feedAnchor = LONG_MIN;
  }
}

// feedWheelRev must survive a reflash: if the app's reconnect settings
// push is late or skipped, a wrong ratio makes every checkpoint abort.
// Persisted in EEPROM 4..8 with a magic byte.
#define FWR_MAGIC_ADDR 4
#define FWR_DATA_ADDR  5
#define FWR_MAGIC      0x5A
void persistFeedWheelRev() {
  EEPROM.write(FWR_MAGIC_ADDR, FWR_MAGIC);
  EEPROM.put(FWR_DATA_ADDR, feedWheelRev);
  EEPROM.commit();
}

// feed_homing_offset must ALSO survive to the boot home: setup() homes
// before the app connects and pushes the tuned offset, so without this the
// power-up home lands at the compile default. Persisted like the
// wheel-rev; the setter runs on every reconnect, so the flash write is
// guarded to actual changes only.
#define FHO_MAGIC_ADDR 9
#define FHO_DATA_ADDR  10
#define FHO_MAGIC      0x5B
void persistFeedHomingOffset() {
  int stored = -1;
  if (EEPROM.read(FHO_MAGIC_ADDR) == FHO_MAGIC) EEPROM.get(FHO_DATA_ADDR, stored);
  if (stored == feedHomingOffset) return;        // unchanged: no flash write
  EEPROM.write(FHO_MAGIC_ADDR, FHO_MAGIC);
  EEPROM.put(FHO_DATA_ADDR, feedHomingOffset);
  EEPROM.commit();
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
  host.print(F(",\"CaseFanLevel\":")); host.print(caseFanLevel);
  host.print(F(",\"CameraLEDLevel\":")); host.print(cameraLEDLevel);
  host.print(F(",\"DebounceTimeout\":")); host.print(debounceTime);
  host.print(F(",\"LaunchSettle\":")); host.print(launchSettle);
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
  host.print(F(",\"MotorPower\":")); host.print(motorPower ? 1 : 0);
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
  if (feederCommand(input)) return;              // optional case feeder

  if (input == "ping") { host.println(F("ok")); return; }
  if (input == "version") { host.println(F(FIRMWARE_VERSION)); return; }
  if (input == "getconfig") { sendConfig(); return; }
  if (input == "stop") {
    // a stop during MOTION lets the cycle land at its aligned rest (7.2
    // semantics; the app's cancel race expects the cycle's done first,
    // then the stop's). Halting mid-pitch put the disc off-grid with a
    // case half over the port.
    if (feedState == F_LAUNCH || feedState == F_GRID || feedState == F_BLIND ||
        feedState == F_OFFSET || feedState == F_NOTIFY) {
      stopPending = true;
      return;
    }
    feedHardStop();
    feedEdgeArm = false;
    feedPrevEdge = LONG_MIN; // edge chain broken: don't learn a fake pitch
    feedState = F_IDLE; forceFeed = false;
    feedGridQuiet = false;   // a silent home cut short must not silence the next cycle
    host.println(F("done"));
    return;
  }
  if (input == "status") {
    host.print(F("FEED microsteps: ")); host.println(feedDrv.microsteps());
    host.print(F("FEED current: ")); host.println(feedDrv.rms_current());
    host.print(F("FEED Stealth: ")); host.println(feedDrv.stealth());
    host.print(F("Motor power: ")); host.println(motorPower ? F("on") : F("off"));
    return;
  }
  if (input == "diag") {
    host.print(F("feedSt=")); host.print(feedSt ? F("ok") : F("NULL"));
    host.print(F(" feedState=")); host.print((int)feedState);
    host.print(F(" feedEdge=")); host.print(feedEdgeAbs);
    host.print(F(" fpos=")); host.print((long)feedSt->getCurrentPosition());
    host.print(F(" flaps=")); host.print(powerFlaps);
    host.print(F(" fault=")); host.print(faultCode);
    host.print(F(" ganchor=")); host.print(feedAnchor == LONG_MIN ? -1 : feedAnchor);
    host.print(F(" gidx=")); host.print(feedIdxN);
    host.print(F(" gchk=")); host.print(feedGridChecks);
    host.print(F(" gre=")); host.print(feedGridReanchors);
    host.print(F(" gdrift=")); host.print(feedGridDrift);
    host.print(F(" txDropPi=")); host.print(outPi.dropped);
    host.print(F(" txDropUsb=")); host.print(outUsb.dropped);
    host.print(F(" rxPi=")); host.print(rxPiBytes);
    host.print(F(" rxUsb=")); host.print(rxUsbBytes);
    host.println();
    return;
  }
  if (input == "sensors") {
    host.print(F("{\"feedHoming\":")); host.print(digitalRead(FEED_HOME));
    host.print(F(",\"prox\":")); host.print(digitalRead(PROX_PIN));
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
  if (input == "faultclear") {
    faultCode = 0;
    feedAbortStreak = 0;
    host.println(F("ok"));
    return;
  }
  // a faulted machine starts no cycle, whichever command asks for one
  if (faultCode &&
      (input.startsWith(F("pf")) || input.startsWith(F("xf:")) ||
       isDigit(input[0]))) {
    host.print(F("fault:"));
    host.println(F("feed jam - wheel cannot reach its sensor"));
    return;
  }
  if (input.startsWith(F("armfree:"))) {          // no arm on this build
    host.println(F("ok"));
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
  if (input == "homesorter") {                     // no arm on this build
    if (!requireMotorPower()) return;
    host.println(F("ok"));
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
  if (input.startsWith(F("sortto:"))) {            // no arm on this build; the
    if (!requireMotorPower()) return;              // app's flush pair still
    host.println(F("ok"));                         // sends it
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
  SET_INT("sortspeed:", sortSpeedSet, 1, 100, );
  SET_INT("feedsteps:", feedSteps, 2, 200, );
  SET_INT("sortsteps:", sortSteps, 5, 100, fillSlotTab());
  SET_INT("feedmotorcurrent:", feedCurrent, 300, 1600, feedDrv.rms_current(feedCurrent));
  SET_INT("sortmotorcurrent:", sortCurrent, 300, 1600, );
  SET_INT("feedaccel:", feedAccF, 100, 5000, );
  SET_INT("feeddec:", feedDecF, 100, 5000, );
  SET_INT("sortaccel:", sortAccF, 100, 5000, );
  SET_INT("sortdecel:", sortDecF, 100, 5000, );
  if (input.startsWith(F("feedhomingoffset:"))) {
    // never more than half a pitch: past that every lost-frame landing
    // walks a pocket through the port
    long v = input.substring(17).toInt();
    long cap = (FEED_PITCH_NOM / 2) / USTEP;
    if (v < 0) v = 0; if (v > 30) v = 30; if (v > cap) v = cap;
    feedHomingOffset = (int)v; persistFeedHomingOffset();
    host.println(F("ok")); return;
  }
  SET_INT("sorthomingoffset:", sortHomingOffset, 0, 100, );
  if (input.startsWith(F("feedwheelrev:"))) {      // wheel usteps/rev (gear!)
    if (feedState != F_IDLE) { host.println(F("error:feed busy")); return; }
    long v = input.substring(13).toInt();
    if (v < 2000) v = 2000; if (v > 20000) v = 20000;
    if (v != feedWheelRev) {                       // ONLY a real change kills
      feedWheelRev = v;                            // the anchor — the app
      feedPitchEst = FEED_PITCH_NOM;               // re-pushes settings on
      feedAnchor = LONG_MIN;                       // every reconnect, and an
      persistFeedWheelRev();                       // unchanged value must not
    }                                              // de-home a running frame
    host.println(F("ok"));
    return;
  }
  SET_INT("sortflagoffset:", sortFlagOffset, 0, 3520, );
  SET_INT("sortflagwidth:", sortFlagWidth, 32, 1000, );
  SET_INT("slotdropdelay:", slotDropDelay, 0, 3000, );
  SET_INT("armdwell:", armDwellMs, 0, 1000, );
  SET_INT("notificationdelay:", notificationDelay, 0, 1000, );
  SET_INT("debounceTime:", debounceTime, 0, 2000, );
  SET_INT("launchsettle:", launchSettle, 0, 1000, );
  SET_INT("automotorstandbytimeout:", motorStandby, 0, 3600, );
  SET_INT("sorthomebackoff:", sortHomeBackoff, 0, 400, );
  SET_INT("sorthomeslow:", sortHomeSlow, 100, 5000, );
  SET_INT("feedlaunch:", feedLaunchSteps, 0, 200, );
  SET_INT("airdroppredelay:", airPre, 0, 500, );
  SET_INT("airdropdsignalduration:", airSignal, 0, 500, );
  SET_INT("airdroppostdelay:", airPost, 0, 3000, );
  SET_INT("cameraledlevel:", cameraLEDLevel, 0, 255, );
  SET_INT("sgfeed:", feedSgThrs, 0, 255, feedDrv.SGTHRS(feedSgThrs));
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
  if (input.startsWith(F("ledcolor:"))) {          // accepted for the app;
    host.println(F("ok"));                         // the ring is the Pi's
    return;
  }
  if (input.startsWith(F("feedjog:"))) {         // bench: raw open-loop jog,
    long n = input.substring(8).toInt();         // NO sensor involvement — for
    if (n < -32000) n = -32000;                  // marked-wheel calibration of
    if (n > 32000) n = 32000;                    // wheel steps per turn
    if (feedState != F_IDLE || !feedSt) { host.println(F("error:feed busy")); return; }
    motorsWake();
    feedAnchor = LONG_MIN;                       // an open-loop jog leaves the grid
    feedSt->setSpeedInHz(1200);
    feedSt->setAcceleration(20000);              // glassy ramp both ends
    feedSt->moveTo((long)feedSt->getCurrentPosition() + n);
    host.println(F("ok"));
    return;
  }
  if (input.length() && isDigit(input[0])) {
    // 7.2's bare "<n>": feed for the queued slot, queue n (the app's
    // non-pipelined path)
    if (!requireMotorPower()) return;
    int s = clampi(input.toInt(), 0, MAX_SLOTS - 1);
    startPipelinedFeed(qSlot, false);
    qSlot = s; slotQueued = true;
    return;
  }
  host.println(F("?"));
}

// =====================================================================
void setup() {
  Serial1.setFIFOSize(256);
  Serial1.begin(9600);                  // Pi header UART — the machine link
  Serial.begin(9600);                   // USB mirror
  Serial2.begin(460800);                // TMC bus (2.1 baud bump: a confirm
                                        // read blocks the loop ~2.5 ms, not
                                        // ~10 ms; TMC2209 auto-bauds, <500k)
  EEPROM.begin(64);
  if (EEPROM.read(FWR_MAGIC_ADDR) == FWR_MAGIC) {   // saved geometry survives
    long v; EEPROM.get(FWR_DATA_ADDR, v);           // a reflash
    if (v >= 2000 && v <= 20000) feedWheelRev = v;
  }
  if (EEPROM.read(FHO_MAGIC_ADDR) == FHO_MAGIC) {   // tuned offset -> boot home
    int v; EEPROM.get(FHO_DATA_ADDR, v);            // lands centered before the
    if (v >= 0 && v <= 30) feedHomingOffset = v;    // app pushes settings
  }

  pinMode(FEED_HOME, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(FEED_HOME), feedHomeIsr, FALLING);
  pinMode(PROX_PIN, INPUT_PULLUP);
  pinMode(FEED_EN, OUTPUT); digitalWrite(FEED_EN, LOW);
  pinMode(AUX_FAN1, OUTPUT); digitalWrite(AUX_FAN1, HIGH);
  pinMode(AUX_FAN2, OUTPUT); digitalWrite(AUX_FAN2, HIGH);
  pinMode(CASEFAN, OUTPUT); analogWrite(CASEFAN, caseFanLevel * 255 / 100);
  pinMode(AIRDROP_PIN, OUTPUT); digitalWrite(AIRDROP_PIN, LOW);

  fillSlotTab();
  feederSetup();                        // feeder outputs safe, fitted or not
  delay(400);

  motorPower = driversPresent();
  if (motorPower) applyDriverConfig();

  engine.init();
  feedSt = engine.stepperConnectToPin(FEED_STEP);
  if (feedSt) {
    feedSt->setDirectionPin(FEED_DIR, true);   // geared mount (72T:25T mesh)
    // short step queue: a mid-run speed change (the pre-edge creep) must
    // take effect fast — the default 20 ms of pre-planned steps is ~200
    // usteps of lag at cruise
    feedSt->setForwardPlanningTimeInMs(5);
    feedApplyMotion();
  }
  // FastAccelStepper's PIO claim overwrites GPIO 0's pin mux (UART0 TX =
  // the Pi machine link): hand the pins back to UART.
  gpio_set_function(0, GPIO_FUNC_UART);
  gpio_set_function(1, GPIO_FUNC_UART);

  // TX was dead through the PIO claim until just above: 100 ms of idle-high
  // lets the Pi's UART resync, so "Ready" arrives intact and the app sees
  // the reboot.
  delay(100);
  host.println(F("Ready"));
  if (!motorPower) {
    host.println(F("info:motor power off"));
  } else {
    feedStartManualHome(false);
  }
}

void loop() {
  if (recvFrom(Serial1, inPi) || recvFrom(Serial, inUsb)) handleCommand();
  // cycle-lifetime lines (done, waiting for brass, error:, fault:) go to
  // the host that STARTED the cycle, not whichever port sent the latest
  // command: a "diag" typed on USB mid-cycle used to steal the Pi's done
  host.active = cycleHost;
  feedService();
  feederLoop();
  checkMotorPower();
  checkMotorStandby();
  outPi.pump();
  outUsb.pump();
}
