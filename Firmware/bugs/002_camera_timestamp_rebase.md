# Bug 002 — picamera2 rebases encoder timestamps to zero per encoder

## Symptom

`camera{1,2}_timestamps.bin` files where the first frame is `0` and subsequent
values are µs-since-first-frame. Not aligned to any global clock. All Michigan
2026-04-13 recordings show this pattern.

Cross-sensor alignment (camera vs IMU/audio/GPS/fuel gauge) impossible for
those recordings without a recovered `t0` per session — file mtime + chrony
gives ~seconds accuracy; audio content correlation gives ~ms.

## Root cause

`picamera2/encoders/encoder.py::_timestamp` rebases every encoder's output:

```python
def _timestamp(self, request):
    ts = int(request.request.metadata[controls.SensorTimestamp] / 1000)
    if self.firsttimestamp is None:
        self.firsttimestamp = ts
        timestamp_us = 0
    else:
        timestamp_us = ts - self.firsttimestamp
    return timestamp_us
```

The underlying `SensorTimestamp` IS `CLOCK_MONOTONIC` (libcamera V4L2 SoF), so
if you reach into `request.metadata` you get the same clock as
`time.monotonic_ns()`. But picamera2 subtracts `firsttimestamp` before the
value ever reaches `Output.outputframe(timestamp=...)`. So downstream code
that stores `timestamp` sees the rebased scalar, and every recording appears
to start at 0.

## History

- `bde3cf8` / `d80e844`: `RawTimestampOutput` did
  `aligned_ts = timestamp + (time.monotonic_ns()//1000 - timestamp_first)`
  on first frame, i.e. anchored to `CLOCK_MONOTONIC` via a per-encoder offset.
  Feb 2026 recordings use this; they store true monotonic µs.
- `4dc0b44` (2026-06-08): removed the offset code with the message
  *"picamera2 timestamps are 64-bit µs since boot (kernel monotonic clock)"*.
  This is factually wrong — the rebase in `_timestamp` was overlooked.
  Michigan recordings use this; they store encoder-relative µs starting at 0.

## Fix

`RawTimestampOutput.outputframe` now records BOTH per frame (16 B/frame,
`<qq`):

- `pi_us` = `time.monotonic_ns()//1000` at outputframe time — global
  `CLOCK_MONOTONIC` anchor, same domain as every other sensor. Has ~few ms
  of callback jitter vs actual SoF.
- `rebased_us` = picamera2's SoF-accurate delta since this encoder's first
  frame (always 0 on frame 0).

Reconstruction: `global_us[i] = pi_us[0] + rebased_us[i]`. Combines
SoF-accurate deltas with a global anchor.

Diagnostic (free): `pi_us[i] - pi_us[0] - rebased_us[i]` is per-frame
callback jitter — measures the delay between actual sensor SoF and the
outputframe callback firing.

## DO NOT re-simplify

If anyone in the future thinks "we can just write `timestamp` verbatim, it's
already CLOCK_MONOTONIC" — no, it's the rebased scalar. Read picamera2's
`_timestamp`. The pi_us column is what anchors to the global clock domain.
Dropping it silently destroys all cross-sensor alignment for future
recordings.
