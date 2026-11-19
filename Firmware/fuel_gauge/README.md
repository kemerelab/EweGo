# MAX17048 Fuel Gauge Testing

## Overview
This project contains scripts for testing and interfacing with the MAX17048 Li+ battery fuel gauge IC on a Raspberry Pi CM4.

## Hardware Configuration
- **Device**: MAX17048 Fuel Gauge IC
- **Platform**: Raspberry Pi
- **I2C Bus**: Bus 3 (device detected at address 0x36)
- **Connections**: SDA/SCL on GPIO pins 2/3

---

## Scripts

### 1. `detect_max17048.py`
**Purpose**: Auto-detection utility to find the MAX17048 on available I2C buses.

**Function**:
- Scans all available I2C buses on the system (`/dev/i2c-*`)
- Attempts to read the version register (0x08) from address 0x36 on each bus
- Reports which bus the MAX17048 is connected to
- Provides troubleshooting tips if device is not found

**Usage**:
```bash
python3 detect_max17048.py
```

**Output**:
- Lists all available I2C buses
- Identifies which bus has the MAX17048
- Displays the IC version number
- Shows the command to run the test script with correct bus number

---

### 2. `max17048_test.py`
**Purpose**: Comprehensive testing and monitoring tool for the MAX17048 fuel gauge.

**Function**:
Full-featured driver and test suite providing:
- Battery voltage reading (VCELL register)
- State of Charge (SOC) reading and tracking
- Configuration register access
- Alert threshold configuration and monitoring
- Quick-start calibration command
- Device reset functionality
- Continuous monitoring with customizable intervals

**Usage**:
```bash
python3 max17048_test.py --bus 3
```

**Test Menu Options**:
1. **Basic Test** - Read version, voltage, SOC, config, and alert status
2. **Continuous Monitoring (30s)** - Monitor voltage and SOC every 2 seconds for 30 seconds
3. **Alert Functionality Test** - Test low battery alert triggering and clearing
4. **Quick-Start Test** - Restart fuel gauge calculations for recalibration
5. **Custom Continuous Monitoring** - User-defined duration and interval
6. **Reset Device** - Perform hardware reset of the MAX17048
7. **Exit** - Close connection and exit

**Key Features**:
- Automatic I2C bus connection
- 16-bit register read/write operations (MSB-first)
- Voltage resolution: 1.25mV per LSB (12-bit ADC)
- SOC resolution: 1/256% precision
- Real-time alert status monitoring
- Graceful keyboard interrupt handling

### 3. `log_fuel_gauge.py` (unit `ewego-fuel`)
**Purpose**: the wearable's unattended battery logger and low-battery guard.

Writes `/opt/ewego/recordings/fuel/<YYYYmmdd_HHMMSS>/fuel_gauge.csv`
(columns `Timestamp, Datetime, Elapsed_Seconds, Voltage_V, SOC_Percent,
Alert`, one row per `--interval`, default 2 s) through
`max17048_test.run_continuous_monitoring`.

**Low-battery shutdown**: when the charge is below `--shutdown-soc`
(default 8 %) **and** the cell voltage is below `--shutdown-voltage`
(default 3.5 V) for `--shutdown-samples` consecutive readings (default 30,
one minute at 2 s), it appends a line to `events.log` in the run directory
(next to the CSV; the CSV columns do not change), prints it to the journal,
and runs `systemctl poweroff`. Poweroff stops `ewego-record.target` through
the normal shutdown ordering, so every recorder closes its files and the
card is synced before the battery dies mid-write (the cause of
`Firmware/bugs/001_sd_power_loss_corruption.md`). One reading above either
threshold resets the count; failed readings (`ERROR` rows) neither count nor
reset it. Both conditions are required because SOC alone is unreliable after
a deep discharge (this gauge once read 1 % at 3.53 V while recharging).

The unit takes the settings from `/etc/ewego/fuel.conf`:

| Setting | Default | Meaning |
|---|---|---|
| `SHUTDOWN` | `1` | `0` = only log the event (bench, USB power, no battery) |
| `SHUTDOWN_SOC` | `8` | percent |
| `SHUTDOWN_VOLTAGE` | `3.5` | volts |
| `SHUTDOWN_SAMPLES` | `30` | consecutive readings |

The logger prints one reading per minute (`--print-every 60`) plus any change
of sensor or alert state, not one line per reading. On the bench:

```bash
python3 log_fuel_gauge.py --out /tmp/fuel --no-shutdown --print-every 0
```

---

## Issue Resolution Log

### Problem: Incorrect Voltage Readings
**Date**: 2026-01-05

**Symptom**:
- Battery voltage reading showed **0.8V** during operation
- Device was running on battery power (impossible at 0.8V for Li-Ion)
- Expected voltage range: 3.0V - 4.2V for Li+ batteries

**Root Cause**:
Incorrect voltage calculation in `max17048_test.py:81`

**Original Code**:
```python
# VCELL register: 12-bit value, LSB = 78.125 µV
voltage = (raw >> 4) * 0.000078125 * 4  # Result in volts
```

**Issue**:
- Multiplication factor was **4** instead of **16**
- This caused voltage readings to be **4× too low**
- The LSB is actually **1.25mV** (not 78.125µV)
- Calculation: 78.125µV × 16 = 1.25mV

**Fix Applied**:
```python
# VCELL register: 12-bit value, LSB = 1.25 mV
voltage = (raw >> 4) * 0.00125  # Result in volts
```

**Result**:
- Before fix: 0.8V (incorrect)
- After fix: 3.534V (correct)
- Voltage now accurately reflects Li-Ion battery state

---

## Testing Results

### Initial Testing (Before Fix)
```
Battery Voltage: 0.848V  ✗ (4× too low)
State of Charge: 0.00%
```

### After Voltage Fix
```
Battery Voltage: 3.534V  ✓ (correct)
State of Charge: 1.00%   ✓ (battery was nearly depleted)
IC Version: 0x0000
Config Register: 0x9797
Alert Threshold: 9%
Alert Status: Inactive
```

### Continuous Monitoring (2 minutes)
- Voltage: Stable at 3.534V
- SOC: Stable at 1.00% (early stage of recharge)
- No errors or communication issues
- Consistent readings every 5 seconds

---

## Technical Details

### MAX17048 Register Map
| Register | Address | Description |
|----------|---------|-------------|
| VCELL | 0x02 | Battery voltage (12-bit, 1.25mV/LSB) |
| SOC | 0x04 | State of charge (MSB: integer %, LSB: 1/256%) |
| MODE | 0x06 | Mode control register |
| VERSION | 0x08 | IC version number |
| CONFIG | 0x0C | Configuration and alert settings |
| COMMAND | 0xFE | Command register (reset, etc.) |

### Commands
- **Quick-Start**: `0x4000` - Restart fuel gauge calculations
- **Reset**: `0x5400` - Full device reset

### Voltage Calculation
```python
raw_value = (MSB << 8) | LSB          # Read 16-bit register
vcell_12bit = raw_value >> 4           # Extract bits [15:4]
voltage_v = vcell_12bit * 0.00125      # Convert to volts (1.25mV LSB)
```

### SOC Calculation
```python
raw_value = (MSB << 8) | LSB           # Read 16-bit register
soc_percent = (raw_value >> 8) + (raw_value & 0xFF) / 256.0
```

---

## Battery State Interpretation

### Voltage vs SOC Guide (Typical Li-Ion)
| Voltage | Approximate SOC | Battery State |
|---------|-----------------|---------------|
| 4.20V | 100% | Fully charged |
| 4.00V | 85-90% | High |
| 3.80V | 60-70% | Medium-high |
| 3.70V | 40-50% | Medium (nominal) |
| 3.50V | 10-20% | Low |
| 3.30V | 0-5% | Very low |
| 3.00V | 0% | Discharged (cutoff) |

**Note**: The MAX17048 fuel gauge tracks actual charge flow, so SOC readings may differ from voltage-based estimates, especially:
- During charging (voltage rises faster than SOC)
- After deep discharge (requires recalibration)
- With battery age/degradation

---

## Quick Start Guide

### 1. Enable I2C (if not already enabled)
```bash
sudo raspi-config
# Navigate to: Interface Options -> I2C -> Enable
# Reboot if prompted
```

### 2. Detect MAX17048
After reboot, run the detection script:
```bash
python3 detect_max17048.py
```
This will scan all I2C buses and tell you which one has the MAX17048.

### 3. Run Tests
Once detected, run the test script with the correct bus number:
```bash
# For this hardware configuration (bus 3):
python3 max17048_test.py --bus 3
```

---

## Usage Notes

1. **I2C Bus Selection**: Always specify `--bus 3` for this hardware configuration
2. **Quick-Start**: Run after battery replacement or if SOC seems inaccurate
3. **Alert Threshold**: Default is 9%, can be set between 1-32%
4. **Monitoring**: Use continuous monitoring to observe charging/discharging trends
5. **SOC Accuracy**: Fuel gauge learns battery characteristics over charge/discharge cycles

---

## Troubleshooting

### Device Not Found
1. Run `detect_max17048.py` to locate the device
2. Check I2C wiring (SDA/SCL on GPIO pins 2/3)
3. Verify power supply to MAX17048
4. Check for proper I2C pull-up resistors (4.7kΩ typical)
5. Scan I2C bus manually: `i2cdetect -y 3`

### SOC Shows 0% Despite Good Voltage
1. Run Quick-Start test (option 4) to recalibrate
2. Allow time for fuel gauge to stabilize (1-2 seconds)
3. Battery may genuinely be depleted despite voltage recovery during charging

### Remote I/O Errors
1. Check I2C bus number is correct (use bus 3)
2. Verify device is powered and responding
3. Check for I2C bus conflicts or electrical issues
4. Try device reset (option 6)

### Permission Denied
```bash
# Run with sudo or add user to i2c group
sudo python3 max17048_test.py --bus 3

# Or add user to i2c group (logout/login required)
sudo usermod -a -G i2c $USER
```

---

## Summary

- **Status**: MAX17048 communication working correctly
- **Voltage Reading**: Fixed and accurate (±1.25mV resolution)
- **SOC Tracking**: Functional, tracks actual battery charge state
- **I2C Bus**: Device located on bus 3 at address 0x36
- **Hardware**: Raspberry Pi with MAX17048 fuel gauge on GPIO pins 2/3

All core functionality verified and operational.
