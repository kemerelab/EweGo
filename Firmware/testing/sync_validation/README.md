# Cross-device sensor-sync validation

Empirical verification that audio and IMU data are **time-synchronized across
EweGo devices**, plus a measurement of the residual offset and long-term drift.
Validated on **ewego008** and **ewego011** (CM4s) over USB, June 2026.

## Why this matters

Multi-device sensor fusion needs the devices' data on a common timeline. Each
device timestamps samples in its own `CLOCK_MONOTONIC` (not comparable across
devices); chrony keeps each device's wall clock (`CLOCK_REALTIME`) synchronized.
This validation confirms the full pipeline actually yields aligned data:

```
sample → device monotonic_us → device wall seconds (chrony) → common timeline
```

See the module docstring in `analyze_sync.py` for the method in detail.

## Results (20-minute capture)

| Quantity | Audio | IMU (gyro \|ω\|) |
|---|---|---|
| Instantaneous alignment | ~8 µs (short runs); first-window lag −0.05 ms | peak **r = 0.996** per motion event |
| Lag over 20 min | −0.05 → −1.06 ms | bounded, ±~5 ms scatter |
| **Drift** | **−0.71 ppm** (218 µs scatter) | **−1.71 ppm** (44 motion windows) |

**Both sensors are synchronized; residual drift is sub-2 ppm** (≈1 ms over 20
min) with no divergence. Audio is the precise channel (broadband → sharp
correlation peak, µs-level); IMU is coarser (narrowband swing motion + 100 Hz
sampling → ±ms peak localization), but the long baseline pins its drift.

### Figures (`results/`)

- **`sync_overview.png`** — 3 rows: (1) IMU gyro overlay + cross-correlation for
  one motion event; (2) IMU lag-vs-time drift fit over the whole capture;
  (3) audio envelope overlay + audio lag-vs-time drift fit.
- **`audio_drift_diagnostic.png`** — first-4 s vs last-4 s loudness envelopes
  (look aligned: 1 ms drift ≪ 10 ms envelope resolution) and raw-waveform zooms
  at start (overlap) vs end (visibly offset by ~1 ms) — the drift made visible.
- **`clock_adjustment.png`** — (left) within each device, `CLOCK_REALTIME` vs
  `CLOCK_MONOTONIC` are rate-locked to ~0 ppm; (right) the between-device audio
  drift. Shows *why* clock_sync can't remove the drift (see findings).

## Key findings & caveats

- **Drift is a clock *frequency* residual, not a bug.** The lag grows linearly →
  a constant ppm offset. chrony bounds the *offset* (lag starts near 0) but
  leaves a small *frequency* residual; here ewego011 disciplines off ewego008
  over the mesh, so ~1 ppm is expected. Enabling the **GPS PPS refclock**
  (supported in `chrony.conf`) would pull this toward the ppb range. It also
  shows in the IMU (system-clock-timestamped), confirming it's common-mode, not
  audio-hardware-specific.
- **`CLOCK_MONOTONIC` is chrony-rate-disciplined; `CLOCK_MONOTONIC_RAW` is not.**
  The sensor timestamps and Python's `time.monotonic()` use `CLOCK_MONOTONIC`,
  whose *rate* is steered by the kernel's NTP/adjtimex discipline along with
  `CLOCK_REALTIME` (only offset *steps* are REALTIME-only). Measured directly:
  wall-vs-monotonic rate deviation is ~0.000 ppm on both devices, so the
  monotonic→wall conversion is rate-identity (a pure offset). Consequently the
  residual −0.71 ppm drift is **not** something the conversion or `clock_sync`
  can remove — `clock_sync` only relates each device's two (co-steered) clocks
  to each other, so the *inter-device* rate gap is common-mode invisible to it.
  Only a shared external reference exposes/removes it: the audio content (used
  here to measure it) or **GPS PPS** (to discipline it away). See
  `clock_adjustment.png` and `clock_domain_check.py`.
- **Always confirm broadband capture before trusting an audio lag.** A first
  attempt gave a *false* ~30 µs alignment that was actually locking onto
  grid-common 60 Hz mains hum / LF rumble. GCC-PHAT is run in the 300 Hz–4 kHz
  music band, and the music-band loudness-envelope correlation (≈0.89) is the
  proof that the *same content* was captured.
- **Gyro |ω| is the trustworthy IMU channel** (rotation-frame invariant); linear
  accel is position-dependent on a rigid body and correlates less.
- **IMU runs at 100 Hz** after the single-block-read fix (`log_imu_data.py`);
  it was ~20 Hz before. 100 Hz is the BNO055 fusion ceiling.
- **voiceHAT audio quality:** recordings are ~70 % sub-200 Hz energy and clip to
  0 dBFS — a separate LF-rumble/grounding issue worth investigating; it does not
  affect the sync result (handled by band-limiting).

## Reproducing

Raw captures (~0.5 GB/device) are **not committed** (gitignored). To regenerate:

```bash
# 1. Capture simultaneously on both devices (laptop, two terminals or background).
#    Keep music playing near both mics; for IMU drift, move both devices
#    together periodically (e.g. on a shared swing) across the window.
ssh user@10.55.8.1  'cd ~/EweGo && uv run python Firmware/record_sensors.py --no-gps'  # ~20 min, Ctrl-C
ssh user@10.55.11.1 'cd ~/EweGo && uv run python Firmware/record_sensors.py --no-gps'

# 2. Pull each device's sensor_test_* dir to a local folder, then analyze:
uv run --no-project --with numpy --with matplotlib \
    python analyze_sync.py <capture_A> <capture_B> --out results --labels ewego008,ewego011
```

`analyze_sync.py` auto-discovers `audio_*.wav`, `audio_*.timestamps.csv`,
`clock_sync.csv`, and `imu/logs/imu_log_*.csv` in each capture dir (also accepts
a flat layout with `audio.wav` / `audio_ts.csv` / `imu.csv`).

`clock_domain_check.py` (same arguments) regenerates `clock_adjustment.png` and
reuses the helpers in `analyze_sync.py`.
