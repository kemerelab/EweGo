# GPS Time Synchronization Guide for Raspberry Pi

## Overview

When logging GPS data alongside other sensors, you need to synchronize timestamps. This guide covers approaches from simple software correlation to professional-grade PPS timing.

**Tested Configuration:**
- Raspberry Pi Compute Module 4
- ZED-X20P GNSS module (TP1 time pulse on GPS_TIME_1)
- UART4 on /dev/ttyAMA4 at 460800 baud, 5 Hz navigation rate
- PPS on GPIO 6

---

## 📊 Quick Comparison

| Method | Accuracy | Complexity | Hardware Required |
|--------|----------|------------|-------------------|
| **Software Logging** | ~10-50 ms | Easy | None |
| **PPS + NTP** | <1 μs | Medium | GPIO wire to PPS |

**Note:** We do NOT use gpsd in this setup because it would conflict with the Python logger's exclusive access to the serial port.

---

## Method 1: Software Time Correlation (Simplest)

**Accuracy: 10-50 ms** (good enough for most applications)

### How It Works:
- Log both system timestamps and GPS timestamps
- Create a correlation file
- Post-process to align sensor data

### Setup:

Use the enhanced logger `gps_logger_timesync.py` which creates two files:
- `gps_log_YYYYMMDD_HHMMSS.ubx` - Raw GPS data
- `gps_log_YYYYMMDD_HHMMSS_timesync.csv` - Time correlation

### Time Sync CSV Format:
```csv
system_time,gps_time,gps_week,gps_tow,offset_seconds,num_satellites
1736134329.123456,2026-01-06T00:52:09.123456,2348,3600.123,-0.023456,12
```

Where:
- **system_time**: Unix timestamp from Raspberry Pi clock
- **gps_time**: GPS time from satellite
- **offset_seconds**: System time - GPS time
- **gps_week**: GPS week number
- **gps_tow**: GPS time of week (seconds since Sunday 00:00)

### Post-Processing Example:

```python
import pandas as pd
import numpy as np

# Load time correlation
timesync = pd.read_csv('gps_log_20260106_005209_timesync.csv')

# Load your other sensor data
imu_data = pd.read_csv('imu_log.csv')  # has 'system_time' column

# Interpolate GPS time for each IMU sample
def system_to_gps_time(system_time, timesync_df):
    """Convert system timestamp to GPS time"""
    # Interpolate offset
    offset = np.interp(
        system_time,
        timesync_df['system_time'],
        timesync_df['offset_seconds']
    )
    return system_time - offset

# Apply to your sensor data
imu_data['gps_time'] = imu_data['system_time'].apply(
    lambda t: system_to_gps_time(t, timesync)
)
```

### Pros:
✅ No hardware changes needed  
✅ Easy to implement  
✅ Works immediately  
✅ Good enough for most robotics applications

### Cons:
⚠️ 10-50ms jitter from OS scheduling  
⚠️ Requires post-processing

---

## Method 2: PPS + Network NTP (Recommended - Production Grade)

**Accuracy: <1 microsecond** ⭐ Best option!

### Overview

This method uses:
- **GPS PPS signal** for microsecond-accurate timing
- **Network NTP** for coarse time reference (which second it is)
- **No gpsd required** - your Python logger keeps exclusive serial port access

This is the recommended approach because:
- ✅ Microsecond accuracy
- ✅ Python logger keeps serial port access (no conflicts)
- ✅ Network NTP is "good enough" for coarse time
- ✅ Simpler than managing gpsd + Python logger conflicts

---

### Hardware Setup

**1. Locate TIMEPULSE pin** on ZED-X20P module

**2. Connect to GPIO** on Raspberry Pi with optional pull-up resistor:

```
ZED-X20P TIMEPULSE ----[10kΩ]---- 3.3V
           |
           └---------------------- GPIO 6 (adjust for your setup)

ZED-X20P GND ---------------------- Pi GND
```

**GPIO Pin Notes:**
- This guide uses **GPIO 6** (tested on CM4 with the EweGo Carrier Board and the ZED-X20P Module Configured for TP2 output)
- Common alternatives: GPIO 18 (most common), GPIO 11
- Use **BCM GPIO numbering** (not physical pin numbers)
- For Compute Module 4: Check your IO board's pinout
- For Standard Pi: GPIO 6 = Physical Pin 31

**Pull-up resistor:**
- Optional but recommended for long wires or noise immunity
- 10kΩ between TIMEPULSE and 3.3V
- Can be omitted for short, clean connections

---

### Configure ZED-X20P in u-center 2

**1. Connect GPS module to u-center 2**

**2. Use the exported `TimePulseConfiguration.ucf` configuration file to load the correct parameters.

#### Alternatively, change them one by one

**1. Navigate to TIMEPULSE configuration:**
   - Configuration → Advanced configuration
   - Search for "TP" or "TIMEPULSE" or "pulse"

**2. Set the following parameters:**

```
Basic Settings:
  pulsedef:           0 (period mode - recommended)
  active:             1 (ON/enabled)

When GPS LOCKED (main PPS settings):
  period_lock_tp1:        1000000 μs (1 second = 1 Hz)
  pulse_length_lock_tp1:  100000 μs (100 ms pulse width)

When GPS UNLOCKED (before satellite lock):
  period_tp1:         0 μs (no pulse)
  pulse_length_tp1:   0 μs (no pulse)

Timing Configuration:
  sync_gnss_tp1:      1 (ON - CRITICAL: sync to GNSS time)
  use_lock_tp1:       1 (ON - use different settings when locked)
  polarity:           1 (rising edge)
  align_tow:          1 (ON - align to time-of-week)
  grid:               1 (GPS time, NOT UTC)
```

**Parameter Explanations:**
- **pulsedef=0**: Period mode (specify time in microseconds)
- **sync_gnss_tp1=1**: Synchronize pulse to GPS time (REQUIRED for accuracy!)
- **use_lock_tp1=1**: Only output valid PPS when GPS has satellite lock
- **grid=1**: Use GPS time (continuous), not UTC (has leap seconds)
- **period_lock_tp1=1000000**: One pulse per second (1 Hz)
- **pulse_length_lock_tp1=100000**: Pulse high for 100ms (10% duty cycle)

**3. Apply and Save:**
   - Click **Send configuration to device**
   - **Save to Flash** (Configuration actions → Save configuration)
   - Select **Flash** and **BBR** (battery-backed RAM)
   - Click **Save**

**4. Power cycle GPS module** to verify settings persist

---

### Raspberry Pi Software Setup

#### Step 1: Install Required Packages

```bash
# Update package list
sudo apt-get update

# Install PPS tools and chrony
sudo apt-get install -y pps-tools chrony

# Verify installation
ppstest --help
chronyd --version
```

**What these packages do:**
- **pps-tools**: Utilities to test PPS signals (`ppstest`)
- **chrony**: Modern NTP daemon with excellent PPS support

---

#### Step 2: Enable PPS Kernel Module

Edit boot configuration:
```bash
sudo nano /boot/firmware/config.txt
# Or on older Raspberry Pi OS: sudo nano /boot/config.txt
```

Add at the end:
```
# GPS UART (if using hardware UART)
dtoverlay=uart4

# GPS PPS on GPIO 6
dtoverlay=pps-gpio,gpiopin=6
```

**Important Notes:**
- `gpiopin=6` uses **BCM GPIO numbering**
- Adjust the pin number to match your hardware
- Common pins: GPIO 18 (default), GPIO 6, GPIO 11
- For Compute Module 4, consult your IO board documentation

**Save and reboot:**
```bash
sudo reboot
```

---

#### Step 3: Verify PPS is Working

After reboot:

```bash
# 1. Check PPS device exists
ls -la /dev/pps0
# Should show: crw------- 1 root root 246, 0 Jan  6 12:34 /dev/pps0

# 2. Check kernel messages
dmesg | grep pps
```

**Expected kernel messages:**
```
[    0.056790] pps_core: LinuxPPS API ver. 1 registered
[    0.056794] pps_core: Software ver. 5.3.6 - Copyright 2005-2007 Rodolfo Giometti
[    4.123456] pps pps0: new PPS source pps@6
[    4.123789] pps pps0: Registered IRQ 123 as PPS source
```

The line `pps pps0: new PPS source pps@6` confirms PPS on GPIO 6!

```bash
# 3. Test PPS pulses (GPS must have satellite lock!)
sudo ppstest /dev/pps0
```

**Expected output when working:**
```
trying PPS source "/dev/pps0"
found PPS source "/dev/pps0"
ok, found 1 source(s), now start fetching data...
source 0 - assert 1767750565.001051401, sequence: 29 - clear  0.000000000, sequence: 0
source 0 - assert 1767750566.001049043, sequence: 30 - clear  0.000000000, sequence: 0
source 0 - assert 1767750567.001048518, sequence: 31 - clear  0.000000000, sequence: 0
```

Each line shows a PPS pulse exactly 1 second apart!

**Troubleshooting if no pulses appear:**

1. **GPS may not have satellite lock:**
   ```bash
   # Check GPS status with your logger
   source ~/gps_env/bin/activate
   python3 gps_logger_timesync.py
   # Look for "Fix: 3D FIX" with 8+ satellites
   ```
   Move GPS antenna to window or outdoors if needed.

2. **Check physical wiring:**
   - Verify TIMEPULSE connected to correct GPIO pin
   - Check GND connection
   - Try different GPIO pin (e.g., GPIO 18)

3. **Verify TIMEPULSE settings in u-center:**
   - Reconnect GPS to u-center
   - Check settings are still there (did they save to Flash?)
   - Verify `active=1` and `sync_gnss_tp1=1`

4. **Check GPIO conflicts:**
   ```bash
   # See what's using GPIO 6
   sudo cat /sys/kernel/debug/gpio | grep -A2 "gpio-6"
   # Should show it's claimed by PPS
   ```

5. **Try falling edge detection:**
   ```bash
   sudo nano /boot/firmware/config.txt
   ```
   Change to:
   ```
   dtoverlay=pps-gpio,gpiopin=6,assert_falling_edge
   ```
   Reboot and test again.

---

### Configure Chrony for PPS + NTP

Edit chrony configuration:
```bash
sudo nano /etc/chrony/chrony.conf
```

**Replace/update with this configuration:**

```
# ============================================================================
# TIME SOURCES
# ============================================================================

# Use Debian vendor zone for coarse time reference
pool 2.debian.pool.ntp.org iburst

# GPS PPS for microsecond-precise time
# Requires network NTP for coarse reference (which second it is)
refclock PPS /dev/pps0 refid PPS precision 1e-6 poll 3 prefer

# Disabled: DHCP-provided time sources (not needed with GPS PPS)
# sourcedir /run/chrony-dhcp

# Disabled: Additional NTP sources directory (not needed)
# sourcedir /etc/chrony/sources.d

# ============================================================================
# AUTHENTICATION & SECURITY
# ============================================================================

# Location of file containing ID/key pairs for NTP authentication
keyfile /etc/chrony/chrony.keys

# ============================================================================
# DRIFT & CALIBRATION
# ============================================================================

# File where chronyd stores rate information
driftfile /var/lib/chrony/chrony.drift

# Save NTS keys and cookies
ntsdumpdir /var/lib/chrony

# Stop bad estimates upsetting machine clock
maxupdateskew 100.0

# ============================================================================
# LOGGING
# ============================================================================

# Enable logging for GPS PPS monitoring
log tracking measurements statistics

# Log files location
logdir /var/log/chrony

# ============================================================================
# SYSTEM CLOCK SYNC
# ============================================================================

# Enable kernel synchronisation (every 11 minutes) of the RTC
# Note: Cannot be used with 'rtcfile' directive
rtcsync

# Step the system clock if adjustment is larger than 1 second
# But only in the first three clock updates
makestep 1 3

# ============================================================================
# LEAP SECONDS
# ============================================================================

# Get TAI-UTC offset and leap seconds from system tz database
leapseclist /usr/share/zoneinfo/leap-seconds.list

# ============================================================================
# ADDITIONAL CONFIGURATION
# ============================================================================

# Disabled: Include configuration files found in /etc/chrony/conf.d
# (to avoid conflicts with our GPS PPS setup)
# confdir /etc/chrony/conf.d
```

**Key configuration explained:**
- `pool 2.debian.pool.ntp.org iburst`: Network NTP for coarse time
- `refclock PPS /dev/pps0 refid PPS precision 1e-6 poll 3 prefer`: GPS PPS
- `prefer`: Prefer PPS over network NTP when both available
- `log tracking measurements statistics`: Enable detailed logging

**Save and restart chrony:**
```bash
sudo systemctl restart chrony
sudo systemctl enable chrony
```

---

### Verify Time Synchronization

**Wait 5-10 minutes** for chrony to converge, then check status:

```bash
# Check time sources
chronyc sources -v
```

**Expected output:**
```
MS Name/IP address         Stratum Poll Reach LastRx Last sample
===============================================================================
#* PPS                           0   3   377     8   +234ns[ +456ns] +/-  123ns
^- 2.debian.pool.ntp.org         2   6   377    12    +2ms[  +2ms]   +/-   15ms
```

**What to look for:**
- `*` next to PPS = PPS is the **selected** time source ✅
- `#` prefix = PPS reference clock (local hardware)
- `^` prefix = Network NTP server
- `+234ns` = Offset in nanoseconds (excellent!)

**Check detailed tracking:**
```bash
chronyc tracking
```

**Expected output:**
```
Reference ID    : 50505300 (PPS)
Stratum         : 1
Ref time (UTC)  : Tue Jan 07 01:23:45 2026
System time     : 0.000000023 seconds fast of NTP time
Last offset     : +0.000000045 seconds
RMS offset      : 0.000000234 seconds
Frequency       : 0.123 ppm slow
Residual freq   : +0.001 ppm
Skew            : 0.012 ppm
Root delay      : 0.000000123 seconds
Root dispersion : 0.000001234 seconds
Update interval : 8.0 seconds
Leap status     : Normal
```

**What to look for:**
- `Reference ID: 50505300 (PPS)` = Using PPS as primary source ✅
- `Stratum: 1` = You are now a Stratum 1 time server! ✅
- `System time: 0.000000023 seconds` = 23 nanoseconds accuracy! ✅
- `RMS offset: 0.000000234 seconds` = 234 nanoseconds RMS ✅

**You now have sub-microsecond time accuracy! 🎉**

---

### Monitor Time Sync Quality

**View live tracking updates:**
```bash
watch -n 2 'chronyc tracking'
```

**Check log files:**
```bash
# Statistics log
tail -f /var/log/chrony/statistics.log

# Tracking log
tail -f /var/log/chrony/tracking.log

# Measurements log
tail -f /var/log/chrony/measurements.log
```

---

### Troubleshooting Chrony

**PPS not showing in sources:**
```bash
# Check chrony sees PPS device
ls -la /dev/pps0

# Check permissions
sudo chmod 666 /dev/pps0  # Temporary fix

# Make permanent (create udev rule)
echo 'KERNEL=="pps0", MODE="0666"' | sudo tee /etc/udev/rules.d/99-pps.rules
sudo udevadm control --reload-rules
```

**PPS shows but not selected (`?` instead of `*`):**
- Wait 10-15 minutes for convergence
- Check GPS has good satellite lock
- Verify network NTP is working: `chronyc sources | grep pool`
- PPS needs coarse time from network first

**Large offsets or poor accuracy:**
```bash
# Check for GPS time jumps
sudo ppstest /dev/pps0
# Timestamps should be smooth, ~1 second apart

# Check system load (high load = poor timing)
uptime

# Restart chrony
sudo systemctl restart chrony
```

**Network NTP not working:**
```bash
# Test internet connectivity
ping -c 3 2.debian.pool.ntp.org

# Check firewall (NTP uses UDP port 123)
sudo iptables -L | grep ntp

# Try different NTP pool
sudo nano /etc/chrony/chrony.conf
# Change to: pool 0.pool.ntp.org iburst
```

---

## Using GPS Time in Your Applications

Once system clock is synced to GPS, **all programs automatically use GPS time!**

```python
import time
from datetime import datetime

# These now give GPS time (via system clock)!
system_time = time.time()
dt = datetime.now()

# Your sensor logs are GPS-synchronized
log_entry = {
    'timestamp': time.time(),  # GPS time!
    'imu_x': read_imu(),
    'gps_lat': gps_lat
}
```

**Your Python logger:**
```bash
source ~/gps_env/bin/activate
python3 gps_logger_timesync.py
```

The `time_offset` column should show very small values (near 0.000s) once chrony locks to PPS.

---

## Complete Setup Checklist

### Hardware:
- [ ] GPS module connected to Raspberry Pi UART
- [ ] TIMEPULSE pin wired to GPIO (e.g., GPIO 6)
- [ ] GND connection verified
- [ ] Optional: 10kΩ pull-up resistor installed

### u-center Configuration:
- [ ] TIMEPULSE parameters configured
- [ ] Settings saved to Flash
- [ ] Power cycled GPS to verify persistence
- [ ] GPS has satellite lock (check with `gpsmon` or logger)

### Raspberry Pi:
- [ ] `pps-tools` and `chrony` installed
- [ ] `dtoverlay=pps-gpio,gpiopin=X` added to boot config
- [ ] Rebooted and verified `/dev/pps0` exists
- [ ] `ppstest` shows PPS pulses
- [ ] Chrony configured with PPS + NTP
- [ ] Chrony converged (PPS showing `*` in sources)

### Verification:
- [ ] `chronyc sources` shows PPS selected with `*`
- [ ] `chronyc tracking` shows sub-microsecond offset
- [ ] Python logger works without conflicts
- [ ] Time offset near 0.000s in logger output

---

## Performance Expectations

**Typical Accuracy with PPS + NTP:**
- **Best case:** 10-50 nanoseconds RMS offset
- **Normal case:** 100-500 nanoseconds RMS offset  
- **Good case:** 1-2 microseconds RMS offset
- **Acceptable:** <10 microseconds RMS offset

**Factors affecting accuracy:**
- GPS satellite signal quality (more satellites = better)
- System load (idle Pi = better timing)
- Temperature stability
- Network NTP quality (better servers = faster convergence)

---

## Summary

**Quick Start (tested configuration):**
1. Install packages: `sudo apt-get install pps-tools chrony`
2. Wire TIMEPULSE to GPIO 6 (or your chosen pin)
3. Configure GPS in u-center 2 (save to Flash!)
4. Add `dtoverlay=pps-gpio,gpiopin=6` to boot config
5. Configure chrony for PPS + NTP
6. Reboot and verify with `ppstest` and `chronyc sources`

**Result:**
- ✅ Sub-microsecond system time accuracy
- ✅ All programs automatically use GPS time
- ✅ Python logger keeps serial port access
- ✅ Stratum 1 NTP server capability
- ✅ Perfect for sensor fusion and data logging

---

## Additional Resources

- [Chrony Documentation](https://chrony.tuxfamily.org/doc/4.3/chrony.conf.html)
- [Raspberry Pi GPIO Pinout](https://pinout.xyz/)
- [u-blox ZED-F9P Integration Manual](https://www.u-blox.com/en/docs/UBX-18010802)
- [PPS Kernel Documentation](https://www.kernel.org/doc/html/latest/driver-api/pps.html)

**For advanced users:**
- Combine multiple time sources (PPS + network + RTC)
- Set up NTP server to share GPS time with other devices
- Use hardware timestamping for even better accuracy
- Configure PTP (Precision Time Protocol) instead of NTP

---

*Last updated: January 2026*
*Tested on: Raspberry Pi Compute Module 4, Raspberry Pi OS Bookworm*
