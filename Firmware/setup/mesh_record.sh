#!/usr/bin/env bash
# ============================================================================
# Start / stop record_sensors.py on every peer on the EweGo mesh
# ============================================================================
# Standalone recording controller — same launch/kill logic as mesh_monitor.py's
# [r]/[s] keys, but as a plain shell script so it works while mesh_monitor is
# out of commission.
#
# Usage:
#   bash mesh_record.sh start           Launch record_sensors.py --no-gps on all peers
#   bash mesh_record.sh stop            SIGINT record_sensors.py + sweep orphans
#   bash mesh_record.sh status          Show which peers have it running
#
# Env vars:
#   SSH_USER   SSH login on the Pis          (default: user)
#   TIMEOUT    per-peer SSH timeout, seconds (default: 12)
#   PEERS      space-separated IPs           (default: auto-discover on bat0)
# ============================================================================

set -uo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
DIM='\033[2m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*" >&2; }

SSH_USER="${SSH_USER:-user}"
TIMEOUT="${TIMEOUT:-25}"

# ControlPath matches mesh_monitor.py — if the monitor is running, start/stop
# rides its already-established mux masters (~1 RTT, no key exchange).
SSH_OPTS=(
    -o ConnectTimeout=10
    -o BatchMode=yes
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o LogLevel=ERROR
    -o ServerAliveInterval=15
    -o GSSAPIAuthentication=no
    -o PreferredAuthentications=publickey
    -o ControlMaster=auto
    -o ControlPath=/tmp/sshmux-ewego-%r@%h:%p
    -o ControlPersist=yes
)

ACTION="${1:-}"
if [ -z "$ACTION" ] || [[ "$ACTION" != "start" && "$ACTION" != "stop" && "$ACTION" != "status" ]]; then
    echo "usage: $0 {start|stop|status}" >&2
    exit 1
fi

# --- discover peers ---------------------------------------------------------
if [ -n "${PEERS:-}" ]; then
    read -r -a PEER_LIST <<< "$PEERS"
else
    if ! ip link show bat0 &>/dev/null; then
        error "bat0 not found — join the mesh first (bash mesh_join.sh join <N>)"
        exit 1
    fi
    info "discovering peers on bat0..."
    for i in {1..254}; do
        ( ping -c 1 -W 1 -I bat0 "10.42.0.${i}" >/dev/null 2>&1 ) &
    done
    wait
    MY_IP=$(ip -4 -o addr show dev bat0 | awk '{split($4,a,"/"); print a[1]; exit}')
    mapfile -t PEER_LIST < <(
        ip -4 neigh show dev bat0 \
        | awk '/lladdr/ && !/FAILED|INCOMPLETE/ {print $1}' \
        | grep -v "^${MY_IP:-none}$" \
        | sort -t. -k4 -n
    )
fi

if [ "${#PEER_LIST[@]}" -eq 0 ]; then
    error "no peers found"
    exit 1
fi
info "peers: ${PEER_LIST[*]}"

# --- commands (mirror mesh_monitor.py _START_REC_CMD / _STOP_REC_CMD) -------
# The (setsid ... < /dev/null > log 2>&1 &) pattern severs stdin/stdout from
# ssh's remote pty so ssh returns immediately. Without the subshell + setsid,
# bash keeps the pty referenced and ssh hangs until the recording ends.
START_CMD='
cd ~/EweGo && \
rm -f /tmp/record_sensors.log && \
(setsid ~/.local/bin/uv run python Firmware/record_sensors.py --no-gps < /dev/null > /tmp/record_sensors.log 2>&1 &) && \
disown -a 2>/dev/null; \
sleep 2; \
pgrep -f "python.*record_sensors.py" >/dev/null && echo ok
'

# SIGINT the parent, then broadly SIGKILL any orphaned recorder children.
# Orphans happen when a prior session died hard and its subprocesses got
# reparented to init — they still hold /dev/snd, /dev/video*, /dev/ttyAMA5.
STOP_CMD='
pkill -SIGINT -f "python.*record_sensors.py" 2>/dev/null; \
sleep 3; \
for pat in "python.*record_sensors.py" "record_audio.py" "dual_cam_jp2" "log_imu_data" "max17048_test"; do \
  pkill -9 -f "$pat" 2>/dev/null; \
done; \
sudo -n fuser -k /dev/snd/pcmC0D0c /dev/video0 /dev/video1 /dev/ttyAMA5 2>/dev/null; \
sleep 1; \
echo ok
'

STATUS_CMD='
if pgrep -f "python.*record_sensors.py" >/dev/null; then
  d=""
  for pid in $(pgrep -f "python.*record_sensors.py"); do
    d=$(ls -l /proc/$pid/fd 2>/dev/null | grep -oE "/home/[^ ]*/sensor_test_[0-9_]+" | head -1)
    [ -n "$d" ] && break
  done
  echo "RECORDING $(basename "${d:-?}")"
else
  echo "idle"
fi
'

case "$ACTION" in
    start)  REMOTE_CMD="$START_CMD"  ;;
    stop)   REMOTE_CMD="$STOP_CMD"   ;;
    status) REMOTE_CMD="$STATUS_CMD" ;;
esac

# --- fan out in parallel ----------------------------------------------------
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

info "${ACTION}ing on ${#PEER_LIST[@]} peer(s) (timeout=${TIMEOUT}s)..."
for ip in "${PEER_LIST[@]}"; do
    (
        out=$(timeout "$TIMEOUT" ssh "${SSH_OPTS[@]}" \
              -o BatchMode=yes "${SSH_USER}@${ip}" \
              "$REMOTE_CMD" 2>&1 </dev/null)
        rc=$?
        echo "$rc" > "$TMPDIR/$ip.rc"
        echo "$out" > "$TMPDIR/$ip.out"
    ) &
done
wait

# --- report -----------------------------------------------------------------
echo
OK_COUNT=0
for ip in "${PEER_LIST[@]}"; do
    rc=$(cat "$TMPDIR/$ip.rc" 2>/dev/null || echo 255)
    out=$(cat "$TMPDIR/$ip.out" 2>/dev/null | tail -1)
    if [ "$rc" -eq 0 ] && [[ "$out" == *ok* || "$out" == RECORDING* || "$out" == idle ]]; then
        printf "  ${GREEN}✓${NC} %-15s %s\n" "$ip" "$out"
        OK_COUNT=$((OK_COUNT + 1))
    else
        printf "  ${RED}✗${NC} %-15s ${DIM}rc=%s${NC} %s\n" "$ip" "$rc" "${out:-<no output>}"
    fi
done
echo
info "${OK_COUNT}/${#PEER_LIST[@]} peer(s) ok"
[ "$OK_COUNT" -eq "${#PEER_LIST[@]}" ]
