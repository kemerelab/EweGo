#!/usr/bin/env python3
"""
MAX17048 Fuel Gauge Test Script
For Raspberry Pi - I2C Bus 1 (SDA/SCL on GPIO pins 2/3)
"""

import os
import smbus2
import time
import sys
import argparse
import csv
from datetime import datetime

# MAX17048 I2C Address
MAX17048_ADDR = 0x36

# MAX17048 Register Addresses
REG_VCELL = 0x02    # Battery voltage
REG_SOC = 0x04      # State of charge
REG_MODE = 0x06     # Mode register
REG_VERSION = 0x08  # IC version
REG_CONFIG = 0x0C   # Configuration
REG_COMMAND = 0xFE  # Command register

# Commands
CMD_QUICK_START = 0x4000
CMD_RESET = 0x5400


class MAX17048:
    """Driver for MAX17048 Fuel Gauge"""

    def __init__(self, bus_number=0, address=MAX17048_ADDR):
        """
        Initialize MAX17048

        Args:
            bus_number: I2C bus number (default: 0)
            address: I2C address (default: 0x36)
        """
        self.bus_number = bus_number
        self.address = address
        try:
            self.bus = smbus2.SMBus(bus_number)
            print(f"✓ Connected to MAX17048 on I2C bus {bus_number} at address 0x{address:02X}")
        except Exception as e:
            print(f"✗ Error opening I2C bus {bus_number}: {e}")
            raise

    def read_register(self, register):
        """Read 16-bit register value (MSB first)"""
        try:
            msb = self.bus.read_byte_data(self.address, register)
            lsb = self.bus.read_byte_data(self.address, register + 1)
            return (msb << 8) | lsb
        except Exception as e:
            print(f"✗ Error reading register 0x{register:02X}: {e}")
            return None

    def write_register(self, register, value):
        """Write 16-bit register value (MSB first)"""
        try:
            msb = (value >> 8) & 0xFF
            lsb = value & 0xFF
            self.bus.write_byte_data(self.address, register, msb)
            self.bus.write_byte_data(self.address, register + 1, lsb)
            return True
        except Exception as e:
            print(f"✗ Error writing register 0x{register:02X}: {e}")
            return False

    def get_vcell(self):
        """
        Get battery voltage in volts

        Returns:
            Battery voltage in volts (float)
        """
        raw = self.read_register(REG_VCELL)
        if raw is None:
            return None
        # VCELL register: 12-bit value, LSB = 1.25 mV
        voltage = (raw >> 4) * 0.00125  # Result in volts
        return voltage

    def get_soc(self):
        """
        Get state of charge

        Returns:
            State of charge in percent (float)
        """
        raw = self.read_register(REG_SOC)
        if raw is None:
            return None
        # SOC register: MSB is integer percent, LSB is 1/256%
        soc = (raw >> 8) + (raw & 0xFF) / 256.0
        return soc

    def get_version(self):
        """
        Get IC version

        Returns:
            Version number (int)
        """
        return self.read_register(REG_VERSION)

    def get_config(self):
        """
        Get configuration register value

        Returns:
            Config register value (int)
        """
        return self.read_register(REG_CONFIG)

    def set_config(self, value):
        """
        Set configuration register

        Args:
            value: 16-bit configuration value
        """
        return self.write_register(REG_CONFIG, value)

    def quick_start(self):
        """
        Issue Quick-Start command
        Restarts fuel gauge calculations in the same manner as initial power-up
        """
        print("Issuing Quick-Start command...")
        return self.write_register(REG_MODE, CMD_QUICK_START)

    def reset(self):
        """
        Reset the MAX17048
        """
        print("Issuing Reset command...")
        result = self.write_register(REG_COMMAND, CMD_RESET)
        if result:
            time.sleep(0.5)  # Wait for reset to complete
        return result

    def get_alert_threshold(self):
        """
        Get alert threshold from config register

        Returns:
            Alert threshold in percent (1-32%)
        """
        config = self.get_config()
        if config is None:
            return None
        # Bits 4-0 of LSB contain ATHD (32 - ATHD = threshold)
        athd = config & 0x1F
        threshold = 32 - athd
        return threshold

    def set_alert_threshold(self, threshold):
        """
        Set alert threshold

        Args:
            threshold: Alert threshold in percent (1-32%)
        """
        if threshold < 1 or threshold > 32:
            print("✗ Alert threshold must be between 1 and 32%")
            return False

        config = self.get_config()
        if config is None:
            return False

        # Calculate ATHD value (32 - threshold)
        athd = 32 - threshold
        # Clear old ATHD bits and set new value
        config = (config & 0xFFE0) | (athd & 0x1F)

        print(f"Setting alert threshold to {threshold}%...")
        return self.set_config(config)

    def clear_alert(self):
        """Clear alert bit in config register"""
        config = self.get_config()
        if config is None:
            return False
        # Clear bit 5 of MSB (ALRT bit)
        config = config & 0xFFDF
        return self.set_config(config)

    def is_alert_active(self):
        """
        Check if alert is active

        Returns:
            True if alert is active, False otherwise
        """
        config = self.get_config()
        if config is None:
            return None
        # Check bit 5 of MSB (ALRT bit)
        return bool(config & 0x0020)

    def close(self):
        """Close I2C bus"""
        self.bus.close()


def print_separator():
    """Print a separator line"""
    print("=" * 60)


def run_basic_test(fg):
    """Run basic functionality test"""
    print_separator()
    print("BASIC FUNCTIONALITY TEST")
    print_separator()

    # Read version
    version = fg.get_version()
    if version is not None:
        print(f"IC Version: 0x{version:04X}")
    else:
        print("✗ Failed to read version")
        return False

    # Read voltage
    voltage = fg.get_vcell()
    if voltage is not None:
        print(f"Battery Voltage: {voltage:.3f} V")
    else:
        print("✗ Failed to read voltage")
        return False

    # Read SOC
    soc = fg.get_soc()
    if soc is not None:
        print(f"State of Charge: {soc:.2f} %")
    else:
        print("✗ Failed to read SOC")
        return False

    # Read config
    config = fg.get_config()
    if config is not None:
        print(f"Config Register: 0x{config:04X}")
    else:
        print("✗ Failed to read config")
        return False

    # Check alert status
    alert = fg.is_alert_active()
    if alert is not None:
        print(f"Alert Status: {'ACTIVE' if alert else 'Inactive'}")

    # Get alert threshold
    threshold = fg.get_alert_threshold()
    if threshold is not None:
        print(f"Alert Threshold: {threshold}%")

    return True


def run_continuous_monitoring(fg, duration=30, interval=2, log_file=None):
    """
    Run continuous monitoring test

    Args:
        fg: MAX17048 instance
        duration: Total monitoring duration in seconds
        interval: Reading interval in seconds
        log_file: Optional path to CSV file for logging data
    """
    print_separator()
    print(f"CONTINUOUS MONITORING TEST ({duration}s)")
    print_separator()

    # Open log file if specified
    csv_writer = None
    file_handle = None
    if log_file:
        try:
            # Open in append mode with line buffering for robustness
            file_handle = open(log_file, 'a', newline='', buffering=1)
            csv_writer = csv.writer(file_handle)

            # Write header if file is new/empty
            if file_handle.tell() == 0:
                csv_writer.writerow(['monotonic_us', 'elapsed_s', 'Voltage_V', 'SOC_Percent', 'Alert'])
                file_handle.flush()

            print(f"✓ Logging to: {log_file}")
        except Exception as e:
            print(f"✗ Error opening log file: {e}")
            print("Continuing without logging...")
            csv_writer = None
            if file_handle:
                file_handle.close()
                file_handle = None

    print("Time(s)  Voltage(V)  SOC(%)   Alert")
    print("-" * 60)

    start_us = time.monotonic_ns() // 1000
    reading_count = 0

    try:
        while (time.monotonic_ns() // 1000 - start_us) < duration * 1_000_000:
            now_us = time.monotonic_ns() // 1000
            elapsed_s = (now_us - start_us) / 1e6
            voltage = fg.get_vcell()
            soc = fg.get_soc()
            alert = fg.is_alert_active()

            if voltage is not None and soc is not None:
                alert_str = "YES" if alert else "NO"
                print(f"{elapsed_s:6.1f}   {voltage:6.3f}      {soc:5.2f}    {alert_str}")
                reading_count += 1

                # Log to file if enabled. Per-row flush + fsync: this writer
                # runs at 0.5 Hz so per-row fsync (2 sync ops/min) is
                # negligible SD wear, and it caps power-loss data loss at one
                # sample. See Michigan 2026-04-13 NUL-tail signature.
                if csv_writer:
                    try:
                        csv_writer.writerow([now_us, f"{elapsed_s:.2f}",
                                           f"{voltage:.4f}", f"{soc:.3f}", alert_str])
                        file_handle.flush()
                        os.fsync(file_handle.fileno())
                    except Exception as e:
                        print(f"✗ Error writing to log: {e}")
            else:
                print(f"{elapsed_s:6.1f}   ERROR reading sensor")
                if csv_writer:
                    try:
                        csv_writer.writerow([now_us, f"{elapsed_s:.2f}",
                                           'ERROR', 'ERROR', 'ERROR'])
                        file_handle.flush()
                        os.fsync(file_handle.fileno())
                    except Exception as e:
                        print(f"✗ Error writing to log: {e}")

            time.sleep(interval)

    except KeyboardInterrupt:
        print("\n✓ Monitoring stopped by user")
    finally:
        # Close log file properly
        if file_handle:
            try:
                file_handle.close()
                print(f"✓ Log file closed: {log_file}")
            except Exception as e:
                print(f"✗ Error closing log file: {e}")

    print(f"\n✓ Completed {reading_count} readings")


def run_alert_test(fg):
    """Test alert functionality"""
    print_separator()
    print("ALERT FUNCTIONALITY TEST")
    print_separator()

    # Get current SOC
    soc = fg.get_soc()
    if soc is None:
        print("✗ Failed to read SOC")
        return False

    print(f"Current SOC: {soc:.2f}%")

    # Set alert threshold just below current SOC to trigger it
    test_threshold = max(1, int(soc) - 1)
    print(f"Setting alert threshold to {test_threshold}% (should trigger alert)...")

    if not fg.set_alert_threshold(test_threshold):
        print("✗ Failed to set alert threshold")
        return False

    time.sleep(1)

    # Check if alert is active
    alert = fg.is_alert_active()
    if alert:
        print("✓ Alert is ACTIVE (as expected)")
    else:
        print("⚠ Alert is not active (may need quick-start)")

    # Clear the alert
    print("Clearing alert...")
    if fg.clear_alert():
        print("✓ Alert cleared")

    time.sleep(0.5)

    # Verify alert is cleared
    alert = fg.is_alert_active()
    if not alert:
        print("✓ Alert confirmed cleared")
    else:
        print("⚠ Alert still active")

    # Restore a reasonable threshold
    fg.set_alert_threshold(10)
    print("✓ Restored alert threshold to 10%")

    return True


def run_quick_start_test(fg):
    """Test quick-start functionality"""
    print_separator()
    print("QUICK-START TEST")
    print_separator()

    print("Reading values before quick-start...")
    voltage_before = fg.get_vcell()
    soc_before = fg.get_soc()

    if voltage_before is not None and soc_before is not None:
        print(f"Before: Voltage={voltage_before:.3f}V, SOC={soc_before:.2f}%")

    # Issue quick-start
    if not fg.quick_start():
        print("✗ Failed to issue quick-start command")
        return False

    print("✓ Quick-start issued, waiting 2 seconds...")
    time.sleep(2)

    print("Reading values after quick-start...")
    voltage_after = fg.get_vcell()
    soc_after = fg.get_soc()

    if voltage_after is not None and soc_after is not None:
        print(f"After:  Voltage={voltage_after:.3f}V, SOC={soc_after:.2f}%")
        print("✓ Quick-start completed (fuel gauge recalibrated)")
        return True
    else:
        print("✗ Failed to read values after quick-start")
        return False


def main():
    """Main test program"""
    # Parse command-line arguments
    parser = argparse.ArgumentParser(description='MAX17048 Fuel Gauge Test Program')
    parser.add_argument('--bus', type=int, default=1,
                        help='I2C bus number (default: 1)')
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("MAX17048 FUEL GAUGE TEST PROGRAM")
    print(f"Raspberry Pi CM4 - I2C Bus {args.bus}")
    print("=" * 60 + "\n")

    try:
        # Initialize fuel gauge
        fg = MAX17048(bus_number=args.bus)

        # Run basic test
        if not run_basic_test(fg):
            print("\n✗ Basic test failed")
            fg.close()
            return 1

        print("\n✓ Basic test passed")

        # Menu for additional tests
        while True:
            print("\n" + "=" * 60)
            print("TEST MENU")
            print("=" * 60)
            print("1. Re-run basic test")
            print("2. Continuous monitoring (30s)")
            print("3. Alert functionality test")
            print("4. Quick-start test")
            print("5. Custom continuous monitoring")
            print("6. Reset device")
            print("7. Exit")
            print("=" * 60)

            choice = input("\nSelect test (1-7): ").strip()

            if choice == '1':
                run_basic_test(fg)
            elif choice == '2':
                run_continuous_monitoring(fg, duration=30, interval=2)
            elif choice == '3':
                run_alert_test(fg)
            elif choice == '4':
                run_quick_start_test(fg)
            elif choice == '5':
                try:
                    duration = int(input("Enter duration (seconds): "))
                    interval = float(input("Enter interval (seconds): "))
                    log_choice = input("Enable logging? (y/n): ").strip().lower()

                    log_file = None
                    if log_choice == 'y':
                        default_log = f"max17048_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                        log_file = input(f"Enter log file path (press Enter for default: {default_log}): ").strip()
                        if not log_file:
                            log_file = default_log

                    run_continuous_monitoring(fg, duration, interval, log_file)
                except ValueError:
                    print("✗ Invalid input")
            elif choice == '6':
                fg.reset()
                time.sleep(2)
                print("✓ Device reset complete")
            elif choice == '7':
                break
            else:
                print("✗ Invalid choice")

        # Cleanup
        fg.close()
        print("\n✓ Test program completed successfully")
        return 0

    except KeyboardInterrupt:
        print("\n\n✓ Test program interrupted by user")
        return 0
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
