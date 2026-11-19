# Firmware/common

`ewego-rec.c` / `ewego-rec.h`: the recorder skeleton shared by
`ewego-imu-usb` (`Firmware/usbimu/`) and `ewego-imu-uart`
(`Firmware/imuuart/`). Plain C, libc only; each recorder's Makefile
compiles it into its static binary (`make -C Firmware/usbimu`), there is no
library to build or install.

What it does, so the recorders only talk to their device:

- reader -> writer ring (8192 fixed-size samples, mutex + condvar on
  CLOCK_MONOTONIC, overruns and peak counted); `--rt` puts the reader on
  SCHED_FIFO 10 + mlockall after the writer thread exists
- the writer thread: the recorder's accounting callback, the CSV row,
  flush every 1 s, `fdatasync` every 2 s, live JSON ten times a second
  (counters + the last 64 samples), stats line + systemd `STATUS=`, the
  summary JSON every stats interval with `"in_progress": true`
- output errors (`fflush`/`ferror`, `fdatasync`, summary writes) counted
  and shown (`writer_error`, `writer_error_msg`, `free_mb`); failing for
  3 s stops the recorder with exit 4 (`REC_EXIT_OUTPUT`)
- session directory `<out>/<YYYYmmdd_HHMMSS>` (suffix `-2`, `-3` when it
  exists), created by `rec_open_output()` which the recorders call only
  after the device opened; atomic tmp + rename for the live and summary
  files
- SIGINT/SIGTERM -> `rec_stop`, blocked in the writer thread; a tiny
  `sd_notify` (`READY=1`, `STATUS=`, `WATCHDOG=1` only while real writes
  complete, `STOPPING=1`) over `$NOTIFY_SOCKET`, no libsystemd
- BNO055 burst formatting for the CSV and live rows (both IMUs read the
  same 46 registers)

A recorder fills a `struct rec_ops` (sample size, CSV header, live column
names, callbacks for accounting, CSV row, live counters/rows, stats line,
summary lines) and calls `rec_open_output`, `rec_start`, `rec_push` per
sample, `rec_end` and `rec_write_summary`. The header documents each call.
ewego-cam may adopt it later.
