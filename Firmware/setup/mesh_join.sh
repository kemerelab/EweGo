#!/usr/bin/env bash
# ============================================================================
# Join or leave the EweGo mesh network from a laptop
# ============================================================================
# Uses B.A.T.M.A.N. Advanced (batman-adv) over IBSS (ad-hoc) mode.
# Requires: batctl, iw, ip (batctl installed automatically if missing)
#
# Usage:
#   bash mesh_join.sh scan               Scan for ad-hoc/IBSS networks (read-only)
#   bash mesh_join.sh join [ip-suffix]   Join as 10.42.0.<suffix> (default: 100)
#   bash mesh_join.sh leave              Disconnect and restore normal WiFi
#   bash mesh_join.sh status             Show mesh neighbors and connectivity
#
# Examples:
#   bash mesh_join.sh scan               → list nearby ad-hoc cells without joining
#   bash mesh_join.sh join 100           → joins as 10.42.0.100
#   bash mesh_join.sh join               → same (100 is default)
#   bash mesh_join.sh leave
# ============================================================================

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*"; }

ACTION="${1:-join}"
SUFFIX="${2:-100}"
MESH_IP="10.42.0.${SUFFIX}"

# Auto-detect WiFi interface (wlan0 on Pi, wlp* on most laptops)
IFACE="${EWEGO_IFACE:-}"
if [ -z "$IFACE" ]; then
    IFACE=$(iw dev 2>/dev/null | awk '/Interface/{print $2; exit}')
fi
if [ -z "$IFACE" ]; then
    error "No WiFi interface found. Set EWEGO_IFACE=<name> to override."
    exit 1
fi
info "Using WiFi interface: $IFACE"

install_batctl() {
    if command -v apt &>/dev/null; then
        sudo apt update -qq && sudo apt install -y batctl
    elif command -v pacman &>/dev/null; then
        sudo pacman -S --noconfirm batctl
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y batctl
    else
        error "Can't auto-install batctl — install it manually"
        exit 1
    fi
}

case "$ACTION" in
    scan)
        # Read-only IBSS discovery — does NOT change interface mode, NM state,
        # or your current WiFi connection. Safe to run while connected.
        if ! command -v iw &>/dev/null; then
            error "iw is not installed (sudo apt install iw / sudo pacman -S iw)"
            exit 1
        fi

        info "Scanning $IFACE for ad-hoc (IBSS) cells (read-only)..."
        if ! ip link show "$IFACE" 2>/dev/null | grep -q "state UP"; then
            warn "$IFACE is not UP — results may be empty"
        fi

        scan_output=$(sudo iw dev "$IFACE" scan 2>&1) || {
            error "Scan failed:"
            echo "$scan_output"
            exit 1
        }

        echo ""
        echo "$scan_output" | awk -v target="ewego-mesh" '
            /^BSS / {
                current = $2
                sub(/\(.*/, "", current)
                is_ibss[current] = 0
                next
            }
            /freq:/ && current { freq[current] = $2; next }
            /signal:/ && current { signal[current] = $2; next }
            /SSID:/ && current {
                line = $0
                sub(/^[ \t]*SSID:[ \t]?/, "", line)
                ssid[current] = (line == "" ? "<hidden>" : line)
                next
            }
            /capability:.*IBSS/ && current { is_ibss[current] = 1; next }
            END {
                found = 0
                for (b in is_ibss) {
                    if (is_ibss[b]) {
                        found++
                        marker = (ssid[b] == target) ? "[EweGo]" : "       "
                        printf "  %s  %-20s  freq=%sMHz  bssid=%s  signal=%sdBm\n", \
                            marker, ssid[b], freq[b], b, signal[b]
                    }
                }
                if (!found) {
                    print "  (no ad-hoc cells detected)"
                    print ""
                    print "  Note: many WiFi drivers (esp. Intel iwlwifi) hide IBSS cells"
                    print "  from managed-mode scans. If you expect a mesh to be up nearby,"
                    print "  try joining directly: bash mesh_join.sh join"
                }
            }
        '
        echo ""
        info "Your WiFi connection was not touched. To join: bash $0 join [ip-suffix]"
        ;;

    join)
        # Install batctl if missing
        if ! command -v batctl &>/dev/null; then
            info "Installing batctl..."
            install_batctl
        fi

        # Load batman-adv module
        info "Loading batman-adv kernel module..."
        sudo modprobe batman-adv

        # Release interface from NetworkManager before changing mode
        if command -v nmcli &>/dev/null; then
            sudo nmcli device disconnect "$IFACE" 2>/dev/null || true
            sudo nmcli device set "$IFACE" managed no 2>/dev/null || true
            sleep 1
        fi

        # Take down and switch to IBSS mode
        info "Setting $IFACE to ad-hoc (IBSS) mode..."
        sudo ip link set "$IFACE" down
        sudo iw dev "$IFACE" set type ibss
        sudo ip link set "$IFACE" up

        # Join the IBSS cell (2437 MHz = channel 6, 2.4 GHz). NOHT is required —
        # the Pi's BCM43455 (and many other drivers) reject HT20 in IBSS mode.
        # We intentionally do NOT pass `fixed-freq <BSSID>`: on mt76x2u (and some
        # other USB chipsets), specifying a fixed BSSID causes the adapter to
        # "create" its own IBSS cell rather than merge with the Pis' existing
        # one, and its broadcast RX path won't decode peer OGMs in that state —
        # batman-adv ends up with zero neighbors. Letting the kernel auto-merge
        # picks up whichever BSSID the Pi mesh has settled on.
        info "Joining IBSS cell ewego-mesh..."
        sudo iw dev "$IFACE" ibss join ewego-mesh 2437 NOHT

        # Add wlan0 to batman
        info "Adding $IFACE to batman mesh..."
        sudo batctl meshif bat0 if add "$IFACE" 2>/dev/null || true

        # Bring up bat0 and assign IP
        sudo ip link set bat0 up
        sudo ip addr flush dev bat0
        sudo ip addr add "${MESH_IP}/24" dev bat0

        echo ""
        info "Connected to mesh as $MESH_IP (bat0)"
        echo "  Show neighbors:  sudo batctl meshif bat0 n"
        echo "  Ping a device:   ping 10.42.0.1"
        echo "  SSH to ewe1:     ssh william@10.42.0.1"
        echo "  Disconnect:      bash $0 leave"
        ;;

    leave)
        info "Tearing down mesh..."

        # Bring down bat0
        sudo ip addr flush dev bat0 2>/dev/null || true
        sudo ip link set bat0 down 2>/dev/null || true

        # Remove wlan0 from batman
        sudo batctl meshif bat0 if del "$IFACE" 2>/dev/null || true

        # Restore managed mode so NetworkManager can reclaim the interface
        sudo ip link set "$IFACE" down
        sudo iw dev "$IFACE" set type managed
        sudo ip link set "$IFACE" up

        # Hand interface back to NetworkManager
        info "Restoring NetworkManager control..."
        if command -v nmcli &>/dev/null; then
            nmcli device set "$IFACE" managed yes 2>/dev/null || true
        fi
        sudo systemctl restart NetworkManager

        info "Disconnected from mesh — normal WiFi should reconnect shortly"
        ;;

    status)
        echo "=== bat0 Interface ==="
        ip addr show bat0 2>/dev/null || warn "bat0 not found (mesh not active?)"
        echo ""
        echo "=== IBSS Interface ($IFACE) ==="
        iw dev "$IFACE" info 2>/dev/null || warn "$IFACE not found"
        echo ""
        echo "=== Mesh Neighbors ==="
        sudo batctl meshif bat0 n 2>/dev/null || warn "No neighbors (or mesh not active)"
        echo ""
        echo "=== Originator Table ==="
        sudo batctl meshif bat0 o 2>/dev/null || warn "No originators (or mesh not active)"
        ;;

    *)
        error "Unknown action: $ACTION"
        echo "Usage: bash $0 [scan|join|leave|status] [ip-suffix]"
        exit 1
        ;;
esac
