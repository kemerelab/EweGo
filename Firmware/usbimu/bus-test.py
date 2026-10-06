#!/usr/bin/env python3
"""
bus-test.py — run the hub_imu IMU stream and USB camera capture at the same
time and report drops. Standard library only.

    python3 bus-test.py                       # IMU + every camera found, 15 s
    python3 bus-test.py --seconds 30 --cams 0 # IMU only
    python3 bus-test.py --cams 1              # IMU + first camera
    python3 bus-test.py --list

macOS: the IMU is read through IOKit (ctypes, nothing to install); cameras
are captured with ffmpeg's avfoundation input if ffmpeg is on the PATH.
NOTE: macOS has the stock USB stack, so two of the collar cameras will not
both start on one bus (the "not enough bandwidth" wall); expect the second
to fail here. The collar, with the patched uvcvideo, is where the
two-camera test is meaningful.

Linux (collar or any Pi): the IMU is read from /dev/hidraw*, cameras with
v4l2-ctl --stream-mmap (sequence-number drop accounting), the same way the
web console does it.

Pass criteria: IMU ~100 reports/s with 0 lost and 0 flagged; every camera
delivers its frames with 0 lost.
"""
import argparse
import ctypes
import ctypes.util
import os
import platform
import re
import select
import shutil
import struct
import subprocess
import sys
import threading
import time

VID, PID = 0x1209, 0x0001
RPT_LEN = 64
# macOS lists the laptop's own cameras, Continuity cameras and screen capture
# too; only external webcams are of interest (override with --cam-match).
CAM_MATCH = r"Webcam"


# ---------------------------------------------------------------------------
# IMU report accounting (shared by both platforms)
# ---------------------------------------------------------------------------

class ImuStats:
    def __init__(self):
        self.n = self.lost = self.flagged = self.bad = 0
        self.last_seq = None
        self.last_dev = None
        self.dts = []
        self.sample = None
        self.lock = threading.Lock()

    def feed(self, b):
        with self.lock:
            if len(b) != RPT_LEN or b[0] != 1:
                self.bad += 1
                return
            flags = b[1]
            seq = struct.unpack_from("<H", b, 2)[0]
            dev = struct.unpack_from("<I", b, 4)[0]
            if self.last_seq is not None:
                miss = (seq - self.last_seq - 1) & 0xFFFF
                self.lost += miss
                if miss == 0 and not (flags & 4):
                    self.dts.append((dev - self.last_dev) & 0xFFFFFFFF)
            self.last_seq, self.last_dev = seq, dev
            self.n += 1
            if flags:
                self.flagged += 1
            if not (flags & 3):
                self.sample = bytes(b)

    def reset(self):
        with self.lock:
            self.n = self.lost = self.flagged = self.bad = 0
            self.last_seq = self.last_dev = None
            self.dts = []

    def report(self, secs):
        with self.lock:
            lines = [f"IMU: {self.n} reports in {secs:.1f}s ({self.n / secs:.1f}/s), lost {self.lost}, "
                     f"flagged {self.flagged}, malformed {self.bad}"]
            if self.dts:
                lines.append(f"     device-clock interval min/mean/max {min(self.dts)}/{sum(self.dts) / len(self.dts):.0f}/"
                             f"{max(self.dts)} us (expect 10000)")
            if self.sample:
                bu = self.sample[8:54]
                s16 = lambda o: struct.unpack_from("<h", bu, o)[0]
                lines.append(f"     last sample: euler {s16(18) / 16:.1f}/{s16(20) / 16:.1f}/{s16(22) / 16:.1f} deg, "
                             f"gravity {s16(38) / 100:.2f}/{s16(40) / 100:.2f}/{s16(42) / 100:.2f}, temp "
                             f"{struct.unpack_from('<b', bu, 44)[0]} C, i2c {struct.unpack_from('<H', self.sample, 54)[0]} us")
            ok = self.n > 0 and self.lost == 0 and self.flagged == 0
            return ok, lines


# ---------------------------------------------------------------------------
# IMU readers
# ---------------------------------------------------------------------------

def imu_reader_linux(stats, stop, secs):
    dev = None
    for p in sorted(os.listdir("/sys/class/hidraw")) if os.path.isdir("/sys/class/hidraw") else []:
        try:
            with open(f"/sys/class/hidraw/{p}/device/uevent") as f:
                if f"HID_ID=0003:{VID:08X}:{PID:08X}" in f.read():
                    dev = f"/dev/{p}"
        except OSError:
            pass
    if not dev:
        print("IMU: no hub_imu hidraw device (firmware >= v0.3.0 plugged in? run as root?)")
        return
    print(f"IMU: {dev}")
    fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
    try:
        while not stop.is_set():
            r, _, _ = select.select([fd], [], [], 0.2)
            if r:
                try:
                    stats.feed(os.read(fd, RPT_LEN))
                except BlockingIOError:
                    pass
    finally:
        os.close(fd)


def imu_reader_macos(stats, stop, secs):
    """IOHIDManager via ctypes: match VID/PID, receive input reports."""
    cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
    iokit = ctypes.cdll.LoadLibrary(ctypes.util.find_library("IOKit"))
    CFTypeRef = ctypes.c_void_p
    cf.CFStringCreateWithCString.restype = CFTypeRef
    cf.CFStringCreateWithCString.argtypes = [CFTypeRef, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFNumberCreate.restype = CFTypeRef
    cf.CFNumberCreate.argtypes = [CFTypeRef, ctypes.c_int, ctypes.c_void_p]
    cf.CFDictionaryCreateMutable.restype = CFTypeRef
    cf.CFDictionaryCreateMutable.argtypes = [CFTypeRef, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
    cf.CFDictionarySetValue.argtypes = [CFTypeRef, CFTypeRef, CFTypeRef]
    cf.CFRunLoopGetCurrent.restype = CFTypeRef
    cf.CFRunLoopRunInMode.restype = ctypes.c_int32
    cf.CFRunLoopRunInMode.argtypes = [CFTypeRef, ctypes.c_double, ctypes.c_bool]
    iokit.IOHIDManagerCreate.restype = CFTypeRef
    iokit.IOHIDManagerCreate.argtypes = [CFTypeRef, ctypes.c_uint32]
    iokit.IOHIDManagerSetDeviceMatching.argtypes = [CFTypeRef, CFTypeRef]
    iokit.IOHIDManagerScheduleWithRunLoop.argtypes = [CFTypeRef, CFTypeRef, CFTypeRef]
    iokit.IOHIDManagerOpen.restype = ctypes.c_int
    iokit.IOHIDManagerOpen.argtypes = [CFTypeRef, ctypes.c_uint32]
    iokit.IOHIDManagerCopyDevices.restype = CFTypeRef
    iokit.IOHIDManagerCopyDevices.argtypes = [CFTypeRef]
    cf.CFSetGetCount.restype = ctypes.c_long
    cf.CFSetGetCount.argtypes = [CFTypeRef]

    REPORT_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
                                 ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint8), ctypes.c_long)
    iokit.IOHIDManagerRegisterInputReportCallback.argtypes = [CFTypeRef, REPORT_CB, ctypes.c_void_p]

    kCFNumberSInt32Type = 3
    kCFStringEncodingUTF8 = 0x08000100
    kCFAllocatorDefault = None
    kCFRunLoopDefaultMode = CFTypeRef.in_dll(cf, "kCFRunLoopDefaultMode")

    def cfstr(s):
        return cf.CFStringCreateWithCString(kCFAllocatorDefault, s.encode(), kCFStringEncodingUTF8)

    def cfnum(v):
        iv = ctypes.c_int32(v)
        return cf.CFNumberCreate(kCFAllocatorDefault, kCFNumberSInt32Type, ctypes.byref(iv))

    # kCFTypeDictionaryKeyCallBacks / ValueCallBacks are exported structs
    key_cb = ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")
    val_cb = ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryValueCallBacks")
    match = cf.CFDictionaryCreateMutable(kCFAllocatorDefault, 0, ctypes.addressof(key_cb), ctypes.addressof(val_cb))
    cf.CFDictionarySetValue(match, cfstr("VendorID"), cfnum(VID))
    cf.CFDictionarySetValue(match, cfstr("ProductID"), cfnum(PID))

    mgr = iokit.IOHIDManagerCreate(kCFAllocatorDefault, 0)
    iokit.IOHIDManagerSetDeviceMatching(mgr, match)

    def on_report(ctx, result, sender, rtype, rid, report, length):
        stats.feed(bytes(report[:length]))

    cb = REPORT_CB(on_report)   # keep a reference alive
    iokit.IOHIDManagerRegisterInputReportCallback(mgr, cb, None)
    iokit.IOHIDManagerScheduleWithRunLoop(mgr, cf.CFRunLoopGetCurrent(), kCFRunLoopDefaultMode)
    if iokit.IOHIDManagerOpen(mgr, 0) != 0:
        print("IMU: IOHIDManagerOpen failed")
        return
    devs = iokit.IOHIDManagerCopyDevices(mgr)
    n = cf.CFSetGetCount(devs) if devs else 0
    if n == 0:
        print(f"IMU: no HID device with VID:PID {VID:04x}:{PID:04x} (firmware >= v0.3.0? plugged in?)")
        return
    print(f"IMU: {n} hub_imu device(s) via IOKit")
    while not stop.is_set():
        cf.CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.2, False)


# ---------------------------------------------------------------------------
# cameras
# ---------------------------------------------------------------------------

def list_cameras():
    if platform.system() == "Darwin":
        if not shutil.which("ffmpeg"):
            return []
        p = subprocess.run(["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                           capture_output=True, text=True)
        cams, in_video = [], False
        for line in p.stderr.splitlines():
            if "video devices" in line:
                in_video = True
                continue
            if "audio devices" in line:
                in_video = False
            m = re.search(r"\[(\d+)\] (.*)$", line) if in_video else None
            if m and re.search(CAM_MATCH, m.group(2)):
                cams.append((m.group(1), m.group(2).strip()))
        return cams
    cams, seen = [], set()
    bp = "/dev/v4l/by-path"
    if os.path.isdir(bp):
        # udev makes two links per camera (…-usb-… and …-usbv2-…) to the same
        # node; keep one entry per device node
        for n in sorted(os.listdir(bp), key=lambda x: ("usbv2" in x, x)):
            if "usb" in n and n.endswith("video-index0"):
                dev = os.path.realpath(os.path.join(bp, n))
                if dev not in seen:
                    seen.add(dev)
                    cams.append((dev, n))
    return cams


cams_at_start = []


def camera_run(cam, secs, size, fps, result):
    """Capture from one camera; fill result dict with frames/lost/notes."""
    w, h = size.split("x")
    idx, name = cam
    result.update({"name": name, "frames": 0, "lost": 0, "fps": 0.0, "note": ""})
    if platform.system() == "Darwin":
        # ffmpeg decodes on the Mac; we only care that frames keep arriving.
        # avfoundation device indices can shift between invocations (Continuity
        # cameras come and go), so resolve the name to an index right now.
        # Same-named cameras are taken in listing order.
        fresh = [c for c in list_cameras() if c[1] == name]
        pos = [c[1] for c in cams_at_start].index(name) if name in [c[1] for c in cams_at_start] else 0
        same_before = sum(1 for c in cams_at_start[:cams_at_start.index(cam)] if c[1] == name)
        if len(fresh) > same_before:
            idx = fresh[same_before][0]
        # No -pixel_format: let AVFoundation pick the device format that matches
        # size and rate (the MJPEG 30 fps one) and decode it; forcing a raw
        # format selected the camera's 5 fps YUYV mode instead.
        argv = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", "-f", "avfoundation",
                "-framerate", str(fps), "-video_size", size,
                "-i", f"{idx}:none", "-t", str(secs), "-f", "null", "-"]
        p = subprocess.run(argv, capture_output=True, text=True)
        m = re.findall(r"frame=\s*(\d+)", p.stderr)
        result["frames"] = int(m[-1]) if m else 0
        result["fps"] = result["frames"] / secs
        err = [l for l in p.stderr.splitlines()
               if re.search(r"error|failed|Input/output|falling back", l, re.I)]
        if err:
            result["note"] = " | ".join(e.strip() for e in err[-2:])[:200]
        elif result["frames"] == 0:
            result["note"] = " | ".join(l.strip() for l in p.stderr.splitlines()[-3:])[:200]
        # ffmpeg's avfoundation input only offers the camera's raw (YUYV) formats,
        # which for the collar camera means 1080p at 5 fps (about 20 MB/s of
        # isochronous traffic, more than MJPEG 30 fps). So on macOS a camera
        # here is a bus load, not a frame-rate test: no sequence numbers, no
        # drop accounting. The collar's v4l2 path is where frames are counted.
        result["lost"] = None
        result["note"] = (result["note"] + " | " if result["note"] else "") + \
            "raw YUYV mode via avfoundation (MJPEG 30 fps not selectable): bus load only, frames informational"
        return
    argv = ["v4l2-ctl", "-d", idx, f"--set-fmt-video=width={w},height={h},pixelformat=MJPG",
            f"--set-parm={fps}", "--stream-mmap", "--stream-poll", f"--stream-count={secs * fps}",
            "--stream-to=/dev/null", "--verbose"]
    p = subprocess.run(argv, capture_output=True, text=True)
    seqs = [int(s) for s in re.findall(r"seq:\s*(\d+)", p.stdout + p.stderr)]
    result["frames"] = len(seqs)
    result["lost"] = sum((b - a - 1) for a, b in zip(seqs, seqs[1:]) if b > a + 1)
    result["fps"] = len(seqs) / secs
    if p.returncode or not seqs:
        result["note"] = (p.stderr.strip().splitlines() or ["no output"])[-1][:120]


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="hub_imu + USB camera bus test")
    ap.add_argument("--seconds", type=int, default=15)
    ap.add_argument("--cams", type=int, default=99, help="how many cameras to run (default all)")
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--stagger", type=float, default=1.0, help="seconds between camera starts")
    ap.add_argument("--cam-match", default=None, help="regex on camera names (macOS), default 'Webcam'")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    global CAM_MATCH
    if a.cam_match:
        CAM_MATCH = a.cam_match

    mac = platform.system() == "Darwin"
    cams = list_cameras()
    if a.list:
        for c in cams:
            print(f"camera {c[0]}: {c[1]}")
        if not cams:
            print("no cameras" + (" (ffmpeg not found)" if mac and not shutil.which("ffmpeg") else ""))
        return 0
    cams = cams[:a.cams]
    cams_at_start.extend(cams)
    if mac and len(cams) > 1:
        print("note: macOS has the stock USB stack; two collar cameras on one bus usually cannot both start here.")
    if mac and cams and not shutil.which("ffmpeg"):
        print("ffmpeg not on PATH: cameras skipped")
        cams = []

    stats = ImuStats()
    stop = threading.Event()
    reader = imu_reader_macos if mac else imu_reader_linux
    t_imu = threading.Thread(target=reader, args=(stats, stop, a.seconds), daemon=True)
    t_imu.start()
    time.sleep(1.0)

    results, threads = [], []
    for c in cams:
        r = {}
        results.append(r)
        t = threading.Thread(target=camera_run, args=(c, a.seconds, a.size, a.fps, r), daemon=True)
        threads.append(t)
        t.start()
        print(f"camera {c[0]} started ({c[1]})")
        time.sleep(a.stagger)

    stats.reset()                      # count from here, not from reader start
    t0 = time.monotonic()
    while time.monotonic() - t0 < a.seconds + a.stagger * len(cams):
        time.sleep(1)
        with stats.lock:
            print(f"  t={time.monotonic() - t0:4.0f}s  imu {stats.n} reports, {stats.lost} lost"
                  + "".join(f"  | cam{i + 1} running" for i in range(len(cams))), end="\r", flush=True)
    for t in threads:
        t.join(timeout=10)
    stop.set()
    t_imu.join(timeout=3)
    print()

    ok, lines = stats.report(time.monotonic() - t0)
    print("\n".join(lines))
    for i, r in enumerate(results):
        lost = r.get("lost", 0)
        cam_ok = r.get("frames", 0) > 0 and (lost is None or lost == 0)
        ok = ok and cam_ok
        print(f"cam{i + 1} ({r.get('name', '?')}): {r.get('frames', 0)} frames, {r.get('fps', 0):.1f} fps, "
              f"lost {'n/a' if lost is None else lost}" + (f"  [{r['note']}]" if r.get("note") else ""))
    if mac and results:
        print("(macOS: camera figures are bus load only; frame drop accounting needs the collar)")
    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
