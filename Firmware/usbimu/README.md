# ewego-imu-usb

Host-side logger for the hub_imu USB IMU module (`Hardware/hub_imu/firmware/`,
firmware v0.3.0 or later). Static C, libc only; runs on the EweGo image
and on any Linux machine.

Two threads (since 0.3): the reader, which only polls the hidraw node,
timestamps each report and pushes it into a RAM ring of 8192 samples, and
the writer, which does the accounting, the CSV and the live file. The
kernel keeps just 64 reports (0.6 s at 100 Hz) per open hidraw node, and
the single-threaded 0.2 lost up to 150 samples at a time whenever its CSV
write blocked on the SD card while the cameras were streaming to it. `--rt`
puts the reader on SCHED_FIFO (priority 10, mlockall), as the image's
`imu-usb.conf` does. Ring overruns are counted in the summary and make the
exit code 2.

    ewego-imu-usb --list                      # find the module (VID:PID 1209:0001)
    ewego-imu-usb --out /opt/ewego/recordings/imu --name imu --seconds 60

The module sends one 64-byte HID report per sample at 100 Hz: a sequence
counter, a device microsecond timestamp taken at the start of the I2C
read, the BNO055's 46-byte register burst, and the I2C read time
(`Hardware/hub_imu/firmware/src/report.h`). The logger stamps each report on
arrival with `CLOCK_MONOTONIC` and `CLOCK_REALTIME`.

## Output

`<out>/<YYYYmmdd_HHMMSS>/<name>.csv` with the same columns and scaling as
the UART logger `Firmware/IMU/log_imu_data.py` produced (so existing
analysis code keeps working), followed by `host_mono_us`, `dev_ts_us`,
`seq`, `i2c_us`, `flags`. Rows for samples the module flagged as invalid
keep the timing columns and leave the sensor columns empty.

`<name>_summary.json`: samples, lost (sequence gaps), flagged samples,
I2C errors, reports the host failed to poll, interval statistics on the
device clock, effective rate, device-vs-host clock drift in ppm, and the
realtime-minus-monotonic offset.

Exit 0 means every sample arrived and none was flagged; 2 otherwise;
3 if reports stop (`--stall`, default 5 s).

## Timing

Two timestamps per sample. The device clock is the one to use for sample
spacing (it is 1 µs and taken at the sensor read); the host clock places
the sample in the wearable's time base, with up to 1 ms of USB polling
jitter. A linear fit of host time against device time over a session
removes that jitter and gives the drift, which the summary reports.

The BNO055 updates its fusion output at 100 Hz on its own clock, and the
module reads it on a timer, so a reading may be up to 10 ms old relative
to the sensor's internal update. That is a property of the sensor in
fusion mode and was equally true of the UART path.

## Live view

`--live DIR` writes `DIR/<name>.json` ten times a second (tmpfs,
`/run/ewego` on the image): the counters plus the last 64 samples decoded
(sample index, seq, device and host timestamps, euler, accel, gyro,
linear accel, flags), which the web console's `/live` page turns into
scrolling traces. `--preview --live DIR` streams without recording. The file
is removed at exit.

## Under systemd

`ewego-imu-usb.service`, installed on the image and not enabled, reads
`/etc/ewego/imu-usb.conf` (OUT_DIR, NAME, DEVICE_ARG, EXTRA_ARGS; the
default EXTRA_ARGS is `--stats 5 --rt --live /run/ewego`).
