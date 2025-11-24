## Overview

This project is a headless **Raspberry Pi 4 + RTL-SDR RF “tripwire”**. It runs a lightweight watcher that uses `rtl_power` sweeps and FFT-based energy detection to monitor several VHF/UHF/ADS-B bands, estimate the local noise floor, and speak short text-to-speech alerts whenever new activity appears.

Unlike a full scanner, this script does **not** camp on channels or decode audio by default. It’s designed as a quick **HUD-style situational-awareness tool** you can clip to a belt or sling bag and forget about until it calls out activity.

## Relationship to the B200 RF-Detection System

This repo is a companion to the Ettus B200-based RF detection rig:

```text
https://github.com/corbinneville1/RF-Detection-System


