# EweGo tests

Black-box tests for the recorders and tools, Python standard library only
(`unittest`), no hardware. CI runs them in the `check` job of
`.github/workflows/build-image.yml` (step "Tests"); a failing test fails the job.

## Running

From the repository root:

    python3 -m unittest discover -s tests -v

One file or one test:

    python3 -m unittest discover -s tests -p test_imu_uart.py -v
    cd tests && python3 -m unittest test_imu_uart.TestUartRecording.test_clean_run -v

The whole suite takes about a minute (most of it is the recorders running
for a few seconds each against fakes). It needs Python 3.10+ and, for the C
recorders, `make` and `gcc` on Linux. Nothing is installed and no real
device is touched.

## What is covered

| file | item | what |
|---|---|---|
| `test_imu_uart.py` + `fake_bno055.py` | R33 | `ewego-imu-uart` against a fake BNO055 on a pty: init, 100 Hz bursts, 0x07 / 0x0A / garbage replies, silence (exit 3), power-cycle recovery, reset into CONFIG mode |
| `test_imu_usb.py` | R34 | `ewego-imu-usb` fed hub_imu reports through a FIFO given as `--device`: clean stream with seq and clock wrap, gaps, flagged and malformed reports, stall, read error, writer closing |
| `test_check_tool.py` | R35 | `ewego-check.py` on synthetic session directories: PASS / WARN / FAIL per stream, missing / truncated / in-progress summaries, run grouping |
| `test_cam.py` | R37 | `ewego-cam` without a camera: CLI validation, `--list`, missing device; `--source FILE` replay through the writer/index path |
| `test_units.py` | R40 | the systemd unit files: preview `ExecStartPre` guards (run against a fake `systemctl`), recorder preview-stop / space-check / notify / watchdog lines |
| `test_webtest.py` | R36 | the web console with fixtures under `EWEGO_LIVE_DIR` and a faked `systemctl` |
| `test_loggers.py` | R26, R29 | fuel-gauge low-battery logic, GPS logger error streaks, with `serial`/`pyubx2`/`smbus2` stubbed |

`fake_bno055.py` also runs standalone (`python3 tests/fake_bno055.py --help`)
and prints a pty path to give `ewego-imu-uart --port`.

## Conventions (for every lane adding tests)

- **Stdlib only.** No pytest, no pip, no apt. Tests must pass on the
  `ubuntu-24.04` runner (Python 3.12) and on Python 3.14.
- **Files:** `tests/test_<thing>.py`; shared code in `tests/helpers.py`
  (import it as `from helpers import ...`; discovery puts `tests/` on
  `sys.path`). Put a module docstring at the top saying what is tested and
  which ROBUSTNESS.md item it belongs to.
- **Build what you test, out of tree.** `helpers.build_tool("usbimu",
  "ewego-imu-usb")` runs the project Makefile (`make CC=gcc`) on a copy of
  `Firmware/<dir>` in a temp directory and returns the binary's path, built
  once per test run. Never build in `Firmware/` itself: the recorders'
  binaries are not in `.gitignore`, and the CI compile step `make clean`s
  before the tests run anyway.
- **Temp files only.** Use `helpers.RecorderTestCase` (fresh `self.tmp` per
  test, removed afterwards) or `tempfile`; never write into the repository,
  `/opt/ewego` or `/run/ewego`.
- **Processes:** start programs with `self.start(argv)` (output to files, so
  pipes cannot block them; killed at cleanup) and always wait with a timeout
  (`p.wait(timeout)` sends SIGTERM, then SIGKILL, and sets `p.killed`). A
  hang must become a failure, not a stuck CI job.
- **Keep it fast:** a few seconds per test at most (`--seconds 2`,
  `--stall 2`); the suite should stay under two minutes on a runner.
- **No real devices.** Fakes only: ptys, FIFOs, temp directories. Do not
  open `/dev/video*`, `/dev/hidraw*`, serial ports or I2C buses, and avoid
  options that may pick a real device on a developer machine (e.g.
  `ewego-cam --device auto:N`).
- **No root.** Never pass `--rt`.

## Skip policy

A test skips (with the reason in the output) only when the machine cannot
run it:

- the C recorders are Linux programs: on macOS and other systems their tests
  skip;
- `make` or `gcc` missing, or the build failing for a platform reason (a
  missing system header such as `linux/videodev2.h`, no static libc) skips;
- a hook the test needs does not exist yet in the program (probe for it,
  e.g. look for the flag in `--help`, and `skipTest` naming the hook).

A compile error on Linux with the toolchain present is a **failure**, not a
skip. Never skip to hide a bug.

## Known-bad behaviour: `expectedFailure`

Behaviour that is wrong today and fixed by an open ROBUSTNESS.md item is
written as the test of the *desired* behaviour, marked
`@unittest.expectedFailure` with the item id in a comment. When the item
lands the test starts to pass, unittest reports it as an "unexpected
success" (which fails the run), and the marker must be removed in the same
change. There are no markers at the moment: the R14 and R16 tests became
plain tests when those items landed (2026-11-18).

Tests named `..._current_behaviour` (or with "CURRENT BEHAVIOUR" in the
docstring) pin down something that is not obviously right but is how the
program behaves today; change them deliberately when the behaviour changes.
None remain after R38 (ewego-check now groups restarts and reads in-progress
summaries).
