#!/bin/bash
#
# build-uvcvideo.sh — runs INSIDE the image chroot (arm64, emulated in CI).
#
# Builds the patched uvcvideo module from /tmp/uvc-src against the image's
# own kernel headers, installs it under /lib/modules/<ver>/updates/ so it
# takes precedence over the stock module, and sets the max_payload option.
#
#   MAX_PAYLOAD=2048 ./build-uvcvideo.sh
#
# The exact-version headers package (linux-headers-<uname -r>) is required,
# because the module must match the installed kernel's symbol versions. If
# the apt mirror no longer carries that exact version, the kernel image is
# upgraded together with its headers so they match again.

set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> $*"; }

MAX_PAYLOAD=${MAX_PAYLOAD:-2048}
SRC=/tmp/uvc-src
[ -f "$SRC/uvc_video.c" ] || die "$SRC does not contain the uvcvideo source"
grep -q uvc_max_payload_param "$SRC/uvc_video.c" || die "$SRC is not patched (run patch-uvcvideo.py first)"

kver() { ls /lib/modules | grep -- '-rpi-v8$' | sort -V | tail -1; }
KVER=$(kver)
[ -n "$KVER" ] || die "no *-rpi-v8 kernel under /lib/modules"
log "Kernel in image: $KVER"

export DEBIAN_FRONTEND=noninteractive
BUILD_PKGS="build-essential bc"
if apt-get install -y -q --no-install-recommends $BUILD_PKGS "linux-headers-$KVER"; then
    HEADERS_PKG="linux-headers-$KVER"
else
    log "exact headers for $KVER not available; upgrading kernel + headers together"
    apt-get install -y -q --no-install-recommends $BUILD_PKGS linux-image-rpi-v8 linux-headers-rpi-v8
    KVER=$(kver)
    HEADERS_PKG="linux-headers-rpi-v8"
    log "Kernel is now: $KVER"
fi
[ -d "/lib/modules/$KVER/build" ] || die "no /lib/modules/$KVER/build after installing headers"

log "Building uvcvideo for $KVER"
make -C "/lib/modules/$KVER/build" M="$SRC" modules -j"$(nproc)" 2>&1 | tail -n 20
[ -f "$SRC/uvcvideo.ko" ] || die "uvcvideo.ko was not produced"

log "Installing to /lib/modules/$KVER/updates/"
install -d "/lib/modules/$KVER/updates"
install -m 644 "$SRC/uvcvideo.ko" "/lib/modules/$KVER/updates/uvcvideo.ko"
depmod -a "$KVER"
modinfo -k "$KVER" uvcvideo | grep -E '^(filename|vermagic|parm:.*max_payload)' || die "modinfo does not show the patched module"

cat > /etc/modprobe.d/ewego-uvc.conf <<EOF
# EweGo: cap each UVC camera's isochronous reservation (bytes per USB
# microframe) so two MJPEG cameras fit on the CM4's single USB 2.0 bus,
# whose budget is about 6000 B/microframe. Provided by the patched uvcvideo
# in /lib/modules/$KVER/updates/. 0 disables the cap.
options uvcvideo max_payload=$MAX_PAYLOAD
EOF
log "max_payload=$MAX_PAYLOAD set in /etc/modprobe.d/ewego-uvc.conf"

log "Removing build tools"
apt-get purge -y -q $BUILD_PKGS "$HEADERS_PKG" >/dev/null
apt-get autoremove -y -q --purge >/dev/null
apt-get clean
rm -rf "$SRC"
