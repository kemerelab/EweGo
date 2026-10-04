#!/bin/bash
# Fetch the firmware's third-party sources at pinned versions into lib/.
# They are not stored in this repository; CI runs this before building, and
# so does anyone building locally. Nothing is installed anywhere.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p lib

fetch() {   # fetch <dir> <url> <tag>
    if [ -d "lib/$1/.git" ] && [ "$(git -C "lib/$1" describe --tags --exact-match 2>/dev/null)" = "$3" ]; then
        echo "lib/$1 already at $3"
        return
    fi
    rm -rf "lib/$1"
    git clone -q --depth 1 --branch "$3" "$2" "lib/$1"
    echo "lib/$1 <- $2 @ $3"
}

fetch tinyusb         https://github.com/hathach/tinyusb.git                  0.21.0
fetch cmsis_device_f0 https://github.com/STMicroelectronics/cmsis_device_f0.git v2.3.8
fetch cmsis_core      https://github.com/STMicroelectronics/cmsis_core.git     v5.9.0
