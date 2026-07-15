#!/usr/bin/env python3
"""
Aligned multi-stream export for EweGo recording sessions.

Composites cam1, cam2, an IMU line chart, and a battery/info panel into a
single MP4 with audio muxed. Every stream in a session dir carries a
monotonic_us stamp (all from CLOCK_MONOTONIC), so alignment is a direct
lookup — no wall-clock, no interpolation across devices, no ambiguity.

Default output layout (1920x1080):

    +------------- 960 ---------------+------------- 960 --------------+
    |                                 |                                |
    |          cam1 (top-left)        |         cam2 (top-right)       |
    |             ~960x540            |            ~960x540            |
    |                                 |                                |
    +---------------------------------+--------------------------------+
    |  IMU: roll / pitch / heading    |  Battery + orientation + time  |
    |       last ~10 s rolling        |  SOC %, Voltage V, Temp C,     |
    |       960x540 chart             |  IMU calibration flags         |
    +---------------------------------+--------------------------------+

Reuses `load_timestamps`, `find_video_file`, `find_audio_file`, and
`ensure_seekable` from play_with_timestamps.py in this same dir.

Usage:
    uv run python Firmware/dualcam/align_streams.py --dir /path/to/session
        [--output aligned.mp4] [--no-flip] [--no-swap] [--no-audio]

Multi-device mode: pass --dir more than once (one session dir per device).
Sessions are aligned on wall-clock time via each session's clock_sync.csv
(monotonic_us -> chrony wall time, 100 Hz). Output is one row per device:
cam1 | cam2 | IMU chart, rendered over the overlapping wall-time window,
with all devices' microphones mixed into a single audio track.

    uv run python Firmware/dualcam/align_streams.py \
        --dir .../Ewego001/sensor_test_X --dir .../Ewego007/sensor_test_Y \
        [--start 60] [--duration 120] [--fps 30] [--output multi.mp4]
"""
import argparse
import csv
import os
import re
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

# Reuse loaders from sibling player.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from play_with_timestamps import (  # noqa: E402
    ensure_seekable,
    find_audio_file,
    find_video_file,
    load_timestamps,
    run_ffmpeg_with_progress,
)


# ---------------------------------------------------------------------------
# Sensor-CSV loaders. Every stream stamps monotonic_us in column 0, so a
# single generic loader works — callers pull whichever columns they want.
# ---------------------------------------------------------------------------
def load_csv_columns(csv_path, columns):
    """Load specific columns from a CSV, keyed by header name.

    Returns dict{name: numpy.ndarray of float64} plus 'monotonic_us' as int64.
    Missing columns become empty arrays; missing file returns None.
    """
    if not csv_path or not os.path.exists(csv_path):
        return None
    with open(csv_path) as f:
        r = csv.reader(f)
        header = next(r)
        idx = {name: header.index(name) for name in columns if name in header}
        # monotonic_us is always column 0 by our writer conventions.
        assert header[0] == "monotonic_us", f"expected monotonic_us as col 0 in {csv_path}"
        mono = []
        cols = {name: [] for name in idx}
        for row in r:
            try:
                m = int(row[0])
                vals = {name: float(row[ci]) for name, ci in idx.items()}
            except (ValueError, IndexError):
                # Skip malformed rows (e.g., truncated NUL tails from crash).
                continue
            mono.append(m)
            for name, v in vals.items():
                cols[name].append(v)
    return {
        "monotonic_us": np.asarray(mono, dtype=np.int64),
        **{name: np.asarray(v, dtype=np.float64) for name, v in cols.items()},
    }


def find_imu_csv(rec_dir):
    """Pick the newest imu_log_*.csv; there can be multiple across restarts."""
    matches = sorted(Path(rec_dir).glob("imu/logs/imu_log_*.csv"),
                     key=lambda p: p.stat().st_mtime)
    return str(matches[-1]) if matches else None


def find_fuel_csv(rec_dir):
    matches = sorted(Path(rec_dir).glob("fuel_gauge_*.csv"),
                     key=lambda p: p.stat().st_mtime)
    return str(matches[-1]) if matches else None


def nearest_index(sorted_mono, t_us):
    """Return index into `sorted_mono` whose value is closest to t_us.

    Uses numpy searchsorted for O(log N). Handles empty arrays by returning
    None so callers can decide whether to draw a placeholder.
    """
    if len(sorted_mono) == 0:
        return None
    i = int(np.searchsorted(sorted_mono, t_us))
    if i == 0:
        return 0
    if i >= len(sorted_mono):
        return len(sorted_mono) - 1
    return i if (sorted_mono[i] - t_us) < (t_us - sorted_mono[i - 1]) else i - 1


# ---------------------------------------------------------------------------
# Panel renderers. Each returns a BGR ndarray of shape (h, w, 3), uint8.
# Kept in pure OpenCV (no matplotlib) for speed — we redraw every frame.
# ---------------------------------------------------------------------------
def render_imu_chart(imu, t_us, w=960, h=540, window_s=10.0):
    """Rolling line chart of heading/roll/pitch anchored to t_us.

    Nominal window: [t_us - window_s, t_us + 0.5s]. Near t=0 the past
    portion is empty, so we shift the window forward to keep chart width
    populated (avoids a big "IMU window empty" placeholder at session
    start). "Now" indicator line always sits at wherever t_us falls
    within the drawn window. Three colored lines. Falls back to a blank
    panel with a placeholder label if imu is None or truly empty.
    """
    img = np.zeros((h, w, 3), dtype=np.uint8)
    # Faint grid
    for gx in range(0, w, w // 10):
        cv2.line(img, (gx, 0), (gx, h), (32, 32, 32), 1)
    for gy in range(0, h, h // 8):
        cv2.line(img, (0, gy), (w, gy), (32, 32, 32), 1)

    if imu is None or len(imu["monotonic_us"]) == 0:
        cv2.putText(img, "No IMU data", (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (128, 128, 128), 2)
        return img

    mono = imu["monotonic_us"]
    window_us = int(window_s * 1e6)
    lookahead_us = int(0.5 * 1e6)
    t_start = t_us - window_us
    t_end = t_us + lookahead_us
    # Near session start there are no past samples yet. Shift the window
    # forward so the chart shows the initial N seconds instead of a blank.
    if t_start < mono[0]:
        t_start = mono[0]
        t_end = min(mono[-1], t_start + window_us + lookahead_us)
    # Same clamp at end of session.
    if t_end > mono[-1]:
        t_end = mono[-1]
        t_start = max(mono[0], t_end - window_us - lookahead_us)
    lo = int(np.searchsorted(mono, t_start))
    hi = int(np.searchsorted(mono, t_end))
    if hi - lo < 2:
        cv2.putText(img, "IMU window empty", (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (128, 128, 128), 2)
        return img

    # Signals to plot: heading (0..360), roll (-180..180), pitch (-90..90)
    # Normalize each to a symmetric ±180 range so a single Y scale works.
    def norm_heading(h_):
        # Fold [0,360) into [-180,180)
        return ((h_ + 180.0) % 360.0) - 180.0

    signals = [
        ("Heading", norm_heading(imu.get("heading_deg", np.zeros_like(mono, dtype=float))[lo:hi]), (255, 128, 0)),
        ("Roll", imu.get("roll_deg", np.zeros_like(mono, dtype=float))[lo:hi], (0, 255, 128)),
        ("Pitch", imu.get("pitch_deg", np.zeros_like(mono, dtype=float))[lo:hi], (0, 128, 255)),
    ]
    win_lo_us = mono[lo]
    win_hi_us = mono[hi - 1]
    win_span_us = max(1, win_hi_us - win_lo_us)
    xs = mono[lo:hi]

    def y_for(v):
        # v in [-180, 180] → [h-margin, margin]
        vc = max(-180.0, min(180.0, v))
        return int(margin_t + (h - margin_t - margin_b) * (1 - (vc + 180.0) / 360.0))

    margin_t, margin_b = 30, 30

    # Y-axis reference lines at -90, 0, +90 degrees
    for ref in (-90, 0, 90):
        y = y_for(ref)
        cv2.line(img, (0, y), (w, y), (64, 64, 64), 1)
        cv2.putText(img, f"{ref:+d}", (w - 60, y - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (128, 128, 128), 1)

    # "Now" indicator
    now_x = int((t_us - win_lo_us) / win_span_us * w)
    cv2.line(img, (now_x, 0), (now_x, h), (200, 200, 0), 1)

    # Plot each signal
    for name, series, color in signals:
        if len(series) < 2:
            continue
        pts = []
        for i in range(len(series)):
            x = int((xs[i] - win_lo_us) / win_span_us * w)
            pts.append((x, y_for(float(series[i]))))
        if len(pts) >= 2:
            cv2.polylines(img, [np.array(pts, dtype=np.int32)],
                          isClosed=False, color=color, thickness=2, lineType=cv2.LINE_AA)

    # Legend
    for i, (name, _, color) in enumerate(signals):
        cv2.putText(img, name, (10 + i * 120, 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)

    # Time-axis label
    cv2.putText(img, f"IMU  last {window_s:.0f}s",
                (w - 200, h - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (160, 160, 160), 1, cv2.LINE_AA)
    return img


def render_info_panel(fuel, imu, t_rel_s, t_us, w=960, h=540):
    """Text panel: SOC, voltage, orientation, temp, calibration, timestamp."""
    img = np.zeros((h, w, 3), dtype=np.uint8)
    cv2.rectangle(img, (0, 0), (w - 1, h - 1), (48, 48, 48), 1)

    row_h = 55
    y = 60

    def put(text, color=(220, 220, 220), scale=1.1, thick=2):
        nonlocal y
        cv2.putText(img, text, (30, y),
                    cv2.FONT_HERSHEY_SIMPLEX, scale, color, thick, cv2.LINE_AA)
        y += row_h

    # Timestamp — big
    mins = int(t_rel_s // 60)
    secs = t_rel_s % 60
    put(f"Time  {mins:02d}:{secs:05.2f}", (0, 255, 0), scale=1.5, thick=3)

    # Battery
    if fuel is not None and len(fuel["monotonic_us"]) > 0:
        fi = nearest_index(fuel["monotonic_us"], t_us)
        soc = fuel.get("SOC_Percent")
        volt = fuel.get("Voltage_V")
        put(f"SOC   {soc[fi]:5.1f}%" if soc is not None and len(soc) else "SOC   n/a",
            (255, 200, 0))
        put(f"Volt  {volt[fi]:5.3f} V" if volt is not None and len(volt) else "Volt  n/a",
            (255, 200, 0))
    else:
        put("Battery: no data", (128, 128, 128))
        put("", (128, 128, 128))

    # IMU orientation + temp + calibration
    if imu is not None and len(imu["monotonic_us"]) > 0:
        ii = nearest_index(imu["monotonic_us"], t_us)

        def val(name, default="n/a"):
            arr = imu.get(name)
            return f"{arr[ii]:6.1f}" if arr is not None and len(arr) else default

        # cv2.putText's FONT_HERSHEY glyphs are ASCII-only; the ° symbol
        # renders as `??`. Use " deg" as a plain-text stand-in.
        put(f"Head  {val('heading_deg')} deg", (255, 128, 0))
        put(f"Roll  {val('roll_deg')} deg", (0, 255, 128))
        put(f"Pitch {val('pitch_deg')} deg", (0, 128, 255))

        # Calibration (0..3 per axis in BNO055)
        cal_sys = int(imu.get("cal_sys", [0])[ii]) if len(imu.get("cal_sys", [])) else 0
        cal_g = int(imu.get("cal_gyro", [0])[ii]) if len(imu.get("cal_gyro", [])) else 0
        cal_a = int(imu.get("cal_accel", [0])[ii]) if len(imu.get("cal_accel", [])) else 0
        cal_m = int(imu.get("cal_mag", [0])[ii]) if len(imu.get("cal_mag", [])) else 0
        temp = imu.get("temp_c")
        put(f"Temp  {temp[ii]:4.1f} C" if temp is not None and len(temp) else "Temp  n/a",
            (200, 200, 255))
        cal_color = (0, 255, 0) if cal_sys == 3 else (0, 200, 200) if cal_sys >= 1 else (128, 128, 128)
        put(f"Cal   sys{cal_sys} g{cal_g} a{cal_a} m{cal_m}", cal_color, scale=0.9, thick=2)
    else:
        put("IMU: no data", (128, 128, 128))
    return img


def _audio_envelope(wav_path, target_fs=100.0):
    """Downsampled |amplitude| envelope of a WAV, normalized to [0, 1]."""
    import wave as _wave
    try:
        w = _wave.open(wav_path, "rb")
    except (OSError, EOFError):
        return None, 0.0
    with w:
        fs, nch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        dtype = {2: np.int16, 4: np.int32}.get(sw)
        if dtype is None:
            return None, 0.0
        dec = max(1, int(round(fs / target_fs)))
        chunks = []
        while True:
            raw = w.readframes(fs * 30)
            if not raw:
                break
            x = np.frombuffer(raw, dtype).astype(np.float32)
            if nch > 1:
                x = x[:len(x) // nch * nch].reshape(-1, nch).mean(axis=1)
            x = np.abs(x - x.mean())  # kill per-chunk DC offset
            n = len(x) // dec * dec
            if n:
                chunks.append(x[:n].reshape(-1, dec).mean(axis=1))
    if not chunks:
        return None, 0.0
    env = np.concatenate(chunks)
    # Plain amplitude normalized to the 99.5th percentile: preserves the
    # linear quiet-vs-loud relationship. A visual gain is applied later at
    # drawing time so quiet-but-present audio (breath, ambient) is visible
    # without the wall-of-black that log-compression produces.
    env /= max(float(np.percentile(env, 99.5)), 1e-9)
    return np.clip(env, 0.0, 1.0), fs / dec


def render_sensor_panel(dev, t_us, wall_t, w=480, h=300, window_s=10.0):
    """Per-device panel on a white background: compass, gravity bubble,
    linear-accel xyz vectors, and a scrolling audio amplitude trace with a
    centered playhead."""
    img = np.full((h, w, 3), 255, dtype=np.uint8)
    third = w // 3
    # Bottom-up layout: reserve audio strip and label row first, then let
    # the dial band take whatever's left. This guarantees the labels have
    # descender room and never bleed into the audio strip.
    status_h = 18                   # top strip for "EwegoNNN 56% 3.77V"
    label_area_h = 20               # baseline text height + descender + margin
    audio_h = max(50, (h - status_h - label_area_h) // 2)
    dial_band_h = max(20, h - status_h - label_area_h - audio_h)
    r = max(10, min(third // 2 - 10, dial_band_h // 2 - 2))
    cy_dial = status_h + dial_band_h // 2
    label_y = status_h + dial_band_h + 12   # baseline
    audio_top = h - audio_h
    GRAY = (140, 140, 140)
    DARK = (60, 60, 60)
    FONT = cv2.FONT_HERSHEY_SIMPLEX

    imu = dev.get("imu")
    ii = None
    if imu is not None and len(imu["monotonic_us"]):
        ii = int(nearest_index(imu["monotonic_us"], t_us))

    def val(col):
        arr = imu.get(col) if imu is not None else None
        if ii is None or arr is None or len(arr) <= ii:
            return None
        return float(arr[ii])

    def center(i):
        return i * third + third // 2, cy_dial

    # Compass: heading 0 = N (up), clockwise.
    cx, cy = center(0)
    cv2.circle(img, (cx, cy), r, GRAY, 1, cv2.LINE_AA)
    for ang, lab in ((0, "N"), (90, "E"), (180, "S"), (270, "W")):
        a = np.deg2rad(ang)
        lx = cx + int((r - 9) * np.sin(a))
        ly = cy - int((r - 9) * np.cos(a))
        cv2.putText(img, lab, (lx - 5, ly + 4), FONT, 0.35,
                    (110, 110, 110), 1, cv2.LINE_AA)
    hd = val("heading_deg")
    if hd is not None:
        a = np.deg2rad(hd)
        tip = (cx + int((r - 18) * np.sin(a)), cy - int((r - 18) * np.cos(a)))
        cv2.arrowedLine(img, (cx, cy), tip, (0, 0, 0), 2, cv2.LINE_AA,
                        tipLength=0.25)

    # Gravity bubble: dot centered = upright; moves toward the tilt.
    cx, cy = center(1)
    cv2.circle(img, (cx, cy), r, GRAY, 1, cv2.LINE_AA)
    cv2.line(img, (cx - r, cy), (cx + r, cy), (200, 200, 200), 1)
    cv2.line(img, (cx, cy - r), (cx, cy + r), (200, 200, 200), 1)
    gx, gy, gz = val("gravity_x"), val("gravity_y"), val("gravity_z")
    if None not in (gx, gy, gz):
        s = r / 9.81
        p = (cx + int(np.clip(gx * s, -r, r)), cy - int(np.clip(gy * s, -r, r)))
        cv2.line(img, (cx, cy), p, (170, 0, 170), 1, cv2.LINE_AA)
        cv2.circle(img, p, 5, (170, 0, 170), -1, cv2.LINE_AA)

    # Linear acceleration (gravity removed): arrows scale with each axis.
    cx, cy = center(2)
    A_FS = 8.0  # m/s^2 at full arrow length
    axes = (((1.0, 0.0), (0, 0, 200)),      # x right, red   (BGR)
            ((0.0, -1.0), (0, 150, 0)),     # y up, green
            ((-0.66, 0.55), (180, 80, 0)))  # z depth diag, blue-orange
    for d, _ in axes:
        cv2.line(img, (cx - int(d[0] * r), cy - int(d[1] * r)),
                 (cx + int(d[0] * r), cy + int(d[1] * r)), (200, 200, 200), 1)
    acc = [val(f"lin_accel_{k}") for k in ("x", "y", "z")]
    mag = None
    if None not in acc:
        for (d, color), a in zip(axes, acc):
            L = float(np.clip(a / A_FS, -1.0, 1.0)) * (r - 4)
            tip = (cx + int(d[0] * L), cy + int(d[1] * L))
            if tip != (cx, cy):
                cv2.arrowedLine(img, (cx, cy), tip, color, 2, cv2.LINE_AA,
                                tipLength=0.3)
        mag = float(np.linalg.norm(acc))
    if imu is None:
        cv2.putText(img, "IMU: n/a", (center(0)[0] - 30, cy_dial), FONT, 0.45,
                    (140, 140, 140), 1, cv2.LINE_AA)

    # Combined label + numeric readout under each dial. Keeps numerics out of
    # the compass's top edge where the status text sits.
    readouts = [
        ("heading", f"{hd:.0f}" if hd is not None else "-"),
        ("gravity", f"gz{gz:+.1f}" if None not in (gx, gy, gz) else "-"),
        ("lin accel", f"{mag:.1f}m/s2" if mag is not None else "-"),
    ]
    for i, (lab, ro) in enumerate(readouts):
        text = f"{lab}  {ro}"
        (tw, _), _ = cv2.getTextSize(text, FONT, 0.4, 1)
        cv2.putText(img, text, (i * third + (third - tw) // 2, label_y),
                    FONT, 0.4, DARK, 1, cv2.LINE_AA)

    # Audio amplitude trace, playhead centered.
    y0 = audio_top
    img[y0:h] = 255  # pure white audio background
    env, efs = dev.get("audio_env"), dev.get("audio_env_fs", 0.0)
    if env is not None and efs > 0 and dev.get("audio_wall0") is not None:
        tt = wall_t - window_s / 2 + np.arange(w) * (window_s / w)
        if dev.get("audio_idx") is not None:
            samp = np.interp(tt, dev["audio_wall"], dev["audio_idx"],
                             left=-1.0, right=-1.0)
            idx = (samp * efs / dev["audio_fs"]).astype(np.int64)
            idx[samp < 0] = -1
        else:
            idx = ((tt - dev["audio_wall0"]) * efs).astype(np.int64)
        ok = (idx >= 0) & (idx < len(env))
        amp = np.zeros(w, dtype=np.float32)
        amp[ok] = env[idx[ok]]
        hpx = (amp * (audio_h // 2 - 3)).astype(np.int32)
        rows = np.arange(audio_h)[:, None]
        mask = np.abs(rows - audio_h // 2) <= hpx[None, :]
        img[y0:y0 + audio_h][mask] = (30, 30, 30)
        cv2.line(img, (w // 2, y0), (w // 2, h - 1), (0, 0, 200), 1)
    else:
        cv2.putText(img, "no audio", (10, y0 + audio_h // 2 + 4), FONT, 0.45,
                    (140, 140, 140), 1, cv2.LINE_AA)
    return img


# ---------------------------------------------------------------------------
# Multi-device support: wall-clock alignment via clock_sync.csv
# ---------------------------------------------------------------------------
def load_clock_sync(rec_dir):
    """Load clock_sync.csv → (mono_us int64 array, wall_s float64 array).

    mono is strictly increasing (CLOCK_MONOTONIC). wall can step forward or
    backward when chrony corrects the clock, so callers that need to search
    by wall time should use a monotonicized copy (np.maximum.accumulate).
    """
    data = load_csv_columns(str(Path(rec_dir) / "clock_sync.csv"), ["wall_time_s"])
    if data is None or len(data["monotonic_us"]) < 2:
        return None
    return data["monotonic_us"], data["wall_time_s"]


def load_imu_concat(rec_dir, columns):
    """Load and concatenate ALL imu_log_*.csv files (IMU restarts split logs)."""
    parts = []
    for p in sorted(Path(rec_dir).glob("imu/logs/imu_log_*.csv")):
        d = load_csv_columns(str(p), columns)
        if d is not None and len(d["monotonic_us"]):
            parts.append(d)
    if not parts:
        return None
    mono = np.concatenate([p["monotonic_us"] for p in parts])
    order = np.argsort(mono, kind="stable")
    out = {"monotonic_us": mono[order]}
    for name in columns:
        col = np.concatenate([
            p.get(name, np.zeros(len(p["monotonic_us"]), dtype=np.float64))
            for p in parts
        ])
        out[name] = col[order]
    return out


class CamReader:
    """Sequential-friendly frame access into a seekable MP4.

    Nearest-frame lookups from a uniform output timeline advance mostly
    one frame at a time; repeats reuse the cached frame, small jumps read
    forward, and anything else falls back to a seek.
    """

    def __init__(self, path):
        self.cap = cv2.VideoCapture(path)
        self.idx = -1
        self.frame = None

    def ok(self):
        return self.cap.isOpened()

    def get(self, target):
        if target == self.idx:
            return self.frame
        if target < self.idx or target > self.idx + 8:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, target)
            self.idx = target - 1
        while self.idx < target:
            r, f = self.cap.read()
            if not r:
                break
            self.idx += 1
            self.frame = f
        return self.frame

    def release(self):
        self.cap.release()


def _device_label(rec_dir):
    m = re.search(r"[Ee]wego\d+", str(rec_dir))
    return m.group(0) if m else Path(rec_dir).parent.name


def rectify_clock_sync(cs_mono, cs_wall, label=""):
    """Remove chrony step corrections from the mono→wall mapping.

    Chrony steps the wall clock mid-session (observed up to ±364 s). Video
    lookups interpolate through the raw mapping and follow every step, but a
    continuous stream like audio can only be placed with one constant offset —
    so steps desync A/V and cause cross-device video jumps. Measured crystal
    drift is ~0 ppm, so the true mapping is affine: rebuild it anchored at the
    LAST clock_sync sample (chrony has settled by then), with slope estimated
    from the step-free residuals.
    """
    dm = np.diff(cs_mono) / 1e6
    dw = np.diff(cs_wall)
    resid = dw - dm
    steps = np.abs(resid) > 0.05  # >50 ms = chrony step, not jitter
    if not steps.any():
        return cs_wall, False
    good = ~steps
    slope = 1.0 + (resid[good].sum() / dm[good].sum() if dm[good].sum() > 0 else 0.0)
    rect = cs_wall[-1] - (cs_mono[-1] - cs_mono) / 1e6 * slope
    net = rect[0] - cs_wall[0]
    print(f"  {label}: rectified {steps.sum()} clock step(s) "
          f"(net {net:+.2f} s at session start, drift {(slope - 1) * 1e6:+.1f} ppm)")
    return rect, True


def _camera_wall_stamps(ts, ts_path, cs_mono, cs_wall):
    """Camera frame stamps → wall time.

    New-format bins ("EWEGOTS2") carry absolute CLOCK_MONOTONIC µs, which map
    through clock_sync exactly. Old-format bins (Michigan Apr 2026 firmware)
    are encoder-relative starting at 0; for those, anchor the LAST frame at
    the .bin file's mtime — the recorder writes it unbuffered, so mtime is
    the wall time of the final frame (accurate to ~±1 s after rsync).
    """
    ts_arr = np.asarray(ts, dtype=np.int64)
    margin = 60_000_000  # 60 s
    if cs_mono[0] - margin <= ts_arr[0] and ts_arr[-1] <= cs_mono[-1] + margin:
        return np.maximum.accumulate(np.interp(ts_arr, cs_mono, cs_wall)), True
    mtime = Path(ts_path).stat().st_mtime
    return mtime - (ts_arr[-1] - ts_arr) / 1e6, False


def _load_device(rec_dir, wall_offset=0.0):
    """Load everything needed to render one device's row. Returns dict or None."""
    cs = load_clock_sync(rec_dir)
    if cs is None:
        print(f"Error: no usable clock_sync.csv in {rec_dir}")
        return None
    cs_mono, cs_wall_raw = cs
    label = _device_label(rec_dir)
    cs_wall, _ = rectify_clock_sync(cs_mono, cs_wall_raw, label)
    if wall_offset:
        print(f"  {label}: applying manual wall offset {wall_offset:+.3f} s")
        cs_wall = cs_wall + wall_offset
        cs_wall_raw = cs_wall_raw + wall_offset
    cs_wall_mon = np.maximum.accumulate(cs_wall)

    cam_dir = Path(rec_dir) / "camera"
    video1, fmt1 = find_video_file(1, str(cam_dir))
    video2, fmt2 = find_video_file(2, str(cam_dir))
    ts_path1 = cam_dir / "camera1_timestamps.bin"
    ts_path2 = cam_dir / "camera2_timestamps.bin"
    if not (video1 and video2 and ts_path1.exists() and ts_path2.exists()):
        print(f"Error: missing cameras/timestamps in {rec_dir}")
        return None
    ts1 = load_timestamps(str(ts_path1))
    ts2 = load_timestamps(str(ts_path2))
    if not ts1 or not ts2:
        print(f"Error: empty camera timestamps in {rec_dir}")
        return None
    seek1, n1 = ensure_seekable(video1, ts1, fmt1)
    seek2, n2 = ensure_seekable(video2, ts2, fmt2)
    if not seek1 or not seek2:
        return None
    ts1, ts2 = np.asarray(ts1[:n1], dtype=np.int64), np.asarray(ts2[:n2], dtype=np.int64)

    ws1, exact1 = _camera_wall_stamps(ts1, ts_path1, cs_mono, cs_wall)
    ws2, exact2 = _camera_wall_stamps(ts2, ts_path2, cs_mono, cs_wall)
    if not (exact1 and exact2):
        print(f"  WARNING: {_device_label(rec_dir)} has old-format (relative) "
              f"camera timestamps; anchored via file mtime (~±1 s accuracy)")

    imu = load_imu_concat(rec_dir, [
        "heading_deg", "roll_deg", "pitch_deg",
        "lin_accel_x", "lin_accel_y", "lin_accel_z",
        "gravity_x", "gravity_y", "gravity_z",
        "temp_c", "cal_sys", "cal_gyro", "cal_accel", "cal_mag",
    ])
    fuel = load_csv_columns(find_fuel_csv(rec_dir), ["Voltage_V", "SOC_Percent"])

    audio = find_audio_file(rec_dir)
    audio_wall0 = None
    audio_wall = audio_idx = None
    audio_fs, audio_rate = 48000, 48000.0
    if audio:
        audio_ts_csv = audio.rsplit(".", 1)[0] + ".timestamps.csv"
        if os.path.exists(audio_ts_csv):
            ats = load_csv_columns(audio_ts_csv, ["sample_index"])
            if ats is not None and len(ats["monotonic_us"]):
                audio_wall0 = float(np.interp(
                    ats["monotonic_us"][0], cs_mono, cs_wall))
                sidx = ats.get("sample_index")
                if (sidx is not None and len(sidx) >= 2
                        and len(sidx) == len(ats["monotonic_us"])):
                    audio_wall = np.maximum.accumulate(
                        np.interp(ats["monotonic_us"], cs_mono, cs_wall))
                    audio_idx = sidx
                    # Measured ADC rate (samples per wall second); mic crystals
                    # run up to ~700 ppm off nominal, which is >1 s A/V drift
                    # over a 30 min session if uncorrected.
                    audio_rate = float(np.polyfit(
                        audio_wall - audio_wall[0], audio_idx, 1)[0])
        if audio_wall0 is None:
            audio = None  # can't place it on the wall timeline

    audio_env, audio_env_fs = (None, 0.0)
    if audio:
        import wave as _wave
        with _wave.open(audio, "rb") as _w:
            audio_fs = _w.getframerate()
        audio_env, audio_env_fs = _audio_envelope(audio)
        if audio_idx is not None:
            print(f"  {label}: audio ADC rate {audio_rate:.2f} Hz "
                  f"({(audio_rate / audio_fs - 1) * 1e6:+.0f} ppm)")

    r1, r2 = CamReader(seek1), CamReader(seek2)
    if not (r1.ok() and r2.ok()):
        print(f"Error opening video captures for {rec_dir}")
        return None

    return dict(
        dir=rec_dir, label=label,
        cs_mono=cs_mono, cs_wall=cs_wall, cs_wall_mon=cs_wall_mon,
        cs_wall_raw=cs_wall_raw, exact=(exact1 and exact2),
        ws1=ws1, ws2=ws2, r1=r1, r2=r2,
        imu=imu, fuel=fuel, audio=audio, audio_wall0=audio_wall0,
        audio_wall=audio_wall, audio_idx=audio_idx,
        audio_fs=audio_fs, audio_rate=audio_rate,
        audio_env=audio_env, audio_env_fs=audio_env_fs,
    )


def _shift_device(dev, c):
    """Shift every wall-time array of a device by c seconds."""
    for key in ("cs_wall", "cs_wall_mon", "ws1", "ws2", "audio_wall"):
        if dev.get(key) is not None:
            dev[key] = dev[key] + c
    if dev.get("audio_wall0") is not None:
        dev["audio_wall0"] += c


def _fit_letterbox(frame, tw, th):
    """Fit frame into (tw, th) preserving source aspect ratio; pad with white
    so bars blend into the composite's white background."""
    h, w = frame.shape[:2]
    scale = min(tw / w, th / h)
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
    out = np.full((th, tw, 3), 255, dtype=np.uint8)
    x0 = (tw - nw) // 2
    y0 = (th - nh) // 2
    out[y0:y0 + nh, x0:x0 + nw] = resized
    return out


def _fit_cover(frame, tw, th):
    """Fit frame into (tw, th) preserving aspect by cropping the excess dimension.
    No padding — the tile is fully filled, at the cost of losing the top/bottom
    or left/right slivers of the source when aspect ratios differ."""
    h, w = frame.shape[:2]
    scale = max(tw / w, th / h)
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
    x0 = (nw - tw) // 2
    y0 = (nh - th) // 2
    return resized[y0:y0 + th, x0:x0 + tw]


def make_multi_aligned_video(rec_dirs, output_file, flip=True, swap=True,
                             include_audio=True, out_w=1920, out_h=1080,
                             fps=30.0, start_s=0.0, duration_s=None,
                             wall_offsets=None):
    """Render an N-device composite over the overlapping wall-time window.

    Layout: one row per device — cam1 | cam2 | IMU chart. A header bar shows
    UTC wall time + elapsed time. Audio is an amix of every device's mic,
    each delayed/trimmed so it sits at its true wall-time position.
    """
    wall_offsets = {k.lower(): v for k, v in (wall_offsets or {}).items()}
    devices = []
    for d in rec_dirs:
        print(f"Loading {d} ...")
        dev = _load_device(d, wall_offsets.get(_device_label(d).lower(), 0.0))
        if dev is None:
            return 1
        devices.append(dev)

    # Overlap window in wall time (both cams of every device must cover it).
    w0 = max(max(dev["ws1"][0], dev["ws2"][0]) for dev in devices)
    w1 = min(min(dev["ws1"][-1], dev["ws2"][-1]) for dev in devices)
    if w1 - w0 <= 0:
        print(f"Error: sessions do not overlap in wall time "
              f"(gap of {w0 - w1:.1f}s)")
        return 1

    # The devices sync to each other over the mesh (chrony orphan mode) and
    # are mutually ms-accurate in "flock time" even when collectively wrong
    # in absolute time. A device that recorded past a late fleet-wide
    # absolute correction (e.g. the field laptop joining the mesh with
    # internet time) gets rectified into TRUE time while devices that
    # stopped earlier stay in FLOCK time — desyncing the composite. So:
    # re-anchor each device to its clock regime at the window centre (flock
    # time, mutually consistent), then shift the whole composite by the
    # median observed late correction so displayed UTC stays honest.
    wc = (w0 + w1) / 2.0
    corr = []
    for dev in devices:
        if not dev["exact"]:
            continue  # mtime-anchored timestamps are not in flock time
        k = int(nearest_index(dev["cs_wall_mon"], wc))
        c = float(dev["cs_wall_raw"][k] - dev["cs_wall"][k])
        if abs(c) > 1e-6:
            _shift_device(dev, c)
            print(f"  {dev['label']}: re-anchored to window clock regime "
                  f"({c:+.2f} s)")
        if abs(c) > 0.2:
            corr.append(-c)
    if corr:
        g = float(np.median(corr))
        for dev in devices:
            if dev["exact"]:
                _shift_device(dev, g)
        print(f"  global UTC correction {g:+.2f} s (median late fleet step)")
    w0 = max(max(dev["ws1"][0], dev["ws2"][0]) for dev in devices)
    w1 = min(min(dev["ws1"][-1], dev["ws2"][-1]) for dev in devices)
    print(f"Overlap: {w1 - w0:.1f}s of common wall time")

    w0f = w0 + max(0.0, start_s)
    w1f = min(w1, w0f + duration_s) if duration_s else w1
    if w1f - w0f <= 0:
        print("Error: --start/--duration leave an empty window")
        return 1

    n_dev = len(devices)
    header_h = 48
    row_h = (out_h - header_h) // n_dev
    # Single-device: stack cams-on-top with a wide panel below. Multi-device:
    # keep the compact side-by-side (cam1 | cam2 | panel) row layout so all
    # devices fit vertically.
    stacked = (n_dev == 1)
    if stacked:
        panel_h = min(320, max(200, row_h // 3))
        cam_h = row_h - panel_h
        cam_w = out_w // 2
        chart_w = out_w
    else:
        # Cams at native 16:9 aspect (Pi Camera h264 source), panel gets 1/3
        # of the total width. Override --width so the final image has that
        # 2:1 cam-to-panel ratio with no wasted horizontal space.
        cam_h = row_h
        cam_w = int(round(cam_h * 16 / 9))
        chart_w = cam_w
        out_w = 2 * cam_w + chart_w
        panel_h = row_h
    n_frames = max(1, int((w1f - w0f) * fps))
    if stacked:
        print(f"Output: {out_w}x{out_h}, stacked layout — cams {cam_w}x{cam_h}, "
              f"panel {chart_w}x{panel_h}, {n_frames} frames @ {fps:.1f} fps "
              f"({(w1f - w0f):.1f}s)")
    else:
        print(f"Output: {out_w}x{out_h}, {n_dev} device rows of {row_h}px, "
              f"{n_frames} frames @ {fps:.1f} fps ({(w1f - w0f):.1f}s)")

    tmp_video = str(Path(output_file).with_suffix(".video_only.mp4"))
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(tmp_video, fourcc, fps, (out_w, out_h))
    if not writer.isOpened():
        print("Error opening VideoWriter")
        return 1

    from datetime import datetime, timezone
    blank = np.full((cam_h, cam_w, 3), 255, dtype=np.uint8)

    print("Rendering composite...")
    for k in range(n_frames):
        t = w0f + k / fps

        rows = []
        for dev in devices:
            i1 = nearest_index(dev["ws1"], t)
            i2 = nearest_index(dev["ws2"], t)
            f1 = dev["r1"].get(int(i1))
            f2 = dev["r2"].get(int(i2))
            if f1 is None or f2 is None:
                tile1 = tile2 = blank.copy()
            else:
                if flip:
                    f1 = cv2.rotate(f1, cv2.ROTATE_180)
                    f2 = cv2.rotate(f2, cv2.ROTATE_180)
                lf, rf = (f2, f1) if swap else (f1, f2)
                tile1 = _fit_letterbox(lf, cam_w, cam_h)
                tile2 = _fit_letterbox(rf, cam_w, cam_h)

            # Device's own monotonic time for IMU/fuel lookup at wall t.
            mono_t = int(np.interp(t, dev["cs_wall_mon"], dev["cs_mono"]))
            chart = render_sensor_panel(dev, mono_t, t, w=chart_w, h=panel_h)

            # Compact status: label + battery.
            status = dev["label"]
            fuel = dev["fuel"]
            if fuel is not None and len(fuel["monotonic_us"]):
                fi = nearest_index(fuel["monotonic_us"], mono_t)
                soc = fuel.get("SOC_Percent")
                volt = fuel.get("Voltage_V")
                if soc is not None and len(soc):
                    status += f"  {soc[fi]:.0f}%"
                if volt is not None and len(volt):
                    status += f" {volt[fi]:.2f}V"
            # Device identity + battery live in the panel top-left; no more
            # yellow overlay on cam1.
            cv2.putText(chart, status, (8, 15),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (60, 60, 60), 1, cv2.LINE_AA)

            if stacked:
                cams = np.hstack((tile1, tile2))
                if cams.shape[1] != out_w:
                    cams = cv2.resize(cams, (out_w, cam_h))
                if chart.shape[1] != out_w:
                    chart = cv2.resize(chart, (out_w, panel_h))
                row = np.vstack((cams, chart))
            else:
                row = np.hstack((tile1, tile2, chart))
                if row.shape[1] != out_w:
                    row = cv2.resize(row, (out_w, row_h))
            rows.append(row)

        header = np.full((header_h, out_w, 3), 255, dtype=np.uint8)
        utc = datetime.fromtimestamp(t, tz=timezone.utc)
        rel = t - w0f
        cv2.putText(header,
                    f"UTC {utc.strftime('%Y-%m-%d %H:%M:%S')}.{utc.microsecond // 1000:03d}"
                    f"   t=+{int(rel // 60):02d}:{rel % 60:05.2f}",
                    (20, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 100, 0), 2, cv2.LINE_AA)

        canvas = np.vstack([header] + rows)
        if canvas.shape[0] != out_h:
            canvas = cv2.resize(canvas, (out_w, out_h))
        writer.write(canvas)

        if k % 100 == 0 or k == n_frames - 1:
            sys.stdout.write(f"\r  {k + 1}/{n_frames}  {(k + 1) / n_frames * 100:5.1f}%")
            sys.stdout.flush()
    sys.stdout.write("\n")
    writer.release()
    for dev in devices:
        dev["r1"].release()
        dev["r2"].release()

    # Mix all device mics, each placed at its wall-time offset.
    audio_devs = [d for d in devices if include_audio and d["audio"]]
    if audio_devs:
        print(f"Mixing audio from {len(audio_devs)} device(s)...")
        cmd = ["ffmpeg", "-y", "-i", tmp_video]
        filters = []
        for j, dev in enumerate(audio_devs):
            fs, rate = dev["audio_fs"], dev["audio_rate"]
            # Play each track at its measured ADC rate so it stays locked to
            # the wall clock, then resample back to nominal for the mix.
            corr = f"asetrate={int(round(rate))},aresample={fs}"
            if dev["audio_idx"] is not None and dev["audio_wall"][0] <= w0f:
                # Dense map: exact sample index at the window start.
                s0 = float(np.interp(w0f, dev["audio_wall"], dev["audio_idx"]))
                cmd += ["-ss", f"{s0 / fs:.6f}", "-i", dev["audio"]]
                delay_ms = 0
            else:
                off = dev["audio_wall0"] - w0f
                if off < 0:
                    cmd += ["-ss", f"{-off * rate / fs:.6f}", "-i", dev["audio"]]
                    delay_ms = 0
                else:
                    cmd += ["-i", dev["audio"]]
                    delay_ms = int(off * 1000)
            filters.append(f"[{j + 1}:a]{corr},adelay=delays={delay_ms}:all=1[a{j}]")
        mix_in = "".join(f"[a{j}]" for j in range(len(audio_devs)))
        # amix's default normalize=1 divides by N so mixing N tracks doesn't
        # clip — but that leaves the muxed audio ~20*log10(N) dB below the
        # source level, which is inaudible in a player. Compensate with a
        # matching volume boost; ffmpeg will soft-clip if peaks coincide.
        filters.append(f"{mix_in}amix=inputs={len(audio_devs)}:duration=longest[amixed]")
        filters.append(f"[amixed]volume={float(len(audio_devs)):.2f}[aout]")
        cmd += ["-filter_complex", ";".join(filters),
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-shortest", output_file]
        rc, err = run_ffmpeg_with_progress(cmd, w1f - w0f)
        if rc != 0:
            print(f"ffmpeg mix failed:\n{err[-500:]}")
            return 1
        try:
            os.remove(tmp_video)
        except OSError:
            pass
    else:
        os.replace(tmp_video, output_file)

    size_mb = os.path.getsize(output_file) / (1024 * 1024)
    print(f"Done: {output_file} ({size_mb:.1f} MB)")
    return 0


# ---------------------------------------------------------------------------
# Composite renderer + writer
# ---------------------------------------------------------------------------
def make_aligned_video(rec_dir, output_file, flip=True, swap=True, include_audio=True,
                       out_w=1920, out_h=1080):
    """Render composite MP4 and mux audio.

    Rendering is Python + OpenCV per frame; muxing is a single ffmpeg -c copy
    call that adds the audio track. Video output goes to a temp file first,
    then ffmpeg copies + muxes to `output_file`.
    """
    cam_dir = Path(rec_dir) / "camera"
    video1, fmt1 = find_video_file(1, str(cam_dir))
    video2, fmt2 = find_video_file(2, str(cam_dir))
    ts_path1 = cam_dir / "camera1_timestamps.bin"
    ts_path2 = cam_dir / "camera2_timestamps.bin"

    if not (video1 and video2 and ts_path1.exists() and ts_path2.exists()):
        print("Error: need camera1/camera2 h264+timestamps under camera/")
        print(f"  video1={video1} video2={video2}")
        print(f"  ts1={ts_path1.exists()} ts2={ts_path2.exists()}")
        return 1

    print(f"Loading camera timestamps...")
    ts1 = load_timestamps(str(ts_path1))
    ts2 = load_timestamps(str(ts_path2))
    if not ts1 or not ts2:
        print("Error: empty timestamp files")
        return 1

    print(f"Preparing seekable containers (cached after first run)...")
    seek1, n1 = ensure_seekable(video1, ts1, fmt1)
    seek2, n2 = ensure_seekable(video2, ts2, fmt2)
    if not seek1 or not seek2:
        return 1
    ts1, ts2 = ts1[:n1], ts2[:n2]

    print(f"Loading IMU + fuel gauge...")
    imu = load_csv_columns(find_imu_csv(rec_dir), [
        "heading_deg", "roll_deg", "pitch_deg",
        "temp_c", "cal_sys", "cal_gyro", "cal_accel", "cal_mag",
    ])
    fuel = load_csv_columns(find_fuel_csv(rec_dir), ["Voltage_V", "SOC_Percent"])
    print(f"  IMU: {'none' if imu is None else len(imu['monotonic_us'])} rows")
    print(f"  Fuel: {'none' if fuel is None else len(fuel['monotonic_us'])} rows")

    audio_file = find_audio_file(rec_dir) if include_audio else None
    print(f"  Audio: {audio_file or 'none'}")

    # Open cam captures
    cap1 = cv2.VideoCapture(seek1)
    cap2 = cv2.VideoCapture(seek2)
    if not cap1.isOpened() or not cap2.isOpened():
        print("Error opening video captures")
        return 1

    # Grid geometry
    cell_w = out_w // 2
    cell_h = out_h // 2
    cam1_ts_np = np.asarray(ts1, dtype=np.int64)
    cam2_ts_np = np.asarray(ts2, dtype=np.int64)

    duration_s = (ts1[-1] - ts1[0]) / 1e6
    fps = len(ts1) / duration_s
    print(f"Output: {out_w}x{out_h} @ {fps:.2f} fps, {len(ts1)} frames, {duration_s:.1f}s")

    # Temp video file (video only), then ffmpeg mux audio.
    tmp_video = str(Path(output_file).with_suffix(".video_only.mp4"))
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(tmp_video, fourcc, fps, (out_w, out_h))
    if not writer.isOpened():
        print("Error opening VideoWriter")
        return 1

    # Precompute cam2 nearest index per cam1 frame.
    print("Building cam1↔cam2 sync map...")
    sync = np.searchsorted(cam2_ts_np, cam1_ts_np).clip(0, len(cam2_ts_np) - 1)
    # Snap to nearest of {i-1, i}.
    left = np.clip(sync - 1, 0, len(cam2_ts_np) - 1)
    d_left = np.abs(cam2_ts_np[left] - cam1_ts_np)
    d_right = np.abs(cam2_ts_np[sync] - cam1_ts_np)
    sync = np.where(d_left < d_right, left, sync)

    t0 = ts1[0]
    print("Rendering composite...")
    last_cam2_idx = -1
    _, frame2 = None, None
    for i in range(len(ts1)):
        r1, frame1 = cap1.read()
        if not r1:
            break
        # Only advance cam2 to the target index (usually sequential; occasional skip).
        target2 = int(sync[i])
        if target2 != last_cam2_idx + 1:
            cap2.set(cv2.CAP_PROP_POS_FRAMES, target2)
        r2, frame2 = cap2.read()
        if not r2:
            break
        last_cam2_idx = target2

        if flip:
            frame1 = cv2.rotate(frame1, cv2.ROTATE_180)
            frame2 = cv2.rotate(frame2, cv2.ROTATE_180)

        left_frame, right_frame = (frame2, frame1) if swap else (frame1, frame2)
        left_frame = cv2.resize(left_frame, (cell_w, cell_h),
                                 interpolation=cv2.INTER_AREA)
        right_frame = cv2.resize(right_frame, (cell_w, cell_h),
                                  interpolation=cv2.INTER_AREA)

        t_us = int(ts1[i])
        chart = render_imu_chart(imu, t_us, w=cell_w, h=cell_h)
        info = render_info_panel(fuel, imu,
                                  t_rel_s=(t_us - t0) / 1e6,
                                  t_us=t_us,
                                  w=cell_w, h=cell_h)

        top = np.hstack((left_frame, right_frame))
        bot = np.hstack((chart, info))
        canvas = np.vstack((top, bot))
        writer.write(canvas)

        if i % 100 == 0 or i == len(ts1) - 1:
            pct = (i + 1) / len(ts1) * 100
            sys.stdout.write(f"\r  {i+1}/{len(ts1)}  {pct:5.1f}%")
            sys.stdout.flush()
    sys.stdout.write("\n")
    writer.release()
    cap1.release()
    cap2.release()

    # Mux audio via ffmpeg (or copy through if none).
    if audio_file and os.path.exists(audio_file):
        print(f"Muxing audio ({audio_file})...")
        # Audio may start at a slightly different monotonic than video; align
        # by (audio_t0 - video_t0) offset from timestamps CSV.
        audio_offset_s = 0.0
        audio_ts_csv = audio_file.rsplit(".", 1)[0] + ".timestamps.csv"
        if os.path.exists(audio_ts_csv):
            audio_ts = load_csv_columns(audio_ts_csv, [])
            if audio_ts is not None and len(audio_ts["monotonic_us"]) > 0:
                audio_offset_s = (int(audio_ts["monotonic_us"][0]) - t0) / 1e6

        cmd = ["ffmpeg", "-y", "-i", tmp_video]
        if abs(audio_offset_s) > 0.001:
            cmd += ["-itsoffset", f"{audio_offset_s:.6f}"]
        cmd += ["-i", audio_file,
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                output_file]
        rc, err = run_ffmpeg_with_progress(cmd, duration_s)
        if rc != 0:
            print(f"ffmpeg mux failed:\n{err[-500:]}")
            return 1
        try:
            os.remove(tmp_video)
        except OSError:
            pass
    else:
        # Rename temp video-only file into place.
        os.replace(tmp_video, output_file)

    size_mb = os.path.getsize(output_file) / (1024 * 1024)
    print(f"Done: {output_file} ({size_mb:.1f} MB)")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Composite cam1+cam2+IMU+battery into one aligned MP4. "
                    "Pass --dir multiple times for a wall-clock-aligned "
                    "multi-device composite."
    )
    parser.add_argument("--dir", required=True, action="append",
                        help="Recording session directory (repeat for multi-device)")
    parser.add_argument("--output", default=None,
                        help="Output MP4 path (default: <dir>/aligned.mp4, "
                             "or ./aligned_multi.mp4 for multi-device)")
    parser.add_argument("--no-flip", action="store_true",
                        help="Skip 180° rotation (cameras are mounted upside down by default)")
    parser.add_argument("--no-swap", action="store_true",
                        help="Skip cam1/cam2 left/right swap")
    parser.add_argument("--no-audio", action="store_true", help="Skip audio track")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=float, default=30.0,
                        help="Multi-device output frame rate (default 30)")
    parser.add_argument("--start", type=float, default=0.0,
                        help="Multi-device: skip this many seconds into the overlap")
    parser.add_argument("--duration", type=float, default=None,
                        help="Multi-device: render at most this many seconds")
    parser.add_argument("--wall-offset", action="append", default=[],
                        metavar="DEVICE:SECONDS",
                        help="Multi-device: shift a device's wall clock, e.g. "
                             "Ewego001:-22.1 (repeatable). Use when a device "
                             "missed a fleet-wide chrony correction.")
    args = parser.parse_args()

    wall_offsets = {}
    for spec in args.wall_offset:
        try:
            name, val = spec.rsplit(":", 1)
            wall_offsets[name] = float(val)
        except ValueError:
            parser.error(f"bad --wall-offset {spec!r}, expected DEVICE:SECONDS")

    rec_dirs = [os.path.abspath(d) for d in args.dir]
    for d in rec_dirs:
        if not os.path.isdir(d):
            print(f"Error: {d} not a directory")
            return 1

    default_name = "aligned.mp4" if len(rec_dirs) == 1 else "aligned_multi.mp4"
    output = args.output or os.path.join(os.getcwd(), default_name)
    return make_multi_aligned_video(
        rec_dirs=rec_dirs,
        output_file=output,
        flip=not args.no_flip,
        swap=not args.no_swap,
        include_audio=not args.no_audio,
        out_w=args.width,
        out_h=args.height,
        fps=args.fps,
        start_s=args.start,
        duration_s=args.duration,
        wall_offsets=wall_offsets,
    )


if __name__ == "__main__":
    sys.exit(main() or 0)
