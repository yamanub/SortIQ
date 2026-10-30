"""Carousel servo gates: a PCA9685 on the Pi's own I2C bus (addr 0x40)
drives eight rack-and-pinion SLIDING DOORS at carousel stations 2-9.

Each door is the case's floor: the case rests on it by gravity, and its
weight is ORTHOGONAL to the door's travel — nothing pushes a door open,
and the wheel feels only sliding friction (the spring-cam design this
replaced stalled the wheel once 5-6 cases' grip tension accumulated).
Gates are DRIVEN-RETURN: open = drive the servo and HOLD it powered;
close = drive to the closed pulse, settle, cut the pulses — the geared
linkage holds position at zero current. Because "no pulses" is NOT a
known position, every startup, run start, and recovery path runs
close_all() before the wheel moves — gate state is commanded, never
assumed. Closed = LOW pulse on this linkage (the gear reverses the
old cam-era sense).

No smbus dependency: raw /dev/i2c-N writes via ioctl(I2C_SLAVE). The
chip's auto-increment mode lets one 5-byte write set a whole channel.
"""

import os
import time

try:
    import fcntl
except ImportError:                      # non-POSIX (the trainer PC)
    fcntl = None

I2C_SLAVE = 0x0703

# register map
MODE1, MODE2, PRESCALE = 0x00, 0x01, 0xFE
LED0_ON_L = 0x06
ALL_LED_ON_L = 0xFA

N_GATES = 8
DEFAULT_CLOSED_US = 900     # slider fully in — the door is the floor
DEFAULT_OPEN_US = 2150      # slider clear of the hole (rack-and-pinion doors)


class Gates:
    """One PCA9685 as eight gate channels. Raises OSError if the bus or
    chip is absent — callers probe with Gates.available() first."""

    def __init__(self, bus=1, addr=0x40, pulses=None):
        if fcntl is None:
            raise OSError("no I2C on this platform")
        self.fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, addr)
        # per-gate [closed_us, open_us]; index = channel = station - 2
        self.pulses = [list(p) for p in (pulses or [])][:N_GATES]
        while len(self.pulses) < N_GATES:
            self.pulses.append([DEFAULT_CLOSED_US, DEFAULT_OPEN_US])
        self.init_chip()

    # -- low level ---------------------------------------------------------
    def _w(self, reg, *vals):
        os.write(self.fd, bytes([reg, *vals]))

    def init_chip(self):
        """Idempotent: 50 Hz servo frame, auto-increment, totem outputs.
        Re-run before every action so a power-cycled chip is always
        reconfigured (its registers reset; ours don't)."""
        self._w(MODE1, 0x30)             # SLEEP + AI (prescale needs sleep)
        self._w(PRESCALE, 121)           # 25MHz/(4096*50Hz)-1
        self._w(MODE1, 0x20)             # wake, AI stays on
        time.sleep(0.001)                # oscillator settle
        self._w(MODE2, 0x04)             # totem-pole outputs

    def _set(self, ch, us):
        base = LED0_ON_L + 4 * ch
        if us <= 0:                      # full-off bit: no pulses at all
            self._w(base, 0, 0, 0, 0x10)
            return
        off = int(us * 4096 / 20000)     # counts of the 20 ms frame
        self._w(base, 0, 0, off & 0xFF, off >> 8)

    # -- gate verbs (channels 0-7 = stations 2-9) --------------------------
    def open(self, channels):
        """Drive the given gates open and HOLD them (powered)."""
        for ch in channels:
            self._set(ch, self.pulses[ch][1])

    def close(self, channels, settle_s=0.25, release=True):
        """Drive the given gates closed; after the travel settle, cut the
        pulses so the spring + friction hold them at zero current."""
        for ch in channels:
            self._set(ch, self.pulses[ch][0])
        if release:
            time.sleep(settle_s)
            for ch in channels:
                self._set(ch, 0)

    def close_all(self, settle_s=0.4):
        """The RITUAL: commanded-closed everything, then release. Runs at
        startup, run start, fault recovery — before any wheel motion that
        follows an unknown gate state."""
        self.init_chip()
        self.close(list(range(N_GATES)), settle_s=settle_s)

    def all_off(self):
        """Everything limp immediately (no drive-closed first) — the
        emergency stop for the servo rail, not the tidy path."""
        self._w(ALL_LED_ON_L, 0, 0, 0, 0x10)

    def release(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    # -- probing -----------------------------------------------------------
    @staticmethod
    def available(bus=1, addr=0x40):
        if fcntl is None or not os.path.exists(f"/dev/i2c-{bus}"):
            return False
        try:
            fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        except OSError:
            return False
        try:
            fcntl.ioctl(fd, I2C_SLAVE, addr)
            os.write(fd, bytes([MODE1]))     # address ACK is the probe
            return True
        except OSError:
            return False
        finally:
            os.close(fd)
