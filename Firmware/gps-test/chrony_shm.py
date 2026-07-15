#!/usr/bin/env python3
"""
Chrony SHM refclock writer.

Publishes GPS time samples into System-V shared-memory segment N so that
chrony's `refclock SHM N` line can pick them up as a time source. This is the
standard interface used by gpsd, ntpsec's own drivers, and various embedded
GPS-disciplined time systems for the last 25+ years — see chrony's manpage
under REFCLOCKS, and gpsd's ntpshm.c for the C reference layout.

Segment key = 0x4E545030 + unit  ('NTP0' in ASCII, plus the refclock unit
number). Chrony creates the segment on startup; we ensure it exists here so
the writer works even if chrony hasn't started yet.

Failure mode: if shmget/shmat fails at construction (e.g., unusual kernel
config), the caller should catch OSError and continue without SHM. If we
stop publishing for any reason, chrony sees valid=0 or a stale count on its
next poll and simply drops the source, falling back to the mesh peers.
"""

import ctypes
import struct


# ----------------------------------------------------------------------------
# SysV IPC bindings via libc
# ----------------------------------------------------------------------------
IPC_CREAT = 0o1000

libc = ctypes.CDLL("libc.so.6", use_errno=True)
libc.shmget.restype = ctypes.c_int
libc.shmget.argtypes = (ctypes.c_int, ctypes.c_size_t, ctypes.c_int)
libc.shmat.restype = ctypes.c_void_p
libc.shmat.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
libc.shmdt.restype = ctypes.c_int
libc.shmdt.argtypes = (ctypes.c_void_p,)

# 0x4E545030 == 'NTP0' — the base key chrony/ntpd use for SHM refclock unit 0.
# Unit N uses base + N.
SHM_KEY_BASE = 0x4E545030


# ----------------------------------------------------------------------------
# `struct shmTime` layout (chrony/refclock_shm.c, gpsd/ntpshm.c).
# ----------------------------------------------------------------------------
# Using @ (native size + native alignment) so the packed bytes match what
# chrony reads directly from the segment as a C struct.
_SHMTIME_FMT = "@ii" "qi" "qi" "iiii" "II" "32s"
# Python's struct.calcsize gives 92 for this layout; the C compiler pads the
# struct to 96 (multiple of the 8-byte time_t alignment). Request 128 to
# guarantee our shmget matches or exceeds chrony's own sizeof-based request —
# System V rounds up to a full page (4 KB) either way, so the extra bytes
# cost nothing.
_SHMTIME_PACK_SIZE = struct.calcsize(_SHMTIME_FMT)
_SHMTIME_SIZE = 128


def _pack_shmtime(mode, count, gps_sec, gps_nsec, recv_sec, recv_nsec,
                  leap, precision, valid):
    return struct.pack(
        _SHMTIME_FMT,
        mode,                # mode: 1 → chrony validates via `count` before use
        count,               # incremented on every publish
        gps_sec,             # clockTimeStampSec (time_t) — GPS UTC wall seconds
        gps_nsec // 1000,    # clockTimeStampUSec
        recv_sec,            # receiveTimeStampSec — CLOCK_REALTIME at capture
        recv_nsec // 1000,   # receiveTimeStampUSec
        leap,                # 0 = no leap
        precision,           # log2 of source precision (seconds); -1 ≈ 0.5s
        0,                   # nsamples (unused)
        valid,               # 1 = fresh sample
        gps_nsec,            # clockTimeStampNSec
        recv_nsec,           # receiveTimeStampNSec
        b"\x00" * 32,        # dummy[8]
    )


class ChronyShmWriter:
    """Publishes one GPS time sample per `publish()` call to SHM refclock unit N.

    Chrony's poll loop reads (count, fields, count) and uses the sample only if
    both count reads match — meaning we can safely write without an explicit
    lock, as long as `count` is incremented on every write. mode=1 selects
    that count-guarded read protocol.

    precision: log2 of expected accuracy in seconds. -1 = 0.5 s (matches gpsd's
    conservative default for GPS-over-UART, where the serial framing latency
    dominates). PPS (a separate refclock, `refclock PPS ... lock GPS`) is what
    delivers the actual sub-µs alignment; SHM 0 just anchors the second.
    """

    def __init__(self, unit=0, precision=-1, perms=0o666):
        self.key = SHM_KEY_BASE + unit
        self.unit = unit
        self.precision = precision
        self.count = 0

        # 0o666 = anyone can read/write. chrony may run as user `_chrony` on
        # Debian; matching UIDs is fragile, so we grant permissive perms on
        # this small (~96 byte) segment. There's nothing sensitive in it.
        self.shmid = libc.shmget(self.key, _SHMTIME_SIZE, IPC_CREAT | perms)
        if self.shmid < 0:
            err = ctypes.get_errno()
            raise OSError(err, f"shmget(key=0x{self.key:x}, size={_SHMTIME_SIZE}) failed")

        addr = libc.shmat(self.shmid, None, 0)
        # shmat returns (void*)-1 on failure, cast to unsigned that's ~0.
        if addr == ctypes.c_void_p(-1).value or addr is None:
            err = ctypes.get_errno()
            raise OSError(err, f"shmat(shmid={self.shmid}) failed")
        self.addr = addr

        # Zero the segment so chrony sees valid=0 until we publish.
        ctypes.memset(self.addr, 0, _SHMTIME_SIZE)

    def publish(self, gps_wall_seconds, sys_wall_seconds):
        """Write one (GPS time, receive time) sample. Both are float seconds
        since the Unix epoch (UTC).

        Call this every time you parse a NAV-PVT (or equivalent) with a valid
        time — typically 1 Hz. `sys_wall_seconds` should be captured with
        `time.time()` (== CLOCK_REALTIME) as close to the serial read as
        possible, since that's the moment chrony treats as "receive time".
        """
        gps_sec = int(gps_wall_seconds)
        gps_nsec = int(round((gps_wall_seconds - gps_sec) * 1e9))
        if gps_nsec >= 1_000_000_000:  # rounding edge
            gps_sec += 1
            gps_nsec = 0

        recv_sec = int(sys_wall_seconds)
        recv_nsec = int(round((sys_wall_seconds - recv_sec) * 1e9))
        if recv_nsec >= 1_000_000_000:
            recv_sec += 1
            recv_nsec = 0

        self.count += 1
        payload = _pack_shmtime(
            1, self.count, gps_sec, gps_nsec, recv_sec, recv_nsec,
            0, self.precision, 1,
        )
        ctypes.memmove(self.addr, payload, len(payload))

    def close(self):
        if getattr(self, "addr", 0):
            libc.shmdt(self.addr)
            self.addr = 0


if __name__ == "__main__":
    # Sanity check: create the segment, publish a sample matching current time,
    # and print the layout. Doesn't need chrony to be running.
    import time
    w = ChronyShmWriter(unit=0)
    t = time.time()
    w.publish(t, t)
    print(f"published sample at t={t:.6f} to SHM key 0x{w.key:x} "
          f"(shmid={w.shmid}, size={_SHMTIME_SIZE} B)")
    print("verify with: `ipcs -m` — look for the key column matching above")
    print("chrony will pick it up on the next refclock poll (typically <=2 s)")
    w.close()
