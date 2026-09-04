#!/bin/bash
#
# vendor-pylib.sh — download the pure-Python packages that Debian does not
# ship (requirements-vendored.txt) and unpack them into a directory that the
# image injector copies to /opt/ewego/pylib. The units put that directory on
# PYTHONPATH, so nothing has to be pip-installed on the collar.
#
#   ./vendor-pylib.sh [OUT_DIR]        default: <repo>/build/pylib
#
# Fails if any resolved wheel is not a py3-none-any wheel, because anything
# with compiled code would have to be built for arm64 instead.

set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

IMAGE_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT=${1:-"$IMAGE_DIR/../build/pylib"}
REQ="$IMAGE_DIR/requirements-vendored.txt"
[ -f "$REQ" ] || die "$REQ not found"
command -v unzip >/dev/null || die "unzip is required"
python3 -m pip --version >/dev/null 2>&1 || die "python3 -m pip is required"

WHEELS=$(mktemp -d)
trap 'rm -rf "$WHEELS"' EXIT

# --python-version 3.11 is the oldest interpreter on a supported Pi OS
# (Bookworm); Trixie has 3.13. Pure wheels satisfy both.
python3 -m pip download --quiet --only-binary=:all: --python-version 3.11 \
    --dest "$WHEELS" -r "$REQ"

shopt -s nullglob
wheels=("$WHEELS"/*.whl)
[ ${#wheels[@]} -gt 0 ] || die "no wheels downloaded"
for w in "${wheels[@]}"; do
    case "$w" in
        *-none-any.whl) ;;
        *) die "$(basename "$w") is not a pure-Python wheel; it cannot be vendored" ;;
    esac
done

rm -rf "$OUT"
mkdir -p "$OUT"
for w in "${wheels[@]}"; do
    unzip -q -o "$w" -d "$OUT"
done

echo "vendored into $OUT:"
for w in "${wheels[@]}"; do echo "  $(basename "$w")"; done
