# EweGo web test console

`ewego_webtest.py` is a single-file HTTP server (Python standard library
only) for exercising each piece of a wearable from a browser. It runs the same
tools you would use over SSH and streams their output to the page.

Open `http://<hostname>.local:8080/` on any device on the same network.
`http://<hostname>.local:8080/live` is the field dashboard (below).

## What it shows

- **Status strip**, refreshed every 5 s: image version, uptime, clock
  synchronized or not, battery voltage and state of charge, free space,
  load and temperature, IP addresses.
- **Devices**: whether `/dev/ttyAMA4`, `/dev/ttyAMA5`, `/dev/i2c-1`, the
  voicehat sound card and any cameras exist, plus buttons for serial port
  mapping, `i2cdetect`, USB tree, ALSA cards, the effective `config.txt`,
  and a `dmesg` tail.
- **Units**: state of `ewego-record.target` and its members (`ewego-cam@camera1/2`,
  `ewego-imu-usb`, `ewego-gps`, `ewego-audio`, `ewego-fuel`), with
  start/stop, journal tail, and a listing of recording directories.
- **Fuel gauge**: read voltage, SOC, IC version.
- **IMU**: run `log_imu_data.py` for N seconds and watch it.
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
  latest run, and how far apart the streams started) and **List runs**.
- **Shut down / Reboot**: stops `ewego-record.target` first so every
  recorder writes its summary, runs `sync`, then powers off or reboots.
  Use it instead of the power switch after a recording: cutting power
  mid-write is what corrupts the card.

## Live view (`/live`)

A dashboard for the field, built for a phone on the wearable's Wi-Fi:

- **Both cameras**, each with a *Flip 180°* button (remembered per camera
  in the browser) for a wearable mounted upside down, and a stats line
  (fps, frames, lost/overruns, write max, queue). The frames are the
  recorders' own: `ewego-cam --live /run/ewego` keeps the newest JPEG on
  tmpfs and the page fetches it as a multipart stream at the **view rate**
  you pick (0.5–5 fps; the header shows the measured kB/s). A 1080p MJPEG
  frame is 150–300 kB, so 2 fps for two cameras is roughly 1 MB/s and
  5 fps is about the most a weak Wi-Fi link will carry. *Pause views* stops
  the image streams and leaves the rest running.
- **USB IMU**: scrolling 10 s traces of acceleration, angular rate and
  orientation at the full 100 Hz (hover for values), with rate, lost,
  drift, calibration status and temperature. Fed by
  `ewego-imu-usb --live /run/ewego`, about 10 kB/s.
- **GPS**: fix type, satellites, accuracy, position (with a map link),
  height, speed and GPS time from the newest NAV-PVT in the GPS logger's
  file, so it is live only while `ewego-gps` runs and a few seconds behind
  the receiver; PPS from the kernel directly.
- **Wearable**: battery, free space, load and temperature, unit states.
- **Controls**: *Preview (no recording)* starts `ewego-cam --preview` for
  both cameras and `ewego-imu-usb --preview` so you can aim the cameras and
  check the IMU before recording; it writes nothing to the card and stops
  itself after 90 s without a viewer. *Start recording* stops the preview
  first (the recorders need the cameras). *Shut down* and *Reboot* as on
  the console.

The page is a single server-sent-events stream (4 updates a second) plus
one multipart image stream per camera; nothing is decoded on the wearable.

## Running it

It is installed and enabled on the EweGo image as `ewego-webtest.service`.
To run it by hand somewhere else:

    sudo python3 ewego_webtest.py --port 8080 --ewego-dir /path/to/EweGo

Only one test may use a given device at a time; a second request gets
HTTP 409 with a message.

## Security

There is no authentication and it runs as root. Use it only on networks you
control (bench, lab, phone hotspot). Disable it on a wearable that will sit
on a network you do not control:

    sudo systemctl disable --now ewego-webtest

## Iterating without rebuilding the image

Copy the file straight to the wearable and restart the unit:

    scp Firmware/webtest/ewego_webtest.py user@ewe.local:/tmp/ && \
      ssh user@ewe.local 'sudo install -m 644 /tmp/ewego_webtest.py /opt/ewego/Firmware/webtest/ && sudo systemctl restart ewego-webtest'
