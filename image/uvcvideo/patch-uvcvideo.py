#!/usr/bin/env python3
"""
patch-uvcvideo.py <uvc-source-dir>

Adds a 'max_payload' module parameter to the Linux uvcvideo driver: a cap,
in bytes per USB microframe, on the isochronous bandwidth a camera may
reserve. 0 (default) leaves the driver unchanged.

Why: USB 2.0 isochronous bandwidth is reserved per stream from a bus budget
of about 6000 bytes per 125 us microframe. Some MJPEG cameras request their
maximum alternate setting (3060-3072 B) at every resolution and frame rate,
so two of them can never stream at once on one bus, which is all the CM4
has. The in-kernel UVC_QUIRK_FIX_BANDWIDTH only rewrites the request for
uncompressed formats. Capping the request to 2048 B (16 MB/s per camera,
several times what 1080p30 MJPEG needs) lets two cameras share the bus.

The edit is anchored on exact source lines and refuses to run if any anchor
is missing, so a kernel that has changed shows up as a build failure rather
than a silently unpatched module.
"""

import sys
from pathlib import Path

CLAMP = '''\t\tbandwidth = stream->ctrl.dwMaxPayloadTransferSize;

\t\tif (uvc_max_payload_param && bandwidth > uvc_max_payload_param) {
\t\t\tuvc_dbg(stream->dev, VIDEO,
\t\t\t\t"EweGo: capping requested bandwidth %u to %u B/frame\\n",
\t\t\t\tbandwidth, uvc_max_payload_param);
\t\t\tbandwidth = uvc_max_payload_param;
\t\t}
'''

EDITS = [
    ("uvc_driver.c",
     "unsigned int uvc_hw_timestamps_param;\n",
     "unsigned int uvc_hw_timestamps_param;\nunsigned int uvc_max_payload_param;\n"),
    ("uvc_driver.c",
     'MODULE_PARM_DESC(hwtimestamps, "Use hardware timestamps");\n',
     'MODULE_PARM_DESC(hwtimestamps, "Use hardware timestamps");\n'
     "module_param_named(max_payload, uvc_max_payload_param, uint, 0644);\n"
     'MODULE_PARM_DESC(max_payload, "EweGo: cap the isochronous payload a camera may reserve '
     '(bytes per microframe, 0 = no cap)");\n'),
    ("uvcvideo.h",
     "extern unsigned int uvc_hw_timestamps_param;\n",
     "extern unsigned int uvc_hw_timestamps_param;\nextern unsigned int uvc_max_payload_param;\n"),
    ("uvc_video.c",
     "\t\tbandwidth = stream->ctrl.dwMaxPayloadTransferSize;\n",
     CLAMP),
]


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    src = Path(sys.argv[1])
    for fname, anchor, replacement in EDITS:
        p = src / fname
        text = p.read_text()
        if replacement.split("\n", 1)[1] and replacement.split("\n", 1)[1].splitlines()[0] in text \
                and "uvc_max_payload_param" in text and anchor in text and replacement in text:
            print(f"{fname}: already patched")
            continue
        n = text.count(anchor)
        if n != 1:
            sys.exit(f"error: anchor found {n} times in {fname}, expected exactly once:\n{anchor!r}")
        p.write_text(text.replace(anchor, replacement))
        print(f"{fname}: patched")
    print("uvcvideo: max_payload parameter added")


if __name__ == "__main__":
    main()
