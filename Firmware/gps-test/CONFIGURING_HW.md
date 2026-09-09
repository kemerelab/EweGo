# Configuring the ZED-X20P modules

The collar reads the module's UART1 on Pi UART4 (`/dev/ttyAMA4`, GPIO 8/9)
at 460800 baud. The module's UART2 is wired to Pi UART3 (GPIO 4/5) as a
spare and is not used. The module's TP1 time pulse (GPS_TIME_1) is on
GPIO 6, TP2 (GPS_TIME_2) on GPIO 11, GPS_READY on GPIO 10 and GPS_EXTINT on
GPIO 26 (from the eweSAW schematic).

## Procedure (u-center 2, Windows only)

0. Install u-center 2.
1. Connect the module through a USB-serial adapter.
2. Click "+" to connect; u-center cycles through baud rates to find the module.
3. Revert the module to default settings. The baud rate becomes 38400.
4. Set the baud rate. This has to be done in two passes because the
   connection drops as soon as the rate changes:
   a. Gear icon → CFG-UART1 → Rate → 460800. Tick RAM only, Set, Send.
      u-center reports failure; the change actually took.
   b. Change u-center's connection to 460800.
   c. Repeat with both RAM and Flash ticked, Set, Send, and confirm both
      rows get a green checkmark.
5. Import `SheepRTK.ucf` and Send. Watch for green checkmarks on both the
   RAM and Flash row of every item.
6. Import `TimePulseConfiguration.ucf` and Send. Same check.
7. Power-cycle the module and confirm the settings survived (see below).

Both `.ucf` files write every item to RAM (layer 0) and Flash (layer 2), so
no separate "save configuration" step is needed.

## What the files set

`SheepRTK.ucf` — protocol and message output on UART1:

| Key | Value | Effect |
|---|---|---|
| CFG-UART1OUTPROT-NMEA | 0 | UBX only on the data port |
| CFG-UART1OUTPROT-RTCM3X | 0 | no RTCM output on the data port |
| CFG-RATE-MEAS | 200 ms | **5 Hz** navigation rate |
| CFG-MSGOUT-UBX_NAV_PVT_UART1 | 1 | position/velocity/time every epoch (logger status + time sync) |
| CFG-MSGOUT-UBX_RXM_RAWX_UART1 | 1 | raw observations every epoch (required for PPK) |
| CFG-MSGOUT-UBX_RXM_SFRBX_UART1 | 1 | broadcast ephemeris (required for PPK) |
| CFG-MSGOUT-UBX_NAV_TIMEGPS_UART1 | 1 | GPS time every epoch |
| CFG-MSGOUT-UBX_NAV_TIMEUTC_UART1 | 1 | UTC time every epoch |

Defaults left alone on purpose: UBX output on UART1 enabled, RTCM3 input on
UART1 enabled (that is how NTRIP corrections get in), all constellations,
default dynamic platform model.

`TimePulseConfiguration.ucf` — second time pulse off:

| Key | Value |
|---|---|
| CFG-TP-PERIOD_TP2 | 0 |
| CFG-TP-LEN_TP2 | 0 |
| CFG-TP-FREQ_TP2 | 0 |
| CFG-TP-TIMEGRID_TP2 | GPS |

TP1 is left at its defaults, which are what the Pi needs: enabled, 1 Hz,
rising edge aligned to the top of the second, 100 ms pulse once the
receiver has a fix, only output while locked (`CFG-TP-USE_LOCKED_TP1`),
UTC time grid.

## Why 5 Hz

RXM-RAWX is 32 bytes per tracked signal per epoch, and the X20P tracks all
bands of all constellations: 80–120 signals under open sky. At 10 Hz that
is 26–39 KB/s plus ephemeris, against the 46 KB/s that 460800 baud carries,
close enough to overflow the module's transmit buffer on a good day. At
5 Hz the same data is 13–20 KB/s, and PPK accuracy does not depend on the
epoch rate. If more temporal resolution is ever needed, keep CFG-RATE-MEAS
at 100 ms and set CFG-MSGOUT-UBX_RXM_RAWX_UART1 to 2 (a per-epoch divider),
which gives 10 Hz NAV-PVT with 5 Hz raw.

Data volume at 5 Hz: roughly 25–80 MB per hour depending on satellites.

## Timing

Two levels are available; both are supported by the current configuration.

**Software correlation (10–50 ms).** The logger records system time
alongside the GPS time from NAV-PVT every 10 s in `*_timesync.csv`. The
uncertainty is the UART and processing latency between the epoch and the
message arriving. Only trust rows where NAV-PVT reported a valid time:
`valid.validTime` and `valid.fullyResolved` set, and `flags2.confirmedTime`
once the receiver has a fix. The current Python logger does not check
these flags, so discard rows from before the first fix in post-processing.

**PPS (sub-microsecond).** TP1 is on GPIO 6. The image loads
`dtoverlay=pps-gpio,gpiopin=6`, which gives `/dev/pps0`. Check the pulse is
arriving with no extra tools:

    cat /sys/class/pps/pps0/assert      # "<seconds>.<ns>#<count>", count rising once per second

The pulse only starts once the receiver has a fix. Disciplining the system
clock from it needs chrony (not on the image yet, planned with the time
gate): `refclock PPS /dev/pps0 lock GPS` plus a coarse time source to number
the seconds, which is either NTP from the base station or the GPS logger
feeding NAV-PVT time to chrony's SOCK refclock. Until then, the PPS edge
timestamps in `/sys/class/pps/pps0/assert` can be logged and used in
post-processing exactly like the timesync CSV, at microsecond precision.

Antenna cable delay (`CFG-TP-ANT_CABLEDELAY`, default 50 ns) and TP1 user
delay are irrelevant at the millisecond level this project needs.

## Confirming the configuration survived a power cycle

From the web test console's GPS card, or over SSH:

    sudo stty -F /dev/ttyAMA4 460800 raw -echo
    sudo timeout 3 cat /dev/ttyAMA4 | xxd | grep -c 'b562'    # UBX sync words: many
    sudo timeout 3 cat /dev/ttyAMA4 | grep -c '\$G'           # NMEA sentences: 0

Then log for a minute and run `validate_ubx.py` on the file: NAV-PVT and
RXM-RAWX near 5 Hz, RXM-SFRBX present. To check for transmit-buffer
overflow on the module, enable UBX-MON-COMMS temporarily in u-center and
confirm the UART1 overrun and dropped counters stay at zero.
