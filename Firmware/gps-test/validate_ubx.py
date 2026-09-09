#!/usr/bin/env python3
"""
UBX Log Validator
Analyzes logged UBX files to verify they're suitable for PPK processing
"""

import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pyubx2 import UBXReader
import os

# Navigation rate configured on the module (CFG-RATE-MEAS = 200 ms, see CONFIGURING_HW.md)
EXPECTED_RATE_HZ = 5.0


class UBXValidator:
    """Validates UBX log files for PPK processing"""
    
    def __init__(self, filename):
        self.filename = filename
        self.stats = {
            'total_messages': 0,
            'message_types': defaultdict(int),
            'first_timestamp': None,
            'last_timestamp': None,
            'nav_pvt_count': 0,
            'rxm_rawx_count': 0,
            'rxm_sfrbx_count': 0,
            'nav_status_count': 0,
            'nav_sat_count': 0,
            'satellites_seen': set(),
            'max_satellites': 0,
            'fix_types': defaultdict(int),
            'carrier_solution_types': defaultdict(int),
            'errors': [],
            'warnings': []
        }
        
    def validate(self):
        """Validate the UBX file"""
        print("\n" + "="*80)
        print(f"UBX Log Validator")
        print("="*80 + "\n")
        
        # Check file exists and size
        if not os.path.exists(self.filename):
            print(f"✗ Error: File not found: {self.filename}\n")
            return False
        
        file_size = os.path.getsize(self.filename)
        print(f"File: {self.filename}")
        print(f"Size: {file_size:,} bytes ({file_size/1024/1024:.2f} MB)\n")
        
        if file_size == 0:
            print("✗ Error: File is empty!\n")
            return False
        
        # Parse file
        print("Parsing UBX messages...")
        try:
            with open(self.filename, 'rb') as f:
                ubr = UBXReader(f)
                
                for raw_data, parsed_msg in ubr:
                    self.stats['total_messages'] += 1
                    self._process_message(parsed_msg)
                    
                    # Progress indicator
                    if self.stats['total_messages'] % 1000 == 0:
                        print(f"\r  Processed {self.stats['total_messages']:,} messages...", 
                              end='', flush=True)
        
        except Exception as e:
            print(f"\n✗ Error parsing file: {e}\n")
            return False
        
        print(f"\r  Processed {self.stats['total_messages']:,} messages... Done!\n")
        
        # Analyze results
        self._analyze()
        
        # Print report
        self._print_report()
        
        return len(self.stats['errors']) == 0
    
    def _process_message(self, msg):
        """Process a single UBX message"""
        msg_id = msg.identity
        self.stats['message_types'][msg_id] += 1
        
        # NAV-PVT: Position, velocity, time
        if msg_id == 'NAV-PVT':
            self.stats['nav_pvt_count'] += 1
            
            # Extract timestamp
            try:
                ts = datetime(msg.year, msg.month, msg.day, 
                            msg.hour, msg.min, msg.second)
                if self.stats['first_timestamp'] is None:
                    self.stats['first_timestamp'] = ts
                self.stats['last_timestamp'] = ts
            except:
                pass
            
            # Track fix types
            fix_type = getattr(msg, 'fixType', None)
            if fix_type is not None:
                self.stats['fix_types'][fix_type] += 1
            
            # Track carrier solution (RTK status)
            carr_soln = getattr(msg, 'carrSoln', None)
            if carr_soln is not None:
                self.stats['carrier_solution_types'][carr_soln] += 1
            
            # Track satellites
            num_sv = getattr(msg, 'numSV', 0)
            if num_sv > self.stats['max_satellites']:
                self.stats['max_satellites'] = num_sv
        
        # RXM-RAWX: Raw measurements (critical for PPK)
        elif msg_id == 'RXM-RAWX':
            self.stats['rxm_rawx_count'] += 1
            
            # Track which satellites have observations
            num_meas = getattr(msg, 'numMeas', 0)
            if num_meas > self.stats['max_satellites']:
                self.stats['max_satellites'] = num_meas
        
        # RXM-SFRBX: Broadcast navigation data (ephemeris - critical for PPK)
        elif msg_id == 'RXM-SFRBX':
            self.stats['rxm_sfrbx_count'] += 1
        
        # NAV-STATUS: Navigation status
        elif msg_id == 'NAV-STATUS':
            self.stats['nav_status_count'] += 1
        
        # NAV-SAT: Satellite information
        elif msg_id == 'NAV-SAT':
            self.stats['nav_sat_count'] += 1
    
    def _analyze(self):
        """Analyze collected statistics"""
        
        # Check for critical message types
        if self.stats['rxm_rawx_count'] == 0:
            self.stats['errors'].append(
                "No RXM-RAWX messages found! These are REQUIRED for PPK processing."
            )
        
        if self.stats['rxm_sfrbx_count'] == 0:
            self.stats['errors'].append(
                "No RXM-SFRBX messages found! These contain ephemeris data REQUIRED for PPK."
            )
        
        # Check message rates
        if self.stats['first_timestamp'] and self.stats['last_timestamp']:
            duration = (self.stats['last_timestamp'] - self.stats['first_timestamp']).total_seconds()
            
            if duration > 0:
                nav_pvt_rate = self.stats['nav_pvt_count'] / duration
                rxm_rawx_rate = self.stats['rxm_rawx_count'] / duration
                
                if nav_pvt_rate < EXPECTED_RATE_HZ * 0.95:  # Allow some tolerance
                    self.stats['warnings'].append(
                        f"NAV-PVT rate is {nav_pvt_rate:.1f} Hz (expected ~{EXPECTED_RATE_HZ:g} Hz)"
                    )
                
                if rxm_rawx_rate < EXPECTED_RATE_HZ * 0.95:
                    self.stats['warnings'].append(
                        f"RXM-RAWX rate is {rxm_rawx_rate:.1f} Hz (expected ~{EXPECTED_RATE_HZ:g} Hz)"
                    )
        
        # Check satellite count
        if self.stats['max_satellites'] < 5:
            self.stats['warnings'].append(
                f"Low satellite count (max: {self.stats['max_satellites']}). "
                "Need at least 5-6 for good PPK results."
            )
    
    def _print_report(self):
        """Print validation report"""
        print("="*80)
        print("VALIDATION REPORT")
        print("="*80 + "\n")
        
        # Time span
        if self.stats['first_timestamp'] and self.stats['last_timestamp']:
            duration = self.stats['last_timestamp'] - self.stats['first_timestamp']
            print(f"Time Span:")
            print(f"  Start:    {self.stats['first_timestamp']}")
            print(f"  End:      {self.stats['last_timestamp']}")
            print(f"  Duration: {duration} ({duration.total_seconds():.1f} seconds)")
            print()
        
        # Message statistics
        print(f"Message Statistics:")
        print(f"  Total messages: {self.stats['total_messages']:,}")
        print()
        
        # Critical messages for PPK
        print("Critical Messages for PPK:")
        print(f"  ✓ RXM-RAWX (raw observations):  {self.stats['rxm_rawx_count']:,} messages", end='')
        if self.stats['rxm_rawx_count'] > 0:
            if self.stats['last_timestamp']:
                duration = (self.stats['last_timestamp'] - self.stats['first_timestamp']).total_seconds()
                if duration > 0:
                    rate = self.stats['rxm_rawx_count'] / duration
                    print(f" ({rate:.1f} Hz)")
                else:
                    print()
            else:
                print()
        else:
            print(" ✗ MISSING!")
        
        print(f"  ✓ RXM-SFRBX (ephemeris):        {self.stats['rxm_sfrbx_count']:,} messages", end='')
        if self.stats['rxm_sfrbx_count'] == 0:
            print(" ✗ MISSING!")
        else:
            print()
        print()
        
        # Position/status messages
        print("Position/Status Messages:")
        print(f"  NAV-PVT (position/velocity):    {self.stats['nav_pvt_count']:,} messages", end='')
        if self.stats['nav_pvt_count'] > 0 and self.stats['last_timestamp']:
            duration = (self.stats['last_timestamp'] - self.stats['first_timestamp']).total_seconds()
            if duration > 0:
                rate = self.stats['nav_pvt_count'] / duration
                print(f" ({rate:.1f} Hz)")
            else:
                print()
        else:
            print()
        
        print(f"  NAV-STATUS (fix status):         {self.stats['nav_status_count']:,} messages")
        print(f"  NAV-SAT (satellite info):        {self.stats['nav_sat_count']:,} messages")
        print()
        
        # Fix type distribution
        if self.stats['fix_types']:
            fix_type_names = {
                0: "NO FIX",
                1: "DEAD RECKONING",
                2: "2D FIX",
                3: "3D FIX",
                4: "GNSS+DR",
                5: "TIME ONLY"
            }
            
            print("Fix Type Distribution:")
            for fix_type, count in sorted(self.stats['fix_types'].items()):
                name = fix_type_names.get(fix_type, f"UNKNOWN ({fix_type})")
                percent = 100 * count / self.stats['nav_pvt_count'] if self.stats['nav_pvt_count'] > 0 else 0
                print(f"  {name:20s}: {count:5d} ({percent:5.1f}%)")
            print()
        
        # Carrier solution distribution (RTK status)
        if self.stats['carrier_solution_types']:
            carr_soln_names = {
                0: "No carrier",
                1: "Float solution",
                2: "Fixed solution"
            }
            
            print("RTK Carrier Solution Distribution:")
            for carr_soln, count in sorted(self.stats['carrier_solution_types'].items()):
                name = carr_soln_names.get(carr_soln, f"UNKNOWN ({carr_soln})")
                percent = 100 * count / self.stats['nav_pvt_count'] if self.stats['nav_pvt_count'] > 0 else 0
                print(f"  {name:20s}: {count:5d} ({percent:5.1f}%)")
            print()
        
        # Satellite statistics
        print(f"Satellite Statistics:")
        print(f"  Max satellites observed: {self.stats['max_satellites']}")
        print()
        
        # All message types
        print("All Message Types:")
        for msg_type, count in sorted(self.stats['message_types'].items(), 
                                      key=lambda x: x[1], reverse=True):
            print(f"  {msg_type:20s}: {count:,}")
        print()
        
        # Warnings
        if self.stats['warnings']:
            print("⚠ WARNINGS:")
            for warning in self.stats['warnings']:
                print(f"  - {warning}")
            print()
        
        # Errors
        if self.stats['errors']:
            print("✗ ERRORS:")
            for error in self.stats['errors']:
                print(f"  - {error}")
            print()
        
        # Final verdict
        print("="*80)
        if self.stats['errors']:
            print("✗ VALIDATION FAILED: File is NOT suitable for PPK processing")
            print("  Please check your u-center configuration and re-record data.")
        else:
            print("✓ VALIDATION PASSED: File appears suitable for PPK processing")
            if self.stats['warnings']:
                print("  Some warnings were found - review above for details.")
            else:
                print("  No issues detected!")
            print("\n  Next steps:")
            print("  1. Convert to RINEX: convbin -r ubx -o rinex -od -os -v 3.04 <file.ubx>")
            print("  2. Process with RTKLIB: rnx2rtkp <rover.obs> <base.obs> <nav_file>")
        print("="*80 + "\n")


def main():
    """Main entry point"""
    
    if len(sys.argv) < 2:
        print("\nUsage: python validate_ubx.py <ubx_file>\n")
        print("Example: python validate_ubx.py gps_log_20260105_142315.ubx\n")
        sys.exit(1)
    
    filename = sys.argv[1]
    
    validator = UBXValidator(filename)
    success = validator.validate()
    
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
