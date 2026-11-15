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
tick; any other failure marks the sample with flag bit 0 and leaves its
sensor columns empty, so the row count still follows the clock.

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
jitter, ring overruns, exit code. `lost`/`gaps` mirror read errors / error
runs so `Firmware/tools/ewego-check.py` treats both IMUs alike.

Exit 0 when every tick produced a sample and nothing overran; 2 otherwise;
3 when no sample succeeded for `--stall` seconds (sensor or UART gone, the
unit restarts it).

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

    make                              # native
    make CC=aarch64-linux-gnu-gcc     # cross for the Pi; CI does this
