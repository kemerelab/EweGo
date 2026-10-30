# hub_cam: project notes

These notes are for whoever picks this up next: why the board exists, how it
got here, and what to check before and after the first order. Parts, port map
and hub configuration are in [README.md](README.md).

## What it is and why

hub_cam is a 31.66 x 16.65 mm FE1.1S USB 2.0 hub carrier sized for the 3/4"
camera strap. It has a USB-C port to the CM4, two JST SH camera ports, and the
J1 header that the [hub_imu](../hub_imu/NOTES.md) module stacks onto. Holes in
the board let it be sewn to the strap.

It replaces the off-the-shelf FE1.1S hub (Amazon B0GTVPVPV9) used on the collar
so far, which has three problems:

- Its 2.0 mm port pitch doesn't match hub_imu's 2.54 mm header.
- The camera wires must be soldered to it, and their insulation melts.
- The host cable is soldered on. On 2026-10-06 the whole hub re-enumerated
  when the collar was moved, and a cold joint on that cable was the suspected
  cause. hub_cam uses a USB-C receptacle instead, with wire pads on the back
  as a fallback.

## History

The design was done in `github.com/ckemere/usb-imu`. That repository keeps the
full commit history. The files were copied here without history on
2026-10-30.

| Date | What happened |
|---|---|
| 2026-10-19 | Schematic draft. USBLC6-2SC6 ESD protection added at both camera connectors. |
| 2026-10-20 | PCB layout: USB-C host port J4 with 5.1k CC pull-downs, backup wire pads J5–J8, JST SH SM04B-SRSS-TB camera ports (replacing the JST GH planned earlier), six thread holes, 1.0 mm board. DRC: 0 errors, 0 unconnected. |

The old repository's top-level README still says "JST-GH". The board uses
**JST SH**, and that's what the cables must be crimped for (see README).

## Before ordering

- **Regenerate the fab outputs.** A `production/hub_cam.zip` exists in the
  old repository's working tree, but it was made at 18:06 on 2026-10-20,
  before the final PCB save at 19:10, so it was not copied. Rerun the
  Fabrication Toolkit plugin.
- J2–J4 and U2/U3 are JLCPCB Extended parts, each with an extended-part fee.
- J1 (the hub_imu header) is marked DNP and is fitted by hand.

## After the first boards arrive

- **Camera port names change.** The collar's `/etc/ewego/cam-camera*.conf`
  use `port:1.3` / `port:1.4`, which are the Amazon hub's ports. On hub_cam
  the cameras are on hub ports 1 and 3 and the IMU is on port 2, so expect
  `port:1.1` / `port:1.3`. Confirm with `ewego-cam --list` and update the
  configs.
- Repeat the bus test (`Firmware/usbimu/bus-test.py`: two cameras at 1080p30
  plus the IMU at 100 Hz, 0 lost), then a long run of the three units. The
  patched uvcvideo `max_payload=1900` still applies, since both cameras share
  one USB 2.0 bus however the hub is built.
- Wiggle-test the USB-C connection while watching
  `dmesg -wT | grep -iE 'disconnect|new .*-speed|over-current|undervolt'`.

## Design limits to keep in mind

- The hub is wired as self-powered with no over-current sensing, and nothing
  limits or switches camera power.
- There is no ESD protection on the host USB-C port. That decision dates from
  when the host cable was soldered on.
