#!/usr/bin/env python3
"""
BNO055 IMU Data Logger for Raspberry Pi CM4
Logs IMU data to CSV file at 50Hz

Features:
- 50Hz polling rate
- CSV logging with timestamps
- Graceful shutdown on SIGINT/SIGTERM
- Comprehensive sensor data capture
"""

import serial
import signal
import sys
import time
import struct
import csv
import json
from datetime import datetime
from pathlib import Path
from enum import IntEnum
from dataclasses import dataclass
from typing import Optional

# ============================================================================
# BNO055 Constants
# ============================================================================

class Register(IntEnum):
    """BNO055 Register Addresses"""
    # Page 0 registers
    CHIP_ID = 0x00
    PAGE_ID = 0x07
    OPR_MODE = 0x3D
    PWR_MODE = 0x3E
    SYS_TRIGGER = 0x3F

    # Calibration status
    CALIB_STAT = 0x35

    # Euler angles
    EUL_HEADING_LSB = 0x1A
    EUL_ROLL_LSB = 0x1C
    EUL_PITCH_LSB = 0x1E

    # Quaternion
    QUA_W_LSB = 0x20
    QUA_X_LSB = 0x22
    QUA_Y_LSB = 0x24
    QUA_Z_LSB = 0x26

    # Linear acceleration
    LIA_X_LSB = 0x28
    LIA_Y_LSB = 0x2A
    LIA_Z_LSB = 0x2C

    # Gravity vector
    GRV_X_LSB = 0x2E
    GRV_Y_LSB = 0x30
    GRV_Z_LSB = 0x32

    # Accelerometer
    ACC_X_LSB = 0x08
    ACC_Y_LSB = 0x0A
    ACC_Z_LSB = 0x0C

    # Gyroscope
    GYR_X_LSB = 0x14
    GYR_Y_LSB = 0x16
    GYR_Z_LSB = 0x18

    # Magnetometer
    MAG_X_LSB = 0x0E
    MAG_Y_LSB = 0x10
    MAG_Z_LSB = 0x12

    # Temperature
    TEMP = 0x34


class OperationMode(IntEnum):
    """BNO055 Operation Modes"""
    CONFIG = 0x00
    ACCONLY = 0x01
    MAGONLY = 0x02
    GYROONLY = 0x03
    ACCMAG = 0x04
    ACCGYRO = 0x05
    MAGGYRO = 0x06
    AMG = 0x07
    IMU = 0x08
    COMPASS = 0x09
    M4G = 0x0A
    NDOF_FMC_OFF = 0x0B
    NDOF = 0x0C  # Full fusion mode


class PowerMode(IntEnum):
    """BNO055 Power Modes"""
    NORMAL = 0x00
    LOW = 0x01
    SUSPEND = 0x02


# UART Protocol constants
UART_START_BYTE = 0xAA
UART_RESPONSE_BYTE = 0xBB
UART_ERROR_BYTE = 0xEE

BNO055_CHIP_ID = 0xA0

# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class EulerAngles:
    heading: float  # degrees
    roll: float     # degrees
    pitch: float    # degrees


@dataclass
class Quaternion:
    w: float
    x: float
    y: float
    z: float


@dataclass
class Vector3:
    x: float
    y: float
    z: float


@dataclass
class CalibrationStatus:
    system: int  # 0-3
    gyro: int    # 0-3
    accel: int   # 0-3
    mag: int     # 0-3

    @property
    def fully_calibrated(self) -> bool:
        return all(v == 3 for v in [self.system, self.gyro, self.accel, self.mag])


# ============================================================================
# BNO055 UART Driver
# ============================================================================

class BNO055:
    """BNO055 IMU driver using UART protocol"""

    def __init__(self, port: str = "/dev/ttyAMA5", baudrate: int = 115200, timeout: float = 1.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial: Optional[serial.Serial] = None
        self._initialized = False

    def open(self) -> bool:
        """Open serial connection and initialize the sensor"""
        try:
            # Close any existing connection
            self.close()

            # Open serial port
            self.serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                write_timeout=self.timeout
            )

            # Clear any stale data
            self.serial.reset_input_buffer()
            self.serial.reset_output_buffer()

            # Give sensor time to stabilize after port open
            time.sleep(1.0)

            # Try to read chip ID with multiple retries
            chip_id = None
            max_attempts = 5

            for attempt in range(max_attempts):
                try:
                    print(f"Reading chip ID (attempt {attempt + 1}/{max_attempts})...")

                    # Clear buffers before each attempt
                    self.serial.reset_input_buffer()

                    chip_id = self._read_byte(Register.CHIP_ID)
                    print(f"Got chip ID: 0x{chip_id:02X}")

                    if chip_id == BNO055_CHIP_ID:
                        print("✓ Chip ID verified!")
                        break
                    else:
                        print(f"Wrong chip ID: 0x{chip_id:02X} (expected 0x{BNO055_CHIP_ID:02X})")

                except Exception as e:
                    print(f"Chip ID read failed: {e}")

                # Wait between retries
                if attempt < max_attempts - 1:
                    time.sleep(0.5)

            if chip_id != BNO055_CHIP_ID:
                print(f"Error: Invalid chip ID: 0x{chip_id:02X} (expected 0x{BNO055_CHIP_ID:02X})")
                return False

            # Give sensor additional time to fully boot
            print("Waiting for sensor to fully initialize...")
            time.sleep(0.65)

            # Reset to config mode first
            print("Configuring sensor...")
            self._set_mode(OperationMode.CONFIG)
            time.sleep(0.05)

            # Set to normal power mode
            self._write_byte(Register.PWR_MODE, PowerMode.NORMAL)
            time.sleep(0.01)

            # Select page 0
            self._write_byte(Register.PAGE_ID, 0x00)
            time.sleep(0.01)

            # Set operation mode to NDOF (full fusion)
            print("Setting NDOF fusion mode...")
            self._set_mode(OperationMode.NDOF)
            time.sleep(0.02)

            self._initialized = True
            print(f"BNO055 initialized successfully on {self.port}")
            return True

        except serial.SerialException as e:
            print(f"Serial error: {e}")
            return False
        except Exception as e:
            print(f"Initialization error: {e}")
            return False

    def close(self):
        """Close serial connection and put sensor in config mode"""
        if self.serial and self.serial.is_open:
            try:
                if self._initialized:
                    self._set_mode(OperationMode.CONFIG)
                    time.sleep(0.02)
            except:
                pass
            finally:
                self.serial.close()
                self.serial = None
        self._initialized = False

    def _write_byte(self, register: int, value: int):
        """Write a byte to a register using UART protocol"""
        cmd = bytes([UART_START_BYTE, 0x00, register, 1, value])
        self.serial.write(cmd)

        response = self.serial.read(2)
        if len(response) < 2:
            raise IOError("Write timeout - no response")

        if response[0] != UART_ERROR_BYTE or response[1] != 0x01:
            raise IOError(f"Write error: {response.hex()}")

    def _read_byte(self, register: int) -> int:
        """Read a single byte from a register"""
        data = self._read_bytes(register, 1)
        return data[0]

    def _read_bytes(self, register: int, length: int, max_retries: int = 3) -> bytes:
        """Read multiple bytes starting from a register with automatic retry"""
        last_error = None

        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    self.serial.reset_input_buffer()
                    time.sleep(0.01)

                cmd = bytes([UART_START_BYTE, 0x01, register, length])
                self.serial.write(cmd)

                header = self.serial.read(2)
                if len(header) < 2:
                    raise IOError("Read timeout - no response header")

                if header[0] == UART_ERROR_BYTE:
                    error_code = header[1]
                    if error_code in [0x07]:
                        last_error = IOError(f"Read error 0x{error_code:02X}")
                        continue
                    else:
                        raise IOError(f"Read error 0x{error_code:02X}")

                if header[0] != UART_RESPONSE_BYTE:
                    raise IOError(f"Unexpected response byte: 0x{header[0]:02X}")

                resp_length = header[1]
                data = self.serial.read(resp_length)

                if len(data) < resp_length:
                    raise IOError(f"Read timeout - got {len(data)} bytes, expected {resp_length}")

                return data

            except IOError as e:
                last_error = e
                if attempt < max_retries - 1:
                    continue
                else:
                    raise

        if last_error:
            raise last_error
        else:
            raise IOError("Read failed after all retries")

    def _set_mode(self, mode: OperationMode):
        """Set the operation mode"""
        self._write_byte(Register.OPR_MODE, mode)

    def get_calibration(self) -> CalibrationStatus:
        """Get calibration status for all sensors"""
        status = self._read_byte(Register.CALIB_STAT)
        return CalibrationStatus(
            system=(status >> 6) & 0x03,
            gyro=(status >> 4) & 0x03,
            accel=(status >> 2) & 0x03,
            mag=status & 0x03
        )

    def get_euler(self) -> EulerAngles:
        """Get Euler angles (heading, roll, pitch) in degrees"""
        data = self._read_bytes(Register.EUL_HEADING_LSB, 6)
        heading, roll, pitch = struct.unpack('<hhh', data)
        return EulerAngles(
            heading=heading / 16.0,
            roll=roll / 16.0,
            pitch=pitch / 16.0
        )

    def get_quaternion(self) -> Quaternion:
        """Get orientation as quaternion"""
        data = self._read_bytes(Register.QUA_W_LSB, 8)
        w, x, y, z = struct.unpack('<hhhh', data)
        scale = 1.0 / (1 << 14)
        return Quaternion(
            w=w * scale,
            x=x * scale,
            y=y * scale,
            z=z * scale
        )

    def get_linear_acceleration(self) -> Vector3:
        """Get linear acceleration (without gravity) in m/s^2"""
        data = self._read_bytes(Register.LIA_X_LSB, 6)
        x, y, z = struct.unpack('<hhh', data)
        return Vector3(x=x / 100.0, y=y / 100.0, z=z / 100.0)

    def get_gravity(self) -> Vector3:
        """Get gravity vector in m/s^2"""
        data = self._read_bytes(Register.GRV_X_LSB, 6)
        x, y, z = struct.unpack('<hhh', data)
        return Vector3(x=x / 100.0, y=y / 100.0, z=z / 100.0)

    def get_accelerometer(self) -> Vector3:
        """Get raw accelerometer data in m/s^2"""
        data = self._read_bytes(Register.ACC_X_LSB, 6)
        x, y, z = struct.unpack('<hhh', data)
        return Vector3(x=x / 100.0, y=y / 100.0, z=z / 100.0)

    def get_gyroscope(self) -> Vector3:
        """Get gyroscope data in degrees/second"""
        data = self._read_bytes(Register.GYR_X_LSB, 6)
        x, y, z = struct.unpack('<hhh', data)
        return Vector3(x=x / 16.0, y=y / 16.0, z=z / 16.0)

    def get_magnetometer(self) -> Vector3:
        """Get magnetometer data in microtesla"""
        data = self._read_bytes(Register.MAG_X_LSB, 6)
        x, y, z = struct.unpack('<hhh', data)
        return Vector3(x=x / 16.0, y=y / 16.0, z=z / 16.0)

    def get_temperature(self) -> int:
        """Get temperature in degrees Celsius"""
        return self._read_byte(Register.TEMP)


# ============================================================================
# IMU Logger Application
# ============================================================================

class IMULogger:
    """IMU data logger with CSV output"""

    def __init__(self, port: str = "/dev/ttyAMA5", poll_rate_hz: float = 50.0, t0_sidecar: str = None):
        self.sensor = BNO055(port=port)
        self.poll_interval = 1.0 / poll_rate_hz
        self.running = False
        self.log_file = None
        self.csv_writer = None
        self.t0_sidecar =  t0_sidecar
        self._setup_signal_handlers()

    def _setup_signal_handlers(self):
        """Setup handlers for graceful shutdown"""
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        sig_name = signal.Signals(signum).name
        print(f"\n[{sig_name}] Shutdown requested...")
        self.running = False

    def _create_log_file(self) -> str:
        """Create a new log file with timestamp"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        filename = log_dir / f"imu_log_{timestamp}.csv"
        return str(filename)

    def _init_csv(self, filename: str):
        """Initialize CSV file with headers"""
        self.log_file = open(filename, 'w', newline='')
        self.csv_writer = csv.writer(self.log_file)

        # Write header
        self.csv_writer.writerow([
            'timestamp',
            'heading_deg', 'roll_deg', 'pitch_deg',
            'quat_w', 'quat_x', 'quat_y', 'quat_z',
            'lin_accel_x', 'lin_accel_y', 'lin_accel_z',
            'gravity_x', 'gravity_y', 'gravity_z',
            'accel_x', 'accel_y', 'accel_z',
            'gyro_x', 'gyro_y', 'gyro_z',
            'mag_x', 'mag_y', 'mag_z',
            'temp_c',
            'cal_sys', 'cal_gyro', 'cal_accel', 'cal_mag'
        ])
        self.log_file.flush()

    def run(self):
        """Main logging loop"""
        print("=" * 60)
        print("BNO055 IMU Data Logger")
        print("=" * 60)
        print(f"Port: {self.sensor.port}")
        print(f"Poll rate: {1.0/self.poll_interval:.1f} Hz")
        print("Press Ctrl+C to stop")
        print("=" * 60)

        # Initialize sensor (retry up to 10 times — BNO055 UART is flaky
        # on startup, especially when other peripherals are initializing)
        for init_attempt in range(10):
            if self.sensor.open():
                break
            if init_attempt < 9:
                wait = 2
                print(f"Init failed, retrying in {wait}s ({9 - init_attempt} left)...")
                time.sleep(wait)
                # Close and reopen serial port for a clean retry
                try:
                    self.sensor.close()
                except Exception:
                    pass
            else:
                print("Failed to initialize sensor after 10 attempts. Exiting.")
                return 1

        # Create log file
        log_filename = self._create_log_file()
        self._init_csv(log_filename)
        print(f"Logging to: {log_filename}")
        print("=" * 60)

        self.running = True
        sample_count = 0
        error_count = 0

        try:
            while self.running:
                loop_start = time.monotonic()

                try:
                    # Get timestamp
                    timestamp = datetime.now().isoformat()

                    # Read all sensor data
                    euler = self.sensor.get_euler()
                    time.sleep(0.002)

                    quat = self.sensor.get_quaternion()
                    time.sleep(0.002)

                    lin_accel = self.sensor.get_linear_acceleration()
                    time.sleep(0.002)

                    gravity = self.sensor.get_gravity()
                    time.sleep(0.002)

                    accel = self.sensor.get_accelerometer()
                    time.sleep(0.002)

                    gyro = self.sensor.get_gyroscope()
                    time.sleep(0.002)

                    mag = self.sensor.get_magnetometer()
                    time.sleep(0.002)

                    temp = self.sensor.get_temperature()
                    time.sleep(0.002)

                    calib = self.sensor.get_calibration()

                    # Write to CSV
                    self.csv_writer.writerow([
                        timestamp,
                        euler.heading, euler.roll, euler.pitch,
                        quat.w, quat.x, quat.y, quat.z,
                        lin_accel.x, lin_accel.y, lin_accel.z,
                        gravity.x, gravity.y, gravity.z,
                        accel.x, accel.y, accel.z,
                        gyro.x, gyro.y, gyro.z,
                        mag.x, mag.y, mag.z,
                        temp,
                        calib.system, calib.gyro, calib.accel, calib.mag
                    ])
                    
                    # Write sidecar to JSON
                    if self.t0_sidecar and sample_count == 0:
                        t0_ns = time.time_ns()
                        with open (self.t0_sidecar, "w") as f:
                            json.dump({"t0_ns": t0_ns}, f)

                    # Flush every 10 samples
                    sample_count += 1
                    if sample_count % 10 == 0:
                        self.log_file.flush()

                    # Print status
                    print(f"\r[{sample_count:6d}] "
                          f"H:{euler.heading:7.2f}° R:{euler.roll:7.2f}° P:{euler.pitch:7.2f}° "
                          f"| Cal: S{calib.system} G{calib.gyro} A{calib.accel} M{calib.mag} "
                          f"| Errors: {error_count}",
                          end="", flush=True)

                except IOError as e:
                    error_count += 1
                    print(f"\n[WARNING] Sensor read error: {e}")
                    time.sleep(0.1)
                    continue

                # Maintain consistent poll rate
                elapsed = time.monotonic() - loop_start
                sleep_time = self.poll_interval - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

        finally:
            # Graceful shutdown
            print("\n" + "=" * 60)
            print("Shutting down...")

            if self.log_file:
                self.log_file.close()
                print(f"Logged {sample_count} samples to {log_filename}")

            self.sensor.close()
            print("Sensor closed. Goodbye!")
            print("=" * 60)

        return 0


# ============================================================================
# Entry Point
# ============================================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="BNO055 IMU Data Logger",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "-p", "--port",
        default="/dev/ttyAMA5",
        help="Serial port for BNO055"
    )
    parser.add_argument(
        "-r", "--rate",
        type=float,
        default=50.0,
        help="Polling rate in Hz"
    )
    
    # Passing arg for writing to sidecar for t0 timestamp
    parser.add_argument(
        "--t0-sidecar",
        default=None,
        help="Path to write t0 timestamp JSON sidecar file"
    )

    args = parser.parse_args()

    logger = IMULogger(port=args.port, poll_rate_hz=args.rate, t0_sidecar=args.t0_sidecar)
    sys.exit(logger.run())


if __name__ == "__main__":
    main()
