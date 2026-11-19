# EweGo web test console

`ewego_webtest.py` is a single-file HTTP server (Python standard library
only) for exercising each piece of a wearable from a browser. It runs the same
tools you would use over SSH and streams their output to the page.

Open `http://<hostname>.local:8080/` on any device on the same network.
`http://<hostname>.local:8080/live` is the field dashboard (below).

## What it shows

- **Status strip**, refreshed every 5 s: image version, uptime, clock
  synchronized or not, battery voltage and state of charge, free space,
  load and temperature, IP addresses. While `ewego-fuel` runs, the battery
  comes from the newest row of its `fuel_gauge.csv` (the console does not
  open the I2C bus the logger is using); otherwise from the MAX17048
  directly.
- **Devices**: whether `/dev/ttyAMA4`, `/dev/ttyAMA5`, `/dev/i2c-1`, the
  voicehat sound card and any cameras exist, plus buttons for serial port
  mapping, `i2cdetect`, USB tree, ALSA cards, the effective `config.txt`,
  and a `dmesg` tail.
- **Units**: state of `ewego-record.target` and its members (`ewego-cam@camera1/2`,
  `ewego-imu-usb`, `ewego-gps`, `ewego-audio`, `ewego-fuel`), with
  start/stop, journal tail (also of the preview units), and a listing of
  recording directories. Starting a unit is refused while a console test
  or the GPS serial bridge holds its device.
- **Fuel gauge**: read voltage, SOC, IC version.
- **IMU**: run `log_imu_data.py` for N seconds and watch it (refused while
  `ewego-imu-uart` or `ewego-preview-imu-uart` owns the port).
- **GPS**: raw read of the port at a chosen baud rate (counts UBX sync
  words, NMEA sentences, RTCM headers), and start/stop/journal of the logger
  unit. The raw read refuses to run while the logger owns the port.
- **PPS**: the status strip shows whether `/dev/pps0` (GPS TP1 on GPIO 6)
  exists, whether edges are arriving, the sequence count, the measured rate,
  and the system clock's offset from the top of the GPS second at the last
  edge (near 0 ms once the clock is on GPS time). "PPS watch" on the GPS
  card prints each edge for N seconds with the interval since the previous
  one, so jitter and missed pulses are visible. TP1 only pulses once the
  receiver has a fix.
- **Audio**: record N seconds from the voicehat card and play it back in
  the browser.
- **Camera**: live MJPEG view (choose device, size, fps), snapshot, and
  the camera's format list. The live view uses `v4l2-ctl` exactly as the
  SSH streaming test does.
- **Drop test, all cameras at once**: captures N seconds from every camera
  simultaneously to `/dev/null` (no streaming, nothing written) and
  reports, per camera, frames received versus expected, frames lost
  according to the kernel's per-buffer sequence numbers and where the gaps
  were, error-flagged frames, effective fps, min/max inter-frame interval,
  and data rate, then PASS/FAIL and any recent UVC/USB kernel messages.
  This is the bench test for two cameras on a hub.
- **Recorder**: start/stop `ewego-record.target` (all recorders at once),
  camera and IMU journals, recording directories, **Check last run** (runs
  `Firmware/tools/ewego-check.py`: a PASS/WARN/FAIL line per stream of the
  latest run, and how far apart the streams started; the last line names
  the tool's exit status: 0 PASS, 1 WARN, 2 FAIL, 3 no recordings) and
  **List runs**. Starting or restarting `ewego-record.target` or a recorder
  unit is queued (`systemctl start --no-block`): the recorders are
  `Type=notify` and can take up to 30 s (camera / hub_imu device wait) to
  report ready, so the request answers at once and the Units table (and
  the live page's badge) follow them up. A refusal (a console test or the
  serial bridge holds the device) still answers 409 at once. Stop stays
  synchronous, so every summary is written when it returns. Recorder exit
  codes are shown with their meaning wherever they appear: 1 setup or
  device error, 2 frames/samples lost, flagged or overrun, 3 stalled or
  device gone, 4 output error (card full / I/O).
  Check last run and the ewego-cam record test are refused while
  `ewego-record.target` is up: they read or write a lot on the SD card the
  recorders are writing to. Stop recording first, or use SSH. Journal
  tails stay available while recording (at most 200 lines,
  `journalctl -u <unit> -n N --no-pager`): they are the diagnostic for a
  recorder misbehaving during a live run.
- **Shut down / Reboot**: stops `ewego-record.target` first so every
  recorder writes its summary, runs `sync`, then powers off or reboots.
  Use it instead of the power switch after a recording: cutting power
  mid-write is what corrupts the card.

## Live view (`/live`)

A dashboard for the field, built for a phone on the wearable's Wi-Fi:

- **Both cameras**, each with a *Flip 180°* button (remembered per camera
  in the browser) for a wearable mounted upside down, *Swap cameras* to
  put camera2 on the left when that is the left eye, and a stats line
  (fps, frames, lost/overruns, write max, queue, then in small text the
  writer error count and message, frames discarded after an output error,
  and the free space the recorder last measured, red when non-zero or
  below the free-space minimum). The frames are the
  recorders' own: `ewego-cam --live /run/ewego` keeps the newest JPEG on
  tmpfs and the page shows it as a plain multipart image stream (the same
  mechanism as the test console's camera view) at the **view rate** you
  pick (0.5–5 fps; the header shows the kB/s the wearable is sending). A 1080p MJPEG
  frame is 150–300 kB, so 2 fps for two cameras is roughly 1 MB/s and
  5 fps is about the most a weak Wi-Fi link will carry. *Pause views* stops
  the image streams and leaves the rest running.
- **Both IMUs**: scrolling 10 s traces of acceleration, angular rate and
  orientation at the full 100 Hz (hover for values), with rate, lost or
  failed reads, calibration status and temperature (and in small text the
  writer error, free space and, for the UART IMU, BNO055 fusion stalls,
  red when non-zero), for the USB module
  (`ewego-imu-usb --live /run/ewego`) and the wearable's own BNO055 on
  UART5 (`ewego-imu-uart --live /run/ewego`), about 10 kB/s each.
- **GPS**: fix type, satellites, accuracy, position (with a map link),
  height, speed and GPS time from the newest NAV-PVT in the GPS logger's
  file, so it is live only while `ewego-gps` runs and a few seconds behind
  the receiver; PPS from the kernel directly.
- **Wearable**: battery, free space, load and temperature, unit states.
- **Badge**: *RECORDING* (solid red) only when every recorder
  (`ewego-cam@camera1/2`, `ewego-imu-usb`, `ewego-imu-uart`) is active,
  its live file in `/run/ewego` is less than 2 s old, and it reports no
  `writer_error`. Otherwise the badge has a red outline and names the
  ones that are not, e.g. *RECORDING (camera2 down, imu-uart starting)*,
  and the Wearable card says why for each (failed, with the exit code and
  its meaning, e.g. *failed (exit 4: output error (card full / I/O))*; no
  live data; stale; writer error with its message). A recorder that is
  inactive because its free-space check (`ewego-check-space`, the units'
  `ExecCondition=`) skipped it reads *skipped: 312 MB free (min 500)*, from
  `/run/ewego/space.json`, and the badge says *camera1 skipped*.
  `ewego-record.target` alone is not enough: it stays active while its
  members fail. *PREVIEW*, *IDLE* and *NO LINK* as before. Only the named
  live files are read from `/run/ewego`; the `port-*.lock` files (ewego-cam
  port claims) and `space.json` that also live there are never listed or
  removed by the console.
- **Controls**: *Preview (no recording)* starts the preview units
  `ewego-preview@camera1`, `ewego-preview@camera2`,
  `ewego-preview-imu-usb` and `ewego-preview-imu-uart` (the recorders in
  `--preview`) so you can aim the cameras and check the IMUs before
  recording; they write nothing to the card. The console stops them after
  90 s without a viewer (and each unit stops itself after an hour if the
  console is gone). A preview refuses to start while its recorder is up;
  starting a recorder, from the page, from SSH or at boot, stops its
  preview first, so the two never share a device. Their output is in the
  journal (`journalctl -u 'ewego-preview*'`, or the Units card on the
  console). *Shut down* and *Reboot* as on the console.

The page is a single server-sent-events stream (4 updates a second) plus
one multipart image stream per camera; nothing is decoded on the wearable.
At most 6 such streams are open at once (one page uses 3); beyond that the
wearable answers 503 and the page retries after 5 s. A phone that walks
out of range frees its streams within about 20 s (send timeout).
(A first version read the image streams with a JavaScript fetch so the
browser could count bytes; it showed nothing in the field browser, so the
images are plain `<img>` elements and the wearable counts what it sends.)

## Running it

It is installed and enabled on the EweGo image as `ewego-webtest.service`.
To run it by hand somewhere else:

    sudo python3 ewego_webtest.py --port 8080 --ewego-dir /path/to/EweGo

Only one test may use a given device at a time; a second request gets
HTTP 409 with a message. A device test (and the GPS serial bridge, and the
camera view) also gets 409 while the unit that owns the device is
`active`, `activating`, `reloading` or `deactivating`, naming the unit and
its state; a serial open that fails with EBUSY reads "port owned by the
recorder".

The preview units live in `image/units/` next to the recorders. When
iterating without an image rebuild, copy them too (see below).

## Security

There is no authentication and it runs as root. Use it only on networks you
control (bench, lab, phone hotspot). Every POST and every `/api/run/` test
must carry the header `X-EweGo: 1` (the pages send it; anything else gets
403). A page on another site can only make "simple" cross-site requests,
which cannot carry custom headers, so it cannot drive the console through
your browser. That is not authentication: anyone on the network can still
send the header. Disable the console on a wearable that will sit on a
network you do not control:

    sudo systemctl disable --now ewego-webtest

## Iterating without rebuilding the image

Copy the file straight to the wearable and restart the unit:

    scp Firmware/webtest/ewego_webtest.py user@ewe.local:/tmp/ && \
      ssh user@ewe.local 'sudo install -m 644 /tmp/ewego_webtest.py /opt/ewego/Firmware/webtest/ && sudo systemctl restart ewego-webtest'

On an image older than the preview units, copy those once as well:

    scp image/units/ewego-preview*.service user@ewe.local:/tmp/ && \
      ssh user@ewe.local 'sudo install -m 644 /tmp/ewego-preview*.service /etc/systemd/system/ && sudo systemctl daemon-reload'

Tests (no hardware needed): `python3 -m unittest discover -s tests -v`
from the repository root.
