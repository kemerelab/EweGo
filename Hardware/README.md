# EweGo hardware

| Folder | What it is | Licence |
|---|---|---|
| [`eweSAW/`](eweSAW/) | CM4 carrier board: power, fuel gauge, IMU, microphones, GNSS header (git submodule, `ckemere/ucm4`, branch `uSAW`) | CC BY 4.0 |
| [`hub_cam/`](hub_cam/) | FE1.1S USB hub carrier for the two cameras and the hub_imu module; laid out, not yet fabricated | CERN-OHL-P-2.0 |
| [`hub_imu/`](hub_imu/) | "EweFeel" USB IMU module (STM32F042 + BNO055) that stacks on the hub, with its firmware in `firmware/` | CERN-OHL-P-2.0 |
| `Case/` | SolidWorks case and camera mounts, with STL exports | |
| `Docs/` | Datasheets for the parts on the collar | vendors' |

Each hub board has a `README.md` (circuit and parts) and a `NOTES.md`
(history, status, gotchas).
