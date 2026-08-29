"""Bridge to the existing CS7.2 (sjseth AI-Case-Sorter) Arduino-Uno firmware.

Lets the SortIQ application drive the *unmodified* CS7.2 board over USB
serial. The app speaks SortIQ's controller protocol; this module
translates to/from the CS7.2 firmware's line protocol, so nothing on the
board changes (Phase 1 of the hardware bring-up).

    SortIQ (app) -> here : LIGHT:*   SORT:<bin>   HOME   PFEED  PSLOT:<bin>
    here -> SortIQ        : SEATED    DONE         JAM
    here -> CS7.2 (Uno)        : <slot>    homefeeder   homesorter   xf:<slot>
                                 pf        ps:<slot>    (SS2 fork only)
    CS7.2 (Uno) -> here        : Ready     done         error:feed overtravel detected

Key impedance mismatch: one CS7.2 `done` means BOTH "the sort you commanded
finished" AND "the next case is now seated at the camera" — its feed is gated
on the case proximity sensor, so `done` implies a real case is in the nest.
Each `done` therefore fans out to DONE + SEATED for the app's loop.

The link is 9600 baud (the stock firmware's `Serial.begin(9600)`), and the
Uno auto-resets when the port opens, printing `Ready` after its bootloader —
so we wait for `Ready` before priming.

HARDWARE-VALIDATION TODO (only confirmable against the real machine):
  * SLOT_MAP: SortIQ bin ids -> physical CS7.2 chute numbers. Default is
    identity; set it to your machine on the first live run.
  * Prime/home timing: how long after boot or a HOME the board will accept a
    feed. Tunable via the timeouts below.
  * The camera->drop pipeline is handled inside the stock firmware (qPos1/qPos2
    shift register) and stock mechanics, so it is inherited here, not
    reimplemented — but eyeball a live run to confirm cases land in the
    intended bins before trusting the numbers.
  * [Verified on the real board] Setter names:
    `feedhomingoffset` / `sorthomingoffset` / `cameraledlevel` work as
    guessed; motor standby is `automotorstandbytimeout` (getconfig key
    `AutoMotorStandbyTimeout`), and the LED getconfig key is
    `CameraLEDLevel`. NOTE: the firmware answers "ok" to ANY key:value —
    including unknown commands — so verify setters by value change in
    getconfig, never by the ack.

From the official docs (Application Guide v1.1.53 + CS7.2 MPU v1.4 manual):
  * Feed Cycle Steps: the guide says 70 WITH a feed homing switch, 80 without
    (blind steps before watching the homing signal). Our default is 60 — on
    first connect, Read from board and adopt the board's values before
    pushing anything.
  * Notification Delay is the firmware's own anti-blur settle (delay before
    `done` after a feed, +20ms steps). Tune it on real brass first; then
    COLLECT_SETTLE_S in webui/server.py can likely shrink toward zero.
  * Sort Homing Offset recommended range is 0-8; after changing Feed Homing
    Offset, test-feed ~6x to confirm the homing sensor still trips at rest.
  * Camera light: board LIGHT+ is PWM (pin D11) *through a 2k trimpot* — if
    the slider maxes out dim, adjust the trimpot or fit the bypass jumper.
  * Cameras: v1 -> 640x480, v2 -> 1280x720 recommended resolutions.
  * The board's USB-C carries an onboard hub (Uno serial + USB-A camera in
    one cable) — a single cable to the Pi is a supported alternative to
    plugging the camera into the Pi directly.
  * The Windows app changed its serial handling in v1.1.51 ("Use Legacy
    Comm" toggle exists) — if we see drops/timeouts on real hardware, the
    pacing of commands may need to match the newer behavior.
"""
import collections
import queue
import threading
import time

from .transport import Transport

FW_BAUD = 9600
PARK_SLOT = 0            # CS7.2 slot used to seat a case with no real sort (home)


class Cs72Transport(Transport):
    """Adapt SortIQ's controller protocol onto the CS7.2 firmware.

    `link` is anything with write(str)/readline(timeout)->str|None/close():
    SerialCs72Link for the real board, FakeCs72Link for off-hardware tests.
    `slot_map` maps SortIQ bin ids to CS7.2 chute numbers (identity if
    omitted). Priming seats the first case so the app's loop gets its opening
    SEATED without special-casing startup.
    """

    def __init__(self, link, slot_map=None, prime=True,
                 ready_timeout=8.0, feed_timeout=15.0):
        self.link = link
        self.slot_map = dict(slot_map or {})
        self.feed_timeout = feed_timeout
        self._app_lines = collections.deque()   # translated, waiting for the app
        self._sort_pending = False               # a SORT is awaiting its DONE
        self._need_prime = prime
        self._ready_timeout = ready_timeout
        self._expect_stop_done = False           # FORCE_FEED's stop-ack pending

    # ---- app -> firmware --------------------------------------------------
    def send(self, line):
        line = line.strip()
        if line.startswith("LIGHT"):
            return   # LED brightness is a setting (cameraledlevel), not per-shot
        if line.startswith("SORT:"):
            bin_id = int(line.split(":", 1)[1])
            self._sort_pending = True
            self.link.write(f"{self._slot(bin_id)}\n")
        elif line == "PFEED":
            # SS2 pipelined cycle trigger: run the mechanics NOW using the
            # arm target queued by the previous PSLOT — sent right after
            # the photo, so the wheel moves while the app classifies. Its
            # done surfaces as DONE + SEATED exactly like a bare sort.
            self._sort_pending = True
            self.link.write("pf\n")
        elif line.startswith("PSLOT:"):
            # the classification result, sent mid-cycle: waits in the
            # Uno's hardware serial buffer and queues as the NEXT feed's
            # arm target. The firmware replies nothing; a lost/never-sent
            # PSLOT makes the next pf answer an error line (-> JAM).
            bin_id = int(line.split(":", 1)[1])
            self.link.write(f"ps:{self._slot(bin_id)}\n")
        elif line.startswith("FSORT:"):
            # forced sort: a bare-N sort answered "waiting for brass", which
            # means the firmware NEVER dropped the nest case (the drop rides
            # the feed cycle, and the gate was dry). Cancel the wait and
            # reissue as xf:N — the firmware sorts the nest case to N and
            # force-feeds the next tube case in one atomic command, so tail
            # cases land in their REAL bins (field bug: a HORNADY in the
            # MONARCH tray at end-of-hopper under the old park-slot flush).
            bin_id = int(line.split(":", 1)[1])
            self._expect_stop_done = True
            self._sort_pending = True
            self.link.write("stop\n")
            self.link.write(f"xf:{self._slot(bin_id)}\n")
        elif line == "CANCEL":
            # step 1 of the end-of-brass flush: kill the firmware's pending
            # prox-gated feed with a bare stop, whose done-ack is swallowed.
            # CRITICAL RACE this exists for: if brass landed on the sensor
            # just before the stop, the waiting feed FIRES first and its
            # real done arrives ahead of the stop's ack. One done is
            # swallowed either way; a second one surfaces as DONE+SEATED —
            # the sort actually completed, so the run loop must NOT flush
            # (the queue is untouched by stop) and simply resumes sorting.
            # Silence after CANCEL = the wait died dry: flush for real.
            self._expect_stop_done = True
            self.link.write("stop\n")
        elif line.startswith("FLUSH:"):
            # one iteration of the end-of-brass flush loop (issued only
            # after CANCEL answered with silence): the drop-riding sort for
            # the case photographed at the camera, as a forced feed.
            #   sortto:prev  moves the arm to `prev` exactly like a bare
            #                sort would have (and collapses qPos1/qPos2,
            #                killing the stale queue skew) — the case at
            #                the drop port falls into `prev`, its true slot
            #   xf:last      zero arm move + forced feed executes the drop
            # Its done surfaces as DONE + SEATED, so the run loop counts
            # the flushed case like any sort and then PHOTOGRAPHS what the
            # feed walked into the camera: the machine takes two feeds to
            # move a case from the prox sensor to the camera (bench-measured),
            # so end-of-brass strands a case the run never
            # saw. The loop keeps flushing each newly photographed case to
            # its own classified slot, and closes with FEED (xf:0) when
            # the camera comes up empty — every straggler lands in its
            # TRUE bin and the wheel ends empty. One checked feed per
            # command, so a jam stops the sequence before anything drops
            # blind. Proven against the firmware simulator
            # (tools/cs72_flush_selftest) before ever touching brass —
            # see PRs #50/#52/#53 for why that bar exists.
            prev, last = (int(x) for x in line.split(":")[1:3])
            self._sort_pending = True
            self.link.write(f"sortto:{self._slot(prev)}\n")
            self.link.write(f"xf:{self._slot(last)}\n")
        elif line == "FORCE_FEED":
            # empty-hopper flush: the firmware is sitting in "waiting for
            # brass" — cancel that wait (its `done` ack is swallowed in
            # _translate) and force-feed, which advances the cases already
            # past the proximity sensor into the nest one at a time
            self._expect_stop_done = True
            self._sort_pending = False
            self.link.write("stop\n")
            self.link.write(f"xf:{PARK_SLOT}\n")
        elif line == "FEED":
            # plain forced feed with the firmware idle (no wait to cancel)
            self._sort_pending = False
            self.link.write(f"xf:{PARK_SLOT}\n")
        elif line == "HOME":
            self.link.write("homefeeder\n")
            self.link.write("homesorter\n")
            self._sort_pending = False
            self._need_prime = True              # re-seat a case after homing
        # STATUS / anything else: nothing the app relies on

    # ---- firmware -> app --------------------------------------------------
    def readline(self, timeout=None):
        if self._app_lines:
            return self._app_lines.popleft()
        if self._need_prime:
            self._prime()
            if self._app_lines:
                return self._app_lines.popleft()

        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else max(deadline - time.monotonic(), 0)
            raw = self.link.readline(timeout=remaining)
            if raw is None:
                return None
            for app_line in self._translate(raw):
                self._app_lines.append(app_line)
            if self._app_lines:
                return self._app_lines.popleft()
            if deadline is not None and time.monotonic() >= deadline:
                return None

    def close(self):
        try:
            self.link.write("stop\n")
        except Exception:
            pass
        self.link.close()

    # ---- translation ------------------------------------------------------
    def _translate(self, raw):
        """Raw CS7.2 line -> zero or more SortIQ lines."""
        raw = raw.strip()
        if raw == "done":
            if self._expect_stop_done:           # ack of FORCE_FEED's stop,
                self._expect_stop_done = False   # not a feed completing
                return []
            out = []
            if self._sort_pending:
                out.append("DONE")               # the commanded sort finished
                self._sort_pending = False
            out.append("SEATED")                 # ...and the next case is seated
            return out
        if raw == "waiting for brass":
            # proximity gate unsatisfied — printed ~1/s while the hopper is
            # dry. The run loop counts these to detect end-of-brass and to
            # flush the cases already sitting past the sensor.
            return ["WAITING"]
        if raw.startswith("fault:"):             # firmware fault stop: the
            self._sort_pending = False           # machine refuses cycles until
            return ["FAULT:" + raw[6:]]          # the operator clears it
        if raw.startswith("error"):              # e.g. "error:feed overtravel detected"
            self._sort_pending = False
            return ["JAM"]
        return []                                # Ready / ok / version: swallow

    def _slot(self, bin_id):
        return self.slot_map.get(bin_id, bin_id)

    def _prime(self):
        """Seat the first case so the app gets its opening SEATED.

        Skip past the boot banner to `Ready`, then force-feed one case; its
        `done` (no sort pending) translates to a single SEATED. Best-effort:
        if the board is slow to boot/home, the app's normal readline timeout
        will retry via HOME, so we never hang here.
        """
        self._need_prime = False
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            raw = self.link.readline(timeout=max(deadline - time.monotonic(), 0))
            if raw is None:
                break
            if raw.strip() == "Ready":
                break
        self._sort_pending = False               # the prime feed is not a real sort
        self.link.write(f"xf:{PARK_SLOT}\n")


def cancel_wait(transport):
    """First move of the end-of-brass flush, PROVEN against the firmware
    simulator (tools/cs72_flush_selftest.py drives THIS function over the
    ported firmware) before ever running on brass — the bar PRs #50/#52/#53
    established after two field misplacements.

    Called when a bare sort answered "waiting for brass" repeatedly: sends
    CANCEL to kill the pending prox-gated feed, then listens long enough to
    catch the race where brass landed just before the stop — the waiting
    feed fires FIRST and its done surfaces here as DONE. The stop never
    touches the firmware's queue, so in that case the sort completed for
    real and the run simply resumes.

    Returns "resumed" (feed won the race — count the case, keep sorting),
    "clean" (the wait died dry — safe to start the flush loop), or "jam"."""
    transport.send("CANCEL")
    deadline = time.monotonic() + 3.0         # outlasts one full feed cycle
    while time.monotonic() < deadline:
        line = transport.readline(timeout=max(deadline - time.monotonic(), 0.1))
        if line == "DONE":
            return "resumed"
        if line == "JAM":
            return "jam"
        if line is None:                       # silence: the wait died dry
            return "clean"                     # (stray WAITINGs are ignored)
    return "clean"


def read_feed_stats(transport, timeout=2.5):
    """Poll the firmware fork's feedstats telemetry over the transport's
    raw link. Must run while the caller still OWNS the link: handing the
    port back DTR-resets the Uno and wipes the RAM counters. Returns the
    parsed dict ({"LastHomingSteps","MaxHomingSteps","FeedCycles"}) or
    None on stock firmware / timeout — callers treat it as optional."""
    import json
    try:
        transport.link.write("feedstats\n")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = transport.link.readline(
                timeout=max(deadline - time.monotonic(), 0.1))
            if raw is None:
                return None
            raw = raw.strip()
            if raw.startswith("{") and "FeedCycles" in raw:
                return json.loads(raw)
    except Exception:
        pass
    return None


class SerialCs72Link:
    """Real CS7.2 board over USB serial (pyserial), line-buffered."""

    def __init__(self, port, baud=FW_BAUD):
        import serial                            # lazy: tests need no pyserial
        self.ser = serial.Serial(port, baud, timeout=0.1)

    def write(self, s):
        self.ser.write(s.encode())

    def readline(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        buf = b""
        while True:
            buf += self.ser.readline()           # b"" after 0.1 s of silence
            if buf.endswith(b"\n"):
                return buf.decode(errors="replace").strip()
            if deadline is not None and time.monotonic() >= deadline:
                return None

    def close(self):
        self.ser.close()


class RawCs72Console:
    """send()/readline() over a CS7.2 link with raw firmware lines — for the
    Machine bring-up console, which speaks the board's native protocol."""

    def __init__(self, link):
        self.link = link

    def send(self, line):
        self.link.write(line.rstrip("\n") + "\n")

    def readline(self, timeout=None):
        return self.link.readline(timeout=timeout)

    def close(self):
        self.link.close()


class FakeCs72Link:
    """In-process stand-in for the CS7.2 firmware, for testing with no board.
    Reproduces the line behavior we depend on:

      * prints `Ready` on construction (post-bootloader banner)
      * any feed command (`xf:<n>` or a bare `<n>`) -> `done` (or a jam if one
        was armed), after `feed_delay` seconds
      * `homefeeder` / `homesorter`, `sortto:<n>`, `key:value` setters -> `ok`
      * `getconfig` -> the config JSON on one line (settings update from setters)
      * `ping` -> ` ok`, `version` -> firmware string

    It is intentionally minimal — enough to exercise the protocol, not physics.
    """
    FW_VERSION = "7.2.250925.6.1"
    # firmware defaults; setters below mutate these and getconfig echoes them
    # mirrors a stock CS7.2 board's getconfig (read from real hardware)
    DEFAULT_CONFIG = {"FeedMotorCurrent": 1000, "FeedMotorSpeed": 94,
                      "FeedCycleSteps": 60, "SortMotorCurrent": 1000,
                      "SortMotorSpeed": 94, "SortSteps": 20,
                      "NotificationDelay": 120, "SlotDropDelay": 300,
                      "FeedHomingOffset": 5, "SortHomingOffset": 0,
                      "AutoMotorStandbyTimeout": 0, "CameraLEDLevel": 200,
                      "AirDropEnabled": 0, "AirDropPreDelay": 30,
                      "AirDropSignalTime": 100, "AirDropPostDelay": 100,
                      # SortIQ fork keys (7.2.250925.6.1-SS1)
                      "SortAccelFactor": 1200, "SortHomeBackoff": 160,
                      "SortHomeSlowDelay": 1400, "FeedLaunchSteps": 48,
                      "FeedDecelOverOffset": 1, "MaxSlots": 12}
    # setter command -> getconfig key (all verified against the real firmware,
    # which also answers "ok" to unknown commands — acks alone prove nothing)
    SETTERS = {"feedspeed": "FeedMotorSpeed", "feedsteps": "FeedCycleSteps",
               "sortspeed": "SortMotorSpeed", "sortsteps": "SortSteps",
               "feedmotorcurrent": "FeedMotorCurrent",
               "sortmotorcurrent": "SortMotorCurrent",
               "notificationdelay": "NotificationDelay",
               "slotdropdelay": "SlotDropDelay",
               "feedhomingoffset": "FeedHomingOffset",
               "sorthomingoffset": "SortHomingOffset",
               "automotorstandbytimeout": "AutoMotorStandbyTimeout",
               "cameraledlevel": "CameraLEDLevel",
               "airdropenabled": "AirDropEnabled",
               "airdroppredelay": "AirDropPreDelay",
               "airdropdsignalduration": "AirDropSignalTime",  # sic, fw typo
               "airdroppostdelay": "AirDropPostDelay",
               "sortaccel": "SortAccelFactor",
               "sorthomebackoff": "SortHomeBackoff",
               "sorthomeslow": "SortHomeSlowDelay",
               "feedlaunch": "FeedLaunchSteps",
               "feeddecel": "FeedDecelOverOffset"}

    def __init__(self, feed_delay=0.0):
        self.feed_delay = feed_delay
        self._out = queue.Queue()
        self._jam_next = False
        self._lock = threading.Lock()
        self.config = dict(self.DEFAULT_CONFIG)
        self._out.put("Ready")

    def arm_jam(self):
        """Make the next feed report a jam instead of `done`."""
        with self._lock:
            self._jam_next = True

    def write(self, s):
        s = s.strip()
        if not s:
            return
        if s == "getconfig":
            import json
            self._out.put(json.dumps(self.config))
            return
        if s.startswith("home"):
            self._out.put("ok")
            return
        if s == "ping":
            self._out.put(" ok")
            return
        if s == "version":
            self._out.put(self.FW_VERSION)
            return
        if s == "stop":
            return
        if s == "feedstats":             # fork telemetry: quiet fake wheel
            import json
            self._out.put(json.dumps({"LastHomingSteps": 0,
                                      "MaxHomingSteps": 0, "FeedCycles": 0}))
            return
        if ":" in s and not s.startswith("xf:"):
            key, _, val = s.partition(":")
            cfg_key = self.SETTERS.get(key)
            if cfg_key is not None:
                try:
                    self.config[cfg_key] = int(float(val))
                except ValueError:
                    pass
            self._out.put("ok")            # sortto:/setters/etc all ack
            return
        # a feed cycle: bare "<n>" or "xf:<n>"
        if s.startswith("xf:") or s.split(":", 1)[0].lstrip("-").isdigit():
            with self._lock:
                jam, self._jam_next = self._jam_next, False
            self._emit_after("error:feed overtravel detected" if jam else "done")

    def _emit_after(self, line):
        if self.feed_delay <= 0:
            self._out.put(line)
        else:
            threading.Timer(self.feed_delay, self._out.put, args=(line,)).start()

    def readline(self, timeout=None):
        try:
            return self._out.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        pass
