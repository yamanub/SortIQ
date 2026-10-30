"""Carousel gate choreography: the pocket ledger + gate timing, as a
TRANSPARENT wrapper around the run transport.

The run loop (webui/server.py RunManager._run) speaks a high-level
protocol — SEATED / PFEED / PSLOT:n / SORT:n / FLUSH:a:b / FEED / HOME —
through a transport object. On the 12-station carousel there is no sort
arm: routing is eight servo-gated cam stations the case RIDES to. This
wrapper observes that same conversation, keeps a ledger of which case is
riding which pocket, and opens the right gates during the right dwells.
The battle-tested loop is untouched; on machines without gate hardware
the wrapper is simply never installed.

Station map (pocket travel order):
    11 load(+case sensor) -> 12 camera -> 1 think buffer ->
    2..9 gated bins (gate channel = station-2) -> 10 catch-all -> 11 ...

Choreography rules (all bench-derived):
* SEATED = the wheel just came to rest. That is the ONLY safe gate
  window: rotate the ledger, open every gate whose case just arrived,
  wait the drop, drive them closed, release — and only then hand SEATED
  to the loop (which captures, then commands the next index).
* SEATED is worked the moment it ARRIVES, on a listener thread that owns
  the link's reads — not when the loop gets around to reading it. In the
  pipelined cycle the wheel seats mid-inference, and working SEATED after
  inference serialized every drop dwell behind the model (~0.35 s on each
  dropping cycle, field-measured). DONE passes straight through; SEATED is
  released to the loop only after its dwell (the contract above holds);
  the dwell never counts against a caller's readline timeout; and every
  wheel-moving command waits out an in-flight dwell plus the close stroke.
  Any direct .link access stops the listener first (raw link readers).
* A slot command (PSLOT/SORT/FLUSH) belongs to the case most recently
  photographed — it may already be riding away from the camera when the
  verdict lands, so verdicts attach to the tracked record, not station 12.
  That record is marked at PHOTO time (PFEED/FLUSH/SORT go out right after
  the photo): the live SEATED can rotate the photographed case off the
  camera before its PSLOT arrives, and marking it at SEATED would hand
  case N's verdict to case N+1.
* A record that leaves the camera with no verdict was an empty pocket
  (the loop only sends slots for present cases) — deleted.
* Station 10 is an OPEN slot — no cam, no gate. Any case still riding
  when it reaches 10 physically falls into the catch-all bin; NOTHING
  ever laps the wheel back to the camera. That is the safety floor: a
  failed gate drop or a post-HOME orphan ends in the catch-all (a safe
  reject), never a mis-sort and never an untracked rider. The ledger
  deletes records at station 10, and a TARGETED record arriving there
  is logged as a missed gate.
* HOME moves the wheel in ways the ledger cannot track (the grid home
  parks a pitch ahead). The ledger is CLEARED; physical riders resolve
  at the catch-all within one lap.
* Gates are driven-return, spring-held: every recovery path runs the
  close-all ritual before wheel motion (springs cannot pull cams back).
"""

import queue
import threading
import time

from sorter.gates import Gates, N_GATES

STATIONS = 12
CAMERA_ST = 12
LOAD_ST = 11
CATCHALL_ST = 10           # open slot, no gate: everything left falls here
FIRST_GATE_ST = 2          # station = FIRST_GATE_ST + channel
CLOSE_TRAVEL_S = 0.25      # door close stroke (gates.close's travel settle)
# commands that move the wheel: never sent under an open or closing door
MOTION_CMDS = {"PFEED", "FEED", "FLUSH", "SORT", "FSORT", "FORCE_FEED", "HOME"}


class _Failure:
    """A listener-thread exception, re-raised in the loop's readline."""
    def __init__(self, exc):
        self.exc = exc


def _next_station(s):
    return 1 if s == STATIONS else s + 1


class GateChoreo:
    """Wraps the run transport. Same send()/readline()/close() surface;
    .link is delegated (the run's cleanup homing uses it — gates get a
    safety close on that access, because homing moves the wheel)."""

    def __init__(self, inner, pulses=None, open_ms=250, clear_ms=50,
                 note=None, warmup=STATIONS, drop_ms=None):
        self.inner = inner
        self.gates = Gates(pulses=pulses)
        # DOOR TIMING, per door: a door must be FULLY open before a case can
        # clear it (bench: a 200 ms dwell closed doors mid-stroke and cases
        # rode on; 250 never missed). open_s = the servo's open stroke,
        # counted from when THAT door's open was commanded; clear_s = the
        # margin after it is fully open. A pre-opened door has usually done
        # most of its stroke under the index, so it waits only the remainder
        # + clear_s; a door opened at SEATED waits the whole stroke.
        # drop_ms (the old single dwell) is read as the open time.
        if drop_ms is not None:
            open_ms = drop_ms
        self.open_s = max(int(open_ms), 0) / 1000.0
        self.clear_s = max(int(clear_ms), 0) / 1000.0
        # END-OF-BRASS CLEAR PAUSE (set by the run loop for the clear): every
        # index waits this much longer before the next one — due doors stay
        # open through it, and the catch-all gets the same time
        self.pause_s = 0.0
        self.opened_at = {}          # gate channel -> monotonic open command
        self.note = note or (lambda m: None)
        # ledger: station -> record {"target": station|None, "seen": bool}
        self.ledger = {}
        self.cam_record = None       # the record last photographed
        self.dropped = 0
        self.missed = 0            # targeted cases that fell to catch-all
        self.orphaned = 0          # bin-bound riders a HOME sent to the
                                   # catch-all (a jam's cost, not a mis-sort)
        self.pre_fired = 0         # doors opened under the index (hidden)
        self.late_fired = 0        # doors opened at SEATED (dwell cost)
        self.rotations = 0
        # PRE-FIRE state: doors opened during the index (stroke hidden
        # under wheel motion) and doors driven closed whose pulse-cutoff
        # is deferred to the next event (release costs a sleep otherwise)
        self.pre_open = set()
        self.pending_release = set()
        # SAFETY (user-caught hazard): pre-firing a door while another case
        # transits its station drops the WRONG case (a WIN riding through
        # station 2 fell out door 1 when it pre-opened for a BLAZER). Guards:
        # pre-open ONLY an EMPTY target station (below); an occupied one
        # waits for SEATED; and a full suppression lap after HOME/cold-start
        # for untracked orphans. SEATED opens cannot collide (wheel stopped,
        # every station holds exactly its settled case).
        self._warmup = warmup
        self.suppress_until = warmup        # rotations before pre-fire arms
                                            # (0 = arm immediately; safe only
                                            # on a known-empty machine, e.g.
                                            # a single-case bench test)
        self.gates.close_all()       # the ritual: never move over unknowns
        # COLD START: a case may already sit in the load-port pocket before
        # the first index (the operator drops brass in, then starts the
        # run). Without this seed record, that first case reaches the
        # camera with no ledger entry, its verdict attaches to nothing,
        # and no gate ever arms.
        self.ledger[LOAD_ST] = {"target": None}
        self.note("choreo: gates close-all ritual done; ledger armed")
        # LIVE SEATED (see module doc): the listener owns inner.readline();
        # the loop reads lines from _q. _lock serializes ledger + gate work
        # between the listener and the loop's send(); it is never held
        # across a dwell or a blocking read.
        self._lock = threading.RLock()
        self._q = queue.Queue()
        self._stop = threading.Event()
        self._in_dwell = threading.Event()   # a SEATED is being worked
        self._doors_quiet_at = 0.0           # monotonic: last close stroke done
        self._failure = None
        # monotonic time the last SEATED arrived = the wheel came to rest
        # (notification delay 0). The run's camera counts its settle from
        # here, not from when the loop got the line — on a drop cycle the
        # line is held through the whole door dwell.
        self.seated_at = None
        self._listener = threading.Thread(target=self._listen, daemon=True,
                                          name="choreo-listener")
        self._listener.start()               # LAST: its first read primes

    # ------------------------------------------------------------------ io
    def send(self, cmd):
        c = cmd.split(":", 1)[0]
        if c in MOTION_CMDS:
            self._await_doors()                  # outside the lock: the
        with self._lock:                         # listener finishes a dwell
            if c == "PSLOT":
                self._attach(int(cmd.split(":")[1]))
            elif c == "SORT":                    # sequential: photo, verdict
                self._mark_photo()
                self._attach(int(cmd.split(":")[1]))
            elif c == "FLUSH":                   # FLUSH:prev:slot — a forced
                self._mark_photo()               # feed too: pre-fire it
                self._attach(int(cmd.split(":")[2]))
                self._pre_fire()
            elif c in ("PFEED", "FEED"):
                # an index is starting: open every door whose case arrives
                # at its target when the wheel stops — the ~200ms stroke
                # hides entirely under the wheel's motion (the user-authored
                # spec). PFEED goes out right after the photo: mark the
                # photographed case for its PSLOT. FEED (empty pocket) has
                # no verdict coming, so it marks nothing.
                if c == "PFEED":
                    self._mark_photo()
                self._pre_fire()
            elif c == "HOME":
                # homing moves the wheel off-ledger: close everything, forget
                # everything; riders resolve at the catch-all within one lap.
                # Those riders are cases the report already credited to a bin
                # (the model had ruled) and that now land UNMATCHED instead —
                # count and name them, or the report reads like a mis-sort.
                lost = sum(1 for r in self.ledger.values()
                           if FIRST_GATE_ST <= (r.get("target") or 0)
                           < FIRST_GATE_ST + N_GATES)
                self.orphaned += lost
                self.gates.close_all()
                self.ledger.clear()
                self.ledger[LOAD_ST] = {"target": None}   # cold-start seed again
                self.cam_record = None
                self.pre_open.clear()
                self.opened_at.clear()
                self.pending_release.clear()
                self.suppress_until = self.rotations + self._warmup   # orphan lap
                self.note(f"choreo: HOME -> gates closed, ledger cleared, "
                          f"pre-fire suppressed for one lap; {lost} case(s) "
                          f"still riding to a gate go to the catch-all "
                          f"(unmatched) instead — total {self.orphaned}")
            self.inner.send(cmd)

    def readline(self, timeout=None):
        """Next board line, in arrival order. SEATED surfaces only after
        its dwell, and the dwell never counts against `timeout` (it used to
        run after the line was read, so callers never budgeted for it)."""
        if self._failure is not None and self._q.empty():
            raise self._failure
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            wait = None if deadline is None else max(deadline - time.monotonic(), 0.0)
            try:
                item = self._q.get(timeout=wait)
            except queue.Empty:
                if self._in_dwell.is_set():      # a SEATED is owed: its dwell
                    deadline = time.monotonic() + 0.05   # is not our wait
                    continue
                if self._failure is not None:
                    raise self._failure
                return None
            if isinstance(item, _Failure):
                self._failure = item.exc
                raise item.exc
            return item

    def __getattr__(self, name):                 # .link and friends
        if name == "link":
            # raw link readers (the run's cleanup homing, read_feed_stats)
            # must own the link: stop the listener first. The wheel is then
            # about to move, so command-close whatever is still open.
            self._shutdown()
            try:
                with self._lock:
                    self.gates.close_all(settle_s=0.3)
                    self.opened_at.clear()
            except OSError as e:
                self.note(f"choreo: door close-all FAILED ({e})")
        return getattr(self.inner, name)

    def close(self):
        self._shutdown()
        try:
            with self._lock:
                self.gates.close_all()
                self.opened_at.clear()
                self.gates.release()
        except OSError as e:
            self.note(f"choreo: door close-all FAILED ({e})")
        self.inner.close()

    # ---------------------------------------------------------- listener
    def _listen(self):
        """Sole reader of the inner transport. Works SEATED on arrival and
        queues every line for the loop in arrival order; an exception (port
        died, I2C) is queued too and re-raised by the loop's readline."""
        try:
            while not self._stop.is_set():
                line = self.inner.readline(timeout=0.25)
                if line is None:
                    continue
                if line == "SEATED":
                    self.seated_at = time.monotonic()
                    self._in_dwell.set()
                    try:
                        self._on_seated()
                        self._q.put(line)        # released AFTER the dwell;
                    finally:                     # queued before clearing so a
                        self._in_dwell.clear()   # waiting reader never misses it
                    continue
                self._q.put(line)
        except Exception as e:
            self._q.put(_Failure(e))

    def _shutdown(self):
        """Stop the listener and wait for it (an in-flight dwell completes)."""
        self._stop.set()
        t = self._listener
        if t.is_alive() and t is not threading.current_thread():
            t.join(timeout=self.open_s + self.clear_s + self.pause_s + 1.5)

    def _await_doors(self, limit=5.0):
        """A wheel move never starts under a door that is open or mid-
        stroke: wait out an in-flight dwell, then the close stroke. (The
        photo between SEATED and the next index used to cover the stroke
        implicitly; the drain and recovery paths have no photo.)"""
        t_end = time.monotonic() + limit
        while self._in_dwell.is_set() and time.monotonic() < t_end:
            time.sleep(0.01)
        wait = self._doors_quiet_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _mark_photo(self):
        """The case at the camera is the one just photographed: its verdict
        attaches to THIS record, wherever the live rotation moves it."""
        self.cam_record = self.ledger.get(CAMERA_ST)

    # ---------------------------------------------------------- pre-fire
    def _due_next_chans(self):
        """Gate channels whose case arrives at its target on the NEXT
        completed index (the record now at station s-1 targeting s)."""
        chans = []
        for st, rec in self.ledger.items():
            ns = _next_station(st)
            if rec.get("target") == ns \
                    and FIRST_GATE_ST <= ns < FIRST_GATE_ST + N_GATES:
                chans.append(ns - FIRST_GATE_ST)
        return chans

    def _pre_fire(self):
        """Index starting: cut pulses on doors that finished closing last
        dwell, then open every door due at the coming stop — but ONLY when
        that door's station is EMPTY right now. If a case currently sits at
        the station it will TRANSIT the door's hole during this index, and
        opening early drops the WRONG case (field-caught: a WIN transiting
        station 2 fell through door 1 when it pre-opened for a BLAZER). An
        occupied station's door waits for SEATED, when rotation is complete
        and only the correct case is there — collision-proof by
        construction. No pre-fire at all during the post-HOME suppression
        lap (untracked orphans)."""
        if self.pending_release:
            self.gates.close(sorted(self.pending_release), release=True,
                             settle_s=0)     # travel already done: just cut
            self.pending_release.clear()
        if self.rotations < self.suppress_until:
            return                           # orphan window: fallback only
        now_chans = []
        for st, rec in self.ledger.items():
            ns = _next_station(st)
            if rec.get("target") == ns \
                    and FIRST_GATE_ST <= ns < FIRST_GATE_ST + N_GATES:
                ch = ns - FIRST_GATE_ST
                if ch in self.pre_open:
                    continue
                # target station occupied NOW = a case will transit its
                # hole this index -> never pre-open; it opens safely at
                # SEATED (via _on_seated's late path) after rotation
                if ns in self.ledger:
                    continue
                now_chans.append(ch)
        if now_chans:
            self.gates.open(now_chans)
            now = time.monotonic()
            for ch in now_chans:
                self.opened_at[ch] = now
            self.pre_open |= set(now_chans)
            self.pre_fired += len(now_chans)
            self.note(f"choreo: pre-opened door(s) "
                      f"{[c + 1 for c in sorted(now_chans)]} under the index")

    # ------------------------------------------------------------- ledger
    def _attach(self, slot):
        if self.cam_record is None:
            # verdict with no tracked case (post-HOME edge) — ignore; the
            # case re-identifies next lap
            return
        self.cam_record["target"] = FIRST_GATE_ST + int(slot)
        self.cam_record = None

    def _on_seated(self):
        """One completed index: rotate, then work the gates in the dwell.
        Runs on the listener thread; the dwell itself holds no lock. The
        dwell lasts until the most recently opened due door has finished
        its open stroke, plus the clear margin."""
        with self._lock:
            due, chans = self._rotate_and_open()
            if due:
                now = time.monotonic()
                open_done = max(self.opened_at.get(ch, now)
                                for ch in chans) + self.open_s
        if not due:
            if self.pause_s:
                time.sleep(self.pause_s)
            return
        wait = (max(open_done - time.monotonic(), 0.0) + self.clear_s
                + self.pause_s)
        time.sleep(wait)                             # fully open, case clear
        with self._lock:
            self._close_after_drop(due, chans, wait)

    def _rotate_and_open(self):
        self.rotations += 1
        moved = {}
        for st, rec in self.ledger.items():
            ns = _next_station(st)
            # leaving the camera without a verdict = empty pocket
            if st == CAMERA_ST and rec.get("target") is None \
                    and rec is not self.cam_record:
                continue
            # arriving at the open catch-all slot: the case PHYSICALLY
            # falls there — nothing ever rides past station 10. Three
            # flavors: TARGETED at 10 = a successful catch-all delivery
            # (slot 9 is a real destination — UNMATCHED lives there);
            # targeted elsewhere = a missed gate (safe reject, logged);
            # untargeted = a post-HOME orphan, silently resolved.
            if ns == CATCHALL_ST:
                t = rec.get("target")
                if t == CATCHALL_ST:
                    self.dropped += 1
                    self.note(f"choreo: catch-all delivery at station 10 "
                              f"(total {self.dropped})")
                elif t:
                    self.missed += 1
                    self.note(f"choreo: MISSED gate at station {t} — case "
                              f"fell to catch-all (miss #{self.missed})")
                continue
            moved[ns] = rec
        self.ledger = moved
        self.ledger[LOAD_ST] = {"target": None}      # potential new case
        # (cam_record is NOT re-pointed here any more: _mark_photo does it
        # at photo time — this SEATED can land before the PSLOT does)

        due = [st for st, rec in self.ledger.items()
               if rec.get("target") == st
               and FIRST_GATE_ST <= st < FIRST_GATE_ST + N_GATES]
        if not due:
            return due, []
        chans = [st - FIRST_GATE_ST for st in due]
        # PRE-FIRED doors are already fully open — the case is falling as
        # the wheel stops; only the settle margin remains. Any door that
        # was NOT pre-fired (first cycle, edge path) falls back to the
        # old open-and-wait — correctness never leans on the optimization.
        late = [ch for ch in chans if ch not in self.pre_open]
        if late:
            self.gates.open(late)
            now = time.monotonic()
            for ch in late:
                self.opened_at[ch] = now
            self.late_fired += len(late)
            self.note(f"choreo: late-opened door(s) "
                      f"{[c + 1 for c in sorted(late)]} (no pre-fire)")
        return due, chans

    def _close_after_drop(self, due, chans, waited_s=None):
        # HOLD-OPEN lookahead: a door whose NEXT arrival targets it too
        # skips the close entirely — same-bin streams get zero door motion.
        # (Door 1's next arrival is the case just photographed; if its
        # PSLOT hasn't landed yet the door closes and pre-fire reopens it.)
        keep = set(self._due_next_chans())
        to_close = [ch for ch in chans if ch not in keep]
        if to_close:
            self.gates.close(to_close, release=False)   # fire, no blocking;
            self.pending_release |= set(to_close)       # pulses cut at the
            self._doors_quiet_at = time.monotonic() + CLOSE_TRAVEL_S
        self.pre_open -= set(to_close)                  # next index start
        for ch in to_close:
            self.opened_at.pop(ch, None)
        self.pre_open |= (set(chans) & keep)
        for st in due:
            self.ledger.pop(st, None)
        self.dropped += len(due)
        held = sorted(c + 1 for c in (set(chans) & keep))
        self.note(f"choreo: dropped at station(s) {sorted(due)} "
                  f"(total {self.dropped})"
                  + (f" after {round(waited_s * 1000)} ms"
                     if waited_s is not None else "")
                  + (f" — door(s) {held} held open for the next case"
                     if held else ""))

    # -------------------------------------------------------------- hold
    def close_for_hold(self, limit=5.0):
        """The run is pausing (operator, tube jam, feeder jam): close every
        door, including one held open for the next case, and forget which
        were open. Pre-fire and the SEATED late path then reopen exactly
        what the next stop needs — a door the ledger still believed open
        would otherwise never be reopened and its case would miss. Never
        mid-dwell: a case may be falling through an open door."""
        t_end = time.monotonic() + limit
        while True:
            with self._lock:
                if not self._in_dwell.is_set() or time.monotonic() > t_end:
                    self.gates.close_all()
                    self.pre_open.clear()
                    self.opened_at.clear()
                    self.pending_release.clear()
                    self._doors_quiet_at = time.monotonic() + CLOSE_TRAVEL_S
                    self.note("choreo: doors closed for a pause")
                    return
            time.sleep(0.02)

    # ------------------------------------------------------------- drain
    def targeted(self):
        return sum(1 for r in self.ledger.values() if r.get("target"))

    def drain(self, max_feeds=STATIONS + 2):
        """End of run: cases can still be riding up to 8 stations past the
        camera. Force feeds until every targeted record has reached its
        gate (rotation + drops happen inside readline). The classic flush
        walked cases TO the camera; this walks them from the camera to
        their bins."""
        for i in range(max_feeds):
            if not self.targeted():
                break
            self.note(f"choreo drain: {self.targeted()} case(s) still "
                      f"riding, feed {i + 1}")
            self.send("FEED")                    # through the wrapper: the
                                                 # drain pre-fires too
            deadline = time.monotonic() + 12.0
            while time.monotonic() < deadline:
                line = self.readline(timeout=1.0)    # rotations + drops
                if line == "SEATED" or line is None:
                    break
                if line == "JAM" or (line and line.startswith("FAULT:")):
                    self.note("choreo drain: aborted (jam/fault) — "
                              "remaining riders left for the operator")
                    with self._lock:
                        self.gates.close_all()
                        self.opened_at.clear()
                    return
        with self._lock:
            self.gates.close_all()
            self.opened_at.clear()
        self.note(f"choreo drain complete: {self.dropped} total drops, "
                  f"{self.targeted()} unresolved")
