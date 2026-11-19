# Robustness programme (audit of 2026-11-18)

Goal: a recording survives anything short of hardware failure, fails loudly
when it cannot, and the live view is a pure mirror of what the recorders have
already written. Items are numbered R1… and referenced from commits as
`[R7]`. Lanes are worked in parallel on separate branches and merged into
`phase-a-image`; the CI `check` job (and from R30 the test step) is the gate.

Legend: `[ ]` open, `[~]` in progress, `[x]` merged.

## Lane A1: ewego-cam (`Firmware/uvccam/`, `image/units/ewego-cam@.service`)

- [ ] **R1** Writer errors are fatal and visible: check `write_all`, `sync_file_range`
  (use WAIT_BEFORE|WRITE|WAIT_AFTER on the previous chunk so EIO surfaces) and
  `fdatasync` results; a persistent write error exits with a distinct code
  (4 = output error) instead of running on with `w->error`. Expose
  `writer_error` and free space (`statvfs`) in the live JSON and `STATUS=`.
- [ ] **R2** Index and timestamp files get the same writeback cadence as the MJPEG
  stream (bounded loss on power cut; today ~30 s of `_index.bin`/`_timestamps.bin`).
- [ ] **R3** On a failed or partial frame write, truncate back to the last good
  frame boundary and do not advance `offset` or write the index record.
- [ ] **R4** Summary JSON written every stats interval via tmp+rename with
  `"in_progress": true`; final write at exit flips it (wall-clock offset
  survives power loss and SIGKILL).
- [ ] **R5** Stats, live JSON and sd_notify move off the capture thread (snapshot
  under the queue mutex, written by the writer thread); gap messages
  rate-limited; stdout line-buffered.
- [ ] **R6** Session directory created after the device is open; signal handlers
  installed before the device wait and the wait loop honours `g_stop`.
- [ ] **R7** `auto:N/M` fallback after `--wait` must not let two instances claim
  one camera (drop the fallback, or claim ports via a lock file in /run/ewego).
- [ ] **R8** Unit: `TimeoutStopSec=90`, `StartLimitIntervalSec=0`, `WatchdogSec`
  fed from the writer thread only after a real write completed; `OOMScoreAdjust=-500`.
- [ ] **R9** Session stamp collision (same second after a restart or a clock step)
  must not truncate an earlier recording (`O_EXCL` / suffix).

## Lane A2: IMU recorders (`Firmware/usbimu/`, `Firmware/imuuart/`, new `Firmware/common/`)

- [ ] **R10** Shared recorder skeleton `Firmware/common/ewego-rec.{c,h}`: ring +
  writer loop, flush/fdatasync/live/summary cadence, atomic file replace,
  session dir, signal handling, `--rt`, `mkdir_p`, time helpers. Both IMU
  recorders use it (the two files are ~70 % identical today). ewego-cam may
  adopt it later.
- [ ] **R11** CSV write errors are fatal and visible: `ferror()`/`fflush`/`fdatasync`
  results checked, distinct exit code 4, `writer_error` in the live JSON.
- [ ] **R12** `fdatasync` every 2–5 s from the writer thread (today: none at all;
  ~30 s of CSV lost on every power cut).
- [ ] **R13** Summary JSON written periodically with `in_progress`, as R4.
- [ ] **R14** UART: detect a sensor that reset into CONFIG mode (reads succeed but
  the fusion registers are frozen/zero): after N consecutive all-zero
  quaternions or a zero calibration byte, read OPR_MODE and re-init if not
  NDOF. Likely the "collar IMU freezes" symptom of v0.6.5.
- [ ] **R15** UART: `write_all` must have a poll(POLLOUT) deadline (today it spins
  on EAGAIN at SCHED_FIFO); `TIOCEXCL` after open so nothing else can share
  the port.
- [ ] **R16** USB: stall detector must not count malformed reports as liveness;
  per-report error message rate-limited.
- [ ] **R17** Session directory after device open; signal handlers before the wait;
  live counters read under the mutex; session stamp collision guard (as R6/R9).
- [ ] **R18** Units: `StartLimitIntervalSec=0`, `OOMScoreAdjust=-500`, `Type=notify`
  + `WatchdogSec` fed from the writer thread.

## Lane B1: console (`Firmware/webtest/`, new `image/units/ewego-preview*.service`)

- [x] **R19** Preview becomes systemd units (`ewego-preview@camera1/2`,
  `ewego-preview-imu-usb`, `ewego-preview-imu-uart`) with `Conflicts=` on their
  recorders; the console starts/stops them with `systemctl` and never spawns a
  recorder itself. The idle reaper stays (stop the units after 90 s without a
  live client).
- [x] **R20** Device tests are refused while the owning recorder is `active`,
  `activating`, `reloading` or `deactivating`, or while the serial bridge or a
  preview unit holds the device; unit start refuses while a test or the bridge
  holds a device.
- [x] **R21** Every POST requires the header `X-EweGo: 1` (defeats cross-site simple
  requests); the live page and console JS send it.
- [x] **R22** RECORDING badge means every recorder unit is active *and* its live
  JSON is fresh (< 2 s) and reports no `writer_error`; otherwise show which
  recorder is down.
- [x] **R23** Handler socket timeout (30 s) and a cap on concurrent stream
  connections; `status()` uses the single-call `units_active()`.
- [x] **R24** Heavy work (`check`, `camrec`, journal dumps) refused while the target
  is active; battery read from the fuel logger's CSV when the fuel unit is
  running instead of opening the I2C bus.

## Lane B2: image, systemd, Python loggers (`image/`, `Firmware/audio|fuel_gauge|gps-test`)

- [ ] **R25** `ewego-record.target` enabled at boot behind a boot-partition flag
  (`ConditionPathExists=/boot/firmware/ewego-record`) so a field reboot
  resumes recording and bench units stay quiet. Documented in `image/README.md`.
- [ ] **R26** Low-battery shutdown in `log_fuel_gauge.py`: `systemctl poweroff`
  after SOC and voltage are both below thresholds for 30 consecutive samples
  (configurable in `fuel.conf`), logged as a row and to the journal. Bench mode
  flag to disable.
- [ ] **R27** journald bounded (`/etc/systemd/journald.conf.d/ewego.conf`:
  `SystemMaxUse=100M`, rate limits); field confs use `--stats 30`; the fuel
  logger stops printing every reading.
- [ ] **R28** `ewego-audio.service` runs `arecord` directly (no Python wrapper, no
  250 ms SIGKILL of arecord); ALSA device named by card id, not `default`;
  `ewego-check` understands arecord's `audio-01.wav` rollover at 2 GiB.
- [ ] **R29** `gps_logger.py`: exit 1 when start fails, exit 3 after N s of
  consecutive read/write errors, flush + periodic fsync of the .ubx, status
  print rate reduced; `TIOCEXCL` on the port. (Full C port is Phase C.)
- [ ] **R30** `ewego-webtest.service`: `Nice=10`, `CPUWeight=30`,
  `OOMScoreAdjust=500`; `StartLimitIntervalSec=0` on audio/fuel/gps units.
- [ ] **R31** Free-space guard: `ExecStartPre` check on every recorder unit (exit a
  distinct code, `RestartPreventExitStatus=`), surfaced on the console.

## Lane C: tests (`tests/`, `.github/workflows/build-image.yml`)

- [ ] **R32** Test harness: `tests/` with Python stdlib `unittest`, run by
  `python3 -m unittest discover -s tests -v`; a `Tests` step in the CI `check`
  job after the native compile. No new dependencies.
- [ ] **R33** ewego-imu-uart black-box test against a pty-based fake BNO055 (chip id,
  NDOF init, 46-byte bursts, 0x07/0x0A errors, garbage, silence → exit 3,
  reset-into-CONFIG scenario for R14), checking CSV columns, summary JSON,
  live JSON, exit codes.
- [ ] **R34** ewego-imu-usb black-box test feeding report structs through a FIFO
  given as `--device`, covering sequence gaps, bad reports, device gone.
- [ ] **R35** ewego-check tests on synthetic session directories (PASS/WARN/FAIL
  paths, missing summary, in-progress summary).
- [ ] **R36** Console tests: `live_snapshot`, `parse_nav_pvt`, SSE dedupe, badge
  logic, POST header check, with `EWEGO_LIVE_DIR` pointing at fixtures.
- [ ] **R37** ewego-cam: writer/index logic testable without a camera (replay a
  recorded MJPEG through the writer path, or a `--source file` test mode).

## Later

- Full C port of the GPS logger with NAV-PVT validity and `gps.json` in /run/ewego.
- Read-only root + data partition (bug 001 option 2).
- Single session id per target start (`/run/ewego/session`) so restarts of one
  unit are self-describing.
- ewego-cam adopts `Firmware/common/`.
