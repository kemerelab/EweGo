# EweGo collar image

A ready-to-flash Raspberry Pi OS Lite (arm64) image with the EweGo firmware
already installed, built by GitHub Actions on every version tag. A collar
flashed from it needs no network access and no setup script: everything is
placed in the image at build time, and nothing runs on first boot except
stock Pi OS behaviour (root filesystem expansion and Raspberry Pi Imager's
hostname / user / Wi-Fi / SSH customisation, all offline).

## What is in the image

| Where | What |
|---|---|
| `/opt/ewego/Firmware/` | the `Firmware/` tree from this repository |
| `/opt/ewego/pylib/` | pyubx2 (+ pynmeagps, pyrtcm), vendored pure-Python wheels |
| apt packages | `python3-picamera2`, `python3-smbus2`, `python3-serial`, `i2c-tools`, `psmisc` — installed inside the image at build time |
| `/etc/systemd/system/ewego-*.service` | `ewego-sensors` (all sensors via `sensor_test.py`), `ewego-dualcam`, `ewego-gps`. **Installed but not enabled.** |
| `/etc/modules-load.d/ewego.conf` | `i2c-dev` |
| `/etc/ewego-image-release` | version tag and build date |
| `config.txt` | `dtparam=ant2` at the top (external antenna); the hardware block from `pi_setup.sh` appended (UART console on GPIO 14/15, cameras, audio, GPS UART3/4, IMU UART5, I2C); `[cm4] otg_mode=1` verified (USB host mode, for webcams) |
| `cmdline.txt` | untouched: `console=serial0,115200` stays, and `serial0` is `ttyAMA0` on TX0/RX0 |

Recordings from `ewego-sensors` land in `/opt/ewego/sensor_test_<timestamp>/`,
the same place relative to the tree as when running from `~/EweGo` by hand.

## Flashing a collar

1. Once, point Raspberry Pi Imager at this repository's image list:
   *Options → Content Repository → custom URL*
   `https://github.com/kemerelab/EweGo/releases/latest/download/imager.json`
   The EweGo image then appears in Imager's OS list with the customisation
   step (hostname, user, Wi-Fi, SSH key) available.
2. Flash, boot. The collar joins the configured Wi-Fi and is reachable as
   `<hostname>.local`, or on the UART console (TX0/RX0, 115200 baud).
3. Start recording when you want it:

       sudo systemctl start ewego-sensors      # this boot only
       sudo systemctl enable --now ewego-sensors   # every boot

   `ewego-dualcam` and `ewego-gps` can be started on their own for
   single-sensor testing.

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
