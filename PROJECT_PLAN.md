# Laparoscopic Camera Trainer

## Overview
A Raspberry Pi-based video passthrough system for laparoscopic surgery training using an Endoskill USB camera. The Pi captures the camera feed and displays it on an HDMI monitor, controlled by physical buttons.

## Hardware

### Confirmed Components
| Component | Details |
|-----------|---------|
| Computer | Raspberry Pi 3 B+ (1GB RAM, full-size HDMI, USB 2.0) |
| Camera | Endoskill USB (VID: 1BCF, PID: 0B09, Sunplus chipset) |
| OS | Raspberry Pi OS Lite (headless, no desktop) |
| Display | HDMI monitor (full-size HDMI cable, no adapter needed) |
| Buttons | 2x momentary push buttons on GPIO |

### Camera Specs
| Property | Value |
|----------|-------|
| Protocol | UVC (USB Video Class) — Linux-compatible out of the box |
| Codec | MJPEG |
| Resolution | 1920x1080 |
| Framerate | 20-22 fps |
| Alt codec | YUY2 (uncompressed, only usable at 640x480) |
| USB bandwidth | ~5-15 MB/s (MJPEG), fits within USB 2.0 |

### Phase 2 — Lights (deferred)
- LED lights inside training box, 5V, powered via USB
- Control via GPIO + N-channel logic-level MOSFET (on/off only)
- Current draw TBD — determines if powered from Pi 5V rail or separate supply
- Manual remote used until this is implemented

## Software Architecture

### Video Pipeline
- **GStreamer** — chosen for minimum latency (1-2 frames)
- Direct V4L2 source → MJPEG decode → KMS/DRM display (no window manager)
- Controlled from Python via GObject Introspection (`gi`)

### State Machine
```
        [BOOT]
          |
          v
   +------------------+
   |  WELCOME SCREEN   |<--- Button B (stop feed)
   |                   |<--- Inactivity timeout
   |  > Resolution     |
   |    Timeout        |
   |    Image flip     |
   |    Brightness     |
   |    Shutdown       |
   |                   |
   |  A=nav  B=start   |
   +--------+----------+
          |
        Button B
          |
          v
   +------------------+
   |   LIVE FEED       |
   |                   |
   |  Camera -> HDMI   |
   +------------------+
```

### Button Mapping
| Button | Action | Welcome Screen | During Feed |
|--------|--------|---------------|-------------|
| A short (~<500ms) | Navigate | Move cursor down | — |
| A long (~>500ms) | Select/Change | Cycle value on selected option | — |
| B press | Toggle feed | Start feed | Stop feed, return to welcome |

### Menu Options
| Option | Values | Default |
|--------|--------|---------|
| Resolution | 1080p, 720p | 1080p |
| Timeout | 2min, 5min, 10min, Off | 5min |
| Image flip | None, Horizontal, Vertical, Both | None |
| Brightness | Auto + manual range (V4L2) | Auto |
| Shutdown | Confirm required (second long-press) | — |

### Settings Persistence
- Settings saved to a config file (JSON or INI)
- Survives reboots
- Loaded on boot, applied before welcome screen displays

### Inactivity Detection
- GStreamer tee splits pipeline — one branch to display, one to sampler
- Sample one frame every ~2 seconds, downscale to 160x120
- Simple pixel difference score between consecutive samples
- If below threshold for configured timeout period -> return to welcome screen
- Warning overlay before timeout triggers (e.g., "Returning to standby in 30s")

### Auto-Start
- systemd service, starts after display subsystem is ready
- Launches Python control script
- Auto-restarts on crash

## Development Workflow
- Code written on Windows dev machine
- Git repo for version control
- SSH into Pi for deployment and testing
- Push to git, pull on Pi (or rsync via SSH)

## Boot Sequence
1. Pi powers on
2. Pi OS Lite boots (~10-15 seconds)
3. systemd starts the laparoscopic trainer service
4. Welcome screen displayed on HDMI
5. Waiting for Button B press

## Project Phases

### Phase 1 — Core (current)
1. Flash Pi OS Lite, configure SSH, connect to network
2. Install GStreamer and dependencies
3. Verify camera works on Pi (v4l2-ctl + test pipeline)
4. Build video passthrough pipeline
5. Add button controls (2 buttons, GPIO)
6. Build welcome screen with menu
7. Implement state machine (welcome <-> feed)
8. Settings persistence
9. Inactivity detection
10. systemd auto-start service

### Phase 2 — Enhancements
- Light control via GPIO + MOSFET
- Recording training sessions
- Screenshots
- Overlays (timer, timestamp)
- Network streaming

## GPIO Pin Assignments (TBD)
- Button A: TBD
- Button B: TBD
- Lights MOSFET: TBD (Phase 2)

## Parts Still Needed
- Momentary push buttons x2
- Jumper wires (4x — 2 per button to GPIO + GND)
- Logic-level N-channel MOSFET (Phase 2, e.g., IRLZ44N)
- 10k ohm resistor (Phase 2, MOSFET gate pull-down)
