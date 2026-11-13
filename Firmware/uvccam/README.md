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

## Output

A session directory `<out>/<YYYYmmdd_HHMMSS>/` (or `--out` itself with
`--no-session-dir`) containing:

| File | Content |
|---|---|
| `<name>.mjpeg` | concatenated JPEG frames; `dualcam/play_with_timestamps.py` reads this |
| `<name>_timestamps.bin` | int64 little-endian, microseconds, `CLOCK_MONOTONIC`, one per frame (same format as the CSI recorder) |
| `<name>_index.bin` | 32-byte records: u64 byte offset in `.mjpeg`, i64 timestamp µs, u32 driver sequence, u32 bytes, u32 V4L2 flags, u32 reserved |
| `<name>_summary.json` | frames, lost, gaps and where, error-flagged frames, interval min/mean/max, effective fps, timestamp clock and source, and the `CLOCK_REALTIME` minus `CLOCK_MONOTONIC` offset at start and end, so timestamps can be mapped to wall time |

## Disk I/O never blocks capture

The first version wrote from the capture thread and called fdatasync every
5 s. On the CM4 that flush stalled the process for 200–340 ms, and both
cameras lost 5–9 frames at every flush: the uvcvideo driver keeps only a
few URBs in flight and resubmits them from a work item that the SD-card
writeback starved, so isochronous data was lost on the bus even though
V4L2 buffers were free. Two fixes:

- the capture thread only dequeues, copies the frame into a RAM queue
  (`--ring`, default 64 MB) and requeues; a writer thread drains the queue
  and starts writeback every `--wb` MB (default 4) with `sync_file_range`,
  dropping written chunks from the page cache, so dirty data never piles
  up into a large flush. If the writer falls behind the budget, frames are
  counted as `writer.overruns` in the summary (exit 2) rather than blocking.
- the image's patched uvcvideo keeps 32 URBs in flight instead of 5
  (~128 ms of USB-side buffering per camera instead of ~20 ms).

The statistics line and `summary.json` report the queue peak, overruns and
the longest single write call, so an SD card that is getting slow shows up
before it costs frames.

## Drop accounting

The driver increments the sequence number for every frame the camera
transmitted; a gap in the sequence is a frame lost between camera and
driver (USB bandwidth, URB errors). ewego-cam logs each gap as it happens,
counts lost frames, and exits with status 2 if anything was lost or
error-flagged, so a bench test is just the exit code. Status 3 means the
camera stopped delivering frames (`--stall` seconds, default 10); under
systemd that triggers a restart.

Every `--stats` seconds (default 5) it prints frames, lost, error frames,
effective fps, interval min/mean/max and data rate, and pushes the same
line to `systemctl status` via the notify socket.

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

## Under systemd

`ewego-cam@camera1.service` and `ewego-cam@camera2.service` read
`/etc/ewego/cam-camera1.conf` / `cam-camera2.conf` (DEVICE, SIZE, FPS,
OUT_DIR, EXTRA_ARGS). Installed on the image, not enabled. DEVICE defaults
to `auto:1/2` / `auto:2/2`; pin a `port:` from `ewego-cam --list` only if a
specific physical side must keep a name when the hub wiring changes.

## Build

    make                              # native
    make CC=aarch64-linux-gnu-gcc     # cross for the Pi; CI does this and
                                      # the image injector installs the result
