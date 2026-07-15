# GPS Logger with NTRIP RTK Corrections

Python script for logging GPS data from u-blox ZED-X20P with RTK corrections via NTRIP.

## Features

- ✅ Logs raw UBX binary data for PPK post-processing
- ✅ Receives and forwards RTCM corrections from NTRIP server
- ✅ Real-time display of fix status, position, and satellites
- ✅ Automatic timestamped log files
- ✅ Threading for simultaneous NTRIP and GPS data handling
- ✅ Works on macOS, Linux, and Raspberry Pi

## Installation

```bash
# Install required packages
pip install pyserial pyubx2

```

## Quick Start

### Step 1: Find Your Serial Port

```bash
python find_serial_port.py
```

This will show all available serial ports. Look for something like:
- macOS: `/dev/tty.usbserial-0001`
- Linux/Pi: `/dev/ttyAMA4' # We're connected to UART4. Could be `/dev/ttyUSB0` or `/dev/ttyACM0` if testing

### Step 2: Edit Configuration

Open `gps_logger.py` and edit the configuration section at the bottom:

```python
# Serial port configuration
SERIAL_PORT = '/dev/ttyAMA4'  # ← Change this to your port
BAUDRATE = 460800

# NTRIP configuration
NTRIP_CONFIG = {
    'host': 'your.ntrip.ip.address',     # ← Your NTRIP server
    'port': 2101,
    'mountpoint': 'sheep',               # ← Mountpoint (sheep for Sparkfun Mosaic)
    'username': None,                    # ← Credentials (not used)
    'password':i None 
}

# To test without NTRIP, use:
# NTRIP_CONFIG = None
```

### Step 3: Run the Logger

```bash
python gps_logger.py
```

### Step 4: Stop Logging

Press `Ctrl+C` to stop logging cleanly.

## Output

### Log File

Raw UBX binary data is saved to: `gps_log_YYYYMMDD_HHMMSS.ubx`

This file contains:
- NAV-PVT (position/velocity/time) at 10 Hz
- RXM-RAWX (raw observations) at 10 Hz
- RXM-SFRBX (ephemeris data)
- NAV-STATUS (fix status)
- NAV-SAT (satellite info)

### Real-Time Display

```
[14:23:15] Fix: 3D FIX          | Sats: 18 | Lat:  37.7749123 | Lon: -122.4194155 | Alt:   15.23m | Msgs:  1234 (10.0 Hz) | Logged: 45.2 KB | RTCM: 12.3 KB
```

Shows:
- **Fix type**: NO FIX, 2D FIX, 3D FIX, etc.
  - Watch for RTK FLOAT (carrier phase lock) or RTK FIXED (cm-level accuracy)
- **Sats**: Number of satellites in solution
- **Position**: Latitude, Longitude, Altitude
- **Msgs**: Total messages received and current rate (should be ~10 Hz)
- **Logged**: Size of log file
- **RTCM**: NTRIP correction data received

## Fix Types Explained

| Fix Type | Meaning | Expected Accuracy |
|----------|---------|-------------------|
| NO FIX | No position solution | N/A |
| 2D FIX | Only lat/lon (no altitude) | ~10m |
| 3D FIX | Full position solution | 2-5m |
| RTK FLOAT | Carrier phase lock, ambiguities not resolved | 10-50cm |
| RTK FIXED | Carrier phase lock with resolved ambiguities | 1-2cm |

Note: The script currently shows basic fix types. For RTK status, you may need to check the `carrSoln` field in NAV-PVT or use NAV-STATUS.

## Testing Without NTRIP

To test GPS logging without RTK corrections:

```python
# In gps_logger.py, set:
NTRIP_CONFIG = None
```

The script will still log all data, but you'll only get standard GPS accuracy (2-5m).

## Converting to RINEX for PPK

After collecting data, convert the UBX file to RINEX format using RTKLIB's `convbin`:

```bash
# Install RTKLIB (if not already installed)
# Download from: http://www.rtklib.com/

# Convert UBX to RINEX 3.04
convbin -r ubx -o rinex -od -os -v 3.04 gps_log_20260105_142315.ubx
```

This generates:
- `.obs` file (observations)
- `.nav` file (ephemeris)

Use these files with RTKLIB's `rnx2rtkp` for PPK processing.

## Troubleshooting

### "Permission denied" on serial port (Linux/macOS)

Add your user to the dialout group:
```bash
# Linux
sudo usermod -a -G dialout $USER
# Then log out and back in

# macOS - usually not needed, but if required:
sudo chmod 666 /dev/tty.usbserial-XXXX
```

### "No data received" or low message rate

1. Check baud rate matches u-center configuration (460800)
2. Verify GPS module has good sky view
3. Try disconnecting and reconnecting USB adapter
4. Check that u-center 2 is closed (can't have two programs accessing same port)

### NTRIP connection fails

1. Verify NTRIP credentials are correct
2. Check mountpoint name (case-sensitive)
3. Test NTRIP server in web browser: `http://username:password@server:port/mountpoint`
4. Some NTRIP servers require position in request (not implemented in this simple client)

### Fix stays at "3D FIX" even with NTRIP

1. Check that RTCM data is being received (RTCM counter should increase)
2. Verify you're close enough to base station (<10-20 km typically)
3. Wait 1-5 minutes for RTK convergence
4. Ensure good sky view with minimal obstructions
5. Check that module is configured for Rover mode (TMODE3 disabled)

### Messages logged but file size isn't growing

Make sure you're letting the script run for a while - file is buffered and may not write immediately. File will be flushed when you stop with Ctrl+C.

## Next Steps

Once you've tested successfully on macOS:

1. Transfer the script to Raspberry Pi
2. Update `SERIAL_PORT` for Pi (usually `/dev/ttyUSB0` or `/dev/ttyAMA0`)
3. Set up as systemd service for automatic startup (optional)
4. Consider adding watchdog/restart logic for long-term deployments

## File Structure

```
.
├── gps_logger.py         # Main logging script
├── find_serial_port.py   # Helper to find serial ports
├── README.md             # This file
└── gps_log_*.ubx         # Generated log files
```

## License

Free to use and modify as needed.
