#!/usr/bin/env python3
"""
Simple audio recorder script using ALSA (arecord)
Records audio from the default microphone and saves to a WAV file

Bench use only. The ewego-audio unit no longer runs this script: it runs
arecord directly through /usr/local/bin/ewego-audio-record
(image/ewego/audio-record.sh), which also picks the voicehat card by name.
"""

import subprocess
import sys
import signal
from datetime import datetime
from pathlib import Path

# Audio recording parameters (high quality)
CHANNELS = 2  # Stereo audio
RATE = 48000  # 48kHz sampling rate (professional standard)
FORMAT = "S32_LE"  # 32-bit signed little-endian (studio quality)

def record_audio(filename=None, duration=None, device=None):
    """
    Record audio from the microphone using ALSA's arecord

    Args:
        filename: Output filename (default: auto-generated with timestamp)
        duration: Recording duration in seconds (default: record until Ctrl+C)
        device: ALSA device name (default: system default)
    """
    if filename is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"recording_{timestamp}.wav"

    # Build arecord command
    cmd = [
        "arecord",
        "-f", FORMAT,           # Audio format (16-bit)
        "-c", str(CHANNELS),    # Number of channels
        "-r", str(RATE),        # Sample rate
    ]

    if device:
        cmd.extend(["-D", device])

    if duration:
        cmd.extend(["-d", str(int(duration))])  # Duration in seconds

    cmd.append(filename)

    print(f"Recording to: {filename}")
    print(f"Format: {CHANNELS} channels, {RATE}Hz, 32-bit")
    if duration:
        print(f"Duration: {duration} seconds")
    else:
        print("Press Ctrl+C to stop recording")

    proc = subprocess.Popen(cmd)
    try:
        rc = proc.wait()
    except KeyboardInterrupt:
        # Ctrl+C reached arecord too (same process group). Give it time to
        # finish the WAV header; subprocess.run would SIGKILL it after 250 ms.
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        print(f"\nRecording stopped by user")
        print(f"Recording saved to: {filename}")
        return 0
    if rc != 0:
        print(f"\nError during recording: arecord exited with {rc}", file=sys.stderr)
        return 1
    print(f"\nRecording saved to: {filename}")
    return 0

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Record audio from microphone using ALSA")
    parser.add_argument("-o", "--output", help="Output filename (default: recording_TIMESTAMP.wav)")
    parser.add_argument("-d", "--duration", type=float, help="Recording duration in seconds (default: until Ctrl+C)")
    parser.add_argument("-D", "--device", help="ALSA device name (default: system default)")

    args = parser.parse_args()

    try:
        exit_code = record_audio(filename=args.output, duration=args.duration, device=args.device)
        sys.exit(exit_code)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
