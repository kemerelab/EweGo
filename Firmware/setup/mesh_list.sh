#!/usr/bin/env bash
# ============================================================================
# List all devices reachable on the EweGo mesh
# ============================================================================
# Discovers peers by ping-sweeping the bat0 subnet, then prints a table of
# every responder with IP, MAC (from ARP), and hostname (best-effort).
# Cross-references batman-adv's originator count so you can tell at a glance
# whether the mesh has converged.
#
# Usage:
#   bash mesh_list.sh             Default: sweep + print table
#   bash mesh_list.sh --quiet     IPs only (one per line), suitable for scripting
# ============================================================================

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*" >&2; }

MODE="${1:-table}"

if ! ip link show bat0 &>/dev/null; then
    error "bat0 not found — not on the mesh."
    echo "  Bring up the mesh first: bash mesh_join.sh join <suffix>"
    exit 1
fi

MY_IP=$(ip -4 -o addr show dev bat0 2>/dev/null | awk '{split($4,a,"/"); print a[1]; exit}')
if [ -z "${MY_IP:-}" ]; then
    error "bat0 has no IPv4 address — mesh not fully up."
    exit 1
fi
SUBNET="${MY_IP%.*}"

if [ "$MODE" != "--quiet" ]; then
    # Mesh-level peer count (best-effort; needs sudo for batctl, swallow on failure)
    ORIG_OUT=$(sudo -n batctl meshif bat0 o 2>/dev/null || true)
    if [ -n "$ORIG_OUT" ]; then
        ORIG_COUNT=$(echo "$ORIG_OUT" | awk '/^ \*/' | wc -l)
        info "batman-adv originators: ${ORIG_COUNT}  (this device: ${MY_IP})"
    else
        info "batman-adv originator count unavailable (need sudo)  (this device: ${MY_IP})"
    fi
    info "Sweeping ${SUBNET}.0/24 over bat0..."
fi

# Parallel ping sweep — populates the ARP table for everything that responds.
# ICMP doesn't need sudo. -W 1 = 1s timeout, -I bat0 = source from mesh iface.
for i in {1..254}; do
    ( ping -c 1 -W 1 -I bat0 "${SUBNET}.${i}" >/dev/null 2>&1 ) &
done
wait

resolve_host() {
    local ip="$1" h=""
    h=$(getent hosts "$ip" 2>/dev/null | awk '{print $2; exit}') || true
    if [ -z "$h" ] && command -v avahi-resolve-address &>/dev/null; then
        h=$(avahi-resolve-address "$ip" 2>/dev/null | awk '{print $2}') || true
    fi
    # Final fallback: infer eweN from the last octet (project convention)
    if [ -z "$h" ]; then
        h="ewe$(echo "$ip" | awk -F. '{print $4}')?"
    fi
    echo "$h"
}

# Build a sorted list of {ip, mac} from ARP plus self
SELF_MAC=$(ip -o link show bat0 | awk '{for (i=1;i<=NF;i++) if ($i=="link/ether") print $(i+1)}')
PEERS=$(
    {
        echo "$MY_IP $SELF_MAC self"
        ip -4 neigh show dev bat0 | awk '/^[0-9]/{
            mac=""
            for (i=1;i<=NF;i++) if ($i ~ /^([0-9a-f]{2}:){5}[0-9a-f]{2}$/) mac=$i
            if (mac != "") print $1, mac, "peer"
        }'
    } | sort -u -t. -k4 -n
)

if [ "$MODE" = "--quiet" ]; then
    echo "$PEERS" | awk '{print $1}'
    exit 0
fi

echo ""
printf "%-15s  %-18s  %-18s  %s\n" "IP" "MAC" "Hostname" "Note"
printf -- '-%.0s' {1..70}; echo
echo "$PEERS" | while read -r ip mac kind; do
    [ -z "$ip" ] && continue
    if [ "$kind" = "self" ]; then
        note="(this device)"
        host=$(hostname)
    else
        note=""
        host=$(resolve_host "$ip")
    fi
    printf "%-15s  %-18s  %-18s  %s\n" "$ip" "$mac" "$host" "$note"
done
