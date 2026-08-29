// SortIQ — FastAccelStepper spike (bench harness, NOT machine firmware)
//
// Proves the phase-1.5 rewrite architecture on the SKR Pico sort arm:
//   1. FastAccelStepper generates steps on the PIO (CPU-free motion)
//   2. real linear acceleration ramps (the AVR446 integer mess is gone)
//   3. StallGuard sampled over the TMC UART *while the arm moves* — the
//      read that used to freeze the step train now costs the motion nothing
//
// USB console at 115200. Commands:
//   home            two-stage home onto the Z-STOP flag (position := 0)
//   s:N             move to slot N (N*320 usteps)
//   auto            continuous 0<->7 sweeps until 'stop' (SG stats per move)
//   speed:HZ        cruise in usteps/s      (default 8500 ~= old speed 94)
//   acc:V           acceleration usteps/s^2 (default 80000)
//   cur:MA          sort motor rms current  (default 1400)
//   sg              one-shot SG_RESULT read
//   stop            stop motion / end auto
//   stat            position, speed, driver readback

#include <Arduino.h>
#include <TMCStepper.h>
#include <FastAccelStepper.h>

// SKR Pico sort axis (Y): step 6, dir 5, enable 7, TMC addr 2 on Serial2
#define STEP_PIN   6
#define DIR_PIN    5
#define EN_PIN     7
#define HOME_PIN   25          // Z-STOP opto: LOW = flag in the slot
#define R_SENSE    0.110f
#define DRV_ADDR   0b10
#define USTEPS_PER_SLOT 320

TMC2209Stepper drv(&Serial2, R_SENSE, DRV_ADDR);
FastAccelStepperEngine engine = FastAccelStepperEngine();
FastAccelStepper *st = nullptr;

uint32_t cruiseHz = 8500;      // usteps/s  (117us/ustep equivalent)
uint32_t accel    = 80000;     // usteps/s^2
uint16_t currentMA = 1400;

// per-move StallGuard stats, sampled continuously while moving
uint32_t sgN = 0, sgSum = 0; uint16_t sgMin = 1023;
// rolling-window jam detector: sustained low load aborts the move. The
// window makes transients harmless (free moves dip to 20 for a sample or
// two); a real jam collapses the whole window.
uint16_t win[8]; uint8_t winI = 0, winFill = 0;
uint32_t jamLow = 150;         // a sample below this is a "deep low"
uint8_t  jamCount = 4;         // this many deep lows in the last 8 = jam
uint32_t jams = 0;
// flag audit: the home flag lives at position ~0. Any time the sensor reads
// the flag while the position counter says the arm is far from it, steps
// were lost (a skip/jam) — deterministic, no load physics involved.
long flagTol = 200;            // usteps of forgiveness around the flag zone
uint32_t skips = 0;
long flagPos = 0;              // absolute position of the flag edge (homing
                               // measures it; slot N lives at flagPos+N*320 —
                               // the library position counter is never written)
bool moving = false;
bool autoRun = false; int autoTarget = 7;

void sgReset() { sgN = 0; sgSum = 0; sgMin = 1023; winI = 0; winFill = 0; }

void sgReport(const char *tag) {
  if (sgN == 0) { Serial.printf("%s: no SG samples\n", tag); return; }
  Serial.printf("%s: SG n=%lu min=%u avg=%lu  (each sample = a blocking UART "
                "read DURING motion)\n", tag, sgN, sgMin, sgSum / sgN);
}

void applyDriver() {
  drv.begin();
  drv.toff(3);
  drv.blank_time(24);
  drv.pwm_freq(2);
  drv.rms_current(currentMA);
  drv.microsteps(16);
  drv.pwm_autoscale(true);
  drv.en_spreadCycle(false);   // StealthChop: StallGuard needs it
  drv.intpol(true);
  drv.ihold(16);
  drv.SGTHRS(0);               // DIAG unused in the spike — we sample SG_RESULT
  drv.TCOOLTHRS(0xFFFFF);
}

void home() {
  Serial.println("homing: fast seek...");
  st->setSpeedInHz(4000); st->setAcceleration(accel);
  st->runForward();
  uint32_t t0 = millis();
  while (digitalRead(HOME_PIN) != LOW) {          // seek the flag
    if (millis() - t0 > 8000) { st->forceStop(); Serial.println("home FAILED (seek timeout)"); return; }
  }
  st->forceStop();
  while (st->isRunning()) {}
  delay(150);
  Serial.println("homing: back off...");
  st->setSpeedInHz(1500);
  st->runBackward();
  t0 = millis();
  while (digitalRead(HOME_PIN) == LOW) {          // leave the flag
    if (millis() - t0 > 4000) { st->forceStop(); Serial.println("home FAILED (backoff timeout)"); return; }
  }
  st->move(-160);                                  // margin past release
  while (st->isRunning()) {}
  delay(150);
  Serial.println("homing: slow re-approach...");
  st->setSpeedInHz(800);
  st->runForward();
  t0 = millis();
  while (digitalRead(HOME_PIN) != LOW) {          // repeatable edge
    if (millis() - t0 > 5000) { st->forceStop(); Serial.println("home FAILED (approach timeout)"); return; }
  }
  st->forceStop();
  while (st->isRunning()) {}
  delay(100);
  flagPos = (long)st->getCurrentPosition();   // measure, never write
  st->setSpeedInHz(cruiseHz);
  Serial.printf("homed. flag at absolute %ld (slot N = flag + N*320)\n", flagPos);
}

void startMove(long target) {
  sgReset();
  st->setSpeedInHz(cruiseHz);
  st->setAcceleration(accel);
  st->moveTo(target);
  moving = true;
}

void setup() {
  Serial.begin(115200);
  Serial2.begin(115200);
  pinMode(HOME_PIN, INPUT_PULLUP);
  pinMode(EN_PIN, OUTPUT); digitalWrite(EN_PIN, LOW);
  delay(2500);
  applyDriver();
  engine.init();
  st = engine.stepperConnectToPin(STEP_PIN);
  if (!st) { Serial.println("FATAL: no PIO stepper — wrong platform?"); while (1) delay(1000); }
  st->setDirectionPin(DIR_PIN, false);   // production 'toward slots' = DIR low
  st->setSpeedInHz(cruiseHz);
  st->setAcceleration(accel);
  Serial.println("FAS spike ready. Commands: home  s:N  auto  stop  speed:HZ  acc:V  cur:MA  sg  stat");
  Serial.printf("driver: %u mA readback, version conn=%d\n", drv.rms_current(), drv.test_connection());
}

String line;

void handle(String cmd) {
  cmd.trim();
  if (cmd == "home") { home(); return; }
  if (cmd.startsWith("s:")) { startMove(flagPos + cmd.substring(2).toInt() * (long)USTEPS_PER_SLOT); return; }
  if (cmd == "auto") { autoRun = true; startMove(flagPos + autoTarget * (long)USTEPS_PER_SLOT); Serial.println("auto sweeps on"); return; }
  if (cmd == "stop") { autoRun = false; st->stopMove(); Serial.println("stopping"); return; }
  if (cmd.startsWith("speed:")) { cruiseHz = cmd.substring(6).toInt(); st->setSpeedInHz(cruiseHz); Serial.printf("cruise %lu usteps/s\n", cruiseHz); return; }
  if (cmd.startsWith("acc:")) { accel = cmd.substring(4).toInt(); st->setAcceleration(accel); Serial.printf("accel %lu\n", accel); return; }
  if (cmd.startsWith("cur:")) { currentMA = cmd.substring(4).toInt(); drv.rms_current(currentMA); Serial.printf("current %u mA\n", currentMA); return; }
  if (cmd.startsWith("jam:")) { jamLow = cmd.substring(4).toInt(); Serial.printf("deep-low threshold %lu\n", jamLow); return; }
  if (cmd == "sg") { Serial.printf("SG_RESULT=%u\n", drv.SG_RESULT()); return; }
  if (cmd == "stat") {
    Serial.printf("pos=%ld running=%d speed=%lu acc=%lu cur=%u home=%s\n",
                  (long)st->getCurrentPosition(), st->isRunning(), cruiseHz, accel,
                  drv.rms_current(), digitalRead(HOME_PIN) == LOW ? "ON-FLAG" : "clear");
    return;
  }
  if (cmd.length()) Serial.println("?");
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') { handle(line); line = ""; }
    else if (c != '\r') line += c;
  }
  if (st->isRunning()) {
    long rel = (long)st->getCurrentPosition() - flagPos;
    bool onFlag = digitalRead(HOME_PIN) == LOW;
    if (onFlag && (rel > flagTol) && (rel < (7L * USTEPS_PER_SLOT - flagTol))) {
      st->forceStop();
      skips++; autoRun = false; moving = false;
      Serial.printf("*** SKIP DETECTED — flag seen %ld usteps from where it belongs — steps were lost ***\n", rel);
      sgReport("skip move");
      return;
    }
    // THE experiment: a blocking multi-ms UART read on every pass while the
    // PIO keeps stepping underneath. On the old core this froze the motor.
    uint16_t r = drv.SG_RESULT();
    if (r == 0) r = drv.SG_RESULT();          // CRC-miss guard
    if (r > 0) {
      sgN++; sgSum += r; if (r < sgMin) sgMin = r;
      // jam window fills ONLY at cruise: accel and decel legitimately read
      // low (the first trip fired on a landing). FAS reports the ramp phase.
      bool coasting = (st->rampState() & RAMP_STATE_COAST) != 0;
      if (!coasting) { winFill = 0; winI = 0; }
      else { win[winI] = r; winI = (winI + 1) % 8; if (winFill < 8) winFill++; }
      if (winFill == 8) {
        // ratcheting oscillates (a 6 next to a 300 averages fine) — count
        // deep lows instead; free moves only show isolated single dips
        uint8_t lows = 0;
        for (uint8_t i = 0; i < 8; i++) if (win[i] < jamLow) lows++;
        if (lows >= jamCount) {
          st->forceStop();
          jams++; autoRun = false; moving = false;
          Serial.printf("*** JAM DETECTED (%u of 8 samples below %lu) — aborted "
                        "at pos %ld ***\n", lows, jamLow, (long)st->getCurrentPosition());
          sgReport("jam move");
        }
      }
    }
  } else if (moving) {
    moving = false;
    Serial.printf("arrived at %ld  ", (long)st->getCurrentPosition());
    sgReport("move");
    if (autoRun) {
      delay(300);
      autoTarget = (autoTarget == 7) ? 0 : 7;
      startMove(flagPos + autoTarget * (long)USTEPS_PER_SLOT);
    }
  }
}
