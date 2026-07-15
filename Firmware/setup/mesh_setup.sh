#!/usr/bin/env bash
# ============================================================================
# EweGo B.A.T.M.A.N. Mesh Networking Setup (Pi-side)
# ============================================================================
# Configures (or removes) the IBSS + batman-adv mesh stack on this Pi.
# Run after pi_setup.sh has set the hostname to eweN.
#
# The CM4's BCM43455 does NOT support 802.11s mesh mode. We use IBSS
# (ad-hoc) mode as the transport with batman-adv for L2 routing. A
# systemd service brings up the mesh on boot (not NetworkManager).
#
# Usage:
#   bash mesh_setup.sh                Install/refresh mesh setup (idempotent)
#   bash mesh_setup.sh install        Same as above
#   bash mesh_setup.sh disable        Stop mesh service, hand wlan0 back to NM
# ============================================================================

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*"; }

ACTION="${1:-install}"

NM_UNMANAGED="/etc/NetworkManager/conf.d/ewego-unmanaged.conf"
MESH_SCRIPT="/usr/local/bin/ewego-mesh-start.sh"
MESH_SERVICE="/etc/systemd/system/ewego-mesh.service"
BATMAN_MODULE="/etc/modules-load.d/batman-adv.conf"

require_ewe_hostname() {
    HOSTNAME_NOW=$(hostname)
    # Accept ewe<digits> with any non-digit middle: ewe7, ewe007, ewego7, ewego007, ...
    # The trailing digit run (decoded as decimal, leading zeros stripped) is the device number.
    if [[ "$HOSTNAME_NOW" =~ ^ewe[^0-9]*([0-9]+)$ ]]; then
        DEVICE_NUM=$((10#${BASH_REMATCH[1]}))
        if [ "$DEVICE_NUM" -lt 1 ] || [ "$DEVICE_NUM" -gt 254 ]; then
            error "Device number $DEVICE_NUM (from '$HOSTNAME_NOW') out of range (1-254)"
            exit 1
        fi
        MESH_IP="10.42.0.${DEVICE_NUM}"
    else
        error "Hostname '$HOSTNAME_NOW' does not match 'ewe[letters]<number>' pattern."
        echo "  Examples: ewe7, ewe007, ewego7, ewego007"
        echo "  Set with: sudo hostnamectl set-hostname ewe7"
        exit 1
    fi
}

install_mesh() {
    require_ewe_hostname
    info "Configuring mesh for $HOSTNAME_NOW (bat0 = $MESH_IP/24)..."

    if ! command -v batctl &>/dev/null; then
        info "Installing batctl..."
        sudo apt update -qq
        sudo apt install -y batctl
    fi

    if [ ! -f "$BATMAN_MODULE" ]; then
        info "Enabling batman-adv module on boot..."
        echo "batman-adv" | sudo tee "$BATMAN_MODULE"
    fi

    # Clean up empty/corrupt netplan files left by first-boot auto-config
    if [ -d /etc/netplan ]; then
        for f in /etc/netplan/90-NM-*.yaml; do
            [ -f "$f" ] || continue
            if [ ! -s "$f" ]; then
                info "Removing empty netplan file: $f"
                sudo rm -f "$f"
            fi
        done
    fi

    # Remove old 802.11s mesh profile if present (from previous setup attempts)
    sudo rm -f /etc/NetworkManager/system-connections/ewego-mesh.nmconnection

    # Tell NetworkManager to leave wlan0 (mesh) and usb0 (USB-C gadget) alone.
    # Both are managed by their own systemd services (ewego-mesh,
    # ewego-usb-gadget). Always reconcile — older deploys wrote wlan0-only,
    # and NM racing the gadget's static IP on usb0 causes it to flap.
    NM_UNMANAGED_WANT='[keyfile]
unmanaged-devices=interface-name:wlan0;interface-name:usb0'
    if [ ! -f "$NM_UNMANAGED" ] || ! diff -q <(echo "$NM_UNMANAGED_WANT") "$NM_UNMANAGED" >/dev/null 2>&1; then
        info "Configuring NetworkManager to ignore wlan0 and usb0..."
        sudo mkdir -p /etc/NetworkManager/conf.d
        echo "$NM_UNMANAGED_WANT" | sudo tee "$NM_UNMANAGED" > /dev/null
    fi

    # --- Mesh startup script ---
    info "Installing mesh startup script ($MESH_SCRIPT)..."
    sudo tee "$MESH_SCRIPT" > /dev/null <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail

# Derive device number from hostname trailing digits (ewe7, ewe007, ewego007, etc.)
HOSTNAME=$(hostname)
if [[ "$HOSTNAME" =~ ^ewe[^0-9]*([0-9]+)$ ]]; then
    DEVICE_NUM=$((10#${BASH_REMATCH[1]}))
    if [ "$DEVICE_NUM" -lt 1 ] || [ "$DEVICE_NUM" -gt 254 ]; then
        echo "ERROR: device number $DEVICE_NUM out of range (1-254)"
        exit 1
    fi
else
    echo "ERROR: hostname '$HOSTNAME' does not match 'ewe[letters]<number>' pattern"
    exit 1
fi

MESH_IP="10.42.0.${DEVICE_NUM}"
IFACE="wlan0"
CELL="02:12:34:56:78:9A"   # Fixed IBSS cell ID — all nodes must match

modprobe batman-adv 2>/dev/null || true

ip link set "$IFACE" down
iw dev "$IFACE" set type ibss
ip link set "$IFACE" up

# Join the IBSS cell (2437 MHz = channel 6, 2.4 GHz). NOHT is required —
# the BCM43455 driver returns EINVAL on 'HT20' in IBSS mode (HT is only
# supported in managed mode on this chip).
iw dev "$IFACE" ibss join ewego-mesh 2437 NOHT fixed-freq "$CELL"

batctl meshif bat0 if add "$IFACE" 2>/dev/null || true

# Raise OGM broadcast rate from default 1000ms → 250ms (4 Hz). Faster mesh
# convergence, tighter TQ resolution, quicker dead-link detection. Costs
# ~2% extra broadcast airtime — negligible on a mesh whose primary purpose
# is time sync.
batctl meshif bat0 orig_interval 250 2>/dev/null || true

ip link set bat0 up
ip addr flush dev bat0
ip addr add "${MESH_IP}/24" dev bat0

echo "Mesh active: bat0 = ${MESH_IP}/24 (IBSS + batman-adv)"
SCRIPT
    sudo chmod 755 "$MESH_SCRIPT"

    # --- Systemd service ---
    info "Installing mesh systemd service ($MESH_SERVICE)..."
    sudo tee "$MESH_SERVICE" > /dev/null <<'EOF'
[Unit]
Description=EweGo B.A.T.M.A.N. Mesh Network
After=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/ewego-mesh-start.sh
ExecStartPost=-/bin/systemctl try-restart chrony
ExecStop=/usr/bin/ip link set bat0 down

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable ewego-mesh.service

    # NM picks up the unmanaged-devices config on reboot.
    # Do NOT restart NetworkManager here — it kills active SSH over WiFi.
    sudo nmcli connection reload 2>/dev/null || true

    echo ""
    info "Mesh setup complete (fully active after reboot)"
    echo "  Service:   ewego-mesh.service (enabled)"
    echo "  Mesh IP:   ${MESH_IP}/24 on bat0"
    echo "  IBSS SSID: ewego-mesh @ 2437MHz (channel 6)"
    echo ""
    echo "  Verify after reboot:"
    echo "    sudo systemctl status ewego-mesh"
    echo "    sudo batctl meshif bat0 n        # neighbors"
    echo "    sudo batctl meshif bat0 o        # originators"
    echo "    ping 10.42.0.<other-device>"
    echo ""
    echo "  Disable later with: bash $0 disable"
}

disable_mesh() {
    info "Disabling mesh — wlan0 will return to NetworkManager..."

    sudo systemctl stop ewego-mesh.service 2>/dev/null || true
    sudo systemctl disable ewego-mesh.service 2>/dev/null || true

    if [ -f "$NM_UNMANAGED" ]; then
        info "Removing NM unmanaged config..."
        sudo rm -f "$NM_UNMANAGED"
    fi

    # Tear down bat0 and detach wlan0 from batman
    sudo ip link set bat0 down 2>/dev/null || true
    sudo batctl meshif bat0 if del wlan0 2>/dev/null || true

    # Restore wlan0 to managed mode so NM can use it
    sudo ip link set wlan0 down 2>/dev/null || true
    sudo iw dev wlan0 set type managed 2>/dev/null || true
    sudo ip link set wlan0 up 2>/dev/null || true

    sudo systemctl reload NetworkManager 2>/dev/null \
        || sudo systemctl restart NetworkManager

    info "Mesh disabled. wlan0 is now under NetworkManager control."
    echo "  Files left in place for easy re-enable: $MESH_SCRIPT, $MESH_SERVICE"
    echo "  Re-enable with: bash $0 install"
}

case "$ACTION" in
    install|"")
        install_mesh
        ;;
    disable)
        disable_mesh
        ;;
    *)
        error "Unknown action: $ACTION"
        echo "Usage: bash $0 [install|disable]"
        exit 1
        ;;
esac
