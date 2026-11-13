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
| `/etc/systemd/system/ewego-*` | `ewego-record.target` = the default recording set: `ewego-cam@camera1`, `ewego-cam@camera2` (USB cameras, `ewego-cam`), `ewego-imu-usb` (hub_imu module), `ewego-gps`, `ewego-audio`, `ewego-fuel`. All **installed but not enabled**; each can also run alone. `ewego-webtest`: the browser test console on port 8080, **enabled** (see `Firmware/webtest/README.md`; no authentication, bench and lab networks only). |
| `/etc/ewego/*.conf` | per-unit settings: `cam-camera1.conf` / `cam-camera2.conf` (hub port, size, fps), `imu-usb.conf`, `gps.conf` (NTRIP, off by default), `audio.conf`, `fuel.conf` |
| `/usr/local/bin/ewego-cam`, `ewego-imu-usb` | static C recorders, cross-compiled in CI |
| `/etc/modules-load.d/ewego.conf` | `i2c-dev` |
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
   presence, battery, clock, and a button per sensor, plus a live camera view.
3. Check the camera hub ports once: `ewego-cam --list` on the wearable,
   and set `DEVICE=port:...` in `/etc/ewego/cam-camera1.conf` and
   `cam-camera2.conf` if they differ from the defaults (`port:1.1`,
   `port:1.3`, the hub_cam board's camera ports).
4. Start recording when you want it (or from the console's Recorder card):

       sudo systemctl start ewego-record.target        # this boot only
       sudo systemctl enable --now ewego-record.target # every boot
       sudo systemctl stop ewego-record.target         # stops all of them

   Any member (`ewego-cam@camera1`, `ewego-imu-usb`, `ewego-gps`,
   `ewego-audio`, `ewego-fuel`) can be started on its own for
   single-sensor testing. Each camera's `summary.json` should show 0 lost
   frames and 0 overruns.

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
| `../.github/workflows/build-image.yml` | the CI pipeline |
