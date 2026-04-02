import serial
import time

# Use your confirmed port
PORT = "/dev/ttyAMA5"
BAUD = 115200

def raw_diag():
    print(f"--- BNO055 Raw Hardware Probe on {PORT} ---")
    try:
        ser = serial.Serial(PORT, BAUD, timeout=1.0)
        
        # Step 1: Force the sensor's UART out of a "hung" state
        # We send a long string of nulls and then a reset command
        print("Attempting to force-clear the bus...")
        ser.write(b'\x00' * 10) 
        time.sleep(0.5)
        ser.reset_input_buffer()
        time.sleep(0.1)

        # Step 2: Manually Read Chip ID (Reg 0x00)
        # Protocol: [Start 0xAA] [Read 0x01] [Reg 0x00] [Len 0x01]
        print("Sending Chip ID request (0xAA 0x01 0x00 0x01)...")
        ser.write(b'\xAA\x01\x00\x01')
        
        response = ser.read(3) # Expect 0xBB 0x01 0xA0
        
        if response:
            print(f"Raw Response: {response.hex().upper()}")
            if response[0] == 0xBB:
                print("SUCCESS: Sensor is alive and talking!")
            elif response[0] == 0xEE:
                print(f"SENSOR ERROR: The chip is alive but reported error {hex(response[1])}")
        else:
            print("FAILURE: No response from sensor at 115200 baud.")

        ser.close()
    except Exception as e:
        print(f"PORT ERROR: {e}")

if __name__ == "__main__":
    raw_diag()
