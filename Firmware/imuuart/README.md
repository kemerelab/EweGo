# ewego-imu-uart

Recorder for the wearable's own BNO055, the one wired to the CM4's UART5
(`/dev/ttyAMA5`, 115200 baud). The C port of `Firmware/IMU/log_imu_data.py`;
static, libc only, same two-thread shape as `ewego-imu-usb` (reader on the
port, writer behind a RAM ring, `--rt` for the reader).

    ewego-imu-uart --out /opt/ewego/recordings/imu-uart --seconds 60
    ewego-imu-uart --preview --live /run/ewego          # live view only

One UART transaction per sample reads registers 0x08..0x35 (46 bytes:
accelerometer, magnetometer, gyroscope, Euler angles, quaternion, linear
acceleration, gravity, temperature, calibration status) on a steady
100 Hz timer, the BNO055's fusion output rate. The Python logger did nine
separate reads per sample at 50 Hz. A BNO055 "buffer overrun" answer
(error 0x07, the sensor was mid-update) is retried once within the same
tick (a retry that succeeds is no error, but counts in `overruns_retried`
in the summary and the live JSON); any other failure marks the sample with flag bit 0 and leaves its
sensor columns empty, so the row count still follows the clock.

## Sensor self-check (0.3)

A BNO055 that browns out resets (0.4-0.65 s) and comes back in CONFIG mode,
answering burst reads normally with every data register at zero; 0.2 kept
recording those flat rows for hours with no error and exit 0 (the likely
cause of the "wearable IMU freezes after a few moments" reports). In NDOF
the quaternion is a unit vector, never all zero, so now:

- a sample whose quaternion is all zero gets flag bit 1 (its columns are
  kept as read) and counts in `fusion_invalid`;
- after 20 such samples in a row, or 3 s of identical sensor data, the
  reader reads OPR_MODE and re-initialises the sensor (always for zero
  quaternions; for frozen data only if the mode is not NDOF), at most every
  2 s. Each such run counts in `fusion_stalls` (and makes the exit code 2),
  each re-init in `fusion_reinits` and `reinits`.

The port is opened with `TIOCEXCL`: while the recorder (or its preview)
runs, anyone else opening it, such as the console's IMU test or a second
recorder, gets EBUSY ("Device or resource busy"; the recorder adds a hint).
Processes with CAP_SYS_ADMIN are exempt by kernel rule. Writes to the port
wait for POLLOUT up to the transaction's deadline instead of spinning.

## Output

`<out>/<YYYYmmdd_HHMMSS>/<name>.csv`: the 28 columns of `log_imu_data.py`
(timestamp, euler, quaternion, linear accel, gravity, accel, gyro, mag,
temperature, calibration), then `host_mono_us` (arrival of the sensor's
answer), `tick_mono_us` (the scheduled sample time; arrival minus tick is
the host jitter), `seq`, `xfer_us` (UART round trip, about 4.5 ms at
115200 baud), `flags`.

`<name>_summary.json`: samples, read errors and error runs, late ticks
(the reader fell a whole period behind and resynchronised), interval
statistics between valid samples, effective rate, largest round trip and
jitter, ring overruns, `fusion_invalid` / `fusion_stalls` /
`fusion_reinits`, `overruns_retried` (0x07 answers whose retry worked),
exit code. `lost`/`gaps` mirror read errors / error runs
so `Firmware/tools/ewego-check.py` treats both IMUs alike; `flagged` is
read errors plus zero-quaternion samples.

Exit 0 when every tick produced a sample and nothing overran; 2 read
errors, ring overruns or a fusion stall; 3 when no sample succeeded for
`--stall` seconds (sensor or UART gone, the unit restarts it); 4 when the
recording could not be written (below).

## Surviving the field (0.4 / ewego-imu-uart 0.3)

The ring, the writer thread, the live file and the summary are shared with
the other IMU recorder (`Firmware/common/ewego-rec.c`).

- The CSV is flushed every second and `fdatasync`'d every 2 s from the
  writer thread, so a power cut loses at most a couple of seconds.
- Write errors are not silent: each failed flush / `fdatasync` / summary
  write is counted (`writer_error`, `writer_error_msg` in the live JSON and
  the summary, `WRITE ERRORS` in the stats line, stderr). Errors that keep
  coming for 3 s (a full or failing card) stop the recorder with exit 4.
  `free_mb` (free space on the recording's filesystem) is in the live JSON,
  the summary and the stats line.
- `<name>_summary.json` is rewritten every stats interval (every 5 s with
  `--stats 0`) with `"in_progress": true` and `"exit_code": null`; the
  final one at a clean exit has `"in_progress": false`. Each write is
  tmp + fdatasync + rename, so after a power cut or SIGKILL the last one is
  intact.
- The session directory is created only once the device is open, and a
  stamp that already exists (restart within the same second, clock step)
  becomes `<stamp>-2`, `-3`, ...; nothing is ever truncated.
- Under systemd (`Type=notify`, `WatchdogSec=30`): `READY=1` once the
  device is open, `STATUS=` with the stats line, and `WATCHDOG=1` from the
  writer thread only while `fdatasync` of new rows keeps completing (in
  `--preview`, while the live file is written). A recorder that hangs or
  stops writing is killed and restarted (`Restart=on-failure`,
  `StartLimitIntervalSec=0`, `OOMScoreAdjust=-500`).

Exit codes: 0 clean; 1 usage, device or setup error (including a session
directory or CSV that cannot be created); 2 lost/flagged samples or ring
overruns; 3 the device stopped (`--stall`) or went away; 4 the recording
could not be written.

The live JSON also carries `fusion_invalid`, `fusion_stalls`, `reinits`
and `overruns_retried`.

## Live view

`--live DIR` writes `DIR/<name>.json` ten times a second with the counters
and the last 64 samples, in the same layout as `ewego-imu-usb`, so the web
console's `/live` page draws both IMUs with one piece of code. `--preview
--live DIR` streams without recording.

## Timing

There is no device clock on this path: `host_mono_us` is the only time
base, taken when the sensor's answer arrives, about 4.5 ms after the
request. The BNO055 refreshes its fusion output at 100 Hz on its own clock,
so a reading can be up to 10 ms old; this was equally true of the Python
logger and of the USB module.

## Under systemd

`ewego-imu-uart.service`, in `ewego-record.target`, reads
`/etc/ewego/imu-uart.conf` (PORT, BAUD, RATE, OUT_DIR, NAME, EXTRA_ARGS;
default EXTRA_ARGS `--stats 5 --rt --live /run/ewego`). The console's
"IMU (BNO055, ttyAMA5)" test refuses to run while the unit or the live
preview owns the port.

## Build

    make                              # native; compiles ../common/ewego-rec.c in
    make CC=aarch64-linux-gnu-gcc     # cross for the Pi; CI does this
