#!/usr/bin/env python3
"""
Sensor Test - Unified Data Logger
Runs all sensors in parallel for battery life testing:
- IMU (BNO055) - 50Hz logging
- Audio Recorder - Continuous recording
- Dual Camera - H.264 30fps recording
- Fuel Gauge (MAX17048) - Battery monitoring every 2 seconds

Sync strategy
- Shared epoch (sync_manifest.json) is written before any sensor starts
- Each sensor subprocess records its own t0_ns at its first sample and writes 
  to the manifest via a sidecar file (<sensor>_t0.json)
- Sidecar file acts as an orchestrator which picks up and merges once all sensors are running
- Manifest contains everything to align the streams post-session

Press Ctrl+C to stop all processes gracefully
"""

import subprocess
import signal
import sys
import time
import os
from pathlib import Path
from datetime import datetime, timezone
import threading
import json

# trim session imports
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trim_session import trim_session


# Resolve paths relative to this script's location (Firmware/)
FIRMWARE_DIR = Path(__file__).resolve().parent

# Sidecar filenames for each sensor subprocess
SIDECAR = {
    "audio": "audio_t0.json",
    "camera": "camera_t0.json",
    "imu": "imu_t0.json",
}


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

        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        print("\n\n[SIGNAL] Shutdown requested...")
        self.stop_all()
        sys.exit(0)
        
    # Synchronization helpers
    def _write_sync_manifest(self):
        """
           Captures shared reference epoch before any sensor subprocess starts
           All  t0 values will be stored relative to this epoch so that post-hoc alignment is a simple subtraction
        """
        epoch_ns = time.time_ns()
        epoch_iso = datetime.now(timezone.utc).isoformat()
        
        manifest = {
            "session":    self.session,
            "epoch_ns":   epoch_ns,
            "epoch_iso":  epoch_iso,
            # Each sensor fills in its own t0_ns once it starts sampling.
            "sensors": {
                "audio":      {"t0_ns": None, "offset_ms": None},
                "camera":     {"t0_ns": None, "offset_ms": None},
                "imu":        {"t0_ns": None, "offset_ms": None},
            }
        }
            
        with open(self.manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
        
        print(f"[SYNC] Epoch written : {epoch_iso}")
        print(f"[SYNC] Epoch (ns) : {epoch_ns}")
        print(f"[SYNC] Manifest : {self.manifest_path}")
        return epoch_ns
    
    def _read_manifest(self):
        with open(self.manifest_path) as f:
            return json.load(f)
        
    def _write_manifest(self, manifest):
        with open(self.manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
            
    def _collect_sensor_t0(self, sensor_name, timeout=15):
        """Wait for a sensor subprtocess to write its sidecar t0 file, then merge value into shared manifest
        
           Each sensor script is responsible for writing a small JSON file:
            {"t0_ns": <int>}
           to self.log_dir / SIDECAR[sensor_name] as soon as the first sample is captured

        Args:
            sensor_name (_type_): _description_
            timeout (int, optional): _description_. Defaults to 15.
        """
        sidecar = self.log_dir / SIDECAR[sensor_name]
        deadline = time.monotonic() + timeout
        
        while time.monotonic() < deadline:
            if sidecar.exists():
                try:
                    with open(sidecar) as f:
                        data = json.load(f)
                    t0_ns = int(data["t0_ns"])
                    
                    manifest = self._read_manifest()
                    epoch_ns = manifest["epoch_ns"]
                    offset_ms = (t0_ns - epoch_ns) / 1_000_000
                    
                    # Collect and Write data to manifest
                    manifest["sensors"][sensor_name]["t0_ns"]    = t0_ns
                    manifest["sensors"][sensor_name]["offset_ms"] = round(offset_ms)
                    self._write_manifest(manifest)
                    
                    print(f"[SYNC] {sensor_name:12s} t0 registered  "
                          f"offset from epoch = {offset_ms:+.1f} ms")
                    return True
                except Exception as e:
                    print(f"[SYNC] Warning: could not read {sensor_name} sidecar: {e}")
            time.sleep(.25)
        print(f"[SYNC] WARNING: {sensor_name} did not report t0 within {timeout}s")
        return False
    
    def trim_data(self,sensor_name, data_file):
        """Trim data files to realtive epoch start times"""
        if not self.manifest_path.exists():
            print("[TRIM] No manifest found, cannot trim data streams")
            return
        
        

    def start_imu_logger(self, max_retries=3):
        """Start IMU data logger with retries (backup to IMU's internal retry)"""
        print("Starting IMU logger...")

        imu_script = FIRMWARE_DIR / "IMU" / "log_imu_data.py"
        imu_log_dir = self.log_dir / "imu"
        sidecar_path = self.log_dir / SIDECAR["imu"]
        
        cmd = [
            sys.executable,
            str(imu_script),
            "--port", "/dev/ttyAMA5",
            "--rate", "50",
            "--t0-sidecar", str(sidecar_path),
        ]

        def kill_stale_port_users():
            """Kill any process holding the IMU serial port from a previous run."""
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

        def launch():
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
            return proc

        proc = launch()

        def monitor():
            nonlocal proc
            for attempt in range(max_retries):
                for line in proc.stdout:
                    line = line.rstrip()
                    if line.startswith("[") and "] H:" in line:
                        continue
                    if any(kw in line for kw in [
                        "============", "BNO055", "initialized", "Logging to:",
                        "WARNING", "Failed", "Error", "Shutting down", "Logged",
                        "samples to", "Init failed", "retrying", "attempt", "Traceback",
                        "Exception", "Permission"
                    ]):
                        print(f"[IMU] {line}")

                # Process ended — check if it crashed
                rc = proc.wait()
                if rc == 0 or not self.running:
                    return
                retries_left = max_retries - attempt - 1
                if retries_left > 0:
                    # Kill any zombie that might still hold the serial port
                    try:
                        proc.kill()
                        proc.wait(timeout=2)
                    except Exception:
                        pass
                    print(f"[IMU] Crashed (exit {rc}), retrying in 3s ({retries_left} left)...")
                    time.sleep(3)
                    proc = launch()
                else:
                    print(f"[IMU] Crashed (exit {rc}), no retries left")

        threading.Thread(target=monitor, daemon=True).start()

    def start_audio_recorder(self):
        """Start audio recorder"""
        print("Starting audio recorder...")
        audio_file = self.log_dir / f"audio_{self.session}.wav"
        sidecar_path = self.log_dir / SIDECAR["audio"]
        audio_script = FIRMWARE_DIR / "audio" / "record_audio.py"
        
        # Edit made - define device to record (only way I can record an audio file from pi)
        cmd = [
            sys.executable,
            str(audio_script),
            "--output", str(audio_file),
            "--device", "hw:2,0",
            "--t0-sidecar", str(sidecar_path),
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        self.processes['audio'] = proc
        print(f"  PID: {proc.pid}")
        print(f"  Recording to: {audio_file}")

        def monitor():
            for line in proc.stdout:
                line = line.rstrip()
                if any(kw in line for kw in [
                    "Recording to:", "Recording stopped", "Recording saved",
                    "Error", "WARNING", "Failed", "Aborted"
                ]):
                    print(f"[AUDIO] {line}")
        threading.Thread(target=monitor, daemon=True).start()

    def start_camera_recorder(self):
        """Start dual camera recorder"""
        print("Starting dual camera recorder...")

        camera_output_dir = self.log_dir / "camera"
        sidecar_path = self.log_dir / SIDECAR["camera"]
        cam_script = FIRMWARE_DIR / "dualcam" / "dual_cam_jp2.py"
        cmd = [
            sys.executable,
            "-c",
            f"""
import sys
import time
import json
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
    # Write once recorder object is constructed, even though actual frame may come in later
    t0_ns = time.time_ns()
    sidecar = Path("{sidecar_path}")
    sidecar.write_text(json.dumps({{"t0_ns": t0_ns}}))

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

        def monitor():
            for line in proc.stdout:
                line = line.rstrip()
                if "INFO" in line or "CAM1:" in line or "CAM2:" in line:
                    continue
                if line.strip() and not line.startswith("["):
                    print(f"[CAMERA] {line}")
        threading.Thread(target=monitor, daemon=True).start()

    def start_fuel_gauge_logger(self):
        """Start fuel gauge logger"""
        print("Starting fuel gauge logger...")

        fuel_log_file = self.log_dir / f"fuel_gauge_{self.session}.csv"
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

        def monitor():
            for line in proc.stdout:
                line = line.rstrip()
                if any(kw in line for kw in [
                    "Connected to", "initialized", "Logging to:",
                    "Monitoring stopped", "Completed", "readings",
                    "WARNING", "Failed"
                ]):
                    print(f"[FUEL] {line}")
        threading.Thread(target=monitor, daemon=True).start()

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

        # Write shared epoch before activating any sensor
        self._write_sync_manifest()
        print()

        self.running = True

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
            
            # Collect t0 timestamps
            print()
            print("[SYNC]Collecting per-sensor t0 timestamps...")
            for sensor in ["camera", "audio", "imu"]:
                self._collect_sensor_t0(sensor)
                
           

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
        """Monitor all processes and report if any crash"""
        while self.running:
            time.sleep(5)

            for name, proc in list(self.processes.items()):
                if proc.poll() is not None:
                    print(f"\n  WARNING: {name.upper()} process died (exit code: {proc.returncode})")

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

        print("\n" + "=" * 70)
        print("ALL PROCESSES STOPPED")
        print("=" * 70)
        print(f"\nAll data saved to: {self.log_dir}/")
        print("\nSummary:")
        print("  - Sync manifest: " + str(self.manifest_path))
        print("  - IMU logs: " + str(self.log_dir / "imu/"))
        print("  - Audio: " + str(self.log_dir / f"audio_{self.session}.wav"))
        print("  - Camera: " + str(self.log_dir / "camera/"))
        print("  - Fuel gauge: " + str(self.log_dir / f"fuel_gauge_{self.session}.csv"))
        if 'gps' in self.processes:
            print("  - GPS: " + str(self.log_dir / "gps/"))
        print("=" * 70)
        
        # Trim all data streams to a common start time
        print("\nStarting post-session data trimming...")
        try:
            trim_session(self.log_dir)
        except Exception as e:
            print(f"[TRIM] ERROR during trimming: {e}")
            import traceback
            traceback.print_exc()
        
    def _print_sync_summary(self):
        """Print readable sync alignment summary from the manifest."""
        try:
            manifest = self._read_manifest()
        except Exception:
            return

        print("\n" + "=" * 70)
        print("SYNC SUMMARY")
        print("=" * 70)
        print(f"  Epoch: {manifest['epoch_iso']}  ({manifest['epoch_ns']} ns)")
        print()
        print(f"  {'Sensor':<14} {'t0 offset from epoch':>24}  {'Status'}")
        print(f"  {'-'*14} {'-'*24}  {'-'*10}")
        for sensor, info in manifest["sensors"].items():
            if info["t0_ns"] is not None:
                status = f"{info['offset_ms']:+.1f} ms"
                flag   = "OK"
            else:
                status = "N/A"
                flag   = "MISSING"
            print(f"  {sensor:<14} {status:>24}  {flag}")
        print("=" * 70)


def main():
    """Main entry point"""
    import argparse
    parser = argparse.ArgumentParser(description="EweGo unified sensor logger")
    parser.add_argument('--no-gps', action='store_true', help='Disable GPS logger (if GPS not installed)')
    args = parser.parse_args()

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
