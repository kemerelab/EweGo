#!/usr/bin/env python3
"""
Play or convert videos with proper timestamps from binary timestamp files.
Supports H.264 (new) and MJPEG (legacy) recordings.
"""

import struct
import sys
import os
import argparse
import subprocess
import threading
import cv2
import numpy as np


TS_MAGIC = b"EWEGOTS2"  # 8 bytes — matches dual_cam_jp2.py::TS_MAGIC


def load_timestamps(timestamp_file):
    """Load timestamps from binary file. Returns a list of µs values in the
    CLOCK_MONOTONIC domain, so directly comparable across cameras and to
    IMU/audio/GPS/fuel gauge timestamps.

    Two on-disk formats, distinguished by an 8-byte magic header:

    - NEW (magic "EWEGOTS2" then <qq per frame): (pi_us, rebased_us) pairs.
      pi_us is time.monotonic_ns()//1000 at outputframe time. rebased_us is
      picamera2's SoF-accurate delta since this encoder's first frame.
      Reconstructed as pi_us[0] + rebased_us[i] to combine SoF-accurate
      deltas with a global CLOCK_MONOTONIC anchor.

    - OLD (no magic, raw <q per frame): single int64 µs. Semantics depend on
      when the recording was made:
        * Pre-4dc0b44 (Feb 2026 batch): kernel CLOCK_MONOTONIC µs. Usable directly.
        * Post-4dc0b44 (Michigan Apr 2026 batch): encoder-relative µs starting
          at 0. Cannot be aligned to other sensors without a recovered t0.
      This loader doesn't distinguish those cases — it just returns the raw
      values and lets callers do the right thing per recording provenance.
    """
    with open(timestamp_file, 'rb') as f:
        header = f.read(len(TS_MAGIC))
        body = f.read() if header == TS_MAGIC else header + f.read()

    if header == TS_MAGIC:
        # NEW format: <qq per frame. Anchor + SoF-accurate rebased delta.
        n = len(body) // 16
        pairs = struct.unpack(f'<{2 * n}q', body[:n * 16])
        pi_vals = list(pairs[0::2])
        rebased = list(pairs[1::2])
        # Strip trailing (0, 0) pairs — shutdown artifact if encoder emits None.
        while pi_vals and pi_vals[-1] == 0 and rebased[-1] == 0:
            pi_vals.pop()
            rebased.pop()
        if not pi_vals:
            return []
        anchor = pi_vals[0]
        return [anchor + r for r in rebased]

    # OLD format: <q per frame.
    n = len(body) // 8
    raw = list(struct.unpack(f'<{n}q', body[:n * 8]))

    # Strip trailing zeros — can occur if the encoder sends a None timestamp
    # on the final frame during shutdown (written as 0 by the recorder).
    while raw and raw[-1] == 0:
        raw.pop()

    return raw


def find_video_file(camera_num, directory='.'):
    """Find the video file for a camera, preferring H.264 over MJPEG.

    Returns (path, format) where format is 'h264' or 'mjpeg'.
    """
    h264_path = os.path.join(directory, f'camera{camera_num}.h264')
    mjpeg_path = os.path.join(directory, f'camera{camera_num}.mjpeg')

    if os.path.exists(h264_path):
        return h264_path, 'h264'
    elif os.path.exists(mjpeg_path):
        return mjpeg_path, 'mjpeg'
    else:
        return None, None


def find_audio_file(directory):
    """Find an audio WAV file in a recording directory.

    Returns the path to the first audio_*.wav found, or None.
    """
    import glob
    matches = sorted(glob.glob(os.path.join(directory, 'audio_*.wav')))
    return matches[0] if matches else None


def play_video(mjpeg_file, timestamps, flip=True):
    """Play video with proper timing based on timestamps."""
    cap = cv2.VideoCapture(mjpeg_file)

    if not cap.isOpened():
        print(f"Error: Cannot open video file {mjpeg_file}")
        return

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"Video: {total_frames} frames")
    print(f"Timestamps: {len(timestamps)} entries")
    print(f"Duration: {(timestamps[-1] - timestamps[0]) / 1e6:.2f} seconds")
    print(f"Press 'q' to quit, SPACE to pause/resume")

    frame_idx = 0
    paused = False
    start_time = cv2.getTickCount()

    while True:
        if not paused:
            ret, frame = cap.read()
            if not ret or frame_idx >= len(timestamps):
                break

            # Rotate 180° if sensors are mounted upside down
            if flip:
                frame = cv2.rotate(frame, cv2.ROTATE_180)

            # Show frame
            cv2.imshow('Video Player', frame)

            # Calculate delay until next frame
            if frame_idx < len(timestamps) - 1:
                current_ts = timestamps[frame_idx]
                next_ts = timestamps[frame_idx + 1]
                delay_us = next_ts - current_ts
                delay_ms = max(1, int(delay_us / 1000))
            else:
                delay_ms = 1

            frame_idx += 1
        else:
            delay_ms = 30  # Faster response when paused

        # Handle keyboard input
        key = cv2.waitKey(delay_ms) & 0xFF
        if key == ord('q'):
            break
        elif key == ord(' '):
            paused = not paused
            print("PAUSED" if paused else "RESUMED")

    cap.release()
    cv2.destroyAllWindows()


def ensure_seekable(video_file, timestamps, fmt='mjpeg'):
    """Remux a raw video to a seekable container using ffmpeg -c copy.

    For H.264: remux to .mp4 (natively seekable with index).
    For MJPEG: remux to .avi (legacy path).

    Caches the result next to the source file so it's only done once.
    Returns (container_path, frame_count) or (None, 0) on failure.
    """
    if fmt == 'h264':
        out_path = video_file.rsplit('.', 1)[0] + '_seekable.mp4'
    else:
        out_path = video_file.rsplit('.', 1)[0] + '_seekable.avi'

    # Use cached file if it exists and is newer than the source
    if os.path.exists(out_path) and os.path.getmtime(out_path) >= os.path.getmtime(video_file):
        cap = cv2.VideoCapture(out_path)
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
        print(f"  Using cached {out_path} ({n} frames)")
        return out_path, n

    n = len(timestamps)
    duration_sec = (timestamps[-1] - timestamps[0]) / 1e6
    fps = n / duration_sec

    print(f"  Remuxing {video_file} -> {out_path} (ffmpeg -c copy, ~instant)...")
    result = subprocess.run(
        ['ffmpeg', '-y', '-r', str(fps), '-i', video_file, '-c', 'copy', out_path],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        print(f"  ffmpeg failed: {result.stderr[-200:]}")
        return None, 0

    cap = cv2.VideoCapture(out_path)
    actual_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    print(f"  Done: {actual_frames} frames")
    return out_path, actual_frames


def build_sync_map(timestamps1, timestamps2):
    """Build a mapping from cam1 frame indices to the closest cam2 frame.

    If timestamps are epoch-aligned (new recorder), this aligns by absolute time.
    If timestamps both start at 0 (old recorder), this still works but won't
    correct for start-time offset or clock drift.

    Returns (sync_map, offset_ms):
      sync_map[i] = cam2 frame index closest in time to cam1 frame i
      offset_ms = initial timestamp offset between cameras
    """
    offset = timestamps2[0] - timestamps1[0]
    offset_ms = offset / 1000

    # For each cam1 frame, find closest cam2 frame by timestamp
    sync_map = []
    j = 0
    for i in range(len(timestamps1)):
        t1 = timestamps1[i]
        # Advance j while the next cam2 frame is closer
        while j < len(timestamps2) - 1 and abs(timestamps2[j + 1] - t1) < abs(timestamps2[j] - t1):
            j += 1
        sync_map.append(j)

    return sync_map, offset_ms


def play_dual_video(video_file1, timestamps1, fmt1,
                     video_file2, timestamps2, fmt2, flip=True, swap=True):
    """Play two videos side-by-side with timestamp-synchronized playback."""

    # Remux to seekable container files (cached, near-instant after first run)
    print(f"Preparing camera 1 ({video_file1}, {fmt1})...")
    avi1, n1 = ensure_seekable(video_file1, timestamps1, fmt1)
    print(f"Preparing camera 2 ({video_file2}, {fmt2})...")
    avi2, n2 = ensure_seekable(video_file2, timestamps2, fmt2)

    if not avi1 or not avi2:
        print("Error: Could not prepare video files")
        return

    # Trim timestamps to actual frame counts from container
    timestamps1 = timestamps1[:n1]
    timestamps2 = timestamps2[:n2]

    # Build sync map: cam1 frame i -> cam2 frame sync_map[i]
    print("Building sync map...")
    sync_map, offset_ms = build_sync_map(timestamps1, timestamps2)
    total_frames = len(timestamps1)

    if abs(offset_ms) > 1:
        print(f"Camera offset: {offset_ms:.1f}ms (epoch-aligned timestamps)")
    else:
        print(f"Camera offset: {offset_ms:.1f}ms (timestamps start together)")

    cap1 = cv2.VideoCapture(avi1)
    cap2 = cv2.VideoCapture(avi2)

    if not cap1.isOpened() or not cap2.isOpened():
        print("Error: Cannot open video files")
        return

    print(f"Camera 1: {n1} frames, duration: {(timestamps1[-1] - timestamps1[0]) / 1e6:.2f}s")
    print(f"Camera 2: {n2} frames, duration: {(timestamps2[-1] - timestamps2[0]) / 1e6:.2f}s")
    print(f"Synced frames: {total_frames}")

    # Read first frame to get dimensions
    ret1, f1 = cap1.read()
    cap2.set(cv2.CAP_PROP_POS_FRAMES, sync_map[0])
    ret2, f2 = cap2.read()
    if not ret1 or not ret2:
        print("Error reading first frame")
        return

    h1, w1 = f1.shape[:2]
    h2, w2 = f2.shape[:2]
    target_height = min(h1, h2)
    s1 = target_height / h1
    s2 = target_height / h2

    # Create window
    window_name = 'Dual Camera Player'
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    combined_width = int(w1 * s1) + int(w2 * s2)
    init_scale = min(1.0, 1600 / combined_width)
    cv2.resizeWindow(window_name, int(combined_width * init_scale), int(target_height * init_scale))

    # Trackbar
    user_seek = [None]
    programmatic = [False]

    def on_trackbar(val):
        if not programmatic[0]:
            user_seek[0] = val

    cv2.createTrackbar('Frame', window_name, 0, total_frames - 1, on_trackbar)

    frame_idx = 0
    cam2_idx = sync_map[0]
    paused = True
    last_frame1 = f1
    last_frame2 = f2

    print("Controls: SPACE=play/pause  q/ESC=quit  LEFT/a=back 10  RIGHT/d=fwd 10")

    def seek_to(target):
        """Seek both captures to a target frame (cam1) and its synced cam2 frame."""
        nonlocal last_frame1, last_frame2, frame_idx, cam2_idx
        target = max(0, min(target, total_frames - 1))
        cam2_target = sync_map[target]
        cap1.set(cv2.CAP_PROP_POS_FRAMES, target)
        cap2.set(cv2.CAP_PROP_POS_FRAMES, cam2_target)
        r1, nf1 = cap1.read()
        r2, nf2 = cap2.read()
        if r1 and r2:
            last_frame1 = nf1
            last_frame2 = nf2
            frame_idx = target
            cam2_idx = cam2_target
            return True
        return False

    def set_trackbar(val):
        programmatic[0] = True
        cv2.setTrackbarPos('Frame', window_name, val)
        programmatic[0] = False

    while True:
        if cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) < 1:
            break

        if user_seek[0] is not None:
            seek_to(user_seek[0])
            user_seek[0] = None
            paused = True

        # ---- Build display ----
        f1 = last_frame1
        f2 = last_frame2

        # Rotate 180° if sensors are mounted upside down
        if flip:
            f1 = cv2.rotate(f1, cv2.ROTATE_180)
            f2 = cv2.rotate(f2, cv2.ROTATE_180)

        fh1, fw1 = f1.shape[:2]
        fh2, fw2 = f2.shape[:2]
        if fh1 != fh2:
            f1 = cv2.resize(f1, (int(fw1 * s1), target_height))
            f2 = cv2.resize(f2, (int(fw2 * s2), target_height))

        combined = np.hstack((f2, f1)) if swap else np.hstack((f1, f2))

        # Overlay — show time relative to start of recording
        t0 = timestamps1[0]
        ts_sec = (timestamps1[frame_idx] - t0) / 1e6
        mins = int(ts_sec // 60)
        secs = ts_sec % 60
        label = f"Time: {mins:02d}:{secs:05.2f} | Frame: {frame_idx}/{total_frames} (cam2:{cam2_idx})"
        if paused:
            label += " [PAUSED]"
        cv2.putText(combined, label, (10, 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2, cv2.LINE_AA)

        cv2.imshow(window_name, combined)

        # ---- Timing ----
        if not paused and frame_idx < total_frames - 1:
            delay_us = timestamps1[frame_idx + 1] - timestamps1[frame_idx]
            delay_ms = max(1, int(delay_us / 1000))
        else:
            delay_ms = 30

        # ---- Keyboard ----
        key = cv2.waitKey(delay_ms) & 0xFF
        if key == ord('q') or key == 27:
            break
        elif key == ord(' '):
            paused = not paused
        elif key == 81 or key == 2 or key == ord('a'):
            seek_to(frame_idx - 10)
            paused = True
            set_trackbar(frame_idx)
        elif key == 83 or key == 3 or key == ord('d'):
            seek_to(frame_idx + 10)
            paused = True
            set_trackbar(frame_idx)

        # ---- Advance when playing ----
        if not paused and frame_idx < total_frames - 1:
            next_cam2 = sync_map[frame_idx + 1]

            r1, nf1 = cap1.read()
            # Only seek cam2 if the target frame isn't the next sequential one
            if next_cam2 != cam2_idx + 1:
                cap2.set(cv2.CAP_PROP_POS_FRAMES, next_cam2)
            r2, nf2 = cap2.read()

            if r1 and r2:
                last_frame1 = nf1
                last_frame2 = nf2
                frame_idx += 1
                cam2_idx = next_cam2
            else:
                paused = True

            if frame_idx % 5 == 0:
                set_trackbar(frame_idx)

    cap1.release()
    cap2.release()
    cv2.destroyAllWindows()


def run_ffmpeg_with_progress(cmd, duration_sec):
    """Run ffmpeg showing a real-time progress bar. Returns (returncode, stderr_text).

    Uses -progress pipe:1 so ffmpeg writes structured progress to stdout.
    Stderr is collected in a background thread to prevent buffer deadlock.
    """
    full_cmd = cmd + ['-progress', 'pipe:1', '-nostats']
    proc = subprocess.Popen(full_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    stderr_buf = []
    def _drain_stderr():
        for line in proc.stderr:
            stderr_buf.append(line)
    t = threading.Thread(target=_drain_stderr, daemon=True)
    t.start()

    bar_width = 40
    out_time_us = 0
    for line in proc.stdout:
        line = line.strip()
        if line.startswith('out_time_us='):
            try:
                out_time_us = int(line.split('=', 1)[1])
            except ValueError:
                pass
        elif line.startswith('progress='):
            pct = min(100.0, out_time_us / (duration_sec * 1e6) * 100) if duration_sec > 0 else 0
            elapsed = out_time_us / 1e6
            filled = int(bar_width * pct / 100)
            bar = '#' * filled + '-' * (bar_width - filled)
            sys.stdout.write(f'\r  [{bar}] {pct:5.1f}%  {elapsed:.0f}s / {duration_sec:.0f}s  ')
            sys.stdout.flush()

    proc.wait()
    t.join()
    sys.stdout.write('\n')
    return proc.returncode, ''.join(stderr_buf)


def export_dual_video(video_file1, timestamps1, fmt1,
                      video_file2, timestamps2, fmt2,
                      output_file, flip=True, swap=True, audio_file=None):
    """Export dual side-by-side video to MP4 with timestamp overlay and optional audio.

    Uses a single ffmpeg filter_complex command — no Python frame loop.
    ffmpeg handles decode, flip, stack, overlay, audio mux, and encode natively.
    """

    print(f"Preparing camera 1 ({video_file1}, {fmt1})...")
    avi1, n1 = ensure_seekable(video_file1, timestamps1, fmt1)
    print(f"Preparing camera 2 ({video_file2}, {fmt2})...")
    avi2, n2 = ensure_seekable(video_file2, timestamps2, fmt2)

    if not avi1 or not avi2:
        print("Error: Could not prepare video files")
        return

    timestamps1 = timestamps1[:n1]
    timestamps2 = timestamps2[:n2]

    # Compute camera time offset (cam2 relative to cam1)
    offset_sec = (timestamps2[0] - timestamps1[0]) / 1e6
    duration_sec = (timestamps1[-1] - timestamps1[0]) / 1e6
    total_frames = len(timestamps1)
    avg_fps = total_frames / duration_sec

    print(f"Exporting to {output_file}")
    print(f"  Duration: {duration_sec:.1f}s @ {avg_fps:.1f}fps")
    print(f"  Camera offset: {offset_sec*1000:.1f}ms")
    if audio_file:
        print(f"  Audio: {audio_file}")

    # Build filter: flip both cameras (180°), then stack side-by-side, then overlay time
    # vflip+hflip = 180° rotation without interpolation (fast)
    if flip:
        f1_filter = "[0:v]vflip,hflip[v1]"
        f2_filter = "[1:v]vflip,hflip[v2]"
    else:
        f1_filter = "[0:v]null[v1]"
        f2_filter = "[1:v]null[v2]"

    # swap: cam2 on left, cam1 on right (default)
    if swap:
        stack = "[v2][v1]hstack=inputs=2[vid]"
    else:
        stack = "[v1][v2]hstack=inputs=2[vid]"

    # drawtext with PTS-based timestamp (matches actual video time)
    drawtext = (
        "[vid]drawtext=text='Time\\: %{pts\\:hms}'"
        ":x=10:y=10:fontsize=24:fontcolor=green"
        ":borderw=1:bordercolor=black[out]"
    )

    filter_complex = f"{f1_filter};{f2_filter};{stack};{drawtext}"

    # Build ffmpeg command
    ffmpeg_cmd = ['ffmpeg', '-y']

    # Input 0: camera 1
    ffmpeg_cmd += ['-i', avi1]

    # Input 1: camera 2, offset to align with camera 1
    if abs(offset_sec) > 0.001:
        ffmpeg_cmd += ['-itsoffset', f'{offset_sec:.6f}']
    ffmpeg_cmd += ['-i', avi2]

    # Input 2: audio (if present)
    if audio_file:
        ffmpeg_cmd += ['-i', audio_file]

    ffmpeg_cmd += ['-filter_complex', filter_complex]
    ffmpeg_cmd += ['-map', '[out]']

    if audio_file:
        ffmpeg_cmd += ['-map', '2:a']

    ffmpeg_cmd += [
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '20',
        '-pix_fmt', 'yuv420p',
    ]
    if audio_file:
        ffmpeg_cmd += ['-c:a', 'aac', '-b:a', '192k', '-shortest']
    ffmpeg_cmd.append(output_file)

    print(f"  Running ffmpeg...")
    returncode, stderr = run_ffmpeg_with_progress(ffmpeg_cmd, duration_sec)

    if returncode != 0:
        print(f"  ffmpeg failed: {stderr[-500:]}")
        return

    # Report output file size
    size_mb = os.path.getsize(output_file) / (1024 * 1024)
    print(f"Export complete: {output_file} ({size_mb:.1f} MB)")


def convert_to_mp4(mjpeg_file, timestamps, output_file):
    """Convert MJPEG to MP4 with proper timing."""
    cap = cv2.VideoCapture(mjpeg_file)

    if not cap.isOpened():
        print(f"Error: Cannot open video file {mjpeg_file}")
        return

    # Get video properties
    frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = len(timestamps)

    # Calculate average FPS for output
    duration_sec = (timestamps[-1] - timestamps[0]) / 1e6
    avg_fps = total_frames / duration_sec

    print(f"Converting {mjpeg_file} -> {output_file}")
    print(f"  Resolution: {frame_width}x{frame_height}")
    print(f"  Frames: {total_frames}")
    print(f"  Duration: {duration_sec:.2f} seconds")
    print(f"  Average FPS: {avg_fps:.2f}")

    # Create video writer
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(output_file, fourcc, avg_fps, (frame_width, frame_height))

    frame_idx = 0
    while frame_idx < total_frames:
        ret, frame = cap.read()
        if not ret:
            break

        out.write(frame)
        frame_idx += 1

        if frame_idx % 100 == 0:
            print(f"  Progress: {frame_idx}/{total_frames} frames ({100*frame_idx/total_frames:.1f}%)")

    cap.release()
    out.release()
    print(f"Conversion complete: {output_file}")


def main():
    parser = argparse.ArgumentParser(description='Play or convert videos with timestamps (H.264 or MJPEG)')
    parser.add_argument('camera', type=str, nargs='?', help='Camera number (1 or 2), or omit for dual view')
    parser.add_argument('--dir', type=str, help='Recording directory (contains camera/ subdir and audio WAV)')
    parser.add_argument('--dual', action='store_true', help='Play both cameras side-by-side (default if no camera specified)')
    parser.add_argument('--convert', action='store_true', help='Convert to MP4 instead of playing')
    parser.add_argument('--output', type=str, help='Output MP4 file (for conversion)')
    parser.add_argument('--no-flip', action='store_true', help='Disable 180° rotation (sensors are mounted upside down by default)')
    parser.add_argument('--no-swap', action='store_true', help='Disable left/right camera swap (cameras are swapped by default)')
    parser.add_argument('--no-audio', action='store_true', help='Skip audio muxing in export')
    parser.add_argument('--export', nargs='?', const=True, metavar='FILE',
                        help='Export dual side-by-side video to MP4 (default: dual_export.mp4 in recording dir)')

    args = parser.parse_args()

    # Resolve directories
    if args.dir:
        rec_dir = os.path.abspath(args.dir)
        cam_dir = os.path.join(rec_dir, 'camera')
        if not os.path.isdir(cam_dir):
            print(f"Error: No camera/ subdirectory in {rec_dir}")
            sys.exit(1)
    else:
        rec_dir = None
        cam_dir = '.'

    flip = not args.no_flip
    swap = not args.no_swap

    # Determine if dual mode
    dual_mode = args.dual or args.camera is None

    if dual_mode and not args.convert:
        # Play both cameras synchronized — auto-detect format
        video1, fmt1 = find_video_file(1, cam_dir)
        video2, fmt2 = find_video_file(2, cam_dir)
        timestamp_file1 = os.path.join(cam_dir, 'camera1_timestamps.bin')
        timestamp_file2 = os.path.join(cam_dir, 'camera2_timestamps.bin')

        for label, path in [('Camera 1 video', video1), ('Camera 2 video', video2),
                            ('Camera 1 timestamps', timestamp_file1),
                            ('Camera 2 timestamps', timestamp_file2)]:
            if path is None or not os.path.exists(path):
                print(f"Error: {label} not found")
                sys.exit(1)

        print(f"Detected formats: cam1={fmt1}, cam2={fmt2}")
        print(f"Loading timestamps...")
        timestamps1 = load_timestamps(timestamp_file1)
        timestamps2 = load_timestamps(timestamp_file2)

        if args.export:
            # Determine output path
            if args.export is True:
                # Default filename, put in recording dir or cwd
                out_dir = rec_dir or '.'
                output_file = os.path.join(out_dir, 'dual_export.mp4')
            else:
                output_file = args.export

            # Find audio file
            audio_file = None
            if not args.no_audio and rec_dir:
                audio_file = find_audio_file(rec_dir)
                if audio_file:
                    print(f"Found audio: {audio_file}")
                else:
                    print("No audio file found, exporting video only")

            export_dual_video(video1, timestamps1, fmt1, video2, timestamps2, fmt2,
                              output_file=output_file, flip=flip, swap=swap,
                              audio_file=audio_file)
        else:
            play_dual_video(video1, timestamps1, fmt1, video2, timestamps2, fmt2,
                            flip=flip, swap=swap)

    else:
        # Single camera mode
        if args.camera is None:
            print("Error: Please specify a camera number (1 or 2) for single camera mode")
            sys.exit(1)

        # Auto-detect video format
        video_file, fmt = find_video_file(args.camera, cam_dir)
        timestamp_file = os.path.join(cam_dir, f'camera{args.camera}_timestamps.bin')

        if video_file is None:
            print(f"Error: No video file found for camera {args.camera} (.h264 or .mjpeg)")
            sys.exit(1)

        if not os.path.exists(timestamp_file):
            print(f"Error: {timestamp_file} not found")
            sys.exit(1)

        # Load timestamps
        print(f"Detected format: {fmt}")
        print(f"Loading timestamps from {timestamp_file}...")
        timestamps = load_timestamps(timestamp_file)

        if args.convert:
            output_file = args.output or f'camera{args.camera}_timestamped.mp4'
            convert_to_mp4(video_file, timestamps, output_file)
        else:
            play_video(video_file, timestamps, flip=flip)


if __name__ == '__main__':
    main()
