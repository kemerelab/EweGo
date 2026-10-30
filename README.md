# EweGo

**Extensible Wearable for Egocentric and Group Observation**

EweGo is an open sensor collar that records the sensorium of each animal in a
group. It captures synchronized vision, audio, motion and RTK position, so we
can study collective behavior such as flocking, social learning and responses
to stress, with the aim of bringing it into neuroscience experiments. It was
built for sheep and is meant to be adapted to other species.

Each collar is a Raspberry Pi CM4 on the **eweSAW** carrier board, with two
USB cameras, a u-blox ZED-X20P GNSS receiver, a BNO055 IMU, stereo
microphones and a battery fuel gauge. Every sample gets a precise timestamp.

| Folder | Contents |
|---|---|
| `Hardware/` | eweSAW carrier, camera hub and EweFeel IMU boards (KiCad), case designs ([details](Hardware/README.md)) |
| `Firmware/` | sensor loggers and test tools |
| `image/` | CI-built, ready-to-flash collar image ([details](image/README.md)) |

## Flashing a collar

In Raspberry Pi Imager, open *Options → Content Repository* and enter
`https://github.com/kemerelab/EweGo/releases/latest/download/imager.json`.
Pick the EweGo image, set the hostname, user and Wi-Fi, and flash.
