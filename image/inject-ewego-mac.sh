#!/bin/bash
#
# inject-ewego-mac.sh — macOS wrapper around inject-ewego.sh.
#
# macOS cannot mount the ext4 root partition of a Raspberry Pi OS image, so
# the injection runs inside a Linux container against the .img FILE. The
# vendored Python packages are downloaded inside the container too, so the
# only thing needed on the Mac is Docker Desktop. On Apple Silicon the
# container is arm64 and the apt step runs natively; on Intel Macs Docker
# Desktop's built-in binfmt emulation handles it.
#
#   xz -dk 2026-xx-xx-raspios-trixie-arm64-lite.img.xz
#   ./inject-ewego-mac.sh 2026-xx-xx-raspios-trixie-arm64-lite.img
#
# Then flash the modified .img with Raspberry Pi Imager ("Use custom").
# Run from a checkout of the EweGo repository (this script finds it by its
# own location).

set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

[ $# -ge 1 ] || die "usage: $0 <raspios.img> [extra inject-ewego.sh options]"
IMG=$1; shift

case "$IMG" in
    *.xz) die "image is compressed — decompress it first:  xz -dk '$IMG'" ;;
esac
[ -f "$IMG" ] || die "image $IMG not found"
command -v docker >/dev/null 2>&1 || die "Docker Desktop is required on macOS"

IMG_DIR=$(cd "$(dirname "$IMG")" && pwd); IMG_NAME=$(basename "$IMG")
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
[ -d "$REPO_ROOT/Firmware" ] || die "this script must live in <repo>/image/"

VERSION=$(git -C "$REPO_ROOT" describe --tags --always --dirty 2>/dev/null || echo unknown)

# --privileged is needed for loop devices, mounting, and chroot.
docker run --rm --privileged \
    -e EWEGO_VERSION="$VERSION" \
    -v "$IMG_DIR":/img \
    -v "$REPO_ROOT":/repo:ro \
    debian:stable-slim \
    bash -c "apt-get update -qq >/dev/null && \
             apt-get install -y -qq rsync fdisk e2fsprogs unzip python3-pip >/dev/null && \
             /repo/image/vendor-pylib.sh /tmp/pylib && \
             /repo/image/inject-ewego.sh /img/$IMG_NAME --pylib /tmp/pylib $*"

echo
echo "Image modified in place: $IMG"
echo "Flash it with Raspberry Pi Imager -> 'Use custom', or:"
echo "  diskutil unmountDisk /dev/diskN && sudo dd if=$IMG_NAME of=/dev/rdiskN bs=4m"
