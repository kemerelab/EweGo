"""Tests for the Python loggers and helper scripts the image runs unattended.

Standard library only. pyserial, pyubx2 and smbus2 are vendored into the
image but not installed on the test host, so they are stubbed in
sys.modules before the loggers are imported.

    python3 -m unittest discover -s tests -v      # from the repository root
"""

import contextlib
import csv
import importlib.util
import io
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
FIRMWARE = REPO / "Firmware"
IMAGE = REPO / "image"


def _stub(name, **attrs):
    if name not in sys.modules:
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
    return sys.modules[name]


class _Unavailable:
    def __init__(self, *a, **k):
        raise OSError("stubbed module: no hardware on the test host")


_stub("smbus2", SMBus=_Unavailable)
_stub("serial", Serial=_Unavailable)
_stub("pyubx2", UBXReader=_Unavailable)

for sub in ("fuel_gauge", "gps-test"):
    p = str(FIRMWARE / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

import log_fuel_gauge  # noqa: E402
import max17048_test  # noqa: E402
import gps_logger  # noqa: E402


def load_ewego_check():
    spec = importlib.util.spec_from_file_location("ewego_check", FIRMWARE / "tools" / "ewego-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


# ---------------------------------------------------------------------------
# R26: fuel gauge low-battery shutdown
# ---------------------------------------------------------------------------

class LowBatteryCount(unittest.TestCase):
    f = staticmethod(log_fuel_gauge.low_battery_count)

    def test_both_low_counts(self):
        self.assertEqual(self.f(0, 3.40, 5.0, 8, 3.5), 1)
        self.assertEqual(self.f(29, 3.40, 5.0, 8, 3.5), 30)

    def test_one_good_value_resets(self):
        self.assertEqual(self.f(12, 3.60, 5.0, 8, 3.5), 0)   # voltage fine
        self.assertEqual(self.f(12, 3.40, 9.0, 8, 3.5), 0)   # charge fine

    def test_thresholds_are_strict(self):
        self.assertEqual(self.f(5, 3.5, 5.0, 8, 3.5), 0)
        self.assertEqual(self.f(5, 3.4, 8.0, 8, 3.5), 0)

    def test_failed_reading_neither_counts_nor_resets(self):
        self.assertEqual(self.f(7, None, 5.0, 8, 3.5), 7)
        self.assertEqual(self.f(7, 3.4, None, 8, 3.5), 7)
        self.assertEqual(self.f(0, None, None, 8, 3.5), 0)


class LowBatteryGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def guard(self, **kw):
        kw.setdefault("enabled", True)
        return log_fuel_gauge.LowBatteryGuard(8, 3.5, 3, events_path=self.tmp / "events.log",
                                              poweroff=lambda: self.calls.append(1), **kw)

    def test_powers_off_once_after_n_consecutive(self):
        g = self.guard()
        with quiet():
            for _ in range(2):
                g(3.4, 5.0)
            self.assertEqual(self.calls, [])
            g(3.4, 5.0)
            self.assertEqual(self.calls, [1])
            for _ in range(5):
                g(3.4, 5.0)
        self.assertEqual(self.calls, [1], "poweroff must be requested only once")
        ev = (self.tmp / "events.log").read_text()
        self.assertIn("low battery", ev)
        self.assertIn("powering off", ev)

    def test_hysteresis_and_none(self):
        g = self.guard()
        with quiet():
            for v, s in [(3.4, 5), (3.4, 5), (3.6, 5),          # reset by a good reading
                         (3.4, 5), (None, None), (3.4, 5)]:      # None does not reset
                g(v, s)
            self.assertEqual(self.calls, [])
            g(3.4, 5)
        self.assertEqual(self.calls, [1])

    def test_disabled_logs_but_does_not_power_off(self):
        g = self.guard(enabled=False)
        with quiet():
            for _ in range(5):
                g(3.4, 5.0)
        self.assertEqual(self.calls, [])
        self.assertIn("shutdown disabled", (self.tmp / "events.log").read_text())


class FakeGauge:
    def __init__(self, readings):
        self.readings = list(readings)
        self.cur = (None, None)

    def get_vcell(self):
        self.cur = self.readings.pop(0) if self.readings else (None, None)
        return self.cur[0]

    def get_soc(self):
        return self.cur[1]

    def is_alert_active(self):
        return False


class FuelCsvFormat(unittest.TestCase):
    def test_columns_rows_and_quiet_printing(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            log = tmp / "fuel_gauge.csv"
            readings = [(3.9, 80.0), (3.9, 79.9), (None, None), (3.9, 79.8), (3.9, 79.7)]
            seen = []

            def on_reading(v, s, a):
                seen.append((v, s))
                return len(seen) == len(readings)

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                max17048_test.run_continuous_monitoring(FakeGauge(readings), duration=60, interval=0,
                                                        log_file=str(log), print_every=60,
                                                        on_reading=on_reading)
            self.assertEqual(seen, readings)
            with log.open(newline="") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0], ["Timestamp", "Datetime", "Elapsed_Seconds", "Voltage_V", "SOC_Percent", "Alert"])
            self.assertEqual(len(rows), 1 + len(readings))
            self.assertEqual(rows[1][3:], ["3.9000", "80.000", "NO"])
            self.assertEqual(rows[3][3:], ["ERROR", "ERROR", "ERROR"])
            # quiet: first reading, the error, the recovery; not every reading
            data_lines = [ln for ln in out.getvalue().splitlines()
                          if ln.strip()[:1].isdigit() and ("ERROR" in ln or "NO" in ln)]
            self.assertEqual(len(data_lines), 3, out.getvalue())
        finally:
            shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# R29: GPS logger error handling
# ---------------------------------------------------------------------------

class ErrorStreakTest(unittest.TestCase):
    def test_gives_up_after_limit(self):
        e = gps_logger.ErrorStreak(5.0)
        self.assertEqual(e.error(100.0), "first")
        self.assertEqual(e.error(102.0), "continue")
        self.assertEqual(e.error(104.9), "continue")
        self.assertEqual(e.error(105.0), "give_up")
        self.assertEqual(e.count, 4)

    def test_ok_resets(self):
        e = gps_logger.ErrorStreak(5.0)
        e.error(100.0)
        e.error(104.0)
        self.assertEqual(e.ok(), 2)
        self.assertEqual(e.ok(), 0)
        self.assertEqual(e.error(106.0), "first")   # a new streak starts the clock again
        self.assertEqual(e.error(110.0), "continue")


class FakeSerial:
    in_waiting = 0

    def __init__(self):
        self.written = b""

    def write(self, b):
        self.written += b
        return len(b)

    def flush(self):
        pass

    def close(self):
        pass


class ScriptedUBR:
    """ubr.read() replacement: items are (raw, parsed) tuples or exceptions;
    when they run out it stops the logger."""

    def __init__(self, logger, items, forever=None):
        self.logger = logger
        self.items = list(items)
        self.forever = forever

    def read(self):
        if self.forever is not None:
            raise self.forever
        if not self.items:
            self.logger.running = False
            return (None, None)
        x = self.items.pop(0)
        if isinstance(x, Exception):
            raise x
        return x


class GpsReadWorker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        with quiet():
            self.lg = gps_logger.GPSLogger("/dev/null", log_dir=self.tmp)
            self.assertTrue(self.lg.open_logfile())
        self.lg.ser = FakeSerial()
        self.lg.running = True

    def tearDown(self):
        with quiet():
            if self.lg.logfile and not self.lg.logfile.closed:
                self.lg.logfile.close()
            self.lg.timesync.close()
        shutil.rmtree(self.tmp)

    def test_persistent_errors_exit_3(self):
        self.lg.errors = gps_logger.ErrorStreak(0.3)
        self.lg.ubr = ScriptedUBR(self.lg, [], forever=OSError("device reports readiness but returned no data"))
        out = io.StringIO()
        t0 = time.monotonic()
        with contextlib.redirect_stdout(out):
            self.lg.read_worker()
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(self.lg.exit_code, gps_logger.EXIT_READ_ERRORS)
        self.assertFalse(self.lg.running)
        # printed once when the streak starts and once when giving up, not per retry
        self.assertEqual(out.getvalue().count("Read/write error"), 1, out.getvalue())
        self.assertEqual(out.getvalue().count("exiting with status 3"), 2)

    def test_transient_errors_recover(self):
        self.lg.errors = gps_logger.ErrorStreak(5.0)
        self.lg.ubr = ScriptedUBR(self.lg, [OSError("x")] * 3 + [(b"\xb5b\x01\x07", None)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.lg.read_worker()
        self.assertEqual(self.lg.exit_code, 0)
        self.assertEqual(self.lg.errors.count, 0)
        self.assertIn("after 3 read/write error(s)", out.getvalue())

    def test_ubx_flushed_while_running(self):
        data = [(b"\xb5b" + bytes([i]) * 10, None) for i in range(5)]
        self.lg.ubr = ScriptedUBR(self.lg, data)
        with mock.patch.object(gps_logger, "FLUSH_EVERY_S", 0.0), \
                mock.patch.object(gps_logger, "FSYNC_EVERY_S", 0.0), quiet():
            self.lg.read_worker()
        # on disk before the file is closed
        self.assertEqual(Path(self.lg.log_filename).read_bytes(), b"".join(d[0] for d in data))


class GpsMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_main(self):
        argv = ["gps_logger.py", "--port", "/dev/nonexistent-gps", "--log-dir", self.tmp]
        with mock.patch.object(sys, "argv", argv), quiet():
            with self.assertRaises(SystemExit) as cm:
                gps_logger.main()
        return cm.exception.code

    def test_failed_start_exits_1(self):
        with mock.patch.object(gps_logger.serial, "Serial", side_effect=OSError("no such port")):
            self.assertEqual(self.run_main(), gps_logger.EXIT_START_FAILED)

    def test_exits_when_read_thread_exits(self):
        def fake_start(lg):
            lg.exit_code = gps_logger.EXIT_READ_ERRORS
            lg.read_thread = threading.Thread(target=lambda: None)
            lg.read_thread.start()
            return True

        with mock.patch.object(gps_logger.GPSLogger, "start", fake_start), \
                mock.patch.object(gps_logger.GPSLogger, "stop", lambda lg: None):
            self.assertEqual(self.run_main(), gps_logger.EXIT_READ_ERRORS)

    def test_unexpected_thread_end_is_a_failure(self):
        def fake_start(lg):
            lg.read_thread = threading.Thread(target=lambda: None)
            lg.read_thread.start()
            return True

        with mock.patch.object(gps_logger.GPSLogger, "start", fake_start), \
                mock.patch.object(gps_logger.GPSLogger, "stop", lambda lg: None):
            self.assertEqual(self.run_main(), gps_logger.EXIT_READ_ERRORS)


class FakeNtrip:
    def __init__(self, reads):
        self.reads = list(reads)
        self.socket = object()
        self.connects = 0

    def read_corrections(self):
        return self.reads.pop(0) if self.reads else None

    def disconnect(self):
        self.socket = None

    def connect(self, verbose=True):
        self.connects += 1
        self.socket = object()
        return True


class NtripReconnect(unittest.TestCase):
    def test_reconnects_after_drop(self):
        tmp = tempfile.mkdtemp()
        try:
            with quiet():
                lg = gps_logger.GPSLogger("/dev/null", log_dir=tmp)
            lg.ser = FakeSerial()
            lg.ntrip = FakeNtrip([b"", b"RTCM"])   # caster closes, then data after reconnect
            lg.running = True
            with mock.patch.object(gps_logger, "NTRIP_RETRY_S", 0.01), quiet():
                t = threading.Thread(target=lg.ntrip_worker, daemon=True)
                t.start()
                deadline = time.monotonic() + 5
                while lg.ser.written != b"RTCM" and time.monotonic() < deadline:
                    time.sleep(0.05)
                lg.running = False
                t.join(5)
            self.assertEqual(lg.ser.written, b"RTCM")
            self.assertGreaterEqual(lg.ntrip.connects, 1)
            self.assertFalse(t.is_alive())
        finally:
            shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# R28: audio recorder and ewego-check's view of arecord's rollover
# ---------------------------------------------------------------------------

def write_wav(path, seconds=0.1, rate=48000, ch=2, bits=32, amp=1 << 24, header_ok=True):
    n = int(seconds * rate)
    frame = struct.pack("<i", amp) * ch
    data = frame * n
    size = len(data) if header_ok else 0x7FFFFFFF
    fmt = struct.pack("<HHIIHH", 1, ch, rate, rate * ch * bits // 8, ch * bits // 8, bits)
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE")
        f.write(b"fmt " + struct.pack("<I", len(fmt)) + fmt)
        f.write(b"data" + struct.pack("<I", size) + data)


class AudioRollover(unittest.TestCase):
    def setUp(self):
        self.chk = load_ewego_check()
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_parts_order(self):
        for n in ("audio-10.wav", "audio-02.wav", "audio-01.wav", "notes.wav", "audio-x.wav"):
            (self.tmp / n).touch()
        self.assertEqual([p.name for p in self.chk.audio_parts(self.tmp)],
                         ["audio-01.wav", "audio-02.wav", "audio-10.wav"])

    def test_single_file(self):
        write_wav(self.tmp / "audio.wav")
        v, line, notes, _ = self.chk.check_audio(self.tmp)
        self.assertEqual(v, "PASS", (line, notes))

    def test_rolled_over_recording_is_one_stream(self):
        write_wav(self.tmp / "audio-01.wav", seconds=0.5)
        write_wav(self.tmp / "audio-02.wav", seconds=0.25)
        v, line, notes, _ = self.chk.check_audio(self.tmp)
        self.assertEqual(v, "PASS", (line, notes))
        self.assertIn("in 2 files", line)

    def test_unfinished_header_only_warns(self):
        write_wav(self.tmp / "audio-01.wav")
        write_wav(self.tmp / "audio-02.wav", header_ok=False)
        v, line, notes, _ = self.chk.check_audio(self.tmp)
        self.assertEqual(v, "WARN")
        self.assertTrue(any("audio-02.wav" in n for n in notes), notes)

    def test_missing(self):
        self.assertEqual(self.chk.check_audio(self.tmp)[0], "FAIL")

    def test_collect_links_every_part(self):
        d = self.tmp / "audio" / "20261118_120000"
        d.mkdir(parents=True)
        write_wav(d / "audio-01.wav")
        write_wav(d / "audio-02.wav")
        run = {"units": {"audio": d}, "stamps": {"audio": "20261118_120000"}}
        with quiet():
            self.chk.collect(run, self.tmp / "out")
        self.assertTrue((self.tmp / "out" / "audio_20261118_120000-01.wav").exists())
        self.assertTrue((self.tmp / "out" / "audio_20261118_120000-02.wav").exists())


@unittest.skipUnless(shutil.which("sh") and shutil.which("awk"), "needs sh and awk")
class AudioDevice(unittest.TestCase):
    CARDS = (" 0 [Headphones     ]: bcm2835_headpho - bcm2835 Headphones\n"
             "                      bcm2835 Headphones\n"
             " 1 [sndrpigooglevoi]: RPi-simple - snd_rpi_googlevoicehat_soundcar\n"
             "                      snd_rpi_googlevoicehat_soundcard\n")

    def device(self, cards_text, *args):
        with tempfile.NamedTemporaryFile("w", suffix=".cards", delete=False) as f:
            f.write(cards_text)
        try:
            env = dict(os.environ, EWEGO_ASOUND_CARDS=f.name)
            r = subprocess.run(["sh", str(IMAGE / "ewego" / "audio-record.sh"), "--print-device", *args],
                               capture_output=True, text=True, env=env, timeout=10)
        finally:
            os.unlink(f.name)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_voicehat_by_name(self):
        self.assertEqual(self.device(self.CARDS), "plughw:CARD=sndrpigooglevoi,DEV=0")

    def test_falls_back_to_default(self):
        self.assertEqual(self.device(" 0 [Headphones     ]: bcm2835_headpho - bcm2835 Headphones\n"), "default")

    def test_explicit_device_wins(self):
        self.assertEqual(self.device(self.CARDS, "hw:3,0"), "hw:3,0")


# ---------------------------------------------------------------------------
# R31: free-space guard
# ---------------------------------------------------------------------------

@unittest.skipUnless(shutil.which("sh") and shutil.which("df") and shutil.which("awk"), "needs sh, df, awk")
class CheckSpace(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_check(self, *args):
        env = dict(os.environ, EWEGO_RUN_DIR=str(self.tmp / "run"))
        env.pop("EWEGO_MIN_FREE_MB", None)
        return subprocess.run(["sh", str(IMAGE / "ewego" / "check-space.sh"), *args],
                              capture_output=True, text=True, env=env, timeout=10)

    def space_json(self):
        import json
        return json.loads((self.tmp / "run" / "space.json").read_text())

    def test_enough_space_prints_free_mb(self):
        r = self.run_check(str(self.tmp / "not" / "yet" / "created"), "0")
        self.assertEqual(r.returncode, 0, r.stderr)
        free = int(r.stdout.strip())
        j = self.space_json()
        self.assertEqual(j["free_mb"], free)
        self.assertTrue(j["ok"])
        self.assertEqual(j["min_mb"], 0)

    def test_below_minimum_exits_75(self):
        r = self.run_check(str(self.tmp), "999999999")
        self.assertEqual(r.returncode, 75)
        self.assertEqual(r.stdout, "")
        self.assertIn("not starting", r.stderr)
        self.assertFalse(self.space_json()["ok"])

    def test_default_minimum_is_500(self):
        r = self.run_check(str(self.tmp))
        self.assertIn(r.returncode, (0, 75))
        self.assertEqual(self.space_json()["min_mb"], 500)


class UnitFiles(unittest.TestCase):
    """The units this lane owns keep the settings the robustness items rely on."""

    def unit(self, name):
        sections, cur = {}, None
        for line in (IMAGE / "units" / name).read_text().splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                cur = sections.setdefault(line[1:-1], [])
            elif line and not line.startswith("#") and cur is not None:
                cur.append(line)
        return sections

    def test_no_start_limit_on_loggers(self):
        for name in ("ewego-audio.service", "ewego-fuel.service", "ewego-gps.service"):
            self.assertIn("StartLimitIntervalSec=0", self.unit(name)["Unit"], name)

    def test_space_guard_is_a_condition(self):
        for name in ("ewego-audio.service", "ewego-gps.service"):
            svc = self.unit(name)["Service"]
            self.assertTrue(any(l.startswith("ExecCondition=/usr/local/bin/ewego-check-space") for l in svc), name)
        # the low-battery guard must never be blocked by a full card
        fuel = self.unit("ewego-fuel.service")["Service"]
        self.assertFalse(any(l.startswith("ExecCondition=") for l in fuel))

    def test_audio_runs_arecord_wrapper_not_python(self):
        svc = self.unit("ewego-audio.service")["Service"]
        start = [l for l in svc if l.startswith("ExecStart=")]
        self.assertEqual(len(start), 1)
        self.assertNotIn("python", start[0])
        self.assertIn("KillSignal=SIGINT", svc)

    def test_autostart_gated_by_boot_flag(self):
        u = self.unit("ewego-autostart.service")
        self.assertIn("ConditionPathExists=/boot/firmware/ewego-record", u["Unit"])
        self.assertIn("WantedBy=multi-user.target", u["Install"])
        # the target itself stays startable by hand on a bench card
        self.assertFalse(any(l.startswith("Condition") for l in self.unit("ewego-record.target")["Unit"]))


if __name__ == "__main__":
    unittest.main()
