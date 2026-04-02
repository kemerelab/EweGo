# EweGo

This project provides a comprehensive, Raspberry Pi-based camera-embedded platform for collecting, storing, transmitting, and visualizing environmental data from various sensors.

## Key Features

To enable camera access, make the following edits to `/boot/firmware/config.txt`:

If using a third-party camera, disable automatic camera detection by setting:
```
camera_auto_detect=0
```

Manually add the correct overlay to your config file:
```
dtoverlay=<sensor>
```

*The correct sensor code for your camera can be found at: https://www.raspberrypi.com/documentation/computers/camera_software.html#configuration*

## Installation

### Prerequisites

A Raspberry Pi (this project specifically uses a Raspberry Pi Compute Module 4 with compatible IO Board).

## .env

## Usage

## Troubleshooting
