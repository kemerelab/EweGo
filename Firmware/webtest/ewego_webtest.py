#!/usr/bin/env python3
"""
EweGo web test console.

A small HTTP server (Python standard library only) that lets you exercise
each piece of a collar from a browser: status strip, one test per sensor
with live output, a live camera view, unit start/stop, journal tails and
a session listing. Everything runs the same tools you would use over SSH;
this just puts their output in a browser.

    python3 ewego_webtest.py [--port 8080] [--ewego-dir /opt/ewego]

Runs as root under ewego-webtest.service. No authentication: only expose it
on networks you control.
"""

import argparse
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

EWEGO = Path(os.environ.get("EWEGO_DIR", "/opt/ewego"))
RUN_DIR = Path(os.environ.get("RUNTIME_DIRECTORY", "/run/ewego-webtest"))
UNITS = ["ewego-sensors", "ewego-gps", "ewego-dualcam"]
GPS_PORT = "/dev/ttyAMA4"
GPS_BAUD = 460800
IMU_PORT = "/dev/ttyAMA5"
FUEL_BUS = 1
FUEL_ADDR = 0x36
SERIAL_PORTS = {"gps": GPS_PORT, "imu": IMU_PORT}

# One test per device at a time, so a stray IMU test cannot fight the
# recorder for the serial port.
LOCKS = {k: threading.Lock() for k in ("imu", "gps", "camera", "audio")}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def sh(argv, timeout=15):
    """Run a command, return (rc, combined output)."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           errors="replace")
        return p.returncode, (p.stdout + p.stderr)
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found\n"
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(argv)}: timed out after {timeout}s\n"


def read_release():
    out = {}
    try:
        for line in Path("/etc/ewego-image-release").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return out


def clock_status():
    rc, out = sh(["timedatectl", "show", "-p", "NTPSynchronized", "-p", "NTP"])
    synced = "NTPSynchronized=yes" in out
    return {"synchronized": synced, "utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())}


def battery():
    try:
        import smbus2  # apt: python3-smbus2, present on the image
    except ImportError:
        return {"error": "smbus2 not installed"}
    try:
        with smbus2.SMBus(FUEL_BUS) as bus:
            v = bus.read_i2c_block_data(FUEL_ADDR, 0x02, 2)
            s = bus.read_i2c_block_data(FUEL_ADDR, 0x04, 2)
            ver = bus.read_i2c_block_data(FUEL_ADDR, 0x08, 2)
        vraw = (v[0] << 8) | v[1]
        sraw = (s[0] << 8) | s[1]
        return {
            "voltage": round((vraw >> 4) * 0.00125, 3),
            "soc": round((sraw >> 8) + (sraw & 0xFF) / 256.0, 2),
            "version": f"0x{(ver[0] << 8) | ver[1]:04X}",
        }
    except OSError as e:
        return {"error": str(e)}


def audio_card():
    """Return (card index, name) of the voicehat card, or None."""
    try:
        txt = Path("/proc/asound/cards").read_text()
    except OSError:
        return None
    for m in re.finditer(r"^\s*(\d+)\s+\[([^\]]+)\]\s*:\s*(.*)$", txt, re.M):
        idx, short, desc = m.group(1), m.group(2).strip(), m.group(3)
        if "voicehat" in short.lower() or "voicehat" in desc.lower():
            return int(idx), short
    return None


def audio_device():
    card = audio_card()
    # plughw lets ALSA convert to S16 for the browser
    return f"plughw:{card[0]},0" if card else "default"


def video_devices():
    """Capture nodes, one per camera, from v4l2-ctl --list-devices."""
    rc, out = sh(["v4l2-ctl", "--list-devices"])
    devs = []
    if rc == 0:
        block_name, first = None, None
        for line in out.splitlines() + [""]:
            if line and not line.startswith(("\t", " ")):
                if first:
                    devs.append({"dev": first, "name": block_name})
                block_name, first = line.strip().rstrip(":"), None
            elif line.strip().startswith("/dev/video") and first is None:
                first = line.strip()
        if first:
            devs.append({"dev": first, "name": block_name})
    if not devs:
        devs = [{"dev": str(p), "name": p.name}
                for p in sorted(Path("/dev").glob("video*"))]
    return devs


def unit_status(unit):
    rc, active = sh(["systemctl", "is-active", unit])
    rc2, enabled = sh(["systemctl", "is-enabled", unit])
    return {"unit": unit, "active": active.strip(), "enabled": enabled.strip()}


def status():
    du = shutil.disk_usage(EWEGO if EWEGO.exists() else "/")
    try:
        temp = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000.0
    except (OSError, ValueError):
        temp = None
    try:
        uptime = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError):
        uptime = None
    rc, ips = sh(["hostname", "-I"])
    devs = {p: os.path.exists(p) for p in (GPS_PORT, IMU_PORT, f"/dev/i2c-{FUEL_BUS}")}
    card = audio_card()
    return {
        "hostname": socket.gethostname(),
        "ips": ips.split(),
        "uptime_s": uptime,
        "load": os.getloadavg(),
        "temp_c": temp,
        "release": read_release(),
        "clock": clock_status(),
        "disk": {"free_gb": round(du.free / 1e9, 2), "total_gb": round(du.total / 1e9, 2),
                 "path": str(EWEGO)},
        "battery": battery(),
        "devices": devs,
        "audio_card": {"index": card[0], "name": card[1]} if card else None,
        "video": video_devices(),
        "units": [unit_status(u) for u in UNITS + ["ewego-webtest"]],
    }


def open_serial_raw(port, baud):
    fd = os.open(port, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    speed = getattr(termios, f"B{baud}")
    cflag = attrs[2]
    cflag = (cflag & ~termios.CSIZE) | termios.CS8 | termios.CREAD | termios.CLOCAL
    cflag &= ~(termios.PARENB | termios.CSTOPB | termios.CRTSCTS)
    attrs = [0, 0, cflag, 0, speed, speed, attrs[6]]
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIFLUSH)
    return fd


def sessions():
    out = []
    if not EWEGO.exists():
        return out
    candidates = list(EWEGO.glob("sensor_test_*")) + list((EWEGO / "recordings").glob("*")) \
        + list((EWEGO / "gps-logs").glob("*"))
    for p in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[:40]:
        if p.is_dir():
            size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        else:
            size = p.stat().st_size
        out.append({"path": str(p.relative_to(EWEGO)), "mtime": int(p.stat().st_mtime),
                    "size_mb": round(size / 1e6, 1)})
    return out


# ---------------------------------------------------------------------------
# tests: each yields lines of text; some run a subprocess
# ---------------------------------------------------------------------------

def child_env():
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("PYTHONPATH", str(EWEGO / "pylib"))
    return env


def stream_process(argv, write, cwd=None, lock_name=None):
    """Run argv, forwarding its output to write(bytes). Kills it if the
    client goes away. Returns the exit code."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            cwd=cwd, env=child_env(), start_new_session=True)
    try:
        write(f"$ {' '.join(argv)}\n".encode())
        for chunk in iter(lambda: proc.stdout.read1(4096), b""):
            write(chunk.replace(b"\r", b"\n"))
        rc = proc.wait()
        write(f"\n[exit {rc}]\n".encode())
        return rc
    finally:
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_fuel(write, q):
    write(b"MAX17048 fuel gauge on I2C bus 1, address 0x36\n")
    b = battery()
    for k, v in b.items():
        write(f"  {k}: {v}\n".encode())
    write(b"\n$ i2cdetect -y 1\n")
    rc, out = sh(["i2cdetect", "-y", str(FUEL_BUS)])
    write(out.encode())


def test_imu(write, q):
    secs = int(q.get("seconds", ["10"])[0])
    cwd = RUN_DIR / "imu"
    cwd.mkdir(parents=True, exist_ok=True)
    write(f"BNO055 on {IMU_PORT}: logging for {secs}s (log goes to {cwd}/logs, not kept)\n".encode())
    stream_process(["timeout", "-s", "INT", "-k", "5", str(secs), sys.executable, "-u",
                    str(EWEGO / "Firmware/IMU/log_imu_data.py"),
                    "--port", IMU_PORT, "--rate", "50"], write, cwd=str(cwd))


def test_gps_raw(write, q):
    secs = int(q.get("seconds", ["3"])[0])
    baud = int(q.get("baud", [str(GPS_BAUD)])[0])
    if unit_status("ewego-gps")["active"] == "active":
        write(b"ewego-gps is running and owns the port. Stop it first, or read its journal instead.\n")
        return
    write(f"Reading {GPS_PORT} at {baud} baud for {secs}s ...\n".encode())
    try:
        fd = open_serial_raw(GPS_PORT, baud)
    except (OSError, AttributeError) as e:
        write(f"open failed: {e}\n".encode())
        return
    data = bytearray()
    end = time.monotonic() + secs
    try:
        while time.monotonic() < end:
            r, _, _ = select.select([fd], [], [], 0.2)
            if r:
                try:
                    data += os.read(fd, 65536)
                except BlockingIOError:
                    pass
    finally:
        os.close(fd)
    ubx = data.count(b"\xb5\x62")
    nmea = data.count(b"$G")
    rtcm = data.count(b"\xd3\x00")
    write(f"  bytes: {len(data)}  ({len(data) / secs / 1024:.1f} KB/s)\n".encode())
    write(f"  UBX sync words: {ubx}   NMEA sentences: {nmea}   RTCM3 headers: {rtcm}\n".encode())
    if not data:
        write(b"  nothing received: check baud, dtoverlay=uart4, and that the module is powered\n")
    else:
        write(b"  first 64 bytes: " + data[:64].hex(" ").encode() + b"\n")
        if ubx == 0 and nmea == 0:
            write(b"  no UBX or NMEA framing: most likely the wrong baud rate\n")
        # any NMEA text is readable as a sanity check
        m = re.search(rb"\$G[A-Z]{4},[^\r\n]*", data)
        if m:
            write(b"  e.g. " + m.group(0)[:120] + b"\n")


def test_audio(write, q):
    secs = int(q.get("seconds", ["5"])[0])
    dev = audio_device()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    wav = RUN_DIR / "audio.wav"
    write(f"Recording {secs}s from {dev} (48 kHz stereo, converted to 16-bit for the browser)\n".encode())
    rc = stream_process(["arecord", "-D", dev, "-f", "S16_LE", "-c", "2", "-r", "48000",
                         "-d", str(secs), str(wav)], write)
    if rc == 0 and wav.exists():
        write(f"saved {wav.stat().st_size} bytes; press play below\n".encode())


def test_alsa(write, q):
    for argv in (["arecord", "-l"], ["cat", "/proc/asound/cards"]):
        stream_process(argv, write)


def test_camera_info(write, q):
    dev = q.get("dev", [None])[0] or (video_devices() or [{"dev": "/dev/video0"}])[0]["dev"]
    stream_process(["v4l2-ctl", "--list-devices"], write)
    stream_process(["v4l2-ctl", "-d", dev, "--list-formats-ext"], write)
    stream_process(["v4l2-ctl", "-d", dev, "--get-fmt-video"], write)


def test_usb(write, q):
    stream_process(["lsusb", "-t"], write)
    stream_process(["lsusb"], write)


def test_dmesg(write, q):
    stream_process(["bash", "-c", "dmesg -T | tail -n 80"], write)


def test_i2c(write, q):
    stream_process(["i2cdetect", "-y", str(FUEL_BUS)], write)


def test_serial_ports(write, q):
    stream_process(["bash", "-c", "ls -l /dev/ttyAMA* /dev/serial* 2>&1; echo; "
                    "for t in /sys/class/tty/ttyAMA*; do echo \"$(basename $t) -> $(readlink $t/device)\"; done"],
                   write)


def test_config(write, q):
    stream_process(["bash", "-c", "grep -v '^#' /boot/firmware/config.txt | grep -v '^$'; echo; "
                    "echo cmdline.txt:; cat /boot/firmware/cmdline.txt"], write)


TESTS = {
    "fuel": (test_fuel, None),
    "imu": (test_imu, "imu"),
    "gps-raw": (test_gps_raw, "gps"),
    "audio": (test_audio, "audio"),
    "alsa": (test_alsa, None),
    "camera-info": (test_camera_info, "camera"),
    "usb": (test_usb, None),
    "dmesg": (test_dmesg, None),
    "i2c": (test_i2c, None),
    "serial": (test_serial_ports, None),
    "config": (test_config, None),
}


# ---------------------------------------------------------------------------
# camera snapshot / stream via v4l2-ctl
# ---------------------------------------------------------------------------

def v4l2_stream_argv(dev, width, height, fps, count):
    return ["v4l2-ctl", "-d", dev,
            f"--set-fmt-video=width={width},height={height},pixelformat=MJPG",
            f"--set-parm={fps}", "--stream-mmap", f"--stream-count={count}",
            "--stream-skip=5" if count else "--stream-skip=0", "--stream-to=-"]


def snapshot(dev, width, height):
    try:
        p = subprocess.run(v4l2_stream_argv(dev, width, height, 15, 1),
                           capture_output=True, timeout=15)
    except subprocess.TimeoutExpired:
        return None, "camera timed out"
    if p.returncode != 0 or not p.stdout.startswith(b"\xff\xd8"):
        return None, p.stderr.decode(errors="replace") or "no JPEG frame returned"
    return p.stdout, None


def mjpeg_frames(proc):
    """Split v4l2-ctl's concatenated JPEG stream into frames."""
    buf = bytearray()
    while True:
        chunk = proc.stdout.read1(1 << 16)
        if not chunk:
            return
        buf += chunk
        while True:
            s = buf.find(b"\xff\xd8")
            if s < 0:
                del buf[:-1]
                break
            e = buf.find(b"\xff\xd9", s + 2)
            if e < 0:
                del buf[:s]
                break
            yield bytes(buf[s:e + 2])
            del buf[:e + 2]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ewego-webtest/1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # -- helpers --
    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text_stream_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

    def write_flush(self, data):
        self.wfile.write(data)
        self.wfile.flush()

    def send_bytes(self, data, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- routing --
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path
        try:
            if path == "/":
                self.send_bytes(PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/status":
                self.send_json(status())
            elif path == "/api/sessions":
                self.send_json(sessions())
            elif path.startswith("/api/run/"):
                self.run_test(path[len("/api/run/"):], q)
            elif path == "/api/journal":
                self.journal(q)
            elif path == "/api/snapshot":
                self.snapshot(q)
            elif path == "/api/stream":
                self.stream(q)
            elif path == "/api/audio/last":
                wav = RUN_DIR / "audio.wav"
                if wav.exists():
                    self.send_bytes(wav.read_bytes(), "audio/wav")
                else:
                    self.send_json({"error": "no recording yet"}, 404)
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        u = urlparse(self.path)
        m = re.fullmatch(r"/api/unit/([a-z0-9-]+)/(start|stop|restart)", u.path)
        try:
            if m and m.group(1) in UNITS:
                unit, action = m.group(1), m.group(2)
                rc, out = sh(["systemctl", action, unit], timeout=45)
                self.send_json({"rc": rc, "output": out, "status": unit_status(unit)})
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- endpoints --
    def run_test(self, name, q):
        if name not in TESTS:
            self.send_json({"error": f"unknown test {name}"}, 404)
            return
        fn, lock_name = TESTS[name]
        lock = LOCKS.get(lock_name)
        if lock and not lock.acquire(blocking=False):
            self.send_json({"error": f"{lock_name} is busy with another test"}, 409)
            return
        try:
            self.send_text_stream_headers()
            fn(self.write_flush, q)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # show the error in the pane rather than dropping the connection
            try:
                self.write_flush(f"\n[error] {type(e).__name__}: {e}\n".encode())
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            if lock:
                lock.release()

    def journal(self, q):
        unit = q.get("unit", ["ewego-sensors"])[0]
        n = min(int(q.get("n", ["100"])[0]), 2000)
        if unit not in UNITS + ["ewego-webtest"]:
            self.send_json({"error": "unknown unit"}, 404)
            return
        rc, out = sh(["journalctl", "-u", unit, "-n", str(n), "--no-pager", "-o", "short-precise"])
        self.send_bytes(out.encode(), "text/plain; charset=utf-8")

    def snapshot(self, q):
        dev = q.get("dev", ["/dev/video0"])[0]
        w, h = q.get("w", ["1280"])[0], q.get("h", ["720"])[0]
        if not LOCKS["camera"].acquire(blocking=False):
            self.send_json({"error": "camera is busy (streaming?)"}, 409)
            return
        try:
            data, err = snapshot(dev, w, h)
        finally:
            LOCKS["camera"].release()
        if data is None:
            self.send_json({"error": err}, 500)
        else:
            self.send_bytes(data, "image/jpeg")

    def stream(self, q):
        dev = q.get("dev", ["/dev/video0"])[0]
        w, h = q.get("w", ["1280"])[0], q.get("h", ["720"])[0]
        fps = q.get("fps", ["15"])[0]
        if not LOCKS["camera"].acquire(blocking=False):
            self.send_json({"error": "camera is busy"}, 409)
            return
        proc = None
        try:
            proc = subprocess.Popen(v4l2_stream_argv(dev, w, h, fps, 0),
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            for frame in mjpeg_frames(proc):
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if proc and proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    proc.wait(timeout=3)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            LOCKS["camera"].release()


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EweGo test console</title>
<style>
  :root { --bg:#f4f5f0; --card:#fbfbf8; --ink:#1e2420; --muted:#5c665f; --line:#d6dad2;
          --accent:#b94a15; --ok:#2f6b3a; --bad:#9b2c2c; --code:#eceee7; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#151816; --card:#1c201d; --ink:#e4e7df; --muted:#9aa39b; --line:#313732;
            --accent:#e8834e; --ok:#7fbf8a; --bad:#e07a7a; --code:#232823; } }
  body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.45 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; }
  header { padding:14px 20px; border-bottom:1px solid var(--line); display:flex; flex-wrap:wrap; gap:10px 24px; align-items:baseline; }
  header h1 { font-size:18px; margin:0; }
  header span { color:var(--muted); font-size:13px; }
  .ok { color:var(--ok); font-weight:600; } .bad { color:var(--bad); font-weight:600; }
  main { display:grid; grid-template-columns:repeat(auto-fit, minmax(340px, 1fr)); gap:14px; padding:14px 20px 40px; }
  .card { background:var(--card); border:1px solid var(--line); padding:12px 14px; display:flex; flex-direction:column; gap:8px; }
  .card h2 { font-size:15px; margin:0; }
  .row { display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
  button { font:inherit; font-size:13px; padding:5px 10px; border:1px solid var(--line); background:var(--code); color:var(--ink); cursor:pointer; }
  button:hover { border-color:var(--accent); }
  button.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
  select, input { font:inherit; font-size:13px; padding:4px 6px; background:var(--code); color:var(--ink); border:1px solid var(--line); }
  pre { background:var(--code); margin:0; padding:8px 10px; font:12px/1.4 ui-monospace, Menlo, Consolas, monospace; white-space:pre-wrap; overflow:auto; max-height:260px; min-height:60px; }
  pre.tall { max-height:420px; }
  table { border-collapse:collapse; font-size:13px; width:100%; }
  td, th { text-align:left; padding:4px 6px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:500; font-size:12px; }
  img { max-width:100%; background:#000; display:block; }
  .wide { grid-column:1 / -1; }
  .kv { display:grid; grid-template-columns:auto 1fr; gap:2px 12px; font-size:13px; }
  .kv b { color:var(--muted); font-weight:500; }
  footer { padding:10px 20px; color:var(--muted); font-size:12px; border-top:1px solid var(--line); }
</style></head>
<body>
<header>
  <h1 id="host">EweGo</h1>
  <span id="release"></span>
  <span id="clock"></span>
  <span id="batt"></span>
  <span id="disk"></span>
  <span id="load"></span>
  <span id="ips"></span>
</header>
<main>

<section class="card">
  <h2>Devices</h2>
  <div class="kv" id="devs"></div>
  <div class="row">
    <button onclick="run('serial','out-devs')">Serial ports</button>
    <button onclick="run('i2c','out-devs')">i2cdetect</button>
    <button onclick="run('usb','out-devs')">USB tree</button>
    <button onclick="run('alsa','out-devs')">ALSA cards</button>
    <button onclick="run('config','out-devs')">config.txt</button>
    <button onclick="run('dmesg','out-devs')">dmesg tail</button>
  </div>
  <pre id="out-devs"></pre>
</section>

<section class="card">
  <h2>Units</h2>
  <table><thead><tr><th>unit</th><th>active</th><th>enabled</th><th></th></tr></thead><tbody id="units"></tbody></table>
  <div class="row">
    <select id="jr-unit"><option>ewego-sensors</option><option>ewego-gps</option><option>ewego-dualcam</option><option>ewego-webtest</option></select>
    <button onclick="journal()">Journal tail</button>
    <button onclick="listSessions()">Sessions</button>
  </div>
  <pre id="out-units" class="tall"></pre>
</section>

<section class="card">
  <h2>Fuel gauge</h2>
  <div class="row"><button class="primary" onclick="run('fuel','out-fuel')">Read</button></div>
  <pre id="out-fuel"></pre>
</section>

<section class="card">
  <h2>IMU (BNO055, ttyAMA5)</h2>
  <div class="row">
    <button class="primary" onclick="run('imu','out-imu','&seconds='+val('imu-s'))">Log</button>
    <input id="imu-s" value="10" size="3"> s
  </div>
  <pre id="out-imu" class="tall"></pre>
</section>

<section class="card">
  <h2>GPS (ZED, ttyAMA4)</h2>
  <div class="row">
    <button class="primary" onclick="run('gps-raw','out-gps','&seconds='+val('gps-s')+'&baud='+val('gps-b'))">Raw read</button>
    <input id="gps-s" value="3" size="3"> s at
    <select id="gps-b"><option>460800</option><option>230400</option><option>115200</option><option>38400</option><option>9600</option></select>
    <button onclick="unit('ewego-gps','start')">Start logger</button>
    <button onclick="unit('ewego-gps','stop')">Stop logger</button>
    <button onclick="journal('ewego-gps','out-gps')">Logger journal</button>
  </div>
  <pre id="out-gps" class="tall"></pre>
</section>

<section class="card">
  <h2>Audio</h2>
  <div class="row">
    <button class="primary" onclick="audio()">Record</button>
    <input id="au-s" value="5" size="3"> s
    <audio id="player" controls></audio>
  </div>
  <pre id="out-audio"></pre>
</section>

<section class="card wide">
  <h2>Camera</h2>
  <div class="row">
    <select id="cam-dev"></select>
    <select id="cam-size"><option value="1280x720">1280×720</option><option value="1920x1080">1920×1080</option><option value="640x480">640×480</option></select>
    <select id="cam-fps"><option>15</option><option>30</option><option>5</option></select>
    <button class="primary" onclick="camLive(true)">Live</button>
    <button onclick="camLive(false)">Stop</button>
    <button onclick="camSnap()">Snapshot</button>
    <button onclick="run('camera-info','out-cam','&dev='+encodeURIComponent(val('cam-dev')))">Formats</button>
    <span id="cam-msg"></span>
  </div>
  <img id="cam" alt="">
  <pre id="out-cam"></pre>
</section>

<section class="card wide">
  <h2>All-sensors recorder (ewego-sensors)</h2>
  <div class="row">
    <button class="primary" onclick="unit('ewego-sensors','start')">Start</button>
    <button onclick="unit('ewego-sensors','stop')">Stop</button>
    <button onclick="journal('ewego-sensors','out-rec')">Journal</button>
    <button onclick="listSessions('out-rec')">Sessions</button>
  </div>
  <pre id="out-rec" class="tall"></pre>
</section>

</main>
<footer>EweGo web test console · no authentication, use only on networks you control · runs the same tools as over SSH</footer>
<script>
const $ = id => document.getElementById(id);
const val = id => $(id).value;
const fmtUp = s => s == null ? '' : (s < 3600 ? Math.round(s/60)+' min' : (s/3600).toFixed(1)+' h');

async function refresh() {
  try {
    const s = await (await fetch('/api/status')).json();
    $('host').textContent = s.hostname;
    $('release').textContent = (s.release.EWEGO_VERSION || 'no release marker') + ' · up ' + fmtUp(s.uptime_s);
    $('clock').innerHTML = 'clock ' + (s.clock.synchronized ? '<span class="ok">synced</span>' : '<span class="bad">NOT synced</span>') + ' ' + s.clock.utc + ' UTC';
    $('batt').innerHTML = s.battery.error ? '<span class="bad">fuel gauge: ' + s.battery.error + '</span>'
        : 'battery ' + s.battery.voltage + ' V · ' + s.battery.soc + ' %';
    $('disk').textContent = 'free ' + s.disk.free_gb + ' / ' + s.disk.total_gb + ' GB';
    $('load').textContent = 'load ' + s.load.map(x => x.toFixed(2)).join(' ') + (s.temp_c != null ? ' · ' + s.temp_c.toFixed(0) + ' °C' : '');
    $('ips').textContent = s.ips.join(' ');
    let d = '';
    for (const [p, ok] of Object.entries(s.devices)) d += '<b>' + p + '</b><span class="' + (ok ? 'ok">present' : 'bad">missing') + '</span>';
    d += '<b>audio card</b>' + (s.audio_card ? '<span class="ok">' + s.audio_card.name + ' (card ' + s.audio_card.index + ')</span>' : '<span class="bad">missing</span>');
    d += '<b>cameras</b>' + (s.video.length ? '<span class="ok">' + s.video.map(v => v.dev + ' ' + (v.name || '')).join(', ') + '</span>' : '<span class="bad">none</span>');
    $('devs').innerHTML = d;
    let u = '';
    for (const x of s.units) {
      const cls = x.active === 'active' ? 'ok' : (x.active === 'failed' ? 'bad' : '');
      u += '<tr><td>' + x.unit + '</td><td class="' + cls + '">' + x.active + '</td><td>' + x.enabled + '</td><td>' +
        (x.unit === 'ewego-webtest' ? '' : '<button onclick="unit(\'' + x.unit + '\',\'start\')">start</button> <button onclick="unit(\'' + x.unit + '\',\'stop\')">stop</button>') + '</td></tr>';
    }
    $('units').innerHTML = u;
    const sel = $('cam-dev');
    if (sel.options.length !== s.video.length) {
      sel.innerHTML = s.video.map(v => '<option value="' + v.dev + '">' + v.dev + (v.name ? ' — ' + v.name : '') + '</option>').join('');
    }
  } catch (e) { $('clock').innerHTML = '<span class="bad">status failed: ' + e + '</span>'; }
}

async function run(name, paneId, extra) {
  const pane = $(paneId); pane.textContent = '…\n';
  const r = await fetch('/api/run/' + name + '?t=' + Date.now() + (extra || ''));
  if (!r.ok) { pane.textContent = 'HTTP ' + r.status + ': ' + await r.text(); return; }
  pane.textContent = '';
  const reader = r.body.getReader(); const dec = new TextDecoder();
  while (true) { const {value, done} = await reader.read(); if (done) break;
    pane.textContent += dec.decode(value, {stream: true}); pane.scrollTop = pane.scrollHeight; }
}

async function unit(name, action) {
  const r = await (await fetch('/api/unit/' + name + '/' + action, {method: 'POST'})).json();
  $('out-units').textContent = 'systemctl ' + action + ' ' + name + ' → rc ' + r.rc + '\n' + r.output + '\n' + JSON.stringify(r.status);
  refresh();
}

async function journal(unitName, paneId) {
  const u = unitName || val('jr-unit'); const pane = $(paneId || 'out-units');
  pane.textContent = await (await fetch('/api/journal?unit=' + u + '&n=150&t=' + Date.now())).text();
  pane.scrollTop = pane.scrollHeight;
}

async function listSessions(paneId) {
  const s = await (await fetch('/api/sessions')).json();
  $(paneId || 'out-units').textContent = s.length ? s.map(x => new Date(x.mtime*1000).toISOString().slice(0,16) + '  ' + String(x.size_mb).padStart(8) + ' MB  ' + x.path).join('\n') : 'no sessions yet';
}

async function audio() {
  await run('audio', 'out-audio', '&seconds=' + val('au-s'));
  $('player').src = '/api/audio/last?t=' + Date.now();
}

function camLive(on) {
  const img = $('cam');
  if (!on) { img.src = ''; img.removeAttribute('src'); $('cam-msg').textContent = ''; return; }
  const [w, h] = val('cam-size').split('x');
  img.src = '/api/stream?dev=' + encodeURIComponent(val('cam-dev')) + '&w=' + w + '&h=' + h + '&fps=' + val('cam-fps') + '&t=' + Date.now();
  $('cam-msg').textContent = 'live';
}

function camSnap() {
  camLive(false);
  const [w, h] = val('cam-size').split('x');
  $('cam').src = '/api/snapshot?dev=' + encodeURIComponent(val('cam-dev')) + '&w=' + w + '&h=' + h + '&t=' + Date.now();
  $('cam-msg').textContent = 'snapshot';
}

refresh(); setInterval(refresh, 5000);
</script>
</body></html>
"""


def main():
    ap = argparse.ArgumentParser(description="EweGo web test console")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--ewego-dir", default=None, help="override /opt/ewego")
    args = ap.parse_args()
    global EWEGO
    if args.ewego_dir:
        EWEGO = Path(args.ewego_dir)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    srv.daemon_threads = True
    print(f"ewego-webtest listening on http://{args.bind}:{args.port}  (EWEGO_DIR={EWEGO})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
