#!/usr/bin/env python3
# ============================================================================
# EweGo Mesh Monitor
# ============================================================================
# Continuous TUI run from the laptop. Polls every peer over SSH and shows:
#   - chrony tracking per Pi (stratum, system offset, RMS, root dispersion)
#   - pairwise system-offset spread across the flock
#   - MAX17048 fuel gauge (voltage + SoC)
#   - ~/EweGo filesystem storage (free / % used / total)
#   - record_sensors.py recording status (dir, file growth, running procs)
#   - batman-adv TQ matrix (per-link quality, 0=lost 255=perfect)
#
# Keys during run:
#   [b] burst chronyc on every peer
#   [r] start a recording session on every peer (record_sensors.py --no-gps)
#   [s] stop recording on every peer (SIGINT the Python process)
#   [q] quit
#
# Requires:
#   - This laptop joined to the mesh (bat0 up; see mesh_join.sh)
#   - SSH key auth to each Pi as the SSH_USER
#   - On each Pi: passwordless `sudo -n batctl meshif bat0 o`
#     (already configured by pi_setup.sh)
#
# Usage:
#   python3 mesh_monitor.py                 # auto-discover peers, 1s refresh
#   python3 mesh_monitor.py -i 5 -u user    # 5s refresh, SSH as 'user'
#   python3 mesh_monitor.py 10.42.0.1 10.42.0.2 ...   # explicit peer list
# ============================================================================

import argparse
import concurrent.futures as cf
import os
import re
import select
import subprocess
import sys
import termios
import threading
import time
import tty
from collections import OrderedDict
from datetime import datetime, timezone


SSH_OPTS = [
    # On a congested mesh RTT reaches 1.5-2.4 s (hotel field notes,
    # 2026-07-09), so a full SSH handshake (~8 round trips) needs tens of
    # seconds. Everything below is tuned to pay that cost ONCE per host:
    #   - ConnectTimeout=10: the TCP handshake alone can exceed 3 s.
    #   - PreferredAuthentications/GSSAPI: skip auth methods that would each
    #     burn extra round trips before failing.
    #   - ControlPersist=yes: keep the mux master alive indefinitely; every
    #     later probe rides it at ~1 RTT with zero key exchanges. A master on
    #     a dead link self-destructs via its own keepalives (below).
    #   - ServerAliveInterval=15 (x3 = 45 s tolerance): RTT spikes alone must
    #     not kill the mux — at the old 5 s x3, a couple of 2 s RTTs could.
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ServerAliveInterval=15",
    "-o", "GSSAPIAuthentication=no",
    "-o", "PreferredAuthentications=publickey",
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=/tmp/sshmux-ewego-%r@%h:%p",
    "-o", "ControlPersist=yes",
]

PROBE = r"""
echo '##HOST##'; hostname
echo '##WLAN_MAC##'; cat /sys/class/net/wlan0/address 2>/dev/null
echo '##BAT_MAC##'; cat /sys/class/net/bat0/address 2>/dev/null
echo '##TRACK##'; chronyc tracking 2>/dev/null
echo '##ORIG##'; sudo -n batctl meshif bat0 o 2>/dev/null || true
echo '##BATT##'
# MAX17048 fuel gauge on I2C bus 1, addr 0x36. VCELL = 0x02-0x03 (12-bit,
# 1.25 mV/LSB after >>4). SOC = 0x04-0x05 (MSB = integer %, LSB = fractional).
# Prefer sudo -n so we get consistent access even if `user` isn't in `i2c`.
# Empty lines here just leave battery blank rather than breaking the probe.
(sudo -n i2cget -y 1 0x36 0x02 2>/dev/null
 sudo -n i2cget -y 1 0x36 0x03 2>/dev/null
 sudo -n i2cget -y 1 0x36 0x04 2>/dev/null
 sudo -n i2cget -y 1 0x36 0x05 2>/dev/null) || true
echo '##REC##'
# Recording health: identify the live sensor_test_*/ dir, count matching
# processes, stat the files that would grow during a capture. Any file that
# doesn't exist yet (e.g., cameras still initializing) reports size 0 — the
# render side interprets that as "flat" rather than crashing.
#
# The Pi's wall clock can jump backward across reboots (observed 2026-07-06:
# a stale dir at 16:53 sat "newer" than a fresh dir at 15:39). So `ls -1td`
# lies. Prefer /proc/<pid>/fd to find whichever dir the running record_sensors.py
# actually has open — that's the truth. Fall back to mtime-based sort when
# nothing is running (STALE is the right label anyway in that case).
running=$(pgrep -f "python3.*record_sensors.py" 2>/dev/null | wc -l)
newest=""
if [ "$running" -gt 0 ]; then
  for pid in $(pgrep -f "python3.*record_sensors.py"); do
    d=$(ls -l /proc/$pid/fd 2>/dev/null | grep -oE "/home/[^ ]*/sensor_test_[0-9_]+" | head -1)
    [ -n "$d" ] && { newest="$d"; break; }
  done
fi
if [ -z "$newest" ]; then
  newest=$(ls -1td ~/EweGo/sensor_test_* 2>/dev/null | head -1)
fi
if [ -n "$newest" ]; then
  # Compute the clock_sync age ON THE PI. The laptop's wall clock can be
  # months off from the Pi's (the flock is synced to itself, not to UTC), so
  # `laptop_now - pi_mtime` produces nonsense like "85 days STALE" on a
  # perfectly-live recording. Doing the subtraction here keeps both timestamps
  # in the same clock domain.
  pi_now=$(date +%s)
  cs_mtime=$(stat -c %Y "$newest/clock_sync.csv" 2>/dev/null || echo 0)
  echo "dir=$(basename "$newest")"
  echo "clock_sync_size=$(stat -c %s "$newest/clock_sync.csv" 2>/dev/null || echo 0)"
  echo "clock_sync_age_s=$((pi_now - cs_mtime))"
  echo "audio_size=$(stat -c %s "$newest"/audio_*.wav 2>/dev/null | head -1)"
  echo "imu_lines=$(wc -l < "$newest"/imu/logs/imu_log_*.csv 2>/dev/null || echo 0)"
  echo "cam1_size=$(stat -c %s "$newest"/camera/camera1.h264 2>/dev/null || echo 0)"
  echo "cam2_size=$(stat -c %s "$newest"/camera/camera2.h264 2>/dev/null || echo 0)"
fi
echo "running=$running"
echo '##DISK##'
# `df` on the ~/EweGo mount — one statvfs, sub-ms. Falls back silently if
# the dir doesn't exist yet.
df -B1 --output=size,used,avail ~/EweGo 2>/dev/null | tail -1
echo '##END##'
""".strip()


# ----------------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------------

def discover_peers():
    r = subprocess.run(["ip", "link", "show", "bat0"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if r.returncode != 0:
        raise SystemExit("bat0 not present — join the mesh first (mesh_join.sh)")

    # Ping sweep with bounded concurrency. Pinging populates ARP, but the kernel
    # also keeps FAILED entries for non-responders, so we filter on lladdr below.
    def ping_one(i):
        subprocess.run(
            ["ping", "-c", "1", "-W", "1", "-I", "bat0", f"10.42.0.{i}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    with cf.ThreadPoolExecutor(max_workers=64) as ex:
        list(ex.map(ping_one, range(1, 255)))

    out = subprocess.check_output(
        ["ip", "-4", "neigh", "show", "dev", "bat0"], text=True)
    # Only accept entries that have a real lladdr AND aren't FAILED/INCOMPLETE.
    # Example reachable line: "10.42.0.2 lladdr 2e:68:8d:16:b1:56 REACHABLE"
    peers = set()
    for line in out.splitlines():
        m = re.match(
            r"(\d+\.\d+\.\d+\.\d+)\s+lladdr\s+([0-9a-f:]{17})\s+(\w+)", line)
        if m and m.group(3) not in ("FAILED", "INCOMPLETE"):
            peers.add(m.group(1))

    self_out = subprocess.check_output(
        ["ip", "-4", "-o", "addr", "show", "dev", "bat0"], text=True)
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", self_out)
    self_ip = m.group(1) if m else None
    peers.discard(self_ip)
    return sorted(peers, key=lambda ip: int(ip.split(".")[-1])), self_ip


# ----------------------------------------------------------------------------
# Probe + parse
# ----------------------------------------------------------------------------

def probe(user, ip, timeout):
    try:
        r = subprocess.run(
            ["ssh", *SSH_OPTS, f"{user}@{ip}", PROBE],
            # stdin=DEVNULL is critical: without it, every ssh child inherits
            # the terminal fd we put into cbreak mode in RawTerminal, and ssh
            # can reset the local tty modes on the way out — silently
            # undoing cbreak. Symptom: [b]/[r]/[s]/[q] look inert because
            # every key waits for Enter, but no warning appears because
            # setcbreak did initially succeed. Field-observed 2026-07-08.
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout)
        return ip, r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return ip, "", "timeout", -1


def parse_sections(text):
    out, cur, buf = {}, None, []
    for line in text.splitlines():
        m = re.match(r"##(\w+)##$", line.strip())
        if m:
            if cur is not None:
                out[cur] = "\n".join(buf).strip()
            cur, buf = m.group(1), []
        else:
            buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(buf).strip()
    return out


def parse_tracking(text):
    fields = {}
    for line in text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fields[k.strip()] = v.strip()
    return fields


def parse_originators(text):
    """Returns dict mac -> TQ for the selected ('*') route to each originator."""
    result = {}
    for line in text.splitlines():
        m = re.match(r"\s*(\*?)\s*([0-9a-f:]{17})\s+\S+\s+\((\d+)\)", line)
        if m and m.group(1) == "*":
            result[m.group(2).lower()] = int(m.group(3))
    return result


def parse_disk(text):
    """
    Read one output line of `df -B1 --output=size,used,avail <path>`:
      "62914560000 11534336000 51380224000"
    Returns (total_bytes, used_bytes, free_bytes) or (None, None, None) on any
    parse issue (df failed, path missing, empty output, etc.).
    """
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            total, used, free = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        if total > 0:
            return total, used, free
    return None, None, None


def parse_recording(text):
    """
    Read key=value lines emitted by the ##REC## block. Returns a dict with
    normalized types; missing keys default to None. `running` always present
    (may be 0). If no `dir` key, no active or historical sensor_test dir exists
    on this Pi.
    """
    result = {
        "dir": None,
        "running": 0,
        "clock_sync_size": None,
        "clock_sync_age_s": None,   # Pi-computed; kept in Pi's clock domain
        "audio_size": None,
        "imu_lines": None,
        "cam1_size": None,
        "cam2_size": None,
    }
    for line in text.splitlines():
        m = re.match(r"([a-z_0-9]+)=(.*)", line.strip())
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if k == "dir":
            result["dir"] = v or None
        elif k in ("running", "clock_sync_size", "clock_sync_age_s",
                   "audio_size", "imu_lines", "cam1_size", "cam2_size"):
            try:
                result[k] = int(v) if v else None
            except ValueError:
                result[k] = None
    return result


def parse_battery(text):
    """
    Parse four hex bytes emitted by i2cget into (voltage_V, soc_percent).
    Any of the four bytes missing → returns (None, None) so the row shows '—'.
    """
    hex_lines = [ln.strip() for ln in text.splitlines()
                 if re.match(r"^0x[0-9a-fA-F]{2}$", ln.strip())]
    if len(hex_lines) < 4:
        return None, None
    try:
        v_msb, v_lsb, s_msb, s_lsb = [int(x, 16) for x in hex_lines[:4]]
    except ValueError:
        return None, None
    # VCELL is a 12-bit value, top-aligned in 16 bits; each LSB = 1.25 mV.
    vcell_raw = (v_msb << 8) | v_lsb
    voltage = (vcell_raw >> 4) * 0.00125
    # SOC: high byte is integer percent, low byte is fractional (÷256).
    soc = s_msb + s_lsb / 256.0
    # Guard against nonsense (I2C garbage or missing chip returning 0xFF..).
    if voltage > 5.5 or voltage < 2.0 or soc > 101 or soc < 0:
        return None, None
    return voltage, soc


def parse_seconds(s):
    m = re.match(r"([+\-\d.eE]+)", s)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------

class C:
    R = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    YEL = "\033[33m"
    GRN = "\033[32m"
    CYAN = "\033[36m"
    CLEAR = "\033[2J\033[H"


def color_stratum(s):
    try:
        si = int(s)
    except (TypeError, ValueError):
        return f"{C.DIM} ?{C.R}"
    if si <= 4:
        c = C.GRN
    elif si <= 12:
        c = C.CYAN
    elif si <= 15:
        c = C.YEL
    else:
        c = C.RED
    return f"{c}{si:>2d}{C.R}"


def color_tq(tq):
    if tq is None:
        return f"{C.DIM} -- {C.R}"
    if tq >= 230:
        c = C.GRN
    elif tq >= 180:
        c = C.YEL
    elif tq > 0:
        c = C.RED
    else:
        c = C.DIM
    return f"{c}{tq:>3d}{C.R}"


def _fmt_bytes(n):
    """Compact human byte count: 1.2M, 3.4G, 512K. Always ≤6 chars."""
    if n is None:
        return "—"
    if n < 1024:
        return f"{n:>4}B"
    for unit in ("K", "M", "G", "T"):
        n /= 1024.0
        if n < 1024 or unit == "T":
            if n < 10:
                return f"{n:4.1f}{unit}"
            return f"{n:4.0f}{unit}"
    return f"{n:.0f}"


def _fmt_count(n):
    """Compact human count: 25k, 1.3M. Fits in ≤5 chars."""
    if n is None:
        return "—"
    if n < 1000:
        return f"{n:>5}"
    if n < 1_000_000:
        return f"{n/1000:4.1f}k" if n < 10_000 else f"{n//1000:>4}k"
    return f"{n/1_000_000:4.1f}M"


def _fmt_age(sec):
    """Compact duration: 4s, 4m12s, 3h07m, 3d21h. Fits in ≤6 chars."""
    if sec is None:
        return "—"
    sec = int(sec)
    if sec < 60:
        return f"{sec:>3}s"
    if sec < 3600:
        return f"{sec // 60:>2}m{sec % 60:02}s"
    if sec < 86400:
        return f"{sec // 3600:>2}h{(sec % 3600) // 60:02}m"
    return f"{sec // 86400:>2}d{(sec % 86400) // 3600:02}h"


def fmt_time(v):
    if v is None:
        return "—"
    a = abs(v)
    if a < 1e-3:
        return f"{v*1e6:+.1f} µs"
    if a < 1.0:
        return f"{v*1e3:+.2f} ms"
    return f"{v:+.2f} s"


def resolve_ref(ref_field, report):
    """Turn 'Reference ID: 0A2A0002 (10.42.0.2)' into a friendly host name."""
    m = re.search(r"\(([^)]+)\)", ref_field)
    if m:
        ip_str = m.group(1)
        if ip_str in report:
            return report[ip_str].get("hostname") or ip_str, False
        return ip_str, False
    # Empty parens → local clock or initial
    if "7F7F" in ref_field.upper():
        return "local", True
    if "00000000" in ref_field:
        return "init", False
    return ref_field.split()[0] if ref_field else "?", False


def render(report, interval, poll_dt, status=None, prev_report=None):
    """
    Section order (top → bottom): MESH LINK QUALITY → TIME SYNC → RECORDING
    → STORAGE → BATTERY. Each section is a small closure below so the top-level
    body is just the composition order — reordering is one edit.
    """
    out = [C.CLEAR]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    out.append(f"{C.BOLD}EweGo Mesh Monitor{C.R}    {ts}    "
               f"refresh={interval:g}s  data age={poll_dt:.1f}s")
    out.append("═" * 80)

    # ------------------------------------------------------------------ helper
    # Pads plain text then wraps in ANSI. If we did `f"{coloured:<28}"` directly,
    # invisible escape bytes would count against the width and rows would drift.
    def col(text, width, color=None, right=False):
        s = f"{text:>{width}}" if right else f"{text:<{width}}"
        return f"{color}{s}{C.R}" if color else s

    # --------------------------------------------------------------- MESH LINK
    def tq_section():
        out.append("")
        out.append(f"{C.BOLD}MESH LINK QUALITY{C.R}  "
                   f"(batctl TQ on selected route, 0=lost 255=perfect; row→col)")
        out.append("─" * 80)

        hosts = [(ip, info.get("hostname") or ip) for ip, info in report.items()]
        short = {ip: (h[-4:] if len(h) > 6 else h) for ip, h in hosts}

        header = f" {'from \\ to':<11}" + "".join(
            f" {short[ip]:>5}" for ip, _ in hosts)
        out.append(header)

        any_orig = False
        for ip, info in report.items():
            row_host = info.get("hostname") or ip
            orig = info.get("originators", {})
            if not orig:
                out.append(f" {row_host:<11}  {C.DIM}batctl unavailable{C.R}")
                continue
            any_orig = True
            cells = []
            for tip, _ in hosts:
                if tip == ip:
                    cells.append(f" {C.DIM} -- {C.R}")
                    continue
                tmac = (report[tip].get("wlan_mac") or "").lower().strip()
                cells.append(f" {color_tq(orig.get(tmac))}")
            out.append(f" {row_host:<11}" + "".join(cells))

        if not any_orig:
            out.append("")
            out.append(f"  {C.YEL}No batctl data — check sudo -n on the Pis.{C.R}")

    # --------------------------------------------------------------- TIME SYNC
    def time_sync_section():
        out.append("")
        out.append(f"{C.BOLD}TIME SYNC{C.R}  (chrony tracking)")
        out.append("─" * 80)
        out.append(f" {'Host':<11} {'S':>2}  {'Ref':<10}  "
                   f"{'SysOffset':>11} {'Last':>11} {'RMS':>11} {'RootDisp':>11}")

        sys_offsets, root_node = {}, None
        for ip, info in report.items():
            host = info.get("hostname") or ip
            if info.get("error"):
                out.append(f" {host:<11} {C.RED}offline{C.R}  ({info['error']})")
                continue

            tr = info.get("tracking", {})
            ref_str = tr.get("Reference ID", "")
            ref_name, is_local_root = resolve_ref(ref_str, report)
            if is_local_root:
                root_node = host

            sm = re.match(r"([\d.eE+-]+)\s+seconds\s+(\w+)",
                          tr.get("System time", ""))
            sysv = None
            if sm:
                v = float(sm.group(1))
                sysv = -v if sm.group(2) == "slow" else v
                sys_offsets[host] = sysv

            last = parse_seconds(tr.get("Last offset", ""))
            rms = parse_seconds(tr.get("RMS offset", ""))
            rdsp = parse_seconds(tr.get("Root dispersion", ""))
            st = tr.get("Stratum", "?")

            out.append(
                f" {host:<11} {color_stratum(st)}  {ref_name[:10]:<10}  "
                f"{fmt_time(sysv):>11} {fmt_time(last):>11} "
                f"{fmt_time(rms):>11} {fmt_time(rdsp):>11}")

        out.append("")
        if len(sys_offsets) >= 2:
            vals = list(sys_offsets.values())
            spread = (max(vals) - min(vals)) * 1e6
            spread_color = C.GRN if spread < 100 else C.YEL if spread < 1000 else C.RED
            root_str = root_node if root_node else f"{C.DIM}cyclic (no local-clock anchor){C.R}"
            out.append(f"  Sync spread (sysOffset): "
                       f"{spread_color}{spread:.1f} µs{C.R}"
                       f"        Root: {C.BOLD}{root_str}{C.R}")

    # --------------------------------------------------------------- RECORDING
    def recording_section():
        out.append("")
        out.append(f"{C.BOLD}RECORDING{C.R}  (record_sensors.py --no-gps)")
        out.append("─" * 80)
        out.append(
            f" {'Host':<11}  {'Status':<9}  {'Dir':<28}  {'Age':>6}  "
            f"{'Clock':>5}  {'Audio':>6}  {'IMU':>6}  {'Cam1':>6}  {'Cam2':>6}"
        )
        now = time.time()

        def growth_color(cur, prev, elapsed):
            if cur is None or prev is None or elapsed <= 0:
                return C.DIM
            if cur > prev:
                return C.GRN
            if elapsed > 30:
                return C.RED
            if elapsed > 5:
                return C.YEL
            return C.DIM

        for ip, info in report.items():
            host = info.get("hostname") or ip
            rec = info.get("recording") or {}
            running = rec.get("running", 0) or 0
            rec_dir = rec.get("dir")
            # Age is computed on the Pi, in the Pi's clock domain — see the
            # ##REC## probe. Trying to compute it laptop-side would be off
            # by the flock-vs-laptop wall-clock difference (86+ days observed
            # in the field 2026-07-08).
            cs_age = rec.get("clock_sync_age_s")

            # State machine:
            #   idle       — no sensor_test_ dir at all
            #   RECORDING  — process running AND clock_sync.csv fresh (<5 s old)
            #   STALE      — dir exists but process gone OR clock_sync mtime old
            if not rec_dir:
                status_col = col("idle",      9, C.DIM)
                dir_col    = col("—",        28, C.DIM)
                age_col    = col("—",         6, C.DIM, right=True)
                clock_col  = col("—",         5, C.DIM, right=True)
                audio_col  = col("—",         6, C.DIM, right=True)
                imu_col    = col("—",         6, C.DIM, right=True)
                cam1_col   = col("—",         6, C.DIM, right=True)
                cam2_col   = col("—",         6, C.DIM, right=True)
            else:
                # `cs_age < 5.0 and cs_age > -5.0` — guard both directions of
                # Pi wall-clock drift. If the Pi clock stepped forward while
                # we were recording, cs_age can briefly go slightly negative;
                # that's still healthy, not stale.
                recording_now = (running >= 1 and cs_age is not None
                                  and -5.0 < cs_age < 5.0)
                if recording_now:
                    status_col = col("RECORDING", 9, C.GRN)
                    clock_col  = col("ok",        5, C.GRN, right=True)
                else:
                    status_col = col("STALE",     9, C.YEL)
                    clock_col  = col("stale",     5, C.YEL, right=True)

                dir_col = col(rec_dir[:28], 28)
                age_col = col(_fmt_age(cs_age) if cs_age is not None else "—",
                              6, right=True)

                prev = (prev_report or {}).get(ip, {})
                prev_rec = prev.get("recording") or {}
                prev_sampled = prev.get("sampled_at")
                elapsed = (now - prev_sampled) if prev_sampled else 0

                def size_col(key):
                    cur = rec.get(key)
                    return col(_fmt_bytes(cur), 6,
                               growth_color(cur, prev_rec.get(key), elapsed),
                               right=True)

                audio_col = size_col("audio_size")
                imu_cur = rec.get("imu_lines")
                imu_col = col(_fmt_count(imu_cur), 6,
                              growth_color(imu_cur, prev_rec.get("imu_lines"), elapsed),
                              right=True)
                cam1_col = size_col("cam1_size")
                cam2_col = size_col("cam2_size")

            out.append(
                f" {host:<11}  {status_col}  {dir_col}  {age_col}  "
                f"{clock_col}  {audio_col}  {imu_col}  {cam1_col}  {cam2_col}"
            )

    # ----------------------------------------------------------------- STORAGE
    def storage_section():
        out.append("")
        out.append(f"{C.BOLD}STORAGE{C.R}  (~/EweGo filesystem)")
        out.append("─" * 80)
        out.append(f" {'Host':<11}    {'Free':>7}   {'% used':>6}   {'Total':>7}")
        for ip, info in report.items():
            host = info.get("hostname") or ip
            total = info.get("disk_total")
            used = info.get("disk_used")
            free = info.get("disk_free")
            if not (total and used is not None and free is not None):
                out.append(f" {host:<11}     {C.DIM}   —  {C.R}    {C.DIM}  — {C.R}    {C.DIM}   —  {C.R}")
                continue

            free_gb  = free  / 1e9
            total_gb = total / 1e9
            pct_used = 100.0 * used / total if total else 0.0

            # Free-space color: 10 GB is ~28 min of full recording (24 fps ×
            # 12 Mbps × 2 cams ≈ 6 MB/s). <2 GB is <5 min — red-zone.
            free_color = (C.GRN if free_gb >= 10 else
                          C.YEL if free_gb >=  2 else C.RED)
            pct_color  = (C.GRN if pct_used < 70 else
                          C.YEL if pct_used < 90 else C.RED)

            out.append(
                f" {host:<11}     "
                f"{free_color}{free_gb:5.1f}G{C.R}    "
                f"{pct_color}{pct_used:4.0f}%{C.R}    "
                f"{total_gb:5.1f}G"
            )

    # ----------------------------------------------------------------- BATTERY
    def battery_section():
        out.append("")
        out.append(f"{C.BOLD}BATTERY{C.R}  (MAX17048 fuel gauge, I2C bus 1)")
        out.append("─" * 80)
        out.append(f" {'Host':<11}   {'Voltage':>8}    {'SoC':>6}")
        any_batt = False
        for ip, info in report.items():
            host = info.get("hostname") or ip
            v, soc = info.get("voltage"), info.get("soc")
            if v is None or soc is None:
                v_str = f"{C.DIM}   —   {C.R}"
                soc_str = f"{C.DIM}  —  {C.R}"
            else:
                any_batt = True
                # Voltage: green ≥3.7 V (nominal Li-ion), yellow 3.4-3.7, red <3.4.
                v_color   = (C.GRN if v   >= 3.7 else
                             C.YEL if v   >= 3.4 else C.RED)
                soc_color = (C.GRN if soc >= 50  else
                             C.YEL if soc >= 20  else C.RED)
                v_str   = f"{v_color}{v:5.2f} V{C.R}"
                soc_str = f"{soc_color}{soc:5.1f}%{C.R}"
            out.append(f" {host:<11}     {v_str}    {soc_str}")
        if not any_batt:
            out.append(f"  {C.DIM}No MAX17048 data — check sudo -n on the Pis or "
                       f"fuel gauge presence.{C.R}")

    # -------------------------------------------------------------- Compose
    tq_section()
    time_sync_section()
    recording_section()
    storage_section()
    battery_section()

    out.append("")
    out.append(f"{C.DIM}[b] burst   [r] start rec   [s] stop rec   [q] quit{C.R}")
    if status:
        out.append(f" {status}")
    return "\n".join(out)


# ----------------------------------------------------------------------------
# Interaction: keypress + burst
# ----------------------------------------------------------------------------

class RawTerminal:
    """Put stdin into cbreak mode so we can read single keys without Enter.
    Falls back silently if stdin isn't a TTY (e.g., piped input).

    `.cbreak_active` tells the caller whether we actually got cbreak. When
    that's False, `[b] [r] [s] [q]` won't respond until Enter is pressed —
    an important thing to warn about at startup because it looks identical
    to "the keypress was ignored" from the user's perspective (field-observed
    2026-07-08).
    """

    def __init__(self):
        self.fd = sys.stdin.fileno() if sys.stdin.isatty() else None
        self.old = None
        self.cbreak_active = False

    def __enter__(self):
        if self.fd is not None:
            try:
                self.old = termios.tcgetattr(self.fd)
                tty.setcbreak(self.fd)
                self.cbreak_active = True
            except (termios.error, OSError):
                self.old = None
        return self

    def __exit__(self, *_):
        if self.old is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)


def read_key(timeout):
    """Block up to `timeout` seconds for a single keypress. Returns char or None.

    Reads directly from the stdin fd with os.read to bypass sys.stdin's
    TextIOWrapper. The wrapper is line-buffered on a TTY, so even after
    select() confirms the fd is readable, sys.stdin.read(1) may block
    waiting for a newline to flush its internal buffer — that made keys
    appear inert until Enter was pressed (field-observed 2026-07-08).
    """
    if not sys.stdin.isatty():
        time.sleep(timeout)
        return None
    fd = sys.stdin.fileno()
    rlist, _, _ = select.select([fd], [], [], timeout)
    if not rlist:
        return None
    try:
        data = os.read(fd, 1)
    except OSError:
        return None
    if not data:
        return None
    return data.decode("utf-8", errors="replace")


def fire_burst(user, peers, timeout=8.0):
    """Fire `chronyc burst 4/8` on every peer in parallel. Returns count of ok hosts."""
    def burst_one(ip):
        try:
            r = subprocess.run(
                ["ssh", *SSH_OPTS, f"{user}@{ip}",
                 "sudo -n chronyc -a 'burst 4/8' >/dev/null && echo ok"],
                stdin=subprocess.DEVNULL,  # see probe() — protects cbreak mode
                capture_output=True, text=True, timeout=timeout)
            return r.returncode == 0 and "ok" in r.stdout
        except subprocess.TimeoutExpired:
            return False

    workers = max(1, min(len(peers), 16))
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        return sum(1 for ok in ex.map(burst_one, peers) if ok)


# Launch and stop via `ewego-sensor.service` (installed by pi_setup.sh 8.6).
#
# We used to send a `setsid nohup uv run python ...` pipe here and a matching
# pkill sweep on stop — both worked but had a lot of moving parts. The
# systemd unit takes all that off our plate:
#   - `Restart=on-failure` in the unit handles mid-session crashes.
#   - `sudo -n systemctl start/stop` returns cleanly to ssh (no pty hangs).
#   - Chrony ordering is handled by the unit's `After=chrony.service`.
#   - `record_sensors.py`'s own `ensure_clean_start()` handles any leftover
#     orphaned children on next launch, so we don't need a defensive pkill
#     sweep here anymore.
#
# Requires the operator user to have passwordless sudo for
# `systemctl enable/disable/start/stop ewego-sensor.service` — sudoers drop-in
# is installed by pi_setup.sh 8.6.
#
# `enable --now`: enable the unit AND start it. The `enable` half persists
# across reboots, so if a Pi power-cycles mid-recording (watchdog reset,
# brownout, operator power-cycle) it comes back and auto-resumes the
# session — closing the crash-recovery gap that plain `start` left open
# (2026-07-09: operator cycled ewe5 mid-test and it came back idle instead
# of resuming, because the unit wasn't enabled).
# `reset-failed` first, because `enable --now` doesn't work when the unit is
# in a `failed` state from a prior aborted attempt.
_START_REC_CMD = (
    "sudo -n systemctl reset-failed ewego-sensor.service 2>/dev/null; "
    "sudo -n systemctl enable --now ewego-sensor.service && echo ok"
)

# `disable --now`: stop the unit AND remove the boot-time enable so a fresh
# boot won't auto-record. Complements the `enable --now` above so recording
# stays exactly on when we want it and off when we don't.
# systemctl stop sends SIGINT (KillSignal=SIGINT in the unit), waits up to
# TimeoutStopSec=15s for record_sensors.py's shutdown to drain, then
# escalates to SIGKILL. The pkill/fuser belt-and-suspenders sweep we used
# to run here is now redundant — but we keep a fuser -k on the four
# hardware nodes just in case a truly wedged subprocess survives systemd's
# SIGKILL cascade (rare, but harmless when it doesn't happen).
_STOP_REC_CMD = (
    "sudo -n systemctl disable --now ewego-sensor.service 2>/dev/null; "
    "sudo -n fuser -k /dev/snd/pcmC0D0c "
    "/dev/video0 /dev/video1 /dev/ttyAMA5 2>/dev/null; "
    "echo ok"
)


def _fire_parallel(user, peers, cmd, timeout=12.0):
    """Run `cmd` on every peer in parallel, count how many return exit=0 with
    'ok' in stdout. Shared by fire_record_start / fire_record_stop / any future
    parallel action."""
    def run_one(ip):
        try:
            r = subprocess.run(
                ["ssh", *SSH_OPTS, f"{user}@{ip}", cmd],
                stdin=subprocess.DEVNULL,  # see probe() — protects cbreak mode
                capture_output=True, text=True, timeout=timeout)
            return r.returncode == 0 and "ok" in r.stdout
        except subprocess.TimeoutExpired:
            return False

    workers = max(1, min(len(peers), 16))
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        return sum(1 for ok in ex.map(run_one, peers) if ok)


def fire_record_start(user, peers):
    """Launch record_sensors.py --no-gps on every peer. Returns count of ok hosts."""
    return _fire_parallel(user, peers, _START_REC_CMD)


def fire_record_stop(user, peers):
    """SIGINT the record_sensors.py process on every peer. Returns count of ok hosts
    (a peer with nothing running still counts as ok — the desired state was already
    achieved)."""
    return _fire_parallel(user, peers, _STOP_REC_CMD)


# ----------------------------------------------------------------------------
# Background polling
# ----------------------------------------------------------------------------

class PeerPoller(threading.Thread):
    """One daemon thread per peer, probing continuously and stashing the
    latest raw result in `self.latest`.

    Why this shape instead of a per-cycle ThreadPoolExecutor on the main
    thread (the original design):
      - The main thread used to block for the full probe duration, so keys
        were only handled at cycle boundaries — on a congested mesh where
        every probe hits the 6 s timeout, [q] took up to 6 s to respond
        (field-observed 2026-07-08).
      - daemon=True means quitting never waits for an in-flight ssh:
        ThreadPoolExecutor registers an atexit hook that joins its workers,
        which was the ^C-then-hang the operator kept hitting.
      - One thread per peer means a slow/offline ewe no longer delays fresh
        data from the healthy ones.

    `latest` is replaced atomically (single reference assignment under the
    GIL), so readers need no lock.
    """

    def __init__(self, user, ip, timeout, interval):
        super().__init__(daemon=True, name=f"poll-{ip}")
        self.user = user
        self.ip = ip
        self.timeout = timeout
        self.interval = interval
        self.latest = None   # (stdout, stderr, rc, sampled_at) or None

    def run(self):
        while True:
            t0 = time.monotonic()
            _, out, err, rc = probe(self.user, self.ip, self.timeout)
            self.latest = (out, err, rc, time.time())
            time.sleep(max(0.05, self.interval - (time.monotonic() - t0)))


def build_report(peers, pollers):
    """Assemble a render-ready report from each poller's latest raw probe.
    Peers that haven't completed their first probe yet show as 'probing…'."""
    report = OrderedDict()
    for ip in peers:
        latest = pollers[ip].latest
        if latest is None:
            report[ip] = {
                "hostname": None, "wlan_mac": "", "bat_mac": "",
                "tracking": {}, "originators": {},
                "voltage": None, "soc": None,
                "disk_total": None, "disk_used": None, "disk_free": None,
                "recording": parse_recording(""),
                "sampled_at": None,
                "error": "probing…",
            }
            continue
        stdout, stderr, rc, sampled_at = latest
        sec = parse_sections(stdout)
        voltage, soc = parse_battery(sec.get("BATT", ""))
        total, used, free = parse_disk(sec.get("DISK", ""))
        report[ip] = {
            "hostname": sec.get("HOST", "").strip() or None,
            "wlan_mac": sec.get("WLAN_MAC", "").strip(),
            "bat_mac": sec.get("BAT_MAC", "").strip(),
            "tracking": parse_tracking(sec.get("TRACK", "")),
            "originators": parse_originators(sec.get("ORIG", "")),
            "voltage": voltage,
            "soc": soc,
            "disk_total": total,
            "disk_used": used,
            "disk_free": free,
            "recording": parse_recording(sec.get("REC", "")),
            "sampled_at": sampled_at,
            "error": stderr.strip() if rc != 0 else "",
        }
    return report


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Continuous TUI for chrony sync + batman-adv TQ on the EweGo mesh.")
    ap.add_argument("-u", "--user", default="user",
                    help="SSH user on each Pi (default: user)")
    ap.add_argument("-i", "--interval", type=float, default=1.0,
                    help="Refresh interval in seconds (default: 1)")
    # 20 s: the first probe pays a full SSH kex (~8 round trips), which at the
    # 1.5-2.4 s RTTs seen on a congested mesh needs 12-20 s. Probes run in
    # background threads, so a long timeout costs nothing when links are fast.
    ap.add_argument("--timeout", type=float, default=20.0,
                    help="SSH probe timeout per Pi (default: 20s)")
    ap.add_argument("peers", nargs="*",
                    help="Peer IPs (default: auto-discover on bat0)")
    args = ap.parse_args()

    if args.peers:
        peers = args.peers
    else:
        print("Discovering peers on bat0 …", file=sys.stderr)
        peers, _ = discover_peers()
        print(f"  found {len(peers)} peer(s): {', '.join(peers)}",
              file=sys.stderr)
        if not peers:
            raise SystemExit("No peers found — are the Pis up and on the mesh?")

    status = None  # transient footer message (e.g. "burst fired …")
    prev_report = OrderedDict()   # previous render's report — colors size deltas

    # SSH probing happens entirely on background daemon threads; the main
    # thread below only reads keys and renders, so keystrokes take effect
    # within ~100 ms no matter how slow or dead the mesh links are.
    pollers = {ip: PeerPoller(args.user, ip, args.timeout, args.interval)
               for ip in peers}
    for p in pollers.values():
        p.start()

    try:
        with RawTerminal() as term:
            if not term.cbreak_active:
                # Warn to stderr AND seed the first frame's status so the
                # very first frame the user sees carries a clear diagnosis.
                warning = ("stdin is not a raw TTY — [b]/[r]/[s]/[q] will "
                           "require you to press Enter after each key.")
                print(f"⚠  {warning}", file=sys.stderr)
                status = f"\033[33m⚠ {warning}\033[0m"

            report = build_report(peers, pollers)
            data_age = 0.0
            last_render = 0.0

            def do_render(msg):
                sys.stdout.write(render(report, args.interval, data_age, msg,
                                         prev_report=prev_report))
                sys.stdout.write("\n")
                sys.stdout.flush()

            def confirm(prompt):
                """Show `prompt` in the footer and wait up to 10 s for a
                single keypress. Returns True only on y/Y. Any other key
                (or timeout) cancels. 10 s is generous on purpose — the
                field-observed failure mode is "user reads the prompt,
                reaches for y, misses the window."
                """
                do_render(f"{C.BOLD}{C.YEL}▶ CONFIRM:{C.R} "
                          f"{prompt}  {C.BOLD}[y/N]{C.R}  "
                          f"{C.DIM}(10 s timeout){C.R}")
                ans = read_key(10.0)
                return ans in ("y", "Y")

            while True:
                now = time.monotonic()
                if now - last_render >= args.interval:
                    prev_report = report
                    report = build_report(peers, pollers)
                    now_wall = time.time()
                    ages = [now_wall - e["sampled_at"]
                            for e in report.values() if e["sampled_at"]]
                    data_age = max(ages) if ages else 0.0
                    do_render(status)
                    last_render = now

                    # Re-apply cbreak ONLY if something clobbered it. Blindly
                    # calling tty.setcbreak() here ate keypresses: it defaults
                    # to when=TCSAFLUSH, which discards pending input
                    # (field-observed 2026-07-08). Check first; if we must
                    # re-apply, TCSANOW keeps the buffered keystroke.
                    if term.cbreak_active:
                        try:
                            mode = termios.tcgetattr(term.fd)
                            if mode[3] & (termios.ECHO | termios.ICANON):
                                mode[3] &= ~(termios.ECHO | termios.ICANON)
                                mode[6][termios.VMIN] = 1
                                mode[6][termios.VTIME] = 0
                                termios.tcsetattr(term.fd, termios.TCSANOW,
                                                  mode)
                        except (termios.error, OSError):
                            pass

                key = read_key(0.1)
                if not key:
                    continue

                if key in ("q", "Q"):
                    break
                if key in ("b", "B"):
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    do_render(f"{C.YEL}firing burst on {len(peers)} peer(s) …{C.R}")
                    ok = fire_burst(args.user, peers)
                    status = (f"{C.GRN}burst fired at {ts} UTC "
                              f"({ok}/{len(peers)} ok){C.R}")
                elif key in ("r", "R"):
                    if confirm(f"start recording on {len(peers)} peer(s)?"):
                        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                        do_render(f"{C.YEL}starting record_sensors.py on "
                                  f"{len(peers)} peer(s) …{C.R}")
                        ok = fire_record_start(args.user, peers)
                        status = (f"{C.GRN}recording start requested at "
                                  f"{ts} UTC ({ok}/{len(peers)} launched){C.R}")
                    else:
                        status = f"{C.DIM}recording start cancelled{C.R}"
                elif key in ("s", "S"):
                    if confirm(f"stop recording on {len(peers)} peer(s)?"):
                        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                        do_render(f"{C.YEL}sending SIGINT to record_sensors.py on "
                                  f"{len(peers)} peer(s) …{C.R}")
                        ok = fire_record_stop(args.user, peers)
                        status = (f"{C.GRN}stop requested at "
                                  f"{ts} UTC ({ok}/{len(peers)} ok){C.R}")
                    else:
                        status = f"{C.DIM}recording stop cancelled{C.R}"
                else:
                    # Any other keypress: echo it. This is a signal for the
                    # user that keys ARE getting through — the alternative
                    # (silent no-op) makes it impossible to tell whether the
                    # TUI is broken or the keystroke was a typo.
                    kview = repr(key) if not key.isprintable() else key
                    status = (f"{C.DIM}key '{kview}' has no binding — "
                              f"use [b] [r] [s] [q]{C.R}")
                # Show the outcome of the action immediately rather than
                # waiting for the next refresh tick.
                do_render(status)
                last_render = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        print()


if __name__ == "__main__":
    main()
