# hub_imu ("EweFeel 1.0"): project notes

These notes are for whoever picks this up next: what the module is for, how it
got here, where its pieces live, and what tends to go wrong. Pins, parts and
layout rules are in [README.md](README.md). The firmware has its own
[README](firmware/README.md).

## What it is and why

A 21 x 14.4 mm module that turns one downstream port of the collar's USB hub
into a USB IMU. An STM32F042F6P6 reads a Bosch BNO055 over I2C and sends each
sample to the CM4 as a HID report.

EweGo moved its cameras from CSI to USB, and the eweSAW carrier's BNO055 is on
UART5. Putting a second IMU on the camera hub places the motion sensor on the
head strap next to the cameras and gives each sample a device timestamp. It
needs no extra wires to the carrier.

## History

The design was done in its own repository, `github.com/ckemere/usb-imu`. That
repository keeps the full commit history. The files were copied here without
history on 2026-10-30.

| Date | What happened |
|---|---|
| 2026-09-23 | First board: STM32F042 crystal-less USB bridge, BNO055, AP2112K LDO, SWD header, LED. DRC clean. |
| 2026-09-24 | Switched to JLCPCB basic parts: LDO changed to XC6206P332MR (SOT-23-3, no EN pin) and LED changed to an 0603. J1 pin order checked against the hub header. BNO055 pins checked against datasheet rev 1.8. |
| 2026-10-04 | Firmware steps 1–3 in EweGo CI (`hubimu-v0.1.0` … `v0.3.0`): enumeration with a DFU console, BNO055 bring-up, and a 100 Hz HID stream. Each step was verified on hardware. |
| 2026-10-05 | Collar bus test (image v0.5.1, `Firmware/usbimu/bus-test.py`): IMU at 100 Hz with 0 lost and 9992–10008 µs intervals while two cameras ran 1080p30 on the same bus. |
| 2026-10-06 | Production run with all units: 44 samples lost in 3 gaps (~0.1%). The firmware counted "hid busy", meaning the host was slow to poll the HID endpoint. |
| 2026-10-16 | Board marked "EweFeel 1.0": J1/J2 set to DNP (hand-soldered), mask clearance 0.005 mm, gerbers, CERN-OHL-P licence, photos. |

## Where the pieces live

| Piece | Location |
|---|---|
| KiCad board, gerbers (`production/hub_imu.zip`) | this directory |
| Firmware | `firmware/`, built by `.github/workflows/hub-imu-firmware.yml`; `hubimu-v*` tags publish `.bin`/`.dfu` releases |
| Host logger | `Firmware/usbimu/ewego-imu-usb.c`; unit `image/units/ewego-imu-usb.service`; config `image/ewego/imu-usb.conf` |
| Report format, shared by firmware and host | `firmware/src/report.h` (64 bytes: version, flags, seq, dev_ts_us, 46-byte burst, i2c_us, period_us) |
| Bench tools | the "USB IMU" card in the web console; `Firmware/usbimu/bus-test.py` (also runs on macOS) |

## Gotchas

- **USB pins:** set `SYSCFG_CFGR1.PA11_PA12_RMP`, or USB will not enumerate.
- **Clocks:** there is no crystal. HSI48 is trimmed to USB SOF by the CRS, so
  `dev_ts_us` follows the host's USB frame clock. `ewego-imu-usb` reports the
  device-to-host drift in ppm. The BNO055 runs on its internal clock, which
  the datasheet allows to be off by up to ±3%.
- **Flashing:** the first flash needs BOOT0 (J2 pin 2) held to 3.3 V at
  plug-in, which brings up the ST bootloader at `0483:df11`. After that, use
  the console's `dfu` command. The SWD header uses a custom pinout, not the
  ARM standard one.
- **The BNO055's own firmware can't be updated:** nBOOT_LOAD is tied high.
- **USB IDs:** VID:PID `1209:0001` is the pid.codes test PID and is fine
  only for lab use. Request a real PID before distributing modules.
- **Mechanical fit:** the Amazon FE1.1S hub has a 2.0 mm port pitch, so it
  doesn't line up with this module's 2.54 mm J1. That mismatch is one reason
  for [hub_cam](../hub_cam/NOTES.md).
- **Firmware builds:** dependencies (TinyUSB 0.21.0, CMSIS) are fetched by
  `firmware/fetch-deps.sh` at pinned tags and never committed. Builds happen
  in CI only. TinyUSB 0.21 merged `usbd_control.c` into `usbd.c` and needs
  `fsdev_common.c`.

## Open items

- Track the ~0.1% "hid busy" loss on long runs. It is on the host side (late
  polling), not I2C.
- A real USB PID if modules leave the lab.
