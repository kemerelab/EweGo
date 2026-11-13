#!/usr/bin/env python3
"""Check the data from one ewego-record.target run.

Each recorder writes its own timestamped session directory:
  <root>/recordings/camera1/<stamp>/camera1.mjpeg, _timestamps.bin, _index.bin, _summary.json
  <root>/recordings/camera2/<stamp>/...
  <root>/recordings/imu/<stamp>/imu.csv, imu_summary.json
  <root>/recordings/audio/<stamp>/audio.wav
  <root>/recordings/fuel/<stamp>/fuel_gauge.csv
  <root>/gps-logs/gps_log_<stamp>.ubx, _timesync.csv
This tool groups the sessions that started within a minute of each other
into one run (the latest by default), checks each stream and prints a
verdict, then shows how far apart the streams started.

Standard library only; runs on the wearable or on a copy of /opt/ewego.

  ewego-check.py                       # latest run under /opt/ewego
  ewego-check.py --run 20261112_2203   # the run whose stamps start like this
  ewego-check.py --list                # all runs
  ewego-check.py --collect /tmp/run1   # lay the run out for dualcam/play_with_timestamps.py
  ewego-check.py --root ~/ewego-copy   # after rsync'ing /opt/ewego somewhere
"""

import argparse
import csv
import json
import os
import re
import struct
import sys
from datetime import datetime
from pathlib import Path

STAMP_RE = re.compile(r"(\d{8}_\d{6})")
UNITS = ["camera1", "camera2", "imu", "audio", "fuel"]
GROUP_WINDOW_S = 60


def stamp_of(name):
    m = STAMP_RE.search(name)
    return m.group(1) if m else None


def stamp_time(stamp):
    return datetime.strptime(stamp, "%Y%m%d_%H%M%S")


def fmt_dur(s):
    if s is None:
        return "?"
    s = int(s)
    return f"{s // 3600}h{(s % 3600) // 60:02d}m{s % 60:02d}s" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


# --------------------------------------------------------------------------
# finding sessions and grouping them into runs
# --------------------------------------------------------------------------

def sessions(root):
    """Return [(unit, stamp, path)] for every session directory / GPS file."""
    out = []
    rec = root / "recordings"
    for unit in UNITS:
        d = rec / unit
        if d.is_dir():
            for p in d.iterdir():
                st = stamp_of(p.name)
                if p.is_dir() and st:
                    out.append((unit, st, p))
    g = root / "gps-logs"
    if g.is_dir():
        for p in g.glob("gps_log_*.ubx"):
            st = stamp_of(p.name)
            if st:
                out.append(("gps", st, p))
    return out


def runs(root):
    """Group sessions whose stamps fall within GROUP_WINDOW_S into runs, newest first."""
    ss = sorted(sessions(root), key=lambda s: s[1])
    groups = []
    for unit, st, p in ss:
        t = stamp_time(st)
        if groups and (t - groups[-1]["t0"]).total_seconds() <= GROUP_WINDOW_S and unit not in groups[-1]["units"]:
            groups[-1]["units"][unit] = p
            groups[-1]["stamps"][unit] = st
        else:
            groups.append({"t0": t, "stamp": st, "units": {unit: p}, "stamps": {unit: st}})
    return list(reversed(groups))


# --------------------------------------------------------------------------
# per-stream checks: each returns (verdict, one-line metrics, [notes], start_wall_us or None)
# --------------------------------------------------------------------------

def load_json(p):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return None


def check_camera(d, name):
    notes = []
    s = load_json(d / f"{name}_summary.json")
    ts = d / f"{name}_timestamps.bin"
    mj = d / f"{name}.mjpeg"
    if s is None:
        n = ts.stat().st_size // 8 if ts.exists() else 0
        mb = mj.stat().st_size / 1e6 if mj.exists() else 0
        notes.append("no summary.json: still recording, or the recorder was killed (not stopped with SIGINT)")
        if n >= 2:
            a = struct.unpack("<q", ts.read_bytes()[:8])[0]
            with ts.open("rb") as f:
                f.seek(-8, os.SEEK_END)
                b = struct.unpack("<q", f.read(8))[0]
            dur = (b - a) / 1e6
            return "WARN", f"{n} frames so far, {mb:.0f} MB, {fmt_dur(dur)}, {n / dur if dur else 0:.1f} fps", notes, None
        return "FAIL", f"{n} frames, {mb:.0f} MB", notes, None
    dur = (s["last_ts_us"] - s["first_ts_us"]) / 1e6 if s["frames"] > 1 else 0
    fps = s.get("effective_fps", 0)
    want = s["requested"]["fps"]
    verdict = "PASS"
    if s["lost"]:
        verdict = "FAIL"
        notes.append(f"{s['lost']} frames lost in {s['gaps']} gaps at seq {s.get('gap_list', '')}")
    if s["writer"]["overruns"]:
        verdict = "FAIL"
        notes.append(f"{s['writer']['overruns']} writer overruns (SD card too slow): queue peak "
                     f"{s['writer']['queue_peak_bytes'] / 1e6:.0f} MB of {s['writer']['queue_budget_bytes'] / 1e6:.0f}")
    if s["error_frames"]:
        notes.append(f"{s['error_frames']} error-flagged (incomplete) frames")
    if s.get("stalls"):
        verdict = "FAIL"
        notes.append(f"{s['stalls']} stalls")
    if fps < want * 0.97:
        verdict = "WARN" if verdict == "PASS" else verdict
        notes.append(f"effective {fps:.2f} fps vs {want} requested (dim light? check --auto-fps)")
    if s["writer"]["write_max_ms"] > 500:
        notes.append(f"longest write {s['writer']['write_max_ms']:.0f} ms (absorbed by the queue)")
    iv = s["interval_us"]
    if iv["max"] > 2.5e6 / want:
        notes.append(f"max frame interval {iv['max'] / 1000:.0f} ms (nominal {1000 / want:.1f})")
    if s["exit_code"] not in (0,):
        notes.append(f"recorder exit code {s['exit_code']}")
    mb = s["bytes"] / 1e6
    start_wall = s["first_ts_us"] + s["realtime_minus_monotonic_us"]
    line = (f"{s['frames']} frames, {fmt_dur(dur)}, {fps:.2f} fps, {mb:.0f} MB, lost {s['lost']}, "
            f"overruns {s['writer']['overruns']}, write max {s['writer']['write_max_ms']:.0f} ms  [{s['device'].split('/')[-1]}]")
    return verdict, line, notes, start_wall


def check_imu(d):
    notes = []
    s = load_json(d / "imu_summary.json")
    c = d / "imu.csv"
    if s is None:
        n = 0
        if c.exists():
            with c.open("rb") as f:
                n = max(0, sum(1 for _ in f) - 1)
        notes.append("no summary.json: still recording, or killed")
        return ("WARN" if n else "FAIL"), f"{n} samples so far", notes, None
    dur = s["samples"] / s["effective_hz"] if s.get("effective_hz") else 0
    verdict = "PASS"
    if s["lost"]:
        pct = 100.0 * s["lost"] / max(1, s["samples"] + s["lost"])
        verdict = "WARN" if pct < 0.5 else "FAIL"
        notes.append(f"{s['lost']} samples lost in {s['gaps']} gaps ({pct:.2f}%): host late polling the HID endpoint")
    if s.get("i2c_errors"):
        notes.append(f"{s['i2c_errors']} I2C errors on the module")
    if abs(s.get("device_minus_host_drift_ppm", 0)) > 200:
        notes.append(f"device clock drift {s['device_minus_host_drift_ppm']:.0f} ppm vs host")
    if abs(s["effective_hz"] - 100) > 1:
        verdict = "WARN" if verdict == "PASS" else verdict
        notes.append(f"rate {s['effective_hz']:.2f} Hz (expected 100)")
    start_wall = s["first_host_mono_us"] + s["realtime_minus_monotonic_us"]
    line = (f"{s['samples']} samples, {fmt_dur(dur)}, {s['effective_hz']:.2f} Hz, lost {s['lost']}, "
            f"interval {s['interval_us']['min']:.0f}-{s['interval_us']['max']:.0f} us, drift {s['device_minus_host_drift_ppm']:.1f} ppm")
    return verdict, line, notes, start_wall


def wav_info(p):
    """(rate, channels, bits, data_bytes, header_ok) from a RIFF/WAVE file."""
    with p.open("rb") as f:
        hdr = f.read(12)
        if len(hdr) < 12 or hdr[:4] != b"RIFF" or hdr[8:12] != b"WAVE":
            return None
        rate = ch = bits = None
        data_bytes = None
        data_off = None
        while True:
            h = f.read(8)
            if len(h) < 8:
                break
            cid, size = h[:4], struct.unpack("<I", h[4:])[0]
            if cid == b"fmt ":
                fmt = f.read(size)
                ch, rate = struct.unpack("<HI", fmt[2:8])
                bits = struct.unpack("<H", fmt[14:16])[0]
            elif cid == b"data":
                data_off = f.tell()
                data_bytes = size
                break
            else:
                f.seek(size + (size & 1), os.SEEK_CUR)
        if data_off is None:
            return None
        actual = p.stat().st_size - data_off
        header_ok = data_bytes not in (0, 0xFFFFFFFF, 0x7FFFFFFF) and abs(data_bytes - actual) <= 1
        return rate, ch, bits, actual, header_ok, data_off


def peak_dbfs(p, data_off, bits, nbytes, samples_per_slice=48000 * 2):
    """Peak over three slices (start / middle / end), in dBFS; cheap, informational."""
    import math
    width = bits // 8
    fmt = {2: "h", 4: "i"}.get(width)
    if not fmt or nbytes < width * 4:
        return None
    peak = 0
    slice_bytes = samples_per_slice * width
    with p.open("rb") as f:
        for frac in (0.0, 0.5, 1.0):
            off = data_off + int((nbytes - slice_bytes) * frac)
            off -= off % width
            f.seek(max(data_off, off))
            b = f.read(min(slice_bytes, data_off + nbytes - max(data_off, off)))
            b = b[: len(b) - len(b) % width]
            if b:
                vals = struct.unpack(f"<{len(b) // width}{fmt}", b)
                peak = max(peak, max(abs(v) for v in vals))
    full = 2 ** (bits - 1)
    return 20 * math.log10(peak / full) if peak else -999


def check_audio(d):
    notes = []
    w = d / "audio.wav"
    if not w.exists():
        return "FAIL", "no audio.wav", notes, None
    info = wav_info(w)
    if not info:
        return "FAIL", f"{w.stat().st_size} bytes, not a WAV header", notes, None
    rate, ch, bits, nbytes, header_ok, data_off = info
    dur = nbytes / (rate * ch * bits // 8) if rate else 0
    verdict = "PASS"
    if not header_ok:
        verdict = "WARN"
        notes.append("WAV header length not final: still recording, or arecord was killed before it could finish the header "
                     "(players may need `ffmpeg -i audio.wav -c copy fixed.wav`)")
    pk = peak_dbfs(w, data_off, bits, nbytes)
    if pk is not None:
        if pk < -60:
            verdict = "WARN" if verdict == "PASS" else verdict
            notes.append(f"peak {pk:.0f} dBFS: near silence, check the mics / ALSA device")
        elif pk > -0.5:
            notes.append("peak at full scale: clipping")
    line = f"{fmt_dur(dur)}, {rate} Hz, {ch} ch, {bits}-bit, {nbytes / 1e6:.0f} MB" + (f", peak {pk:.0f} dBFS (sampled)" if pk is not None else "")
    return verdict, line, notes, None


def check_fuel(d):
    notes = []
    c = d / "fuel_gauge.csv"
    if not c.exists():
        return "FAIL", "no fuel_gauge.csv", notes, None
    rows = []
    with c.open(newline="") as f:
        r = csv.reader(f)
        next(r, None)
        for row in r:
            if len(row) >= 5 and row[3] != "ERROR":
                try:
                    rows.append((float(row[0]), float(row[3]), float(row[4])))
                except ValueError:
                    pass
    if not rows:
        return "FAIL", "no readings", notes, None
    dur = rows[-1][0] - rows[0][0]
    v0, v1 = rows[0][1], rows[-1][1]
    soc0, soc1 = rows[0][2], rows[-1][2]
    verdict = "PASS"
    if v1 < 3.5:
        verdict = "WARN"
        notes.append(f"battery at {v1:.2f} V")
    if dur > 0 and len(rows) / dur < 0.3:
        notes.append(f"only {len(rows)} readings in {fmt_dur(dur)}")
    line = f"{len(rows)} readings, {fmt_dur(dur)}, {v0:.3f} -> {v1:.3f} V, SOC {soc0:.1f} -> {soc1:.1f} %"
    if dur > 600 and v0 > v1:
        line += f", {(v0 - v1) / (dur / 3600) * 1000:.0f} mV/h"
    return verdict, line, notes, int(rows[0][0] * 1e6)


def check_gps(ubx):
    """Scan the raw UBX file for NAV-PVT (class 1 id 7): count, fix types, last position."""
    notes = []
    data = ubx.read_bytes()
    n = 0
    fixes = {}
    last = None
    first_itow = last_itow = None
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
            itow = struct.unpack_from("<I", data, p)[0]
            fix = data[p + 20]
            flags = data[p + 21]
            nsv = data[p + 23]
            lon, lat, h = struct.unpack_from("<iii", data, p + 24)
            hacc = struct.unpack_from("<I", data, p + 40)[0]
            carr = (flags >> 6) & 3
            key = {0: "no fix", 2: "2D", 3: "3D", 4: "GNSS+DR", 5: "time only"}.get(fix, str(fix))
            if fix == 3 and carr:
                key = "RTK float" if carr == 1 else "RTK fixed"
            fixes[key] = fixes.get(key, 0) + 1
            n += 1
            if first_itow is None:
                first_itow = itow
            last_itow = itow
            last = (lat / 1e7, lon / 1e7, h / 1000.0, nsv, hacc / 1000.0, key)
        i = j + 6 + ln + 2
    mb = L / 1e6
    if n == 0:
        return "FAIL", f"{mb:.1f} MB, no NAV-PVT messages", ["is the GPS logger configured for UBX NAV-PVT output?"], None
    dur = ((last_itow - first_itow) % (7 * 86400 * 1000)) / 1000.0 if n > 1 else 0
    good = sum(v for k, v in fixes.items() if k not in ("no fix", "time only"))
    verdict = "PASS" if good / n > 0.9 else ("WARN" if good else "FAIL")
    if good < n:
        notes.append(f"{n - good} of {n} epochs without a position fix")
    rate = n / dur if dur else 0
    if dur and abs(rate - 5.0) > 0.5:
        notes.append(f"{rate:.2f} epochs/s (expected 5 Hz)")
    hist = ", ".join(f"{k} {v}" for k, v in sorted(fixes.items(), key=lambda kv: -kv[1]))
    line = f"{n} NAV-PVT, {fmt_dur(dur)}, {rate:.1f} Hz, {mb:.1f} MB; {hist}"
    if last and last[5] not in ("no fix", "time only"):
        notes.append(f"last: {last[0]:.6f}, {last[1]:.6f}, {last[2]:.1f} m, {last[3]} SV, hAcc {last[4]:.2f} m ({last[5]})")
    sync = ubx.with_name(ubx.name.replace(".ubx", "_timesync.csv"))
    if sync.exists():
        with sync.open("rb") as f:
            rows = max(0, sum(1 for _ in f) - 1)
        notes.append(f"{rows} time-sync rows in {sync.name}")
    return verdict, line, notes, None


# --------------------------------------------------------------------------

def report(run, root):
    worst = "PASS"
    order = {"PASS": 0, "WARN": 1, "FAIL": 2}
    starts = {}
    print(f"run {run['stamp']}  ({run['t0']:%Y-%m-%d %H:%M:%S})  root {root}")
    print()
    for unit in UNITS + ["gps"]:
        p = run["units"].get(unit)
        if p is None:
            v, line, notes, start = "MISSING", "no session for this run", [], None
        else:
            try:
                if unit in ("camera1", "camera2"):
                    v, line, notes, start = check_camera(p, unit)
                elif unit == "imu":
                    v, line, notes, start = check_imu(p)
                elif unit == "audio":
                    v, line, notes, start = check_audio(p)
                elif unit == "fuel":
                    v, line, notes, start = check_fuel(p)
                else:
                    v, line, notes, start = check_gps(p)
            except Exception as e:  # a half-written file should not hide the other streams
                v, line, notes, start = "FAIL", f"could not read: {type(e).__name__}: {e}", [], None
        if v == "MISSING":
            if unit in ("camera1", "camera2", "imu"):
                worst = "FAIL"
            elif worst == "PASS":
                worst = "WARN"
        else:
            worst = max(worst, v, key=lambda k: order[k])
        if start:
            starts[unit] = start
        where = f"  ({p.relative_to(root) if p else ''})" if p else ""
        print(f"{unit:8s} {v:7s} {line}{where}")
        for nt in notes:
            print(f"{'':8s} {'':7s} - {nt}")
    if len(starts) > 1:
        ref = min(starts.values())
        print()
        print("start offsets (wall clock, from the earliest stream): " +
              ", ".join(f"{u} +{(t - ref) / 1e6:.2f} s" for u, t in sorted(starts.items(), key=lambda kv: kv[1])))
        print("(camera timestamps are CLOCK_MONOTONIC; summary.json's realtime_minus_monotonic_us maps them to wall time)")
    print()
    print(f"overall: {worst}")
    return worst


def collect(run, dest):
    """Lay the run out the way dualcam/play_with_timestamps.py expects:
    <dest>/camera/camera{1,2}.mjpeg + _timestamps.bin, <dest>/audio_<stamp>.wav."""
    dest = Path(dest)
    (dest / "camera").mkdir(parents=True, exist_ok=True)

    def link(src, dst):
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        try:
            os.link(src, dst)
        except OSError:
            os.symlink(src.resolve(), dst)

    n = 0
    for cam in ("camera1", "camera2"):
        d = run["units"].get(cam)
        if not d:
            print(f"{cam}: missing")
            continue
        for suffix in (".mjpeg", "_timestamps.bin", "_index.bin", "_summary.json"):
            src = d / f"{cam}{suffix}"
            if src.exists():
                link(src, dest / "camera" / src.name)
                n += 1
    a = run["units"].get("audio")
    if a and (a / "audio.wav").exists():
        link(a / "audio.wav", dest / f"audio_{run['stamps']['audio']}.wav")
        n += 1
    for unit in ("imu", "fuel"):
        d = run["units"].get(unit)
        if d:
            for src in d.iterdir():
                link(src, dest / src.name)
                n += 1
    g = run["units"].get("gps")
    if g:
        link(g, dest / g.name)
        sync = g.with_name(g.name.replace(".ubx", "_timesync.csv"))
        if sync.exists():
            link(sync, dest / sync.name)
        n += 1
    print(f"{n} files linked under {dest}")
    print(f"play:   python3 Firmware/dualcam/play_with_timestamps.py --dir {dest}")
    print(f"export: python3 Firmware/dualcam/play_with_timestamps.py --dir {dest} --export {dest}/dual.mp4")
    print("(play_with_timestamps.py needs opencv-python and numpy, so run it on a workstation after rsync -L of this directory)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=os.environ.get("EWEGO_DIR", "/opt/ewego"), help="EweGo data root (default /opt/ewego)")
    ap.add_argument("--run", help="stamp prefix of the run to check (default: the latest)")
    ap.add_argument("--list", action="store_true", help="list runs and exit")
    ap.add_argument("--collect", metavar="DIR", help="link the run's files into DIR for play_with_timestamps.py")
    ap.add_argument("--json", action="store_true", help="print the run's file list as JSON (for scripts)")
    a = ap.parse_args()
    root = Path(a.root).expanduser()
    rs = runs(root)
    if not rs:
        print(f"no recordings under {root}")
        return 1
    if a.list:
        for r in rs:
            print(f"{r['stamp']}  {', '.join(sorted(r['units']))}")
        return 0
    run = rs[0]
    if a.run:
        m = [r for r in rs if any(st.startswith(a.run) for st in r["stamps"].values())]
        if not m:
            print(f"no run matching {a.run}; --list shows them")
            return 1
        run = m[0]
    if a.json:
        print(json.dumps({"stamp": run["stamp"], "files": {u: str(p) for u, p in run["units"].items()}}, indent=1))
        return 0
    worst = report(run, root)
    if a.collect:
        print()
        collect(run, a.collect)
    return {"PASS": 0, "WARN": 0, "FAIL": 2}[worst]


if __name__ == "__main__":
    sys.exit(main())
