#!/usr/bin/env python3
"""
EweGo web test console.

A small HTTP server (Python standard library only) that lets you exercise
each piece of a wearable from a browser: status strip, one test per sensor
with live output, a live camera view, unit start/stop, journal tails and
a session listing. /live is the field dashboard: both cameras, IMU traces,
GPS, battery and the recorder controls, fed by the files the recorders keep
on tmpfs (/run/ewego) or by preview processes when nothing is recording. Everything runs the same tools you would use over SSH;
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
import struct
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
UNITS = ["ewego-record.target", "ewego-cam@camera1", "ewego-cam@camera2", "ewego-imu-usb",
         "ewego-gps", "ewego-audio", "ewego-fuel"]
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


_PPS_LAST = {}   # device -> (monotonic, seq) from the previous status call, for rate


def read_pps_assert(dev="pps0"):
    """Parse /sys/class/pps/<dev>/assert: '<realtime seconds>.<ns>#<sequence>'."""
    try:
        raw = Path(f"/sys/class/pps/{dev}/assert").read_text().strip()
    except OSError:
        return None
    if "#" not in raw:
        return None
    ts, seq = raw.split("#", 1)
    try:
        return {"ts": float(ts), "seq": int(seq)}
    except ValueError:
        return None


def pps_status():
    """State of the GPS time pulse on /dev/pps0 (TP1 on GPIO 6)."""
    devs = sorted(p.name for p in Path("/sys/class/pps").glob("pps*")) if Path("/sys/class/pps").exists() else []
    if not devs:
        return {"present": False, "error": "no /dev/pps* (is dtoverlay=pps-gpio,gpiopin=6 in config.txt?)"}
    dev = devs[0]
    a = read_pps_assert(dev)
    if a is None:
        return {"present": True, "device": dev, "pulses": False, "seq": 0, "age_s": None, "rate_hz": None,
                "note": "no pulse yet: TP1 only runs once the receiver has a fix"}
    now_m = time.monotonic()
    rate = None
    prev = _PPS_LAST.get(dev)
    if prev and now_m - prev[0] >= 1.5:
        rate = (a["seq"] - prev[1]) / (now_m - prev[0])
    if not prev or now_m - prev[0] >= 1.5:
        _PPS_LAST[dev] = (now_m, a["seq"])
    age = time.time() - a["ts"]
    return {
        "present": True, "device": dev, "pulses": age < 2.5 and a["seq"] > 0,
        "seq": a["seq"], "age_s": round(age, 3), "rate_hz": None if rate is None else round(rate, 3),
        # fractional second of the edge in the system clock: ~0 when the clock is on GPS time
        "clock_offset_ms": round(((a["ts"] + 0.5) % 1.0 - 0.5) * 1000, 1),
    }


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
    """USB cameras only: one capture node per camera.

    The kernel names every UVC capture node /dev/v4l/by-path/<...>-usb-<port>-video-index0
    (index1 is the camera's metadata node), and the Pi's own codec/ISP/HEVC
    nodes have no USB by-path entry, so this skips them. The by-path name
    doubles as a stable label for the physical hub port."""
    devs, seen = [], set()
    # udev makes two links per camera (…-usb-… and …-usbv2-…) to the same node
    links = sorted(Path("/dev/v4l/by-path").glob("*usb*-video-index0"), key=lambda x: ("usbv2" in x.name, x.name)) \
        if Path("/dev/v4l/by-path").exists() else []
    for p in links:
        dev = os.path.realpath(p)
        if dev in seen:
            continue
        seen.add(dev)
        node = Path("/sys/class/video4linux") / os.path.basename(dev)
        try:
            name = (node / "name").read_text().strip()
        except OSError:
            name = ""
        m = re.search(r"usb-([\d.:]+)-video", p.name)
        devs.append({"dev": dev, "name": name, "port": m.group(1) if m else p.name, "by_path": str(p)})
    if devs:
        return devs
    # fallback: any node whose driver is uvcvideo (older udev without by-path)
    for node in sorted(Path("/sys/class/video4linux").glob("video*")):
        try:
            drv = os.path.basename(os.readlink(node / "device" / "driver"))
            idx = (node / "index").read_text().strip()
        except OSError:
            continue
        if drv == "uvcvideo" and idx == "0":
            name = (node / "name").read_text().strip() if (node / "name").exists() else ""
            devs.append({"dev": f"/dev/{node.name}", "name": name, "port": "?", "by_path": ""})
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
        "pps": pps_status(),
        "bridge": BRIDGE.status(),
        "devices": devs,
        "audio_card": {"index": card[0], "name": card[1]} if card else None,
        "video": video_devices(),
        "units": [unit_status(u) for u in UNITS + ["ewego-webtest"]],
    }


def open_serial_raw(port, baud, writable=False):
    fd = os.open(port, (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    speed = getattr(termios, f"B{baud}")
    cflag = attrs[2]
    cflag = (cflag & ~termios.CSIZE) | termios.CS8 | termios.CREAD | termios.CLOCAL
    cflag &= ~(termios.PARENB | termios.CSTOPB | termios.CRTSCTS)
    attrs = [0, 0, cflag, 0, speed, speed, attrs[6]]
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIFLUSH)
    return fd


class SerialBridge:
    """Expose a serial port on a TCP port, both directions, one client at a
    time, so PyGPSClient (or u-center) on a laptop can talk to the GNSS
    module over the network. Standard library only."""

    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.stop_evt = threading.Event()
        self.state = {"running": False, "port": None, "dev": None, "baud": None,
                      "client": None, "rx": 0, "tx": 0, "error": None}

    def status(self):
        with self.lock:
            return dict(self.state)

    def start(self, dev, baud, tcp_port):
        with self.lock:
            if self.state["running"]:
                return False, "bridge already running"
            if not LOCKS["gps"].acquire(blocking=False):
                return False, "serial port busy (raw read running?)"
            try:
                fd = open_serial_raw(dev, baud, writable=True)
            except (OSError, AttributeError) as e:
                LOCKS["gps"].release()
                return False, f"open {dev}: {e}"
            try:
                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind(("0.0.0.0", tcp_port))
                srv.listen(1)
                srv.settimeout(0.5)
            except OSError as e:
                os.close(fd)
                LOCKS["gps"].release()
                return False, f"listen on {tcp_port}: {e}"
            self.stop_evt.clear()
            self.state.update({"running": True, "port": tcp_port, "dev": dev, "baud": baud,
                               "client": None, "rx": 0, "tx": 0, "error": None})
            self.thread = threading.Thread(target=self._run, args=(fd, srv), daemon=True)
            self.thread.start()
            return True, f"listening on TCP {tcp_port} <-> {dev} @ {baud}"

    def stop(self):
        with self.lock:
            if not self.state["running"]:
                return False, "bridge not running"
            self.stop_evt.set()
        self.thread.join(timeout=5)
        return True, "bridge stopped"

    def _run(self, fd, srv):
        try:
            while not self.stop_evt.is_set():
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    # keep draining the serial port so stale data does not pile up
                    try:
                        os.read(fd, 65536)
                    except (BlockingIOError, OSError):
                        pass
                    continue
                # blocking with a bound: select() tells us when recv will not
                # block, and sendall() may wait briefly on a slow Wi-Fi link
                conn.settimeout(5)
                with self.lock:
                    self.state["client"] = f"{addr[0]}:{addr[1]}"
                try:
                    termios.tcflush(fd, termios.TCIFLUSH)
                    while not self.stop_evt.is_set():
                        r, _, _ = select.select([fd, conn], [], [], 0.5)
                        if fd in r:
                            try:
                                data = os.read(fd, 65536)
                            except BlockingIOError:
                                data = b""
                            if data:
                                conn.sendall(data)
                                with self.lock:
                                    self.state["tx"] += len(data)
                        if conn in r:
                            try:
                                data = conn.recv(65536)
                            except BlockingIOError:
                                data = b""
                            if not data:
                                break        # client closed
                            os.write(fd, data)
                            with self.lock:
                                self.state["rx"] += len(data)
                except OSError as e:
                    with self.lock:
                        self.state["error"] = str(e)
                finally:
                    conn.close()
                    with self.lock:
                        self.state["client"] = None
        finally:
            srv.close()
            os.close(fd)
            with self.lock:
                self.state["running"] = False
                self.state["client"] = None
            LOCKS["gps"].release()


BRIDGE = SerialBridge()


def sessions():
    out = []
    if not EWEGO.exists():
        return out
    # recordings/<unit>/<YYYYmmdd_HHMMSS>/ from the C recorders and the audio/fuel
    # units; sensor_test_* and gps-logs/ from the older Python scripts
    candidates = list(EWEGO.glob("sensor_test_*")) + list((EWEGO / "recordings").glob("*/*")) \
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


def test_pps(write, q):
    """Watch the time pulse for N seconds: one line per edge with the
    interval since the previous edge and the system clock's offset from the
    top of the second."""
    secs = int(q.get("seconds", ["10"])[0])
    st = pps_status()
    if not st.get("present"):
        write((st["error"] + "\n").encode())
        return
    dev = st["device"]
    write(f"Watching /dev/{dev} (GPS TP1 on GPIO 6) for {secs}s. "
          f"Offset = system clock minus nearest whole second at the edge; "
          f"near 0 ms means the system clock is on GPS time.\n".encode())
    write(b"   seq        edge time (UTC)        interval    offset\n")
    last = read_pps_assert(dev)
    last_seq = last["seq"] if last else -1
    last_ts = last["ts"] if last else None
    seen = 0
    end = time.monotonic() + secs
    while time.monotonic() < end:
        time.sleep(0.02)
        a = read_pps_assert(dev)
        if not a or a["seq"] == last_seq:
            continue
        interval = "" if last_ts is None else f"{(a['ts'] - last_ts) * 1000:9.3f} ms"
        offset = ((a["ts"] + 0.5) % 1.0 - 0.5) * 1000
        t = time.strftime("%H:%M:%S", time.gmtime(a["ts"])) + f".{int((a['ts'] % 1) * 1e6):06d}"
        write(f"{a['seq']:6d}   {t}   {interval:>13s}   {offset:+8.1f} ms\n".encode())
        if a["seq"] - last_seq > 1 and last_seq >= 0:
            write(f"        (missed {a['seq'] - last_seq - 1} edge(s) between reads)\n".encode())
        last_seq, last_ts = a["seq"], a["ts"]
        seen += 1
    if seen == 0:
        write(b"no edges seen. TP1 only pulses once the receiver has a fix; check the GPS fix in the logger journal.\n")
    else:
        write(f"{seen} edges in {secs}s\n".encode())


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


_SEQ_RE = re.compile(r"seq:\s*(\d+)")
_TS_RE = re.compile(r"ts:\s*(\d+\.\d+)")
_BYTES_RE = re.compile(r"bytesused:\s*(\d+)")


def test_dualcam(write, q):
    """Capture from several cameras at once to /dev/null and account for
    dropped frames using the kernel's per-buffer sequence numbers, as
    printed by v4l2-ctl --verbose. No streaming, no files."""
    secs = int(q.get("seconds", ["20"])[0])
    size = q.get("size", ["1920x1080"])[0]
    fps = int(q.get("fps", ["30"])[0])
    devs = [d for d in q.get("devs", [""])[0].split(",") if d] or [v["dev"] for v in video_devices()]
    if not devs:
        write(b"no cameras found\n")
        return
    w, h = size.split("x")
    count = secs * fps
    stagger = float(q.get("stagger", ["1.0"])[0])
    write(f"Capturing {count} frames ({secs}s at {fps} fps, {size} MJPG) from {len(devs)} camera(s) "
          f"simultaneously to /dev/null, starts staggered by {stagger:g}s.\n"
          f"Drop detection uses the kernel sequence number of each buffer; "
          f"a gap means the frame was lost between camera and driver.\n".encode())
    rc, quirks = sh(["cat", "/sys/module/uvcvideo/parameters/quirks"])
    rc_mp, mp = sh(["cat", "/sys/module/uvcvideo/parameters/max_payload"])
    write(f"uvcvideo quirks={quirks.strip() or '?'}, max_payload={mp.strip() if rc_mp == 0 else 'n/a (stock module)'}\n\n".encode())
    rc, dmesg_before = sh(["bash", "-c", "dmesg | wc -l"])
    # Turn on the driver's VIDEO trace for the duration so the kernel logs
    # the reservation each camera actually got ("Selecting alternate
    # setting N (M B/frame bandwidth)"); sysfs does not expose the
    # high-bandwidth multiplier, so that log line is the reliable number.
    tracef = Path("/sys/module/uvcvideo/parameters/trace")
    try:
        old_trace = tracef.read_text().strip()
        tracef.write_text("1024")
    except OSError:
        old_trace = None
    usb_if = {d: usb_interface_of(d)[0] for d in devs}

    def reserved(d):
        rc, out = sh(["bash", "-c", f"dmesg | tail -n +{int(dmesg_before.strip() or 0) + 1}"])
        m = None
        for m in re.finditer(rf"uvcvideo {re.escape(usb_if[d] or '~')}: Selecting alternate setting (\d+) \((\d+) B/frame", out):
            pass
        return (int(m.group(1)), int(m.group(2))) if m else None

    stats = {d: {"frames": 0, "first": None, "last": None, "gaps": 0, "lost": 0, "gap_at": [],
                 "errors": 0, "bytes": 0, "t0": None, "t1": None, "dt_min": None, "dt_max": 0.0,
                 "last_ts": None, "tail": [], "started": None} for d in devs}
    procs = {}
    for i, d in enumerate(devs):
        if i and stagger > 0:
            time.sleep(stagger)
        # --stream-poll: use select() with v4l2-ctl's timeout instead of a
        # blocking DQBUF, so a camera that starts but never delivers a frame
        # reports "select timeout" instead of hanging until we kill it.
        argv = ["v4l2-ctl", "-d", d, f"--set-fmt-video=width={w},height={h},pixelformat=MJPG",
                f"--set-parm={fps}", "--stream-mmap", "--stream-poll", f"--stream-count={count}",
                "--stream-to=/dev/null", "--verbose"]
        procs[d] = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        stats[d]["started"] = time.monotonic()
        write(f"  started {d}\n".encode())

    def reader(d):
        s = stats[d]
        for raw in procs[d].stdout:
            line = raw.decode(errors="replace")
            m = _SEQ_RE.search(line)
            if not m:
                s["tail"].append(line.strip())
                s["tail"] = s["tail"][-6:]
                continue
            seq = int(m.group(1))
            s["frames"] += 1
            if s["first"] is None:
                s["first"] = seq
            elif seq != s["last"] + 1:
                s["gaps"] += 1
                s["lost"] += seq - s["last"] - 1
                if len(s["gap_at"]) < 8:
                    s["gap_at"].append(f"{s['last']}->{seq}")
            s["last"] = seq
            if "error" in line.lower():
                s["errors"] += 1
            mb = _BYTES_RE.search(line)
            if mb:
                s["bytes"] += int(mb.group(1))
            mt = _TS_RE.search(line)
            if mt:
                ts = float(mt.group(1))
                if s["t0"] is None:
                    s["t0"] = ts
                elif s["last_ts"] is not None:
                    dt = ts - s["last_ts"]
                    s["dt_min"] = dt if s["dt_min"] is None else min(s["dt_min"], dt)
                    s["dt_max"] = max(s["dt_max"], dt)
                s["t1"] = ts
                s["last_ts"] = ts

    threads = [threading.Thread(target=reader, args=(d,), daemon=True) for d in devs]
    for t in threads:
        t.start()
    killed = set()
    try:
        deadline = time.monotonic() + secs + 15 + stagger * len(devs)
        while any(p.poll() is None for p in procs.values()) and time.monotonic() < deadline:
            time.sleep(2)
            parts = []
            for d in devs:
                s = stats[d]
                note = ""
                if s["frames"] == 0 and procs[d].poll() is None and time.monotonic() - s["started"] > 4:
                    note = " (streaming but NO frames yet)"
                a = reserved(d)
                if a:
                    note += f" [alt {a[0]}: {a[1]} B/uframe reserved]"
                parts.append(f"{d}: {s['frames']} frames, {s['lost']} lost{note}")
            write(("  " + "   ".join(parts) + "\n").encode())
    finally:
        for d, p in procs.items():
            if p.poll() is None:
                killed.add(d)
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for t in threads:
            t.join(timeout=3)

    write(b"\nResult:\n")
    ok = True
    for d in devs:
        s = stats[d]
        rc = procs[d].returncode
        dur = (s["t1"] - s["t0"]) if s["t0"] is not None and s["t1"] is not None else 0
        eff = (s["frames"] - 1) / dur if dur > 0 and s["frames"] > 1 else 0
        how = "killed by the test after the deadline" if d in killed else f"exit {rc}"
        write(f"  {d}: {how}, {s['frames']}/{count} frames, {s['lost']} lost in {s['gaps']} gap(s)"
              f"{' at ' + ', '.join(s['gap_at']) if s['gap_at'] else ''}, {s['errors']} error-flagged\n".encode())
        if s["frames"] > 1:
            write(f"      {eff:.2f} fps effective, interval min {s['dt_min'] * 1000:.1f} / max {s['dt_max'] * 1000:.1f} ms, "
                  f"{s['bytes'] / max(dur, 1e-9) / 1e6:.1f} MB/s, {s['bytes'] / s['frames'] / 1e3:.0f} KB/frame\n".encode())
        if s["frames"] < count or s["lost"] or s["errors"] or rc != 0:
            ok = False
            write(("      v4l2-ctl said: " + (" | ".join(s["tail"]) if s["tail"] else "(nothing)") + "\n").encode())
            if s["frames"] == 0:
                write(b"      no frames at all. If v4l2-ctl reported 'No space left on device' the USB bandwidth\n"
                      b"      reservation failed: try 'Reload uvcvideo with quirks=128' below and rerun. If it just\n"
                      b"      timed out with no message, the stream started but the bus or hub never delivered\n"
                      b"      data: try 1280x720 or 15 fps, check hub power, and see lsusb -t below.\n")
    write((b"\nPASS: no frames lost on any camera\n" if ok else b"\nFAIL: see above\n"))
    total = sum((reserved(d) or (0, 0))[1] for d in devs)
    write(f"reserved bus bandwidth, all cameras: {total} B/microframe (budget ~6000 minus overhead; ~5000 is safe)\n".encode())
    if old_trace is not None:
        try:
            tracef.write_text(old_trace)
        except OSError:
            pass
    rc, out = sh(["bash", "-c", f"dmesg -T | tail -n +{int(dmesg_before.strip() or 0) + 1} | tail -n 20"])
    write(b"\nkernel messages during the test:\n" + (out.encode() if out.strip() else b"  (none)\n"))
    rc, out = sh(["lsusb", "-t"])
    write(b"\nUSB tree:\n" + out.encode())


def uvc_altsetting(dev):
    """(alt setting, bytes per microframe) of a UVC camera's streaming
    interface while it streams, from sysfs. None if not streaming/unknown."""
    try:
        ctrl_if = Path(os.path.realpath(Path("/sys/class/video4linux") / os.path.basename(dev) / "device"))
        usb_dev = ctrl_if.parent
        base = ctrl_if.name.rsplit(":", 1)[0]          # e.g. 1-1.4
        for intf in sorted(usb_dev.glob(base + ":*")):
            try:
                if (intf / "bInterfaceClass").read_text().strip() != "0e":   # video class
                    continue
                if (intf / "bInterfaceSubClass").read_text().strip() != "02":  # streaming
                    continue
                alt = int((intf / "bAlternateSetting").read_text().strip())
                eps = list(intf.glob("ep_*"))
                if not eps:
                    return alt, 0
                mps = int((eps[0] / "wMaxPacketSize").read_text().strip(), 16)
                return alt, (mps & 0x7FF) * (((mps >> 11) & 3) + 1)
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return None


def test_camrec(write, q):
    """Record N seconds from the selected cameras with the real recorder
    (ewego-cam), one process per camera, and show each summary."""
    secs = int(q.get("seconds", ["20"])[0])
    size = q.get("size", ["1920x1080"])[0]
    fps = q.get("fps", ["30"])[0]
    stagger = float(q.get("stagger", ["1.0"])[0])
    devs = [d for d in q.get("devs", [""])[0].split(",") if d] or [v["dev"] for v in video_devices()]
    if not shutil.which("ewego-cam"):
        write(b"ewego-cam is not installed on this image (needs v0.4.0 or later)\n")
        return
    if not devs:
        write(b"no cameras selected\n")
        return
    out_root = EWEGO / "recordings" / "webtest"
    out_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    write(f"ewego-cam: {len(devs)} camera(s), {secs}s at {size} {fps} fps -> {out_root}/{stamp}/\n\n".encode())
    procs = {}
    for i, d in enumerate(devs):
        if i and stagger > 0:
            time.sleep(stagger)
        name = f"cam{i + 1}"
        argv = ["ewego-cam", "--device", d, "--size", size, "--fps", fps, "--seconds", str(secs),
                "--out", str(out_root / stamp), "--no-session-dir", "--name", name, "--stats", "2"]
        procs[name] = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        write(f"$ {' '.join(argv)}\n".encode())

    lock = threading.Lock()

    def pump(name):
        for raw in procs[name].stdout:
            with lock:
                try:
                    write(f"[{name}] ".encode() + raw.replace(b"\r", b"\n"))
                except (BrokenPipeError, ConnectionResetError):
                    return

    threads = [threading.Thread(target=pump, args=(n,), daemon=True) for n in procs]
    for t in threads:
        t.start()
    deadline = time.monotonic() + secs + 20 + stagger * len(devs)
    try:
        while any(p.poll() is None for p in procs.values()) and time.monotonic() < deadline:
            time.sleep(0.5)
    finally:
        for p in procs.values():
            if p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGINT)
                except ProcessLookupError:
                    pass
        for t in threads:
            t.join(timeout=5)
    write(b"\nResult:\n")
    ok = True
    for name, p in procs.items():
        rc = p.wait()
        summ = out_root / stamp / f"{name}_summary.json"
        write(f"  {name}: exit {rc} ({'no loss' if rc == 0 else 'frames lost/error-flagged' if rc == 2 else 'stalled' if rc == 3 else 'error'})\n".encode())
        if summ.exists():
            try:
                s = json.loads(summ.read_text())
                write(f"      frames {s['frames']}, lost {s['lost']} in {s['gaps']} gap(s), errors {s['error_frames']}, "
                      f"{s['effective_fps']:.2f} fps, interval {s['interval_us']['min'] / 1000:.1f}/"
                      f"{s['interval_us']['mean'] / 1000:.1f}/{s['interval_us']['max'] / 1000:.1f} ms, "
                      f"{s['bytes'] / 1e6:.1f} MB, ts {s['timestamp_clock']}/{s['timestamp_source']}\n".encode())
            except (ValueError, KeyError) as e:
                write(f"      summary unreadable: {e}\n".encode())
        ok = ok and rc == 0
    write((b"\nPASS\n" if ok else b"\nFAIL\n"))
    write(f"files kept under {out_root}/{stamp}/ (delete when done)\n".encode())


def usb_interface_of(dev):
    """'1-1.4:1.0' (USB interface id used in uvcvideo kernel messages) for a
    /dev/videoN, and the bus/dev numbers for lsusb."""
    try:
        ctrl_if = Path(os.path.realpath(Path("/sys/class/video4linux") / os.path.basename(dev) / "device"))
        usb_dev = ctrl_if.parent
        return ctrl_if.name, (usb_dev / "busnum").read_text().strip(), (usb_dev / "devnum").read_text().strip()
    except OSError:
        return None, None, None


def uvc_altsettings(dev):
    """Table of the camera's video-streaming alternate settings from lsusb:
    [(alt, bytes per microframe)]. This is what the max_payload cap selects
    from: the driver picks the smallest entry >= the (capped) request."""
    ifid, bus, devnum = usb_interface_of(dev)
    if not bus:
        return []
    rc, out = sh(["lsusb", "-v", "-s", f"{bus}:{devnum}"], timeout=20)
    table, alt, in_vs, video_class = [], None, False, False
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("bInterfaceClass"):
            video_class = re.match(r"bInterfaceClass\s+14\b", s) is not None  # 14 = Video
        elif s.startswith("bInterfaceSubClass"):
            # subclass 2 is VideoStreaming only within class 14; audio
            # streaming interfaces (class 1) also use subclass 2
            in_vs = video_class and re.match(r"bInterfaceSubClass\s+2\b", s) is not None
        elif s.startswith("bAlternateSetting"):
            alt = int(s.split()[1])
        elif s.startswith("wMaxPacketSize") and in_vs and alt is not None:
            m = re.search(r"0x([0-9a-fA-F]+)\s+(\d+)x\s+(\d+)\s+bytes", s)
            if m:
                table.append((alt, int(m.group(2)) * int(m.group(3))))
    return sorted(set(table))


def test_uvc_probe(write, q):
    """Stream a few frames from one camera with uvcvideo tracing on and
    show what the camera asked for and which alternate setting the
    driver chose. This is the number that has to fit on the bus."""
    dev = q.get("dev", [None])[0] or (video_devices() or [{"dev": "/dev/video0"}])[0]["dev"]
    size = q.get("size", ["1920x1080"])[0]
    fps = q.get("fps", ["30"])[0]
    w, h = size.split("x")
    tracef = Path("/sys/module/uvcvideo/parameters/trace")
    try:
        old = tracef.read_text().strip()
        tracef.write_text("1025")          # PROBE (1) | VIDEO (1024)
    except OSError as e:
        write(f"cannot set uvcvideo trace: {e}\n".encode())
        return
    rc, before = sh(["bash", "-c", "dmesg | wc -l"])
    rc_mp, mp = sh(["cat", "/sys/module/uvcvideo/parameters/max_payload"])
    rc, mi = sh(["bash", "-c", "modinfo -F filename uvcvideo"])
    rc, kv = sh(["uname", "-r"])
    write(f"running kernel: {kv.strip()}\n".encode())
    write(f"uvcvideo in use: {mi.strip()}\n".encode())
    write(f"max_payload: {mp.strip() if rc_mp == 0 and mp.strip() else 'NOT AVAILABLE (stock module loaded)'}\n".encode())
    rc, inv = sh(["bash", "-c", "for d in /lib/modules/*; do echo \"  $d: $(ls $d/updates 2>/dev/null | tr '\\n' ' ')\"; done; "
                  "echo '  modprobe.d:'; cat /etc/modprobe.d/ewego-uvc.conf 2>/dev/null | grep -v '^#' || echo '  (no ewego-uvc.conf)'; "
                  "grep EWEGO_VERSION /etc/ewego-image-release 2>/dev/null"])
    write(b"module inventory (kernel dir: files in updates/):\n" + inv.encode())
    if rc_mp != 0:
        write(b"-> the patched module is not loaded. If the running kernel does not match the directory that\n"
              b"   holds updates/uvcvideo.ko, the image was built for a different kernel than it boots.\n")
    write(f"Probing {dev} at {size} {fps} fps MJPG with uvcvideo trace on ...\n".encode())
    argv = ["v4l2-ctl", "-d", dev, f"--set-fmt-video=width={w},height={h},pixelformat=MJPG",
            f"--set-parm={fps}", "--stream-mmap", "--stream-poll", "--stream-count=10", "--stream-to=/dev/null"]
    p = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    alt = uvc_altsetting(dev)
    try:
        tracef.write_text(old)
    except OSError:
        pass
    write(("v4l2-ctl: " + (p.stderr.strip() or "ok") + "\n").encode())
    rc, out = sh(["bash", "-c", f"dmesg -T | tail -n +{int(before.strip() or 0) + 1} | grep -iE 'uvcvideo|usb [0-9]'"])
    write(b"kernel:\n" + (out.encode() if out.strip() else b"  (nothing logged)\n"))
    table = uvc_altsettings(dev)
    if table:
        write(b"\nthis camera's video alternate settings (bytes per microframe):\n  " +
              ", ".join(f"alt {a}: {b}" for a, b in table).encode() + b"\n")
        write(b"the driver picks the smallest entry >= the request (after the max_payload cap), so to\n"
              b"land on a given entry set max_payload to at most that value. Two cameras need their two\n"
              b"reservations to sum to less than ~6000 minus per-packet overhead; ~5000 is safe.\n")
    write(b"\nHow to read it: 'Selecting alternate setting N (M B/frame bandwidth)' is the payload per\n"
          b"125 us microframe the driver reserved for this camera. 3060-3072 is the maximum a camera\n"
          b"can ask for; two of those never fit. On this kernel the quirks=128 bandwidth fix applies\n"
          b"to uncompressed formats only, so it does not change what an MJPEG camera requests.\n")


def test_uvc_cap(write, q):
    """Set uvcvideo max_payload at runtime (no reload; applies to the next
    stream start). Persist by editing /etc/modprobe.d/ewego-uvc.conf."""
    p = Path("/sys/module/uvcvideo/parameters/max_payload")
    if not p.exists():
        write(b"the patched uvcvideo is not loaded (no max_payload parameter)\n")
        return
    val = q.get("value", [""])[0].strip()
    write(f"max_payload was {p.read_text().strip()}\n".encode())
    if val:
        try:
            int(val)
            p.write_text(val + "\n")
        except (ValueError, OSError) as e:
            write(f"could not set: {e}\n".encode())
            return
        write(f"max_payload is now {p.read_text().strip()} (until reboot; the image default is in "
              f"/etc/modprobe.d/ewego-uvc.conf)\n".encode())
    else:
        write(b"no value given\n")


def test_uvc_quirk(write, q):
    """Reload the uvcvideo module with or without the bandwidth quirk.
    Fails harmlessly if a camera is open."""
    on = q.get("on", ["1"])[0] == "1"
    rc, cur = sh(["cat", "/sys/module/uvcvideo/parameters/quirks"])
    write(f"current uvcvideo quirks={cur.strip()}\n".encode())
    argv = ["bash", "-c", "modprobe -r uvcvideo && modprobe uvcvideo " + ("quirks=128" if on else "") +
            " && sleep 1 && cat /sys/module/uvcvideo/parameters/quirks && ls /dev/video*"]
    rc = stream_process(argv, write)
    if rc != 0:
        write(b"reload failed: is a camera in use (live view, recorder)? Stop it and retry.\n")
    else:
        write(b"note: this lasts until reboot. To make it permanent, put 'options uvcvideo quirks=128'\n"
              b"in /etc/modprobe.d/uvcvideo.conf (the plan has this going into the image).\n")


def hubimu_hidraw():
    """/dev/hidrawN of the hub_imu module (VID:PID 1209:0001), or None."""
    for p in sorted(Path("/sys/class/hidraw").glob("hidraw*")):
        try:
            if "HID_ID=0003:00001209:00000001" in (p / "device" / "uevent").read_text():
                return f"/dev/{p.name}"
        except OSError:
            pass
    return None


def test_imu_usb(write, q):
    """Read the hub_imu HID stream for N seconds: rate, sequence gaps,
    flags, device-clock intervals, and one decoded sample."""
    secs = int(q.get("seconds", ["5"])[0])
    dev = hubimu_hidraw()
    acm = sorted(Path("/dev").glob("ttyACM*"))
    write(f"hub_imu console: {', '.join(str(a) for a in acm) or 'no ttyACM device'}\n".encode())
    if not dev:
        write(b"no hub_imu HID device (VID:PID 1209:0001). Firmware >= v0.3.0 streams HID; check 'USB tree'.\n")
        return
    write(f"reading {dev} for {secs}s ...\n".encode())
    import struct
    fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
    n = lost = flagged = 0
    last_seq = None
    last_dev = None
    dts = []
    sample = None
    end = time.monotonic() + secs
    try:
        while time.monotonic() < end:
            r, _, _ = select.select([fd], [], [], 0.5)
            if not r:
                continue
            try:
                b = os.read(fd, 64)
            except BlockingIOError:
                continue
            if len(b) != 64 or b[0] != 1:
                continue
            flags, seq, dev_ts = b[1], struct.unpack_from("<H", b, 2)[0], struct.unpack_from("<I", b, 4)[0]
            if last_seq is not None:
                miss = (seq - last_seq - 1) & 0xFFFF
                lost += miss
                if miss == 0 and not (flags & 4):
                    dts.append((dev_ts - last_dev) & 0xFFFFFFFF)
            last_seq, last_dev = seq, dev_ts
            n += 1
            if flags:
                flagged += 1
            if not (flags & 3):
                sample = b
    finally:
        os.close(fd)
    write(f"  reports: {n} in {secs}s ({n / secs:.1f}/s), lost {lost}, flagged {flagged}\n".encode())
    if dts:
        write(f"  device-clock interval: min {min(dts)} / mean {sum(dts) / len(dts):.0f} / max {max(dts)} us  "
              f"(expect 10000)\n".encode())
    if sample:
        bu = sample[8:54]
        s16 = lambda o: struct.unpack_from("<h", bu, o)[0]
        i2c_us = struct.unpack_from("<H", sample, 54)[0]
        write(f"  last sample: euler h/r/p {s16(18) / 16:.1f} {s16(20) / 16:.1f} {s16(22) / 16:.1f} deg, "
              f"gravity {s16(38) / 100:.2f} {s16(40) / 100:.2f} {s16(42) / 100:.2f} m/s^2, "
              f"temp {struct.unpack_from('<b', bu, 44)[0]} C, calib sys/gyr/acc/mag "
              f"{(bu[45] >> 6) & 3}/{(bu[45] >> 4) & 3}/{(bu[45] >> 2) & 3}/{bu[45] & 3}, i2c {i2c_us} us\n".encode())
    elif n:
        write(b"  every report was flagged: sensor not running on the module (see its console)\n")
    if n == 0:
        write(b"  nothing received: is the module's HID stream on? ('stream' toggles it on its console)\n")


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


def test_firstboot(write, q):
    """Who ran apt on this card, and what first-boot automation is configured."""
    stream_process(["bash", "-c",
                    "echo '== apt history (kernel/upgrade entries)'; "
                    "grep -B2 -A4 -E 'linux-image|Upgrade:' /var/log/apt/history.log 2>/dev/null | head -60 || echo none; "
                    "echo; echo '== Imager / cloud-init user-data (package directives)'; "
                    "grep -n -iE 'package|apt|upgrade' /boot/firmware/user-data 2>/dev/null || echo '(no user-data or no package lines)'; "
                    "echo; echo '== cloud-init config package/upgrade settings'; "
                    "grep -rn -iE 'package_(update|upgrade)|package-update' /etc/cloud/ 2>/dev/null || echo '(none)'; "
                    "echo; echo '== cloud-init log: apt activity'; "
                    "grep -iE 'apt|package' /var/log/cloud-init.log 2>/dev/null | grep -viE 'debug' | tail -n 20 || echo '(no cloud-init log)'; "
                    "echo; echo '== unattended-upgrades / apt timers'; "
                    "systemctl list-timers --all --no-pager 2>/dev/null | grep -iE 'apt|unattended' || echo '(no apt timers)'; "
                    "dpkg -l unattended-upgrades 2>/dev/null | tail -n 1; "
                    "echo; echo '== kernel packages and holds'; "
                    "dpkg -l 'linux-image-*' 2>/dev/null | grep '^[hi]i' ; apt-mark showhold 2>/dev/null"],
                   write)


def test_config(write, q):
    stream_process(["bash", "-c", "grep -v '^#' /boot/firmware/config.txt | grep -v '^$'; echo; "
                    "echo cmdline.txt:; cat /boot/firmware/cmdline.txt"], write)



# ---------------------------------------------------------------------------
# live view: what the recorders publish on tmpfs, plus preview processes
# ---------------------------------------------------------------------------

LIVE_DIR = Path(os.environ.get("EWEGO_LIVE_DIR", "/run/ewego"))
RECORDERS = ["ewego-cam@camera1", "ewego-cam@camera2", "ewego-imu-usb"]
CAMERAS = ["camera1", "camera2"]
FIX_NAMES = {0: "no fix", 1: "dead reckoning", 2: "2D", 3: "3D", 4: "GNSS+DR", 5: "time only"}


def read_conf(path):
    """KEY=VALUE lines of an /etc/ewego/*.conf file (comments ignored)."""
    out = {}
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def tool(name):
    """Path of a C tool: the image installs them in /usr/local/bin; a checkout
    may have a native build next to the source."""
    p = shutil.which(name)
    if p:
        return p
    for sub in ("uvccam", "usbimu"):
        c = EWEGO / "Firmware" / sub / name
        if c.exists():
            return str(c)
    return name


class Preview:
    """ewego-cam --preview and ewego-imu-usb --preview feeding LIVE_DIR while
    nothing is recording: the live page's cameras and IMU without writing a
    byte to the card. Stopped before any recorder starts (they need the
    cameras), and on its own after IDLE_S without a live-page client."""
    IDLE_S = 90

    def __init__(self):
        self.lock = threading.Lock()
        self.procs = {}
        self.last_seen = time.monotonic()
        self.message = ""
        self.started = None
        self.holding = False     # we hold LOCKS["camera"] while processes run
        threading.Thread(target=self._reaper, daemon=True).start()

    def touch(self):
        self.last_seen = time.monotonic()

    def status(self):
        with self.lock:
            procs = {n: ("running" if p.poll() is None else f"exited {p.returncode}") for n, p in self.procs.items()}
        return {"running": any(v == "running" for v in procs.values()), "procs": procs,
                "message": self.message, "since": self.started}

    def start(self):
        if self.status()["running"]:
            self.touch()
            return True, "preview already running"
        active = [u for u in RECORDERS if unit_status(u)["active"] == "active"]
        if active:
            return False, "recorders are running (" + ", ".join(active) + "): the live view already shows their data"
        with self.lock:
            self._stop_locked()
            if not LOCKS["camera"].acquire(blocking=False):
                return False, "the cameras are busy with a console test; wait for it to finish"
            self.holding = True
            LIVE_DIR.mkdir(parents=True, exist_ok=True)
            RUN_DIR.mkdir(parents=True, exist_ok=True)
            n = max(len(video_devices()), 1)
            for i, cam in enumerate(CAMERAS, 1):
                conf = read_conf(f"/etc/ewego/cam-{cam}.conf")
                argv = [tool("ewego-cam"), "--device", conf.get("DEVICE", f"auto:{i}/{n}"),
                        "--size", conf.get("SIZE", "1920x1080"), "--fps", conf.get("FPS", "30"),
                        "--name", cam, "--preview", "--live", str(LIVE_DIR), "--stats", "0", "--wait", "10"]
                self._spawn(cam, argv)
            conf = read_conf("/etc/ewego/imu-usb.conf")
            argv = [tool("ewego-imu-usb"), "--preview", "--live", str(LIVE_DIR),
                    "--name", conf.get("NAME", "imu"), "--stats", "0"] + conf.get("DEVICE_ARG", "").split()
            self._spawn("imu", argv)
            if not self.procs:
                msg = self.message
                self._stop_locked()
                return False, "preview could not start: " + msg
            self.started = time.time()
            self.message = "preview running"
            self.touch()
        return True, "preview started: cameras and IMU streaming to the live view, nothing recorded"

    def _spawn(self, name, argv):
        log = open(RUN_DIR / f"preview-{name}.log", "wb")
        try:
            self.procs[name] = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                                                start_new_session=True)
        except OSError as e:
            self.message = f"{argv[0]}: {e}"
        finally:
            log.close()

    def stop(self, why="stopped"):
        with self.lock:
            had = self._stop_locked()
            if had:
                self.message = f"preview {why}"
        return had

    def _stop_locked(self):
        """Kill what is left, forget it, give the camera lock back. Caller holds self.lock."""
        had = False
        for p in self.procs.values():
            if p.poll() is None:
                had = True
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGINT)
                except ProcessLookupError:
                    pass
        for p in self.procs.values():
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                p.wait()
        self.procs = {}
        self.started = None
        if self.holding:
            self.holding = False
            LOCKS["camera"].release()
        return had

    def _reaper(self):
        while True:
            time.sleep(5)
            with self.lock:
                running = any(p.poll() is None for p in self.procs.values())
                stale = bool(self.procs) and not running
            if stale:
                self.stop("ended (see preview-*.log in the runtime directory)")
            elif running and time.monotonic() - self.last_seen > self.IDLE_S:
                self.stop(f"stopped after {self.IDLE_S} s without a live-view client")


PREVIEW = Preview()


def live_json(name):
    p = LIVE_DIR / f"{name}.json"
    try:
        st = p.stat()
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    d["age_s"] = round(time.time() - st.st_mtime, 1)
    return d


def live_frame_info(name):
    try:
        st = (LIVE_DIR / f"{name}.jpg").stat()
    except OSError:
        return None
    return {"age_s": round(time.time() - st.st_mtime, 1), "bytes": st.st_size}


def parse_nav_pvt(data):
    """Last complete UBX NAV-PVT in data, decoded, or None."""
    last = None
    i = 0
    L = len(data)
    while i < L - 8:
        j = data.find(b"\xb5\x62", i)
        if j < 0 or j + 6 > L:
            break
        cls, mid, ln = data[j + 2], data[j + 3], struct.unpack_from("<H", data, j + 4)[0]
        if j + 6 + ln + 2 > L:
            break
        if cls == 1 and mid == 7 and ln >= 92:
            p = j + 6
            y, mo, d, h, mi, s = struct.unpack_from("<HBBBBB", data, p + 4)
            valid = data[p + 11]
            fix = data[p + 20]
            flags = data[p + 21]
            nsv = data[p + 23]
            lon, lat, height, hmsl = struct.unpack_from("<iiii", data, p + 24)
            hacc, vacc = struct.unpack_from("<II", data, p + 40)
            gspeed = struct.unpack_from("<i", data, p + 60)[0]
            carr = (flags >> 6) & 3
            name = FIX_NAMES.get(fix, str(fix))
            if fix == 3 and carr:
                name = "RTK float" if carr == 1 else "RTK fixed"
            last = {"fix": name, "fix_type": fix, "nsv": nsv, "lat": lat / 1e7, "lon": lon / 1e7,
                    "height_m": round(height / 1000.0, 1), "hmsl_m": round(hmsl / 1000.0, 1),
                    "hacc_m": round(hacc / 1000.0, 2), "vacc_m": round(vacc / 1000.0, 2),
                    "speed_ms": round(gspeed / 1000.0, 2),
                    "utc": f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}" if valid & 0x03 else None,
                    "valid_time": bool(valid & 0x03)}
        i = j + 6 + ln + 2
    return last


def gps_live():
    """Newest NAV-PVT in the GPS logger's current .ubx file (the logger owns the
    serial port, so the live view reads its log; Python's file buffer puts a
    few seconds between the receiver and the file)."""
    logs = sorted((EWEGO / "gps-logs").glob("gps_log_*.ubx"), key=lambda p: p.stat().st_mtime) \
        if (EWEGO / "gps-logs").is_dir() else []
    if not logs:
        return {"present": False}
    p = logs[-1]
    try:
        st = p.stat()
        with p.open("rb") as f:
            f.seek(max(0, st.st_size - 65536))
            tail = f.read()
    except OSError:
        return {"present": False}
    out = {"present": True, "file": p.name, "age_s": round(time.time() - st.st_mtime, 1),
           "size_mb": round(st.st_size / 1e6, 1)}
    pvt = parse_nav_pvt(tail)
    if pvt:
        out.update(pvt)
    return out


_LIVE_CACHE = {}


def cached(key, ttl, fn):
    now = time.monotonic()
    hit = _LIVE_CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    v = fn()
    _LIVE_CACHE[key] = (now, v)
    return v


def live_snapshot():
    """Everything the live page shows, in one object (sent 4x a second over SSE).
    Cheap file reads every time; subprocess- or bus-backed values are cached."""
    units = cached("units", 2.0, lambda: {u: unit_status(u)["active"] for u in UNITS})
    du = cached("disk", 5.0, lambda: shutil.disk_usage(EWEGO if EWEGO.exists() else "/"))
    try:
        temp = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000.0
    except (OSError, ValueError):
        temp = None
    cams = {}
    for cam in CAMERAS:
        cams[cam] = {"stats": live_json(cam), "frame": live_frame_info(cam)}
    return {
        "t": time.time(),
        "hostname": socket.gethostname(),
        "units": units,
        "recording": units.get("ewego-record.target") == "active",
        "preview": PREVIEW.status(),
        "cam": cams,
        "imu": live_json("imu"),
        "gps": cached("gps", 1.0, gps_live),
        "pps": cached("pps", 1.0, pps_status),
        "battery": cached("battery", 2.0, battery),
        "disk": {"free_gb": round(du.free / 1e9, 2), "total_gb": round(du.total / 1e9, 2)},
        "load": os.getloadavg()[0],
        "temp_c": temp,
    }


def test_check(write, q):
    argv = [sys.executable, "-u", str(EWEGO / "Firmware/tools/ewego-check.py"), "--root", str(EWEGO)]
    if q.get("list"):
        argv.append("--list")
    if q.get("run"):
        argv += ["--run", q["run"][0]]
    stream_process(argv, write)


TESTS = {
    "fuel": (test_fuel, None),
    "imu": (test_imu, "imu"),
    "gps-raw": (test_gps_raw, "gps"),
    "pps": (test_pps, None),
    "audio": (test_audio, "audio"),
    "alsa": (test_alsa, None),
    "camera-info": (test_camera_info, "camera"),
    "dualcam": (test_dualcam, "camera"),
    "uvc-quirk": (test_uvc_quirk, "camera"),
    "uvc-probe": (test_uvc_probe, "camera"),
    "uvc-cap": (test_uvc_cap, None),
    "camrec": (test_camrec, "camera"),
    "usb": (test_usb, None),
    "imu-usb": (test_imu_usb, None),
    "dmesg": (test_dmesg, None),
    "i2c": (test_i2c, None),
    "serial": (test_serial_ports, None),
    "config": (test_config, None),
    "firstboot": (test_firstboot, None),
    "check": (test_check, None),
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
            elif path == "/live":
                self.send_bytes(LIVE_PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/live/events":
                self.live_events(q)
            elif path.startswith("/api/live/cam/"):
                self.live_cam(q, path[len("/api/live/cam/"):])
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
        q = parse_qs(u.query)
        m = re.fullmatch(r"/api/unit/([a-z0-9@.-]+)/(start|stop|restart)", u.path)
        try:
            if m and m.group(1) in UNITS:
                unit, action = m.group(1), m.group(2)
                if action != "stop" and PREVIEW.stop("stopped for the recorder"):
                    time.sleep(1)   # let the preview processes release the cameras
                rc, out = sh(["systemctl", action, unit], timeout=45)
                self.send_json({"rc": rc, "output": out, "status": unit_status(unit)})
            elif u.path in ("/api/preview/start", "/api/preview/stop"):
                if u.path.endswith("start"):
                    ok, msg = PREVIEW.start()
                else:
                    ok, msg = True, ("preview stopped" if PREVIEW.stop() else "preview was not running")
                self.send_json({"ok": ok, "message": msg, "status": PREVIEW.status()})
            elif u.path in ("/api/power/poweroff", "/api/power/reboot"):
                self.power(u.path.rsplit("/", 1)[1])
            elif u.path == "/api/bridge/start":
                if unit_status("ewego-gps")["active"] == "active":
                    self.send_json({"ok": False, "message": "ewego-gps is running and owns the port; stop it first"})
                    return
                dev = q.get("dev", [GPS_PORT])[0]
                baud = int(q.get("baud", [str(GPS_BAUD)])[0])
                tcp_port = int(q.get("port", ["5010"])[0])
                ok, msg = BRIDGE.start(dev, baud, tcp_port)
                self.send_json({"ok": ok, "message": msg, "status": BRIDGE.status()})
            elif u.path == "/api/bridge/stop":
                ok, msg = BRIDGE.stop()
                self.send_json({"ok": ok, "message": msg, "status": BRIDGE.status()})
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- endpoints --
    def live_events(self, q):
        """Server-sent events: the live snapshot 4x a second, IMU samples
        only the ones the client has not seen (by running sample index)."""
        interval = min(max(float(q.get("interval", ["0.25"])[0]), 0.1), 0.5)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last_n = -1
        try:
            while True:
                PREVIEW.touch()
                snap = live_snapshot()
                imu = snap["imu"]
                if imu:
                    rows = [r for r in imu.pop("recent", []) if r[0] > last_n]
                    if rows:
                        last_n = rows[-1][0]
                    imu["rows"] = rows
                self.write_flush(b"data: " + json.dumps(snap).encode() + b"\n\n")
                time.sleep(interval)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def live_cam(self, q, name):
        """multipart/x-mixed-replace of the recorder's (or preview's) newest
        frame, at most `fps` a second and only when the frame changed. The
        file is replaced atomically, so every read is a whole JPEG."""
        if name not in CAMERAS:
            self.send_json({"error": "unknown camera"}, 404)
            return
        try:
            fps = min(max(float(q.get("fps", ["2"])[0]), 0.2), 10.0)
        except ValueError:
            fps = 2.0
        p = LIVE_DIR / f"{name}.jpg"
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last_mtime, last_sent = None, 0.0
        try:
            while True:
                # the browser never sends on this connection again: readable means closed
                r, _, _ = select.select([self.connection], [], [], 0)
                if r and not self.connection.recv(1, socket.MSG_PEEK):
                    return
                try:
                    st = p.stat()
                except OSError:
                    st = None
                now = time.monotonic()
                if st and st.st_mtime_ns != last_mtime and now - last_sent >= 1.0 / fps:
                    try:
                        data = p.read_bytes()
                    except OSError:
                        data = b""
                    if data.startswith(b"\xff\xd8"):
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
                        self.wfile.flush()
                        last_mtime, last_sent = st.st_mtime_ns, now
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def power(self, action):
        """Stop recording cleanly (every recorder writes its summary), flush
        the card, then power off or reboot a second after answering."""
        PREVIEW.stop("stopped for " + action)
        rc, out = sh(["systemctl", "stop", "ewego-record.target"], timeout=90)
        sh(["sync"], timeout=60)
        self.send_json({"ok": True, "action": action, "stop_rc": rc, "output": out,
                        "message": f"recorders stopped (rc {rc}), disk synced, {action} in a moment: "
                                   "the wearable will drop off the network"})
        threading.Thread(target=lambda: (time.sleep(1), subprocess.run(["systemctl", action])), daemon=True).start()

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
        unit = q.get("unit", ["ewego-cam@camera1"])[0]
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
  <span id="pps"></span>
  <span id="disk"></span>
  <span id="load"></span>
  <span id="ips"></span>
  <span><a href="/live" style="color:var(--accent)">Live view</a></span>
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
    <button onclick="run('firstboot','out-devs')">apt / first-boot history</button>
    <button onclick="run('dmesg','out-devs')">dmesg tail</button>
  </div>
  <pre id="out-devs"></pre>
</section>

<section class="card">
  <h2>Units</h2>
  <table><thead><tr><th>unit</th><th>active</th><th>enabled</th><th></th></tr></thead><tbody id="units"></tbody></table>
  <div class="row">
    <select id="jr-unit"><option>ewego-cam@camera1</option><option>ewego-cam@camera2</option><option>ewego-imu-usb</option><option>ewego-gps</option><option>ewego-audio</option><option>ewego-fuel</option><option>ewego-record.target</option><option>ewego-webtest</option></select>
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
    <button onclick="run('pps','out-gps','&seconds='+val('pps-s'))">PPS watch</button>
    <input id="pps-s" value="10" size="3"> s
    <button onclick="unit('ewego-gps','start')">Start logger</button>
    <button onclick="unit('ewego-gps','stop')">Stop logger</button>
    <button onclick="journal('ewego-gps','out-gps')">Logger journal</button>
  </div>
  <div class="row">
    <button onclick="bridge('start')">Serial bridge for PyGPSClient: start</button>
    <button onclick="bridge('stop')">stop</button>
    TCP port <input id="br-port" value="5010" size="5"> at <select id="br-baud"><option>460800</option><option>230400</option><option>115200</option><option>38400</option></select>
    <span id="br-status"></span>
  </div>
  <pre id="out-gps" class="tall"></pre>
</section>

<section class="card">
  <h2>USB IMU (hub_imu, BNO055 over HID)</h2>
  <div class="row">
    <button class="primary" onclick="run('imu-usb','out-imuusb','&seconds='+val('iu-s'))">Read stream</button>
    <input id="iu-s" value="5" size="3"> s
    <button onclick="unit('ewego-imu-usb','start')">Start logger</button>
    <button onclick="unit('ewego-imu-usb','stop')">Stop logger</button>
    <button onclick="journal('ewego-imu-usb','out-imuusb')">Logger journal</button>
  </div>
  <pre id="out-imuusb"></pre>
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
    <button onclick="run('uvc-probe','out-cam','&dev='+encodeURIComponent(val('cam-dev'))+'&size='+val('cam-size')+'&fps='+val('cam-fps'))">Bandwidth probe</button>
    <span id="cam-msg"></span>
  </div>
  <div class="row">
    <button class="primary" onclick="dualcam()">Drop test: selected cameras at once</button>
    <span id="dc-list"></span>
    <input id="dc-s" value="20" size="3"> s at the size and fps above, no streaming, frames discarded
    · stagger <input id="dc-st" value="1" size="3"> s
    <button onclick="run('uvc-quirk','out-cam','&on=1')">Reload uvcvideo with quirks=128</button>
    <button onclick="run('uvc-quirk','out-cam','&on=0')">Reload without</button>
    · max_payload <input id="uvc-cap" value="1900" size="5">
    <button onclick="run('uvc-cap','out-cam','&value='+val('uvc-cap'))">Set (live)</button>
  </div>
  <div class="row">
    <button class="primary" onclick="camrec()">Record test with ewego-cam: selected cameras</button>
    <input id="cr-s" value="20" size="3"> s, writes real files (mjpeg + timestamps + index + summary) under recordings/webtest/
  </div>
  <img id="cam" alt="">
  <pre id="out-cam"></pre>
</section>

<section class="card wide">
  <h2>Recorder (ewego-record.target: cameras, USB IMU, GPS, audio, fuel gauge)</h2>
  <div class="row">
    <button class="primary" onclick="unit('ewego-record.target','start')">Start all</button>
    <button onclick="unit('ewego-record.target','stop')">Stop all</button>
    <button onclick="journal('ewego-cam@camera1','out-rec')">camera1 journal</button>
    <button onclick="journal('ewego-imu-usb','out-rec')">IMU journal</button>
    <button onclick="listSessions('out-rec')">Sessions</button>
    <button onclick="run('check','out-rec')">Check last run</button>
    <button onclick="run('check','out-rec','&list=1')">List runs</button>
    <a href="/live">Live view</a>
  </div>
  <div class="row">
    <button onclick="power('poweroff')">Shut down</button>
    <button onclick="power('reboot')">Reboot</button>
    <span style="color:var(--muted);font-size:13px">stops the recorders first so every summary gets written, syncs the card, then powers off: use this instead of the switch after a recording</span>
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
    const b = s.bridge;
    $('br-status').innerHTML = b.running
      ? '<span class="ok">bridge on :' + b.port + '</span> ' + b.dev + ' @ ' + b.baud + (b.client ? ' · client ' + b.client : ' · waiting for client') + ' · to laptop ' + (b.tx/1024).toFixed(0) + ' KB, from laptop ' + b.rx + ' B' + (b.error ? ' · <span class="bad">' + b.error + '</span>' : '')
      : 'bridge off';
    const p = s.pps;
    $('pps').innerHTML = !p.present ? '<span class="bad">PPS: no device</span>'
        : (p.pulses ? '<span class="ok">PPS live</span> seq ' + p.seq + (p.rate_hz != null ? ' · ' + p.rate_hz.toFixed(2) + ' Hz' : '') + ' · clock ' + (p.clock_offset_ms >= 0 ? '+' : '') + p.clock_offset_ms + ' ms'
                    : '<span class="bad">PPS: no pulse</span>' + (p.seq ? ' (last ' + Math.round(p.age_s) + ' s ago)' : ' (waiting for fix)'));
    $('disk').textContent = 'free ' + s.disk.free_gb + ' / ' + s.disk.total_gb + ' GB';
    $('load').textContent = 'load ' + s.load.map(x => x.toFixed(2)).join(' ') + (s.temp_c != null ? ' · ' + s.temp_c.toFixed(0) + ' °C' : '');
    $('ips').textContent = s.ips.join(' ');
    let d = '';
    for (const [p, ok] of Object.entries(s.devices)) d += '<b>' + p + '</b><span class="' + (ok ? 'ok">present' : 'bad">missing') + '</span>';
    d += '<b>audio card</b>' + (s.audio_card ? '<span class="ok">' + s.audio_card.name + ' (card ' + s.audio_card.index + ')</span>' : '<span class="bad">missing</span>');
    d += '<b>USB cameras</b>' + (s.video.length ? '<span class="ok">' + s.video.map(v => v.dev + ' (port ' + v.port + ')').join(', ') + '</span>' : '<span class="bad">none</span>');
    $('devs').innerHTML = d;
    let u = '';
    for (const x of s.units) {
      const cls = x.active === 'active' ? 'ok' : (x.active === 'failed' ? 'bad' : '');
      u += '<tr><td>' + x.unit + '</td><td class="' + cls + '">' + x.active + '</td><td>' + x.enabled + '</td><td>' +
        (x.unit === 'ewego-webtest' ? '' : '<button onclick="unit(\'' + x.unit + '\',\'start\')">start</button> <button onclick="unit(\'' + x.unit + '\',\'stop\')">stop</button>') + '</td></tr>';
    }
    $('units').innerHTML = u;
    const sel = $('cam-dev');
    const key = s.video.map(v => v.dev).join(',');
    if (sel.dataset.key !== key) {
      sel.dataset.key = key;
      sel.innerHTML = s.video.map(v => '<option value="' + v.dev + '">' + v.dev + ' · port ' + v.port + (v.name ? ' · ' + v.name : '') + '</option>').join('');
      $('dc-list').innerHTML = s.video.map(v => '<label><input type="checkbox" class="dc-dev" value="' + v.dev + '" checked> ' + v.dev + ' (port ' + v.port + ')</label>').join(' ');
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

async function bridge(action) {
  const r = await (await fetch('/api/bridge/' + action + '?port=' + val('br-port') + '&baud=' + val('br-baud'), {method: 'POST'})).json();
  $('out-gps').textContent = r.message + (r.ok && action === 'start'
    ? '\n\nIn PyGPSClient: Settings -> connection "Socket", host ' + location.hostname + ', port ' + val('br-port') + ', protocol TCP client, then Connect.\n'
      + 'The bridge is two-way, so PyGPSClient can also send UBX configuration to the module.\nStop the bridge before starting the GPS logger unit.'
    : '');
  refresh();
}

async function power(action) {
  if (!confirm((action === 'poweroff' ? 'Shut down' : 'Reboot') + ' the wearable? Recording stops first.')) return;
  $('out-rec').textContent = 'stopping recorders…';
  const r = await (await fetch('/api/power/' + action, {method: 'POST'})).json();
  $('out-rec').textContent = r.message + '\n' + (r.output || '');
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

function dualcam() {
  camLive(false);
  const devs = Array.from(document.querySelectorAll('.dc-dev:checked')).map(o => o.value).join(',');
  if (!devs) { $('out-cam').textContent = 'no cameras selected'; return; }
  run('dualcam', 'out-cam', '&seconds=' + val('dc-s') + '&stagger=' + val('dc-st') + '&size=' + val('cam-size') + '&fps=' + val('cam-fps') + '&devs=' + encodeURIComponent(devs));
}

function camrec() {
  camLive(false);
  const devs = Array.from(document.querySelectorAll('.dc-dev:checked')).map(o => o.value).join(',');
  if (!devs) { $('out-cam').textContent = 'no cameras selected'; return; }
  run('camrec', 'out-cam', '&seconds=' + val('cr-s') + '&stagger=' + val('dc-st') + '&size=' + val('cam-size') + '&fps=' + val('cam-fps') + '&devs=' + encodeURIComponent(devs));
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


# ---------------------------------------------------------------------------
# live page: cameras, IMU traces, GPS, battery, recorder controls
# ---------------------------------------------------------------------------

LIVE_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EweGo live</title>
<style>
  :root { --bg:#f4f5f0; --card:#fbfbf8; --ink:#1e2420; --muted:#5c665f; --line:#d6dad2; --grid:#e3e6de;
          --accent:#b94a15; --ok:#2f6b3a; --bad:#9b2c2c; --warn:#8a5a00; --code:#eceee7;
          --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#151816; --card:#1c201d; --ink:#e4e7df; --muted:#9aa39b; --line:#313732; --grid:#262b27;
            --accent:#e8834e; --ok:#7fbf8a; --bad:#e07a7a; --warn:#e0b25a; --code:#232823;
            --s1:#3987e5; --s2:#d95926; --s3:#199e70; } }
  body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.4 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; }
  header { padding:10px 16px; border-bottom:1px solid var(--line); display:flex; flex-wrap:wrap; gap:6px 18px; align-items:baseline; }
  header h1 { font-size:17px; margin:0; }
  header a { color:var(--accent); }
  header span { color:var(--muted); font-size:13px; }
  .badge { font-weight:700; font-size:12px; padding:2px 8px; border-radius:3px; color:#fff; background:var(--muted); }
  .badge.rec { background:var(--bad); } .badge.pre { background:var(--ok); }
  .ok { color:var(--ok); font-weight:600; } .bad { color:var(--bad); font-weight:600; } .warn { color:var(--warn); font-weight:600; }
  .bar { padding:8px 16px; display:flex; flex-wrap:wrap; gap:6px; align-items:center; border-bottom:1px solid var(--line); }
  main { display:grid; grid-template-columns:repeat(auto-fit, minmax(320px, 1fr)); gap:12px; padding:12px 16px 30px; }
  .card { background:var(--card); border:1px solid var(--line); padding:10px 12px; display:flex; flex-direction:column; gap:6px; min-width:0; }
  .card h2 { font-size:14px; margin:0; display:flex; justify-content:space-between; align-items:center; gap:8px; }
  .card h2 small { color:var(--muted); font-weight:400; font-size:12px; }
  .wide { grid-column:1 / -1; }
  .camwrap { position:relative; background:#000; aspect-ratio:16/9; display:flex; align-items:center; justify-content:center; overflow:hidden; }
  .camwrap img { width:100%; height:100%; object-fit:contain; display:block; }
  .camwrap img.flip { transform:rotate(180deg); }
  .camwrap .msg { position:absolute; color:#ddd; font-size:13px; background:rgba(0,0,0,.55); padding:4px 8px; display:none; }
  .camwrap.stale .msg { display:block; }
  .stats { font:12px/1.4 ui-monospace, Menlo, Consolas, monospace; color:var(--muted); white-space:pre-wrap; }
  button { font:inherit; font-size:13px; padding:5px 10px; border:1px solid var(--line); background:var(--code); color:var(--ink); cursor:pointer; }
  button:hover { border-color:var(--accent); }
  button.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
  button.danger { border-color:var(--bad); color:var(--bad); }
  button.on { border-color:var(--accent); box-shadow:inset 0 0 0 1px var(--accent); }
  select { font:inherit; font-size:13px; padding:4px 6px; background:var(--code); color:var(--ink); border:1px solid var(--line); }
  canvas { width:100%; height:130px; display:block; }
  .chart { position:relative; }
  .legend { display:flex; gap:14px; font-size:12px; color:var(--muted); flex-wrap:wrap; }
  .legend i { display:inline-block; width:10px; height:3px; vertical-align:middle; margin-right:5px; border-radius:2px; }
  .legend b { color:var(--ink); font-weight:600; font-variant-numeric:tabular-nums; }
  .kv { display:grid; grid-template-columns:auto 1fr; gap:2px 12px; font-size:13px; }
  .kv b { color:var(--muted); font-weight:500; }
  #msg { color:var(--muted); font-size:13px; }
  .hint { color:var(--muted); font-size:12px; }
</style></head>
<body>
<header>
  <h1 id="host">EweGo live</h1>
  <span id="state" class="badge">…</span>
  <span id="batt"></span>
  <span id="gpsq"></span>
  <span id="disk"></span>
  <span id="load"></span>
  <span id="link"></span>
  <span><a href="/">test console</a></span>
</header>
<div class="bar">
  view rate <select id="rate"><option value="0.5">0.5 fps</option><option value="1">1 fps</option><option value="2" selected>2 fps</option><option value="5">5 fps</option></select>
  <button id="pause" onclick="togglePause()">Pause views</button>
  <span style="flex:1"></span>
  <button id="pv" onclick="preview()">Preview (no recording)</button>
  <button class="primary" onclick="rec('start')">Start recording</button>
  <button onclick="rec('stop')">Stop recording</button>
  <button class="danger" onclick="power('poweroff')">Shut down</button>
  <button class="danger" onclick="power('reboot')">Reboot</button>
  <span id="msg"></span>
</div>
<main>

<section class="card">
  <h2>camera1 <small id="c1-tag"></small><button onclick="flip('camera1')" id="flip-camera1">Flip 180°</button></h2>
  <div class="camwrap" id="w-camera1"><img id="img-camera1" alt="camera1"><span class="msg" id="m-camera1">no frames</span></div>
  <div class="stats" id="s-camera1">–</div>
</section>

<section class="card">
  <h2>camera2 <small id="c2-tag"></small><button onclick="flip('camera2')" id="flip-camera2">Flip 180°</button></h2>
  <div class="camwrap" id="w-camera2"><img id="img-camera2" alt="camera2"><span class="msg" id="m-camera2">no frames</span></div>
  <div class="stats" id="s-camera2">–</div>
</section>

<section class="card wide">
  <h2>USB IMU (BNO055) <small>last 10 s · hover for values</small></h2>
  <div class="stats" id="s-imu">–</div>
  <div class="chart"><div class="legend" id="lg-acc"></div><canvas id="ch-acc"></canvas></div>
  <div class="chart"><div class="legend" id="lg-gyr"></div><canvas id="ch-gyr"></canvas></div>
  <div class="chart"><div class="legend" id="lg-eul"></div><canvas id="ch-eul"></canvas></div>
</section>

<section class="card">
  <h2>GPS <small id="gps-src"></small></h2>
  <div class="kv" id="gps"></div>
  <div class="hint">Position comes from the GPS logger's file, so it is only live while ewego-gps runs (a few seconds behind the receiver). PPS is read from the kernel directly.</div>
</section>

<section class="card">
  <h2>Wearable</h2>
  <div class="kv" id="sys"></div>
  <table style="font-size:13px;border-collapse:collapse"><tbody id="units"></tbody></table>
</section>

</main>
<script>
const $ = id => document.getElementById(id);
const fmtDur = s => { s = Math.round(s||0); return s >= 3600 ? Math.floor(s/3600)+'h'+String(Math.floor(s%3600/60)).padStart(2,'0')+'m' : Math.floor(s/60)+'m'+String(s%60).padStart(2,'0')+'s'; };
const SERIES = [getComputedStyle(document.documentElement).getPropertyValue('--s1').trim(),
                getComputedStyle(document.documentElement).getPropertyValue('--s2').trim(),
                getComputedStyle(document.documentElement).getPropertyValue('--s3').trim()];

// ---- cameras: one multipart stream per camera, parsed by hand so the page can count bytes ----
const cams = {camera1: {}, camera2: {}};
let paused = false, bytesThisSec = 0, kbps = 0;
setInterval(() => { kbps = bytesThisSec / 1024; bytesThisSec = 0; $('link').textContent = 'views ' + kbps.toFixed(0) + ' kB/s'; }, 1000);

function findSeq(buf, seq, from) {
  outer: for (let i = from; i <= buf.length - seq.length; i++) {
    for (let j = 0; j < seq.length; j++) if (buf[i+j] !== seq[j]) continue outer;
    return i;
  }
  return -1;
}
const CRLF2 = [13,10,13,10];

async function streamCam(name) {
  const c = cams[name];
  if (c.ctl) c.ctl.abort();
  if (paused) return;
  const ctl = new AbortController(); c.ctl = ctl;
  try {
    const r = await fetch('/api/live/cam/' + name + '?fps=' + $('rate').value + '&t=' + Date.now(), {signal: ctl.signal});
    const reader = r.body.getReader();
    let buf = new Uint8Array(0);
    for (;;) {
      const {value, done} = await reader.read(); if (done) break;
      bytesThisSec += value.length;
      const nb = new Uint8Array(buf.length + value.length); nb.set(buf); nb.set(value, buf.length); buf = nb;
      for (;;) {
        const h = findSeq(buf, CRLF2, 0); if (h < 0) { if (buf.length > 1<<20) buf = new Uint8Array(0); break; }
        const hdr = new TextDecoder().decode(buf.subarray(0, h));
        const m = /Content-Length:\s*(\d+)/i.exec(hdr);
        if (!m) { buf = buf.subarray(h + 4); continue; }
        const len = +m[1]; if (buf.length < h + 4 + len) break;
        const body = buf.slice(h + 4, h + 4 + len); buf = buf.subarray(h + 4 + len);
        const url = URL.createObjectURL(new Blob([body], {type: 'image/jpeg'}));
        const img = $('img-' + name); const old = img.src; img.src = url; if (old) URL.revokeObjectURL(old);
        c.lastFrame = Date.now(); c.frameBytes = len;
      }
    }
  } catch (e) { /* aborted or dropped */ }
  if (!ctl.signal.aborted) setTimeout(() => streamCam(name), 2000);
}
function restartCams() { for (const n in cams) streamCam(n); }
$('rate').onchange = restartCams;
function togglePause() {
  paused = !paused; $('pause').textContent = paused ? 'Resume views' : 'Pause views'; $('pause').classList.toggle('on', paused);
  if (paused) { for (const n in cams) if (cams[n].ctl) cams[n].ctl.abort(); } else restartCams();
}
function flip(name) {
  const img = $('img-' + name); img.classList.toggle('flip');
  try { localStorage.setItem('ewego.flip.' + name, img.classList.contains('flip') ? '1' : '0'); } catch (e) {}
  $('flip-' + name).classList.toggle('on', img.classList.contains('flip'));
}
for (const n in cams) { try { if (localStorage.getItem('ewego.flip.' + n) === '1') { $('img-' + n).classList.add('flip'); $('flip-' + n).classList.add('on'); } } catch (e) {} }

// ---- IMU traces ----
const WIN_US = 10e6;
const imu = {t: [], ch: {heading: [], roll: [], pitch: [], ax: [], ay: [], az: [], gx: [], gy: [], gz: []}};
const CHARTS = [
  {id: 'acc', title: 'acceleration m/s²', keys: ['ax', 'ay', 'az'], names: ['x', 'y', 'z']},
  {id: 'gyr', title: 'angular rate °/s', keys: ['gx', 'gy', 'gz'], names: ['x', 'y', 'z']},
  {id: 'eul', title: 'orientation °', keys: ['heading', 'roll', 'pitch'], names: ['heading', 'roll', 'pitch']},
];
let hoverX = null;
for (const ch of CHARTS) {
  const cv = $('ch-' + ch.id);
  cv.addEventListener('mousemove', e => { hoverX = e.offsetX / cv.clientWidth; draw(); });
  cv.addEventListener('mouseleave', () => { hoverX = null; draw(); });
}
function addRows(rows) {
  // cols: n, seq, dev_ts_us, host_mono_us, heading, roll, pitch, ax, ay, az, gx, gy, gz, lax, lay, laz, flags
  for (const r of rows) {
    if (r[4] === null) continue;
    imu.t.push(r[3]);
    imu.ch.heading.push(r[4]); imu.ch.roll.push(r[5]); imu.ch.pitch.push(r[6]);
    imu.ch.ax.push(r[7]); imu.ch.ay.push(r[8]); imu.ch.az.push(r[9]);
    imu.ch.gx.push(r[10]); imu.ch.gy.push(r[11]); imu.ch.gz.push(r[12]);
  }
  const tEnd = imu.t.length ? imu.t[imu.t.length - 1] : 0;
  let cut = 0; while (cut < imu.t.length && imu.t[cut] < tEnd - WIN_US - 2e6) cut++;
  if (cut) { imu.t.splice(0, cut); for (const k in imu.ch) imu.ch[k].splice(0, cut); }
}
let drawPending = false;
function draw() { if (drawPending) return; drawPending = true; requestAnimationFrame(() => { drawPending = false; drawNow(); }); }
function drawNow() {
  const n = imu.t.length; if (!n) return;
  const tEnd = imu.t[n - 1], t0 = tEnd - WIN_US;
  const css = getComputedStyle(document.documentElement);
  const grid = css.getPropertyValue('--grid').trim(), muted = css.getPropertyValue('--muted').trim(), ink = css.getPropertyValue('--ink').trim();
  for (const ch of CHARTS) {
    const cv = $('ch-' + ch.id), dpr = window.devicePixelRatio || 1;
    const W = cv.clientWidth, H = cv.clientHeight;
    if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
    const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
    const padL = 44, padR = 8, padT = 6, padB = 16;
    let lo = Infinity, hi = -Infinity, i0 = 0;
    while (i0 < n && imu.t[i0] < t0) i0++;
    for (const k of ch.keys) for (let i = i0; i < n; i++) { const v = imu.ch[k][i]; if (v < lo) lo = v; if (v > hi) hi = v; }
    if (!isFinite(lo)) continue;
    if (hi - lo < 1e-6) { lo -= 1; hi += 1; }
    const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
    const X = t => padL + (t - t0) / WIN_US * (W - padL - padR), Y = v => padT + (hi - v) / (hi - lo) * (H - padT - padB);
    // recessive grid: 3 horizontal lines with labels, 1-second ticks
    g.strokeStyle = grid; g.lineWidth = 1; g.fillStyle = muted; g.font = '10px ui-monospace, Menlo, monospace'; g.textAlign = 'right';
    for (let k = 0; k <= 2; k++) { const v = lo + (hi - lo) * k / 2, y = Y(v); g.beginPath(); g.moveTo(padL, y); g.lineTo(W - padR, y); g.stroke(); g.fillText(v.toFixed(Math.abs(hi - lo) < 5 ? 2 : 0), padL - 4, y + 3); }
    g.textAlign = 'center';
    for (let s = 0; s <= 10; s += 2) { const x = X(t0 + s * 1e6); g.beginPath(); g.moveTo(x, padT); g.lineTo(x, H - padB); g.stroke(); g.fillText('-' + (10 - s) + ' s', x, H - 4); }
    // the traces: 2 px, series colors in fixed order
    ch.keys.forEach((k, si) => {
      g.strokeStyle = SERIES[si]; g.lineWidth = 2; g.lineJoin = 'round'; g.beginPath();
      let first = true;
      for (let i = i0; i < n; i++) { const x = X(imu.t[i]), y = Y(imu.ch[k][i]); if (first) { g.moveTo(x, y); first = false; } else g.lineTo(x, y); }
      g.stroke();
    });
    // hover crosshair: values at the pointer's time go into the legend
    let hi_i = n - 1;
    if (hoverX !== null) {
      const th = t0 + Math.min(Math.max((hoverX * W - padL) / (W - padL - padR), 0), 1) * WIN_US;
      hi_i = i0; while (hi_i < n - 1 && imu.t[hi_i] < th) hi_i++;
      const x = X(imu.t[hi_i]); g.strokeStyle = ink; g.lineWidth = 1; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(x, padT); g.lineTo(x, H - padB); g.stroke(); g.setLineDash([]);
      ch.keys.forEach((k, si) => { g.fillStyle = SERIES[si]; g.beginPath(); g.arc(x, Y(imu.ch[k][hi_i]), 4, 0, 7); g.fill(); });
    }
    $('lg-' + ch.id).innerHTML = '<span>' + ch.title + (hoverX !== null ? ' at -' + ((tEnd - imu.t[hi_i]) / 1e6).toFixed(2) + ' s' : '') + '</span>' +
      ch.keys.map((k, si) => '<span><i style="background:' + SERIES[si] + '"></i>' + ch.names[si] + ' <b>' + imu.ch[k][hi_i].toFixed(2) + '</b></span>').join('');
  }
}
window.addEventListener('resize', draw);

// ---- status stream ----
function camLine(name, c) {
  const st = c.stats, fr = c.frame, wrap = $('w-' + name), m = $('m-' + name);
  if (!st && !fr) { wrap.classList.add('stale'); m.textContent = 'no frames: not recording and no preview'; $('s-' + name).textContent = '–'; return; }
  const stale = fr && fr.age_s > 4;
  wrap.classList.toggle('stale', !!stale); if (stale) m.textContent = 'last frame ' + Math.round(fr.age_s) + ' s ago';
  if (!st) { $('s-' + name).textContent = fr ? 'frame ' + (fr.bytes / 1024).toFixed(0) + ' kB, ' + fr.age_s + ' s old' : '–'; return; }
  $('c' + name.slice(-1) + '-tag').textContent = (st.preview ? 'preview' : 'recording') + ' · ' + st.width + '×' + st.height + ' @ ' + st.fps_requested;
  const bad = st.lost || st.overruns || st.errors;
  $('s-' + name).innerHTML = st.fps.toFixed(1) + ' fps · ' + st.frames + ' frames · ' + (bad ? '<span class="bad">' : '') + st.lost + ' lost, ' + st.overruns + ' overruns, ' + st.errors + ' err' + (bad ? '</span>' : '') +
    ' · ' + fmtDur(st.elapsed_s) + (st.preview ? '' : ' · ' + st.written_mb.toFixed(0) + ' MB · write max ' + st.write_max_ms + ' ms · queue ' + st.queue_mb + ' MB (peak ' + st.queue_peak_mb + ')') +
    (fr ? ' · frame ' + (fr.bytes / 1024).toFixed(0) + ' kB' : '');
}
function imuLine(st) {
  if (!st) { $('s-imu').textContent = 'no data: not recording and no preview'; return; }
  const bad = st.lost || st.queue_overruns || st.flagged;
  $('s-imu').innerHTML = (st.preview ? 'preview' : 'recording') + ' · ' + st.hz.toFixed(1) + ' Hz · ' + st.samples + ' samples · ' + (bad ? '<span class="bad">' : '') + st.lost + ' lost, ' + st.flagged + ' flagged, ' + st.queue_overruns + ' overruns' + (bad ? '</span>' : '') +
    ' · drift ' + (st.drift_ppm >= 0 ? '+' : '') + st.drift_ppm + ' ppm · queue peak ' + st.queue_peak + ' · ' + fmtDur(st.elapsed_s) +
    (st.cal ? ' · cal sys' + st.cal[0] + ' gyro' + st.cal[1] + ' acc' + st.cal[2] + ' mag' + st.cal[3] : '') + (st.temp_c != null ? ' · ' + st.temp_c + ' °C' : '') +
    (st.age_s > 2 ? ' · <span class="warn">stale ' + st.age_s + ' s</span>' : '');
}
function gpsBlock(gp, pps) {
  const fixCls = f => /RTK|3D|GNSS/.test(f || '') ? 'ok' : (f === '2D' ? 'warn' : 'bad');
  $('gps-src').textContent = gp.present ? gp.file + ' · ' + gp.size_mb + ' MB · written ' + Math.round(gp.age_s) + ' s ago' : 'no GPS log';
  let h = '';
  if (gp.fix !== undefined) {
    h += '<b>fix</b><span class="' + fixCls(gp.fix) + '">' + gp.fix + '</span><b>satellites</b><span>' + gp.nsv + '</span>' +
         '<b>accuracy</b><span>h ' + gp.hacc_m + ' m · v ' + gp.vacc_m + ' m</span>' +
         '<b>position</b><span>' + gp.lat.toFixed(6) + ', ' + gp.lon.toFixed(6) + ' <a target="_blank" href="https://www.openstreetmap.org/?mlat=' + gp.lat + '&mlon=' + gp.lon + '#map=17/' + gp.lat + '/' + gp.lon + '">map</a></span>' +
         '<b>height</b><span>' + gp.hmsl_m + ' m MSL · ' + gp.height_m + ' m ellipsoid</span><b>speed</b><span>' + gp.speed_ms + ' m/s</span>' +
         '<b>GPS time</b><span>' + (gp.utc || 'not valid') + '</span>';
  } else if (gp.present) h += '<b>fix</b><span class="bad">no NAV-PVT in the last 64 kB of the log</span>';
  else h += '<b>fix</b><span>start recording (or ewego-gps) to see the receiver</span>';
  h += '<b>PPS</b>' + (!pps.present ? '<span class="bad">no device</span>' : pps.pulses ? '<span class="ok">live</span> <span>seq ' + pps.seq + (pps.rate_hz != null ? ' · ' + pps.rate_hz.toFixed(2) + ' Hz' : '') + ' · clock ' + (pps.clock_offset_ms >= 0 ? '+' : '') + pps.clock_offset_ms + ' ms</span>' : '<span class="bad">no pulse</span>');
  $('gps').innerHTML = h;
  $('gpsq').innerHTML = gp.fix !== undefined ? 'GPS <span class="' + fixCls(gp.fix) + '">' + gp.fix + '</span> ' + gp.nsv + ' sv · ' + gp.hacc_m + ' m' : 'GPS: no data';
}
let lastSnap = 0;
function update(s) {
  lastSnap = Date.now();
  $('host').textContent = 'EweGo live · ' + s.hostname;
  const st = $('state'); st.className = 'badge ' + (s.recording ? 'rec' : s.preview.running ? 'pre' : '');
  st.textContent = s.recording ? 'RECORDING' : s.preview.running ? 'PREVIEW' : 'IDLE';
  $('pv').textContent = s.preview.running ? 'Stop preview' : 'Preview (no recording)'; $('pv').classList.toggle('on', s.preview.running);
  $('batt').innerHTML = s.battery.error ? '<span class="bad">fuel gauge: ' + s.battery.error + '</span>' : 'battery ' + s.battery.voltage + ' V · <span class="' + (s.battery.soc < 15 ? 'bad' : s.battery.soc < 30 ? 'warn' : '') + '">' + s.battery.soc.toFixed(0) + ' %</span>';
  $('disk').textContent = 'free ' + s.disk.free_gb + ' / ' + s.disk.total_gb + ' GB';
  $('load').textContent = 'load ' + s.load.toFixed(2) + (s.temp_c != null ? ' · ' + s.temp_c.toFixed(0) + ' °C' : '');
  for (const n in cams) camLine(n, s.cam[n]);
  imuLine(s.imu);
  if (s.imu && s.imu.rows && s.imu.rows.length) { addRows(s.imu.rows); draw(); }
  gpsBlock(s.gps, s.pps);
  $('sys').innerHTML = '<b>preview</b><span>' + (s.preview.message || 'off') + '</span><b>clock</b><span>' + new Date(s.t * 1000).toISOString().replace('T', ' ').slice(0, 19) + ' UTC</span>';
  $('units').innerHTML = Object.entries(s.units).map(([u, a]) => '<tr><td style="padding:2px 8px 2px 0">' + u + '</td><td class="' + (a === 'active' ? 'ok' : a === 'failed' ? 'bad' : '') + '">' + a + '</td></tr>').join('');
}
function connect() {
  const es = new EventSource('/api/live/events');
  es.onmessage = e => { try { update(JSON.parse(e.data)); } catch (err) { $('msg').textContent = 'bad update: ' + err; } };
  es.onerror = () => { $('state').textContent = 'NO LINK'; $('state').className = 'badge'; };
}
setInterval(() => { if (Date.now() - lastSnap > 5000) { $('state').textContent = 'NO LINK'; $('state').className = 'badge'; } }, 2000);

// ---- controls ----
async function post(url) { const r = await fetch(url, {method: 'POST'}); return r.json(); }
async function preview() {
  const on = $('pv').classList.contains('on');
  const r = await post('/api/preview/' + (on ? 'stop' : 'start'));
  $('msg').textContent = r.message;
  if (!on && r.ok) setTimeout(restartCams, 1500);
}
async function rec(action) {
  $('msg').textContent = action + 'ing recorder…';
  const r = await post('/api/unit/ewego-record.target/' + action);
  $('msg').textContent = 'systemctl ' + action + ' ewego-record.target → rc ' + r.rc + (r.output ? ' ' + r.output.trim() : '');
  setTimeout(restartCams, 3000);
}
async function power(action) {
  if (!confirm((action === 'poweroff' ? 'Shut down' : 'Reboot') + ' the wearable? Recording stops first and every recorder writes its summary.')) return;
  $('msg').textContent = 'stopping recorders…';
  const r = await post('/api/power/' + action);
  $('msg').textContent = r.message;
}
restartCams(); connect();
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
