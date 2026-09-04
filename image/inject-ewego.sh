#!/bin/bash
#
# inject-ewego.sh — bake the EweGo firmware into a Raspberry Pi OS Lite
# image (or an already-flashed SD card) so a collar works from the very
# first boot with no network access and no first-boot steps of its own.
#
#   sudo ./inject-ewego.sh raspios.img --pylib build/pylib      # image file
#   sudo ./inject-ewego.sh /dev/sdX    --pylib build/pylib      # flashed card
#
# Options:
#   --pylib DIR   directory produced by vendor-pylib.sh (pure-Python packages
#                 that Debian does not ship). Default: <repo>/build/pylib
#   --no-apt      skip the apt step (for quick tests of the file injection)
#   --grow SIZE   grow an image FILE by SIZE before injecting (default 1G,
#                 0 to disable). Ignored for block devices.
#
# What it does to the target:
#   rootfs: /opt/ewego/                          Firmware tree from this repo
#           /opt/ewego/pylib/                    vendored pure-Python packages
#           /etc/systemd/system/ewego-*.service  installed, NOT enabled
#           /etc/modules-load.d/ewego.conf       i2c-dev
#           /etc/ewego-image-release             version + build date
#           apt packages from apt-packages.txt   installed inside the image
#                                                through an emulated chroot
#   boot:   config.txt                           dtparam=ant2 at the top,
#                                                hardware block appended,
#                                                [cm4] otg_mode=1 verified
#           cmdline.txt                          untouched (console=serial0 stays)
#
# Needs on the machine running it: root; losetup + partx (image files);
# sfdisk + e2fsprogs (--grow); rsync; and for the apt step either an arm64
# host, or qemu-user-static with binfmt_misc registered.

set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> $*"; }

[ "$(id -u)" -eq 0 ] || die "run with sudo (mounting partitions needs root)"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_DIR="$REPO_ROOT/image"

TARGET=""
PYLIB="$REPO_ROOT/build/pylib"
DO_APT=1
GROW="1G"

while [ $# -gt 0 ]; do
    case "$1" in
        --pylib)  PYLIB=$2; shift 2 ;;
        --no-apt) DO_APT=0; shift ;;
        --grow)   GROW=$2; shift 2 ;;
        -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
        -*)       die "unknown option $1" ;;
        *)        [ -z "$TARGET" ] || die "unexpected argument $1"; TARGET=$1; shift ;;
    esac
done

[ -n "$TARGET" ]  || die "usage: $0 <device-or-image> [--pylib DIR] [--no-apt] [--grow SIZE]"
[ -e "$TARGET" ]  || die "$TARGET not found"
[ -d "$PYLIB" ]   || die "pylib directory $PYLIB not found (run image/vendor-pylib.sh first)"
[ -d "$REPO_ROOT/Firmware" ] || die "Firmware/ not found next to image/ — run from a repo checkout"
for f in apt-packages.txt config.txt.ewego units; do
    [ -e "$IMAGE_DIR/$f" ] || die "missing $IMAGE_DIR/$f"
done
command -v rsync >/dev/null || die "rsync is required"

APT_PACKAGES=$(grep -Ev '^\s*(#|$)' "$IMAGE_DIR/apt-packages.txt" | xargs)
EWEGO_VERSION=${EWEGO_VERSION:-$(git -C "$REPO_ROOT" describe --tags --always --dirty 2>/dev/null || echo unknown)}

# --- grow image files so the apt packages fit ------------------------------
if [ ! -b "$TARGET" ] && [ "$GROW" != "0" ]; then
    log "Growing $TARGET by $GROW"
    truncate -s "+$GROW" "$TARGET"
    # extend partition 2 (rootfs) to the end of the file
    echo ', +' | sfdisk -q -N 2 --no-reread --no-tell-kernel "$TARGET"
fi

# --- attach image files to a loop device; use block devices as-is ----------
LOOPDEV=""
WORK=""
ROOT_MNT=""
BOOT_MNT=""
cleanup() {
    set +e
    if [ -n "$ROOT_MNT" ]; then
        for m in dev/pts dev proc sys tmp boot/firmware; do
            mountpoint -q "$ROOT_MNT/$m" && umount "$ROOT_MNT/$m"
        done
        mountpoint -q "$ROOT_MNT" && umount "$ROOT_MNT"
    fi
    [ -n "$LOOPDEV" ] && losetup -d "$LOOPDEV"
    [ -n "$WORK" ] && rm -rf "$WORK"
}
trap cleanup EXIT

if [ -b "$TARGET" ]; then
    DEV=$TARGET
else
    LOOPDEV=$(losetup -fP --show "$TARGET")   # -P scans the partition table
    DEV=$LOOPDEV
    # some environments (containers, WSL) don't create the p1/p2 nodes; partx does
    [ -b "${DEV}p1" ] || partx -a "$DEV" 2>/dev/null || true
fi

# partition names differ: /dev/sdb -> sdb1/sdb2, /dev/loop0|mmcblk0 -> p1/p2
if [ -b "${DEV}p1" ]; then
    BOOT_PART=${DEV}p1 ROOT_PART=${DEV}p2
elif [ -b "${DEV}1" ]; then
    BOOT_PART=${DEV}1 ROOT_PART=${DEV}2
else
    die "cannot find partitions on $DEV — is this a Raspberry Pi OS image?"
fi

if [ ! -b "$TARGET" ] && [ "$GROW" != "0" ]; then
    log "Resizing root filesystem on $ROOT_PART"
    e2fsck -fp "$ROOT_PART" >/dev/null || true
    resize2fs "$ROOT_PART"
fi

WORK=$(mktemp -d)
ROOT_MNT=$WORK/root
mkdir -p "$ROOT_MNT"
mount "$ROOT_PART" "$ROOT_MNT"
[ -d "$ROOT_MNT/etc/systemd/system" ] || die "$ROOT_PART doesn't look like a Linux rootfs"
BOOT_MNT=$ROOT_MNT/boot/firmware
[ -d "$BOOT_MNT" ] || die "no /boot/firmware in rootfs — this image is older than Bookworm"
mount "$BOOT_PART" "$BOOT_MNT"
[ -f "$BOOT_MNT/config.txt" ] || die "no config.txt on the boot partition"

# --- rootfs: firmware tree --------------------------------------------------
log "Installing firmware tree to /opt/ewego"
install -d "$ROOT_MNT/opt/ewego"
rsync -a --delete \
    --exclude-from="$REPO_ROOT/.rsyncignore" \
    --exclude 'pylib/' \
    "$REPO_ROOT/Firmware" \
    "$REPO_ROOT/requirements.txt" "$REPO_ROOT/pyproject.toml" "$REPO_ROOT/LICENSE" \
    "$ROOT_MNT/opt/ewego/"

log "Installing vendored Python packages to /opt/ewego/pylib"
rm -rf "$ROOT_MNT/opt/ewego/pylib"
install -d "$ROOT_MNT/opt/ewego/pylib"
cp -a "$PYLIB"/. "$ROOT_MNT/opt/ewego/pylib/"

# --- rootfs: units (installed, deliberately not enabled) ------------------
log "Installing systemd units (not enabled)"
for u in "$IMAGE_DIR"/units/*.service; do
    install -m 644 "$u" "$ROOT_MNT/etc/systemd/system/$(basename "$u")"
done
# make sure a re-run never leaves a stale enable symlink behind
rm -f "$ROOT_MNT"/etc/systemd/system/multi-user.target.wants/ewego-*.service

# --- rootfs: kernel modules and release marker ----------------------------
echo i2c-dev > "$ROOT_MNT/etc/modules-load.d/ewego.conf"
{
    echo "EWEGO_VERSION=$EWEGO_VERSION"
    echo "EWEGO_BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "EWEGO_APT_PACKAGES=\"$APT_PACKAGES\""
} > "$ROOT_MNT/etc/ewego-image-release"

# --- rootfs: apt packages, installed inside the image ---------------------
if [ "$DO_APT" -eq 1 ]; then
    log "Installing apt packages inside the image: $APT_PACKAGES"

    # Emulation: on an arm64 host the chroot runs natively. Elsewhere,
    # binfmt_misc must hand aarch64 binaries to qemu. Copying the static
    # qemu binary into the rootfs covers registrations without the F flag.
    QEMU=$(command -v qemu-aarch64-static || true)
    QEMU_COPIED=""
    if [ "$(uname -m)" != "aarch64" ] && [ -n "$QEMU" ] && [ ! -e "$ROOT_MNT/usr/bin/qemu-aarch64-static" ]; then
        cp "$QEMU" "$ROOT_MNT/usr/bin/qemu-aarch64-static"
        QEMU_COPIED=1
    fi

    mount -t proc proc "$ROOT_MNT/proc"
    mount -t sysfs sys "$ROOT_MNT/sys"
    mount --bind /dev "$ROOT_MNT/dev"
    mount --bind /dev/pts "$ROOT_MNT/dev/pts"
    mount -t tmpfs tmpfs "$ROOT_MNT/tmp"

    # DNS for the chroot. The host's resolv.conf may point at a local stub
    # resolver (127.0.0.53) that the chroot cannot reach, so write real ones.
    RESOLV_BAK=""
    if [ -e "$ROOT_MNT/etc/resolv.conf" ] || [ -L "$ROOT_MNT/etc/resolv.conf" ]; then
        mv "$ROOT_MNT/etc/resolv.conf" "$ROOT_MNT/etc/resolv.conf.ewego-bak"
        RESOLV_BAK=1
    fi
    printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > "$ROOT_MNT/etc/resolv.conf"

    # Never let package scripts start services inside the chroot.
    printf '#!/bin/sh\nexit 101\n' > "$ROOT_MNT/usr/sbin/policy-rc.d"
    chmod 755 "$ROOT_MNT/usr/sbin/policy-rc.d"

    if ! chroot "$ROOT_MNT" /usr/bin/env -i \
            PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
            HOME=/root LC_ALL=C.UTF-8 LANG=C.UTF-8 DEBIAN_FRONTEND=noninteractive \
            bash -c "apt-get update -q && \
                     apt-get install -y -q --no-install-recommends $APT_PACKAGES && \
                     apt-get clean && rm -rf /var/lib/apt/lists/*"; then
        echo "hint: 'Exec format error' means aarch64 emulation is not set up — install qemu-user-static and binfmt-support" >&2
        die "apt step failed"
    fi

    rm -f "$ROOT_MNT/usr/sbin/policy-rc.d"
    rm -f "$ROOT_MNT/etc/resolv.conf"
    [ -n "$RESOLV_BAK" ] && mv "$ROOT_MNT/etc/resolv.conf.ewego-bak" "$ROOT_MNT/etc/resolv.conf"
    [ -n "$QEMU_COPIED" ] && rm -f "$ROOT_MNT/usr/bin/qemu-aarch64-static"

    for m in tmp dev/pts dev sys proc; do umount "$ROOT_MNT/$m"; done
else
    log "Skipping apt step (--no-apt)"
fi

# --- boot partition: config.txt -------------------------------------------
log "Editing config.txt"
CONFIG=$BOOT_MNT/config.txt

# External antenna on the CM4. Has to appear early in the file to take effect.
if ! grep -q '^dtparam=ant2' "$CONFIG"; then
    { printf '# EweGo: CM4 external antenna (must be early in the file)\ndtparam=ant2\n\n'; cat "$CONFIG"; } > "$CONFIG.new"
    mv "$CONFIG.new" "$CONFIG"
fi

# Hardware block (same content as Firmware/setup/pi_setup.sh writes).
if ! grep -q '=== EweGo Hardware Configuration ===' "$CONFIG"; then
    { printf '\n'; cat "$IMAGE_DIR/config.txt.ewego"; } >> "$CONFIG"
fi

# USB host mode on the CM4's USB-C connector, so a webcam can be plugged in.
# Stock Pi OS already carries this; make sure it survives.
if ! grep -q '^otg_mode=1' "$CONFIG"; then
    printf '\n[cm4]\notg_mode=1\n\n[all]\n' >> "$CONFIG"
fi

sync
log "done: EweGo $EWEGO_VERSION injected into $TARGET"
echo "    units installed but not enabled: $(ls "$IMAGE_DIR"/units | tr '\n' ' ')"
echo "    enable on the collar with: sudo systemctl enable --now ewego-sensors"
