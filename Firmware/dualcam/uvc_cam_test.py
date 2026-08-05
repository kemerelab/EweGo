#!/usr/bin/env python3
"""
Minimal single UVC camera recorder with RAW timestamp logging to disk.
Same structure as dual_cam_jp2_hw.py (which uses picamera2/libcamera for
CSI sensors), but built on OpenCV's V4L2 backend since UVC cameras are
plain V4L2 devices and libcamera/picamera2 will never see them.

Purpose: quick pass/fail check that the U20CAM-1080P-S1 enumerates and
streams over USB host mode before wiring it into the real pipeline.
"""

import time
import signal
import sys
import struct
import threading
import argparse
from pathlib import Path
from datetime import datetime
import shutil
import subprocess
import os

try:
    import cv2
except ImportError:
    print("opencv not installed. Install with: uv add opencv-python-headless")
    sys.exit(1)


# --- systemd status integration (unchanged from dual_cam_jp2_hw.py) -------
_SYSTEMD_NOTIFY = shutil.which("systemd-notify")


def _systemd_notify(msg: str) -> None:
    if not _SYSTEMD_NOTIFY:
        return
    if "NOTIFY_SOCKET" not in os.environ:
        return
    try:
        subprocess.run([_SYSTEMD_NOTIFY, msg], check=False)
    except Exception:
        pass


def systemd_ready() -> None:
    _systemd_notify("READY=1")


def systemd_set_status(status: str) -> None:
    _systemd_notify(f"STATUS={status}")


# --- Camera / Recording code ------------------------------------------------


class RawTimestampWriter:
    """
    Writes one 8-byte little-endian int64 timestamp (microseconds since
    epoch) per frame to a .bin file, and tracks inter-frame interval stats.
    Mirrors RawTimestampOutput's bookkeeping from dual_cam_jp2_hw.py.
    """

    def __init__(self, timestamp_file):
        self.ts_file = open(timestamp_file, "wb")
        self.last_ts = None
        self.count = 0

        self.intervals = []
        self.interval_sum = 0
        self.interval_min = float("inf")
        self.interval_max = 0

    def record(self, timestamp_us: int):
        self.ts_file.write(struct.pack("<q", timestamp_us))
        self.count += 1

        ts = timestamp_us / 1e6
        if self.last_ts is not None:
            interval = (ts - self.last_ts) * 1000  # ms
            self.intervals.append(interval)
            self.interval_sum += interval
            self.interval_min = min(self.interval_min, interval)
            self.interval_max = max(self.interval_max, interval)
        self.last_ts = ts

    def get_stats(self):
        if not self.intervals:
            return None
        stats = {
            "count": self.count,
            "avg": self.interval_sum / len(self.intervals),
            "min": self.interval_min,
            "max": self.interval_max,
        }
        self.intervals = []
        self.interval_sum = 0
        self.interval_min = float("inf")
        self.interval_max = 0
        return stats

    def close(self):
        self.ts_file.close()


class UvcTestRecorder:
    def __init__(self, device="/dev/video0", width=1920, height=1080, fps=30):
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps

        self.cap = None
        self.writer = None
        self.ts = None
        self.running = False

        self.session = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.dir = Path(f"recordings/{self.session}")
        self.dir.mkdir(parents=True, exist_ok=True)

    def start(self):
        print(f"Opening {self.device} ...")

        # CAP_V4L2 backend explicitly — avoids gstreamer/ffmpeg auto-backend
        # guessing on the Pi, and lets us set FOURCC directly.
        self.cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not self.cap.isOpened():
            raise RuntimeError(
                f"Could not open {self.device}. Check `lsusb`, `dmesg | grep -i uvc`, "
                f"and `v4l2-ctl --list-devices` — camera must enumerate as a USB host "
                f"device before this will work."
            )

        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self.cap.set(cv2.CAP_PROP_FPS, self.fps)

        actual_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = self.cap.get(cv2.CAP_PROP_FPS)
        print(f"Negotiated: {actual_w}x{actual_h} @ {actual_fps:.1f}fps (requested {self.width}x{self.height} @ {self.fps})")

        video_path = str(self.dir / "camera_uvc.avi")
        fourcc = cv2.VideoWriter_fourcc(*"MJPG")
        self.writer = cv2.VideoWriter(video_path, fourcc, actual_fps or self.fps, (actual_w, actual_h))

        self.ts = RawTimestampWriter(str(self.dir / "camera_uvc_timestamps.bin"))

        self.running = True
        print(f"Recording to: {self.dir}")
        print("Timestamps: camera_uvc_timestamps.bin (int64 microseconds, little-endian)")
        print(f"Expected interval: {1000 / self.fps:.2f}ms @ {self.fps}fps\n")

        systemd_set_status(f"Recording to {self.dir}")
        systemd_ready()

        threading.Thread(target=self._capture_loop, daemon=True).start()
        threading.Thread(target=self._print_stats, daemon=True).start()

    def _capture_loop(self):
        while self.running:
            ok, frame = self.cap.read()
            if not ok:
                print("WARN: frame grab failed")
                continue
            timestamp_us = int(time.time() * 1e6)
            self.writer.write(frame)
            self.ts.record(timestamp_us)

    def _print_stats(self):
        while self.running:
            time.sleep(1)
            stats = self.ts.get_stats() if self.ts else None
            if stats:
                print(
                    f"CAM: {stats['count']:4d}f | "
                    f"avg={stats['avg']:5.1f}ms | "
                    f"min={stats['min']:5.1f}ms | "
                    f"max={stats['max']:6.1f}ms"
                )
                systemd_set_status(
                    f"frames={stats['count']} avg={stats['avg']:.1f}ms max={stats['max']:.1f}ms"
                )

    def stop(self):
        if not self.running:
            return
        print("\nStopping...")
        systemd_set_status("Stopping")
        self.running = False
        time.sleep(0.2)  # let capture loop exit its last iteration

        try:
            if self.cap:
                self.cap.release()
        except Exception:
            pass
        try:
            if self.writer:
                self.writer.release()
        except Exception:
            pass
        try:
            if self.ts:
                self.ts.close()
        except Exception:
            pass

        print("Stopped.")
        systemd_set_status("Stopped")


def main():
    parser = argparse.ArgumentParser(description="UVC camera host-mode test")
    parser.add_argument("--device", default="/dev/video0", help="V4L2 device path")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=30)
    args = parser.parse_args()

    recorder = UvcTestRecorder(args.device, args.width, args.height, args.fps)

    def signal_handler(sig, frame):
        print("\n\nInterrupt received...")
        recorder.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        recorder.start()

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


if __name__ == "__main__":
    main()
