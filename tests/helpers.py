"""Shared helpers for the EweGo black-box tests (stdlib only).

- build_tool(): compile a C recorder from Firmware/<dir> with the project's
  own Makefile (`make CC=gcc`) in a private copy of the source directory, so
  no build product ever lands in the git tree (the recorders' binaries are
  not covered by .gitignore). One build per tool per test process.
- RecorderTestCase: a TestCase with a fresh temp dir per test and helpers to
  start a program, wait for it with a timeout, and read its outputs.

Skip policy (see tests/README.md): a test skips, with the reason printed,
when the platform cannot run it (not Linux, no make/gcc, a system header or
static libc missing). A compile error on Linux with everything present is a
failure, not a skip.
"""

import atexit
import csv
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIRMWARE = REPO / "Firmware"

# Compiler/linker output that means "this machine lacks something", not "the code is broken".
_PLATFORM_ERRORS = re.compile(
    r"fatal error: [^\n]*: No such file or directory"   # a missing system header (linux/videodev2.h, ...)
    r"|cannot find -l\w+"                                # no static libc / libpthread
    r"|library not found"                                # macOS ld
    r"|unsupported option '-static'")

_build_root = None
_built = {}


def _build_dir():
    global _build_root
    if _build_root is None:
        _build_root = tempfile.mkdtemp(prefix="ewego-tests-build-")
        atexit.register(shutil.rmtree, _build_root, True)
    return _build_root


def require_linux(what="this test"):
    if not sys.platform.startswith("linux"):
        raise unittest.SkipTest(f"{what} needs Linux (platform is {sys.platform})")


def build_tool(subdir, binary):
    """Build Firmware/<subdir>/<binary> and return the path of the executable.

    Raises unittest.SkipTest for platform reasons, AssertionError when the
    build fails where it should work. All EweGo C tools are Linux programs
    (V4L2, hidraw, termios + CLOCK_MONOTONIC clock_nanosleep), so non-Linux skips."""
    key = (subdir, binary)
    cached = _built.get(key)
    if isinstance(cached, unittest.SkipTest):
        raise cached
    if cached:
        return cached
    try:
        require_linux(binary)
        for tool in ("make", "gcc"):
            if shutil.which(tool) is None:
                raise unittest.SkipTest(f"{tool} not found; cannot build {binary}")
        dst = Path(_build_dir()) / subdir
        shutil.copytree(FIRMWARE / subdir, dst, ignore=shutil.ignore_patterns(binary, "*.o"), dirs_exist_ok=True)
        r = subprocess.run(["make", "-C", str(dst), "CC=gcc"], capture_output=True, text=True, timeout=300)
        out = (r.stdout + r.stderr).strip()
        exe = dst / binary
        if r.returncode != 0 or not exe.exists():
            m = _PLATFORM_ERRORS.search(out)
            if m:
                raise unittest.SkipTest(f"{binary} does not build here ({m.group(0)})")
            raise AssertionError(f"make -C Firmware/{subdir} CC=gcc failed:\n{out[-3000:]}")
    except unittest.SkipTest as e:
        _built[key] = e
        raise
    _built[key] = exe
    return exe


def read_csv(path):
    """(header, rows) of a CSV file."""
    with open(path, newline="") as f:
        r = csv.reader(f)
        header = next(r)
        return header, list(r)


def read_json(path):
    with open(path) as f:
        return json.load(f)


class Proc:
    """A started program; stdout/stderr go to files so no pipe can fill up and block it."""

    def __init__(self, argv, workdir, label="proc"):
        self.argv = [str(a) for a in argv]
        self.out_path = Path(workdir) / f"{label}.stdout"
        self.err_path = Path(workdir) / f"{label}.stderr"
        self._out = open(self.out_path, "wb")
        self._err = open(self.err_path, "wb")
        self.p = subprocess.Popen(self.argv, stdout=self._out, stderr=self._err, stdin=subprocess.DEVNULL)
        self.killed = False      # set when wait() had to stop it

    def poll(self):
        return self.p.poll()

    def wait(self, timeout):
        """Wait up to timeout s; past that, SIGTERM (the recorders' clean stop),
        then SIGKILL after 5 s more. Returns the exit status."""
        try:
            rc = self.p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.killed = True
            self.p.send_signal(signal.SIGTERM)
            try:
                rc = self.p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.p.kill()
                rc = self.p.wait()
        self._close()
        return rc

    def terminate(self):
        if self.p.poll() is None:
            self.p.send_signal(signal.SIGTERM)

    def _close(self):
        for f in (self._out, self._err):
            if not f.closed:
                f.close()

    @property
    def stdout(self):
        return self.out_path.read_text(errors="replace")

    @property
    def stderr(self):
        return self.err_path.read_text(errors="replace")

    def cleanup(self):
        if self.p.poll() is None:
            self.p.kill()
            self.p.wait()
        self._close()


def run(argv, timeout=30, **kw):
    """subprocess.run with captured text output and a timeout (a hang raises TimeoutExpired)."""
    return subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, **kw)


class RecorderTestCase(unittest.TestCase):
    """A fresh temp dir per test (self.tmp, a Path) and process bookkeeping."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ewego-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def start(self, argv, label="proc"):
        p = Proc(argv, self.tmp, label)
        self.addCleanup(p.cleanup)
        return p

    @staticmethod
    def diag(p):
        """Context for assertion messages."""
        return f"\n--- argv: {' '.join(p.argv)}\n--- stderr (tail):\n{p.stderr[-3000:]}"

    @staticmethod
    def session_dir(out):
        """The single timestamped session directory under out."""
        ds = sorted(d for d in Path(out).iterdir() if d.is_dir())
        if len(ds) != 1:
            raise AssertionError(f"expected one session directory under {out}, found {ds}")
        return ds[0]

    @staticmethod
    def wait_for(pred, timeout, interval=0.02):
        """Poll pred() until it returns something truthy (returned) or timeout (None)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            v = pred()
            if v:
                return v
            time.sleep(interval)
        return None


def summary_check(tc, summary, keys):
    """Assert every key is present in a summary dict."""
    missing = [k for k in keys if k not in summary]
    tc.assertFalse(missing, f"summary lacks {missing}: {sorted(summary)}")


def env_without(*names):
    return {k: v for k, v in os.environ.items() if k not in names}
