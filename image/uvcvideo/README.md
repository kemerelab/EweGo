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

    options uvcvideo max_payload=2048

If a camera asks for more than `max_payload` bytes per microframe, the
request is capped before the alternate setting is chosen (the camera's
own request is logged with `trace=1024`). 2048 B per microframe is 16 MB/s
per camera, several times what 1080p30 MJPEG produces, and two cameras at
2048 fit the bus with room to spare. `0` disables the cap.

The edit is anchored on exact source lines; if the kernel changes, the
build fails loudly instead of producing an unpatched module.

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
