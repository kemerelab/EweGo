"""[R33] ewego-imu-uart, black box, against a fake BNO055 on a pty.

Each test starts tests/fake_bno055.py's FakeBNO055 with a scripted
behaviour, runs the real recorder (built from Firmware/imuuart with its own
Makefile) on the pty for a couple of seconds, and checks the CSV, the
summary JSON, the live JSON and the exit code.

Exit codes under test (from the program): 0 every tick produced a sample;
2 read errors or ring overruns; 3 no valid sample for --stall s; 1 usage,
open or init failure.
"""

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from fake_bno055 import FakeBNO055, MODE_CONFIG, MODE_NDOF, REG_OPR_MODE
from helpers import Proc, RecorderTestCase, build_tool, read_csv, read_json, summary_check

CSV_HEADER = [
    "timestamp", "heading_deg", "roll_deg", "pitch_deg", "quat_w", "quat_x", "quat_y", "quat_z",
    "lin_accel_x", "lin_accel_y", "lin_accel_z", "gravity_x", "gravity_y", "gravity_z",
    "accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z", "mag_x", "mag_y", "mag_z", "temp_c",
    "cal_sys", "cal_gyro", "cal_accel", "cal_mag",
    "host_mono_us", "tick_mono_us", "seq", "xfer_us", "flags",
]
SUMMARY_KEYS = ["program", "device", "rate_hz", "samples", "lost", "gaps", "flagged", "read_errors",
                "error_runs", "late_ticks", "errors_by_code", "reinits", "queue", "interval_us",
                "effective_hz", "xfer_us_max", "jitter_us_max", "first_host_mono_us",
                "realtime_minus_monotonic_us", "exit_code"]
ERROR_CODE_KEYS = ["timeout_or_garbage", "bus_overrun_07", "receive_timeout_0a", "other"]
LIVE_KEYS = ["program", "name", "device", "preview", "session", "elapsed_s", "samples", "lost", "gaps",
             "flagged", "read_errors", "queue_overruns", "queue_peak", "hz", "temp_c", "cal",
             "updated_mono_us", "updated_real_us", "cols", "recent"]
NAME = "imu-uart"
COL = {c: i for i, c in enumerate(CSV_HEADER)}


def rows_with_flag(rows, flag):
    return [r for r in rows if int(r[COL["flags"]]) & flag]


class UartBase(RecorderTestCase):
    @classmethod
    def setUpClass(cls):
        cls.exe = build_tool("imuuart", "ewego-imu-uart")

    def record(self, fake, *args, seconds=2, timeout=30, session_dir=False, live=None):
        """Run the recorder against fake; return (exit code, Proc, out dir)."""
        out = self.tmp / "out"
        argv = [self.exe, "--port", fake.port, "--out", out, "--stats", "0", "--name", NAME]
        if seconds:
            argv += ["--seconds", seconds]
        if not session_dir:
            argv.append("--no-session-dir")
        if live:
            argv += ["--live", live]
        argv += list(args)
        p = self.start(argv, "imu-uart")
        rc = p.wait(timeout)
        self.assertFalse(p.killed, f"recorder did not exit within {timeout} s" + self.diag(p))
        return rc, p, out

    def outputs(self, out):
        header, rows = read_csv(out / f"{NAME}.csv")
        summary = read_json(out / f"{NAME}_summary.json")
        return header, rows, summary

    def assertSummaryConsistent(self, s, rows, rc):
        summary_check(self, s, SUMMARY_KEYS)
        self.assertEqual(sorted(s["errors_by_code"]), sorted(ERROR_CODE_KEYS))
        self.assertEqual(sorted(s["queue"]), ["overruns", "peak", "size"])
        self.assertEqual(s["exit_code"], rc)
        # "lost" and "flagged" mirror read_errors so ewego-check treats both IMUs alike
        self.assertEqual(s["lost"], s["read_errors"])
        self.assertEqual(s["flagged"], s["read_errors"])
        self.assertEqual(s["gaps"], s["error_runs"])
        # The tick that trips --stall is counted in errors_by_code but never
        # queued, so it is missing from read_errors and from the CSV (exit 3 only).
        self.assertEqual(sum(s["errors_by_code"].values()), s["read_errors"] + (1 if rc == 3 else 0))
        # every tick is a CSV row, valid or flagged
        self.assertEqual(len(rows), s["samples"] + s["read_errors"])
        self.assertEqual(len(rows_with_flag(rows, 1)), s["read_errors"])
        self.assertEqual(s["queue"]["overruns"], 0)


class TestUartCli(UartBase):
    def test_help(self):
        p = self.start([self.exe, "--help"])
        self.assertEqual(p.wait(10), 0)
        self.assertIn("usage: ewego-imu-uart", p.stdout)

    def test_usage_errors_exit_1(self):
        for args in ([], ["--out", self.tmp / "o", "--rate", "0"], ["--out", self.tmp / "o", "--rate", "500"],
                     ["--preview"], ["--bogus"]):
            with self.subTest(args=args):
                p = self.start([self.exe, *args], "usage")
                self.assertEqual(p.wait(10), 1, self.diag(p))

    def test_missing_port_exit_1(self):
        out = self.tmp / "out"
        p = self.start([self.exe, "--port", self.tmp / "no-such-tty", "--out", out, "--seconds", "1"])
        self.assertEqual(p.wait(10), 1, self.diag(p))
        self.assertIn("no-such-tty", p.stderr)
        # Note (not asserted, R17 changes it): today the session directory is
        # created before the port is opened, so a failed start leaves an empty
        # <out>/<stamp>/ behind.


class TestUartRecording(UartBase):
    def test_clean_run(self):
        """Normal sensor: 100 Hz rows, all fields filled, live file while running, exit 0."""
        live = self.tmp / "live"
        with FakeBNO055() as fake:
            out = self.tmp / "out"
            argv = [self.exe, "--port", fake.port, "--out", out, "--seconds", "2", "--stats", "1",
                    "--name", NAME, "--live", live]
            p = self.start(argv, "imu-uart")
            live_file = live / f"{NAME}.json"
            snap = self.wait_for(lambda: live_file.exists() and json.loads(live_file.read_text()), 5)
            time.sleep(0.8)
            snap2 = json.loads(live_file.read_text()) if live_file.exists() else None
            rc = p.wait(15)
            writes = list(fake.writes)
        self.assertEqual(rc, 0, self.diag(p))

        sess = self.session_dir(out)
        self.assertRegex(sess.name, r"^\d{8}_\d{6}$")
        header, rows, s = self.outputs(sess)
        self.assertEqual(header, CSV_HEADER)
        self.assertTrue(all(len(r) == len(CSV_HEADER) for r in rows))
        self.assertTrue(150 <= len(rows) <= 205, f"{len(rows)} rows in 2 s at 100 Hz")
        self.assertSummaryConsistent(s, rows, rc)
        self.assertEqual(s["read_errors"], 0)
        self.assertEqual(s["reinits"], 0)
        self.assertEqual(s["rate_hz"], 100)
        self.assertAlmostEqual(s["effective_hz"], 100, delta=3)
        self.assertTrue(s["program"].startswith("ewego-imu-uart "))
        self.assertEqual(s["device"], fake.port)

        seqs = [int(r[COL["seq"]]) for r in rows]
        self.assertEqual(seqs, list(range(len(rows))))
        ticks = [int(r[COL["tick_mono_us"]]) for r in rows]
        self.assertEqual({b - a for a, b in zip(ticks, ticks[1:])}, {10000}, "ticks on a 10 ms grid")
        self.assertTrue(all(r[COL["flags"]] == "0" for r in rows))
        last = rows[-1]
        self.assertNotEqual(float(last[COL["quat_w"]]), 0.0)
        self.assertEqual(last[COL["cal_sys"]:COL["cal_mag"] + 1], ["3", "3", "3", "3"])
        self.assertAlmostEqual(float(last[COL["gravity_z"]]), 9.81, places=2)
        self.assertEqual(last[COL["temp_c"]], "25")

        # init sequence: CONFIG, PWR_MODE normal, PAGE 0, NDOF; CONFIG again at exit
        regs = [(reg, val) for _, reg, val in writes]
        self.assertEqual(regs[:4], [(REG_OPR_MODE, MODE_CONFIG), (0x3E, 0x00), (0x07, 0x00), (REG_OPR_MODE, MODE_NDOF)])
        self.assertEqual(regs[-1], (REG_OPR_MODE, MODE_CONFIG))

        # live JSON while running
        self.assertIsNotNone(snap, "no live JSON appeared within 5 s" + self.diag(p))
        for js in (snap, snap2):
            self.assertIsNotNone(js)
            summary_check(self, js, LIVE_KEYS)
            self.assertEqual(js["name"], NAME)
            self.assertEqual(js["session"], str(sess))
            self.assertEqual(js["preview"], 0)
            self.assertEqual(len(js["cols"]), 17)
            self.assertLessEqual(len(js["recent"]), 64)
            self.assertTrue(all(len(row) == len(js["cols"]) for row in js["recent"]))
        self.assertGreater(snap2["samples"], snap["samples"], "live counters advance")
        self.assertEqual(len(snap2["recent"]), 64)
        self.assertEqual(snap2["cal"], [3, 3, 3, 3])
        self.assertFalse(live_file.exists(), "live file is removed at exit")

    def test_preview_writes_live_file_only(self):
        live = self.tmp / "live"
        with FakeBNO055() as fake:
            p = self.start([self.exe, "--port", fake.port, "--preview", "--live", live, "--seconds", "1",
                            "--name", NAME, "--stats", "0"])
            js = self.wait_for(lambda: (live / f"{NAME}.json").exists()
                               and json.loads((live / f"{NAME}.json").read_text()), 5)
            rc = p.wait(10)
        self.assertEqual(rc, 0, self.diag(p))
        self.assertIsNotNone(js)
        self.assertEqual(js["preview"], 1)
        self.assertIsNone(js["session"])
        self.assertEqual(sorted(x.name for x in self.tmp.iterdir() if x.is_dir()), ["live"])

    def test_slow_chip_id_is_retried(self):
        """The first two CHIP_ID reads go unanswered (slow power-up): retried 500 ms apart."""
        with FakeBNO055(chip_id_silent=2) as fake:
            rc, p, out = self.record(fake, seconds=1)
        self.assertEqual(rc, 0, self.diag(p))
        self.assertIn("chip id attempt 2", p.stderr)
        self.assertEqual(fake.stats["chip_id_reads"], 3)

    def test_bus_overrun_retried_once_is_clean(self):
        """EE 07 once: the program drains and asks again; the retry succeeds.

        Documents current behaviour: such recovered overruns are not counted
        anywhere (summary, live JSON or flags), so the summary cannot show
        how often the sensor was busy."""
        with FakeBNO055(overrun_every=20) as fake:
            rc, p, out = self.record(fake)
        header, rows, s = self.outputs(out)
        self.assertGreaterEqual(fake.stats["overrun_07"], 5)
        self.assertEqual(rc, 0, self.diag(p))
        self.assertSummaryConsistent(s, rows, rc)
        self.assertEqual(s["read_errors"], 0)
        self.assertEqual(s["errors_by_code"]["bus_overrun_07"], 0)

    def test_bus_overrun_twice_flags_the_sample(self):
        """EE 07 on the read and on its retry: one flagged row (sensor columns empty), exit 2."""
        with FakeBNO055(overrun_every=20, overrun_repeat=2) as fake:
            rc, p, out = self.record(fake)
        header, rows, s = self.outputs(out)
        self.assertEqual(rc, 2, self.diag(p))
        self.assertSummaryConsistent(s, rows, rc)
        events = fake.stats["overrun_07"] // 2
        self.assertGreaterEqual(events, 5)
        self.assertAlmostEqual(s["errors_by_code"]["bus_overrun_07"], events, delta=1)
        self.assertEqual(s["errors_by_code"]["bus_overrun_07"], s["read_errors"])
        for r in rows_with_flag(rows, 1):
            self.assertEqual(r[1:28], [""] * 27, "sensor columns of a failed read are empty")
            self.assertNotEqual(r[COL["host_mono_us"]], "")
        self.assertGreater(s["samples"], 150)

    def test_receive_timeout_0a(self):
        with FakeBNO055(rx_timeout_every=25) as fake:
            rc, p, out = self.record(fake)
        header, rows, s = self.outputs(out)
        self.assertEqual(rc, 2, self.diag(p))
        self.assertSummaryConsistent(s, rows, rc)
        self.assertGreaterEqual(fake.stats["rx_timeout_0a"], 5)
        self.assertAlmostEqual(s["errors_by_code"]["receive_timeout_0a"], fake.stats["rx_timeout_0a"], delta=1)
        self.assertEqual(s["errors_by_code"]["receive_timeout_0a"], s["read_errors"])
        self.assertEqual(s["reinits"], 0)

    def test_garbage_reply(self):
        """Junk instead of BB/EE: flagged as timeout/garbage, stream resynchronises."""
        with FakeBNO055(garbage_every=25) as fake:
            rc, p, out = self.record(fake)
        header, rows, s = self.outputs(out)
        self.assertEqual(rc, 2, self.diag(p))
        self.assertSummaryConsistent(s, rows, rc)
        self.assertGreaterEqual(fake.stats["garbage"], 4)
        self.assertGreaterEqual(s["errors_by_code"]["timeout_or_garbage"], 1)
        self.assertLessEqual(s["read_errors"], fake.stats["garbage"] + 1)
        self.assertGreater(s["samples"], 150, "valid samples keep coming between the junk")
        first_bad = next(i for i, r in enumerate(rows) if r[COL["flags"]] != "0")
        self.assertTrue(any(r[COL["flags"]] == "0" for r in rows[first_bad + 1:first_bad + 5]))

    def test_silence_exits_3(self):
        """Sensor stops answering for good: re-init attempts, then exit 3 after --stall."""
        with FakeBNO055(silent_after=0.5) as fake:
            t0 = time.monotonic()
            rc, p, out = self.record(fake, "--stall", "2", seconds=30, timeout=20)
            took = time.monotonic() - t0
        header, rows, s = self.outputs(out)
        self.assertEqual(rc, 3, self.diag(p))
        self.assertLess(took, 12)
        self.assertIn("sensor or UART gone", p.stderr)
        self.assertEqual(s["exit_code"], 3)
        self.assertGreaterEqual(s["reinits"], 1)
        self.assertGreater(s["errors_by_code"]["timeout_or_garbage"], 0)
        self.assertSummaryConsistent(s, rows, rc)
        self.assertTrue(40 <= s["samples"] <= 60, f"{s['samples']} samples before 0.5 s of silence")

    def test_silence_then_power_cycle_recovers(self):
        """Silent for 2.5 s, back in CONFIG mode: the 2 s re-init restores NDOF, data resumes."""
        with FakeBNO055(silent_after=0.5, silent_for=2.5) as fake:
            rc, p, out = self.record(fake, "--stall", "8", seconds=6, timeout=25)
        header, rows, s = self.outputs(out)
        self.assertEqual(rc, 2, self.diag(p))
        self.assertSummaryConsistent(s, rows, rc)
        self.assertGreaterEqual(s["reinits"], 1)
        self.assertIn("sensor re-initialised", p.stderr)
        t_back = [t for t, what in fake.events if what.startswith("silence ends")]
        self.assertTrue(t_back)
        self.assertTrue(any(t >= t_back[0] and reg == REG_OPR_MODE and val == MODE_NDOF
                            for t, reg, val in fake.writes), f"no NDOF write after the silence: {fake.writes}")
        tail = rows[-50:]
        self.assertTrue(all(r[COL["flags"]] == "0" and float(r[COL["quat_w"]]) != 0 for r in tail),
                        "fusion data flows again after the re-init")
        self.assertGreater(s["late_ticks"], 0, "the blocking re-init shows up as late ticks")


class TestUartResetIntoConfigMode(UartBase):
    """The BNO055 resets into CONFIG mode but keeps answering register reads:
    every read succeeds, the fusion outputs and calibration read zero. The
    recorder should notice (all-zero quaternion / zero calibration -> read
    OPR_MODE -> re-init). Item R14 (lane A2) adds that; until then the
    recorder logs zeros forever and exits 0, which is the suspected cause of
    the "collar IMU freezes" symptom. Flip the expectedFailure when R14 lands.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ctmp = Path(tempfile.mkdtemp(prefix="ewego-test-"))
        cls.addClassCleanup(shutil.rmtree, cls.ctmp, True)
        cls.fake = FakeBNO055(reset_after=1.0)
        cls.fake.start()
        cls.addClassCleanup(cls.fake.stop)
        out = cls.ctmp / "out"
        cls.proc = Proc([cls.exe, "--port", cls.fake.port, "--out", out, "--seconds", "4", "--stats", "0",
                         "--no-session-dir", "--name", NAME], cls.ctmp, "imu-uart")
        cls.addClassCleanup(cls.proc.cleanup)
        cls.rc = cls.proc.wait(25)
        cls.fake.stop()
        cls.header, cls.rows = read_csv(out / f"{NAME}.csv")
        cls.summary = read_json(out / f"{NAME}_summary.json")
        resets = [t for t, what in cls.fake.events if what.startswith("reset")]
        cls.t_reset = resets[0] if resets else None

    def test_scenario_happened(self):
        """Preconditions (true today): the fake reset, the recorder kept reading and logged zeros."""
        self.assertFalse(self.proc.killed)
        self.assertIsNotNone(self.t_reset)
        self.assertGreater(self.fake.stats["data_config_mode"], 50)
        zero = [r for r in self.rows if r[COL["flags"]] == "0" and float(r[COL["quat_w"]]) == 0
                and r[COL["cal_sys"]] == "0"]
        self.assertGreater(len(zero), 0, "rows read while the chip sat in CONFIG mode")

    @unittest.expectedFailure  # R14: flip to a plain test when the recorder re-inits a CONFIG-mode sensor
    def test_recorder_restores_ndof(self):
        self.assertTrue(any(t > self.t_reset and reg == REG_OPR_MODE and val == MODE_NDOF
                            for t, reg, val in self.fake.writes),
                        "recorder never rewrote OPR_MODE=NDOF after the sensor reset")
        tail = self.rows[-30:]
        self.assertTrue(all(float(r[COL["quat_w"]]) != 0 for r in tail if r[COL["flags"]] == "0"),
                        "quaternion still zero at the end of the recording")


if __name__ == "__main__":
    unittest.main()
