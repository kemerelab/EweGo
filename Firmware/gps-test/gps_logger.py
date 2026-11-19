#!/usr/bin/env python3
"""
GPS Logger with Time Synchronization Tracking
Logs GPS data and maintains correlation between GPS time and system time

Exit status (unit ewego-gps restarts on failure):
  0  stopped by SIGINT / Ctrl+C
  1  could not start (serial port or log file)
  3  READ_ERROR_LIMIT_S seconds of nothing but read/write errors, or the
     read thread ended on its own
"""

import serial
import threading
import time
import sys
import os
import base64
import fcntl
import termios
from datetime import datetime, timezone
from pyubx2 import UBXReader
import socket
import csv

EXIT_START_FAILED = 1
EXIT_READ_ERRORS = 3
READ_ERROR_LIMIT_S = 5.0   # consecutive read/write errors for this long -> exit 3
FLUSH_EVERY_S = 1.0        # .ubx buffer pushed to the kernel
FSYNC_EVERY_S = 5.0        # .ubx and _timesync.csv pushed to the card
STATUS_EVERY_S = 30.0      # status line in the journal
NTRIP_RETRY_S = 30.0       # wait between NTRIP reconnect attempts


class ErrorStreak:
    """Consecutive read/write errors in the read loop.

    error(now) returns "first" for the first error of a streak (print it),
    "continue" while the streak is younger than limit_s, and "give_up" once
    errors alone have lasted limit_s seconds. ok() ends the streak and
    returns how many errors it had (0 if none)."""

    def __init__(self, limit_s=READ_ERROR_LIMIT_S):
        self.limit_s = limit_s
        self.first = None
        self.count = 0

    def error(self, now):
        self.count += 1
        if self.first is None:
            self.first = now
            return "give_up" if self.limit_s <= 0 else "first"
        if now - self.first >= self.limit_s:
            return "give_up"
        return "continue"

    def ok(self):
        n = self.count
        self.first = None
        self.count = 0
        return n

    def duration(self, now):
        return 0.0 if self.first is None else now - self.first

class TimeSync:
    """Track GPS time vs system time correlation"""
    
    def __init__(self, log_filename_base):
        self.sync_filename = f"{log_filename_base}_timesync.csv"
        self.sync_file = None
        self.csv_writer = None
        
    def open(self):
        """Open time sync log file"""
        self.sync_file = open(self.sync_filename, 'w', newline='')
        self.csv_writer = csv.writer(self.sync_file)
        self.csv_writer.writerow([
            'system_time',
            'gps_time', 
            'gps_week',
            'gps_tow',
            'offset_seconds',
            'num_satellites'
        ])
        print(f"✓ Time sync logging to: {self.sync_filename}")
        
    def log(self, system_time, gps_datetime, gps_week, gps_tow, num_sv):
        """Log time correlation"""
        if self.csv_writer:
            gps_timestamp = gps_datetime.timestamp()
            offset = system_time - gps_timestamp
            self.csv_writer.writerow([
                f"{system_time:.6f}",
                gps_datetime.isoformat(),
                gps_week,
                f"{gps_tow:.3f}",
                f"{offset:.6f}",
                num_sv
            ])
            self.sync_file.flush()

    def fsync(self):
        """Push the time sync rows to the card."""
        if self.sync_file:
            self.sync_file.flush()
            os.fsync(self.sync_file.fileno())

    def close(self):
        """Close time sync log"""
        if self.sync_file:
            self.sync_file.close()
            print(f"✓ Time sync log closed: {self.sync_filename}")


class NTRIPClient:
    """NTRIP client to fetch RTK corrections"""
    
    def __init__(self, host, port, mountpoint, username, password):
        self.host = host
        self.port = port
        self.mountpoint = mountpoint
        self.username = username
        self.password = password
        self.socket = None
        self.running = False
        
    def connect(self, verbose=True):
        """Connect to NTRIP caster"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # Bounded connect: an unreachable caster (collar away from the
            # lab network) used to block here for up to two minutes.
            self.socket.settimeout(5)
            self.socket.connect((self.host, self.port))
            self.socket.settimeout(10)  # recv in read_corrections must not hang forever either
            
            # Build NTRIP request
            request = (
                f"GET /{self.mountpoint} HTTP/1.0\r\n"
                f"User-Agent: NTRIP PythonClient/1.0\r\n"
            )
            
            # Add authentication header if credentials provided
            if self.username and self.password:
                auth = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
                request += f"Authorization: Basic {auth}\r\n"
            
            request += "\r\n"
            
            self.socket.sendall(request.encode())
            
            # Read response
            response = self.socket.recv(1024).decode()
            if "200 OK" not in response:
                raise Exception(f"NTRIP connection failed: {response}")
            
            auth_str = " (authenticated)" if self.username else " (no auth)"
            print(f"✓ Connected to NTRIP: {self.host}:{self.port}/{self.mountpoint}{auth_str}")
            return True
            
        except Exception as e:
            if verbose:
                print(f"✗ NTRIP connection failed: {e}")
            self.disconnect()
            return False

    def disconnect(self):
        """Drop the socket (the worker reconnects later)"""
        if self.socket:
            try:
                self.socket.close()
            except OSError:
                pass
        self.socket = None

    def read_corrections(self):
        """Read RTCM corrections from NTRIP stream.

        Returns the bytes read, b'' when the caster closed the stream, or
        None on a socket error or 10 s without data."""
        if self.socket:
            try:
                data = self.socket.recv(1024)
                return data
            except:
                return None
        return None
    
    def close(self):
        """Close NTRIP connection"""
        if self.socket:
            self.disconnect()
            print("✓ NTRIP connection closed")


class GPSLogger:
    """Main GPS logger class with time synchronization"""
    
    def __init__(self, serial_port, baudrate=230400, ntrip_config=None, log_dir=None,
                 status_every=STATUS_EVERY_S):
        self.serial_port = serial_port
        self.status_every = status_every
        self.errors = ErrorStreak(READ_ERROR_LIMIT_S)
        self.exit_code = 0
        self.baudrate = baudrate
        self.ntrip_config = ntrip_config
        
        # Serial connection
        self.ser = None
        self.ubr = None
        
        # NTRIP client
        self.ntrip = None
        
        # Logging
        log_dir = log_dir or "./logs"
        os.makedirs(log_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.log_filename_base = os.path.join(log_dir, f"gps_log_{timestamp}")
        self.log_filename = f"{self.log_filename_base}.ubx"
        self.logfile = None
        
        # Time synchronization
        self.timesync = TimeSync(self.log_filename_base)
        self.last_timesync_log = 0
        
        # Statistics
        self.stats = {
            'messages': 0,
            'bytes_logged': 0,
            'rtcm_bytes': 0,
            'start_time': None,
            'last_fix_type': 0,
            'last_carr_soln': 0,  # Carrier solution status (0=none, 1=float, 2=fixed)
            'last_num_sv': 0,
            'last_lat': 0.0,
            'last_lon': 0.0,
            'last_height': 0.0,
            'time_offset': 0.0  # GPS time - system time
        }
        
        # Threading
        self.running = False
        self.ntrip_thread = None
        self.read_thread = None
        
    def connect_serial(self):
        """Connect to GPS module via serial"""
        try:
            self.ser = serial.Serial(
                self.serial_port,
                self.baudrate,
                timeout=1
            )
            # Exclusive open: a second logger or a console test opening the
            # same port would otherwise steal bytes from the .ubx stream.
            try:
                fcntl.ioctl(self.ser.fileno(), termios.TIOCEXCL)
            except (OSError, AttributeError, ValueError) as e:
                print(f"⚠ Could not get exclusive access to {self.serial_port}: {e}")
            self.ubr = UBXReader(self.ser)
            print(f"✓ Connected to GPS: {self.serial_port} @ {self.baudrate} baud")
            return True
        except Exception as e:
            print(f"✗ Serial connection failed: {e}")
            return False
    
    def connect_ntrip(self):
        """Connect to NTRIP server if configured"""
        if not self.ntrip_config:
            print("⚠ NTRIP not configured - running without RTK corrections")
            return True
        
        self.ntrip = NTRIPClient(
            self.ntrip_config['host'],
            self.ntrip_config['port'],
            self.ntrip_config['mountpoint'],
            self.ntrip_config['username'],
            self.ntrip_config['password']
        )
        return self.ntrip.connect()
    
    def open_logfile(self):
        """Open log file for writing"""
        try:
            self.logfile = open(self.log_filename, 'wb')
            print(f"✓ Logging UBX to: {self.log_filename}")
            self.timesync.open()
            return True
        except Exception as e:
            print(f"✗ Failed to open log file: {e}")
            return False
    
    def _sleep_while_running(self, seconds):
        """Sleep up to seconds, returning early (False) when stopping."""
        t_end = time.monotonic() + seconds
        while self.running and time.monotonic() < t_end:
            time.sleep(0.5)
        return self.running

    def ntrip_worker(self):
        """Thread worker to read NTRIP corrections and send to GPS.

        If the caster is unreachable or the stream drops, it retries every
        NTRIP_RETRY_S seconds; the .ubx logging carries on without
        corrections meanwhile (degraded, not fatal)."""
        print("⚡ NTRIP correction thread started")

        down_reported = False
        last_rtcm_print = time.monotonic()
        rtcm_since_print = 0

        while self.running and self.ntrip:
            if self.ntrip.socket is None:
                if not down_reported:
                    print(f"⚠ NTRIP not connected; retrying every {NTRIP_RETRY_S:.0f} s "
                          "(logging continues without corrections)")
                    down_reported = True
                if not self._sleep_while_running(NTRIP_RETRY_S):
                    break
                if self.ntrip.connect(verbose=False):
                    down_reported = False
                continue

            corrections = self.ntrip.read_corrections()
            if not corrections:  # b'' = caster closed the stream, None = error / 10 s silence
                print("⚠ NTRIP stream dropped" + (" (closed by caster)" if corrections == b"" else ""))
                self.ntrip.disconnect()
                continue
            try:
                # Send RTCM corrections to GPS module
                self.ser.write(corrections)
                self.ser.flush()  # Ensure data is sent immediately
            except Exception as e:
                print(f"⚠ NTRIP: sending corrections to the receiver failed: {e}")
                time.sleep(1)
                continue
            self.stats['rtcm_bytes'] += len(corrections)
            rtcm_since_print += 1
            now = time.monotonic()
            if now - last_rtcm_print >= self.status_every:
                print(f"[RTCM] {rtcm_since_print} blocks in {now - last_rtcm_print:.0f} s, "
                      f"total {self.stats['rtcm_bytes']} bytes")
                last_rtcm_print = now
                rtcm_since_print = 0

        print("⚡ NTRIP correction thread stopped")

    def read_worker(self):
        """Thread worker to read GPS data and log it"""
        print("⚡ GPS read thread started")
        
        last_status_time = time.time()
        last_flush = last_fsync = time.monotonic()
        parse_errors = 0
        total_reads = 0

        while self.running:
            try:
                # Record system time
                system_time = time.time()
                
                # Check serial buffer usage (if supported)
                try:
                    in_waiting = self.ser.in_waiting
                    if in_waiting > 32768:  # More than half full
                        print(f"\n⚠ Serial buffer high: {in_waiting} bytes waiting")
                except:
                    pass
                
                # Read UBX message
                total_reads += 1
                (raw_data, parsed_msg) = self.ubr.read()
                
                if raw_data:
                    # Log raw binary data (for PPK)
                    self.logfile.write(raw_data)
                    self.stats['bytes_logged'] += len(raw_data)
                    self.stats['messages'] += 1
                    
                    # Parse specific messages for monitoring
                    # Note: parsed_msg may be None if message couldn't be parsed
                    if parsed_msg and parsed_msg.identity == 'NAV-PVT':
                        self.stats['last_fix_type'] = parsed_msg.fixType
                        self.stats['last_num_sv'] = parsed_msg.numSV
                        self.stats['last_lat'] = parsed_msg.lat
                        self.stats['last_lon'] = parsed_msg.lon
                        self.stats['last_height'] = parsed_msg.height / 1000.0  # mm to m
                        
                        # Store carrier solution status for RTK detection
                        self.stats['last_carr_soln'] = getattr(parsed_msg, 'carrSoln', 0)
                        
                        # Log time synchronization every 10 seconds
                        if system_time - self.last_timesync_log >= 10.0:
                            try:
                                gps_datetime = datetime(
                                    parsed_msg.year, parsed_msg.month, parsed_msg.day,
                                    parsed_msg.hour, parsed_msg.min, parsed_msg.second,
                                    microsecond=int(parsed_msg.nano / 1000),
                                    tzinfo=timezone.utc  # GPS time is UTC-based
                                )
                                
                                # Calculate GPS week and time of week
                                gps_epoch = datetime(1980, 1, 6, tzinfo=timezone.utc)
                                delta = gps_datetime - gps_epoch
                                gps_week = int(delta.total_seconds() / (7 * 24 * 3600))
                                gps_tow = delta.total_seconds() % (7 * 24 * 3600)
                                
                                # Log correlation
                                self.timesync.log(
                                    system_time,
                                    gps_datetime,
                                    gps_week,
                                    gps_tow,
                                    parsed_msg.numSV
                                )
                                
                                # Calculate offset for display
                                gps_timestamp = gps_datetime.timestamp()
                                self.stats['time_offset'] = system_time - gps_timestamp
                                
                                self.last_timesync_log = system_time
                            except:
                                pass
                    elif not parsed_msg:
                        # Track parse errors
                        parse_errors += 1
                        if parse_errors % 100 == 0:
                            error_rate = (parse_errors / total_reads) * 100
                            print(f"\n⚠ Parse error rate: {error_rate:.1f}% ({parse_errors}/{total_reads})")
                    
                    # Print status every status_every seconds
                    if time.time() - last_status_time >= self.status_every:
                        self.print_status()
                        last_status_time = time.time()

                # The .ubx goes through a buffered file: hand it to the kernel
                # every second and to the card every few seconds, so a power
                # cut loses seconds of data, not the buffer.
                now_m = time.monotonic()
                if now_m - last_flush >= FLUSH_EVERY_S:
                    self.logfile.flush()
                    last_flush = now_m
                    if now_m - last_fsync >= FSYNC_EVERY_S:
                        os.fsync(self.logfile.fileno())
                        self.timesync.fsync()
                        last_fsync = now_m

                n = self.errors.ok()
                if n:
                    print(f"✓ Reading again after {n} read/write error(s)")

            except Exception as e:
                if not self.running:  # shutting down: the port was closed under us
                    break
                now_m = time.monotonic()
                state = self.errors.error(now_m)
                if state == "first":
                    print(f"⚠ Read/write error: {e} (exiting with status {EXIT_READ_ERRORS} "
                          f"if errors continue for {self.errors.limit_s:g} s)")
                elif state == "give_up":
                    print(f"✗ {self.errors.count} consecutive read/write errors over "
                          f"{self.errors.duration(now_m):.1f} s (last: {e}); exiting with status {EXIT_READ_ERRORS}")
                    self.exit_code = EXIT_READ_ERRORS
                    self.running = False
                    break
                time.sleep(0.1)

        print("⚡ GPS read thread stopped")
    
    def print_status(self):
        """Print current GPS status"""
        # Base fix types
        fix_types = {
            0: "NO FIX",
            1: "DEAD RECKONING",
            2: "2D FIX",
            3: "3D FIX",
            4: "GNSS+DR",
            5: "TIME ONLY"
        }
        
        # Determine actual fix status combining fixType and carrSoln
        fix_type = self.stats['last_fix_type']
        carr_soln = self.stats['last_carr_soln']
        
        if carr_soln == 2:
            # RTK Fixed solution
            fix_str = "RTK FIXED"
        elif carr_soln == 1:
            # RTK Float solution
            fix_str = "RTK FLOAT"
        else:
            # Use base fix type
            fix_str = fix_types.get(fix_type, "UNKNOWN")
        
        # Estimate data rate
        if self.stats['start_time']:
            elapsed = time.time() - self.stats['start_time']
            msg_rate = self.stats['messages'] / elapsed if elapsed > 0 else 0
        else:
            msg_rate = 0
        
        print(f"[{datetime.now().strftime('%H:%M:%S')}] "
              f"Fix: {fix_str:15s} | "
              f"Sats: {self.stats['last_num_sv']:2d} | "
              f"Lat: {self.stats['last_lat']:11.7f} | "
              f"Lon: {self.stats['last_lon']:11.7f} | "
              f"Alt: {self.stats['last_height']:7.2f}m | "
              f"Time offset: {self.stats['time_offset']:+7.3f}s | "
              f"Msgs: {self.stats['messages']:5d} ({msg_rate:.1f} Hz) | "
              f"Logged: {self.stats['bytes_logged']/1024:.1f} KB | "
              f"RTCM: {self.stats['rtcm_bytes']/1024:.1f} KB{' !!' if self.ntrip else ''}",
              flush=True)
    
    def start(self):
        """Start logging"""
        print("\n" + "="*80)
        print("GPS Logger with Time Synchronization")
        print("="*80 + "\n")
        
        # Connect to GPS
        if not self.connect_serial():
            return False
        
        # Connect to NTRIP
        if not self.connect_ntrip():
            print("⚠ Continuing without NTRIP corrections...")
        
        # Open log file
        if not self.open_logfile():
            return False
        
        # Start threads
        self.running = True
        self.stats['start_time'] = time.time()
        
        # Start NTRIP thread if configured
        if self.ntrip:
            self.ntrip_thread = threading.Thread(target=self.ntrip_worker, daemon=True)
            self.ntrip_thread.start()
        
        # Start GPS read thread
        self.read_thread = threading.Thread(target=self.read_worker, daemon=True)
        self.read_thread.start()
        
        print("\n✓ Logging started - Press Ctrl+C to stop\n")
        print(f"Status display updates every {self.status_every:g} seconds:")
        print("Time offset shows (System Time - GPS Time)")
        print("-" * 80)
        
        return True
    
    def stop(self):
        """Stop logging"""
        print("\n\n" + "="*80)
        print("Stopping GPS Logger...")
        print("="*80)
        
        self.running = False
        
        # Wait for threads to finish
        if self.read_thread:
            self.read_thread.join(timeout=2)
        if self.ntrip_thread:
            self.ntrip_thread.join(timeout=2)
        
        # Close connections
        if self.logfile:
            try:
                self.logfile.flush()
                os.fsync(self.logfile.fileno())
            except (OSError, ValueError) as e:
                print(f"⚠ Could not sync {self.log_filename}: {e}")
            self.logfile.close()
            print(f"✓ Log file closed: {self.log_filename}")
        
        self.timesync.close()
        
        if self.ntrip:
            self.ntrip.close()
        
        if self.ser:
            self.ser.close()
            print("✓ Serial connection closed")
        
        # Print final statistics
        if self.stats['start_time']:
            elapsed = time.time() - self.stats['start_time']
            avg_rate = self.stats['messages'] / elapsed if elapsed > 0 else 0
            
            print(f"\nFinal Statistics:")
            print(f"  Duration: {elapsed:.1f} seconds")
            print(f"  Messages: {self.stats['messages']} ({avg_rate:.1f} Hz average)")
            print(f"  Data logged: {self.stats['bytes_logged']/1024/1024:.2f} MB")
            print(f"  RTCM received: {self.stats['rtcm_bytes']/1024:.1f} KB")
            print(f"  Final time offset: {self.stats['time_offset']:+.3f} seconds")
        
        print(f"\n✓ Shutdown complete\n")


def main():
    """Main entry point"""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', default='/dev/ttyAMA4')
    parser.add_argument('--baud', type=int, default=460800)
    parser.add_argument('--log-dir', default=None)
    parser.add_argument('--ntrip', default=os.environ.get('EWEGO_NTRIP', ''),
                        help='NTRIP caster as HOST:PORT/MOUNTPOINT[,USER:PASSWORD], e.g. '
                             '192.168.1.213:2101/sheep (the lab Sparkfun caster). '
                             'Default: $EWEGO_NTRIP, or disabled.')
    parser.add_argument('--no-ntrip', action='store_true', help='Disable RTK corrections')
    parser.add_argument('--status-every', type=float, default=STATUS_EVERY_S,
                        help=f'seconds between status lines (default {STATUS_EVERY_S:g})')
    args = parser.parse_args()

    # ========== CONFIGURATION ==========
    SERIAL_PORT = args.port
    BAUDRATE = args.baud

    # NTRIP configuration: None disables corrections. Previously hard-coded to
    # the lab caster, which made the logger block on connect away from the lab.
    NTRIP_CONFIG = None
    if args.ntrip and not args.no_ntrip:
        try:
            spec, _, auth = args.ntrip.partition(',')
            hostport, _, mountpoint = spec.partition('/')
            host, _, port = hostport.partition(':')
            user, _, password = auth.partition(':') if auth else (None, None, None)
            NTRIP_CONFIG = {
                'host': host,
                'port': int(port or 2101),
                'mountpoint': mountpoint or 'sheep',
                'username': user or None,
                'password': password or None,
            }
        except ValueError:
            print(f"✗ Bad --ntrip value {args.ntrip!r}; expected HOST:PORT/MOUNTPOINT[,USER:PASSWORD]")
            sys.exit(2)

    # ===================================
    
    # Create logger
    logger = GPSLogger(
        serial_port=SERIAL_PORT,
        baudrate=BAUDRATE,
        ntrip_config=NTRIP_CONFIG,
        log_dir=args.log_dir,
        status_every=args.status_every
    )

    # Start logging; a failed start must exit non-zero so the unit's
    # Restart=on-failure tries again
    if not logger.start():
        logger.stop()
        sys.exit(EXIT_START_FAILED)

    # Run until Ctrl+C / SIGINT, or until the read thread gives up
    interrupted = False
    try:
        while logger.read_thread.is_alive():
            time.sleep(0.2)
    except KeyboardInterrupt:
        interrupted = True
    if not interrupted and logger.exit_code == 0:
        print("✗ GPS read thread ended unexpectedly")
        logger.exit_code = EXIT_READ_ERRORS

    # Stop logging
    logger.stop()
    sys.exit(logger.exit_code)


if __name__ == '__main__':
    main()
