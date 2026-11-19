#!/usr/bin/env python3
"""Log the MAX17048 fuel gauge to CSV until stopped (unit ewego-fuel).

Writes <out>/<YYYYmmdd_HHMMSS>/fuel_gauge.csv with the same columns that
sensor_test.py used (Timestamp, Datetime, Elapsed_Seconds, Voltage_V,
SOC_Percent, Alert) by calling max17048_test.run_continuous_monitoring.

Low-battery shutdown (the wearable's guard against the battery dying
mid-write, which corrupts the SD card: Firmware/bugs/001): when the charge
is below --shutdown-soc AND the cell voltage is below --shutdown-voltage
for --shutdown-samples consecutive readings, it writes a line to events.log
in the run directory and to the journal, then runs `systemctl poweroff`,
which stops ewego-record.target through the normal shutdown ordering so
every recorder closes its files. A single reading above either threshold
resets the count; failed readings neither count nor reset it.
--no-shutdown (SHUTDOWN=0 in /etc/ewego/fuel.conf) disables it on the bench.

Quiet by default: one line per --print-every seconds (and on any change of
sensor or alert state) instead of one per reading, so the journal on the SD
card is not written every two seconds.
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

TEN_YEARS = 315360000

DEFAULT_SHUTDOWN_SOC = 8.0        # percent
DEFAULT_SHUTDOWN_VOLTAGE = 3.5    # volts
DEFAULT_SHUTDOWN_SAMPLES = 30     # consecutive readings (60 s at --interval 2)


def low_battery_count(count, voltage, soc, soc_min, voltage_min):
    """Return the new count of consecutive low-battery readings.

    A reading counts as low only when both the charge and the voltage are
    below their thresholds; any good reading resets the count to 0; a failed
    reading (None) leaves it unchanged, so a flaky I2C bus can neither
    trigger a shutdown nor postpone one forever.
    """
    if voltage is None or soc is None:
        return count
    if soc < soc_min and voltage < voltage_min:
        return count + 1
    return 0


class LowBatteryGuard:
    """Feeds readings to low_battery_count and powers off once."""

    def __init__(self, soc_min, voltage_min, samples, enabled=True,
                 events_path=None, poweroff=None):
        self.soc_min = soc_min
        self.voltage_min = voltage_min
        self.samples = samples
        self.enabled = enabled
        self.events_path = events_path
        self.poweroff = poweroff or request_poweroff
        self.count = 0
        self.triggered = False

    def __call__(self, voltage, soc, alert=None):
        """Callback for run_continuous_monitoring; True stops the loop."""
        prev = self.count
        self.count = low_battery_count(self.count, voltage, soc, self.soc_min, self.voltage_min)
        if self.count == 1 and prev == 0:
            print(f"low battery: SOC {soc:.1f}% < {self.soc_min:g}% and {voltage:.3f} V < "
                  f"{self.voltage_min:g} V; {'powering off' if self.enabled else 'would power off (shutdown disabled)'} "
                  f"after {self.samples} consecutive readings", flush=True)
        elif self.count == 0 and prev > 0:
            print(f"low battery cleared after {prev} reading(s)", flush=True)
        if self.triggered or self.count < self.samples:
            return False
        self.triggered = True
        msg = (f"low battery: SOC {soc:.2f}% and {voltage:.4f} V for {self.count} consecutive readings "
               f"(thresholds {self.soc_min:g}% / {self.voltage_min:g} V)")
        if not self.enabled:
            self.event(msg + "; shutdown disabled, still logging")
            return False
        self.event(msg + "; powering off")
        self.poweroff()
        return False   # keep logging until systemd stops us

    def event(self, msg):
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        if self.events_path:
            try:
                with open(self.events_path, "a") as f:
                    f.write(line + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            except OSError as e:
                print(f"could not write {self.events_path}: {e}", flush=True)


def request_poweroff():
    try:
        r = subprocess.run(["systemctl", "poweroff"], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            print(f"systemctl poweroff failed (rc {r.returncode}): {r.stderr.strip()}", flush=True)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"systemctl poweroff failed: {e}", flush=True)


def main():
    p = argparse.ArgumentParser(description="MAX17048 fuel gauge CSV logger")
    p.add_argument("--out", default="/opt/ewego/recordings/fuel",
                   help="parent directory; one timestamped subdirectory per run")
    p.add_argument("--interval", type=float, default=2.0, help="seconds between readings")
    p.add_argument("--bus", type=int, default=1, help="I2C bus number")
    p.add_argument("--flat", action="store_true", help="write into --out itself, no timestamped subdirectory")
    p.add_argument("--print-every", type=float, default=60.0,
                   help="seconds between printed readings (0 = every reading); state changes always print")
    p.add_argument("--shutdown", type=int, choices=(0, 1), default=1,
                   help="1 = power off on low battery (default), 0 = only log it (bench)")
    p.add_argument("--no-shutdown", dest="shutdown", action="store_const", const=0,
                   help="same as --shutdown 0")
    p.add_argument("--shutdown-soc", type=float, default=DEFAULT_SHUTDOWN_SOC,
                   help=f"charge below which a reading counts as low, percent (default {DEFAULT_SHUTDOWN_SOC:g})")
    p.add_argument("--shutdown-voltage", type=float, default=DEFAULT_SHUTDOWN_VOLTAGE,
                   help=f"cell voltage below which a reading counts as low (default {DEFAULT_SHUTDOWN_VOLTAGE:g})")
    p.add_argument("--shutdown-samples", type=int, default=DEFAULT_SHUTDOWN_SAMPLES,
                   help=f"consecutive low readings before powering off (default {DEFAULT_SHUTDOWN_SAMPLES})")
    a = p.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from max17048_test import MAX17048, run_continuous_monitoring  # noqa: E402

    d = Path(a.out)
    if not a.flat:
        d = d / datetime.now().strftime("%Y%m%d_%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    log = d / "fuel_gauge.csv"

    guard = LowBatteryGuard(a.shutdown_soc, a.shutdown_voltage, max(1, a.shutdown_samples),
                            enabled=bool(a.shutdown), events_path=d / "events.log")
    fg = MAX17048(bus_number=a.bus)
    fg.verbose = a.print_every <= 0
    print(f"Fuel gauge initialized, logging to {log}", flush=True)
    print(f"low-battery shutdown {'ON' if a.shutdown else 'OFF (bench)'}: SOC < {a.shutdown_soc:g}% and "
          f"< {a.shutdown_voltage:g} V for {guard.samples} readings ({guard.samples * a.interval:.0f} s)", flush=True)
    try:
        run_continuous_monitoring(fg, duration=TEN_YEARS, interval=a.interval, log_file=str(log),
                                  print_every=a.print_every, on_reading=guard)
    except KeyboardInterrupt:
        pass
    finally:
        fg.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
