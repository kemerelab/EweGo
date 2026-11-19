"""[R34] ewego-imu-usb, black box, fed 64-byte hub_imu reports through a FIFO.

The recorder only needs poll() + read() on whatever --device names, so a
named pipe stands in for /dev/hidrawN: the test opens the write end once the
recorder has opened the read end and writes one report per 10 ms. Each
report is written whole (64 < PIPE_BUF, so atomic) and the recorder reads
64 bytes at a time, so framing holds as long as every write is a full
report; the short-report case is written alone with pauses around it.

Report layout: Hardware/hub_imu/firmware/src/report.h (version, flags, seq,
dev_ts_us, 46-byte burst, i2c_us, period_us, 6 reserved).

Exit codes (from the program): 0 clean; 2 lost/flagged samples or ring
overruns; 3 no reports for --stall s or a read error; 1 usage/device error.
"""

import errno
import json
import math
import os
import struct
import threading
import time
import unittest

from helpers import RecorderTestCase, build_tool, read_csv, read_json, run, summary_check

CSV_HEADER = [
    "timestamp", "heading_deg", "roll_deg", "pitch_deg", "quat_w", "quat_x", "quat_y", "quat_z",
    "lin_accel_x", "lin_accel_y", "lin_accel_z", "gravity_x", "gravity_y", "gravity_z",
    "accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z", "mag_x", "mag_y", "mag_z", "temp_c",
    "cal_sys", "cal_gyro", "cal_accel", "cal_mag",
    "host_mono_us", "dev_ts_us", "seq", "i2c_us", "flags",
]
COL = {c: i for i, c in enumerate(CSV_HEADER)}
SUMMARY_KEYS = ["program", "device", "samples", "lost", "gaps", "flagged", "i2c_errors", "host_not_polling",
                "bad_reports", "queue", "realtime", "interval_us", "effective_hz", "i2c_us_max",
                "device_minus_host_drift_ppm", "first_dev_ts_us", "first_host_mono_us",
                "realtime_minus_monotonic_us", "exit_code"]
LIVE_KEYS = ["program", "name", "device", "preview", "session", "elapsed_s", "samples", "lost", "gaps",
             "flagged", "i2c_errors", "queue_overruns", "queue_peak", "hz", "drift_ppm", "i2c_us_max",
             "temp_c", "cal", "updated_mono_us", "updated_real_us", "cols", "recent"]
NAME = "imu"
FLAG_I2C_ERROR, FLAG_NO_SENSOR, FLAG_DROPPED = 0x01, 0x02, 0x04
PERIOD_US = 10000


def burst(k):
    """46 BNO055 data bytes for sample k (same layout as the chip, 0x08..0x35)."""
    t = k / 100.0
    half = math.radians(t * 10 % 360) / 2
    return struct.pack("<3h3h3h3h4h3h3hbB",
                       int(30 * math.sin(t)), 0, 981,                     # accel, 1 m/s^2 = 100 LSB
                       320, -160, 480,                                    # mag
                       0, 0, 16,                                          # gyro
                       int((t * 10 % 360) * 16), 0, 0,                    # euler
                       int(16384 * math.cos(half)), 0, 0, int(16384 * math.sin(half)),
                       0, 0, 0,                                           # linear accel
                       0, 0, 981,                                         # gravity
                       24, 0xFF)                                          # temp, calib


def report(seq, dev_ts, k=0, flags=0, i2c_us=5200, version=1, length=64):
    b = struct.pack("<BBHI", version, flags, seq & 0xFFFF, dev_ts & 0xFFFFFFFF) + burst(k) \
        + struct.pack("<HH", i2c_us, PERIOD_US) + bytes(6)
    assert len(b) == 64
    return b[:length] if length <= 64 else b + bytes(length - 64)


class Feeder(threading.Thread):
    """Writes a scripted stream into the FIFO at 100 Hz.

    script(k) -> list of byte strings to write in slot k (empty = a slot
    with nothing sent, i.e. a lost sample), or None to end the stream.
    At the end the FIFO is closed when close_at_end, else held open (silent)
    until stop()."""

    def __init__(self, fifo, script, close_at_end=False):
        super().__init__(daemon=True, name="hid-feeder")
        self.fifo, self.script, self.close_at_end = fifo, script, close_at_end
        self.stop_evt = threading.Event()
        self.sent = 0           # reports written
        self.fd = None
        self.error = None

    def _open(self):
        # O_NONBLOCK write-open fails with ENXIO until a reader has the FIFO open
        while not self.stop_evt.is_set():
            try:
                fd = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as e:
                if e.errno != errno.ENXIO:
                    raise
                time.sleep(0.005)
                continue
            os.set_blocking(fd, True)
            return fd
        return None

    def run(self):
        try:
            self.fd = self._open()
            if self.fd is None:
                return
            t_next = time.monotonic()
            k = 0
            while not self.stop_evt.is_set():
                items = self.script(k)
                if items is None:
                    break
                for b in items:
                    os.write(self.fd, b)
                    if len(b) == 64 and b[0] == 1:
                        self.sent += 1
                k += 1
                t_next += PERIOD_US / 1e6
                d = t_next - time.monotonic()
                if d > 0:
                    time.sleep(d)
            if self.close_at_end:
                os.close(self.fd)
                self.fd = None
                return
            self.stop_evt.wait()
        except BrokenPipeError:
            pass                # the recorder exited and closed its end
        except Exception as e:  # surfaced by the test
            self.error = e
        finally:
            if self.fd is not None:
                os.close(self.fd)

    def stop(self):
        self.stop_evt.set()
        self.join(timeout=5)


SEQ0 = 65500                       # wraps past 65535 within the first second
DEV0 = 0xFFFFFFFF - 400000         # device clock wraps 32 bits after 0.4 s


def normal(k):
    return report(SEQ0 + k, DEV0 + k * PERIOD_US, k)


class UsbBase(RecorderTestCase):
    @classmethod
    def setUpClass(cls):
        cls.exe = build_tool("usbimu", "ewego-imu-usb")

    def setUp(self):
        super().setUp()
        self.fifo = self.tmp / "hidraw-fake"
        os.mkfifo(self.fifo)

    def record(self, script, *args, seconds=2, timeout=20, close_at_end=False, live=None, session_dir=False):
        out = self.tmp / "out"
        argv = [self.exe, "--device", self.fifo, "--out", out, "--stats", "0", "--name", NAME]
        if seconds:
            argv += ["--seconds", seconds]
        if not session_dir:
            argv.append("--no-session-dir")
        if live:
            argv += ["--live", live]
        argv += list(args)
        feeder = Feeder(self.fifo, script, close_at_end)
        feeder.start()
        self.addCleanup(feeder.stop)
        p = self.start(argv, "imu-usb")
        return p, feeder, out

    def finish(self, p, feeder, timeout=20):
        rc = p.wait(timeout)
        feeder.stop()
        self.assertIsNone(feeder.error)
        return rc

    def outputs(self, out):
        header, rows = read_csv(out / f"{NAME}.csv")
        return header, rows, read_json(out / f"{NAME}_summary.json")

    def assertCommon(self, header, rows, s, rc):
        self.assertEqual(header, CSV_HEADER)
        self.assertTrue(all(len(r) == len(CSV_HEADER) for r in rows))
        summary_check(self, s, SUMMARY_KEYS)
        self.assertEqual(sorted(s["queue"]), ["overruns", "peak", "size"])
        self.assertEqual(s["exit_code"], rc)
        self.assertEqual(s["samples"], len(rows))
        self.assertEqual(s["queue"]["overruns"], 0)


class TestUsbCli(UsbBase):
    def test_help(self):
        r = run([self.exe, "--help"], timeout=10)
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage: ewego-imu-usb", r.stdout)

    def test_usage_errors_exit_1(self):
        for args in ([], ["--preview"], ["--bogus"]):
            with self.subTest(args=args):
                self.assertEqual(run([self.exe, *args], timeout=10).returncode, 1)

    def test_list_without_device(self):
        """--list only scans /sys/class/hidraw; on a machine without a hub_imu it says so, exit 1."""
        r = run([self.exe, "--list"], timeout=10)
        if r.returncode == 0:
            self.skipTest(f"a hub_imu is attached here ({r.stdout.strip()})")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no hub_imu device", r.stdout)

    def test_missing_device_exit_1(self):
        r = run([self.exe, "--device", self.tmp / "no-such-hidraw", "--out", self.tmp / "out", "--seconds", "1"],
                timeout=10)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("no-such-hidraw", r.stderr)
        # Note (not asserted, R17 changes it): the session directory is
        # created before the device is opened and is left behind empty.


class TestUsbRecording(UsbBase):
    def test_clean_stream(self):
        """Reports with seq and the device clock both wrapping: no gap, 100 Hz, exit 0, live file."""
        live = self.tmp / "live"
        p, feeder, out = self.record(lambda k: [normal(k)], live=live, session_dir=True)
        live_file = live / f"{NAME}.json"
        snap = self.wait_for(lambda: live_file.exists() and json.loads(live_file.read_text()), 5)
        time.sleep(0.8)
        snap2 = json.loads(live_file.read_text()) if live_file.exists() else None
        rc = self.finish(p, feeder)
        self.assertEqual(rc, 0, self.diag(p))
        sess = self.session_dir(out)
        header, rows, s = self.outputs(sess)
        self.assertCommon(header, rows, s, rc)
        self.assertTrue(150 <= len(rows) <= 205, f"{len(rows)} rows")
        self.assertEqual((s["lost"], s["gaps"], s["flagged"], s["bad_reports"]), (0, 0, 0, 0))
        seqs = [int(r[COL["seq"]]) for r in rows]
        self.assertEqual(seqs, [(SEQ0 + i) & 0xFFFF for i in range(len(rows))])
        self.assertIn(0, seqs, "seq wrapped")
        self.assertEqual(s["interval_us"]["min"], PERIOD_US)
        self.assertEqual(s["interval_us"]["max"], PERIOD_US, "device-clock interval survives the 32-bit wrap")
        self.assertAlmostEqual(s["effective_hz"], 100.0, places=2)
        self.assertEqual(s["first_dev_ts_us"], DEV0)
        self.assertEqual(s["i2c_us_max"], 5200)
        self.assertEqual(s["device"], str(self.fifo))
        last = rows[-1]
        self.assertEqual(last[COL["flags"]], "0")
        self.assertEqual(last[COL["temp_c"]], "24")
        self.assertEqual(last[COL["cal_sys"]:COL["cal_mag"] + 1], ["3", "3", "3", "3"])
        self.assertAlmostEqual(float(last[COL["gravity_z"]]), 9.81, places=2)
        self.assertAlmostEqual(float(last[COL["accel_z"]]), 9.81, places=2)

        self.assertIsNotNone(snap, "no live JSON within 5 s" + self.diag(p))
        for js in (snap, snap2):
            summary_check(self, js, LIVE_KEYS)
            self.assertEqual(js["session"], str(sess))
            self.assertEqual(len(js["cols"]), 17)
            self.assertLessEqual(len(js["recent"]), 64)
            self.assertTrue(all(len(row) == 17 for row in js["recent"]))
        self.assertGreater(snap2["samples"], snap["samples"])
        self.assertEqual(snap2["cal"], [3, 3, 3, 3])
        self.assertFalse(live_file.exists(), "live file removed at exit")

    def test_sequence_gaps(self):
        """Samples never sent (3 at k=50, 1 at k=120): lost 4 in 2 gaps, exit 2."""
        skip = {50, 51, 52, 120}
        p, feeder, out = self.record(lambda k: [] if k in skip else [normal(k)])
        rc = self.finish(p, feeder)
        header, rows, s = self.outputs(out)
        self.assertEqual(rc, 2, self.diag(p))
        self.assertCommon(header, rows, s, rc)
        self.assertEqual(s["lost"], 4)
        self.assertEqual(s["gaps"], 2)
        self.assertEqual(s["flagged"], 0)
        self.assertIn("gap: seq", p.stderr)
        # intervals across a gap are left out of the statistics
        self.assertEqual(s["interval_us"]["max"], PERIOD_US)

    def test_flagged_reports(self):
        """3 reports with the I2C error flag, 2 with 'previous dropped': flagged 5, exit 2."""
        i2c = {30, 31, 90}
        dropped = {60, 150}

        def script(k):
            f = (FLAG_I2C_ERROR if k in i2c else 0) | (FLAG_DROPPED if k in dropped else 0)
            return [report(SEQ0 + k, DEV0 + k * PERIOD_US, k, flags=f, i2c_us=9100 if k == 100 else 5200)]

        p, feeder, out = self.record(script)
        rc = self.finish(p, feeder)
        header, rows, s = self.outputs(out)
        self.assertEqual(rc, 2, self.diag(p))
        self.assertCommon(header, rows, s, rc)
        self.assertEqual((s["flagged"], s["i2c_errors"], s["host_not_polling"], s["lost"]), (5, 3, 2, 0))
        self.assertEqual(s["i2c_us_max"], 9100)
        bad = [r for r in rows if int(r[COL["flags"]]) & FLAG_I2C_ERROR]
        self.assertEqual(len(bad), 3)
        for r in bad:
            self.assertEqual(r[1:28], [""] * 27, "sensor columns of an I2C-error sample are empty")
        drop = [r for r in rows if int(r[COL["flags"]]) & FLAG_DROPPED]
        self.assertEqual(len(drop), 2)
        self.assertNotEqual(drop[0][COL["quat_w"]], "", "a 'previous dropped' sample still has data")

    def test_bad_reports(self):
        """Wrong version (x2) and a 32-byte report (x1) are counted as bad_reports and skipped."""
        def script(k):
            if k in (40, 80):
                return [report(SEQ0 + k, DEV0 + k * PERIOD_US, k, version=2)]
            if k == 120:
                time.sleep(0.03)                       # let the reader drain, so the short write is read alone
                return [report(SEQ0 + k, DEV0 + k * PERIOD_US, k, length=32)]
            if k == 121:
                time.sleep(0.03)
            return [normal(k)]

        p, feeder, out = self.record(script)
        rc = self.finish(p, feeder)
        header, rows, s = self.outputs(out)
        self.assertCommon(header, rows, s, rc)
        self.assertEqual(s["bad_reports"], 3, p.stderr[-500:])
        self.assertRegex(p.stderr, r"unexpected report #\d+: 32 bytes")
        self.assertIn("version 2", p.stderr)
        self.assertLessEqual(s["samples"], feeder.sent)
        # the skipped reports' seqs show up as a 1-sample gap each
        self.assertEqual((s["lost"], s["gaps"]), (3, 3))
        # Documents current behaviour: bad_reports alone does not fail the exit
        # code (only lost/flagged/overruns do); here exit is 2 because of the gaps.
        self.assertEqual(rc, 2)

    def test_stall_exit_3(self):
        """The writer stays connected but goes quiet after 1 s: exit 3 after --stall 2."""
        p, feeder, out = self.record(lambda k: [normal(k)] if k < 100 else None, "--stall", "2", seconds=30)
        t0 = time.monotonic()
        rc = self.finish(p, feeder, timeout=15)
        self.assertEqual(rc, 3, self.diag(p))
        self.assertLess(time.monotonic() - t0, 8)
        self.assertIn("no valid reports for 2 s", p.stderr)
        header, rows, s = self.outputs(out)
        self.assertCommon(header, rows, s, rc)
        self.assertEqual(s["samples"], 100)
        self.assertEqual(s["lost"], 0)

    def test_read_error_exit_3(self):
        """A read() error ends the run with exit 3 ("device unplugged?") and a summary.

        A hidraw node whose device is gone returns EIO; without hardware, a
        directory given as --device makes read() fail (EISDIR) the same way."""
        d = self.tmp / "not-a-device"
        d.mkdir()
        out = self.tmp / "out"
        p = self.start([self.exe, "--device", d, "--out", out, "--no-session-dir", "--stats", "0",
                        "--name", NAME, "--seconds", "10"], "imu-usb")
        rc = p.wait(10)
        self.assertEqual(rc, 3, self.diag(p))
        self.assertIn("device unplugged?", p.stderr)
        s = read_json(out / f"{NAME}_summary.json")
        self.assertEqual((s["samples"], s["exit_code"]), (0, 3))

    def test_writer_closes_exit_3(self):
        """The writer closes the FIFO after 0.5 s: read() returns 0 (EOF) forever.

        Desired: no valid report for --stall s -> exit 3. Today each 0-byte
        read counts as a bad report, refreshes the stall timer and skips the
        --seconds check (`continue`), so the recorder spins at 100 % CPU until
        it is signalled. A pty whose master closes behaves the same (read
        returns 0 on Linux); a real hidraw node returns EIO, which does exit 3.
        """
        p, feeder, out = self.record(lambda k: [normal(k)] if k < 50 else None, "--stall", "2", "--quiet",
                                     seconds=4, close_at_end=True)
        rc = p.wait(7)          # SIGTERM after 7 s if still spinning
        feeder.stop()
        s = read_json(out / f"{NAME}_summary.json")
        self.assertFalse(p.killed, f"recorder still running 6.5 s after EOF; bad_reports={s['bad_reports']}")
        self.assertEqual(rc, 3)


if __name__ == "__main__":
    unittest.main()
