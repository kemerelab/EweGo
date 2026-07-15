#!/usr/bin/env bash
# Low-latency dual-camera preview over the BATMAN mesh.
#
# Transport: raw H.264 over UDP, both directions independent.
#   Pi:      rpicam-vid → python3 UDP forwarder → laptop:PORT
#   Laptop:  ffplay udp://@0.0.0.0:PORT with format pre-declared (-f h264)
#
# Why not SSH-tunneled TCP:
#   - TCP retransmit head-of-line blocking stalls the whole stream for
#     hundreds of ms on any mesh packet loss.
#   - SSH channels also buffer ~64 KB, adding baseline delay.
# Why not ffmpeg on the Pi:
#   - Not installed by default and rpicam-apps here lacks libav support.
#   - Python 3 is on every Pi image; a 5-line inline script is enough to
#     forward stdin bytes to UDP.
#
# Why raw H.264 (no MPEG-TS container):
#   - Zero mux/demux overhead.
#   - Packet loss only glitches the affected macroblocks; the H.264 parser
#     resyncs at the next start code. `--intra $FPS` bounds recovery to 1 s.

set -euo pipefail

# ---- knobs -----------------------------------------------------------------
FPS=15
PORT0=5000
PORT1=5001
MESH_IFACE=bat0

# ---- quality presets -------------------------------------------------------
apply_quality() {
    case "$1" in
        low)    WIDTH=640;  HEIGHT=480;  BITRATE=500000  ;;
        med)    WIDTH=1280; HEIGHT=720;  BITRATE=2000000 ;;
        high)   WIDTH=1920; HEIGHT=1080; BITRATE=4000000 ;;
        *) echo "Unknown quality: $1 (want low|med|high)" >&2; exit 1 ;;
    esac
    INTRA=$FPS   # 1-second I-frame interval; bounds glitch recovery time
}

# ---- args ------------------------------------------------------------------
# Usage: ./stream_ssh.sh [user@host] [low|med|high]
PI_TARGET="${1:-}"
QUALITY="${2:-}"

LAPTOP_IP=$(ip -4 -o addr show "$MESH_IFACE" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1 || true)
if [[ -z "$LAPTOP_IP" ]]; then
    read -rp "No IP on $MESH_IFACE. Enter laptop mesh IP (e.g. 10.42.0.100): " LAPTOP_IP
fi

# ---- Pi discovery ----------------------------------------------------------
discover_pis() {
    # Ping-sweep primes the ARP table. Peers may drop ICMP echo replies but
    # they still answer ARP, so the neighbor table is authoritative — same
    # technique as Firmware/setup/mesh_list.sh. -I bat0 forces the packets
    # out the mesh interface.
    for i in $(seq 1 99); do
        (ping -c1 -W1 -I "$MESH_IFACE" "10.42.0.$i" >/dev/null 2>&1) &
    done
    wait
    # Only lines with a resolved MAC address count as real peers; FAILED and
    # INCOMPLETE entries linger with no MAC.
    ip -4 neigh show dev "$MESH_IFACE" 2>/dev/null | \
        awk '/^10\.42\.0\./ {
            for (i=1; i<=NF; i++)
                if ($i ~ /^([0-9a-f]{2}:){5}[0-9a-f]{2}$/) {
                    split($1, a, "."); print a[4]; next
                }
        }' | sort -n -u
}

if [[ -z "$PI_TARGET" ]]; then
    echo "Scanning mesh for Pis on 10.42.0.1-99..."
    mapfile -t FOUND < <(discover_pis | sort -n)

    if [[ ${#FOUND[@]} -eq 0 ]]; then
        echo "No Pis reachable on $MESH_IFACE."
        read -rp "Enter Pi user@host manually [user@ewe1.local]: " PI_TARGET
        PI_TARGET="${PI_TARGET:-user@ewe1.local}"
    elif [[ ${#FOUND[@]} -eq 1 ]]; then
        N="${FOUND[0]}"
        PI_TARGET="user@10.42.0.$N"
        echo "Found only 10.42.0.$N — using it."
    else
        echo "Found ${#FOUND[@]} Pis:"
        for idx in "${!FOUND[@]}"; do
            N="${FOUND[$idx]}"
            printf "  %d) 10.42.0.%s\n" "$((idx + 1))" "$N"
        done
        read -rp "Select [1]: " SEL
        SEL="${SEL:-1}"
        if ! [[ "$SEL" =~ ^[0-9]+$ ]] || (( SEL < 1 || SEL > ${#FOUND[@]} )); then
            echo "Invalid selection." >&2
            exit 1
        fi
        N="${FOUND[$((SEL - 1))]}"
        PI_TARGET="user@10.42.0.$N"
    fi
fi
echo "Laptop $MESH_IFACE IP: $LAPTOP_IP  →  Pi: $PI_TARGET"

# ---- quality selection -----------------------------------------------------
if [[ -z "$QUALITY" ]]; then
    echo "Quality:"
    echo "  1) low   640x480   500 kbps  (fastest start, chunky)"
    echo "  2) med   1280x720  2 Mbps    (default)"
    echo "  3) high  1920x1080 4 Mbps    (sharpest, most bandwidth)"
    read -rp "Select [2]: " QSEL
    QSEL="${QSEL:-2}"
    case "$QSEL" in
        1|low)  QUALITY=low  ;;
        2|med)  QUALITY=med  ;;
        3|high) QUALITY=high ;;
        *) echo "Invalid quality selection." >&2; exit 1 ;;
    esac
fi
apply_quality "$QUALITY"
echo "Quality: $QUALITY (${WIDTH}x${HEIGHT} @ ${FPS}fps, $((BITRATE / 1000)) kbps)"

# ---- preflight: firewall + port availability --------------------------------
# ufw active without a rule for $MESH_IFACE will silently drop the inbound UDP
# stream and leave ffplay stuck at "vq=0KB" (bit us on CachyOS 2026-07-09).
if systemctl is-active --quiet ufw 2>/dev/null; then
    if ! sudo -n ufw status verbose 2>/dev/null | grep -q "$MESH_IFACE"; then
        echo "  ! ufw is active with no rule matching $MESH_IFACE."
        echo "    Inbound UDP on $PORT0/$PORT1 will be dropped."
        echo "    Fix once:  sudo ufw allow in on $MESH_IFACE"
    fi
fi

# Kill anything already bound to the receive ports. Prior aborted runs can
# leave ffplay/ffmpeg holding the socket → "Address already in use". `fuser`
# is more portable than lsof and doesn't need sudo for our own processes.
for p in "$PORT0" "$PORT1"; do
    fuser -k -n udp "$p" 2>/dev/null || true
done
sleep 0.2

# ---- cleanup ---------------------------------------------------------------
# Guarded so it runs once. Local ssh children get SIGKILL: with SIGTERM they
# stay open waiting for the remote pipe to close, which needs Pi processes to
# die first — through `ssh -T` on disconnect they don't get signaled, so the
# whole shutdown blocks for 5+ seconds. Remote pkill fires and forgets via
# setsid so we don't wait on it either.
CLEANED=0
cleanup() {
    [[ $CLEANED -eq 1 ]] && return
    CLEANED=1
    echo "Cleaning up..."
    local pids
    pids=$(jobs -p)
    [[ -n "$pids" ]] && kill -9 $pids 2>/dev/null || true
    # Detached remote reaper: won't block cleanup if the Pi is slow or
    # unreachable. `setsid` + redirection = fully backgrounded.
    setsid ssh -o BatchMode=yes -o ConnectTimeout=3 "$PI_TARGET" \
        'pkill -9 -f rpicam-vid || true; pkill -9 -f "python3.*SOCK_DGRAM" || true' \
        </dev/null >/dev/null 2>&1 &
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# ---- start receivers first, so packets aren't lost during ramp-up ---------
# Format pre-declared (-f h264) → probesize can be tiny.
# `-r $FPS` gives ffplay an initial rate hint (avoids the "not enough frames
# to estimate rate" warning).
FFPLAY_OPTS=(
    -hide_banner -loglevel warning
    # Playback timing: `-sync ext` needs an external clock (audio, wall). With
    # no audio and no wall-clock timestamps in the stream, framedrop can't tell
    # it's behind → queue grows. `-sync video` sets the video clock as master,
    # combined with -framedrop this drops frames whenever decode falls behind
    # capture.
    -sync video -framedrop
    -fflags nobuffer+discardcorrupt+flush_packets
    -flags low_delay -flags2 fast
    -avioflags direct
    -max_delay 0
    -probesize 32k -analyzeduration 100000
    -f h264
)

# Smaller kernel UDP receive buffer = older packets get dropped when we can't
# keep up. Was 1 MB (~5 s of stream) → 64 KB (~250 ms at 2 Mbps).
UDP_ARGS="fifo_size=65536&overrun_nonfatal=1&reuse=1"

echo "Starting ffplay receivers on udp://:$PORT0 and udp://:$PORT1..."
ffplay "${FFPLAY_OPTS[@]}" -window_title 'Camera 0' \
    -i "udp://@0.0.0.0:$PORT0?$UDP_ARGS" &
ffplay "${FFPLAY_OPTS[@]}" -window_title 'Camera 1' \
    -i "udp://@0.0.0.0:$PORT1?$UDP_ARGS" &

sleep 0.3

# ---- launch pipeline on Pi -------------------------------------------------
# Inline Python UDP forwarder — reads raw H.264 from stdin, sends chunks to
# the laptop. Chunk size 1400 keeps each UDP payload under typical Ethernet
# MTU (bat0 default 1500) so no IP fragmentation.
#
# Heredoc variables are expanded on the LAPTOP side before being sent — the
# Pi receives literal values.
echo "Launching remote pipelines on $PI_TARGET (cameras 0/1 → udp $PORT0/$PORT1)..."

# One ssh session per camera — matches the structure of the original working
# script. Each ssh runs one rpicam-vid → python3 UDP forwarder pipeline.
# The python forwarder writes progress to stderr so we can see whether bytes
# actually flow from encoder → socket. rpicam-vid's own stderr also comes
# back so libcamera errors are visible.
launch_remote() {
    local cam=$1 port=$2
    ssh -T -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
        "$PI_TARGET" bash -s <<EOF
rpicam-vid -t 0 -n --camera $cam --inline --flush \
    --width $WIDTH --height $HEIGHT --framerate $FPS \
    --bitrate $BITRATE --intra $INTRA --profile baseline \
    --codec h264 -o - | \
python3 -u -c "
import sys, socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
target = ('$LAPTOP_IP', $port)
sys.stderr.write('[cam$cam→:$port] forwarder up, target=' + repr(target) + '\n')
sys.stderr.flush()
buf = sys.stdin.buffer
total, last = 0, 0
while True:
    chunk = buf.read(1400)
    if not chunk:
        sys.stderr.write(f'[cam$cam→:$port] EOF after {total} bytes\n')
        break
    s.sendto(chunk, target)
    total += len(chunk)
    if total - last > 500000:
        sys.stderr.write(f'[cam$cam→:$port] sent {total} bytes\n')
        sys.stderr.flush()
        last = total
"
EOF
}

# Two ssh sessions, one per camera. Backgrounded — script's final `wait`
# blocks until both exit (or Ctrl-C).
launch_remote 0 "$PORT0" &
launch_remote 1 "$PORT1" &

wait
echo "Streams ended."
