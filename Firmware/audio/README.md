# Audio Testing Scripts

Simple utilities for testing audio capture on a Raspberry Pi using ALSA's `arecord`.

## On the wearable: unit `ewego-audio`

The recording unit runs `arecord` directly through
`/usr/local/bin/ewego-audio-record` (source: `image/ewego/audio-record.sh`),
not `record_audio.py`. arecord is the unit's main process, so the unit's
SIGINT reaches it and it finishes the WAV header before exiting (the old
Python wrapper let `subprocess.run` SIGKILL arecord 250 ms after the
interrupt).

- Output: `/opt/ewego/recordings/audio/<YYYYmmdd_HHMMSS>/audio.wav`, 48 kHz,
  stereo, S32_LE (384 kB/s).
- Rollover: a WAV file stops at 2 GiB (about 93 minutes at this format).
  arecord then renames `audio.wav` to `audio-01.wav` and continues in
  `audio-02.wav`, `audio-03.wav`, ... `ewego-check.py` treats all of them as
  one recording, and `--collect` links each part.
- Device: the voicehat card (the stereo PDM mics) is found by name in
  `/proc/asound/cards` and used as `plughw:CARD=<id>,DEV=0`, the same way
  the web console finds it; ALSA `default` is not guaranteed to be that card.
  Set `DEVICE=` in `/etc/ewego/audio.conf` to pin another device;
  `ewego-audio-record --print-device` shows the choice.

## Scripts

### `record_audio.py`

Bench recorder (`--output FILE`, `--duration S`, `--device DEV`) with the
same format. Kept for bench use; the unit no longer uses it.

### `local_test.sh`

Captures audio locally with a real-time VU meter display. Useful for testing audio input levels on the device itself.

```bash
./local_test.sh
```

Audio is recorded to `/dev/null` (discarded) — this is for monitoring input levels only.

### `wireless_test.sh`

Streams audio from a remote Raspberry Pi to your local machine for playback and monitoring.

```bash
./wireless_test.sh
```

You will be prompted for the Pi's address (defaults to `pi.local`). The script:
1. SSHs into the Pi
2. Captures audio using `arecord`
3. Streams it back via SSH
4. Plays it locally using `ffplay` with minimal latency settings

#### Requirements for wireless mode:
- `ffplay` (from FFmpeg) installed on the local machine
- SSH access to the Raspberry Pi
- Pi must have `arecord` available

## Usage Examples

**Test audio locally on the Pi:**
```bash
ssh pi@pi.local
./local_test.sh
```

**Stream audio from Pi to your computer:**
```bash
./wireless_test.sh
# Enter: raspberrypi.local
```

## Troubleshooting

### No audio device found
List available ALSA devices:
```bash
arecord -l
```

If your device is on a different card/device number, update the `-D hw:X,Y` parameter in the scripts.
