# ewego-cam

Standalone recorder for one UVC (USB) camera. One C file, static binary,
no dependencies beyond libc and the kernel headers, so it runs on the
EweGo image and on any other Pi for bench tests. It talks V4L2 to the
in-kernel `uvcvideo` driver, not libuvc, so every frame's timestamp is the
kernel's `CLOCK_MONOTONIC` stamp taken when the frame's first USB packet
arrived, independent of anything user space does.

    ewego-cam --list
    ewego-cam --device auto:1/2 --out /opt/ewego/recordings/camera1 --name camera1 \
              --size 1920x1080 --fps 30 --seconds 600

`--device` takes `/dev/videoN`, a `/dev/v4l/by-path/...` link,
`port:<hub port>` (the physical USB port in the by-path name, stable across
reboots where device numbers are not), or `auto:N[/M]`: the Nth camera with
the cameras ordered by hub port, expecting M of them. `auto:1/2` and
`auto:2/2` are the image defaults, so camera1 is always the camera on the
lower hub port on any hub. The recorder waits until the camera set has been
unchanged for 1.5 s before choosing (after a hub reset the cameras come back
one at a time), and if fewer than M cameras show up within `--wait` it
records with what is there and says so. `--list` prints both forms.

Two instances never record the same camera: each one holds an `flock` on
`/run/ewego/port-<port>.lock` (`--lock-dir`, `""` turns it off) for the
camera it opened, released by the kernel however the process ends. While
waiting, `auto:N/M` only takes the Nth camera; at the end of `--wait` it
takes the Nth even if the set is incomplete, and if another instance holds
that one, the first camera no instance holds (the log says so, and the
summary's `port` records which physical camera it was). Before 0.6 the
fallback could make camera1 take camera2's camera and camera2 then loop on
EBUSY. `port:` and `/dev/videoN` specs claim their port too and wait while
another instance holds it.

## Output

A session directory `<out>/<YYYYmmdd_HHMMSS>/` (or `--out` itself with
`--no-session-dir`), created only once the camera is open and streaming (a
failed or stopped start leaves nothing behind). The files are opened
`O_EXCL`: if the stamp already holds a recording (a restart within the same
second, or the clock stepped back when GPS time arrived) the session becomes
`<stamp>-2`, `<stamp>-3`, ...; with `--no-session-dir` an existing
recording is an error (exit 4). It contains:

| File | Content |
|---|---|
| `<name>.mjpeg` | concatenated JPEG frames; `dualcam/play_with_timestamps.py` reads this |
| `<name>_timestamps.bin` | int64 little-endian, microseconds, `CLOCK_MONOTONIC`, one per frame (same format as the CSI recorder) |
| `<name>_index.bin` | 32-byte records: u64 byte offset in `.mjpeg`, i64 timestamp µs, u32 driver sequence, u32 bytes, u32 V4L2 flags, u32 reserved |
| `<name>_summary.json` | frames, lost, gaps and where, error-flagged frames, interval min/mean/max, effective fps, timestamp clock and source, and the `CLOCK_REALTIME` minus `CLOCK_MONOTONIC` offset at start and end, so timestamps can be mapped to wall time |

The summary is written when recording starts and rewritten every `--stats`
seconds (5 s if `--stats 0`), atomically (temporary file, `fdatasync`,
`rename`), with `"in_progress": true` and `"exit_code": null`; the final
write at exit has `"in_progress": false` and the exit code. After a power
cut or SIGKILL the summary from the last interval is there, with the
wall-clock mapping. Keys added in 0.6: `in_progress`, `port` (hub port of
the camera, `""` if unknown), `free_mb` (free space on the output
filesystem, MB), `writer.error_msg` (`""`, or e.g. `"write .mjpeg: No space
left on device"`) and `writer.discarded` (frames dropped after an output
error). `writer.error` stays 0/1.

## Disk I/O never blocks capture

The first version wrote from the capture thread and called fdatasync every
5 s. On the CM4 that flush stalled the process for 200–340 ms, and both
cameras lost 5–9 frames at every flush: the uvcvideo driver keeps only a
few URBs in flight and resubmits them from a work item that the SD-card
writeback starved, so isochronous data was lost on the bus even though
V4L2 buffers were free. Two fixes:

- the capture thread only dequeues, copies the frame into a RAM queue
  (`--ring`, default 64 MB) and requeues; a writer thread drains the queue
  and starts writeback every `--wb` MB (default 4) or every 2 s with
  `sync_file_range`, dropping written chunks from the page cache, so dirty
  data never piles up into a large flush. If the writer falls behind the
  budget, frames are counted as `writer.overruns` in the summary (exit 2)
  rather than blocking. The stats line, `STATUS=`, the live JSON and the
  summary are produced by the writer thread too (from a snapshot taken
  under the queue lock); per-frame messages from the capture thread (gaps,
  queue full) are limited to one a second, every gap still goes into
  `gap_list`.
- the image's patched uvcvideo keeps 32 URBs in flight instead of 5
  (~128 ms of USB-side buffering per camera instead of ~20 ms).

The statistics line and `summary.json` report the queue peak, overruns and
the longest single write call, so an SD card that is getting slow shows up
before it costs frames.

## Power cuts and card errors

- Each writeback step waits for the chunk kicked out by the step before
  (`SYNC_FILE_RANGE_WAIT_BEFORE|WRITE|WAIT_AFTER`: data already on its way,
  so the wait is short) and checks the result; `SYNC_FILE_RANGE_WRITE`
  alone never reports an I/O error. The `_timestamps.bin` and `_index.bin`
  files get the same steps as the video, so on a power cut all three lose
  the same last few seconds (before 0.6 the index and timestamps sat in the
  page cache for ~30 s).
- Any failed write, writeback or final `fdatasync` is an output error: the
  three files are cut back to the last complete frame (every index record
  still points at a whole frame), the recording stops and the process exits
  4, so systemd restarts it into a new session instead of the recorder
  running on with nothing reaching the card. The error is in the stats line
  (`OUTPUT ERROR: ...`), the live JSON and the summary.

## Drop accounting

The driver increments the sequence number for every frame the camera
transmitted; a gap in the sequence is a frame lost between camera and
driver (USB bandwidth, URB errors). ewego-cam logs each gap as it happens,
counts lost frames, and exits with status 2 if anything was lost or
error-flagged, so a bench test is just the exit code. Status 3 means the
camera stopped delivering frames (`--stall` seconds, default 10); under
systemd that triggers a restart.

| Exit | Meaning |
|---|---|
| 0 | clean, nothing lost (also: stopped while waiting for the camera, nothing recorded) |
| 1 | usage or device error, no camera after `--wait` |
| 2 | frames lost, error-flagged, or dropped by a full write queue |
| 3 | camera stalled (`--stall`) |
| 4 | output error: cannot create or write the files (card full, I/O error, or `--no-session-dir` onto an existing recording); takes precedence over 1, 2 and 3 |

Every `--stats` seconds (default 5) it prints frames, lost, error frames,
effective fps, interval min/mean/max, data rate, queue, longest write and
free space on the output filesystem, and pushes the same line to
`systemctl status` via the notify socket.

## Timestamps

- The value recorded is the kernel's stamp at arrival of the frame's first
  packet: exposure end plus readout plus the camera's JPEG encode, a near
  constant offset for a given camera, quantised by the driver's URB size
  (up to 4 ms). `summary.json` records whether the driver reported the
  clock as monotonic and the source as start-of-exposure (only with
  `uvcvideo hwtimestamps=1`).
- Timestamps that go backwards are reported and not used for interval
  statistics.
- Wall time: `realtime_minus_monotonic_us` in the summary, taken at start
  and at end, converts the monotonic stamps to UTC for a session.

## Live view

`--live DIR` keeps two small files in DIR (meant for tmpfs, `/run/ewego`
on the image): `<name>.jpg`, the newest frame, replaced atomically at most
five times a second from the writer thread, and `<name>.json`, the
counters (frames, lost, overruns, fps over the last second, write max,
queue), rewritten once a second. Keys added in 0.6: `port`, `free_mb`
(free space on the output filesystem in MB, `null` in `--preview`),
`writer_error` (0/1), `writer_error_msg` (`""` or e.g.
`"write .mjpeg: No space left on device"`) and `discarded`. The web console's `/live` page streams the
JPEG to the browser at the rate you choose there, so the Wi-Fi cost is
yours to set (a 1080p MJPEG frame is 150–300 kB). `--preview --live DIR`
streams the camera into those files and records nothing; the console uses
it to show the cameras before a recording starts. Both files are removed at
exit.

## Under systemd

`ewego-cam@camera1.service` and `ewego-cam@camera2.service` read
`/etc/ewego/cam-camera1.conf` / `cam-camera2.conf` (DEVICE, SIZE, FPS,
OUT_DIR, EXTRA_ARGS). Installed on the image, not enabled. DEVICE defaults
to `auto:1/2` / `auto:2/2`; pin a `port:` from `ewego-cam --list` only if a
specific physical side must keep a name when the hub wiring changes.
EXTRA_ARGS defaults to `--stats 5 --live /run/ewego`.

The unit is `Type=notify` with `WatchdogSec=30`: the writer thread sends
`WATCHDOG=1` only right after a frame reached the files (at most every
2 s; also during the `--wait` loop), so a writer stuck on the card, a
stuck capture thread or a camera that stopped delivering ends in a
restart. `TimeoutStopSec=90` covers draining a full queue through a busy
card; while the drain makes progress the recorder also sends
`EXTEND_TIMEOUT_USEC`. `StartLimitIntervalSec=0` (never stop restarting),
`OOMScoreAdjust=-500`. Signal handlers are installed before the device
wait, so `systemctl stop` at any point ends cleanly.

## Testing without a camera

`--source FILE` replays the JPEG frames found in FILE (for example an
`.mjpeg` from an earlier recording) once at `--fps`, with synthetic
`CLOCK_MONOTONIC` timestamps and consecutive sequence numbers, through the
same queue, writer, live files and summary as a camera, and stops at the end
of the file as on SIGINT (exit 0 and the final summary; the recorded
`.mjpeg` is byte-identical to FILE when FILE is a concatenation of
SOI..EOI frames). `--device` is not needed. `--source-loop` replays the
file over and over instead (a soak test; stop with `--seconds` or SIGINT);
`--seconds` also cuts a single replay short. `--source-lose N` skips one sequence number every N frames, to
exercise the loss accounting. For an output error, limit the file size:

    ewego-cam --source old.mjpeg --out /tmp/t                                  # one pass, exit 0
    ewego-cam --source old.mjpeg --out /tmp/t --source-loop --seconds 600      # soak, exit 0
    ewego-cam --source old.mjpeg --out /tmp/t --source-loop --seconds 10 --source-lose 30  # exit 2
    (trap '' XFSZ; ulimit -f 10000; ewego-cam --source old.mjpeg --out /tmp/t --source-loop --seconds 10)  # exit 4

## Build

    make                              # native
    make CC=aarch64-linux-gnu-gcc     # cross for the Pi; CI does this and
                                      # the image injector installs the result
