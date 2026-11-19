# EweGo image

A ready-to-flash Raspberry Pi OS Lite (arm64) image with the EweGo firmware
already installed, built by GitHub Actions on every version tag. A wearable
flashed from it needs no network access and no setup script: everything is
placed in the image at build time, and nothing runs on first boot except
stock Pi OS behaviour (root filesystem expansion and Raspberry Pi Imager's
hostname / user / Wi-Fi / SSH customisation, all offline).

## What is in the image

| Where | What |
|---|---|
| `/opt/ewego/Firmware/` | the `Firmware/` tree from this repository |
| `/opt/ewego/pylib/` | pyubx2 (+ pynmeagps, pyrtcm), vendored pure-Python wheels |
| apt packages | `python3-smbus2`, `python3-serial`, `i2c-tools`, `psmisc`, `ffmpeg`, `dfu-util`, `avahi-daemon` — installed inside the image at build time. No `python3-picamera2`: the cameras are USB. |
| `/etc/systemd/system/ewego-*` | `ewego-record.target` = the default recording set: `ewego-cam@camera1`, `ewego-cam@camera2` (USB cameras, `ewego-cam`), `ewego-imu-usb` (hub_imu module), `ewego-imu-uart` (the wearable's own BNO055 on UART5, `ewego-imu-uart`), `ewego-gps`, `ewego-audio`, `ewego-fuel`. All **installed but not enabled**; each can also run alone. `ewego-autostart`: **enabled**, starts `ewego-record.target` at boot only when `/boot/firmware/ewego-record` exists (see *Recording at boot* below). `ewego-webtest`: the browser test console on port 8080, **enabled** (see `Firmware/webtest/README.md`; no authentication, bench and lab networks only). |
| `/etc/ewego/*.conf` | per-unit settings: `cam-camera1.conf` / `cam-camera2.conf` (hub port, size, fps), `imu-usb.conf`, `imu-uart.conf`, `gps.conf` (NTRIP, off by default), `audio.conf` (ALSA device override), `fuel.conf` (interval, low-battery shutdown thresholds) |
| `/usr/local/bin/ewego-cam`, `ewego-imu-usb`, `ewego-imu-uart` | static C recorders, cross-compiled in CI |
| `/usr/local/bin/ewego-check-space` | free-space guard (`ewego/check-space.sh`, see *Unattended operation*) |
| `/usr/local/bin/ewego-audio-record` | the `ewego-audio` unit's recorder (`ewego/audio-record.sh`): finds the voicehat card by name in `/proc/asound/cards` (`plughw:CARD=<id>,DEV=0`; `DEVICE=` in `audio.conf` overrides) and execs `arecord`, so arecord gets the unit's SIGINT and finishes the WAV header. Past 2 GiB (~93 min) arecord continues in `audio-01.wav`, `audio-02.wav`, ...; `ewego-check` reads them as one recording. `Firmware/audio/record_audio.py` stays for bench use only. |
| `/etc/modules-load.d/ewego.conf` | `i2c-dev` |
| `/etc/systemd/journald.conf.d/ewego.conf` | journal bounded: persistent (previous boots stay readable for post-mortems, `journalctl -b -1`), `SystemMaxUse=100M`, `RuntimeMaxUse=50M`, rate limit 1000 lines per 30 s per unit. The recorders print stats every 30 s (`--stats 30` in the confs), the fuel logger one line a minute, the GPS logger a status line every 30 s, so the card is not written continuously by logging. |
| `/lib/modules/<ver>/updates/uvcvideo.ko`, `/etc/modprobe.d/ewego-uvc.conf` | patched UVC driver with a `max_payload=2048` cap so two MJPEG cameras fit on the CM4's single USB 2.0 bus; built in the chroot at image build time (see `uvcvideo/README.md`) |
| `/etc/ewego-image-release` | version tag and build date |
| `config.txt` | `dtparam=ant2` at the top (external antenna); the hardware block appended (UART console on GPIO 14/15, no CSI camera overlays, audio, GPS UART4 with GPIO 4/5 left as unpulled inputs, GPS PPS on GPIO 6 as `/dev/pps0`, IMU UART5, I2C); `[cm4] otg_mode=1` verified (USB host mode, for webcams) |
| `cmdline.txt` | untouched: `console=serial0,115200` stays, and `serial0` is `ttyAMA0` on TX0/RX0 |

Each recorder writes one timestamped directory per start under
`/opt/ewego/recordings/<unit>/<YYYYmmdd_HHMMSS>/` (camera1, camera2, imu,
audio, fuel); the GPS logger keeps writing to `/opt/ewego/gps-logs/`.

## Flashing a wearable

1. Once, point Raspberry Pi Imager at this repository's image list:
   *Options → Content Repository → custom URL*
   `https://github.com/kemerelab/EweGo/releases/latest/download/imager.json`
   The EweGo image then appears in Imager's OS list with the customisation
   step (hostname, user, Wi-Fi, SSH key) available.
2. Flash, boot. The wearable joins the configured Wi-Fi and is reachable as
   `<hostname>.local`, or on the UART console (TX0/RX0, 115200 baud).
   Open `http://<hostname>.local:8080/` for the test console: device
   presence, battery, clock, and a button per sensor, plus a live camera
   view. `http://<hostname>.local:8080/live` is the field dashboard: both
   cameras (with flip and swap buttons), traces from both IMUs, GPS,
   battery, and the recorder, preview and shutdown controls.
3. The camera units pick the two USB cameras by hub-port order
   (`DEVICE=auto:1/2`, `auto:2/2`): camera1 is the camera on the lower hub
   port, on any hub. `ewego-cam --list` shows the order; set `DEVICE=port:...`
   in `/etc/ewego/cam-camera*.conf` only to pin a physical port.
4. Start recording when you want it (or from the console's Recorder card):

       sudo systemctl start ewego-record.target        # this boot only
       sudo systemctl stop ewego-record.target         # stops all of them

   To record from every boot, set the boot flag (next section) rather than
   `systemctl enable` the target.

   Any member (`ewego-cam@camera1`, `ewego-imu-usb`, `ewego-imu-uart`, `ewego-gps`,
   `ewego-audio`, `ewego-fuel`) can be started on its own for
   single-sensor testing.
5. Check a run (or press **Check last run** on the console's Recorder card):

       python3 /opt/ewego/Firmware/tools/ewego-check.py            # latest run
       python3 /opt/ewego/Firmware/tools/ewego-check.py --list     # all runs
       python3 /opt/ewego/Firmware/tools/ewego-check.py --collect /tmp/run1

   It groups the sessions that started together into one run and prints a
   PASS/WARN/FAIL line per stream (cameras: frames, lost, overruns, fps,
   longest write; IMU: samples, lost, rate, drift; audio: duration, format,
   peak level; fuel gauge: voltage and charge trend; GPS: NAV-PVT count, fix
   types, last position), then how far apart the streams started. Exit
   status 2 on any FAIL. `--collect DIR` lays the files out the way
   `Firmware/dualcam/play_with_timestamps.py` expects, for rsync to a
   workstation.
6. Power off from the console (**Shut down** on the Recorder card or the
   live page), not the switch: it stops the recorders so every summary is
   written and syncs the card first. Power loss mid-write is what corrupts
   the filesystem. In the field the fuel logger does the same on its own
   when the battery runs low (next section).

## Unattended operation

**Low-battery shutdown.** `ewego-fuel` is also the battery guard: when the
charge is below `SHUTDOWN_SOC` (8 %) **and** the cell voltage below
`SHUTDOWN_VOLTAGE` (3.5 V) for `SHUTDOWN_SAMPLES` (30) consecutive readings,
one minute at the 2 s interval, it writes a line to `events.log` next to
`fuel_gauge.csv` and to the journal, then runs `systemctl poweroff`, which
stops the recorders through the normal shutdown ordering. One good reading
resets the count; failed readings neither count nor reset it. Settings in
`/etc/ewego/fuel.conf`; on the bench (USB power, no battery) set
`SHUTDOWN=0` to only log the event. The thresholds are a first guess: check
them against a full discharge of the real battery under recording load (see
`Firmware/fuel_gauge/README.md`).

**Free-space guard.** `ewego-check-space DIR [MIN_MB]` prints the free MB on
DIR's filesystem, or exits 75 when less than MIN_MB (default 500) is free,
and writes the result to `/run/ewego/space.json`
(`{"dir", "free_mb", "min_mb", "ok", "checked"}`) for the console. The
recorder units run it as `ExecCondition=`: below the minimum the start is
skipped without a failure or restart loop (`systemctl status` shows the
condition; `ExecStartPre=` would not do, because systemd restarts after an
`ExecStartPre=` failure whatever `RestartPreventExitStatus=` says).
`ewego-fuel` runs it with a minimum of 0 and never blocks: the battery guard
must run even on a full card.

**Restarts and priorities.** The loggers restart on failure with no start
limit (`StartLimitIntervalSec=0`): a GPS that is absent at boot is retried
every 10 s for as long as the target runs. The OOM killer takes the web
console first (`OOMScoreAdjust=500`, also `Nice=10`, `CPUWeight=30`) and the
fuel guard last (`-900`; GPS and audio `-500`).

**GPS logger.** Exits 1 when it cannot open the port, 3 after 5 s of nothing
but read/write errors (both restart), flushes the `.ubx` every second and
fsyncs it every 5 s, opens the port exclusively, and reconnects a dropped
NTRIP stream every 30 s (logging continues without corrections meanwhile).

## Recording at boot (field cards)

A wearable in the field has no operator: after a reboot (brown-out,
watchdog, someone power-cycling it) it must resume recording on its own.
`ewego-autostart.service` is enabled in the image and starts
`ewego-record.target` at boot **only when the flag file
`/boot/firmware/ewego-record` exists**. The flag lives on the FAT boot
partition, so it can be created or removed from any laptop with the card in
a reader (the file is `ewego-record` at the top of the `bootfs` volume), or
on the wearable:

    sudo touch /boot/firmware/ewego-record     # field card: record at every boot
    sudo rm /boot/firmware/ewego-record        # bench card: come up idle

- Bench cards leave the flag absent: the wearable boots idle and recording
  is started from the console or `systemctl`.
- Field cards get the flag after flashing (flashing rewrites the boot
  partition, so set it again after every reflash). Raspberry Pi Imager's
  customisation only adds its own `user-data` / `network-config` /
  `meta-data` files there and cloud-init does not remove other files, so the
  flag survives first boot. The first boot of a fresh card also resizes the
  root filesystem and may reboot once; recording then starts on the boot
  after that, like any other boot.
- The flag gates the autostart unit, not the target: a `Condition` on a
  target does not stop systemd from starting the units it `Wants=`, and the
  target must stay startable by hand on a bench card.
- `systemctl stop ewego-record.target` (or **Stop all** on the console)
  stops recording until the next boot; delete the flag to keep it stopped.
- Without network the clock comes from the last saved time, so the run
  directory stamps after a field reboot are only as good as that; the GPS
  `_timesync.csv` gives the correction.

## Building the image

Tag and push; CI does the rest and attaches the image to a GitHub Release:

    git tag v0.1.0 && git push --tags

`workflow_dispatch` builds the image without a tag and keeps it as a
workflow artifact for three days.

### Building locally on a Mac

Needs Docker Desktop. On Apple Silicon the apt step runs natively; on Intel
Macs Docker Desktop's emulation handles it.

    xz -dk 2026-xx-xx-raspios-trixie-arm64-lite.img.xz
    ./image/inject-ewego-mac.sh 2026-xx-xx-raspios-trixie-arm64-lite.img

### Building locally on Linux

    sudo apt install qemu-user-static binfmt-support rsync fdisk e2fsprogs unzip python3-pip
    ./image/vendor-pylib.sh build/pylib
    sudo ./image/inject-ewego.sh raspios.img --pylib build/pylib

`inject-ewego.sh` also works on a flashed SD card (`/dev/sdX`), in which
case the partition is not grown and the apt step needs the card's free space.

## Files

| File | Purpose |
|---|---|
| `inject-ewego.sh` | the injector: mounts the image, copies the firmware, runs apt in a chroot, edits config.txt |
| `inject-ewego-mac.sh` | Docker wrapper for macOS |
| `vendor-pylib.sh` | downloads `requirements-vendored.txt` as pure wheels and unpacks them |
| `apt-packages.txt` | packages installed inside the image |
| `requirements-vendored.txt` | pure-Python packages not in Debian |
| `config.txt.ewego` | the hardware block appended to `config.txt` |
| `units/` | the systemd units |
| `ewego/*.conf` | `/etc/ewego/` settings per unit; `ewego/journald-ewego.conf` becomes `/etc/systemd/journald.conf.d/ewego.conf` |
| `ewego/*.sh` | helper scripts installed as `/usr/local/bin/ewego-<name>` (`ewego-check-space`, `ewego-audio-record`) |
| `../.github/workflows/build-image.yml` | the CI pipeline |
