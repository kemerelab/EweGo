# Bug 001: SD Card "Bricked" After Battery Power Loss

## Symptoms
- Pi boots but never connects to wifi or wired network
- No activity lights after initial boot sequence
- SD card works fine when mounted on a desktop (filesystem intact)
- Same SD card fails on multiple Pis
- A different SD card with the same OS image works immediately

## Root Cause
When the battery dies during operation, the Pi loses power mid-write. This can
truncate files that were being written from the page cache to the SD card.

In this case, **NetworkManager's netplan config files** were corrupted to 0 bytes:
```
/etc/netplan/90-NM-*.yaml  →  0 bytes (empty)
```

The failure chain:
1. NetworkManager periodically writes/updates its netplan config files
2. Battery dies → power cut mid-write → files allocated but content never flushed
3. On next boot, netplan finds the 0-byte files
4. These empty files **override** the original cloud-init `network-config` from
   `/boot/firmware/network-config`
5. Result: no network interfaces are configured, wifi never comes up
6. cloud-init won't regenerate the config because it already ran on first boot
   (it's a once-per-instance operation)

## Diagnosis
Found by mounting the SD card on a desktop and checking:
```bash
ls -la /media/<user>/rootfs/etc/netplan/
# Shows 0-byte .yaml files with timestamps matching the power loss event

cat /media/<user>/rootfs/var/log/cloud-init-output.log
# Shows the Pi IS booting — but eth0 and wlan0 are both DOWN
```

## Fix
Run the repair script (requires sudo, SD card mounted):
```bash
sudo bash Firmware/bugs/fix_sd_power_loss.sh
```

This writes a fresh netplan config with the wifi credentials from the boot
partition's `network-config` file.

## Long-Term Prevention

### Option 1: Low-battery safe shutdown (Recommended)
Use the MAX17048 fuel gauge to trigger `shutdown -h now` when SOC drops below
10%. The battery has plenty of charge at 10% to complete a clean shutdown (~2s).
This is the simplest fix with zero performance impact.

Could be implemented as:
- A check in `record_sensors.py`'s fuel gauge monitoring loop
- Or a standalone systemd service that polls the fuel gauge

### Option 2: Read-only root overlay
Mount rootfs as read-only with a tmpfs overlay. All writes go to RAM and the SD
card is never modified during operation. Raspberry Pi OS supports this via
`raspi-config > Performance > Overlay FS`. Requires a separate writable data
partition for recordings/logs.

### Option 3: Sync mount (Not recommended)
Adding `sync` to fstab forces every write to flush immediately. This would hurt
recording performance (more timestamp stutters, potential frame drops) since the
dual H.264 streams + sensor logs generate continuous I/O.

## Files
- `fix_sd_power_loss.sh` — Repair script for this specific bug
- Cloud-init network source: `/boot/firmware/network-config` (on boot partition)
- Corrupted files: `/etc/netplan/90-NM-*.yaml` (on rootfs)

## Date
First observed: 2026-02-15
