#!/usr/bin/env python3
"""Log the MAX17048 fuel gauge to CSV until stopped (unit ewego-fuel).

Writes <out>/<YYYYmmdd_HHMMSS>/fuel_gauge.csv with the same columns that
sensor_test.py used (Timestamp, Datetime, Elapsed_Seconds, Voltage_V,
SOC_Percent, Alert) by calling max17048_test.run_continuous_monitoring.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from max17048_test import MAX17048, run_continuous_monitoring  # noqa: E402

TEN_YEARS = 315360000


def main():
    p = argparse.ArgumentParser(description="MAX17048 fuel gauge CSV logger")
    p.add_argument("--out", default="/opt/ewego/recordings/fuel",
                   help="parent directory; one timestamped subdirectory per run")
    p.add_argument("--interval", type=float, default=2.0, help="seconds between readings")
    p.add_argument("--bus", type=int, default=1, help="I2C bus number")
    p.add_argument("--flat", action="store_true", help="write into --out itself, no timestamped subdirectory")
    a = p.parse_args()

    d = Path(a.out)
    if not a.flat:
        d = d / datetime.now().strftime("%Y%m%d_%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    log = d / "fuel_gauge.csv"

    fg = MAX17048(bus_number=a.bus)
    print(f"Fuel gauge initialized, logging to {log}", flush=True)
    try:
        run_continuous_monitoring(fg, duration=TEN_YEARS, interval=a.interval, log_file=str(log))
    except KeyboardInterrupt:
        pass
    finally:
        fg.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
