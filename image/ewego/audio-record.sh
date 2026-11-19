#!/bin/sh
# ewego-audio-record: the ewego-audio unit's recorder (installed by
# image/inject-ewego.sh as /usr/local/bin/ewego-audio-record).
#
#   ewego-audio-record OUT_DIR [ALSA_DEVICE]   record OUT_DIR/<YYYYmmdd_HHMMSS>/audio.wav
#   ewego-audio-record --print-device          show the device it would use
#
# arecord is exec'd, so it is the unit's main process and gets the unit's
# SIGINT itself: it finishes the WAV header on the way out (a Python
# subprocess wrapper used to SIGKILL it 250 ms after SIGINT). Format: 48 kHz,
# stereo, S32_LE. At the 2 GiB WAV limit (~93 min) arecord renames audio.wav
# to audio-01.wav and continues in audio-02.wav, audio-03.wav, ...
#
# With no ALSA_DEVICE the voicehat card (the stereo mics) is found by name in
# /proc/asound/cards, as the web console does, and used as
# plughw:CARD=<id>,DEV=0, since ALSA "default" is not guaranteed to be that
# card. If no voicehat card is listed it falls back to "default" with a
# warning.

CARDS=${EWEGO_ASOUND_CARDS:-/proc/asound/cards}

voicehat_device() {
    # /proc/asound/cards: " 0 [sndrpigooglevoi]: RPi-simple - snd_rpi_googlevoicehat_soundcar"
    # followed by an indented description line. Print the bracketed id of the
    # first card whose lines mention voicehat.
    [ -r "$CARDS" ] || return 1
    id=$(awk '
        /^ *[0-9]+ \[/ { id = $0; sub(/^ *[0-9]+ \[/, "", id); sub(/\].*$/, "", id); gsub(/ /, "", id) }
        tolower($0) ~ /voicehat/ && id != "" { print id; exit }
    ' "$CARDS")
    [ -n "$id" ] || return 1
    echo "plughw:CARD=$id,DEV=0"
}

resolve_device() {
    if [ -n "${1:-}" ]; then
        echo "$1"
    elif ! voicehat_device; then
        echo "ewego-audio-record: no voicehat card in $CARDS; using ALSA default" >&2
        echo default
    fi
}

if [ "${1:-}" = "--print-device" ]; then
    resolve_device "${2:-}"
    exit 0
fi

out=${1:-}
if [ -z "$out" ]; then
    echo "usage: ewego-audio-record OUT_DIR [ALSA_DEVICE] | --print-device" >&2
    exit 2
fi
dev=$(resolve_device "${2:-}")
d="$out/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$d" || exit 1
echo "ewego-audio-record: $d/audio.wav from $dev (48 kHz, 2 ch, S32_LE)"
exec /usr/bin/arecord -q -D "$dev" -f S32_LE -c 2 -r 48000 "$d/audio.wav"
