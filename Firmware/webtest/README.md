# EweGo web test console

`ewego_webtest.py` is a single-file HTTP server (Python standard library
only) for exercising each piece of a collar from a browser. It runs the same
tools you would use over SSH and streams their output to the page.

Open `http://<hostname>.local:8080/` on any device on the same network.

## What it shows

- **Status strip**, refreshed every 5 s: image version, uptime, clock
  synchronized or not, battery voltage and state of charge, free space,
  load and temperature, IP addresses.
- **Devices**: whether `/dev/ttyAMA4`, `/dev/ttyAMA5`, `/dev/i2c-1`, the
  voicehat sound card and any cameras exist, plus buttons for serial port
  mapping, `i2cdetect`, USB tree, ALSA cards, the effective `config.txt`,
  and a `dmesg` tail.
- **Units**: state of `ewego-sensors`, `ewego-gps`, `ewego-dualcam`, with
  start/stop, journal tail, and a listing of session directories.
- **Fuel gauge**: read voltage, SOC, IC version.
- **IMU**: run `log_imu_data.py` for N seconds and watch it.
- **GPS**: raw read of the port at a chosen baud rate (counts UBX sync
  words, NMEA sentences, RTCM headers), and start/stop/journal of the logger
  unit. The raw read refuses to run while the logger owns the port.
- **Audio**: record N seconds from the voicehat card and play it back in
  the browser.
- **Camera**: live MJPEG view (choose device, size, fps), snapshot, and
  the camera's format list. The live view uses `v4l2-ctl` exactly as the
  SSH streaming test does.
- **All-sensors recorder**: start/stop `ewego-sensors`, journal, sessions.

## Running it

It is installed and enabled on the collar image as `ewego-webtest.service`.
To run it by hand somewhere else:

    sudo python3 ewego_webtest.py --port 8080 --ewego-dir /path/to/EweGo

Only one test may use a given device at a time; a second request gets
HTTP 409 with a message.

## Security

There is no authentication and it runs as root. Use it only on networks you
control (bench, lab, phone hotspot). Disable it on a collar that will sit
on a network you do not control:

    sudo systemctl disable --now ewego-webtest

## Iterating without rebuilding the image

Copy the file straight to the collar and restart the unit:

    scp Firmware/webtest/ewego_webtest.py user@ewe.local:/tmp/ && \
      ssh user@ewe.local 'sudo install -m 644 /tmp/ewego_webtest.py /opt/ewego/Firmware/webtest/ && sudo systemctl restart ewego-webtest'
