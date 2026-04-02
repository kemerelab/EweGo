#!/usr/bin/env python3
"""
Trim Session - Takes raw sensor data and trims it to the latest common epoch start time across all sensors


Outputs to sensor_test directory
"""

import csv
import json
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path
from datetime import datetime, timezone

def trim_session(log_dir:Path) -> bool:
  """Trim alls ensor data files to a common start time
  Args:
  log_dir: Path to the session directory containing "sync_manifest.json"
  
  Returns:
  True if trimming was succesful, False otherwise
  """
  
  log_dir = Path(log_dir)
  manifest_path = log_dir/"sync_manifest.json"
  
  if not manifest_path.exists():
    print(f"[TRIM] Error: sync_manifest.json not found in {log_dir}")
    return False
  
  with open(manifest_path, "r") as f:
    manifest = json.load(f)
    
  session = manifest["session"]
  epoch_ns = manifest["epoch_ns"]
  sensors = manifest["sensors"]
  
  # Finding the latest start time to base trimming off of
  reported_start_times = {
    name: info for name, info in sensors.items()
    if info.get("t0_ns") is not None
  }
  
  if not reported_start_times:
    print(f"[TRIM] ERROR: No sensor t0 values found in manifest. Cannot trim session.")
    return False
  
  # Handled cases for no/missing t0 values
  missing = [name for name, info in sensors.items() if info.get("t0_ns") is None]
  if missing:
    print(f"[TRIM] WARNING: Missing t0 values for: {','.join(missing)}. Skipping these sensors for trimming.")
    
  latest_t0_ns = max(info["t0_ns"] for info in reported_start_times.values())
  latest_start_iso = _ns_to_iso(latest_t0_ns)
  
  print()
  print("=" * 70)
  print("TRIM SESSION")
  print("=" * 70)
  print(f"  Session         : {session}")
  print(f"  Epoch           : {_ns_to_iso(epoch_ns)}")
  print(f"  Common start    : {latest_start_iso}  (latest t0)")
  print()
  print(f"  {'Sensor':<14} {'t0 offset from epoch':>22}  {'trim amount':>16}")
  print(f"  {'-'*14} {'-'*22}  {'-'*16}")
  for name, info in reported_start_times.items():
      offset_ms = (info["t0_ns"] - epoch_ns)  / 1_000_000
      trim_ms   = (latest_t0_ns - info["t0_ns"]) / 1_000_000
      print(f"  {name:<14} {offset_ms:>+20.3f} ms  {trim_ms:>14.3f} ms")
  print()
  
  # Add to seperate directory for now, once data can be verified, we can write everything to the existing directory for "sensor_test"
  trimmed_dir = log_dir / "trimmed"
  trimmed_dir.mkdir(exist_ok=True)
  print(f"  Output dir: {trimmed_dir}")
  print()
  
  all_ok =True
  # Trimming each sensor file from manifest
  if "audio"  in reported_start_times:
    ok = _trim_audio(log_dir, session, latest_t0_ns, reported_start_times["audio"]["t0_ns"],trimmed_dir)
    all_ok = all_ok and ok
    
  # Trimming each sensor file from manifest
  if "imu"  in reported_start_times:
    ok = _trim_imu(log_dir,latest_t0_ns,trimmed_dir)
    all_ok = all_ok and ok
    
  # Trimming each sensor file from manifest
  if "camera"  in reported_start_times:
    ok = _trim_camera(log_dir, session, latest_t0_ns, reported_start_times["camera"]["t0_ns"],trimmed_dir)
    all_ok = all_ok and ok
    
  # Trimmed data manifest
  manifest["trim"] ={
    "latest_t0_ns": latest_t0_ns,
    "latest_start_iso": latest_start_iso,
    "trimmed_sensors": list(reported_start_times.keys()),
    "skipped_sensors": missing
  }
  
  out_mainfest = trimmed_dir/"sync_manifest.json"
  with open(out_mainfest, "w") as f:
    json.dump(manifest, f, indent=2)
  print(f"[TRIM] Manifest written: {out_mainfest}")
  
  print()
  print("=" * 70)
  print(f"TRIM {'COMPLETED' if all_ok else 'COMPLETED WITH ERRORS'}")
  print("=" * 70)
  return all_ok

# -----------------------------------------
# Pre-sensor trimming helper functions
# ------------------------------------------

# Audio
def _trim_audio(log_dir: Path, session:str, latest_t0_ns: int, audio_t0_ns: int, trimmed_dir: Path) -> bool:
  """Trim audio file to start at latest t0 time
    Samples dropped based on WAV file's sample rate and channel count, so trimming is accurate to the nearest collected smaple
  """
  src = log_dir / f"audio_{session}.wav"
  dst = trimmed_dir / f"audio_{session}_trimmed.wav"

  if not src.exists():
    print(f"[TRIM] Error: Audio file not found: {src}")
    return False
  
  trim_ns = latest_t0_ns - audio_t0_ns
  trim_sec = trim_ns/1e9
  
  try:
    with wave.open(str(src), "rb") as r:
      n_channels = r.getnchannels()
      sample_width = r.getsampwidth()
      frame_rate = r.getframerate()
      n_frames =r.getnframes()
      
      frames_to_skip = int(trim_sec * frame_rate)
      
      if frames_to_skip >= n_frames:
        print(f"[AUDIO TRIM] ERROR: trim ({trim_sec:.3f}s) >= total audio "
              f"duration ({n_frames/frame_rate:.3f}s). Nothing to write")
        return False
      
      r.setpos(frames_to_skip)
      remaining_frames = r.readframes(n_frames -frames_to_skip)
    with wave.open(str(dst), "wb") as w:
      w.setnchannels(n_channels)
      w.setsampwidth(sample_width)
      w.setframerate(frame_rate)
      w.writeframes(remaining_frames)
      
    kept_sec = (n_frames - frames_to_skip) / frame_rate
    print(f"  [AUDIO TRIM] {trim_sec*1000:.1f} ms ")
    return True
  except Exception as e:
    print(f"   [AUDIO TRIM] ERROR: {e}")
    return False
  
# IMU
def _trim_imu(log_dir: Path, latest_t0_ns: int, trimmed_dir: Path) -> bool:
  """Drop all IMU csv rows whose timestamp is before the latest t0 time
  """
  imu_dir = log_dir / "imu/logs"
  csv_files = sorted(imu_dir.glob("*.csv"))

  if not csv_files:
    print(f"  [IMU] WARNING: No CSV files found in {imu_dir}, skipping")
    return False
  
  # Making trimmed imu sub directory
  out_imu_dir = trimmed_dir / "imu"
  out_imu_dir.mkdir(exist_ok=True)
  
  all_ok = True
  for src in csv_files:
    dst = out_imu_dir / src.name
  
    try:
      with open(src, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        
        if "timestamp" not in fieldnames:
          print(f"  [IMU]  WARNING: {src.name} has no 'timestamp' column, copying data as is")
          shutil.copy2(src, dst)
          continue
        
        latest_t0_dt = datetime.fromtimestamp(latest_t0_ns / 1e9)
        rows = [row for row in reader
                if datetime.fromisoformat(row["timestamp"]) >= latest_t0_dt]
        
        with open(dst, "w", newline="") as f:
          writer = csv.DictWriter(f, fieldnames=fieldnames)
          writer.writeheader()
          writer.writerows(rows)
        print(f"  [IMU]  Trimmed {src.name} -> {len(rows)} rows kept -> {dst.name}")
        
    except Exception as e:
      print(f"   [IMU] ERROR processing {src.name}: {e}")
      all_ok = False
  return all_ok

# Camera
def _trim_camera(log_dir: Path, session:str, latest_t0_ns: int, camera_t0_ns: int, trimmed_dir: Path) -> bool:
  """
  Trim camera video using ffmpeg -ss to trim offset
  H.264 can only cut at keyframe boundaries
  """
  camera_dir = log_dir / "camera"
  video_files = sorted(camera_dir.glob("*.mp4")) + sorted(camera_dir.glob("*.h264"))
  
  if not video_files:
    print(f"  [TRIM CAMERA] WARNING: NO video files found in {camera_dir}, skipping")
    return False
  
  # Making trimmed camera sub directory
  out_camera_dir = trimmed_dir / "camera"
  out_camera_dir.mkdir(exist_ok=True)
  
  trim_sec = (latest_t0_ns - camera_t0_ns) / 1e9
  
  all_ok = True
  for src in video_files:
    dst =out_camera_dir / src.name
    try:
      # Using ffmepg to trim audio with "seeking" method
      cmd = [
        "ffmpeg",
        "-y",
        "-ss", f"{trim_sec:.6f}", # start time of new video starting after t0
        "-i", str(src),
        "-c", "copy", # skip re-encode, just copy the frames
        str(dst)
      ]
      result = subprocess.run(cmd, capture_output=True, text=True)
      if result.returncode != 0:
        print(f"  [TRIM CAMERA] ERROR (ffmpeg): {result.stderr[-300:]}")
        all_ok = False
      else:
        print(f" [TRIM CAMERA] Trimmed {trim_sec*1000:.1f}ms -> {dst.name}")
    except FileNotFoundError:
      print(f"  [TRIM CAMERA] ERROR: ffmpeg not found. Install with: sudo apt install ffmpeg")
      return False
    except Exception as e:
      print(f"  [CAMERA] ERROR: {e}")
      all_ok = False
  return all_ok


def _ns_to_iso(ns: int) -> str:
  """
  Convert nanosecond time stamp to datetime format
  Args:
      ns (int): _description_

  Returns:
      str: datetime
  """
  from datetime import datetime, timezone
  return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat()

# From CLI
if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 trim_session.py <session_dir>")
        print("  e.g. python3 trim_session.py ../sensor_test_20240222_143055")
        sys.exit(1)

    ok = trim_session(Path(sys.argv[1]))
    sys.exit(0 if ok else 1)