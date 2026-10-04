# hub_imu firmware

Firmware for the hub_imu module: an STM32F042F6P6 that turns one port of
the collar's FE1.1S USB hub into a USB IMU (Bosch BNO055 over I2C). The
hardware lives in `LabTools/SmallProjects/USB-IMU/hub_imu`; its README is
the reference for pins and the circuit.

Nothing is built or installed on a developer machine. GitHub Actions
(`.github/workflows/hub-imu-firmware.yml`) compiles every push that touches
this directory with `gcc-arm-none-eabi`, prints flash and RAM usage, and
attaches `hub_imu.bin`; a `hubimu-v*` tag publishes a release.

## Flashing (DFU through the hub)

First time, with blank flash: hold BOOT0 (J2 pin 2) to 3.3 V (J2 pin 1)
while plugging the module into the hub, then release. The chip starts the
STM32 system bootloader and enumerates as `0483:df11`. Then, on the collar
(dfu-util is on the image) or any machine with dfu-util:

    dfu-util -l
    dfu-util -a 0 -s 0x08000000:leave -D hub_imu.bin

`:leave` starts the new firmware. From then on, `dfu` on the module's
console reboots it into the bootloader, so the jumper is never needed again.

## USB device

Composite device, VID:PID `1209:0001` (pid.codes test PID; lab use only):

| Interface | Class | Purpose |
|---|---|---|
| 0/1 | CDC-ACM | text console: `/dev/ttyACM*` on Linux, `/dev/cu.usbmodem*` on macOS (`screen /dev/cu.usbmodem* 115200`) |
| 2 | HID (vendor page) | IMU data: one 64-byte input report per sample, 1 ms interrupt endpoint |

Console commands: `help`, `version`, `id`, `cal`, `read`, `init`, `dfu`.

## Status

- **Step 1 (v0.1.0, verified on hardware)**: enumerate, LED blink (5 Hz
  until configured by the host, then 1 Hz), console with `dfu`.
- **Step 2 (v0.2.0)**: I2C1 on PF0/PF1 at 100 kHz, BNO055 reset on PA6,
  NDOF fusion mode at boot. `id` prints chip/sensor/firmware IDs and
  sys_status (5 = fusion running); `cal` the four calibration levels;
  `read` one formatted 46-byte burst and how long the I2C read took;
  `init` re-runs the bring-up. LED at 2 Hz if the sensor failed; the
  banner says why.
- Step 3: 100 Hz sampling, HID reports with the 46-byte register burst
  (0x08–0x35), device microsecond timestamp and sequence counter; host
  logger `ewego-imu-usb` on the collar.

## Layout

| File | Purpose |
|---|---|
| `Makefile` | bare-metal build, `-Os`, prints size |
| `fetch-deps.sh` | clones TinyUSB 0.21.0, cmsis_device_f0 v2.3.8, cmsis_core v5.9.0 into `lib/` (not committed) |
| `src/STM32F042X6.ld` | 32 KB flash / 6 KB RAM, `.noinit` for the bootloader request |
| `src/startup.c` | vector table, reset handler, jump to the system bootloader on request |
| `src/main.c` | clocks (HSI48 + CRS), USB remap, LED, console and its commands |
| `src/i2c.*` | register-level I2C1 master with timeouts |
| `src/bno055.*` | BNO055 reset, identification, NDOF mode, 46-byte burst read |
| `src/usb_descriptors.*` | composite CDC + HID descriptors |
| `src/tusb_config.h` | TinyUSB configuration |
