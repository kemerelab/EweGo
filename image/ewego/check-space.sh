#!/bin/sh
# ewego-check-space: free-space guard for the recorder units (installed by
# image/inject-ewego.sh as /usr/local/bin/ewego-check-space).
#
#   ewego-check-space DIR [MIN_MB]
#
# Prints the free space (MB) on the filesystem holding DIR (or its nearest
# existing parent: the recorders create their output directories themselves)
# and exits 0, or exits 75 (EX_TEMPFAIL) when less than MIN_MB (default 500,
# or $EWEGO_MIN_FREE_MB) is free. Either way it writes the result to
# $EWEGO_RUN_DIR/space.json (default /run/ewego/space.json) for the console.
#
# Used as ExecCondition= in the recorder units: a nonzero exit there skips
# the start without marking the unit failed and without a restart loop (an
# ExecStartPre= failure is restarted regardless of RestartPreventExitStatus=).
# When it cannot read the free space it says so and exits 0: a broken check
# must not stop a recording.

dir=${1:-/opt/ewego/recordings}
min=${2:-${EWEGO_MIN_FREE_MB:-500}}
run_dir=${EWEGO_RUN_DIR:-/run/ewego}

case $min in
    ''|*[!0-9]*) echo "ewego-check-space: bad minimum '$min', using 500" >&2; min=500 ;;
esac

d=$dir
while [ ! -d "$d" ]; do
    case $d in
        */*) d=${d%/*}; [ -n "$d" ] || d=/ ;;
        *) d=. ;;
    esac
done

free=$(df -Pk -- "$d" 2>/dev/null | awk 'NR == 2 { printf "%d\n", $4 / 1024 }')
case $free in
    ''|*[!0-9]*)
        echo "ewego-check-space: cannot read the free space on $d; not blocking" >&2
        exit 0 ;;
esac

ok=true
[ "$free" -lt "$min" ] && ok=false

# space.json, written atomically (several units run the check at once)
esc=$(printf '%s' "$dir" | sed 's/\\/\\\\/g; s/"/\\"/g')
if mkdir -p "$run_dir" 2>/dev/null; then
    tmp="$run_dir/.space.json.$$"
    if printf '{"dir": "%s", "free_mb": %s, "min_mb": %s, "ok": %s, "checked": "%s"}\n' \
            "$esc" "$free" "$min" "$ok" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$tmp" 2>/dev/null; then
        mv -f "$tmp" "$run_dir/space.json" 2>/dev/null || rm -f "$tmp"
    fi
fi

if [ "$ok" = false ]; then
    echo "ewego-check-space: only $free MB free on $d (minimum $min MB); not starting" >&2
    exit 75
fi
echo "$free"
exit 0
