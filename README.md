# EweGo

Sensor collar firmware and hardware for a Raspberry Pi CM4 on the eweSAW
carrier: dual cameras, GPS (u-blox ZED with RTK), 9-axis IMU, stereo
microphones, and a battery fuel gauge.

- `Firmware/` — the recording scripts and per-sensor tools
- `image/` — the CI-built, ready-to-flash collar image (see `image/README.md`)
- `Hardware/` — carrier board (KiCad) and case designs

## Flashing a collar

Point Raspberry Pi Imager at
`https://github.com/kemerelab/EweGo/releases/latest/download/imager.json`
(*Options → Content Repository*), pick the EweGo image, set hostname, user,
Wi-Fi and SSH key in the customisation step, and flash. Details in
`image/README.md`.
