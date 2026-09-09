# EweGo — working notes for Claude Code sessions

Sensor collar for sheep: Raspberry Pi CM4 on the eweSAW carrier with two USB
cameras (moving from CSI), u-blox ZED-X20P GNSS, BNO055 IMU, stereo mics,
MAX17048 fuel gauge. Plan and decisions live in the bring-up plan artifact:
https://claude.ai/code/artifact/828028de-6284-4f76-b3f8-3f5925ac1dfe

## How to work here

- **Test through GitHub Actions, never locally.** Push, tag a release
  (`gh release create vX.Y.Z --target phase-a-image ...`), and watch with
  `gh run list` / `gh run view <id>`. Do not run Docker or image builds on the
  developer's machine, and never install or update software on it.
- The image build (`.github/workflows/build-image.yml`) takes about 14 min:
  `check` job on every push, `image` job on `v*` tags. It downloads
  Raspberry Pi OS Lite arm64 (latest, currently Trixie), runs
  `image/inject-ewego.sh` (grows the image, copies `Firmware/` to
  `/opt/ewego`, runs apt in an emulated chroot, builds the patched uvcvideo
  module, edits config.txt), then publishes the image + `imager.json` to
  the release. Flash with Raspberry Pi Imager via
  `https://github.com/kemerelab/EweGo/releases/latest/download/imager.json`.
- No first-boot steps of any kind: everything is baked in at build time so
  a collar works in the field with no network.
- Fast iteration for the web console without an image rebuild: scp
  `Firmware/webtest/ewego_webtest.py` to `/opt/ewego/Firmware/webtest/` on
  the collar and `systemctl restart ewego-webtest`.
- Keep C ports byte-compatible with the existing CSV / .ubx / int64-µs
  timestamp outputs so `play_with_timestamps.py` keeps working.

## State (2026-09-09)

- Branch `phase-a-image` (not merged to main). Releases v0.1.0 … v0.2.3
  built green. Test collar: hostname `ewego`, user `kemerelab`, web console
  at http://ewego.local:8080.
- Phase A done: Python firmware installed but not enabled; units
  `ewego-sensors`, `ewego-gps`, `ewego-dualcam` (disabled) and
  `ewego-webtest` (enabled). Wi-Fi/hostname/user via Imager customisation.
- GPS: data on UART4 (`/dev/ttyAMA4` @ 460800), module UART2 on GPIO 4/5 is
  unused and left as unpulled inputs (`gpio=4,5=ip,pn`). Nav rate lowered to
  5 Hz (`CFG-RATE-MEAS` 200). TP1 PPS on GPIO 6 → `/dev/pps0`
  (`dtoverlay=pps-gpio,gpiopin=6`). Config decoded in
  `Firmware/gps-test/CONFIGURING_HW.md`.
- USB cameras: MJPEG-only 1080p30. **Bandwidth wall**: the camera requests
  3060 B/µframe at every format; two cannot share the CM4's single USB 2.0
  bus (~6000 B/µframe). `quirks=128` is uncompressed-only on the 6.12
  kernel. Decision: patched uvcvideo with a `max_payload` cap (2048),
  built in the image chroot — see `image/uvcvideo/README.md`. Options
  "different camera" and "second USB bus" are not available.
- v0.3.1 = first image with the patched module, built green (image kernel
  6.18.34+rpt-rpi-v8; the injector follows whatever kernel the image has).
  Next: confirm on the collar (Bandwidth probe should log "capping
  requested bandwidth 3060 to 2048", drop test with both cameras PASS), then write
  `ewego-cam`, a standalone static C V4L2 recorder (MJPEG frames +
  int64 µs kernel timestamps + seq/flags index, drop accounting via
  sequence gaps; design in plan §4).
- Known limits: NetworkManager still in use (SD corruption after power
  loss, bug 001, until low-battery shutdown lands); NTRIP caster
  hard-coded to 192.168.1.213 in gps_logger.py; no RTC on the CM4.
