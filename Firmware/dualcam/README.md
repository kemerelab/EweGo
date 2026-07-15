# dual_cam_jp2

## Overview
`dual_cam_jp2.py` is a minimal dual-camera recording program for Raspberry Pi using **Picamera2**.
It captures frames from two CSI cameras simultaneously, writes H.264 video streams using the hardware GPU encoder, and logs raw per-frame timestamps to binary files for precise timing analysis.

Each time the program starts, it creates a **new recording session directory**.

---

## Output
On each start, a new directory is created:

```
recordings/YYYYMMDD_HHMMSS/
├── camera1.h264
├── camera1_timestamps.bin
├── camera2.h264
└── camera2_timestamps.bin
```

Timestamp files contain epoch-aligned little-endian int64 timestamps (microseconds from system monotonic clock). Both cameras share the same time base for synchronization.

---

## Running as a systemd service

### Install location
The recommended installation path is:

```
/opt/dualcam/dual_cam_jp2.py
```

Ensure it is executable:

```bash
sudo chmod +x /opt/dualcam/dual_cam_jp2.py
```

---

### Service file
Create the systemd unit:

```bash
sudo nano /etc/systemd/system/dualcam.service
```

Paste:

```ini
[Unit]
Description=Dual Camera Recorder (Picamera2)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
NotifyAccess=main
User=pi
Group=pi
WorkingDirectory=/opt/dualcam
ExecStart=/usr/bin/python3 -u /opt/dualcam/dual_cam_jp2.py
KillSignal=SIGTERM
Restart=on-failure
RestartSec=2
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Adjust `User`/`Group` if you do not use `pi`.

---

### Enable and start
```bash
sudo systemctl daemon-reload
sudo systemctl enable dualcam.service
sudo systemctl start dualcam.service
```

---

## Start / Stop
```bash
sudo systemctl start dualcam.service
sudo systemctl stop dualcam.service
```

Each `start` creates a new recording directory.

---

## Check status
```bash
systemctl status dualcam.service
```

This shows whether the recorder is running and includes a live status line with frame counts and timing statistics.

---

## View logs (recommended)
```bash
journalctl -u dualcam.service -f
```

This shows camera initialization messages and periodic frame timing statistics.

---

## Notes
- The program runs non-interactively when started as a service.
- Stop recording with `systemctl stop`; files are closed cleanly.
- Uses hardware H.264 encoder at 12 Mbps per camera (minimal CPU load).
- Both cameras run at 1920x1080 @ 24fps (max stutter-free rate on CM4's shared encoder block).

---

## play_with_timestamps.py

Video player for recordings with timestamp-based synchronization.

### Features
- **Auto-detects format**: looks for `.h264` files first, falls back to `.mjpeg` (legacy)
- **Dual camera sync**: builds a frame-pairing map from timestamps so both cameras stay aligned, even with start-time offset or clock drift
- **Seekable playback**: remuxes raw streams to seekable containers (`.mp4` for H.264, `.avi` for MJPEG) via ffmpeg on first run, then caches the result
- **Controls**: SPACE=play/pause, q/ESC=quit, LEFT/a=back 10 frames, RIGHT/d=forward 10 frames, trackbar for seeking

### Usage

```bash
# Dual side-by-side view (default) — run from the camera/ subdir
cd sensor_test_YYYYMMDD_HHMMSS/camera/
uv run python play_with_timestamps.py

# Or point at the session root with --dir (auto-finds camera/ and audio)
uv run python play_with_timestamps.py --dir sensor_test_YYYYMMDD_HHMMSS/

# Single camera
uv run python play_with_timestamps.py 1

# Export dual side-by-side MP4 with audio (auto-detected from session dir)
uv run python play_with_timestamps.py --dir sensor_test_YYYYMMDD_HHMMSS/ --export
uv run python play_with_timestamps.py --dir sensor_test_YYYYMMDD_HHMMSS/ --export output.mp4

# Export without audio
uv run python play_with_timestamps.py --dir sensor_test_YYYYMMDD_HHMMSS/ --export --no-audio

# Convert single camera to MP4
uv run python play_with_timestamps.py 1 --convert --output custom_name.mp4
```

The `--export` path uses ffmpeg natively (no Python frame loop) — fast and lossless remux for the video, re-encoded with libx264 only for the final output. Audio is automatically picked up from `audio_*.wav` in the session directory when `--dir` is used.

### Requirements
- `opencv-python` and `numpy` (`uv run --extra dev` on dev machine)
- `ffmpeg` (installed via apt, dev machine only)

---

## align_streams.py

Cross-device composite exporter. Given one or more session directories, it
renders a single MP4 that shows every device's `cam1 | cam2 | sensor panel`
row stacked vertically, with all mics mixed into one audio track.

### What it does

- **Single device (one `--dir`)**: 2x2 layout — `cam1` and `cam2` on top,
  IMU rolling chart and battery/orientation info panel on the bottom.
- **Multi-device (`--dir` repeated)**: one row per device, aligned on
  wall-clock time via each session's `clock_sync.csv` (monotonic_us ↔
  chrony wall time, 100 Hz). The renderer picks the overlapping wall-time
  window across all devices and composites only that window.
- **Audio**: every device's mic is delayed / trimmed to sit at its true
  wall-time position, then `amix`ed with a volume compensation for the
  N-track division.
- **Clock rectification**: chrony can step the wall clock mid-session by
  hundreds of seconds. `align_streams.py` fits an affine mono→wall mapping
  anchored at the last `clock_sync` sample (where chrony has settled),
  removes step corrections, and re-anchors each device to its clock
  regime at the window centre. Devices that stopped before a late
  fleet-wide correction get a global UTC shift so the displayed clock in
  the header stays honest.

### Anatomy of a real command

```bash
uv run --project ~/Rice/RobinsonLab/Sheep/EweGo \
    python ~/Rice/RobinsonLab/Sheep/EweGo/Firmware/dualcam/align_streams.py \
    --dir ~/Rice/RobinsonLab/Data/EweGo/Michigan_Recordings/Ewego001/sensor_test_20260709_035932 \
    --dir ~/Rice/RobinsonLab/Data/EweGo/Michigan_Recordings/Ewego003/sensor_test_20260709_033418 \
    --dir ~/Rice/RobinsonLab/Data/EweGo/Michigan_Recordings/Ewego004/sensor_test_20260709_033210 \
    --dir ~/Rice/RobinsonLab/Data/EweGo/Michigan_Recordings/Ewego006/sensor_test_20260709_031658 \
    --dir ~/Rice/RobinsonLab/Data/EweGo/Michigan_Recordings/Ewego007/sensor_test_20260709_031046 \
    --height 1620 \
    --output ~/Rice/RobinsonLab/Sheep/EweGo/fiveway_0459_final_1620p30.mp4
```

Piece by piece:

- `uv run --project ~/Rice/RobinsonLab/Sheep/EweGo` — activate the EweGo
  project's venv (pyproject.toml + uv.lock) from any working directory.
  Needed because the recordings live under `~/Rice/RobinsonLab/Data/…`,
  not inside the source tree.
- `python …/Firmware/dualcam/align_streams.py` — absolute path to the
  script so it runs regardless of `cwd`.
- Five `--dir` arguments, one per device — each points at a full
  `sensor_test_YYYYMMDD_HHMMSS/` session dir. The dir must contain
  `clock_sync.csv`, `camera/camera{1,2}.h264`, `camera/camera{1,2}_timestamps.bin`,
  and (optionally) `imu/logs/imu_log_*.csv`, `fuel_gauge_*.csv`, `audio_*.wav`.
  Device labels (`Ewego001`, `Ewego003`, …) are auto-detected from the path.
- `--height 1620` — output height in pixels. Multi-device mode ignores
  `--width` and auto-computes it so each row is exactly one 16:9 cam +
  one 16:9 cam + one sensor panel wide with no wasted space. With 5
  devices and a 48 px header, each row is `(1620 - 48) / 5 = 314 px`
  tall → `cam_w = 558 px` → output is `1674 x 1620`. Bump `--height` to
  get more per-camera detail.
- `--output …/fiveway_0459_final_1620p30.mp4` — final MP4 path. The
  recording session sessions started at `04:59` UTC, hence the filename.

### Common flags

| Flag | Purpose |
| --- | --- |
| `--dir PATH` | Session directory; repeat for multi-device. Required. |
| `--output PATH` | Final MP4. Defaults to `./aligned.mp4` (single) or `./aligned_multi.mp4` (multi). |
| `--height N` | Output height. Width auto-computed in multi-device mode. |
| `--fps N` | Multi-device output frame rate (default 30). Independent of the source cams' 24 fps. |
| `--start SEC` | Skip this many seconds into the overlap window. |
| `--duration SEC` | Cap render to this many seconds of the overlap. |
| `--no-flip` | Skip the default 180° rotation (cameras are mounted upside down). |
| `--no-swap` | Skip cam1/cam2 left/right swap. |
| `--no-audio` | Video only. |
| `--wall-offset DEVICE:SECONDS` | Manually shift one device's wall clock. Use when a device missed a fleet-wide chrony correction and the composite shows a step-mismatched row. Repeatable. Example: `--wall-offset Ewego003:-18.4`. |

### Output layout

Multi-device (N ≥ 2):

```
+-------- header: UTC wall-clock + elapsed --------+
| Ewego001 cam1 | Ewego001 cam2 | Ewego001 panel   |
| Ewego003 cam1 | Ewego003 cam2 | Ewego003 panel   |
| Ewego004 cam1 | Ewego004 cam2 | Ewego004 panel   |
| Ewego006 cam1 | Ewego006 cam2 | Ewego006 panel   |
| Ewego007 cam1 | Ewego007 cam2 | Ewego007 panel   |
+---------------------------------------------------+
```

Each device's panel shows a compass (heading), gravity bubble, linear-
accel arrows, a scrolling audio-amplitude trace with a centered playhead,
and a `label + SOC% + voltage` status line.

Single device: cam1 and cam2 side-by-side on top; IMU rolling chart +
battery/info panel below.

### First-run notes

- The player caches remuxed `.mp4` sidecars next to each `.h264` — the
  first run of a session is slower because ffmpeg has to remux; subsequent
  runs reuse the cached files.
- Recordings from the Michigan Apr 2026 firmware batch have old-format
  camera timestamps (encoder-relative starting at zero — see
  `bugs/002_camera_timestamp_rebase.md`). `align_streams.py` anchors
  those via the `.bin` file's mtime (~±1 s accuracy) and prints a
  `WARNING: old-format (relative) camera timestamps` line.

### Requirements

Same as `play_with_timestamps.py` — `opencv-python`, `numpy`, `ffmpeg`.
