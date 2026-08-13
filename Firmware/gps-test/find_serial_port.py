#!/usr/bin/env python3
import serial.tools.list_ports

def show_serial_ports():
    print("=" * 60)
    print(" Available Serial Ports")
    print("=" * 60)
    
    # Get a list of all available serial ports
    ports = serial.tools.list_ports.comports()
    
    if not ports:
        print("No serial ports found!")
        return

    for port in sorted(ports):
        print(f"Device:      {port.device}")
        print(f"Name:        {port.name}")
        print(f"Description: {port.description}")
        print(f"Hardware ID: {port.hwid}")
        
        # Check USB-specific details if available
        if port.vid is not None and port.pid is not None:
            print(f"Vendor ID:   {hex(port.vid)}")
            print(f"Product ID:  {hex(port.pid)}")
            print(f"Manufacturer:{port.manufacturer}")
        
        print("-" * 60)

if __name__ == "__main__":
    show_serial_ports()