#!/usr/bin/env python3
"""
Minimal dual camera recorder with RAW timestamp logging to disk
Writes timestamps as raw float64 binary data directly to disk
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


# --- Camera / Recording code ------------------------------------------------

try:
    from picamera2 import Picamera2
    # from picamera2.encoders import H264Encoder
    # from picamera2.encoders import MJPEGEncoder
    from picamera2.encoders import JpegEncoder
    from picamera2.outputs import FileOutput
except ImportError:
    print("Error: Install picamera2")
    sys.exit(1)


class RawTimestampOutput(FileOutput):
    """Write raw timestamps directly to disk"""

    def __init__(self, video_file, timestamp_file, camera_id):
        super().__init__(video_file)
        self.camera_id = camera_id
        self.ts_file = open(timestamp_file, 'wb')  # Binary write
        self.last_ts = None
        self.count = 0

        # Stats tracking
        self.intervals = []
        self.interval_sum = 0
        self.interval_min = float('inf')
        self.interval_max = 0

    def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=None):
        # Convert to seconds (float64)
        ts = timestamp / 1e6 if timestamp else 0.0

        # Write raw timestamp to disk immediately (8 bytes, little-endian int64)
        self.ts_file.write(struct.pack('<q', timestamp))
        self.count += 1

        # Calculate interval for stats
        if self.last_ts is not None:
            interval = (ts - self.last_ts) * 1000  # ms
            self.intervals.append(interval)
            self.interval_sum += interval
            if interval < self.interval_min:
                self.interval_min = interval
            if interval > self.interval_max:
                self.interval_max = interval

        self.last_ts = ts

        # Write video frame
        return super().outputframe(frame, keyframe, timestamp, packet, audio)

    def get_stats(self):
        """Get interval statistics"""
        if not self.intervals:
            return None
        return {
            'count': len(self.intervals),
            'avg': self.interval_sum / len(self.intervals),
            'min': self.interval_min,
            'max': self.interval_max
        }

    def close(self):
        """Close timestamp file"""
        self.ts_file.close()


class MinimalRecorder:
    def __init__(self):
        self.cam1 = None
        self.cam2 = None
        self.out1 = None
        self.out2 = None
        self.running = False

        # Output directory
        self.session = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.dir = Path(f"recordings/{self.session}")
        self.dir.mkdir(parents=True, exist_ok=True)

    def start(self):
        """Initialize and start recording"""
        print("Initializing cameras...")

        # Camera 1
        self.cam1 = Picamera2(0)
        config1 = self.cam1.create_video_configuration(
            main={"size": (1920, 1080), "format": "RGB888"},
            controls={"FrameRate": 30},
            buffer_count=16
        )
        self.cam1.configure(config1)

        # Camera 2
        self.cam2 = Picamera2(1)
        config2 = self.cam2.create_video_configuration(
            main={"size": (1920, 1080), "format": "RGB888"},
            controls={"FrameRate": 30},
            buffer_count=16
        )
        self.cam2.configure(config2)

        print("Starting recording...")

        # Create outputs with raw timestamp files
        self.out1 = RawTimestampOutput(
            str(self.dir / "camera1.mjpeg"),
            str(self.dir / "camera1_timestamps.bin"),
            camera_id=1
        )
        self.out2 = RawTimestampOutput(
            str(self.dir / "camera2.mjpeg"),
            str(self.dir / "camera2_timestamps.bin"),
            camera_id=2
        )

        # Create encoders
        enc1 = JpegEncoder()
        enc2 = JpegEncoder()

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
        print(f"Timestamps: camera1_timestamps.bin, camera2_timestamps.bin")
        print(f"Format: Raw binary float64 (8 bytes per timestamp)")
        print(f"Expected interval: 33.33ms @ 30fps\n")

        # Start stats thread
        threading.Thread(target=self.print_stats, daemon=True).start()

    def print_stats(self):
        """Print statistics every second and update OLED if present."""
        while self.running:
            time.sleep(10)

            stats1 = self.out1.get_stats()
            stats2 = self.out2.get_stats()

            if stats1:
                # Camera 1
                print(f"CAM1: {stats1['count']:4d}f | "
                      f"avg={stats1['avg']:5.1f}ms | "
                      f"min={stats1['min']:5.1f}ms | "
                      f"max={stats1['max']:6.1f}ms")

            if stats2:
                # Camera 2
                print(f"CAM2: {stats2['count']:4d}f | "
                      f"avg={stats2['avg']:5.1f}ms | "
                      f"min={stats2['min']:5.1f}ms | "
                      f"max={stats2['max']:6.1f}ms")

                print()


    def stop(self):
        """Stop recording"""
        if not self.running:
            return

        print("\nStopping...")
        self.running = False

        # Stop recording
        self.cam1.stop_recording()
        self.cam2.stop_recording()

        # Stop cameras
        self.cam1.stop()
        self.cam2.stop()

        # Close timestamp files
        self.out1.close()
        self.out2.close()

        # Final stats
        stats1 = self.out1.get_stats()
        stats2 = self.out2.get_stats()

        print(f"\nFinal Statistics:")
        if stats1:
            print(f"Camera 1: {self.out1.count} frames")
            print(f"  Avg: {stats1['avg']:.2f}ms, Min: {stats1['min']:.2f}ms, Max: {stats1['max']:.2f}ms")
        else:
            print("Camera 1: no stats (not enough frames)")

        if stats2:
            print(f"Camera 2: {self.out2.count} frames")
            print(f"  Avg: {stats2['avg']:.2f}ms, Min: {stats2['min']:.2f}ms, Max: {stats2['max']:.2f}ms")
        else:
            print("Camera 2: no stats (not enough frames)")

        print(f"\nFiles saved to: {self.dir}")
        print(f"  camera1.mjpeg, camera1_timestamps.bin")
        print(f"  camera2.mjpeg, camera2_timestamps.bin")

        # Close cameras
        self.cam1.close()
        self.cam2.close()


def main():
    print("=" * 60)
    print("MINIMAL DUAL CAMERA RECORDER - RAW TIMESTAMPS")
    print("=" * 60)
    print()

    recorder = MinimalRecorder()

    # Signal handler
    def signal_handler(sig, frame):
        print("\n\nInterrupt received...")
        recorder.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    # Start recording
    try:
        recorder.start()

        # Wait for 'q' or Ctrl+C
        print("Press 'q' + Enter to stop, or Ctrl+C")
        while True:
            try:
                cmd = input()
                if cmd.lower() == 'q':
                    break
            except EOFError:
                break

        recorder.stop()

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        recorder.stop()


if __name__ == "__main__":
    main()


