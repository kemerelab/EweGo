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
#   --bin DIR     directory of cross-compiled static binaries to install into
#                 /usr/local/bin (ewego-cam ...). Default: <repo>/build/bin;
#                 skipped with a warning if absent
#   --no-apt      skip the apt step (for quick tests of the file injection)
#   --no-uvc      skip building the patched uvcvideo module (see uvcvideo/)
#   --grow SIZE   grow an image FILE by SIZE before injecting (default 1G,
#                 0 to disable). Ignored for block devices.
# Environment:
#   UVC_MAX_PAYLOAD  bytes per microframe each camera may reserve (default 2048)
#
# What it does to the target:
#   rootfs: /opt/ewego/                          Firmware tree from this repo
#           /opt/ewego/pylib/                    vendored pure-Python packages
#           /etc/systemd/system/ewego-*.service  installed, NOT enabled
#           /etc/modules-load.d/ewego.conf       i2c-dev
#           /etc/ewego-image-release             version + build date
#           apt packages from apt-packages.txt   installed inside the image
#                                                through an emulated chroot
#           /lib/modules/<ver>/updates/uvcvideo.ko  patched UVC driver with a
#                                                max_payload cap, built in the
#                                                same chroot (uvcvideo/README.md)
#           /etc/modprobe.d/ewego-uvc.conf       options uvcvideo max_payload=2048
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
BINDIR="$REPO_ROOT/build/bin"
DO_APT=1
DO_UVC=1
GROW="1G"
UVC_MAX_PAYLOAD=${UVC_MAX_PAYLOAD:-2048}

while [ $# -gt 0 ]; do
    case "$1" in
        --pylib)  PYLIB=$2; shift 2 ;;
        --bin)    BINDIR=$2; shift 2 ;;
        --no-apt) DO_APT=0; shift ;;
        --no-uvc) DO_UVC=0; shift ;;
        --grow)   GROW=$2; shift 2 ;;
        -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
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
if [ "$DO_UVC" -eq 1 ]; then
    command -v git >/dev/null || die "git is required to fetch the uvcvideo source (or pass --no-uvc)"
    command -v python3 >/dev/null || die "python3 is required to patch the uvcvideo source (or pass --no-uvc)"
    [ "$DO_APT" -eq 1 ] || die "--no-uvc is required together with --no-apt (the module is built in the chroot)"
fi

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

# --- rootfs: compiled binaries and their config ---------------------------
if [ -d "$BINDIR" ]; then
    for b in "$BINDIR"/*; do
        [ -f "$b" ] || continue
        log "Installing /usr/local/bin/$(basename "$b")"
        install -m 755 "$b" "$ROOT_MNT/usr/local/bin/$(basename "$b")"
    done
else
    echo "warning: no binaries directory at $BINDIR (ewego-cam will be missing); pass --bin DIR" >&2
fi
install -d "$ROOT_MNT/etc/ewego"
for f in "$IMAGE_DIR"/ewego/*.conf; do
    install -m 644 "$f" "$ROOT_MNT/etc/ewego/$(basename "$f")"
done

# --- rootfs: units (installed, deliberately not enabled) ------------------
log "Installing systemd units (not enabled)"
for u in "$IMAGE_DIR"/units/*.service; do
    install -m 644 "$u" "$ROOT_MNT/etc/systemd/system/$(basename "$u")"
done
# make sure a re-run never leaves a stale enable symlink behind
rm -f "$ROOT_MNT"/etc/systemd/system/multi-user.target.wants/ewego-*.service

# The only unit enabled by default is the web test console (port 8080,
# no authentication), so a freshly flashed collar can be exercised from a
# browser. Recording units stay disabled until started deliberately.
ENABLE_UNITS="ewego-webtest.service"
install -d "$ROOT_MNT/etc/systemd/system/multi-user.target.wants"
for u in $ENABLE_UNITS; do
    log "Enabling $u"
    ln -sf "/etc/systemd/system/$u" "$ROOT_MNT/etc/systemd/system/multi-user.target.wants/$u"
done

# --- rootfs: no apt at first boot -------------------------------------------
# Raspberry Pi Imager's cloud-init user-data asks for package installs and
# a full upgrade on first boot ("packages: [avahi-daemon]",
# "package_upgrade: true"). That needs network, takes minutes, and replaced
# the kernel under the patched uvcvideo once. Everything is baked in here
# instead (avahi-daemon is in apt-packages.txt), so drop cloud-init's
# package module from the module list. user-data cannot re-enable a module
# that is not in the list.
if [ -f "$ROOT_MNT/etc/cloud/cloud.cfg" ]; then
    log "Disabling cloud-init package installs/upgrades at first boot"
    python3 - "$ROOT_MNT/etc/cloud" "$ROOT_MNT/etc/cloud/cloud.cfg.d/99-ewego-no-apt.cfg" <<'PY'
import glob, re, sys
cloud_dir, dst = sys.argv[1], sys.argv[2]
out = ("# EweGo: everything is installed at image build time. Do not let cloud-init\n"
       "# (Raspberry Pi Imager's user-data) install or upgrade packages at first boot.\n"
       "package_update: false\npackage_upgrade: false\npackage_reboot_if_required: false\n")
removed = 0
lists = {}
# The module lists may live in cloud.cfg or in a cloud.cfg.d/ drop-in, and
# the package module may sit in any stage list depending on the version.
# Rewrite every list that contains it; the last definition wins in cloud-init.
for path in [cloud_dir + "/cloud.cfg"] + sorted(glob.glob(cloud_dir + "/cloud.cfg.d/*.cfg")):
    if path == dst:
        continue
    try:
        text = open(path).read()
    except OSError:
        continue
    for key in ("cloud_init_modules", "cloud_config_modules", "cloud_final_modules"):
        m = re.search(r"^" + key + r":[ \t]*\n((?:[ \t]+-.*\n|[ \t]*#.*\n|[ \t]*\n)+)", text, re.M)
        if m:
            lists[key] = (path, [l for l in m.group(1).splitlines() if l.strip().startswith("-")])
for key, (path, items) in lists.items():
    kept = [l for l in items if "package" not in l.lower()]
    if len(kept) != len(items):
        removed += len(items) - len(kept)
        out += key + ":\n" + "\n".join(kept) + "\n"
        print("cloud-init: %s in %s: removed %d package module entr%s" %
              (key, path, len(items) - len(kept), "y" if len(items) - len(kept) == 1 else "ies"))
if not removed:
    print("warning: no package module found in any cloud-init module list; "
          "writing only package_update/upgrade: false. Module lists seen:")
    for key, (path, items) in lists.items():
        print("  %s (%s): %s" % (key, path, ", ".join(l.strip() for l in items)))
with open(dst, "w") as f:
    f.write(out)
PY
fi

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

    in_chroot() {
        chroot "$ROOT_MNT" /usr/bin/env -i \
            PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
            HOME=/root LC_ALL=C.UTF-8 LANG=C.UTF-8 DEBIAN_FRONTEND=noninteractive \
            "$@"
    }

    if ! in_chroot bash -c "apt-get update -q && \
                            apt-get install -y -q --no-install-recommends $APT_PACKAGES"; then
        echo "hint: 'Exec format error' means aarch64 emulation is not set up — install qemu-user-static and binfmt-support" >&2
        die "apt step failed"
    fi

    # --- patched uvcvideo module (see uvcvideo/README.md) ------------------
    if [ "$DO_UVC" -eq 1 ]; then
        KVER=$(find "$ROOT_MNT/lib/modules" -mindepth 1 -maxdepth 1 -name '*-rpi-v8' -printf '%f\n' | sort -V | tail -1)
        [ -n "$KVER" ] || die "no *-rpi-v8 kernel in the image"
        KBRANCH="rpi-$(echo "$KVER" | cut -d. -f1,2).y"
        log "Fetching uvcvideo source from raspberrypi/linux $KBRANCH for kernel $KVER"
        git clone -q --depth 1 --filter=blob:none --sparse --branch "$KBRANCH" \
            https://github.com/raspberrypi/linux.git "$WORK/rpi-linux"
        git -C "$WORK/rpi-linux" sparse-checkout set --no-cone drivers/media/usb/uvc >/dev/null
        rm -rf "$ROOT_MNT/tmp/uvc-src"
        cp -a "$WORK/rpi-linux/drivers/media/usb/uvc" "$ROOT_MNT/tmp/uvc-src"
        rm -rf "$WORK/rpi-linux"
        python3 "$IMAGE_DIR/uvcvideo/patch-uvcvideo.py" "$ROOT_MNT/tmp/uvc-src"
        install -m 755 "$IMAGE_DIR/uvcvideo/build-uvcvideo.sh" "$ROOT_MNT/tmp/build-uvcvideo.sh"
        log "Building the patched uvcvideo inside the image (emulated; takes a few minutes)"
        in_chroot env MAX_PAYLOAD="$UVC_MAX_PAYLOAD" bash /tmp/build-uvcvideo.sh || die "uvcvideo build failed"
        rm -f "$ROOT_MNT/tmp/build-uvcvideo.sh"
    fi

    in_chroot bash -c "apt-get clean && rm -rf /var/lib/apt/lists/*"

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
echo "    units installed: $(ls "$IMAGE_DIR"/units | tr '\n' ' ')"
echo "    enabled by default: $ENABLE_UNITS (web console at http://<host>:8080)"
echo "    start recording on the collar with: sudo systemctl start ewego-sensors"
