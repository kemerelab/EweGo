#!/usr/bin/env python3
"""
Sensor Test - Unified Data Logger
Runs all sensors in parallel for battery life testing:
- IMU (BNO055) - 50Hz logging
- Audio Recorder - Continuous recording
- Dual Camera - H.264 30fps recording
- Fuel Gauge (MAX17048) - Battery monitoring every 2 seconds

Press Ctrl+C to stop all processes gracefully
"""

import subprocess
import signal
import sys
import time
import os
import csv
from pathlib import Path
from datetime import datetime, timezone
import threading


_DEVICES = (
    "/dev/snd/pcmC0D0c",  # voiceHAT capture PCM
    "/dev/video0",         # cam1
    "/dev/video1",         # cam2
    "/dev/ttyAMA5",        # BNO055 IMU UART
)


def ensure_clean_start():
    """Terminate any previous record_sensors.py + child recorders, and free the
    hardware they were holding, so this launch starts on clean state.

    Rationale: without this, if a new session starts while a previous one is
    still running (operator hit `r` twice in mesh_monitor, ssh dropped mid-
    stop, previous crashed with orphans, etc.), the new subprocesses all fail
    with "Device or resource busy" — but the parent's clock_sync writer
    thread keeps ticking. Result: a session directory with only clock_sync.csv
    populated and every other stream at 0 bytes. The Michigan captures had
    dozens of these.

    Design choice: we RESTART cleanly rather than refuse to launch. The
    common operator intent when pressing `r` again is "start recording NOW",
    not "please make me manually clean up first."

    IMPORTANT: This runs BEFORE we do any hardware-touching setup. We
    explicitly filter our own PID out of the parent-pattern kill list — a
    naive `pkill -f 'python.*record_sensors.py'` would signal ourselves too.
    """
    my_pid = os.getpid()

    # 1. Find any other record_sensors.py parent(s) (not us).
    # Match `python3.*record_sensors.py` — NOT `python.*` — because the wider
    # pattern also matches our own parent `uv run python record_sensors.py`,
    # and killing it propagates SIGINT back to us (uv forwards signals to its
    # children). Under systemd's Restart=on-failure that turned into a start
    # loop until we hit the StartLimitBurst=10 ceiling (field-observed
    # 2026-07-09: every Pi's ewego-sensor.service in `failed` state,
    # traceback = KeyboardInterrupt in ensure_clean_start's time.sleep(3)).
    # Also filter our parent PID as belt-and-braces so a future launcher
    # change (e.g. an env wrapper that inserts "python3" in its argv) can't
    # regress this.
    my_ppid = os.getppid()
    r = subprocess.run(
        ["pgrep", "-f", "python3.*record_sensors.py"],
        capture_output=True, text=True,
    )
    others = [int(p) for p in r.stdout.split()
              if p.isdigit() and int(p) not in (my_pid, my_ppid)]

    # 2. Also check whether any of our devices are currently held. Even with
    #    no other record_sensors.py, an orphaned child recorder (record_audio,
    #    dual_cam) from a hard-killed previous session may still hold hardware.
    busy = _hardware_busy()

    if not others and not busy:
        return  # already clean, fast path

    print(f"[startup] cleaning up before launch "
          f"({len(others)} predecessor(s), hardware busy: {busy})", flush=True)

    # 3. SIGINT the previous parent(s) for a graceful drain.
    for pid in others:
        try:
            os.kill(pid, signal.SIGINT)
        except ProcessLookupError:
            pass
    time.sleep(3)

    # 4. SIGKILL any survivors.
    for pid in others:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    # 5. Broad sweep for orphaned recorder children by name. These patterns
    #    are distinct from our own command line so pkill -f is safe here.
    for pat in ("record_audio.py", "dual_cam_jp2",
                "log_imu_data", "max17048_test"):
        subprocess.run(
            ["pkill", "-9", "-f", pat],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    # 6. Final safety net: fuser -k on each hardware node. Catches renamed
    #    processes or hand-launched arecord/gst tools that our name-based
    #    sweep would miss. `sudo -n` — silent no-op if not available.
    subprocess.run(
        ["sudo", "-n", "fuser", "-k", *_DEVICES],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1)


def _hardware_busy():
    """True if any of the hardware devices record_sensors.py needs is currently
    held by a process. Best-effort — requires passwordless sudo to be useful;
    returns False silently if not available."""
    try:
        r = subprocess.run(
            ["sudo", "-n", "fuser", *_DEVICES],
            capture_output=True, text=True, timeout=2,
        )
        return bool(r.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False

# Resolve paths relative to this script's location (Firmware/)
FIRMWARE_DIR = Path(__file__).resolve().parent


class BatteryLifeTest:
    """Orchestrates all sensor processes for battery life testing"""

    def __init__(self):
        self.processes = {}
        self.running = False
        self.session = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Put output in the project root (one level above Firmware/)
        self.log_dir = FIRMWARE_DIR.parent / f"sensor_test_{self.session}"
        self.log_dir.mkdir(exist_ok=True)

        # Create subdirectories for organized logging
        (self.log_dir / "imu").mkdir(exist_ok=True)
        (self.log_dir / "camera").mkdir(exist_ok=True)
        (self.log_dir / "gps").mkdir(exist_ok=True)

        # session_events.log — one-line-per-event append log used by the
        # subprocess health watchdog. Open once here, closed on shutdown by
        # the operating system (we don't close explicitly because the log
        # should survive graceless termination too).
        self._events_file = open(self.log_dir / "session_events.log", "a",
                                  buffering=1)
        self._events_lock = threading.Lock()

        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        print("\n\n[SIGNAL] Shutdown requested...")
        self.stop_all()
        sys.exit(0)

    def _clock_sync_worker(self, out_path):
        """Write (monotonic_us, wall_time_s) pairs at 10 ms intervals.

        monotonic_us: time.monotonic_ns() // 1000 — unaffected by NTP, same
          clock domain as camera timestamps and all other sensor timestamps.
        wall_time_s:  time.time() — chrony-disciplined wall clock.

        Together these let post-processing convert any monotonic timestamp to
        wall time and correlate events across devices via chrony-synced wall time.

        Durability: os.fsync() every 1 s. line-buffering flushes to kernel
        page cache but writeback to SD can lag ~30 s. In Michigan 2026-04-13
        the hardware watchdog fired mid-recording and the tail of clock_sync
        was kilobytes of NUL bytes — up to 30 s of samples lost. The fsync
        forces the FS to durably persist and caps the loss at <1 s.
        """
        interval_s = 0.01  # 10 ms = 100 Hz
        fsync_interval_s = 1.0
        with open(out_path, "w", newline="", buffering=1) as f:
            writer = csv.writer(f)
            writer.writerow(["monotonic_us", "wall_time_s"])
            fd = f.fileno()
            next_tick = time.monotonic()
            last_fsync = time.monotonic()
            while self.running:
                mono = time.monotonic_ns() // 1000
                wall = time.time()
                writer.writerow([mono, f"{wall:.6f}"])
                now = time.monotonic()
                if now - last_fsync >= fsync_interval_s:
                    f.flush()
                    os.fsync(fd)
                    last_fsync = now
                next_tick += interval_s
                sleep_s = next_tick - time.monotonic()
                if sleep_s > 0:
                    time.sleep(sleep_s)
            # Final flush on graceful shutdown so the last <1 s isn't lost.
            f.flush()
            os.fsync(fd)

    # -----------------------------------------------------------------
    # Session-events log + subprocess health watchdog
    # -----------------------------------------------------------------

    def _log_event(self, sensor, event, **fields):
        """Append a single line to session_events.log.

        Format: ISO8601 wall time + name + event + key=value pairs.
        Used for both start/died/restarted lifecycle events and the
        restart-budget backoff notice.

        Persistence: the log is line-buffered and fsync'd on every write
        so that if the Pi loses power mid-session, the last restart event
        made it to disk. Session_events is the primary source of truth
        for reconstructing "what happened" without the systemd journal.
        """
        line = "{ts} {sensor} {event}".format(
            ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            sensor=sensor,
            event=event,
        )
        for k, v in fields.items():
            line += f" {k}={v}"
        line += "\n"

        # Serialize concurrent writes from different watchdog threads.
        with self._events_lock:
            self._events_file.write(line)
            self._events_file.flush()
            try:
                os.fsync(self._events_file.fileno())
            except OSError:
                pass

    def _clear_device(self, device_path):
        """Best-effort SIGKILL any process holding a hardware device between
        restarts. Called just before relaunching a crashed sensor subprocess.

        Between restarts within a session, the previous subprocess has
        already exited (that's why we're restarting), but audio + camera
        subprocesses have been observed leaving their PCM / video device
        held for a beat after exit (ALSA close is not synchronous with
        process exit). This gives us a firm reset before the new instance
        tries to open the device."""
        try:
            subprocess.run(
                ["sudo", "-n", "fuser", "-k", device_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    def _monitor_subprocess(self, name, launcher_fn,
                             max_restarts=30, window_s=300,
                             pre_restart_devices=()):
        """Watch a subprocess and restart it if it dies while we're still
        running. Runs in its own daemon thread; caller invokes launcher_fn()
        once before calling this to bootstrap.

        Restart budget is a rolling token bucket: `max_restarts` restarts
        allowed within any `window_s`-second window. When exceeded, back off
        until the oldest restart timestamp falls out of the window. This
        stops runaway restart storms when hardware is truly broken (camera
        cable pulled, ALSA device unrecoverable, SD card returning EIO) but
        stays generous for real recovery patterns.

        `pre_restart_devices` is a list of device node paths to `fuser -k`
        before each relaunch — for subprocesses whose crash tends to leak
        exclusive hardware locks (audio PCM, video devices).
        """
        restart_times = []  # rolling timestamps of past restarts

        while self.running:
            proc = self.processes.get(name)
            if proc is None:
                time.sleep(0.5)
                continue

            # Block until this specific proc exits. Any wait() the launcher
            # thread does won't interfere — Popen.wait() is race-safe on
            # separate threads.
            try:
                rc = proc.wait()
            except Exception:
                time.sleep(1)
                continue

            if not self.running:
                # Operator-initiated stop; that's not a crash.
                return

            # Exit status 0 means the child decided to quit on its own — a
            # clean voluntary exit, not a crash. Restarting it would loop
            # forever (see ewe3/ewe8 2026-07-09: dual_cam_jp2 exited 0 on
            # a camera-detection edge case, we restarted 10 times before
            # hitting the budget). Log and stop watching this sensor;
            # other sensors keep going.
            if rc == 0:
                self._log_event(name, "exited_clean", exit_code=rc)
                return

            # Non-zero exit: it's a crash. Check restart budget.
            now = time.monotonic()
            restart_times = [t for t in restart_times if now - t < window_s]
            if len(restart_times) >= max_restarts:
                oldest = restart_times[0]
                wait_s = max(1.0, window_s - (now - oldest))
                self._log_event(
                    name, "restart_budget_exceeded",
                    exit_code=rc,
                    restarts_in_window=len(restart_times),
                    window_s=window_s,
                    backoff_s=int(wait_s),
                )
                time.sleep(wait_s)
                continue  # loop: re-check budget, maybe try again

            self._log_event(name, "died", exit_code=rc,
                             restart_count=len(restart_times) + 1)

            for dev in pre_restart_devices:
                self._clear_device(dev)

            try:
                launcher_fn()
                restart_times.append(time.monotonic())
                new_proc = self.processes.get(name)
                pid = new_proc.pid if new_proc else "?"
                self._log_event(name, "restarted", pid=pid)
            except Exception as e:
                self._log_event(name, "restart_failed", error=repr(e))
                time.sleep(3)

    def _spawn_stdout_reader(self, name, proc, keyword_filter, prefix):
        """Read a subprocess's merged stdout+stderr in a daemon thread and
        forward lines matching any of `keyword_filter` to our own stdout with
        the given prefix. Exits naturally when the child closes its output
        (either exit or SIGKILL). Called by each launcher_fn."""

        def reader():
            try:
                for line in proc.stdout:
                    line = line.rstrip()
                    # Drop noisy status lines
                    if name == "imu" and line.startswith("[") and "] H:" in line:
                        continue
                    if any(kw in line for kw in keyword_filter):
                        print(f"[{prefix}] {line}")
            except Exception:
                pass

        threading.Thread(target=reader, daemon=True).start()

    def start_imu_logger(self):
        """Start IMU data logger. Restart-on-crash is handled by the generic
        _monitor_subprocess watchdog (see below) — the launcher_fn kills any
        stale process still holding /dev/ttyAMA5, launches, and hands off."""
        print("Starting IMU logger...")

        imu_script = FIRMWARE_DIR / "IMU" / "log_imu_data.py"
        imu_log_dir = self.log_dir / "imu"
        cmd = [
            sys.executable,
            str(imu_script),
            "--port", "/dev/ttyAMA5",
            "--rate", "100"
        ]

        def kill_stale_port_users():
            try:
                result = subprocess.run(
                    ["fuser", "/dev/ttyAMA5"],
                    capture_output=True, text=True, timeout=5
                )
                if result.stdout.strip():
                    pids = result.stdout.strip().split()
                    for pid in pids:
                        pid = pid.strip()
                        if pid.isdigit():
                            print(f"  Killing stale process {pid} on /dev/ttyAMA5")
                            os.kill(int(pid), signal.SIGKILL)
                    time.sleep(0.5)
            except Exception:
                pass

        def launcher():
            kill_stale_port_users()
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(imu_log_dir)
            )
            self.processes['imu'] = proc
            print(f"  PID: {proc.pid}")
            print(f"  Logging to: {imu_log_dir}")
            self._spawn_stdout_reader("imu", proc, prefix="IMU",
                keyword_filter=[
                    "============", "BNO055", "initialized", "Logging to:",
                    "WARNING", "Failed", "Error", "Shutting down", "Logged",
                    "samples to", "Init failed", "retrying", "attempt",
                    "Traceback", "Exception", "Permission"
                ])

        launcher()
        self._log_event("imu", "started", pid=self.processes['imu'].pid)
        threading.Thread(
            target=self._monitor_subprocess,
            args=("imu", launcher),
            kwargs=dict(pre_restart_devices=["/dev/ttyAMA5"]),
            daemon=True,
        ).start()

    def start_audio_recorder(self):
        """Start audio recorder. Each restart writes a new WAV — filename
        gets a `_r{N}` suffix on restarts so we don't clobber the previous
        (partial) WAV. Downstream analysis concatenates by session dir."""
        print("Starting audio recorder...")

        audio_script = FIRMWARE_DIR / "audio" / "record_audio.py"
        # Restart counter lives in the closure so each launch picks a
        # distinct filename. Start with the original naming so the
        # common (no-crash) case doesn't have an _r0 suffix.
        restart_n = [0]

        def launcher():
            if restart_n[0] == 0:
                audio_file = self.log_dir / f"audio_{self.session}.wav"
            else:
                audio_file = (self.log_dir /
                              f"audio_{self.session}_r{restart_n[0]}.wav")
            restart_n[0] += 1

            proc = subprocess.Popen(
                [sys.executable, str(audio_script), "--output", str(audio_file)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            self.processes['audio'] = proc
            print(f"  PID: {proc.pid}")
            print(f"  Recording to: {audio_file}")
            self._spawn_stdout_reader("audio", proc, prefix="AUDIO",
                keyword_filter=[
                    "Recording to:", "Recording stopped", "Recording saved",
                    "Error", "WARNING", "Failed", "Aborted"
                ])

        launcher()
        self._log_event("audio", "started", pid=self.processes['audio'].pid)
        threading.Thread(
            target=self._monitor_subprocess,
            args=("audio", launcher),
            kwargs=dict(pre_restart_devices=["/dev/snd/pcmC0D0c"]),
            daemon=True,
        ).start()

    def start_camera_recorder(self):
        """Start dual camera recorder. Each restart writes into a suffixed
        subdirectory (`camera_r{N}/`) so we don't clobber H.264 or timestamps
        from the previous instance."""
        print("Starting dual camera recorder...")

        restart_n = [0]

        def launcher():
            if restart_n[0] == 0:
                camera_output_dir = self.log_dir / "camera"
            else:
                camera_output_dir = self.log_dir / f"camera_r{restart_n[0]}"
                camera_output_dir.mkdir(exist_ok=True)
            restart_n[0] += 1

            cmd = [
                sys.executable,
                "-c",
                f"""
import sys
sys.path.insert(0, '{FIRMWARE_DIR / "dualcam"}')

import dual_cam_jp2
from pathlib import Path

# Override the MinimalRecorder __init__ to use our output directory
original_init = dual_cam_jp2.MinimalRecorder.__init__

def patched_init(self):
    self.cam1 = None
    self.cam2 = None
    self.out1 = None
    self.out2 = None
    self.running = False
    self.session = "{self.session}"
    self.dir = Path("{camera_output_dir}")
    self.dir.mkdir(parents=True, exist_ok=True)

dual_cam_jp2.MinimalRecorder.__init__ = patched_init

dual_cam_jp2.main()
"""
            ]

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            self.processes['camera'] = proc
            print(f"  PID: {proc.pid}")
            print(f"  Recording to: {camera_output_dir}")
            self._spawn_stdout_reader("camera", proc, prefix="CAMERA",
                keyword_filter=["Error", "WARNING", "Failed", "Traceback",
                                 "Exception", "Aborted"])

        launcher()
        self._log_event("camera", "started", pid=self.processes['camera'].pid)
        threading.Thread(
            target=self._monitor_subprocess,
            args=("camera", launcher),
            kwargs=dict(pre_restart_devices=["/dev/video0", "/dev/video1"]),
            daemon=True,
        ).start()

    def start_fuel_gauge_logger(self):
        """Start fuel gauge logger. I2C is stateless so no device cleanup
        needed between restarts."""
        print("Starting fuel gauge logger...")

        fuel_log_file = self.log_dir / f"fuel_gauge_{self.session}.csv"

        def launcher():
            cmd = [
                sys.executable,
                "-c",
                f"""
import sys
sys.path.insert(0, '{FIRMWARE_DIR / "fuel_gauge"}')
from max17048_test import MAX17048, run_continuous_monitoring

fg = MAX17048(bus_number=1)
print("Fuel gauge initialized")

try:
    run_continuous_monitoring(fg, duration=315360000, interval=2, log_file='{fuel_log_file}')
finally:
    fg.close()
"""
            ]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            self.processes['fuel_gauge'] = proc
            print(f"  PID: {proc.pid}")
            print(f"  Logging to: {fuel_log_file}")
            self._spawn_stdout_reader("fuel_gauge", proc, prefix="FUEL",
                keyword_filter=[
                    "Connected to", "initialized", "Logging to:",
                    "Monitoring stopped", "Completed", "readings",
                    "WARNING", "Failed"
                ])

        launcher()
        self._log_event("fuel_gauge", "started",
                         pid=self.processes['fuel_gauge'].pid)
        threading.Thread(
            target=self._monitor_subprocess,
            args=("fuel_gauge", launcher),
            daemon=True,
        ).start()

    def start_gps_logger(self):
        """Start GPS logger"""
        print("Starting GPS logger...")

        gps_log_dir = self.log_dir / "gps"
        gps_script = FIRMWARE_DIR / "gps-test" / "gps_logger.py"
        cmd = [
            sys.executable,
            str(gps_script),
            "--port", "/dev/ttyAMA4",
            "--baud", "460800",
            "--log-dir", str(gps_log_dir),
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        self.processes['gps'] = proc
        print(f"  PID: {proc.pid}")
        print(f"  Logging to: {gps_log_dir}")

        def monitor():
            for line in proc.stdout:
                line = line.rstrip()
                if any(kw in line for kw in [
                    "Connected", "Logging", "Fix:", "NTRIP", "error",
                    "failed", "stopped", "Statistics"
                ]):
                    print(f"[GPS] {line}")
        threading.Thread(target=monitor, daemon=True).start()

    def start_all(self, enable_gps=True):
        """Start all processes"""
        print("=" * 70)
        print("SENSOR TEST - UNIFIED DATA LOGGER")
        print("=" * 70)
        print(f"Session: {self.session}")
        print(f"Log directory: {self.log_dir}")
        print("=" * 70)
        print()

        self.running = True

        # Mark the session start in the events log so downstream analyzers
        # (and post-hoc journalctl searches) can bracket a session cleanly.
        self._log_event("session", "started",
                         pid=os.getpid(), dir=self.log_dir.name)

        # Start clock-sync logger before any sensor so the log spans the
        # full session including sensor startup.
        threading.Thread(
            target=self._clock_sync_worker,
            args=(self.log_dir / "clock_sync.csv",),
            daemon=True,
        ).start()

        try:
            # Start cameras first — they take longest to initialize and
            # their init interferes with the IMU's UART.
            self.start_camera_recorder()
            time.sleep(3)  # Wait for camera init to finish

            self.start_audio_recorder()
            time.sleep(1)

            self.start_imu_logger()
            time.sleep(1)

            self.start_fuel_gauge_logger()
            time.sleep(1)

            if enable_gps:
                self.start_gps_logger()
                time.sleep(1)

            print()
            print("=" * 70)
            print("ALL PROCESSES STARTED SUCCESSFULLY")
            print("=" * 70)
            print("Press Ctrl+C to stop all processes")
            print("=" * 70)
            print()

            self._monitor_processes()

        except Exception as e:
            print(f"\n  Error starting processes: {e}")
            import traceback
            traceback.print_exc()
            self.stop_all()
            return 1

        return 0

    def _monitor_processes(self):
        """Main-thread idle loop that keeps the process alive so signals
        (SIGINT/SIGTERM from systemd or an operator's Ctrl-C) can fire and
        trigger stop_all(). Per-subprocess crash detection + restart is
        handled by the _monitor_subprocess watchdog threads started by each
        launcher; see session_events.log for a real audit trail. Nothing
        to do here except sleep."""
        while self.running:
            time.sleep(5)

    def stop_all(self):
        """Stop all processes gracefully"""
        if not self.running:
            return

        print("\n" + "=" * 70)
        print("STOPPING ALL PROCESSES...")
        print("=" * 70)

        self.running = False

        for name, proc in self.processes.items():
            if proc.poll() is None:
                print(f"Stopping {name}...")
                try:
                    proc.send_signal(signal.SIGINT)
                except Exception as e:
                    print(f"  Error sending signal to {name}: {e}")

        print("\nWaiting for processes to exit gracefully...")
        for name, proc in self.processes.items():
            try:
                proc.wait(timeout=10)
                print(f"  {name} stopped")
            except subprocess.TimeoutExpired:
                print(f"  {name} did not stop gracefully, forcing...")
                proc.kill()
                proc.wait()

        # Final event-log entry + close so the file is complete on disk even
        # if we get killed by a lingering signal a moment from now.
        try:
            self._log_event("session", "stopped")
            self._events_file.close()
        except Exception:
            pass

        print("\n" + "=" * 70)
        print("ALL PROCESSES STOPPED")
        print("=" * 70)
        print(f"\nAll data saved to: {self.log_dir}/")
        print("\nSummary:")
        print("  - Clock sync: " + str(self.log_dir / "clock_sync.csv"))
        print("  - IMU logs: " + str(self.log_dir / "imu/"))
        print("  - Audio: " + str(self.log_dir / f"audio_{self.session}.wav"))
        print("  - Camera: " + str(self.log_dir / "camera/"))
        print("  - Fuel gauge: " + str(self.log_dir / f"fuel_gauge_{self.session}.csv"))
        print("  - Events: " + str(self.log_dir / "session_events.log"))
        if 'gps' in self.processes:
            print("  - GPS: " + str(self.log_dir / "gps/"))
        print("=" * 70)


def main():
    """Main entry point"""
    import argparse
    parser = argparse.ArgumentParser(description="EweGo unified sensor logger")
    parser.add_argument('--no-gps', action='store_true', help='Disable GPS logger (if GPS not installed)')
    args = parser.parse_args()

    # Clean-slate every launch: kill any previous record_sensors.py + orphaned
    # child recorders, free the hardware nodes. Without this we'd produce a
    # session dir with only clock_sync.csv when hardware is already held.
    # See ensure_clean_start() for the full rationale.
    ensure_clean_start()

    test = BatteryLifeTest()

    try:
        return test.start_all(enable_gps=not args.no_gps)
    except KeyboardInterrupt:
        print("\n\nInterrupt received...")
        test.stop_all()
        return 0
    except Exception as e:
        print(f"\n  Fatal error: {e}")
        import traceback
        traceback.print_exc()
        test.stop_all()
        return 1


if __name__ == "__main__":
    sys.exit(main())
