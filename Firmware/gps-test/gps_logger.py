#!/usr/bin/env python3
"""
GPS Logger with Time Synchronization Tracking
Logs GPS data and maintains correlation between GPS time and system time
"""

import serial
import threading
import time
import sys
import os
import base64
from datetime import datetime, timezone
from pyubx2 import UBXReader
import socket
import csv

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
        
    def connect(self):
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
            print(f"✗ NTRIP connection failed: {e}")
            return False
    
    def read_corrections(self):
        """Read RTCM corrections from NTRIP stream"""
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
            self.socket.close()
            print("✓ NTRIP connection closed")


class GPSLogger:
    """Main GPS logger class with time synchronization"""
    
    def __init__(self, serial_port, baudrate=230400, ntrip_config=None, log_dir=None):
        self.serial_port = serial_port
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
    
    def ntrip_worker(self):
        """Thread worker to read NTRIP corrections and send to GPS"""
        print("⚡ NTRIP correction thread started")
        
        rtcm_count = 0
        last_rtcm_time = time.time()
        
        while self.running and self.ntrip:
            try:
                corrections = self.ntrip.read_corrections()
                if corrections and len(corrections) > 0:
                    # Send RTCM corrections to GPS module
                    bytes_written = self.ser.write(corrections)
                    self.ser.flush()  # Ensure data is sent immediately
                    self.stats['rtcm_bytes'] += len(corrections)
                    rtcm_count += 1
                    
                    # Debug output every 10 RTCM messages
                    if rtcm_count % 10 == 0:
                        elapsed = time.time() - last_rtcm_time
                        rate = 10.0 / elapsed if elapsed > 0 else 0
                        print(f"\n[RTCM] Sent {len(corrections)} bytes (rate: {rate:.1f} msg/s, total: {self.stats['rtcm_bytes']} bytes)")
                        last_rtcm_time = time.time()
                else:
                    time.sleep(0.1)  # Small delay if no data
                    
            except Exception as e:
                print(f"⚠ NTRIP error: {e}")
                time.sleep(1)
        
        print("⚡ NTRIP correction thread stopped")
    
    def read_worker(self):
        """Thread worker to read GPS data and log it"""
        print("⚡ GPS read thread started")
        
        last_status_time = time.time()
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
                    
                    # Print status every 2 seconds
                    if time.time() - last_status_time >= 2.0:
                        self.print_status()
                        last_status_time = time.time()
                        
            except Exception as e:
                if self.running:  # Only print if not shutting down
                    print(f"\n⚠ Read error: {e}")
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
        
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] "
              f"Fix: {fix_str:15s} | "
              f"Sats: {self.stats['last_num_sv']:2d} | "
              f"Lat: {self.stats['last_lat']:11.7f} | "
              f"Lon: {self.stats['last_lon']:11.7f} | "
              f"Alt: {self.stats['last_height']:7.2f}m | "
              f"Time offset: {self.stats['time_offset']:+7.3f}s | "
              f"Msgs: {self.stats['messages']:5d} ({msg_rate:.1f} Hz) | "
              f"Logged: {self.stats['bytes_logged']/1024:.1f} KB | "
              f"RTCM: {self.stats['rtcm_bytes']/1024:.1f} KB{' !!' if self.ntrip else ''}",
              end='', flush=True)
    
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
        print("Status display updates every 2 seconds:")
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
        log_dir=args.log_dir
    )
    
    # Start logging
    if logger.start():
        try:
            # Run until Ctrl+C
            while True:
                time.sleep(0.1)
        except KeyboardInterrupt:
            pass
    
    # Stop logging
    logger.stop()


if __name__ == '__main__':
    main()
