"""[R40] The preview / recorder exclusion and the free-space guard in image/units/.

Pure file parsing, plus running each preview unit's ExecStartPre shell guard
with a fake `systemctl` on PATH (no systemd, no devices):

- every preview unit (ewego-preview@, -imu-usb, -imu-uart) has an
  ExecStartPre that refuses (exit 1) while its recorder is active,
  activating, reloading, deactivating or refreshing, and lets it start when
  the recorder is inactive or failed; the busy states match the console's
  BUSY_STATES;
- every recorder unit with a preview (ewego-cam@, ewego-imu-usb,
  ewego-imu-uart) stops that preview first (`ExecStartPre=-/bin/systemctl
  stop ...`, the `-` so a missing preview unit is not an error) and carries
  `ExecCondition=/usr/local/bin/ewego-check-space` (a short card skips the
  start cleanly instead of a restart loop);
- audio and GPS have the ExecCondition too; the fuel logger is never gated
  (it only records the space, `ExecStartPre=-... 0`).
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

from helpers import REPO

UNITS = REPO / "image" / "units"
# preview unit -> (recorder unit as named in the preview's guard, the recorder's unit file, instance)
PAIRS = {
    "ewego-preview@.service": ("ewego-cam@%i.service", "ewego-cam@.service", "camera1"),
    "ewego-preview-imu-usb.service": ("ewego-imu-usb.service", "ewego-imu-usb.service", None),
    "ewego-preview-imu-uart.service": ("ewego-imu-uart.service", "ewego-imu-uart.service", None),
}
BUSY = ["active", "activating", "reloading", "deactivating", "refreshing"]
CHECK_SPACE = "/usr/local/bin/ewego-check-space"


def directives(path, key):
    """Values of every `key=` line in a unit file (no continuation lines are used in ours)."""
    out = []
    for line in path.read_text().splitlines():
        if line.startswith(key + "="):
            out.append(line[len(key) + 1:])
    return out


def console_busy_states():
    txt = (REPO / "Firmware" / "webtest" / "ewego_webtest.py").read_text()
    m = re.search(r"^BUSY_STATES = \(([^)]*)\)", txt, re.M)
    return re.findall(r'"([a-z]+)"', m.group(1))


class TestUnitFiles(unittest.TestCase):
    def test_recorders_stop_their_preview_and_check_space(self):
        for preview, (_, rec_file, _) in PAIRS.items():
            with self.subTest(recorder=rec_file):
                p = UNITS / rec_file
                pre = directives(p, "ExecStartPre")
                want = "-/bin/systemctl stop " + preview.replace("@.service", "@%i.service")
                self.assertIn(want, pre, f"{rec_file} lacks ExecStartPre={want}")
                cond = directives(p, "ExecCondition")
                self.assertEqual(len(cond), 1, cond)
                self.assertTrue(cond[0].startswith(CHECK_SPACE + " "), cond)
                self.assertEqual(directives(p, "Type"), ["notify"])
                self.assertEqual(directives(p, "WatchdogSec"), ["30"])
                # a space check in ExecStartPre would be restarted regardless of RestartPreventExitStatus
                self.assertFalse([x for x in pre if CHECK_SPACE in x], pre)

    def test_other_recorders_gated_fuel_never(self):
        for unit in ("ewego-audio.service", "ewego-gps.service"):
            with self.subTest(unit=unit):
                cond = directives(UNITS / unit, "ExecCondition")
                self.assertEqual(len(cond), 1)
                self.assertTrue(cond[0].startswith(CHECK_SPACE + " "))
        fuel = UNITS / "ewego-fuel.service"
        self.assertEqual(directives(fuel, "ExecCondition"), [])
        self.assertTrue(all(x.startswith("-") for x in directives(fuel, "ExecStartPre") if CHECK_SPACE in x))

    def test_preview_units_have_the_guard_and_no_conflicts(self):
        for preview, (rec, _, _) in PAIRS.items():
            with self.subTest(preview=preview):
                p = UNITS / preview
                self.assertEqual(directives(p, "Conflicts"), [])
                pre = directives(p, "ExecStartPre")
                self.assertEqual(len(pre), 1, pre)
                self.assertTrue(pre[0].startswith("/bin/sh -c '"), pre[0])
                self.assertIn(f"systemctl is-active {rec}", pre[0])
                self.assertIn("|".join(BUSY) + ")", pre[0])
                self.assertIn("exit 1", pre[0])

    def test_guard_states_match_the_console(self):
        self.assertEqual(console_busy_states(), BUSY)


@unittest.skipIf(shutil.which("sh") is None, "no /bin/sh")
class TestPreviewGuardLogic(unittest.TestCase):
    """Run each guard as systemd would (after `$$` -> `$` and `%i` expansion)
    against a fake systemctl that prints a chosen state."""

    def setUp(self):
        self.bin = tempfile.mkdtemp(prefix="ewego-units-")
        self.addCleanup(shutil.rmtree, self.bin, True)
        fake = os.path.join(self.bin, "systemctl")
        with open(fake, "w") as f:
            # records its arguments, prints the state from the environment
            f.write('#!/bin/sh\necho "$*" >> "$FAKE_LOG"\necho "$FAKE_STATE"\n')
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)
        self.log = os.path.join(self.bin, "log")

    def guard_script(self, preview, instance):
        pre = directives(UNITS / preview, "ExecStartPre")[0]
        m = re.fullmatch(r"/bin/sh -c '(.*)'", pre)
        self.assertIsNotNone(m, pre)
        script = m.group(1).replace("$$", "$")
        return script.replace("%i", instance) if instance else script

    def run_guard(self, script, state):
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ.get("PATH", ""),
                   FAKE_STATE=state, FAKE_LOG=self.log)
        return subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True, timeout=10)

    def test_guard_refuses_while_the_recorder_is_busy(self):
        for preview, (rec, _, inst) in PAIRS.items():
            script = self.guard_script(preview, inst)
            rec_name = rec.replace("%i", inst) if inst else rec
            for state in BUSY + ["inactive", "failed", "unknown"]:
                with self.subTest(preview=preview, state=state):
                    if os.path.exists(self.log):
                        os.unlink(self.log)
                    r = self.run_guard(script, state)
                    self.assertEqual(r.returncode, 1 if state in BUSY else 0, (state, r.stderr))
                    if state in BUSY:
                        self.assertIn("is running: no preview", r.stderr)
                    with open(self.log) as f:
                        self.assertEqual(f.read().split(), ["is-active", rec_name])


if __name__ == "__main__":
    unittest.main()
