# Patched uvcvideo: `max_payload`

## The problem

USB 2.0 isochronous bandwidth is reserved per stream from a bus budget of
about 6000 bytes per 125 µs microframe. When a UVC camera starts streaming
it asks for a payload size (`dwMaxPayloadTransferSize`) and the kernel
picks the smallest alternate setting that satisfies it. The candidate
collar camera asks for **3060 B at every resolution and frame rate**, so a
second identical camera fails with:

    usb 1-1.4: Not enough bandwidth for altsetting 11

The CM4 has one USB 2.0 bus. The stock remedy, `uvcvideo quirks=128`
(`UVC_QUIRK_FIX_BANDWIDTH`), rewrites the request only for **uncompressed**
formats in the Pi 6.12 kernel, so it does nothing for MJPEG.

## The patch

`patch-uvcvideo.py` adds one module parameter to the in-tree driver:

    options uvcvideo max_payload=1900

If a camera asks for more than `max_payload` bytes per microframe, the
request is capped before the alternate setting is chosen (the camera's
own request is logged with `trace=1024`). The driver then picks the
smallest alternate setting **at or above** the capped value, so the cap
must sit at or below the alternate setting you want. The collar camera's
table is 800, 944, 1280, 1600, 1984, 2880, 3060 (alt 5–11); 1900 lands on
alt 9 = 1984 B/µframe, 15.9 MB/s per camera, several times what 1080p30
MJPEG produces (3–5 MB/s measured). Two cameras at 1984 total 3968, well
inside the ~5000 that clears the controller's overhead accounting. 2048
was tried first and rounded up to 2880, which two cameras cannot share.
`0` disables the cap. The web console's Bandwidth probe prints a camera's
table and can set the value at runtime.

**Bench result 2026-09-09:** two cameras at 1920×1080 30 fps, 6 s, 0 lost
frames on either, with max_payload=1900.

The edit is anchored on exact source lines; if the kernel changes, the
build fails loudly instead of producing an unpatched module.

The patch also raises `UVC_URBS` from 5 to 32. With 32 packets per URB
that is ~128 ms of USB-side buffering per camera instead of ~20 ms; the
URBs are resubmitted from a work item that SD-card writeback on the CM4
can delay, which showed up as 5–9 frames lost on both cameras at every
fdatasync of the recorder. Cost: about 2 MB of DMA memory per streaming
camera.

## How it is built

`inject-ewego.sh` does this inside the image's chroot, after the apt step:

1. reads the image's kernel version from `/lib/modules/*-rpi-v8`;
2. sparse-clones `drivers/media/usb/uvc` from `raspberrypi/linux` at the
   matching `rpi-6.NN.y` branch and applies the patch;
3. `build-uvcvideo.sh` installs `linux-headers-<exact version>` and
   `build-essential`, builds the module out of tree, installs it to
   `/lib/modules/<ver>/updates/uvcvideo.ko` (which depmod prefers over the
   stock module), writes `/etc/modprobe.d/ewego-uvc.conf`, then purges
   the build tools again.

If the apt mirror no longer has headers for the image's exact kernel, the
kernel and headers are upgraded together so they match.

Skip it with `inject-ewego.sh --no-uvc`. Change the cap with
`UVC_MAX_PAYLOAD=… inject-ewego.sh …`, or on a collar by editing the
modprobe file and reloading the module.

## Checking it on a collar

    modinfo uvcvideo | grep -E 'filename|max_payload'   # filename should be under updates/
    cat /sys/module/uvcvideo/parameters/max_payload     # 2048
    # web console -> Camera -> Bandwidth probe: kernel log shows
    #   "EweGo: capping requested bandwidth 3060 to 2048 B/frame"
    #   "Selecting alternate setting N (2048 B/frame bandwidth)"
    # then the drop test with both cameras selected.

## Risks

- The camera is told by COMMIT that it may use 3060 B per packet but then
  gets an endpoint that carries 2048. Most firmware fills packets to the
  endpoint size and streams fine; some may misbehave. The drop test shows
  which: frames arrive with zero gaps, or they do not.
- The module is tied to the kernel version in the image. `apt upgrade` of
  the kernel on a collar would leave the patched module behind (the stock
  one for the new kernel loads instead, and the cap silently disappears).
  Collars are not expected to run apt in the field.
