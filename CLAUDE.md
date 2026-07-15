# EweGo - Sheep Monitoring System

## Project Overview
Battery-powered sensor platform on a Raspberry Pi CM4 for monitoring sheep.
Records dual cameras, IMU, audio, GPS, and battery level simultaneously.

## Repository Structure
```
EweGo/
├── Firmware/           # All Pi-side code
│   ├── dualcam/        # Dual camera recorder + player
│   ├── IMU/            # BNO055 IMU logger (UART5)
│   ├── audio/          # Audio recorder (Google AIY Voice Hat)
│   ├── fuel_gauge/     # MAX17048 battery monitor (I2C bus 1)
│   ├── gps-test/       # u-blox ZED-X20P GPS (UART3 + UART4)
│   ├── setup/          # Pi setup script (pi_setup.sh)
│   ├── bugs/           # Known bugs, fixes, and documentation
│   └── record_sensors.py  # Unified orchestrator (runs all sensors)
├── Hardware/           # KiCad PCB design (submodule: eweSAW)
├── sensor_test_*/      # Recording output directories (gitignored)
├── pyproject.toml      # Python deps (pyserial, pyubx2, numpy, sounddevice; opencv as dev extra)
└── uv.lock             # Pinned lock — single source of truth, deployed to Pi (uv sync --frozen)
```

## Key Technical Details

### Dual Camera Recorder (`Firmware/dualcam/dual_cam_jp2.py`)
- Hardware H.264 encoder, 12 Mbps per camera, 1920x1080 @ 24fps
- 24fps is the max stutter-free rate (CM4 has a single shared encoder block)
- 30fps causes ~23% frame doubling; 20fps and 24fps have 0% drops
- Timestamps: raw picamera2 values, µs since boot (64-bit kernel monotonic clock)
- Binary format: little-endian int64 (`<q`), microseconds
- Unbuffered writes (`buffering=0`) for timestamp persistence on power loss

### Player (`Firmware/dualcam/play_with_timestamps.py`)
- Auto-detects .h264 vs .mjpeg (legacy)
- Remuxes to seekable containers via ffmpeg (cached)
- `build_sync_map()` pairs cam1/cam2 frames by closest timestamp
- Strips trailing zero-padding on load (guard against None timestamp on final frame)
- Run from recording dir: `uv run python /path/to/play_with_timestamps.py`

### Sensor Test Orchestrator (`Firmware/record_sensors.py`)
- Launches all sensors as subprocesses with monitoring threads
- Uses `FIRMWARE_DIR = Path(__file__).resolve().parent` for portable paths
- IMU has retry logic (max_retries=3) for UART startup race conditions
- Camera subprocess monkey-patches MinimalRecorder to redirect output dir

### Pi Environment
- picamera2 installed via apt (not pip/uv — python-prctl build fails)
- uv venv with `--system-site-packages` to access apt picamera2
- `/boot/firmware/config.txt` overlays: disable-bt, imx708 x2, googlevoicehat, uart3, uart4, uart5, i2c_arm
- `disable-bt` frees the PL011 so debug console (GPIO 14/15) is stable; Bluetooth unused
- `i2c-dev` kernel module for userspace I2C (fuel gauge on bus 1)
- i2c3 overlay conflicts with i2c_arm on same GPIO 2/3 — don't use both
- GPIO 4/5 = UART3 (GPS secondary), GPIO 8/9 = UART4 (GPS data), GPIO 12/13 = UART5 (IMU)

### Mesh Networking
- B.A.T.M.A.N. Advanced (batman-adv) over IBSS (ad-hoc) mode — no router needed
- The CM4's BCM43455 does NOT support 802.11s; IBSS is the transport layer
- IBSS SSID: `ewego-mesh`, channel 6 (2437 MHz), fixed cell ID `02:12:34:56:78:9A`
- IBSS join uses `NOHT` — `HT20` returns EINVAL on BCM43455 in IBSS mode
- batman-adv kernel module creates virtual `bat0` interface for L2 mesh routing
- Hostname convention: `eweN` (ewe1, ewe2, ...) → mesh IP `10.42.0.N/24` on bat0
- Managed by systemd `ewego-mesh.service` (not NetworkManager)
- NM ignores wlan0 via `/etc/NetworkManager/conf.d/ewego-unmanaged.conf`
- Infrastructure WiFi not available (ad-hoc takes over wlan0; use USB adapter or ethernet)
- Join from laptop: `bash Firmware/setup/mesh_join.sh join 100` → 10.42.0.100
- Verify: `sudo batctl meshif bat0 n` (neighbors), `sudo batctl meshif bat0 o` (originators)

### USB-C SSH (per-device subnets)
- Each Pi advertises a unique /24 on its `usb0`: `10.55.<DEVICE_NUM>.1/24`
  (eweN → 10.55.N.1). Avoids same-subnet routing ambiguity when multiple Pis
  are plugged into one laptop simultaneously.
- Laptop helper: `bash Firmware/setup/ewego_usb.sh [list|up|down|ssh] [N|hostname]`
  - `list` (default): show USB Ethernet ifaces with their current IPs and
    reachability of the configured Pi at `10.55.<N>.1`
  - `up N`: assign `10.55.N.100/24`; with multiple USB ifaces, tries each
    until one's Pi responds at `10.55.N.1`
  - `ssh N`: assign IP if needed, then ssh to `user@10.55.N.1`
- NCM gadget (configfs, `usb_gadget_ncm.sh` + `ewego-usb-gadget.service`),
  installed by `pi_setup.sh` section 6 — replaced g_ether/ECM, whose host-side
  `cdc_ether` driver TX-stalls on newer laptop kernels
- The gadget service assigns the usb0 IP (NM unmanages usb0 — a NM profile
  racing the static IP caused flapping)
- Config.txt booby-traps auto-handled: `otg_mode=1`, `dr_mode=host`, and bare
  `dtoverlay=dwc2` are detected and corrected to `dr_mode=peripheral`
- Cloud-init `preserve_hostname` is flipped to `true` when pi_setup.sh renames
  the host, otherwise the original imager hostname comes back on every reboot
- USB-C SSH and mesh are fully independent (different ifaces, different subnets)

### Known Bugs
- See `Firmware/bugs/` for documented issues and fix scripts
- **Power loss / first-boot corruption**: Pi reset during first boot can truncate
  netplan configs to 0 bytes, killing network. `pi_setup.sh` now writes WiFi
  config directly to `/etc/NetworkManager/system-connections/` to avoid this.
  Fix for existing broken installs: `Firmware/bugs/fix_sd_power_loss.sh`

### Deployment
- Deploy to Pi: `bash Firmware/setup/deploy.sh [user@]ewe1.local`
  - Rsyncs using `.rsyncignore` (excludes Hardware/, .venv/, __pycache__/, .git/, recordings/, CLAUDE.md)
  - Prompts to run `pi_setup.sh` and reboot on the remote
  - Works over mesh (`10.42.0.N`) or infrastructure WiFi
- Run test: `cd ~/EweGo && uv run python Firmware/record_sensors.py`

## User Preferences
- Prefers concise communication
- Wants explanations of *why* before implementing fixes
- Uses uv for Python package management
- Pi hostnames: ewe1.local, ewe2.local, ... (eweN convention)
