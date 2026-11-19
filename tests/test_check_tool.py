"""[R35] Firmware/tools/ewego-check.py on synthetic session directories.

Builds a fake EweGo data root (recordings/<unit>/<stamp>/... and
gps-logs/gps_log_<stamp>.ubx) with files shaped like the recorders' real
outputs, then runs the tool as a program (verdict lines, overall verdict,
exit code) and, for finer checks, calls its per-stream functions directly.

Exit codes (from the tool): 0 PASS or WARN, 2 FAIL, 1 no recordings / no
such run. Pure Python: runs on every platform.
"""

import importlib.util
import json
import math
import re
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import REPO, run

TOOL = REPO / "Firmware" / "tools" / "ewego-check.py"
STAMP = "20261118_120000"


def load_tool():
    spec = importlib.util.spec_from_file_location("ewego_check", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- fixtures: what each recorder leaves behind -----------------------------

MONO0 = 9_000_000_000            # CLOCK_MONOTONIC us at the start
REAL_MINUS_MONO = 1_785_000_000_000_000


def camera_summary(name, frames=300, fps=30.0, **over):
    s = {
        "program": "ewego-cam 0.5.2", "name": name, "device": "/dev/v4l/by-path/platform-usb-0:1.1:1.0-video-index0",
        "requested": {"width": 1920, "height": 1080, "fps": 30, "format": "MJPG"},
        "frames": frames, "bytes": frames * 200_000, "lost": 0, "gaps": 0, "gap_list": "", "error_frames": 0,
        "stalls": 0,
        "writer": {"frames": frames, "bytes": frames * 200_000, "overruns": 0, "queue_peak_bytes": 4_000_000,
                   "queue_budget_bytes": 67_108_864, "write_max_ms": 120.0, "error": 0},
        "first_seq": 0, "last_seq": frames - 1,
        "first_ts_us": MONO0, "last_ts_us": MONO0 + int((frames - 1) * 1e6 / fps),
        "interval_us": {"min": 33000.0, "mean": 1e6 / fps, "max": 34500.0, "n": frames - 1},
        "effective_fps": fps, "timestamp_clock": "monotonic", "timestamp_source": "end-of-frame/arrival",
        "clock_at_start": {"monotonic_us": MONO0, "realtime_us": MONO0 + REAL_MINUS_MONO},
        "clock_at_end": {"monotonic_us": MONO0 + 10_000_000, "realtime_us": MONO0 + 10_000_000 + REAL_MINUS_MONO},
        "realtime_minus_monotonic_us": REAL_MINUS_MONO, "exit_code": 0,
    }
    deep_update(s, over)
    return s


def imu_usb_summary(samples=1000, **over):
    s = {
        "program": "ewego-imu-usb 0.3.1", "device": "/dev/hidraw0", "samples": samples, "lost": 0, "gaps": 0,
        "flagged": 0, "i2c_errors": 0, "host_not_polling": 0, "bad_reports": 0,
        "queue": {"overruns": 0, "peak": 3, "size": 8192}, "realtime": 1,
        "interval_us": {"min": 9990, "mean": 10000.0, "max": 10010, "n": samples - 1},
        "effective_hz": 100.0, "i2c_us_max": 5300, "device_minus_host_drift_ppm": 12.0,
        "first_dev_ts_us": 1234, "first_host_mono_us": MONO0 + 200_000,
        "realtime_minus_monotonic_us": REAL_MINUS_MONO, "exit_code": 0,
    }
    deep_update(s, over)
    return s


def imu_uart_summary(samples=1000, **over):
    s = {
        "program": "ewego-imu-uart 0.2.1", "device": "/dev/ttyAMA5", "baud": 115200, "rate_hz": 100,
        "samples": samples, "lost": 0, "gaps": 0, "flagged": 0, "read_errors": 0, "error_runs": 0, "late_ticks": 0,
        "errors_by_code": {"timeout_or_garbage": 0, "bus_overrun_07": 0, "receive_timeout_0a": 0, "other": 0},
        "reinits": 0, "queue": {"overruns": 0, "peak": 2, "size": 8192}, "realtime": 1,
        "interval_us": {"min": 9800, "mean": 10000.0, "max": 10200, "n": samples - 1},
        "effective_hz": 100.0, "xfer_us_max": 4600, "jitter_us_max": 900,
        "first_host_mono_us": MONO0 + 300_000, "realtime_minus_monotonic_us": REAL_MINUS_MONO, "exit_code": 0,
    }
    deep_update(s, over)
    return s


def deep_update(d, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            deep_update(d[k], v)
        else:
            d[k] = v


def wav_bytes(seconds=1.0, rate=48000, ch=2, amp=0.5, final_header=True):
    n = int(seconds * rate)
    frames = b"".join(struct.pack("<hh", v, v) for v in
                      (int(amp * 32767 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(n)))
    size = len(frames) if final_header else 0x7FFFFFFF   # arecord writes a placeholder until it closes the file
    fmt = struct.pack("<HHIIHH", 1, ch, rate, rate * ch * 2, ch * 2, 16)
    return (b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt
            + b"data" + struct.pack("<I", size) + frames)


def nav_pvt(itow_ms, fix=3, carr=0, nsv=14):
    p = bytearray(92)
    struct.pack_into("<I", p, 0, itow_ms)
    p[20] = fix
    p[21] = 0x01 | (carr << 6)
    p[23] = nsv
    struct.pack_into("<iii", p, 24, -954000000, 297000000, 15000)     # lon, lat (1e-7 deg), height mm
    struct.pack_into("<I", p, 40, 800)                                # hAcc mm
    return b"\xb5\x62\x01\x07" + struct.pack("<H", 92) + bytes(p) + b"\x00\x00"   # checksum not checked


class Root:
    """A fake /opt/ewego with helpers to add one run's sessions."""

    def __init__(self, path):
        self.path = Path(path)
        self.rec = self.path / "recordings"

    def session(self, unit, stamp):
        d = self.rec / unit / stamp
        d.mkdir(parents=True, exist_ok=True)
        return d

    def camera(self, name, stamp, summary=True, frames=300, **over):
        d = self.session(name, stamp)
        (d / f"{name}.mjpeg").write_bytes(b"\xff\xd8" + bytes(1000) + b"\xff\xd9")
        ts = b"".join(struct.pack("<q", MONO0 + i * 33333) for i in range(frames))
        (d / f"{name}_timestamps.bin").write_bytes(ts)
        (d / f"{name}_index.bin").write_bytes(bytes(32 * frames))
        if summary is True:
            summary = camera_summary(name, frames=frames, **over)
        if summary is not None:
            (d / f"{name}_summary.json").write_text(summary if isinstance(summary, str) else json.dumps(summary))
        return d

    def imu(self, unit, stamp, summary=True, rows=1000, **over):
        d = self.session(unit, stamp)
        with (d / f"{unit}.csv").open("w") as f:
            f.write("timestamp,heading_deg,...,flags\n")
            for i in range(rows):
                f.write(f"2026-11-18T12:00:{i // 100:02d}.{i % 100:02d}0000,1.0,...,0\n")
        if summary is True:
            summary = (imu_uart_summary if unit == "imu-uart" else imu_usb_summary)(samples=rows, **over)
        if summary is not None:
            (d / f"{unit}_summary.json").write_text(summary if isinstance(summary, str) else json.dumps(summary))
        return d

    def audio(self, stamp, **kw):
        d = self.session("audio", stamp)
        (d / "audio.wav").write_bytes(wav_bytes(**kw))
        return d

    def fuel(self, stamp, volts=3.95, n=30):
        d = self.session("fuel", stamp)
        t0 = 1_785_000_000.0
        lines = ["Timestamp,Datetime,Elapsed_Seconds,Voltage_V,SOC_Percent,Alert"]
        for i in range(n):
            lines.append(f"{t0 + 2 * i:.3f},2026-11-18 12:00:{(2 * i) % 60:02d},{2 * i:.1f},{volts - i * 1e-4:.4f},"
                         f"{80 - i * 0.01:.2f},False")
        (d / "fuel_gauge.csv").write_text("\n".join(lines) + "\n")
        return d

    def gps(self, stamp, n=50, nofix=0):
        g = self.path / "gps-logs"
        g.mkdir(parents=True, exist_ok=True)
        data = b"".join(nav_pvt(100_000 + 200 * i, fix=0 if i < nofix else 3) for i in range(n))
        f = g / f"gps_log_{stamp}.ubx"
        f.write_bytes(data)
        return f

    def full_run(self, stamp=STAMP):
        """Every stream of a clean run, sessions a second or two apart like real units."""
        base = stamp[:-2]
        sec = int(stamp[-2:])
        st = lambda k: f"{base}{sec + k:02d}"   # noqa: E731
        self.camera("camera1", st(0))
        self.camera("camera2", st(1))
        self.imu("imu", st(1))
        self.imu("imu-uart", st(2))
        self.audio(st(2))
        self.fuel(st(3))
        self.gps(st(0))


VERDICT_LINE = re.compile(r"^(camera1|camera2|imu-uart|imu|audio|fuel|gps)\s+(PASS|WARN|FAIL|MISSING)\s", re.M)


class CheckToolBase(unittest.TestCase):
    def setUp(self):
        self.root = Root(tempfile.mkdtemp(prefix="ewego-check-"))
        self.addCleanup(shutil.rmtree, self.root.path, True)

    def check(self, *args, expect_rc=None):
        r = run([sys.executable, TOOL, "--root", self.root.path, *args], timeout=60)
        if expect_rc is not None:
            self.assertEqual(r.returncode, expect_rc, r.stdout + r.stderr)
        return r

    def verdicts(self, out):
        v = dict(VERDICT_LINE.findall(out))
        m = re.search(r"^overall: (\w+)", out, re.M)
        v["overall"] = m.group(1) if m else None
        return v


class TestCheckRuns(CheckToolBase):
    def test_all_pass(self):
        self.root.full_run()
        r = self.check(expect_rc=0)
        v = self.verdicts(r.stdout)
        self.assertEqual(v, {u: "PASS" for u in ["camera1", "camera2", "imu", "imu-uart", "audio", "fuel", "gps",
                                                  "overall"]}, r.stdout)
        self.assertIn("start offsets", r.stdout)

    def test_no_recordings_exit_1(self):
        r = self.check(expect_rc=1)
        self.assertIn("no recordings", r.stdout)

    def test_missing_streams(self):
        """A missing camera/IMU stream fails the run; missing audio/fuel/gps only warns."""
        self.root.full_run()
        shutil.rmtree(self.root.rec / "audio")
        r = self.check(expect_rc=0)
        v = self.verdicts(r.stdout)
        self.assertEqual((v["audio"], v["overall"]), ("MISSING", "WARN"))
        shutil.rmtree(self.root.rec / "camera2")
        r = self.check(expect_rc=2)
        self.assertEqual(self.verdicts(r.stdout)["overall"], "FAIL")

    def test_grouping_list_and_run_selection(self):
        self.root.full_run("20261118_090000")
        self.root.full_run("20261118_120000")
        r = self.check("--list", expect_rc=0)
        lines = r.stdout.strip().splitlines()
        self.assertTrue(lines[0].startswith("20261118_120000"), r.stdout)   # newest first
        self.assertTrue(any(ln.startswith("20261118_090000") for ln in lines))
        r = self.check(expect_rc=0)
        self.assertIn("run 20261118_120000", r.stdout)
        r = self.check("--run", "20261118_09", expect_rc=0)
        self.assertIn("run 20261118_090000", r.stdout)
        r = self.check("--run", "1999", expect_rc=1)
        self.assertIn("no run matching", r.stdout)

    def test_unit_restart_splits_the_run(self):
        """CURRENT BEHAVIOUR, documented: a recorder restarted by systemd 30 s into
        a run gets a new session directory; the grouping puts it in a run of its
        own, which is then "the latest run", so the default check reports the
        other streams MISSING and FAILs. ROBUSTNESS.md "Later" (one session id
        per target start) would change this; update the expectations then."""
        self.root.full_run()
        self.root.camera("camera1", STAMP[:-2] + "30")
        r = self.check(expect_rc=2)
        v = self.verdicts(r.stdout)
        self.assertEqual((v["camera1"], v["camera2"], v["overall"]), ("PASS", "MISSING", "FAIL"), r.stdout)
        r = self.check("--list", expect_rc=0)
        self.assertEqual(len(r.stdout.strip().splitlines()), 2, r.stdout)

    def test_json_listing(self):
        self.root.full_run()
        r = self.check("--json", expect_rc=0)
        js = json.loads(r.stdout)
        self.assertEqual(js["stamp"], STAMP)
        self.assertEqual(sorted(js["files"]), sorted(["camera1", "camera2", "imu", "imu-uart", "audio", "fuel", "gps"]))


class TestCheckStreams(CheckToolBase):
    """One defect per test on an otherwise clean run, checked end to end."""

    def run_with(self, unit, expect_unit, expect_overall, expect_rc, note=None):
        r = self.check(expect_rc=expect_rc)
        v = self.verdicts(r.stdout)
        self.assertEqual(v.get(unit), expect_unit, r.stdout)
        self.assertEqual(v["overall"], expect_overall, r.stdout)
        if note:
            self.assertRegex(r.stdout, note)
        return r

    def test_imu_slow_i2c_burst_warns(self):
        self.root.full_run()
        self.root.imu("imu", STAMP[:-2] + "01", i2c_us_max=9000)
        # WARN exits 0, like PASS
        self.run_with("imu", "WARN", "WARN", 0, r"I2C burst up to 9\.0 ms")

    def test_imu_few_lost_warns_many_lost_fail(self):
        self.root.full_run()
        self.root.imu("imu", STAMP[:-2] + "01", rows=10000, lost=3, gaps=2)
        self.run_with("imu", "WARN", "WARN", 0, r"3 samples lost in 2 gaps")
        self.root.imu("imu", STAMP[:-2] + "01", rows=1000, lost=200, gaps=20)
        self.run_with("imu", "FAIL", "FAIL", 2)

    def test_uart_read_errors_wording(self):
        self.root.full_run()
        self.root.imu("imu-uart", STAMP[:-2] + "02", rows=1000, lost=50, gaps=4, read_errors=50, flagged=50)
        self.run_with("imu-uart", "FAIL", "FAIL", 2, r"50 failed UART reads in 4 runs")

    def test_imu_ring_overruns_fail(self):
        self.root.full_run()
        self.root.imu("imu-uart", STAMP[:-2] + "02", queue={"overruns": 7})
        self.run_with("imu-uart", "FAIL", "FAIL", 2, r"7 ring overruns")

    def test_imu_rate_off_warns(self):
        self.root.full_run()
        self.root.imu("imu", STAMP[:-2] + "01", effective_hz=50.0)
        self.run_with("imu", "WARN", "WARN", 0, r"rate 50\.00 Hz")

    def test_camera_lost_frames_fail(self):
        self.root.full_run()
        self.root.camera("camera1", STAMP, lost=5, gaps=2, gap_list="10+3,200+2")
        self.run_with("camera1", "FAIL", "FAIL", 2, r"5 frames lost in 2 gaps")

    def test_camera_writer_overruns_fail(self):
        self.root.full_run()
        self.root.camera("camera2", STAMP[:-2] + "01", writer={"overruns": 3})
        self.run_with("camera2", "FAIL", "FAIL", 2, r"3 writer overruns")

    def test_camera_low_fps_warns(self):
        self.root.full_run()
        self.root.camera("camera1", STAMP, fps=24.4)
        self.run_with("camera1", "WARN", "WARN", 0, r"effective 24\.40 fps")

    def test_camera_without_summary(self):
        """No summary: still recording or killed. Frames on disk -> WARN, none -> FAIL."""
        self.root.full_run()
        (self.root.rec / "camera1" / STAMP / "camera1_summary.json").unlink()
        self.run_with("camera1", "WARN", "WARN", 0, r"no summary\.json: still recording, or the recorder was killed")
        (self.root.rec / "camera1" / STAMP / "camera1_timestamps.bin").write_bytes(b"")
        self.run_with("camera1", "FAIL", "FAIL", 2)

    def test_imu_without_summary(self):
        self.root.full_run()
        (self.root.rec / "imu" / (STAMP[:-2] + "01") / "imu_summary.json").unlink()
        self.run_with("imu", "WARN", "WARN", 0, r"1000 samples so far")
        (self.root.rec / "imu" / (STAMP[:-2] + "01") / "imu.csv").unlink()
        self.run_with("imu", "FAIL", "FAIL", 2)

    def test_truncated_summary_is_treated_as_missing(self):
        """A summary cut short (power loss mid-write) reads like no summary at all."""
        self.root.full_run()
        text = json.dumps(imu_usb_summary())
        self.root.imu("imu", STAMP[:-2] + "01", summary=text[: len(text) // 2])
        self.run_with("imu", "WARN", "WARN", 0, r"no summary\.json")

    def test_summary_missing_keys_does_not_crash(self):
        """An older/foreign summary without the expected keys gives FAIL 'could not read', not a traceback."""
        self.root.full_run()
        s = camera_summary("camera1")
        del s["writer"]
        self.root.camera("camera1", STAMP, summary=s)
        r = self.run_with("camera1", "FAIL", "FAIL", 2, r"could not read: KeyError")
        self.assertNotIn("Traceback", r.stderr)

    def test_in_progress_summary_current_behaviour(self):
        """Periodic summaries (R4/R13) carry "in_progress": true while a recorder runs.

        CURRENT BEHAVIOUR, documented, not desired: ewego-check ignores the
        key and judges the partial summary as if it were final (PASS here,
        no note). When the tool learns the key (e.g. WARN "still
        recording"), change these expectations."""
        self.root.full_run()
        self.root.imu("imu", STAMP[:-2] + "01", in_progress=True, exit_code=0)
        self.root.camera("camera1", STAMP, in_progress=True)
        r = self.check(expect_rc=0)
        v = self.verdicts(r.stdout)
        self.assertEqual((v["imu"], v["camera1"], v["overall"]), ("PASS", "PASS", "PASS"), r.stdout)
        self.assertNotRegex(r.stdout.lower(), r"in.progress")

    def test_audio_header_not_final_warns(self):
        self.root.full_run()
        self.root.audio(STAMP[:-2] + "02", final_header=False)
        self.run_with("audio", "WARN", "WARN", 0, r"WAV header length not final")

    def test_audio_silence_warns(self):
        self.root.full_run()
        self.root.audio(STAMP[:-2] + "02", amp=0.0)
        self.run_with("audio", "WARN", "WARN", 0, r"near silence")

    def test_audio_not_a_wav_fails(self):
        self.root.full_run()
        (self.root.rec / "audio" / (STAMP[:-2] + "02") / "audio.wav").write_bytes(b"garbage" * 10)
        self.run_with("audio", "FAIL", "FAIL", 2, r"not a WAV header")

    def test_fuel_low_battery_warns(self):
        self.root.full_run()
        self.root.fuel(STAMP[:-2] + "03", volts=3.4)
        self.run_with("fuel", "WARN", "WARN", 0, r"battery at 3\.4")

    def test_gps_without_fix(self):
        self.root.full_run()
        self.root.gps(STAMP, n=50, nofix=50)
        self.run_with("gps", "FAIL", "FAIL", 2, r"50 of 50 epochs without a position fix")
        self.root.gps(STAMP, n=50, nofix=10)
        self.run_with("gps", "WARN", "WARN", 0)


class TestCheckFunctions(unittest.TestCase):
    """The per-stream functions, imported (finer than the printed report)."""

    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def setUp(self):
        self.root = Root(tempfile.mkdtemp(prefix="ewego-check-"))
        self.addCleanup(shutil.rmtree, self.root.path, True)

    def test_check_imu_start_wall(self):
        d = self.root.imu("imu", STAMP)
        verdict, line, notes, start = self.tool.check_imu(d, "imu")
        self.assertEqual(verdict, "PASS")
        self.assertEqual(notes, [])
        self.assertEqual(start, MONO0 + 200_000 + REAL_MINUS_MONO)
        self.assertIn("1000 samples", line)
        self.assertIn("drift 12.0 ppm", line)

    def test_check_imu_uart_line(self):
        d = self.root.imu("imu-uart", STAMP)
        verdict, line, notes, start = self.tool.check_imu(d, "imu-uart")
        self.assertEqual(verdict, "PASS")
        self.assertIn("uart max 4.6 ms", line)
        self.assertNotIn("drift", line)

    def test_lost_threshold_is_half_a_percent(self):
        for lost, want in ((4, "WARN"), (6, "FAIL")):
            with self.subTest(lost=lost):
                d = self.root.imu("imu", STAMP, rows=1000, lost=lost, gaps=1)
                self.assertEqual(self.tool.check_imu(d, "imu")[0], want)

    def test_runs_grouping_window(self):
        """Sessions within 60 s of the first one form a run; a unit seen twice starts a new run."""
        self.root.imu("imu", "20261118_120000")
        self.root.imu("imu-uart", "20261118_120059")
        self.root.imu("imu", "20261118_120030")    # imu again -> new run
        rs = self.tool.runs(self.root.path)
        self.assertEqual([sorted(r["units"]) for r in rs], [["imu", "imu-uart"], ["imu"]])
        self.root.camera("camera1", "20261118_120200")  # > 60 s after 12:00:30
        self.assertEqual(len(self.tool.runs(self.root.path)), 3)


if __name__ == "__main__":
    unittest.main()
