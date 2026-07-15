#!/usr/bin/env python3
"""
Minimal dual camera recorder with RAW timestamp logging to disk
Writes timestamps as raw int64 binary data directly to disk

Uses hardware H.264 encoder to offload CPU and avoid timestamp gaps
caused by CPU-intensive MJPEG encoding + SD card I/O stalls.
"""

import time
import signal
import sys
import struct
import threading
from pathlib import Path
from datetime import datetime
import shutil
import subprocess
import os


# --- systemd status integration -------------------------------------------
# If this program is launched by systemd with Type=notify, we can expose a
# compact runtime status line in `systemctl status <service>`.
# This is a no-op when not running under systemd (no NOTIFY_SOCKET) or when
# systemd-notify is not available.
_SYSTEMD_NOTIFY = shutil.which("systemd-notify")


def _systemd_notify(msg: str) -> None:
    if not _SYSTEMD_NOTIFY:
        return
    if "NOTIFY_SOCKET" not in os.environ:
        return
    try:
        subprocess.run([_SYSTEMD_NOTIFY, msg], check=False)
    except Exception:
        # Never allow status updates to affect recording.
        pass


def systemd_ready() -> None:
    _systemd_notify("READY=1")


def systemd_set_status(status: str) -> None:
    _systemd_notify(f"STATUS={status}")


# --- Camera / Recording code ------------------------------------------------

try:
    from picamera2 import Picamera2
    from picamera2.encoders import H264Encoder
    from picamera2.outputs import FileOutput
except ImportError:
    print("picamera2 not installed. Install with: sudo apt install python3-picamera2")
    sys.exit(1)


TS_MAGIC = b"EWEGOTS2"  # 8 bytes — marks the "<qq" (pi_us, rebased_us) format.


class RawTimestampOutput(FileOutput):
    """
    FileOutput subclass that writes raw int64 timestamps to a separate .bin file
    and tracks inter-frame intervals for stats.
    """

    def __init__(self, video_file, timestamp_file, camera_id):
        super().__init__(video_file)
        self.camera_id = camera_id
        self.ts_file = open(timestamp_file, "wb", buffering=0)  # Unbuffered: survives power loss
        self.ts_file.write(TS_MAGIC)  # Format sentinel — distinguishes from old <q-only files.
        self.last_ts = None
        self.count = 0
        # Per-camera stall watchdog: monotonic-us timestamp of the last frame
        # accepted. `_camera_stall_watchdog` compares this against wall time
        # and exits non-zero if the gap exceeds a threshold — surfaces a
        # single-camera silent death (see ewe5 2026-07-09: cam2 stopped
        # delivering frames ~25s in, wrapper stayed alive with only cam1
        # recording).
        self.last_frame_pi_us = 0

        # Stats tracking (guarded by _lock)
        self._lock = threading.Lock()
        self.intervals = []
        self.interval_sum = 0
        self.interval_min = float("inf")
        self.interval_max = 0

    def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=None):
        # picamera2's `timestamp` here is µs since this encoder's first frame —
        # NOT since boot. See picamera2/encoders/encoder.py::_timestamp: it derives
        # from V4L2 SensorTimestamp (CLOCK_MONOTONIC) but subtracts self.firsttimestamp
        # per encoder. Do NOT "simplify" by dropping the pi_us column — see
        # Firmware/bugs/camera_timestamp_rebase.md.
        #
        # Format: two int64 µs values per frame ("<qq"):
        #   pi_us      = time.monotonic_ns()//1000 at outputframe time — global
        #                CLOCK_MONOTONIC anchor, same domain as IMU/audio/GPS/fuel
        #                gauge. Has ~few ms of callback jitter vs the actual SoF.
        #   rebased_us = picamera2's SoF-accurate delta since this encoder's
        #                first frame (always 0 on frame 0).
        # Reconstruction: global_us[i] = pi_us[0] + rebased_us[i].
        pi_us = time.monotonic_ns() // 1000
        rebased_us = timestamp or 0

        self.ts_file.write(struct.pack("<qq", pi_us, rebased_us))
        self.count += 1
        self.last_frame_pi_us = pi_us

        # Convert to seconds for interval stats (use rebased for SoF-accurate deltas)
        ts = rebased_us / 1e6

        if self.last_ts is not None:
            interval = (ts - self.last_ts) * 1000  # ms
            with self._lock:
                self.intervals.append(interval)
                self.interval_sum += interval
                if interval < self.interval_min:
                    self.interval_min = interval
                if interval > self.interval_max:
                    self.interval_max = interval

        self.last_ts = ts

        return super().outputframe(frame, keyframe, timestamp, packet, audio)

    def get_stats(self):
        """Get current statistics and reset tracking"""
        with self._lock:
            if not self.intervals:
                return None

            stats = {
                "count": self.count,
                "avg": self.interval_sum / len(self.intervals),
                "min": self.interval_min,
                "max": self.interval_max,
            }

            # Reset interval tracking (keep count)
            self.intervals = []
            self.interval_sum = 0
            self.interval_min = float("inf")
            self.interval_max = 0

            return stats

    def close(self):
        """Close timestamp file"""
        self.ts_file.close()
        return super().close()


class MinimalRecorder:
    def __init__(self):
        self.cam1 = None
        self.cam2 = None
        self.out1 = None
        self.out2 = None
        self.running = False

        # Output directory (new session per start)
        self.session = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.dir = Path(f"recordings/{self.session}")
        self.dir.mkdir(parents=True, exist_ok=True)

    def start(self):
        """Initialize and start recording"""
        print("Initializing cameras...")

        # Hardware pre-check: refuse to launch (voluntary exit_code=0) if
        # fewer than 2 cameras are on the bus. Restarting doesn't help when
        # the cable's out — record_sensors.py's _monitor_subprocess treats
        # exit_code=0 as clean quit and stops the retry loop. On 2026-07-09
        # ewe3 had 1/2 and ewe8 had 0/2 cameras; the retry budget got hit.
        cams = Picamera2.global_camera_info()
        if len(cams) < 2:
            print(
                f"[FATAL] Only {len(cams)} camera(s) detected on i2c bus; "
                f"need 2. Check ribbon cables and sensor headers. "
                f"Exiting cleanly — parent will not restart.",
                flush=True,
            )
            systemd_set_status(f"Missing camera(s) — detected {len(cams)}/2")
            sys.exit(0)

        # Camera 1
        self.cam1 = Picamera2(0)
        config1 = self.cam1.create_video_configuration(
            main={"size": (1920, 1080), "format": "YUV420"},
            controls={"FrameRate": 24},
            buffer_count=32,
        )
        self.cam1.configure(config1)

        # Camera 2
        self.cam2 = Picamera2(1)
        config2 = self.cam2.create_video_configuration(
            main={"size": (1920, 1080), "format": "YUV420"},
            controls={"FrameRate": 24},
            buffer_count=32,
        )
        self.cam2.configure(config2)

        print("Starting recording...")

        # Create outputs with raw timestamp files
        self.out1 = RawTimestampOutput(
            str(self.dir / "camera1.h264"),
            str(self.dir / "camera1_timestamps.bin"),
            camera_id=1,
        )
        self.out2 = RawTimestampOutput(
            str(self.dir / "camera2.h264"),
            str(self.dir / "camera2_timestamps.bin"),
            camera_id=2,
        )

        # Create H.264 encoders (hardware GPU — offloads CPU entirely)
        enc1 = H264Encoder(bitrate=12_000_000)
        enc2 = H264Encoder(bitrate=12_000_000)

        # Start cameras
        self.cam1.start()
        time.sleep(0.1)
        self.cam2.start()
        time.sleep(0.1)

        # Start recording
        self.cam1.start_recording(enc1, self.out1)
        self.cam2.start_recording(enc2, self.out2)

        self.running = True
        print(f"Recording to: {self.dir}")
        print("Timestamps: camera1_timestamps.bin, camera2_timestamps.bin")
        print("Format: Raw binary <qq (pi_us, rebased_us), 16 bytes per frame")
        print("Encoder: H.264 hardware (12 Mbps per camera)")
        print("Expected interval: 41.67ms @ 24fps\n")

        # Update systemd service status (optional; requires Type=notify)
        systemd_set_status(f"Recording to {self.dir}")
        systemd_ready()

        # Start stats thread
        threading.Thread(target=self.print_stats, daemon=True).start()

        # Start per-camera stall watchdog. If either camera stops delivering
        # frames while we're still "running", exit non-zero so the parent
        # (record_sensors.py's _monitor_subprocess) can restart us.
        threading.Thread(
            target=self._camera_stall_watchdog, daemon=True
        ).start()

    def _camera_stall_watchdog(self, stall_threshold_s=5.0, startup_grace_s=8.0):
        """Watch both outputs for frame delivery.

        `stall_threshold_s`: if either camera's `last_frame_pi_us` is older
        than this vs. our wall reading of monotonic_ns, treat it as dead.
        5s = ~120 frames dropped at 24 fps, well past any realistic hiccup.

        `startup_grace_s`: don't fire during the first N seconds — the first
        `outputframe` callback can lag several seconds after `start_recording`
        while the encoder pipeline primes.
        """
        start = time.monotonic()
        while self.running:
            time.sleep(1.0)
            if time.monotonic() - start < startup_grace_s:
                continue
            now_us = time.monotonic_ns() // 1000
            for label, out in (("cam1", self.out1), ("cam2", self.out2)):
                if out is None:
                    continue
                last = out.last_frame_pi_us
                if last == 0:
                    # No frame yet after grace period → cam never delivered.
                    age_s = time.monotonic() - start
                else:
                    age_s = (now_us - last) / 1e6
                if age_s > stall_threshold_s:
                    print(
                        f"[watchdog] {label} stalled: last frame "
                        f"{age_s:.1f}s ago (threshold {stall_threshold_s}s). "
                        f"Exiting non-zero for parent restart.",
                        flush=True,
                    )
                    systemd_set_status(f"{label} stalled — exiting for restart")
                    # Bypass normal stop() (which may itself hang on the dead
                    # camera). os._exit skips atexit/teardown but the OS
                    # cleans up file descriptors and cgroup members.
                    os._exit(2)

    def print_stats(self):
        """Print statistics every period and update systemd status if present."""
        while self.running:
            time.sleep(1)

            stats1 = self.out1.get_stats() if self.out1 else None
            stats2 = self.out2.get_stats() if self.out2 else None

            if stats1:
                # Camera 1
                print(
                    f"CAM1: {stats1['count']:4d}f | "
                    f"avg={stats1['avg']:5.1f}ms | "
                    f"min={stats1['min']:5.1f}ms | "
                    f"max={stats1['max']:6.1f}ms"
                )

            if stats2:
                # Camera 2
                print(
                    f"CAM2: {stats2['count']:4d}f | "
                    f"avg={stats2['avg']:5.1f}ms | "
                    f"min={stats2['min']:5.1f}ms | "
                    f"max={stats2['max']:6.1f}ms"
                )
                print()

            # Update systemd status line (compact summary)
            parts = []
            if stats1:
                parts.append(
                    f"cam1 frames={stats1['count']} avg={stats1['avg']:.1f}ms max={stats1['max']:.1f}ms"
                )
            if stats2:
                parts.append(
                    f"cam2 frames={stats2['count']} avg={stats2['avg']:.1f}ms max={stats2['max']:.1f}ms"
                )
            if parts:
                systemd_set_status(" | ".join(parts))

    def stop(self):
        """Stop recording"""
        if not self.running:
            return

        print("\nStopping...")
        systemd_set_status("Stopping")
        self.running = False

        # Stop recording
        try:
            if self.cam1 and self.out1:
                self.cam1.stop_recording()
        except Exception:
            pass
        try:
            if self.cam2 and self.out2:
                self.cam2.stop_recording()
        except Exception:
            pass

        # Stop cameras
        try:
            if self.cam1:
                self.cam1.stop()
        except Exception:
            pass
        try:
            if self.cam2:
                self.cam2.stop()
        except Exception:
            pass

        # Close outputs (closes timestamp file too)
        try:
            if self.out1:
                self.out1.close()
        except Exception:
            pass
        try:
            if self.out2:
                self.out2.close()
        except Exception:
            pass

        print("Stopped.")
        systemd_set_status("Stopped")


def main():
    recorder = MinimalRecorder()

    def signal_handler(sig, frame):
        print("\n\nInterrupt received...")
        recorder.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start recording. Any exception during start() (typical failure mode:
    # picamera2 can't find a camera on the bus, encoder init fails, etc.)
    # must exit non-zero — otherwise record_sensors.py's _monitor_subprocess
    # sees a clean status-0 exit and treats it as voluntary shutdown. On
    # 2026-07-09 ewe3/ewe8 hit this: recorder.start() raised, this except
    # block caught + printed the traceback, main() fell through the end
    # and Python exited 0 → 10× cascade restart (10× is the old budget).
    try:
        recorder.start()

        # If running interactively, allow 'q' to stop.
        # If running under systemd (no TTY), just run until SIGTERM/SIGINT.
        if sys.stdin.isatty():
            print("Press 'q' + Enter to stop, or Ctrl+C")
            while True:
                try:
                    cmd = input()
                    if cmd.lower() == "q":
                        break
                except EOFError:
                    break
            recorder.stop()
        else:
            print("No TTY detected (service mode). Running until SIGTERM.")
            while recorder.running:
                time.sleep(1)

        recorder.stop()

    except Exception as e:
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()
        recorder.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()

