/// SortIQ sorter firmware — BigTreeTech SKR Pico (RP2040) port of the
/// CS7.2 SortIQ fork (firmware/CS72_SortIQ/CS72_SortIQ.ino).
///
/// Derived from Seth Hahner's AI-Case-Sorter-CS7.2 firmware
/// (https://github.com/sjseth/AI-Case-Sorter-CS7.2), GPL-3.0, via the
/// SortIQ fork. This file is likewise GPL-3.0 (see LICENSE at the repo
/// root); modifications by the SortIQ project, 2026.
///
/// WHAT THIS IS: the fork's state machine, function for function, in the
/// same order, re-targeted at the SKR Pico. The serial protocol is the
/// fork's exact superset, so the SortIQ app talks to this board without
/// knowing the difference. What the new hardware buys:
///   * hardware UART to the TMC2209s (one bus, addressed) instead of
///     bit-banged SoftwareSerial — the drivers can be asked things
///   * StallGuard jam detection on both motors: DIAG read at cruise,
///     trips answer error:feed stall detected / error:sort stall detected
///     (the app already turns error lines into a JAM)
///   * motor power as a STATE: the Pi stays on while the 12V is switched,
///     so the drivers appear and vanish under a running board. Presence
///     is polled when idle, config re-applied when power returns, motion
///     refused with error:motor power off while it's gone
///   * WS2812 camera ring on the SERVOS header: brightness and color are
///     values, driven by PIO, never disturbing step timing
///   * the Pi link on the board's UART header (Serial1); USB-C mirrors
///     the protocol for bench work from a PC
///
/// WHAT IT IS NOT (yet): the gated-wheel carousel of phase 2. The seam for
/// it is marked at the bottom (MachineMode); phase 1 is the stock layout.
///
/// Port notes are tagged [PICO] where behavior had to differ from the Uno.

#include <Arduino.h>
#include <TMCStepper.h>
#include <Adafruit_NeoPixel.h>
#include "board_skr_pico.h"

///END OF USER CONFIGURATIONS ///
///DO NOT EDIT BELOW THIS LINE ///
int cameraLEDLevel = CAMERA_LED_LEVEL;
int caseFanLevel = CASEFAN_LEVEL;
double fanPercentConversion=0;
int notificationDelay = FEED_CYCLE_NOTIFICATION_DELAY;
bool airDropEnabled = AIR_DROP_ENABLED;
int feedCycleSignalTime = FEED_CYCLE_COMPLETE_SIGNALTIME;
int feedCyclePreDelay = FEED_CYCLE_COMPLETE_PRESIGNALDELAY;
int feedCyclePostDelay = FEED_CYCLE_COMPLETE_POSTDELAY;
int slotDropDelay = SLOT_DROP_DELAY;
int dropDelay =  airDropEnabled ? feedCyclePostDelay : slotDropDelay;
int feedDirection = FEED_IN_REVERSE == false;
long autoMotorStandbyTimeout = AUTO_MOTORSTANDBY_TIMEOUT;

int feedCurrent = FEED_CURRENT;
int sortCurrent = SORT_CURRENT;
bool enableSftCurrCtrl = true;

int feedSpeed = FEED_MOTOR_SPEED; //represents a number between 1-100
int feedSteps = FEED_STEPS;
int feedMotorSpeed = 500;//this is default and calculated at runtime. do not change this value

int sortSpeed = SORT_MOTOR_SPEED; //represents a number between 1-100
int sortSteps = SORTER_CHUTE_SEPERATION;
int sortMotorSpeed = 500;//this is default and calculated at runtime. do not change this value
int homingSteps = 0;

int feedMicroSteps = feedSteps * FEED_MICROSTEPS;

//SHELLSORTER FORK STATE
int slotPositions[MAX_SLOTS];     //absolute usteps from home, per slot
int accFactor = ACC_FACTOR;       //trapezoid start/stop delay (sortaccel:)
int sortHomeBackoff = SORT_HOME_BACKOFF;
int sortHomeSlowDelay = SORT_HOME_SLOW_DELAY;
int feedLaunchSteps = FEED_LAUNCH_STEPS;
bool feedDecelOverOffset = FEED_DECEL_OVER_OFFSET;
int armDwellMs = 0;               //extra hold before EVERY arm move (armdwell:)
//trapezoid ramp state (sorter)
unsigned int trapDelay = ACC_FACTOR;
int trapN = 0;                    //accel steps taken so far
int rampSteps = 0;                //length of the accel ramp, for the mirror
//launch ramp state (feed)
unsigned int feedTrapDelay = ACC_FACTOR;
int feedTrapN = 0;
//two-stage homing state (sorter): 0=fast seek, 1=backoff, 2=slow approach
int sortHomeStage = 0;
int backoffLeft = 0;      //margin usteps beyond the sensor release point
int backoffTravel = 0;    //usteps spent clearing the flag (bounded)
#define BACKOFF_TRAVEL_MAX 400
#define SORT_DRIFT_TOL 64 //usteps of quiet nudge allowed at slot-0 arrival
//feed homing telemetry (feedstats)
unsigned int homingStepsThisCycle = 0;
unsigned int lastHomingSteps = 0;
unsigned int maxHomingSteps = 0;
unsigned long feedCyclesDone = 0;

//stallguard runtime state
bool sgEnabled = STALLGUARD_ENABLED;
uint8_t feedSgThrs = FEED_SGTHRS;
uint8_t sortSgThrs = SORT_SGTHRS;
uint32_t sgTcoolThrs = SG_TCOOLTHRS;
uint8_t sgFeedHits = 0;               //consecutive DIAG-high cruise steps
uint8_t sgSortHits = 0;
unsigned int sgFeedCruise = 0;        //cruise steps so far this move (arming)
unsigned int sgSortCruise = 0;
unsigned long feedStalls = 0;         //lifetime trip counters (feedstats)
unsigned long sortStalls = 0;
//sgprobe: bench tuning aid — sample SG_RESULT every 32 cruise steps and
//report min/avg at move end. [PICO] a hardware-UART read is ~1ms, so the
//probe costs far less than on the Uno, but it is still bench-only.
bool sgProbe = false;
unsigned int sgProbeMin = 1023, sgProbeN = 0;
unsigned long sgProbeSum = 0;

//motor power state [PICO]
bool motorPower = false;
unsigned long lastPowerPoll = 0;

int feedOverTravelSteps = feedMicroSteps - (FEED_OVERSTEP_THRESHOLD * FEED_MICROSTEPS);
int feedOffsetSteps = FEED_HOMING_OFFSET_STEPS;
int feedHomingOffset = feedOffsetSteps * FEED_MICROSTEPS;

bool FeedScheduled = false;
bool IsFeeding = false;
bool IsFeedHoming = false;
bool IsFeedHomingOffset = false;
bool FeedCycleInProgress = false;
bool FeedCycleComplete = false;
bool IsFeedError = false;

int FeedSteps = feedMicroSteps;
int FeedHomingOffsetSteps = feedHomingOffset;
int feedDelayMS = 150;
int sortDelayMS = 400;

bool forceFeed=false;
String input = "";
int qPos1 = 0;
int qPos2 = 0;
//SS2 pipelined feed: qPos2 holds a REAL assignment (a slot some command
//queued) vs the placeholder a pf leaves behind while the app is still
//classifying. pf refuses to run on a placeholder.
bool slotQueued = true;
int sortStepsToNextPosition = 0;
int sortStepsToNextPositionTracker=0;
bool SortInProgress = false;
bool SortComplete = false;
bool IsSorting = false;
bool IsSortHoming = false;

int sortOffsetSteps = SORT_HOMING_OFFSET_STEPS;
int sortHomingOffset = sortOffsetSteps * SORT_MICROSTEPS;
bool IsSortHomingOffset = false;
int SortHomingOffsetSteps = sortHomingOffset;
bool sorterIsHomed = false;

int slotDelayCalc = 0;

bool IsTestCycle=false;
bool IsSortTestCycle=false;
unsigned long testCycleInterval=0;
unsigned long testsCompleted=0;
int sortToSlot=0;
unsigned long theTime;
unsigned long timeSinceLastSortMove;
unsigned long timeSinceLastMotorMove;
unsigned long msgResetTimer;

//debounce variables
unsigned long lastTrigger = 0;
int triggerTimeout = DEBOUNCE_TIMEOUT;
int debounceTime= DEBOUNCE_PAUSE_TIME;
bool proxActivated = false;
bool sensorDelay = false;

// [PICO] one hardware UART bus, two addressed drivers
TMC2209Stepper sortmotorUART(&Serial2, R_SENSE, SORT_DRIVER_ADDR);
TMC2209Stepper feedmotorUART(&Serial2, R_SENSE, FEED_DRIVER_ADDR);

// [PICO] the camera ring
Adafruit_NeoPixel ring(LED_RING_COUNT, LED_RING_PIN, NEO_GRB + NEO_KHZ800);
uint8_t ringR = 255, ringG = 255, ringB = 255;   //color mix (ledcolor:)

// ---- host link [PICO] -------------------------------------------------------
// Commands arrive on the Pi header UART (Serial1) or USB-C (Serial); replies
// go to whichever spoke last. The protocol itself is untouched.
class DualHost : public Print {
 public:
  Stream *active = &Serial1;
  size_t write(uint8_t c) override { return active->write(c); }
  size_t write(const uint8_t *b, size_t n) override { return active->write(b, n); }
  int available() { return Serial1.available() + Serial.available(); }
  int read() {
    if (Serial1.available()) { active = &Serial1; return Serial1.read(); }
    if (Serial.available())  { active = &Serial;  return Serial.read(); }
    return -1;
  }
};
DualHost host;

// forward declarations (the .ino relied on Arduino's auto-prototypes)
void recvWithEndMarker(); void resetCommand(); void checkSerial();
bool stringToBool(String str); void runAux(); void fillSlotPositions();
int slotPos(int i); void sortTrapInit(); void moveSorterToNextPosition(int position);
void moveSorterToPosition(int position); void runSortMotor(); void setAccSortDelay();
void stepSortMotor(bool forward); void onSortComplete(); void checkFeedErrors();
void onFeedComplete(); void scheduleRun(); void getProxState(); bool readyToFeed();
void runFeedMotor(); void homeFeedMotor(); void feedStatsRecord(); void sortHomingFailed();
void homeSortMotor(); void stepFeedMotor(); void setAccFeedDelay();
void setSorterMotorSpeed(int speed); void setFeedMotorSpeed(int speed);
int setSpeedConversion(int speed); void MotorStandByCheck(); void adjustCameraLED(int level);
void adjustFanLevel(int level); void jogSorter();
void sgApply(); void sgProbeReset(); void sgProbeSample(TMC2209Stepper &drv);
void sgProbeReport(const char *which); bool sgCheckFeed(bool cruising);
bool sgCheckSort(bool cruising); void feedStallDetected(); void sortStallDetected();
void applyDriverConfig(); bool driversPresent(); void checkMotorPower();
bool requireMotorPower(); void ringShow();

void setup() {
  Serial1.begin(HOST_BAUD);              // Pi header UART (GPIO0/1)
  Serial.begin(HOST_BAUD);               // USB-C mirror
  Serial2.begin(DRIVER_BAUD);            // TMC2209 bus (GPIO8/9)
  delay(200);

  //enable motor controllers
  pinMode(FEED_ENABLE, OUTPUT);
  pinMode(SORT_ENABLE, OUTPUT);
  digitalWrite(SORT_ENABLE, LOW);
  digitalWrite(FEED_ENABLE, LOW);

  pinMode(FEED_DIAG_PIN, INPUT);
  pinMode(SORT_DIAG_PIN, INPUT);

  // [PICO] driver config is applied whenever motor power is seen, not only
  // here — the 12V may arrive after boot (or cycle while running)
  motorPower = driversPresent();
  if (motorPower) applyDriverConfig();

  fillSlotPositions();                   // stock-equivalent grid until the
                                         // app pushes per-slot overrides

  setSorterMotorSpeed(SORT_MOTOR_SPEED);
  setFeedMotorSpeed(FEED_MOTOR_SPEED);

  pinMode(FEED_DIRPIN, OUTPUT);
  pinMode(FEED_STEPPIN, OUTPUT);
  pinMode(SORT_DIRPIN, OUTPUT);
  pinMode(SORT_STEPPIN, OUTPUT);

  pinMode(FEED_DONE_SIGNAL, OUTPUT);
  digitalWrite(FEED_DONE_SIGNAL, LOW);
  pinMode(FEED_HOMING_SENSOR, INPUT_PULLUP);
  pinMode(SORT_HOMING_SENSOR, INPUT_PULLUP);
  pinMode(FEED_SENSOR, INPUT_PULLUP);

  pinMode(CASEFAN_PWM, OUTPUT);
  pinMode(AUX_FAN, OUTPUT);            // [PICO] board/Pi fan: plain on, no control
  digitalWrite(AUX_FAN, HIGH);
  ring.begin();
  adjustCameraLED(cameraLEDLevel);
  adjustFanLevel(caseFanLevel);

  digitalWrite(FEED_DIRPIN, feedDirection);

  lastTrigger = millis();
  if (motorPower) jogSorter();

  IsFeedHoming=true;
  IsSortHoming=true;
  msgResetTimer = millis();

  host.print(F("Ready\n"));
  if (!motorPower) host.print(F("info:motor power off\n"));
}

void loop() {
   checkSerial();
   getProxState();
   runSortMotor();
   onSortComplete();
   scheduleRun();
   checkFeedErrors();
   runFeedMotor();
   homeFeedMotor();
   homeSortMotor();
   onFeedComplete();
   runAux();
   MotorStandByCheck();
   checkMotorPower();
}

bool commandReady = false;
char endMarker = '\n';

void recvWithEndMarker() {
    while (host.available() > 0 ) {
        int rc = host.read();
        if (rc < 0) return;
        if (rc == '\r') continue;          // [PICO] tolerate CRLF hosts
        if (rc != endMarker) {
            input += (char)rc;
        }
        else {
            commandReady=true;
            return;
        }
    }
 }
void resetCommand(){
  input="";
  commandReady=false;
}

void checkSerial(){
  if(FeedCycleInProgress==false && SortInProgress==false && host.available()>0){

       recvWithEndMarker();

       if(!commandReady){
        return;
       }

      if (input.startsWith("stop")) {
         resetCommand();
          FeedScheduled=false;
          IsFeedHoming=false;
          IsFeedHomingOffset = false;
          IsSortHomingOffset = false;
          FeedCycleComplete=true;
          FeedCycleInProgress = false;
          IsTestCycle=false;
         return;
      }

     //this should be most cases
      if (isDigit(input[0])) {
        if (!requireMotorPower()) { resetCommand(); return; }
        moveSorterToNextPosition(input.toInt());
        FeedScheduled = true;
        IsFeeding = false;
        scheduleRun();
        resetCommand();
        return;
      }
      if (input.startsWith("homefeeder")) {
        if (!requireMotorPower()) { resetCommand(); return; }
        feedDelayMS=400;
          IsFeedHoming=true;
         host.print(F("ok\n"));
         resetCommand();
         return;
      }
      if (input.startsWith("homesorter")) {
        if (!requireMotorPower()) { resetCommand(); return; }
        sortDelayMS=400;
           jogSorter();
        qPos1 = 0;
        qPos2 = 0;
        slotQueued = true;   //SS2: the queue is rebuilt to a known state
          homingSteps=0;
          sortHomeStage=0;
          IsSortHoming=true;
          host.print(F("ok\n"));
          resetCommand();
         return;
      }
     if(input.startsWith("status")){
        host.print(F("SORT microsteps: "));   host.println(sortmotorUART.microsteps());
        host.print(F("SORT current: "));   host.println(sortmotorUART.rms_current());
        host.print(F("SORT Stealth: "));   host.println(sortmotorUART.stealth());

        host.print(F("FEED microsteps: "));   host.println(feedmotorUART.microsteps());
        host.print(F("FEED current: "));   host.println(feedmotorUART.rms_current());
        host.print(F("FEED Stealth: "));   host.println(feedmotorUART.stealth());
        host.print(F("Motor power: "));   host.println(motorPower ? F("on") : F("off"));
        resetCommand();
        return;
     }

       if(input.startsWith("feedmotorcurrent:")){
            input.replace("feedmotorcurrent:", "");
            feedCurrent = input.toInt();
            if(feedCurrent>1800){
              feedCurrent=1800;
            }
          feedmotorUART.rms_current(feedCurrent);       // Set motor RMS current
           host.print(F("ok\n"));
         resetCommand();
        return;
       }
      if(input.startsWith("sortmotorcurrent:")){
            input.replace("sortmotorcurrent:", "");
            sortCurrent = input.toInt();
            if(sortCurrent>1800){
              sortCurrent=1800;
            }
          sortmotorUART.rms_current(sortCurrent);       // Set motor RMS current
           host.print(F("ok\n"));
         resetCommand();
        return;
       }
      if (input.startsWith("sortto:")) {
          if (!requireMotorPower()) { resetCommand(); return; }
          input.replace("sortto:", "");
          moveSorterToPosition(input.toInt());
           host.print(F("ok\n"));
           resetCommand();
         return;
      }

      if (input.startsWith("xf:")) {
          if (!requireMotorPower()) { resetCommand(); return; }
          input.replace("xf:", "");
          forceFeed = true;
          moveSorterToNextPosition(input.toInt());
          resetCommand();
          FeedScheduled = true;
          IsFeeding = false;
          scheduleRun();
          return;
      }

      if (input.startsWith("getconfig")) {
        host.print(F("{\"FeedMotorCurrent\":"));
        host.print(feedCurrent);
        host.print(F(",\"FeedMotorSpeed\":"));
        host.print(feedSpeed);
        host.print(F(",\"FeedCycleSteps\":"));
        host.print(feedSteps);
        host.print(F(",\"SortMotorCurrent\":"));
        host.print(sortCurrent);
        host.print(F(",\"SortMotorSpeed\":"));
        host.print(sortSpeed);
        host.print(F(",\"SortSteps\":"));
        host.print(sortSteps);
        host.print(F(",\"NotificationDelay\":"));
        host.print(notificationDelay);
        host.print(F(",\"SlotDropDelay\":"));
        host.print(slotDropDelay);
        host.print(F(",\"AirDropEnabled\":"));
        host.print(airDropEnabled);
        host.print(F(",\"AirDropPostDelay\":"));
        host.print(feedCyclePostDelay);
        host.print(F(",\"AirDropPreDelay\":"));
        host.print(feedCyclePreDelay);
        host.print(F(",\"AirDropSignalTime\":"));
        host.print(feedCycleSignalTime);
        host.print(F(",\"FeedHomingOffset\":"));
        host.print(feedOffsetSteps);
        host.print(F(",\"SortHomingOffset\":"));
        host.print(sortOffsetSteps);
        host.print(F(",\"AutoMotorStandbyTimeout\":"));
        host.print(autoMotorStandbyTimeout);
        host.print(F(",\"CaseFanSpeedEnabled\":"));
        host.print(CASEFAN_SW_CTRL);
        host.print(F(",\"CaseFanLevel\":"));
        host.print(caseFanLevel);
        host.print(F(",\"CameraLEDLevel\":"));
        host.print(cameraLEDLevel);
        host.print(F(",\"DebounceTimeout\":"));
        host.print(triggerTimeout);
        host.print(F(",\"DebouncePauseTime\":"));
        host.print(debounceTime);
        //// SHELLSORTER FORK KEYS ////
        host.print(F(",\"SortAccelFactor\":"));
        host.print(accFactor);
        host.print(F(",\"SortHomeBackoff\":"));
        host.print(sortHomeBackoff);
        host.print(F(",\"SortHomeSlowDelay\":"));
        host.print(sortHomeSlowDelay);
        host.print(F(",\"FeedLaunchSteps\":"));
        host.print(feedLaunchSteps);
        host.print(F(",\"FeedDecelOverOffset\":"));
        host.print(feedDecelOverOffset);
        host.print(F(",\"ArmDwellMs\":"));
        host.print(armDwellMs);
        host.print(F(",\"MaxSlots\":"));
        host.print(MAX_SLOTS);
        host.print(F(",\"SlotPositions\":\""));
        for (int i = 0; i < MAX_SLOTS; i++) {
          if (i) { host.print(','); }
          host.print(slotPositions[i]);
        }
        host.print(F("\""));
        //// PICO KEYS ////
        host.print(F(",\"StallGuardEnabled\":"));
        host.print(sgEnabled);
        host.print(F(",\"FeedStallThreshold\":"));
        host.print(feedSgThrs);
        host.print(F(",\"SortStallThreshold\":"));
        host.print(sortSgThrs);
        host.print(F(",\"MotorPower\":"));
        host.print(motorPower);
        host.print(F(",\"Board\":\"SKR_PICO\""));
        host.print(F("}\n"));
        resetCommand();
        return;
      }

        if (input.startsWith("debounceTimeout:")) {
          input.replace("debounceTimeout:", "");
          triggerTimeout = input.toInt();
          host.print(F("ok\n"));
          resetCommand();
          return;
        }

        if (input.startsWith("debounceTime:")) {
          input.replace("debounceTime:", "");
          debounceTime = input.toInt();
          host.print(F("ok\n"));
          resetCommand();
          return;
        }

       //set feed speed. Values 1-100. Def 60
      if (input.startsWith("feedspeed:")) {
        input.replace("feedspeed:", "");
        feedSpeed = input.toInt();
        setFeedMotorSpeed(feedSpeed);
        host.print(F("ok\n"));
        resetCommand();
        return;
      }
      //set feed homing offset
      if (input.startsWith("feedhomingoffset:")) {
        input.replace("feedhomingoffset:", "");
        feedOffsetSteps = input.toInt();
        feedHomingOffset = feedOffsetSteps * FEED_MICROSTEPS;
        FeedHomingOffsetSteps = feedHomingOffset;
        host.print(F("ok\n"));
        resetCommand();
        return;
      }
      if (input.startsWith("sorthomingoffset:")) {
        input.replace("sorthomingoffset:", "");
        sortOffsetSteps = input.toInt();
        sortHomingOffset = sortOffsetSteps * SORT_MICROSTEPS;
        SortHomingOffsetSteps = sortHomingOffset;
        host.print(F("ok\n"));
        resetCommand();
        return;
      }

      //// SHELLSORTER FORK COMMANDS ////
      //per-slot absolute position, in usteps from home: slotpos:<i>:<usteps>
      //(push sortsteps FIRST on connect - it refills the whole table)
      if (input.startsWith("slotpos:")) {
        input.replace("slotpos:", "");
        int sep = input.indexOf(':');
        if (sep > 0) {
          int idx = input.substring(0, sep).toInt();
          int val = input.substring(sep + 1).toInt();
          if (idx >= 0 && idx < MAX_SLOTS) {
            slotPositions[idx] = val;
          }
        }
        host.print(F("ok\n"));
        resetCommand();
        return;
      }
      //trapezoid start/stop delay in us (bigger = gentler launch)
      if (input.startsWith("sortaccel:")) {
        input.replace("sortaccel:", "");
        accFactor = input.toInt();
        if (accFactor < 100) { accFactor = 100; }
        if (accFactor > 5000) { accFactor = 5000; }
        host.print(F("ok\n"));
        resetCommand();
        return;
      }
      if (input.startsWith("sorthomebackoff:")) {
        input.replace("sorthomebackoff:", "");
        sortHomeBackoff = input.toInt();
        if (sortHomeBackoff < 0) { sortHomeBackoff = 0; }
        if (sortHomeBackoff > 200) { sortHomeBackoff = 200; }
        host.print(F("ok\n"));
        resetCommand();
        return;
      }
      if (input.startsWith("sorthomeslow:")) {
        input.replace("sorthomeslow:", "");
        sortHomeSlowDelay = input.toInt();
        if (sortHomeSlowDelay < 100) { sortHomeSlowDelay = 100; }
        if (sortHomeSlowDelay > 5000) { sortHomeSlowDelay = 5000; }
        host.print(F("ok\n"));
        resetCommand();
        return;
      }
      if (input.startsWith("feedlaunch:")) {
        input.replace("feedlaunch:", "");
        feedLaunchSteps = input.toInt();
        if (feedLaunchSteps < 0) { feedLaunchSteps = 0; }
        if (feedLaunchSteps > 200) { feedLaunchSteps = 200; }
        host.print(F("ok\n"));
        resetCommand();
        return;
      }
      if (input.startsWith("armdwell:")) {
        input.replace("armdwell:", "");
        armDwellMs = input.toInt();
        if (armDwellMs < 0) { armDwellMs = 0; }
        if (armDwellMs > 1000) { armDwellMs = 1000; }
        host.print(F("ok\n"));
        resetCommand();
        return;
      }
      if (input.startsWith("feeddecel:")) {
        input.replace("feeddecel:", "");
        feedDecelOverOffset = stringToBool(input);
        host.print(F("ok\n"));
        resetCommand();
        return;
      }
      //SS2 pipelined feed. pf = run one full cycle NOW (arm to the slot
      //queued by the previous ps:, then feed), leaving the queue tail as
      //a placeholder; ps:<slot> = assign the queue tail for the case the
      //app just photographed.
      if (input.startsWith("ps:")) {
        input.replace("ps:", "");
        qPos2 = input.toInt();
        slotQueued = true;
        resetCommand();       //silent: a reply would interleave with the
        return;               //cycle's done; the pf guard audits misuse
      }
      if (input.startsWith("pf")) {
        if (slotQueued == false) {
          host.print(F("error:no slot queued\n"));
          resetCommand();
          return;
        }
        if (!requireMotorPower()) { resetCommand(); return; }
        moveSorterToNextPosition(qPos2);  //arm -> the previous ps: slot;
        slotQueued = false;               //the self-assigned tail is a
                                          //placeholder until the next ps:
        FeedScheduled = true;
        IsFeeding = false;
        scheduleRun();
        resetCommand();
        return;
      }
      //feed homing telemetry: seek usteps last cycle / worst / cycle count
      if (input.startsWith("feedstats")) {
        host.print(F("{\"LastHomingSteps\":"));
        host.print(lastHomingSteps);
        host.print(F(",\"MaxHomingSteps\":"));
        host.print(maxHomingSteps);
        host.print(F(",\"FeedCycles\":"));
        host.print(feedCyclesDone);
        host.print(F(",\"FeedStalls\":"));
        host.print(feedStalls);
        host.print(F(",\"SortStalls\":"));
        host.print(sortStalls);
        host.print(F("}\n"));
        resetCommand();
        return;
      }
      //// STALLGUARD COMMANDS ////
      if (input.startsWith("sgfeed:")) {
        input.replace("sgfeed:", "");
        int v = input.toInt();
        if (v < 0) { v = 0; } if (v > 255) { v = 255; }
        feedSgThrs = (uint8_t)v; sgApply();
        host.print(F("ok\n")); resetCommand(); return;
      }
      if (input.startsWith("sgsort:")) {
        input.replace("sgsort:", "");
        int v = input.toInt();
        if (v < 0) { v = 0; } if (v > 255) { v = 255; }
        sortSgThrs = (uint8_t)v; sgApply();
        host.print(F("ok\n")); resetCommand(); return;
      }
      if (input.startsWith("sgtcool:")) {
        input.replace("sgtcool:", "");
        long v = input.toInt();
        if (v < 0) { v = 0; } if (v > 0xFFFFFL) { v = 0xFFFFFL; }
        sgTcoolThrs = (uint32_t)v; sgApply();
        host.print(F("ok\n")); resetCommand(); return;
      }
      if (input.startsWith("sgprobe:")) {
        input.replace("sgprobe:", "");
        sgProbe = stringToBool(input);
        host.print(F("ok\n")); resetCommand(); return;
      }
      if (input.startsWith("sgstats")) {
        host.print(F("{\"enabled\":")); host.print(sgEnabled ? 1 : 0);
        host.print(F(",\"feedThrs\":")); host.print(feedSgThrs);
        host.print(F(",\"sortThrs\":")); host.print(sortSgThrs);
        host.print(F(",\"feedSG\":")); host.print(motorPower ? feedmotorUART.SG_RESULT() : 0);
        host.print(F(",\"sortSG\":")); host.print(motorPower ? sortmotorUART.SG_RESULT() : 0);
        host.print(F(",\"feedDiag\":")); host.print(digitalRead(FEED_DIAG_PIN));
        host.print(F(",\"sortDiag\":")); host.print(digitalRead(SORT_DIAG_PIN));
        host.print(F(",\"feedStalls\":")); host.print(feedStalls);
        host.print(F(",\"sortStalls\":")); host.print(sortStalls);
        host.print(F(",\"motorPower\":")); host.print(motorPower ? 1 : 0);
        host.print(F("}\n"));
        resetCommand(); return;
      }
      if (input.startsWith("sg:")) {
        input.replace("sg:", "");
        sgEnabled = stringToBool(input);
        host.print(F("ok\n")); resetCommand(); return;
      }
      //// PICO COMMANDS ////
      //ledcolor:r,g,b — the ring's color mix (white = 255,255,255); the
      //level from cameraledlevel: scales it
      if (input.startsWith("ledcolor:")) {
        input.replace("ledcolor:", "");
        int c1 = input.indexOf(','), c2 = input.lastIndexOf(',');
        if (c1 > 0 && c2 > c1) {
          ringR = constrain(input.substring(0, c1).toInt(), 0, 255);
          ringG = constrain(input.substring(c1 + 1, c2).toInt(), 0, 255);
          ringB = constrain(input.substring(c2 + 1).toInt(), 0, 255);
          ringShow();
        }
        host.print(F("ok\n")); resetCommand(); return;
      }
      //// END SHELLSORTER FORK COMMANDS ////

      if (input.startsWith("sortspeed:")) {
        input.replace("sortspeed:", "");
        sortSpeed = input.toInt();
        setSorterMotorSpeed(sortSpeed);
        host.print(F("ok\n"));
        resetCommand();
        return;
      }
      if (input.startsWith("sortsteps:")) {
        input.replace("sortsteps:", "");
        sortSteps = input.toInt();
        fillSlotPositions();     //fork: rebuilds the whole slot table, so
                                 //any slotpos overrides must be re-pushed
        host.print(F("ok\n"));
        resetCommand();
        return;
      }
      //set feed steps. Values 1-1000. Def 100
      if (input.startsWith("feedsteps:")) {
        input.replace("feedsteps:", "");
        feedSteps = input.toInt();
        feedMicroSteps = feedSteps * FEED_MICROSTEPS;
        feedOverTravelSteps = feedMicroSteps - (FEED_OVERSTEP_THRESHOLD * FEED_MICROSTEPS);
        host.print(F("ok\n"));
        resetCommand();
        return;
      }
      if (input.startsWith("notificationdelay:")) {
        input.replace("notificationdelay:", "");
        notificationDelay = input.toInt();
        host.print(F("ok\n"));
        resetCommand();
        return;
      }
      if (input.startsWith("slotdropdelay:")) {
        input.replace("slotdropdelay:", "");
        slotDropDelay = input.toInt();
        dropDelay =  airDropEnabled ? feedCyclePostDelay: slotDropDelay;
        host.print(F("ok\n"));
        resetCommand();
        return;
      }
      if (input.startsWith("airdropenabled:")) {
        input.replace("airdropenabled:", "");
        airDropEnabled = stringToBool(input);
        dropDelay =  airDropEnabled ? feedCyclePostDelay: slotDropDelay;
        host.print(F("ok\n"));
        resetCommand();
        return;
      }
      if (input.startsWith("airdroppostdelay:")) {
        input.replace("airdroppostdelay:", "");
        feedCyclePostDelay = input.toInt();
        dropDelay =  airDropEnabled ? feedCyclePostDelay: slotDropDelay;
        host.print(F("ok\n"));
        resetCommand();
        return;
      }
       if (input.startsWith("airdroppredelay:")) {
        input.replace("airdroppredelay:", "");
        feedCyclePreDelay = input.toInt();
        host.print(F("ok\n"));
        resetCommand();
        return;
      }
      if (input.startsWith("airdropdsignalduration:")) {
        input.replace("airdropdsignalduration:", "");
        feedCycleSignalTime = input.toInt();
        host.print(F("ok\n"));
        resetCommand();
        return;
      }
      if (input.startsWith("fan:")) {
        input.replace("fan:", "");
        caseFanLevel = input.toInt();
        adjustFanLevel(caseFanLevel);
        host.print(F("ok\n"));
        resetCommand();
        return;
      }
      if (input.startsWith("automotorstandbytimeout:")) {
        input.replace("automotorstandbytimeout:", "");
        autoMotorStandbyTimeout = input.toDouble();
        host.print(F("ok\n"));
        resetCommand();
        return;
      }
      if (input.startsWith("cameraledlevel:")) {
         input.replace("cameraledlevel:", "");
         adjustCameraLED(input.toInt() );
         host.print(F("ok\n"));
         resetCommand();
         return;
       }

      if (input.startsWith("test:")) {
        input.replace("test:", "");
        IsTestCycle=true;
        testCycleInterval=input.toFloat();
        testsCompleted=0;
        FeedScheduled=false;
        FeedCycleInProgress=false;
        host.print(F("testing started\n"));
        resetCommand();
        return;
      }

       if (input.startsWith("sorttest:")) {
        input.replace("sorttest:", "");
        IsSortTestCycle=true;
        testCycleInterval=static_cast<unsigned long>(input.toInt());
        testsCompleted=0;
        host.print(F("testing started\n"));
        resetCommand();
        return;
      }

       if (input.startsWith("ping")) {
        resetCommand();
        host.print(F(" ok\n"));
        return;
      }
      if (input.startsWith("version")) {
        host.print(F(FIRMWARE_VERSION));
        host.print(F("\n"));
        resetCommand();
        return;
      }
      resetCommand();
      host.print(F("ok\n"));
  }
}

bool stringToBool(String str) {
  str.toLowerCase();
  if(str=="true"){
    return true;
  }
  if(str == "1"){
    return true;
  }
  return false;
}

//this method is to run all "other" routines not in the main duty cycles (such as tests)
void runAux(){
  //This runs the feed and sort test if scheduled
  if(IsTestCycle==true&&FeedScheduled==false&&FeedCycleInProgress==false){
    if(testsCompleted<testCycleInterval){
       int slot = random(0,6);
         moveSorterToNextPosition(slot);
        FeedScheduled = true;
        IsFeeding = false;
        scheduleRun();
        host.print(testsCompleted);
        host.print(F(" - "));
        host.print(slot);
        host.print(F("\n"));
        testsCompleted++;
    }else{
      IsTestCycle=false;
      testCycleInterval=0;
      testsCompleted=0;
    }
  }

  //this runs the sorter only test cycles if scheduled
  if(IsSortTestCycle==true&&SortInProgress==false){
    if(testsCompleted<testCycleInterval){
       int slot = random(0,8);
       delay(40);
       host.print(testsCompleted);
       host.print(F(" - Sorting to: "));
       host.println(slot);
       moveSorterToPosition(slot);
        testsCompleted++;
    }else{
      moveSorterToPosition(0);
      host.println("Sort Test Completed");
      IsSortTestCycle=false;
      testsCompleted=0;
      testCycleInterval=0;
    }
  }
}

//per-slot position table: index -> absolute usteps from home. Filled from
//sortSteps (exact stock grid) until slotpos:<i>:<v> overrides an entry.
void fillSlotPositions(){
  for(int i=0;i<MAX_SLOTS;i++){
    slotPositions[i] = i * sortSteps * SORT_MICROSTEPS;
  }
}

int slotPos(int i){
  if(i < 0) i = 0;
  if(i >= MAX_SLOTS) i = MAX_SLOTS - 1;
  return slotPositions[i];
}

void sortTrapInit(){
  trapDelay = accFactor;
  trapN = 0;
  rampSteps = 0;
}

void moveSorterToNextPosition(int position){
    sgSortHits = 0; sgSortCruise = 0; sgProbeReset();   //stallguard: fresh move
    sortToSlot=position;
    sortStepsToNextPosition = slotPos(qPos1) - slotPos(qPos2);
    sortStepsToNextPositionTracker = sortStepsToNextPosition;
    sortTrapInit();
    if(sortStepsToNextPosition !=0){
      theTime = millis();
       slotDelayCalc = (dropDelay - (theTime - timeSinceLastSortMove));
       slotDelayCalc = slotDelayCalc > 0? slotDelayCalc : 1;
       if(slotDelayCalc > dropDelay){
         slotDelayCalc=dropDelay;
       }
      delay(slotDelayCalc);
      //fork: unconditional extra hold on top of the remainder logic —
      //brass-clearance time for slow tumblers in the arm tube
      if(armDwellMs > 0){
        delay(armDwellMs);
      }
    }
    qPos1 = qPos2;
    qPos2 =position;
    slotQueued = true;   //SS2: every legacy caller passes a real slot; the
                         //pf handler overrides this right after its call
    SortInProgress = true;
    SortComplete = false;
    IsSorting = true;
}

void moveSorterToPosition(int position){
    sgSortHits = 0; sgSortCruise = 0; sgProbeReset();
    sortToSlot=position;
    sortStepsToNextPosition = slotPos(qPos1) - slotPos(position);
    sortStepsToNextPositionTracker = sortStepsToNextPosition;
    sortTrapInit();
    qPos1 =position;
    qPos2 =position;
    slotQueued = true;   //SS2: sortto collapses the queue to a real slot
    SortInProgress = true;
    SortComplete = false;
    IsSorting = true;
}

void runSortMotor(){
  if(IsSorting==true){

    if(sortStepsToNextPosition==0){

      if(qPos1==0){
        //arm the homing state machine ONCE per arrival
        if(IsSortHoming==false){
          homingSteps=0;
          sortHomeStage=0;
          backoffTravel=0;
          IsSortHoming=true;
        }
      }else{
         IsSorting=false;
         SortComplete = true;
      }
      return;
    }
    setAccSortDelay();
    if(sgCheckSort(sortDelayMS == sortMotorSpeed)){ return; }  //stall? move aborted

    if(sortStepsToNextPosition > 0){
      stepSortMotor(true);
      sortStepsToNextPosition--;
    }
    else {
      stepSortMotor(false);
      sortStepsToNextPosition++;
    }
  }
}
//True trapezoid (AVR446-style integer approximation of constant accel):
//delay_n = delay_{n-1} - 2*delay_{n-1}/(4n+1) while accelerating, mirrored
//on the way down. Short moves become triangles.
void setAccSortDelay(){
    if(ACC_SORT_ENABLED == false){
      sortDelayMS=sortMotorSpeed;
      return;
    }
    int remaining = abs(sortStepsToNextPosition);       //includes this step
    int total = abs(sortStepsToNextPositionTracker);
    int done = total - remaining;

    if(remaining <= rampSteps){                          //down ramp (mirror)
      if(trapN > 1){
        trapDelay += (2UL * trapDelay) / (4UL * trapN + 1);
        trapN--;
      }
      if(trapDelay > (unsigned int)accFactor){
        trapDelay = accFactor;
      }
      sortDelayMS = trapDelay;
      return;
    }

    if(trapDelay > (unsigned int)sortMotorSpeed && done < (total + 1) / 2){
      trapN++;                                           //up ramp
      trapDelay -= (2UL * trapDelay) / (4UL * trapN + 1);
      if(trapDelay < (unsigned int)sortMotorSpeed){
        trapDelay = sortMotorSpeed;
      }
      rampSteps = trapN;
      sortDelayMS = trapDelay;
      return;
    }

    trapDelay = sortMotorSpeed;                          //cruise
    sortDelayMS = sortMotorSpeed;
}
bool sortDirection = false;
void stepSortMotor(bool forward){
     sortDirection = forward == SORT_IN_REVERSE;
     digitalWrite(SORT_ENABLE, LOW);
     digitalWrite(SORT_DIRPIN, sortDirection);
    digitalWrite(SORT_STEPPIN, HIGH);
    delayMicroseconds(10);  //pulse width
    digitalWrite(SORT_STEPPIN, LOW);
    delayMicroseconds(sortDelayMS); //controls motor speed
}
void onSortComplete(){
  if(SortInProgress==true && SortComplete==true){
        sgProbeReport("sort");
        SortInProgress=false;
        timeSinceLastSortMove = millis();
        timeSinceLastMotorMove = timeSinceLastSortMove;
  }
}

void checkFeedErrors(){
 if(FeedCycleComplete == false && FeedSteps < feedOverTravelSteps){
      FeedScheduled=false;
      FeedCycleComplete=true;
      IsFeeding=false;
      IsFeedHoming= false;
      IsFeedHomingOffset=false;
      IsFeedError = true;
      FeedCycleInProgress = false;
      host.println("error:feed overtravel detected");
 }
}

void onFeedComplete(){
  if(FeedCycleComplete==true&& IsFeedError==false){
    timeSinceLastMotorMove = millis();
   //this allows some time for the brass to start dropping before generating the airblast
    if(airDropEnabled)
    {
      delay(feedCyclePreDelay);
      digitalWrite(FEED_DONE_SIGNAL, HIGH);
      delay(feedCycleSignalTime);
      digitalWrite(FEED_DONE_SIGNAL,LOW);
    }
    delay(notificationDelay);
    host.print(F("done\n"));
    FeedCycleComplete=false;
    forceFeed= false;
    return;
  }
}

void scheduleRun(){
  if(FeedScheduled==true && IsFeeding==false){
      if(readyToFeed()){
      //set run variables
      IsFeedError=false;
      sgFeedHits = 0; sgFeedCruise = 0; sgProbeReset();   //stallguard: fresh feed
      FeedSteps = feedMicroSteps;
      FeedScheduled=false;
      FeedCycleInProgress = true;
      FeedCycleComplete=false;
      IsFeeding=true;
      feedTrapDelay = accFactor;        //arm the launch ramp (fork)
      feedTrapN = 0;
      homingStepsThisCycle = 0;         //feedstats telemetry
    }else{
      theTime = millis();
      if(theTime - msgResetTimer > 1000){
          host.println("waiting for brass");
          msgResetTimer = millis();
      }
    }
  }
}

void getProxState(){
  //if the sensor is triggered, update the last trigger time and set the variable proxActivated
  if(digitalRead(FEED_SENSOR) == FEEDSENSOR_TYPE){
      proxActivated=true;
      lastTrigger = millis();
      return;
  }
  //sensor is not triggered
  proxActivated=false;
  //check to see if the time since last trigger is longer than the timeout, if so set the delay variable.
  if(millis() - lastTrigger > (unsigned long)triggerTimeout){
    sensorDelay = true;
  }
}

bool readyToFeed()
{
  //if feedsensor is not enabled, or it is a forcefeed,  we are always ready!
  if(FEEDSENSOR_ENABLED==false || forceFeed==true){
    return true;
  }
  //if no brass is detected, we are not ready
  if(proxActivated == false){
    return false;
  }
  //sensorDelay is calculated in getProxState()
  if(sensorDelay){
        delay(debounceTime);
        sensorDelay = false;
        return false;
  }
   return true;
}

void runFeedMotor() {
  if(SortInProgress){
    return;
  }

  if(IsFeeding==true && FeedSteps > 0 )
  {
    setAccFeedDelay();
    if(sgCheckFeed(feedDelayMS == feedMotorSpeed)){ return; }  //stall? move aborted
    stepFeedMotor();
    FeedSteps--;
    return;
  }
  if(IsFeeding==true){
    IsFeeding=false;
    IsFeedHoming = true;
  }
  return;
}

void homeFeedMotor(){
  if(IsFeedHoming==true ){
    if(FEED_HOMING_ENABLED == false){
      IsFeedHoming=false;
       IsFeedHomingOffset = false;
      FeedCycleComplete=true;
      FeedCycleInProgress = false;
      return;
    }

    if (digitalRead(FEED_HOMING_SENSOR) == FEED_HOMING_SENSOR_TYPE) {
      IsFeedHoming=false;
      if(FeedCycleInProgress){ //if we are homing initially, we don't need to apply offsets
          IsFeedHomingOffset = true;
          FeedHomingOffsetSteps = feedHomingOffset;
      }
      return;
    }
    feedDelayMS = feedMotorSpeed;   //the sensor edge is taken at cruise, by
    if(sgCheckFeed(true)){ return; }  //stall on the seek = jammed wheel
    stepFeedMotor();                //design - never creep across the port
    FeedSteps--;
    homingStepsThisCycle++;         //feedstats: drift/jam telemetry
    return;
  }

  if(IsFeedHomingOffset == true){
    if(feedHomingOffset == 0)
    {
      IsFeedHomingOffset = false;
      FeedCycleComplete=true;
      FeedCycleInProgress = false;
      feedStatsRecord();
      return;
    }
    if(IsFeedHomingOffset == true && FeedHomingOffsetSteps > 0){
      //fork: shape the stop across the offset — linear decel ramp, or
      //flat-out with a crisp dead stop (feeddecel:0)
      if(feedDecelOverOffset && feedHomingOffset > 0){
        long doneOff = (long)feedHomingOffset - FeedHomingOffsetSteps;
        feedDelayMS = feedMotorSpeed +
          (int)(((long)(accFactor - feedMotorSpeed) * doneOff) / feedHomingOffset);
      }else{
        feedDelayMS = feedMotorSpeed;
      }
      stepFeedMotor();
      FeedHomingOffsetSteps--;
      FeedSteps--;
    }
    else if(IsFeedHomingOffset == true && FeedHomingOffsetSteps<=0){
      IsFeedHomingOffset = false;
      FeedCycleComplete=true;
      FeedCycleInProgress = false;
      feedStatsRecord();
    }
  }
}

void feedStatsRecord(){
  sgProbeReport("feed");
  lastHomingSteps = homingStepsThisCycle;
  if(homingStepsThisCycle > maxHomingSteps){
    maxHomingSteps = homingStepsThisCycle;
  }
  feedCyclesDone++;
}

//Homing search budget exhausted: the flag was never (re)found. Report and
//hand the board back instead of hanging; the app's jam path re-homes.
void sortHomingFailed(){
  IsSorting=false;
  SortComplete=true;
  IsSortHoming=false;
  IsSortHomingOffset=false;
  homingSteps=0;
  sortHomeStage=0;
  host.println(F("error:sort homing failed"));
}

//Two-stage homing (fork): fast seek to the sensor at cruise, back off past
//it, then re-approach slowly - the trigger edge is taken at a low, constant
//speed every time, which is what makes slot positions repeatable.
void homeSortMotor(){
  if(IsSortHoming==true && SORT_HOMING_ENABLED == false){
     IsSorting=false;
         SortComplete = true;
         IsSortHoming =false;
         IsSortHomingOffset = false;
         return;
  }
  if(IsSortHoming==true){
    if(IsSorting==true){
         if(sortHomeStage == 0){                 //arrival check at slot 0
           if(digitalRead(SORT_HOMING_SENSOR)==SORT_HOMING_SENSOR_TYPE){
             IsSorting=false;
             SortComplete = true;
             IsSortHoming =false;
             homingSteps=0;
             return;
           }
           //the rest point sits ON the sensor threshold — nudge forward quietly
           if(homingSteps < SORT_DRIFT_TOL){
             sortDelayMS = sortHomeSlowDelay;
             stepSortMotor(true);
             homingSteps++;
             return;
           }
           sortHomeStage = 3;                    //beyond tolerance: REAL drift
           return;
         }
         if(sortHomeStage == 3){                 //drift seek to the flag
           if(digitalRead(SORT_HOMING_SENSOR)!=SORT_HOMING_SENSOR_TYPE){
            if(homingSteps < (200*SORT_MICROSTEPS)){
                sortDelayMS = sortMotorSpeed;
                if(sgCheckSort(true)){ return; }  //arm met something on the seek
                stepSortMotor(true);
                homingSteps++;
                return;
            }
            sortHomingFailed();
            return;
           }
           sortHomeStage = 1;
           backoffLeft = sortHomeBackoff;
           backoffTravel = 0;
           return;
         }
         if(sortHomeStage == 1){                 //back off until the flag
           if(digitalRead(SORT_HOMING_SENSOR)==SORT_HOMING_SENSOR_TYPE){
             if(backoffTravel < BACKOFF_TRAVEL_MAX){  //RELEASES, then margin
               sortDelayMS = sortHomeSlowDelay;
               stepSortMotor(false);
               backoffTravel++;
               return;
             }
             sortHomeStage = 2;                  //flag too wide to clear
             return;
           }
           if(backoffLeft > 0){
             sortDelayMS = sortHomeSlowDelay;
             stepSortMotor(false);
             backoffLeft--;
             return;
           }
           sortHomeStage = 2;
           return;
         }
         //stage 2: slow re-approach - the repeatable edge
         if(digitalRead(SORT_HOMING_SENSOR)!=SORT_HOMING_SENSOR_TYPE){
          if(homingSteps < (210*SORT_MICROSTEPS)){
              sortDelayMS = sortHomeSlowDelay;
              stepSortMotor(true);
              homingSteps++;
              return;
          }
          sortHomingFailed();
          return;
         }
         IsSorting=false;
         SortComplete = true;
         IsSortHoming =false;
         homingSteps=0;
         sortHomeStage=0;
         return;
    }
    else if(IsSortHomingOffset != true){
      if(sortHomeStage == 0){                    //fast seek
        if(digitalRead(SORT_HOMING_SENSOR)!=SORT_HOMING_SENSOR_TYPE){
            if(homingSteps < (210*SORT_MICROSTEPS)){
                sortDelayMS = sortMotorSpeed;
                stepSortMotor(true);
                homingSteps++;
            }else{
              sortHomingFailed();
            }
        }else{
          sortHomeStage = 1;
          backoffLeft = sortHomeBackoff;
          backoffTravel = 0;
        }
      }
      else if(sortHomeStage == 1){               //back off until the flag
        if(digitalRead(SORT_HOMING_SENSOR)==SORT_HOMING_SENSOR_TYPE){
          if(backoffTravel < BACKOFF_TRAVEL_MAX){
            sortDelayMS = sortHomeSlowDelay;
            stepSortMotor(false);
            backoffTravel++;
          }else{
            sortHomeStage = 2;
          }
        }
        else if(backoffLeft > 0){
          sortDelayMS = sortHomeSlowDelay;
          stepSortMotor(false);
          backoffLeft--;
        }else{
          sortHomeStage = 2;
        }
      }
      else if(digitalRead(SORT_HOMING_SENSOR)!=SORT_HOMING_SENSOR_TYPE){
          if(homingSteps < (210*SORT_MICROSTEPS)){
              sortDelayMS = sortHomeSlowDelay;   //slow re-approach
              stepSortMotor(true);
              homingSteps++;
          }else{
            sortHomingFailed();
          }
      }else{ //homed on the slow edge - schedule the offset move
        IsSortHomingOffset=true;
        SortHomingOffsetSteps = sortHomingOffset;
        homingSteps = 0;
        sortHomeStage = 0;
      }
    }

   //If sort homing offset true, means we are in offset steps
    if(IsSortHomingOffset == true){
      if(sortHomingOffset == 0)
      {
        IsSortHomingOffset = false;
        IsSortHoming=false;
        SortComplete=true;
        return;
      }
      if(SortHomingOffsetSteps > 0){
        stepSortMotor(true);
        SortHomingOffsetSteps--;
      }
      else if(IsSortHomingOffset == true && SortHomingOffsetSteps<=0){
        IsSortHomingOffset = false;
        IsSortHoming=false;
        SortComplete=true;
        homingSteps=0;
      }
    }
  }
}

void stepFeedMotor(){
    digitalWrite(FEED_ENABLE, LOW);
    digitalWrite(FEED_STEPPIN, HIGH);
    delayMicroseconds(3);  //pulse width
    digitalWrite(FEED_STEPPIN, LOW);
    delayMicroseconds(feedDelayMS); //controls motor speed
}

//Feed launch ramp (fork): a short launch ramp, then flat cruise through the
//blind steps AND the homing seek - the sensor edge is taken at cruise.
void setAccFeedDelay(){
    int done = feedMicroSteps - FeedSteps;
    if(done < feedLaunchSteps && feedTrapDelay > (unsigned int)feedMotorSpeed){
      feedTrapN++;
      feedTrapDelay -= (2UL * feedTrapDelay) / (4UL * feedTrapN + 1);
      if(feedTrapDelay < (unsigned int)feedMotorSpeed){
        feedTrapDelay = feedMotorSpeed;
      }
      feedDelayMS = feedTrapDelay;
      return;
    }
    feedDelayMS = feedMotorSpeed;
}

void setSorterMotorSpeed(int speed) {
  sortMotorSpeed = setSpeedConversion(speed);
}
void setFeedMotorSpeed(int speed) {
  feedMotorSpeed = setSpeedConversion(speed);
}

int setSpeedConversion(int speed) {
  if (speed < 1 || speed > 100) {
    return 500;
  }
  return 1060 - ((int)(((double)(speed - 1) / 99) * (1000 - 60)) + 60);
}

void MotorStandByCheck(){
  if(SortInProgress || IsFeeding)
    return;
  if(autoMotorStandbyTimeout==0)
    return;
  theTime = millis();
  if(theTime - timeSinceLastMotorMove > (unsigned long)(autoMotorStandbyTimeout*1000) ) {
     digitalWrite(FEED_ENABLE, HIGH);
     digitalWrite(SORT_ENABLE, HIGH);
  }
}

// [PICO] the ring: level scales the color mix; same 0-255 knob as the
// stock PWM, driven by PIO (never disturbs step timing)
void ringShow(){
  uint8_t l = cameraLEDLevel;
  uint32_t c = ring.Color((uint16_t)ringR * l / 255,
                          (uint16_t)ringG * l / 255,
                          (uint16_t)ringB * l / 255);
  for (int i = 0; i < LED_RING_COUNT; i++) ring.setPixelColor(i, c);
  ring.show();
}

void adjustCameraLED(int level)
 {
   level = level > 255 ? 255 : level;
   level = level < 0 ? 0 : level;
   cameraLEDLevel = level;
   ringShow();
 }

void adjustFanLevel(int level)
{
  fanPercentConversion = level * 2.55;
  level = fanPercentConversion;
  analogWrite(CASEFAN_PWM, level);
 }

int js=0;
int jogSteps = 25 * SORT_MICROSTEPS;
void jogSorter(){
    for(js=0;js<jogSteps;js++){
      stepSortMotor(false);
    }
}

//---------------- stallguard jam detection ----------------
void sgApply(){
  feedmotorUART.SGTHRS(feedSgThrs);
  sortmotorUART.SGTHRS(sortSgThrs);
  feedmotorUART.TCOOLTHRS(sgTcoolThrs);
  sortmotorUART.TCOOLTHRS(sgTcoolThrs);
}
void sgProbeReset(){
  sgProbeMin = 1023; sgProbeSum = 0; sgProbeN = 0;
}
void sgProbeSample(TMC2209Stepper &drv){
  unsigned int r = drv.SG_RESULT();
  if(r < sgProbeMin){ sgProbeMin = r; }
  sgProbeSum += r; sgProbeN++;
}
void sgProbeReport(const char *which){
  if(!sgProbe || sgProbeN == 0){ return; }
  host.print(F("sg:")); host.print(which);
  host.print(F(":min=")); host.print(sgProbeMin);
  host.print(F(",avg=")); host.print(sgProbeSum / sgProbeN);
  host.print(F(",n=")); host.print(sgProbeN);
  host.print(F("\n"));
  sgProbeReset();
}
//Returns true when the move was aborted for a stall. cruising = caller is
//at flat cruise speed (the only phase StallGuard reads cleanly).
bool sgCheckFeed(bool cruising){
  if(!sgEnabled || !cruising){ sgFeedHits = 0; return false; }
  sgFeedCruise++;
  if(sgFeedCruise <= SG_ARM_STEPS){ return false; }     //let SG settle
  if(sgProbe && (sgFeedCruise % 32) == 0){ sgProbeSample(feedmotorUART); }
  if(digitalRead(FEED_DIAG_PIN) == HIGH){
    if(++sgFeedHits >= SG_TRIP_HITS){ feedStallDetected(); return true; }
  }else{
    sgFeedHits = 0;
  }
  return false;
}
bool sgCheckSort(bool cruising){
  if(!sgEnabled || !cruising){ sgSortHits = 0; return false; }
  sgSortCruise++;
  if(sgSortCruise <= SG_ARM_STEPS){ return false; }
  if(sgProbe && (sgSortCruise % 32) == 0){ sgProbeSample(sortmotorUART); }
  if(digitalRead(SORT_DIAG_PIN) == HIGH){
    if(++sgSortHits >= SG_TRIP_HITS){ sortStallDetected(); return true; }
  }else{
    sgSortHits = 0;
  }
  return false;
}
//A feed stall ends the cycle the way an overtravel does: flags cleared,
//IsFeedError set so onFeedComplete stays quiet, and the error line is the
//reply the app turns into a JAM (home + retry).
void feedStallDetected(){
  feedStalls++;
  sgFeedHits = 0;
  FeedScheduled=false;
  FeedCycleComplete=true;
  IsFeeding=false;
  IsFeedHoming=false;
  IsFeedHomingOffset=false;
  IsFeedError=true;
  FeedCycleInProgress=false;
  sgProbeReport("feed");
  host.println(F("error:feed stall detected"));
}
//A sort stall leaves the arm at an unknown position: abort like a failed
//homing does, and cancel any feed queued behind the sort — with the arm
//lost, that feed would drop brass into the wrong slot.
void sortStallDetected(){
  sortStalls++;
  sgSortHits = 0;
  sortStepsToNextPosition = 0;
  FeedScheduled=false;
  IsFeeding=false;
  FeedCycleInProgress=false;
  FeedCycleComplete=false;
  IsTestCycle=false;
  IsSorting=false;
  SortComplete=true;
  IsSortHoming=false;
  IsSortHomingOffset=false;
  homingSteps=0;
  sortHomeStage=0;
  sgProbeReport("sort");
  host.println(F("error:sort stall detected"));
}

//---------------- motor power as a state [PICO] ----------------
// The drivers' UART only answers while the 12V rail is up. Everything
// that must be re-sent after a power cycle lives in applyDriverConfig().
bool driversPresent(){
  return feedmotorUART.test_connection() == 0 && sortmotorUART.test_connection() == 0;
}
void applyDriverConfig(){
  feedmotorUART.begin();
  feedmotorUART.toff(4);
  sortmotorUART.begin();
  sortmotorUART.toff(4);
  feedmotorUART.rms_current(feedCurrent);
  feedmotorUART.microsteps(FEED_MICROSTEPS);
  feedmotorUART.pwm_autoscale(true);
  feedmotorUART.en_spreadCycle(false);   // stealthChop (StallGuard4 needs it)
  feedmotorUART.intpol(true);
  feedmotorUART.ihold(2);
  sortmotorUART.rms_current(sortCurrent);
  sortmotorUART.microsteps(SORT_MICROSTEPS);
  sortmotorUART.pwm_autoscale(true);
  sortmotorUART.en_spreadCycle(false);
  sortmotorUART.intpol(true);
  sortmotorUART.ihold(2);
  sgApply();
  digitalWrite(FEED_ENABLE, LOW);
  digitalWrite(SORT_ENABLE, LOW);
}
//polled only while nothing moves — a UART probe mid-move would stall the
//step train
void checkMotorPower(){
  if(SortInProgress || IsFeeding || IsFeedHoming || IsSortHoming) return;
  if(millis() - lastPowerPoll < POWER_POLL_MS) return;
  lastPowerPoll = millis();
  bool now = driversPresent();
  if(now == motorPower) return;
  motorPower = now;
  if(motorPower){
    applyDriverConfig();
    host.println(F("info:motor power on"));
    IsFeedHoming=true;            //positions are unknown after a power cycle:
    IsSortHoming=true;            //re-home exactly like a boot
  }else{
    host.println(F("info:motor power off"));
  }
}
//a motion command with the 12V off would walk the step budget into a
//homing error a long time later — refuse it now, with the reason
bool requireMotorPower(){
  if(motorPower) return true;
  host.println(F("error:motor power off"));
  return false;
}

//---------------- phase 2 seam ----------------
// The gated-wheel carousel (one wheel, stations around the rim, per-station
// servo gates on a PCA9685, a verdict FIFO riding with the pockets) is a
// different main loop from the stock feed+sort pair above. It will live
// behind a MachineMode switch here, sharing the protocol layer, the driver
// layer, StallGuard, the ring, and the power-state logic. Nothing in phase 1
// depends on it.
