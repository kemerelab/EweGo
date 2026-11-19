"""Unit tests for the web console (Firmware/webtest/ewego_webtest.py).

Stdlib only, no hardware: systemctl and every other command go through the
module's sh(), which is replaced by a fake systemd; live files are fixtures
in a temporary EWEGO_LIVE_DIR.

    python3 -m unittest discover -s tests -v
"""

import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_LIVE = tempfile.mkdtemp(prefix="ewego-live-")
os.environ["EWEGO_LIVE_DIR"] = _LIVE
os.environ.setdefault("RUNTIME_DIRECTORY", tempfile.mkdtemp(prefix="ewego-run-"))

_spec = importlib.util.spec_from_file_location("ewego_webtest", ROOT / "Firmware/webtest/ewego_webtest.py")
W = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(W)
W.Handler.log_message = lambda self, *a: None     # keep the test output readable


class FakeSystemd:
    """Stands in for sh(): answers systemctl is-active/is-enabled/start/stop
    from a dict of unit states, records every command."""

    def __init__(self, states=None):
        self.states = dict(states or {})
        self.calls = []
        self.start_fails = set()     # units whose start fails (ExecStartPre refusal etc.)

    def __call__(self, argv, timeout=15, merge=True):
        self.calls.append(list(argv))
        if argv[0] == "systemctl":
            verb, units = argv[1], argv[2:]
            if verb == "is-active":
                return 0, "".join(self.states.get(u, "inactive") + "\n" for u in units)
            if verb == "is-enabled":
                return 0, "".join("disabled\n" for _ in units)
            if verb in ("start", "restart"):
                rc = 0
                for u in units:
                    if u in self.start_fails:
                        self.states[u] = "failed"
                        rc = 1
                    else:
                        self.states[u] = "active"
                return rc, "" if rc == 0 else "Job failed. See journalctl.\n"
            if verb == "stop":
                for u in units:
                    self.states[u] = "inactive"
                return 0, ""
        return 0, ""

    def ran(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSystemd()
        self._orig_sh = W.sh
        W.sh = self.fake
        W._LIVE_CACHE.clear()
        W.PREVIEW.__init__()
        self.live = Path(_LIVE)
        for f in self.live.iterdir():
            f.unlink()
        self._tmp = tempfile.TemporaryDirectory(prefix="ewego-root-")
        self._orig_ewego = W.EWEGO
        W.EWEGO = Path(self._tmp.name)

    def tearDown(self):
        W.sh = self._orig_sh
        W.EWEGO = self._orig_ewego
        self._tmp.cleanup()
        for lock in W.LOCKS.values():
            if lock.locked():
                lock.release()


class PreviewUnits(Base):
    """R19: the preview is four systemd units driven by systemctl."""

    def test_start_starts_the_four_units_and_holds_the_camera_lock(self):
        ok, msg = W.PREVIEW.start()
        self.assertTrue(ok, msg)
        starts = self.fake.ran("systemctl", "start")
        self.assertEqual(len(starts), 1)
        self.assertEqual(sorted(starts[0][2:]), sorted(W.PREVIEW_UNITS.values()))
        self.assertTrue(W.PREVIEW.status(fresh=True)["running"])
        self.assertTrue(W.LOCKS["camera"].locked())
        self.assertTrue(W.PREVIEW.stop())
        self.assertFalse(W.LOCKS["camera"].locked())
        self.assertTrue(all(self.fake.states[u] == "inactive" for u in W.PREVIEW_UNITS.values()))

    def test_refused_while_a_recorder_is_active_or_activating(self):
        for state in ("active", "activating"):
            self.fake.states = {"ewego-imu-uart": state}
            ok, msg = W.PREVIEW.start()
            self.assertFalse(ok)
            self.assertIn("ewego-imu-uart", msg)
            self.assertEqual(self.fake.ran("systemctl", "start"), [])
            # the recorder was not touched
            self.assertEqual(self.fake.ran("systemctl", "stop"), [])
            self.assertFalse(W.LOCKS["camera"].locked())

    def test_all_units_failing_reports_failure_and_releases_the_lock(self):
        self.fake.start_fails = set(W.PREVIEW_UNITS.values())
        ok, msg = W.PREVIEW.start()
        self.assertFalse(ok)
        self.assertIn("journalctl", msg)
        self.assertFalse(W.LOCKS["camera"].locked())

    def test_partial_start_names_what_is_missing(self):
        self.fake.start_fails = {"ewego-preview-imu-usb"}
        ok, msg = W.PREVIEW.start()
        self.assertTrue(ok)
        self.assertIn("imu", msg)

    def test_reaper_stops_after_idle_and_notices_external_stop(self):
        W.PREVIEW.start()
        W.PREVIEW.last_seen -= W.Preview.IDLE_S + 1
        W.PREVIEW.reap_once()
        self.assertFalse(W.PREVIEW.status(fresh=True)["running"])
        self.assertIn("without a live-view client", W.PREVIEW.message)
        self.assertFalse(W.LOCKS["camera"].locked())
        # stopped behind the console's back (a recorder's ExecStartPre)
        W.PREVIEW.start()
        for u in W.PREVIEW_UNITS.values():
            self.fake.states[u] = "inactive"
        W.PREVIEW.reap_once()
        self.assertFalse(W.LOCKS["camera"].locked())
        self.assertIn("ended", W.PREVIEW.message)

    def test_recorder_start_stops_preview_first(self):
        W.PREVIEW.start()
        self.fake.calls.clear()
        h = make_handler("POST", "/api/unit/ewego-record.target/start", {"X-EweGo": "1"})
        h.do_POST()
        stops = [i for i, c in enumerate(self.fake.calls) if c[:2] == ["systemctl", "stop"]]
        start = [i for i, c in enumerate(self.fake.calls) if c == ["systemctl", "start", "ewego-record.target"]]
        self.assertTrue(stops and start and stops[0] < start[0], self.fake.calls)

    def test_preview_units_ship_with_asymmetric_exclusion(self):
        for name, rec in (("ewego-preview@.service", "ewego-cam@%i.service"),
                          ("ewego-preview-imu-usb.service", "ewego-imu-usb.service"),
                          ("ewego-preview-imu-uart.service", "ewego-imu-uart.service")):
            txt = (ROOT / "image/units" / name).read_text()
            self.assertNotIn("\nConflicts=", txt, name)
            self.assertIn("ExecStartPre=", txt, name)
            self.assertIn(rec, txt, name)
            self.assertIn("--preview", txt, name)
            self.assertIn("RuntimeMaxSec=", txt, name)
            self.assertIn("[Install]", txt, name)


class BusyRefusal(Base):
    """R20: tests and the bridge refuse while a unit owns the device; unit
    start refuses while a console test or the bridge holds it."""

    def run_test(self, name, qs=""):
        h = make_handler("GET", f"/api/run/{name}?seconds=1{qs}", {"X-EweGo": "1"})
        h.do_GET()
        return reply(h)

    def test_device_tests_refused_in_every_busy_state(self):
        for test, unit in (("imu", "ewego-imu-uart"), ("imu", "ewego-preview-imu-uart"),
                           ("gps-raw", "ewego-gps"), ("dualcam", "ewego-cam@camera2"),
                           ("camrec", "ewego-preview@camera1"), ("audio", "ewego-audio")):
            for state in ("active", "activating", "reloading", "deactivating"):
                self.fake.states = {unit: state}
                code, body = self.run_test(test)
                self.assertEqual(code, 409, (test, unit, state, body))
                self.assertIn(unit, body["error"])
                self.assertIn(state, body["error"])
                self.assertFalse(any(l.locked() for l in W.LOCKS.values()))

    def test_failed_unit_does_not_block(self):
        self.fake.states = {"ewego-gps": "failed"}
        self.assertEqual(W.device_owners("gps"), [])

    def test_bridge_refused_while_gps_logger_activating(self):
        self.fake.states = {"ewego-gps": "activating"}
        h = make_handler("POST", "/api/bridge/start", {"X-EweGo": "1"})
        h.do_POST()
        code, body = reply(h)
        self.assertEqual(code, 409)
        self.assertFalse(body["ok"])
        self.assertIn("ewego-gps", body["message"])

    def test_unit_start_refused_while_a_test_holds_the_device(self):
        W.LOCKS["imu"].acquire()
        for unit in ("ewego-imu-uart", "ewego-record.target"):
            h = make_handler("POST", f"/api/unit/{unit}/start", {"X-EweGo": "1"})
            h.do_POST()
            code, body = reply(h)
            self.assertEqual(code, 409, unit)
            self.assertIn("imu test", body["message"])
        self.assertEqual(self.fake.ran("systemctl", "start"), [])
        # stopping is always allowed, and other units are not affected
        h = make_handler("POST", "/api/unit/ewego-imu-uart/stop", {"X-EweGo": "1"})
        h.do_POST()
        self.assertEqual(reply(h)[0], 200)
        h = make_handler("POST", "/api/unit/ewego-fuel/start", {"X-EweGo": "1"})
        h.do_POST()
        self.assertEqual(reply(h)[0], 200)

    def test_unit_start_refused_while_bridge_holds_gps(self):
        W.LOCKS["gps"].acquire()
        orig = W.BRIDGE.status
        W.BRIDGE.status = lambda: {"running": True}
        try:
            h = make_handler("POST", "/api/unit/ewego-gps/start", {"X-EweGo": "1"})
            h.do_POST()
            code, body = reply(h)
        finally:
            W.BRIDGE.status = orig
        self.assertEqual(code, 409)
        self.assertIn("bridge", body["message"])

    def test_preview_camera_lock_does_not_block_recording(self):
        W.PREVIEW.start()
        h = make_handler("POST", "/api/unit/ewego-record.target/start", {"X-EweGo": "1"})
        h.do_POST()
        self.assertEqual(reply(h)[0], 200)
        self.assertFalse(W.LOCKS["camera"].locked())

    def test_ebusy_reads_as_owned_by_the_recorder(self):
        import errno
        msg = W.serial_open_error("/dev/ttyAMA5", OSError(errno.EBUSY, "Device or resource busy"))
        self.assertIn("owned by the recorder", msg)


class PostHeader(Base):
    """R21: POSTs (and /api/run tests) need X-EweGo: 1."""

    POSTS = ["/api/unit/ewego-record.target/start", "/api/unit/ewego-gps/stop",
             "/api/preview/start", "/api/preview/stop", "/api/power/poweroff",
             "/api/power/reboot", "/api/bridge/start", "/api/bridge/stop", "/api/nothing"]

    def test_post_without_header_is_403_and_does_nothing(self):
        for path in self.POSTS:
            for hdr in ({}, {"X-EweGo": "0"}, {"Origin": "http://evil.example"}):
                h = make_handler("POST", path, hdr)
                h.do_POST()
                code, body = reply(h)
                self.assertEqual(code, 403, (path, hdr))
                self.assertIn("X-EweGo", body["error"])
        self.assertEqual(self.fake.calls, [])

    def test_post_with_header_is_handled(self):
        h = make_handler("POST", "/api/unit/ewego-gps/start", {"X-EweGo": "1"})
        h.do_POST()
        code, body = reply(h)
        self.assertEqual(code, 200)
        self.assertEqual(body["status"]["active"], "active")
        self.assertTrue(self.fake.ran("systemctl", "start", "ewego-gps"))
        h = make_handler("POST", "/api/preview/stop", {"X-EweGo": "1"})
        h.do_POST()
        self.assertEqual(reply(h)[0], 200)

    def test_run_needs_header_status_does_not(self):
        h = make_handler("GET", "/api/run/uvc-quirk?on=1")
        h.do_GET()
        self.assertEqual(reply(h)[0], 403)
        self.assertEqual(self.fake.calls, [])
        h = make_handler("GET", "/api/sessions")
        h.do_GET()
        self.assertEqual(reply(h)[0], 200)

    def test_every_page_post_sends_the_header(self):
        import re
        for page in (W.PAGE, W.LIVE_PAGE):
            for m in re.finditer(r"fetch\([^;]*method: 'POST'[^;]*", page):
                self.assertIn("headers:", m.group(0))
            for m in re.finditer(r"fetch\('/api/run/[^;]*", page):
                self.assertIn("headers:", m.group(0))


ALL_UP = {"ewego-record.target": "active", "ewego-cam@camera1": "active", "ewego-cam@camera2": "active",
          "ewego-imu-usb": "active", "ewego-imu-uart": "active"}


class Badge(Base):
    """R22: RECORDING means every recorder is active with fresh live JSON
    and no writer_error."""

    def write_live(self, name, age=0.2, **extra):
        p = self.live / f"{name}.json"
        p.write_text(json.dumps(dict({"preview": 0}, **extra)))
        t = time.time() - age
        os.utime(p, (t, t))

    def all_fresh(self):
        for n in ("camera1", "camera2", "imu", "imu-uart"):
            self.write_live(n)

    def snap(self):
        W._LIVE_CACHE.clear()
        return W.live_snapshot()

    def test_all_healthy_is_recording(self):
        self.fake.states = dict(ALL_UP)
        self.all_fresh()
        s = self.snap()
        self.assertTrue(s["recording"])
        self.assertTrue(s["recording_ok"])
        self.assertEqual(s["recording_label"], "RECORDING")
        self.assertTrue(all(r["ok"] for r in s["recorders"].values()))
        json.dumps(s)    # the SSE stream serialises it

    def test_idle(self):
        s = self.snap()
        self.assertFalse(s["recording"])
        self.assertEqual(s["recording_label"], "IDLE")

    def test_failed_member_while_target_active(self):
        self.fake.states = dict(ALL_UP, **{"ewego-cam@camera2": "failed"})
        self.all_fresh()
        s = self.snap()
        self.assertTrue(s["recording"])
        self.assertFalse(s["recording_ok"])
        self.assertEqual(s["recording_label"], "RECORDING (camera2 down)")
        self.assertEqual(s["recorders"]["camera2"]["problem"], "failed")

    def test_stale_live_json(self):
        self.fake.states = dict(ALL_UP)
        self.all_fresh()
        self.write_live("imu-uart", age=5)
        s = self.snap()
        self.assertEqual(s["recording_label"], "RECORDING (imu-uart down)")
        self.assertIn("old", s["recorders"]["imu-uart"]["problem"])

    def test_missing_live_json(self):
        self.fake.states = dict(ALL_UP)
        self.all_fresh()
        (self.live / "imu.json").unlink()
        s = self.snap()
        self.assertEqual(s["recording_label"], "RECORDING (imu-usb down)")

    def test_writer_error(self):
        self.fake.states = dict(ALL_UP)
        self.all_fresh()
        self.write_live("camera1", writer_error="ENOSPC")
        self.write_live("camera2", writer_error=None)     # present but null: fine
        s = self.snap()
        self.assertEqual(s["recording_label"], "RECORDING (camera1 down)")
        self.assertIn("ENOSPC", s["recorders"]["camera1"]["problem"])
        self.assertTrue(s["recorders"]["camera2"]["ok"])

    def test_activating_reads_starting_and_several_listed(self):
        self.fake.states = dict(ALL_UP, **{"ewego-imu-uart": "activating", "ewego-cam@camera1": "inactive"})
        self.all_fresh()
        s = self.snap()
        self.assertFalse(s["recording_ok"])
        self.assertEqual(s["recording_label"], "RECORDING (camera1 down, imu-uart starting)")

    def test_single_recorder_without_target(self):
        self.fake.states = {"ewego-imu-usb": "active"}
        self.write_live("imu")
        s = self.snap()
        self.assertTrue(s["recording"])
        self.assertFalse(s["recording_ok"])


# ---------------------------------------------------------------------------
# a Handler without a socket: enough to call do_GET/do_POST and read the reply
# ---------------------------------------------------------------------------

class _Sock:
    def __init__(self):
        self.closed = False

    def settimeout(self, t):
        pass


def make_handler(method, path, headers=None):
    import email.message
    import io
    h = W.Handler.__new__(W.Handler)
    h.command, h.path, h.request_version = method, path, "HTTP/1.1"
    h.requestline = f"{method} {path} HTTP/1.1"
    h.client_address = ("127.0.0.1", 1)
    h.server = None
    h.connection = _Sock()
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    h.headers = msg
    h.wfile = io.BytesIO()
    h.rfile = io.BytesIO()
    h.close_connection = True
    return h


def reply(h):
    """(status code, json or text body) of what the handler wrote."""
    raw = h.wfile.getvalue()
    head, _, body = raw.partition(b"\r\n\r\n")
    code = int(head.split(b" ", 2)[1])
    try:
        return code, json.loads(body)
    except ValueError:
        return code, body.decode(errors="replace")


if __name__ == "__main__":
    unittest.main()
