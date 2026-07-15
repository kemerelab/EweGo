# EweGo setup & networking scripts

The scripts in this directory bring up a fresh Pi, configure USB-C SSH,
join the B.A.T.M.A.N. mesh, and deploy code from a laptop. They are designed
to be idempotent — re-running them is safe.

## Scripts at a glance

| Script | Side | What it does |
|---|---|---|
| `deploy.sh`        | laptop | Rsync firmware to a Pi, optionally run `pi_setup.sh` and reboot |
| `pi_setup.sh`      | Pi     | Base setup: packages, hostname, USB-C gadget, sensor overlays |
| `mesh_setup.sh`    | Pi     | Install B.A.T.M.A.N. mesh service (`install` / `disable`) |
| `mesh_join.sh`     | laptop | Join the mesh from a laptop with an IBSS-capable adapter |
| `ewego_usb.sh`     | laptop | Configure laptop IP for a USB-C-connected Pi and SSH in |
| `test_batman.sh`   | Pi     | Validate the mesh stack step by step (diagnostic) |
| `bugs/fix_sd_power_loss.sh` | Pi | Recover a Pi whose netplan got truncated by first-boot power loss |

## Quick start: bring up a new Pi

1. Flash Raspberry Pi OS (Bookworm/Trixie 64-bit). Set the hostname to
   `eweN` or `ewegoN` (N = device number, 1–254) in the imager.
2. Boot the Pi. Confirm it's reachable via mDNS:
   ```
   ssh user@eweN.local
   ```
3. From your laptop, deploy the firmware:
   ```
   bash Firmware/setup/deploy.sh user@eweN.local
   ```
   When prompted, answer `y` to run `pi_setup.sh`, then `y` again to reboot.

4. After reboot, plug a USB-C cable to the Pi. Verify USB-C SSH:
   ```
   bash Firmware/setup/ewego_usb.sh ssh N
   ```

5. Enable mesh networking (run on the Pi, over USB-C SSH):
   ```
   bash ~/EweGo/Firmware/setup/mesh_setup.sh install
   sudo reboot
   ```

After reboot the mesh service starts automatically — the Pi joins the mesh
on boot from then on.

## Connecting to a Pi over USB-C

Each Pi advertises its own /24 on `usb0` at `10.55.<N>.1/24`. The helper
script `ewego_usb.sh` manages the laptop side.

```
bash Firmware/setup/ewego_usb.sh list         # show plugged-in Pis + status
bash Firmware/setup/ewego_usb.sh up <N>       # assign laptop IP for Pi N
bash Firmware/setup/ewego_usb.sh ssh <N>      # configure + SSH (one shot)
bash Firmware/setup/ewego_usb.sh down <N>     # remove laptop IP
```

You can have multiple Pis plugged in simultaneously — each gets its own
subnet, so there's no routing ambiguity. The script accepts plain numbers
(`7`), `eweN` (`ewe7`), or `ewegoN` (`ewego007`) — all parse to device 7.

If your Pi user isn't `user`, override:
```
EWEGO_USER=william bash Firmware/setup/ewego_usb.sh ssh 7
```

## Mesh networking

The mesh is **B.A.T.M.A.N.-adv over IBSS** on the 2.4 GHz radio:

- SSID: `ewego-mesh`, channel 6, fixed BSSID `02:12:34:56:78:9A`
- IBSS join uses `NOHT` (BCM43455 driver rejects HT20 in IBSS mode)
- Each Pi gets `bat0 = 10.42.0.<N>/24`
- Brought up by `ewego-mesh.service` (systemd, auto-starts at boot)

### Install / disable on a Pi

```
bash mesh_setup.sh install      # idempotent — safe to re-run
bash mesh_setup.sh disable      # stops service, returns wlan0 to NetworkManager
```

`install` refuses to run if the hostname doesn't match `eweN` or `ewegoN`
(it derives the mesh IP from the trailing digits).

### Verify the mesh is up

On any Pi, after boot:
```
systemctl is-active ewego-mesh        # should print "active"
ip -br addr show bat0                  # bat0 should have 10.42.0.<N>/24
sudo batctl meshif bat0 n              # neighbours seen on the mesh
sudo batctl meshif bat0 o              # full originator table
ping 10.42.0.<other-device>            # cross-Pi reachability
```

A second Pi running the same firmware appears as a neighbour within ~5 s
of booting. No manual peering needed.

### Joining the mesh from a laptop

`mesh_join.sh` puts a laptop's WiFi adapter into IBSS mode and onto the
mesh. Requires an IBSS-capable adapter — most modern Intel `iwlwifi` cards
do **not** support IBSS. Test with `iw phy | grep -A20 "Supported interface modes"`.

```
bash mesh_join.sh scan                # list nearby IBSS cells (read-only)
bash mesh_join.sh join 100            # join as 10.42.0.100
bash mesh_join.sh status              # show neighbours, bat0, IBSS state
bash mesh_join.sh leave               # tear down, hand wlan0 back to NM
```

A USB WiFi adapter based on Atheros AR9271 (e.g. TP-Link TL-WN722N v1) is
a reliable workaround for laptops without IBSS-capable internal radios.

## Running tests

### Mesh stack diagnostic (Pi side)

`test_batman.sh` validates each step of the IBSS + batman-adv chain
without permanently changing the system. Run it when the mesh service
fails to come up.

```
bash test_batman.sh                   # run all checks
bash test_batman.sh --teardown        # clean up after a failed run
```

### Sensor orchestrator

Once `pi_setup.sh` has installed the sensors and `uv` venv:

```
cd ~/EweGo
uv run python Firmware/record_sensors.py
```

This launches all sensors (dual cameras, IMU, GPS, audio, fuel gauge)
as subprocesses and writes timestamped recordings into `sensor_test_*/`.

## Common workflows

### Deploy a code change to one Pi
```
bash deploy.sh user@ewe7.local        # answer 'n' to setup re-run unless needed
```

### Deploy to a Pi already on the mesh from your laptop
If your laptop is on the mesh via `mesh_join.sh`, point `deploy.sh` at the
mesh IP directly:
```
bash deploy.sh user@10.42.0.7
```

### Bring up a Pi with mismatched hostname
If a Pi was flashed with the wrong hostname (e.g. two Pis both called
`ewego007`), rename in place — `pi_setup.sh` will fix cloud-init's
`preserve_hostname` flag so the rename sticks across reboots:
```
sudo hostnamectl set-hostname ewe8
bash ~/EweGo/Firmware/setup/pi_setup.sh
sudo reboot
```

### Recover a Pi that lost network after first-boot power loss
```
bash Firmware/bugs/fix_sd_power_loss.sh
```

## Reference: address scheme

| Path | Subnet | Pi address | Laptop address |
|---|---|---|---|
| USB-C (per device)  | `10.55.<N>.0/24` | `10.55.<N>.1`  | `10.55.<N>.100` |
| Mesh (BATMAN/IBSS)  | `10.42.0.0/24`    | `10.42.0.<N>` | `10.42.0.100` (when joined) |

`<N>` is the device number, parsed from the hostname's trailing digits
(`ewe7`, `ewe007`, `ewego7`, `ewego007` all parse to 7).
