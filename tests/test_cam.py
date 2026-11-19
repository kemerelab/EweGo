"""[R37] ewego-cam without a camera.

Only what runs with no V4L2 device: --help, argument validation, --list on
a machine without USB cameras, and a --device that never appears. The
writer/index logic itself needs a frame source that is not a camera:
TestCamReplay probes `--help` for a `--source` option (a recorded MJPEG
replayed through the writer path) and skips until that hook exists.

ewego-cam never opens a camera in these tests: --list only globs
/dev/v4l/by-path, and the device tests name paths that do not exist or are
not video devices (/dev/null).
"""

import struct
import time
import unittest

from helpers import RecorderTestCase, build_tool, read_json, run


class CamBase(RecorderTestCase):
    @classmethod
    def setUpClass(cls):
        cls.exe = build_tool("uvccam", "ewego-cam")
        cls.help_text = run([cls.exe, "--help"], timeout=10).stdout


class TestCamCli(CamBase):
    def test_help(self):
        r = run([self.exe, "--help"], timeout=10)
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage: ewego-cam --device", r.stdout)
        for opt in ("--wait", "--stall", "--live", "--preview", "--list", "--seconds"):
            self.assertIn(opt, r.stdout)

    def test_usage_errors_exit_1(self):
        out = self.tmp / "out"
        cases = {
            "no args": [],
            "no --out": ["--device", "/dev/video99"],
            "preview without --live": ["--device", "/dev/video99", "--preview"],
            "bad --size": ["--device", "/dev/video99", "--out", out, "--size", "big"],
            "--buffers too many": ["--device", "/dev/video99", "--out", out, "--buffers", "99"],
            "--buffers too few": ["--device", "/dev/video99", "--out", out, "--buffers", "1"],
            "--fps 0": ["--device", "/dev/video99", "--out", out, "--fps", "0"],
            "--ring 0": ["--device", "/dev/video99", "--out", out, "--ring", "0"],
            "bad auto spec": ["--device", "auto:0", "--out", out],
            "bad auto/M": ["--device", "auto:2/1", "--out", out],
            "unknown option": ["--bogus"],
        }
        for what, args in cases.items():
            with self.subTest(what):
                t0 = time.monotonic()
                r = run([self.exe, *args], timeout=10)
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertLess(time.monotonic() - t0, 3, "validation must not wait for a device")
        self.assertFalse(out.exists(), "nothing is created for a rejected command line")

    def test_list(self):
        r = run([self.exe, "--list"], timeout=10)
        self.assertEqual(r.returncode, 0)
        lines = r.stdout.strip().splitlines()
        if lines != ["no USB cameras"]:
            # a developer machine with a webcam: entries look like "auto:1 port:1.3 ..."
            self.assertTrue(all(ln.startswith("auto:") and " port:" in ln for ln in lines), r.stdout)

    def test_missing_device_exits_1_after_wait(self):
        out = self.tmp / "out"
        t0 = time.monotonic()
        r = run([self.exe, "--device", "/dev/video99", "--out", out, "--wait", "1", "--seconds", "1"], timeout=15)
        took = time.monotonic() - t0
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("no camera for /dev/video99 after 1 s", r.stderr)
        self.assertLess(took, 5)
        # Note (not asserted; lane A1's R6 moves directory creation after the
        # device open): today the wait happens before the session directory
        # is made, so this case leaves nothing under --out.

    def test_missing_hub_port_exits_1(self):
        # (auto:N is not tried here: on a machine with a webcam it would open it)
        r = run([self.exe, "--device", "port:9.9", "--out", self.tmp / "out", "--wait", "1"], timeout=15)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("no camera for port:9.9", r.stderr)

    def test_not_a_video_device_exits_1(self):
        """/dev/null exists but is no V4L2 device: VIDIOC_QUERYCAP fails, exit 1."""
        out = self.tmp / "out"
        r = run([self.exe, "--device", "/dev/null", "--out", out, "--wait", "1", "--seconds", "1"], timeout=15)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("VIDIOC_QUERYCAP", r.stderr)
        # Note (not asserted; R6 changes it): today the session directory is
        # created before the device is opened, so this leaves an empty
        # <out>/<stamp>/ behind.


class TestCamReplay(CamBase):
    """R37 proper: the frame/index/timestamp writer driven from a recorded MJPEG.

    Needs a hook in ewego-cam: `--source FILE.mjpeg` (replay frames at --fps
    through the normal queue/writer path instead of V4L2). Skips until
    `--help` lists it."""

    def setUp(self):
        super().setUp()
        if "--source" not in self.help_text:
            self.skipTest("ewego-cam has no --source replay mode yet (hook needed for R37)")

    @staticmethod
    def mjpeg(n):
        frames = [b"\xff\xd8" + bytes([i % 251]) * (1000 + 37 * i) + b"\xff\xd9" for i in range(n)]
        return frames, b"".join(frames)

    def test_replay_writes_stream_index_and_timestamps(self):
        frames, data = self.mjpeg(60)
        src = self.tmp / "in.mjpeg"
        src.write_bytes(data)
        out = self.tmp / "out"
        r = run([self.exe, "--device", "replay", "--source", src, "--out", out, "--no-session-dir",
                 "--name", "camera1", "--fps", "30", "--stats", "0"], timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((out / "camera1.mjpeg").read_bytes(), data)
        ts = (out / "camera1_timestamps.bin").read_bytes()
        self.assertEqual(len(ts), 8 * len(frames))
        t = struct.unpack(f"<{len(frames)}q", ts)
        self.assertTrue(all(b > a for a, b in zip(t, t[1:])))
        idx = (out / "camera1_index.bin").read_bytes()
        self.assertEqual(len(idx), 32 * len(frames))
        off = 0
        for i, f in enumerate(frames):
            o, tsu, seq, nbytes, _flags, _res = struct.unpack_from("<QqIIII", idx, 32 * i)
            self.assertEqual((o, nbytes, tsu), (off, len(f), t[i]))
            off += len(f)
        s = read_json(out / "camera1_summary.json")
        self.assertEqual((s["frames"], s["lost"], s["exit_code"]), (len(frames), 0, 0))


if __name__ == "__main__":
    unittest.main()
