# GPS PPS Time Synchronization with Chrony

Guide for setting up time synchronization using GPS-PPS signal from SparkFun ZED-X20P GNSS module with Chrony NTP Server

## Prerequistes

- SparkFun ZED-X20P GNSS module
- GPS anntenna with clear sky view
- Serial connection to Raspberry Pi
- PPS signal connected to GPIO pin

## Hardware config

Connect PPS signal to GPIO pins, then:

```bash
# Configure PPS in boot config
sudo nano /boot/config.txt

```

```bash
# Save and reboot:
sudo reboot

```

## Verify PPS
After reboot:

```bash
# Check if PPS device exists
ls -la /dev/pps*
# Should show: /dev/pps0

```

```bash
# Test PPS signal (requires GPS fix)
sudo ppstest /dev/pps0
# Should show pulses every second:
# source 0 - assert 1738012345.000000000, sequence: 1
# source 0 - assert 1738012346.000000000, sequence: 2

```
## Installation

### Step 1: Install Required Packages

```bash
# Update system
sudo apt update && sudo apt upgrade -y

```

```bash
# Install Chrony
sudo apt install -y chrony

```

```bash
# Install PPS tools
sudo apt install -y pps-tools

```

```bash
# Install GPS daemon (gpsd)
sudo apt install -y gpsd gpsd-clients

```

```bash
# Install Python GPS library (for testing)
pip3 install gps3

```

### Step 2: Configure GPSD

Stop and disable default gpsd:
```bash
sudo systemctl stop gpsd
sudo systemctl stop gpsd.socket
sudo systemctl disable gpsd
sudo systemctl disable gpsd.socket
```
Edit gpsd configuration:
```bash
sudo nano /etc/default/gpsd
```

Add the following settings:
```bash
# Devices gpsd should collect to at boot time
DEVICES="/dev/ttyACM0 /dev/pps0" #Change to your port

# Options for gpsd
GPSD_OPTIONS="-n"

# Automatically start gpsd
START_DAEMON="true"

# Use USB power saving if applicable
USBAUTO="false"
```

Enable and start gpsd:
```bash
sudo systemctl enable gpsd
sudo systemctl enable gpsd.socket
sudo systemctl start gpsd
```
Verify gpsd is working, check GPS status:
```bash
cgps -s

# Should show satellites and position after GPS gets fix
# Can take 5-10 minutes on first boot

# Check PPS in gpsd
gpsmon

# Should show "PPS: yes" when GPS has fix
```

## Configure Chrony
### Step 1: Backup Original Config

```bash
sudo cp /etc/chrony/chrony.conf /etc/chrony/chrony.conf.backup

```

### Step 2: Edit Chrony Configuration

```bash
sudo nano /etc/chrony/chrony.conf

```
Replace with chrony.conf file in folder
Save and exit (Ctrl+X, Y, Enter)

### Step 3: Edit Chrony Configuration
Chrony needs access to PPS device
```bash
sudo usermod -a -G dialout _chrony

```

### Step 4: Restart Chrony

```bash
sudo systemctl restart chrony

```

## Verification & Testing
### Step 1: Check Chrony Sources

```bash
chronyc sources -v

```

### Step 2: Check Chrony Sources

```bash
chronyc tracking

```
Expected output:

Reference ID    : 50505300 (PPS)
Stratum         : 1
Ref time (UTC)  : Mon Jan 27 15:30:45 2026
System time     : 0.000000234 seconds fast of NTP time
Last offset     : +0.000000156 seconds
RMS offset      : 0.000000089 seconds
Frequency       : 12.345 ppm slow
Residual freq   : +0.001 ppm
Skew            : 0.023 ppm
Root delay      : 0.000000001 seconds
Root dispersion : 0.000012345 seconds
Update interval : 2.0 seconds
Leap status     : Normal

### Step 3: Check Chrony Sources
```bash
watch -n 1 'chronyc tracking'
# Press Ctrl+C to exit

```

### View Logs
```bash
sudo journalctl -u chrony -f
sudo journalctl -u gpsd -f

```

### Step 4: Check GPS Fix
```bash
# GPS daemon status
cgps -s

```
Should show:
- 8+ satellites
- Status: 3D FIX
- PPS: yes

## Troubleshooting

### No PPS pulses

Check GPIO connection:
```bash
# Verify PPS kernel module loaded
lsmod | grep pps
# Should show: pps_gpio

# Check which GPIO PPS is on
cat /sys/class/pps/pps0/assert
# Should increment every second when GPS has fix

# Test manually
sudo ppstest /dev/pps0
```

### GPS not getting fix

Check if GPS is receiving data,should see NMEA sentences scrolling:
```bash
sudo cat /dev/serial0

```

Check GPS with gpsmon
Shows satellite signal strength, need 4+ satellites for fix
```bash
gpsmon

```

### Chrony not using PPS
Check Chrony logs:
```bash
sudo journalctl -u chrony -f

```

Common errors:
"PPS not locked to GPS" = GPS doesn't have fix yet
"Permission denied /dev/pps0" = Run: sudo usermod -a -G dialout _chrony
"Could not open PPS source" = Check /dev/pps0 exists

### Time is still drifting
Check if GPS data is reaching Chrony:
```bash
sudo chronyc sources -v

```

GPS should show:
- Reach: 377 (all recent polls successful)
- LastRx: <10 (recently received data

If GPS shows "?" mark:
```bash
sudo systemctl restart gpsd
sudo systemctl restart chrony

```

