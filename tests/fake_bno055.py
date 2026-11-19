#!/usr/bin/env python3
"""A fake BNO055 on a pseudo-terminal, speaking the BNO055 UART protocol.

The host opens the pty slave (FakeBNO055.port) as if it were /dev/ttyAMA5.

Protocol (datasheet 4.7):
  read   host: AA 01 reg len        device: BB len data...  |  EE code
  write  host: AA 00 reg len data   device: EE 01 (write ok) |  EE code

The register file is page 0 of the chip: CHIP_ID 0xA0 at 0x00, OPR_MODE at
0x3D (CONFIG = 0x00 after power-up, NDOF = 0x0C), PWR_MODE 0x3E, PAGE_ID
0x07. Reads of the 46-byte data block 0x08..0x35 return a synthetic,
slowly changing sample; in CONFIG mode the fusion outputs (euler,
quaternion, linear acceleration, gravity) and the calibration byte read as
zero, which is what a BNO055 that reset itself looks like to the host.

Scripted misbehaviour (all optional, combinable):
  overrun_every=N, overrun_repeat=k   every Nth data read answers EE 07 (bus
                                      overrun), k times in a row
  rx_timeout_every=N                  every Nth data read answers EE 0A
  garbage_every=N                     every Nth data read answers junk bytes
  silent_after=T, silent_for=D        stop answering anything T s after the
                                      first data read, for D s (None = for
                                      good); coming back, the chip is in
                                      CONFIG mode (it power-cycled)
  reset_after=T                       T s after the first data read the chip
                                      drops into CONFIG mode but keeps
                                      answering; it stays there until the
                                      host writes OPR_MODE = NDOF again
  chip_id_silent=N                    ignore the first N CHIP_ID reads (slow
                                      boot)

Usable from tests:

    with FakeBNO055(overrun_every=25) as fake:
        run ewego-imu-uart --port fake.port ...
        fake.stats  -> counters, fake.writes -> [(t, reg, val)]

or standalone, for poking at the recorder by hand:

    python3 tests/fake_bno055.py [--overrun-every 25] ...   (prints the port)

Linux and macOS (os.openpty); stdlib only.
"""

import argparse
import math
import os
import select
import struct
import threading
import time
import tty

REG_CHIP_ID = 0x00
REG_PAGE_ID = 0x07
REG_DATA = 0x08
REG_OPR_MODE = 0x3D
REG_PWR_MODE = 0x3E
MODE_CONFIG = 0x00
MODE_NDOF = 0x0C
BURST_LEN = 46


class FakeBNO055:
    def __init__(self, overrun_every=0, overrun_repeat=1, rx_timeout_every=0, garbage_every=0,
                 silent_after=None, silent_for=None, reset_after=None, chip_id_silent=0):
        self.overrun_every = overrun_every
        self.overrun_repeat = overrun_repeat
        self.rx_timeout_every = rx_timeout_every
        self.garbage_every = garbage_every
        self.silent_after = silent_after
        self.silent_for = silent_for
        self.reset_after = reset_after
        self.chip_id_silent = chip_id_silent

        self.regs = bytearray(0x80)
        self.regs[REG_CHIP_ID] = 0xA0
        self.regs[0x01] = 0xFB      # ACC_ID
        self.regs[0x02] = 0x32      # MAG_ID
        self.regs[0x03] = 0x0F      # GYR_ID
        self.regs[REG_OPR_MODE] = MODE_CONFIG

        self.lock = threading.Lock()
        self.writes = []            # (t since start, reg, val) for every register write
        self.events = []            # (t since start, what) for scripted events
        self.stats = {"data_reads": 0, "data_ok": 0, "data_config_mode": 0, "chip_id_reads": 0,
                      "other_reads": 0, "writes": 0, "overrun_07": 0, "rx_timeout_0a": 0, "garbage": 0,
                      "ignored_while_silent": 0, "junk_in": 0}
        self.t0 = None              # time.monotonic() at start()
        self.t_first_data = None    # time of the first data read (scripted times count from here)
        self._pending_overruns = 0
        self._silent_done = False
        self._reset_done = False
        self._stop = threading.Event()
        self._thread = None
        self.master = self.slave = None
        self.port = None

    # ---- lifecycle -------------------------------------------------------

    def start(self):
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)      # no echo, no line discipline, before the host opens it
        self.port = os.ttyname(self.slave)
        self.t0 = time.monotonic()
        self._thread = threading.Thread(target=self._serve, name="fake-bno055", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        for fd in (self.master, self.slave):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.master = self.slave = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ---- state ----------------------------------------------------------

    @property
    def opr_mode(self):
        return self.regs[REG_OPR_MODE]

    def now(self):
        return time.monotonic() - self.t0

    def since_first_data(self):
        return None if self.t_first_data is None else time.monotonic() - self.t_first_data

    def _event(self, what):
        self.events.append((round(self.now(), 3), what))

    def _silent(self):
        s = self.since_first_data()
        if self.silent_after is None or s is None or s < self.silent_after:
            return False
        if self.silent_for is not None and s >= self.silent_after + self.silent_for:
            if not self._silent_done:
                self._silent_done = True
                self.regs[REG_OPR_MODE] = MODE_CONFIG   # power-cycled: back in CONFIG mode
                self._event("silence ends, chip back in CONFIG mode")
            return False
        return True

    def data_block(self):
        """The 46 bytes at 0x08..0x35 for the current mode and time."""
        t = self.now()
        ndof = self.opr_mode == MODE_NDOF
        acc = (int(30 * math.sin(t)), 0, 981)                 # 1 m/s^2 = 100 LSB
        mag = (320, -160, 480)                                # 1 uT = 16 LSB
        gyr = (int(16 * math.sin(2 * t)), 0, 0)               # 1 dps = 16 LSB
        if ndof:
            heading = int((t * 10 % 360) * 16)                # 1 deg = 16 LSB
            eul = (heading, 0, 0)
            half = math.radians(t * 10 % 360) / 2
            quat = (int(16384 * math.cos(half)), 0, 0, int(16384 * math.sin(half)))
            lia = (int(30 * math.sin(t)), 0, 0)
            grv = (0, 0, 981)
            calib = 0xFF                                      # sys/gyr/acc/mag all 3
        else:
            eul, quat, lia, grv, calib = (0, 0, 0), (0, 0, 0, 0), (0, 0, 0), (0, 0, 0), 0
        b = struct.pack("<3h3h3h3h4h3h3hbB", *acc, *mag, *gyr, *eul, *quat, *lia, *grv, 25, calib)
        assert len(b) == BURST_LEN
        return b

    # ---- protocol -------------------------------------------------------

    def _reply_read(self, reg, ln):
        if reg == REG_CHIP_ID:
            self.stats["chip_id_reads"] += 1
            if self.stats["chip_id_reads"] <= self.chip_id_silent:
                return b""
        if reg == REG_DATA and ln == BURST_LEN:
            return self._reply_data()
        self.stats["other_reads"] += 1
        if reg + ln > len(self.regs) or ln == 0:
            return bytes([0xEE, 0x04])                        # invalid address
        return bytes([0xBB, ln]) + bytes(self.regs[reg:reg + ln])

    def _reply_data(self):
        if self.t_first_data is None:
            self.t_first_data = time.monotonic()
        self.stats["data_reads"] += 1
        n = self.stats["data_reads"]
        s = self.since_first_data()
        if self.reset_after is not None and not self._reset_done and s >= self.reset_after:
            self._reset_done = True
            self.regs[REG_OPR_MODE] = MODE_CONFIG
            self._event("reset into CONFIG mode")
        if self.overrun_every and n % self.overrun_every == 0:
            self._pending_overruns = self.overrun_repeat
        if self._pending_overruns > 0:
            self._pending_overruns -= 1
            self.stats["overrun_07"] += 1
            return bytes([0xEE, 0x07])
        if self.rx_timeout_every and n % self.rx_timeout_every == 0:
            self.stats["rx_timeout_0a"] += 1
            return bytes([0xEE, 0x0A])
        if self.garbage_every and n % self.garbage_every == 0:
            self.stats["garbage"] += 1
            return bytes([0x55, 0x13, 0x37, 0x00, 0xFF, 0x42, 0x10, 0x99])
        if self.opr_mode != MODE_NDOF:
            self.stats["data_config_mode"] += 1
        else:
            self.stats["data_ok"] += 1
        return bytes([0xBB, BURST_LEN]) + self.data_block()

    def _reply_write(self, reg, data):
        self.stats["writes"] += 1
        for i, v in enumerate(data):
            if reg + i < len(self.regs) and reg + i != REG_CHIP_ID:
                self.regs[reg + i] = v
                self.writes.append((round(self.now(), 3), reg + i, v))
        return bytes([0xEE, 0x01])

    def handle(self, buf):
        """Consume complete commands from buf (a bytearray); return the reply bytes."""
        out = b""
        while buf:
            if buf[0] != 0xAA:
                del buf[0]
                self.stats["junk_in"] += 1
                continue
            if len(buf) < 4:
                break
            op, reg, ln = buf[1], buf[2], buf[3]
            if op == 0x01:
                del buf[:4]
                if self._silent():
                    self.stats["ignored_while_silent"] += 1
                    continue
                with self.lock:
                    out += self._reply_read(reg, ln)
            elif op == 0x00:
                if len(buf) < 4 + ln:
                    break
                data = bytes(buf[4:4 + ln])
                del buf[:4 + ln]
                if self._silent():
                    self.stats["ignored_while_silent"] += 1
                    continue
                with self.lock:
                    out += self._reply_write(reg, data)
            else:
                del buf[0]
                self.stats["junk_in"] += 1
        return out

    def _serve(self):
        buf = bytearray()
        while not self._stop.is_set():
            r, _, _ = select.select([self.master], [], [], 0.05)
            if not r:
                continue
            try:
                chunk = os.read(self.master, 4096)
            except OSError:         # EIO: no slave open (cannot happen while we hold self.slave)
                time.sleep(0.01)
                continue
            if not chunk:
                continue
            buf += chunk
            reply = self.handle(buf)
            if reply:
                try:
                    os.write(self.master, reply)
                except OSError:
                    pass


def main():
    ap = argparse.ArgumentParser(description="fake BNO055 on a pty (prints the port to use)")
    ap.add_argument("--overrun-every", type=int, default=0)
    ap.add_argument("--overrun-repeat", type=int, default=1)
    ap.add_argument("--rx-timeout-every", type=int, default=0)
    ap.add_argument("--garbage-every", type=int, default=0)
    ap.add_argument("--silent-after", type=float)
    ap.add_argument("--silent-for", type=float)
    ap.add_argument("--reset-after", type=float)
    ap.add_argument("--chip-id-silent", type=int, default=0)
    a = ap.parse_args()
    fake = FakeBNO055(overrun_every=a.overrun_every, overrun_repeat=a.overrun_repeat,
                      rx_timeout_every=a.rx_timeout_every, garbage_every=a.garbage_every,
                      silent_after=a.silent_after, silent_for=a.silent_for, reset_after=a.reset_after,
                      chip_id_silent=a.chip_id_silent)
    with fake:
        print(f"fake BNO055 on {fake.port}  (e.g. ewego-imu-uart --port {fake.port} --out /tmp/x)", flush=True)
        try:
            while True:
                time.sleep(5)
                print(f"opr_mode 0x{fake.opr_mode:02X} {fake.stats}", flush=True)
        except KeyboardInterrupt:
            pass
        print("events:", fake.events)


if __name__ == "__main__":
    main()
