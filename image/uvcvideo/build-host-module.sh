#!/bin/bash
#
# build-host-module.sh — build and install the patched uvcvideo (max_payload
# cap, 32 URBs) for the kernel running on THIS Debian/Ubuntu machine, e.g. a
# Linux VM or a bench Pi that is not running the collar image.
#
#   sudo ./build-host-module.sh [MAX_PAYLOAD]      (default 1900)
#
# It installs the kernel headers and build tools with apt, fetches the
# uvcvideo source for the running kernel's base version from the mainline
# tree, applies patch-uvcvideo.py, builds, installs the module under
# /lib/modules/<ver>/updates/ (which depmod prefers over the stock one),
# writes /etc/modprobe.d/ewego-uvc.conf and reloads the module.
#
# A kernel upgrade reverts to the stock driver: re-run after one. The patch
# is anchored on exact source lines and fails loudly if the kernel's uvc
# driver has changed; report the version if that happens.

set -euo pipefail
die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> $*"; }

[ "$(id -u)" -eq 0 ] || die "run with sudo"
MAX_PAYLOAD=${1:-1900}
HERE="$(cd "$(dirname "$0")" && pwd)"
KVER=$(uname -r)
BASE=${KVER%%-*}                       # 6.8.0-45-generic -> 6.8.0
MAJMIN=$(echo "$BASE" | cut -d. -f1,2) # 6.8
PATCH=${BASE#"$MAJMIN".}               # 0 / 12 / ...
if [ "$PATCH" = "0" ] || [ "$PATCH" = "$BASE" ]; then TAG="v$MAJMIN"; else TAG="v$BASE"; fi
log "kernel $KVER -> uvcvideo source from torvalds/linux $TAG"

export DEBIAN_FRONTEND=noninteractive
apt-get install -y -q --no-install-recommends "linux-headers-$KVER" build-essential git python3 >/dev/null
[ -d "/lib/modules/$KVER/build" ] || die "no /lib/modules/$KVER/build after installing headers"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
git clone -q --depth 1 --filter=blob:none --sparse --branch "$TAG" https://github.com/torvalds/linux.git "$WORK/linux" \
    || die "no tag $TAG in torvalds/linux; pass the right source by hand"
git -C "$WORK/linux" sparse-checkout set --no-cone drivers/media/usb/uvc >/dev/null
cp -a "$WORK/linux/drivers/media/usb/uvc" "$WORK/uvc"
python3 "$HERE/patch-uvcvideo.py" "$WORK/uvc"

log "building against /lib/modules/$KVER/build"
make -C "/lib/modules/$KVER/build" M="$WORK/uvc" modules -j"$(nproc)" 2>&1 | tail -n 5
[ -f "$WORK/uvc/uvcvideo.ko" ] || die "uvcvideo.ko was not produced"

install -d "/lib/modules/$KVER/updates"
install -m 644 "$WORK/uvc/uvcvideo.ko" "/lib/modules/$KVER/updates/uvcvideo.ko"
depmod -a "$KVER"
cat > /etc/modprobe.d/ewego-uvc.conf <<EOF
# EweGo: cap each UVC camera's isochronous reservation (bytes per USB
# microframe) so two MJPEG cameras share one USB 2.0 bus. 0 disables.
options uvcvideo max_payload=$MAX_PAYLOAD
EOF

log "reloading uvcvideo (close any camera users first)"
modprobe -r uvcvideo 2>/dev/null || die "could not unload uvcvideo: a camera is in use"
modprobe uvcvideo
echo "uvcvideo: $(modinfo -F filename uvcvideo)"
echo "max_payload: $(cat /sys/module/uvcvideo/parameters/max_payload)"
log "done. Verify with: sudo python3 Firmware/webtest/ewego_webtest.py (Bandwidth probe) or dmesg after a capture"
