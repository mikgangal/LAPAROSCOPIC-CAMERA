# Laparoscopic Camera Trainer — Complete Documentation

## Project Overview

A dedicated Raspberry Pi-based video passthrough system for laparoscopic surgery training. The system captures video from an Endoskill USB camera and displays it on an HDMI monitor with minimal latency. A single button on the camera cable controls all settings via an on-screen display (OSD). The system auto-pauses on inactivity and can shut down cleanly via a hardware power button.

Built in April 2026 on a Raspberry Pi 4B running Raspberry Pi OS Lite (Bookworm, headless).

---

## Hardware

### Components Used

| Component | Details |
|-----------|---------|
| Raspberry Pi 4B | 4-core Cortex-A72, used for its USB 3.0 and better CPU vs Pi 3 B+ |
| Endoskill USB Camera | VID: 1BCF, PID: 0B09, Sunplus Innovation chipset |
| HDMI Monitor | Connected to HDMI-A-2 (second micro-HDMI port), forced to 1080p |
| Micro-HDMI to HDMI cable | Required for Pi 4 (Pi 3 B+ has full-size HDMI) |
| USB-C power supply | 5V 3A laptop-grade supply |
| Activity LED (optional) | GPIO 22 (pin 15) with 100 ohm resistor |
| Power button (optional) | GPIO 3 (pin 5) to GND (pin 6), momentary switch |

### Why Pi 4 Instead of Pi 3 B+

We initially planned for the Pi 3 B+ (simpler HDMI, no adapter needed). Testing revealed:
- Pi 3 B+ at 1080p: 11.5 fps (CPU bottleneck in JPEG decode + color conversion)
- Pi 4 at 1080p: 17-22 fps depending on pipeline configuration
- The Pi 4's Cortex-A72 cores are roughly 3-4x faster per-core than the A53

The SD card, OS, and software work identically on both — just swap the card.

### Endoskill Camera Specifications

Discovered through testing on both Windows (AMCap) and Linux (v4l2-ctl):

| Property | Value |
|----------|-------|
| Protocol | UVC (USB Video Class), Linux-compatible out of the box |
| Codec | MJPEG (compressed) and YUY2 (uncompressed) |
| Best mode | MJPEG 1920x1080 at 20-22 fps |
| YUY2 at 1080p | Only 5 fps (too much USB bandwidth) |
| YUY2 at 640x480 | 23 fps (usable but low resolution) |
| USB bandwidth | ~5-15 MB/s in MJPEG mode, fits USB 2.0 easily |

**Important discovery**: OpenCV's DirectShow backend on Windows could NOT negotiate MJPEG — it always fell back to YUY2, showing only 5 fps at 1080p. AMCap and GStreamer/V4L2 on Linux handle MJPEG correctly. This caused initial confusion about the camera's capabilities.

### Camera V4L2 Controls

The camera exposes these adjustable parameters (discovered via `v4l2-ctl --list-ctrls-menus`):

| Control | Range | Default | Used in OSD |
|---------|-------|---------|-------------|
| brightness | -64 to 64 | 13 | Yes |
| contrast | 0 to 95 | 5 | Yes |
| saturation | 0 to 100 | 60 | Yes |
| gain | 1 to 8 | 1 | Yes |
| sharpness | 1 to 7 | 3 | Yes |
| auto_exposure | 1 (manual) or 3 (auto) | 3 | Yes |
| exposure_time_absolute | 10 to 626 | 156 | Yes (mapped to %) |
| power_line_frequency | 0 (off), 1 (50Hz), 2 (60Hz) | 1 | Yes |
| white_balance_automatic | 0/1 | 1 | No |
| gamma | 64 to 300 | 86 | No |
| hue | -2000 to 2000 | 0 | No |

### Camera Button

The camera has a physical button on an inline PCB (marked "Ry-USB sig3 20240221"). This button was reverse-engineered for use as the primary control:

- **USB protocol**: sends interrupt packet `02 01 00 01` on endpoint 0x87 (VideoControl interface)
- **Detection method**: passive monitoring via `/sys/kernel/debug/usb/usbmon/1u`
- **Limitation**: only sends events while the camera is actively streaming
- **Limitation**: sends exactly 1 event per press (no hold/release detection)
- **Solution**: single click vs double click detection with 500ms window

The button was initially invisible to Linux because the UVC driver doesn't create an input device for it. We discovered it by reading raw USB interrupt packets via `pyusb`, then found that `usbmon` can passively sniff the same packets without interfering with the video stream.

**What didn't work for the button**:
- `/dev/input/event*` — no input device created by uvcvideo driver
- `v4l2-ctl --wait-for-event` — no supported event type for buttons
- `uvcvideo` module `quirks` parameter — didn't create input device
- Detaching USB interface 0 — kills the video feed
- Long press detection — button sends only 1 event regardless of hold duration

### USB Power Management

- USB ports on Pi 4 stay powered even after `shutdown -h` (VL805 controller behavior)
- `uhubctl` is used to cut USB power on shutdown: `uhubctl -l 1-1 -a off`
- This is configured in `lapcam.service` as `ExecStop`
- USB autosuspend is disabled (`usbcore.autosuspend=-1` in cmdline.txt) to prevent camera disconnects
- The camera's USB cable is fragile — had to be resoldered. USB disconnects manifest as `No such device` errors in GStreamer

### GPIO Pin Assignments

| Pin | GPIO | Function | Notes |
|-----|------|----------|-------|
| 5 | GPIO 3 | Power button (shutdown/wake) | via `gpio-shutdown` overlay, hardwired wake |
| 6 | GND | Power button ground | |
| 15 | GPIO 22 | Activity LED | via `gpio-led` overlay, mirrors SD card (mmc0) |
| 14 | GND | LED ground | |

GPIO 3 is special — it's the only pin that can wake the Pi from halt state (hardwired in the BCM2711 bootloader).

---

## Software Architecture

### State Machine

```
[OFF] ──power btn──> [BOOT] ──auto──> [FEED]
  ^                                      |
  |                              inactivity timeout
  |                                      |
  |                                      v
  └──── pause timer ────────── [PAUSE]
                                  |
                            camera btn → [FEED]
```

### File Structure

| File | Purpose |
|------|---------|
| `lapcam.py` | Main application (~1100 lines) — state machine, GStreamer pipeline, USB button monitor, OSD, motion detection |
| `lapcam.service` | systemd unit file — auto-start, USB power management, usbmon setup |
| `boot_splash.py` | Early boot message (writes "Booting..." to framebuffer) — kept for reference but removed from service |
| `config.json` | Persistent settings (all OSD values saved here) |
| `setup.sh` | First-time Pi setup — installs GStreamer, Python deps, V4L2 tools |
| `test_camera.sh` | Camera validation script — tests MJPEG at various resolutions |
| `deploy.sh` | Installs systemd service and starts the app |
| `PROJECT_PLAN.md` | Original project plan (from planning phase) |

### GStreamer Pipeline

The core video pipeline (built in `build_feed_pipeline()`):

```
v4l2src device=/dev/video0
  ! image/jpeg,width=1920,height=1080,framerate=30/1
  ! jpegdec
  ! queue max-size-buffers=2 leaky=downstream    ← decode thread
  ! videoconvert
  ! videobalance name=blackout                    ← for pause blackout
  ! textoverlay name=hud_overlay                  ← single overlay for all HUD
  ! [videoscale if resolution != display]
  ! queue max-size-buffers=2 leaky=downstream    ← display thread
  ! kmssink sync=false                            ← GPU-accelerated display
```

**Key design decisions**:

1. **Queue elements** — the single most impactful optimization. Adding `queue` between decode and display stages allows them to run on separate CPU cores. This boosted kmssink from 14 fps to 22 fps (+57%). The queues use `leaky=downstream` to drop old frames rather than blocking.

2. **Single textoverlay** — we initially used 3 separate textoverlay elements (stats, splash, OSD). Each one costs ~3 fps because it forces format conversion. Combining all text into one overlay made it nearly free.

3. **kmssink** — uses the GPU's DRM/KMS subsystem for proper page flipping (no tearing). Initially slower than fbdevsink (14 vs 20 fps) until queue elements fixed it.

4. **videobalance** — used to black out the feed during pause mode by setting `brightness=-1`. The pipeline keeps running (for instant resume), but the user sees a black screen with pause info overlaid via textoverlay.

5. **MJPEG decode is CPU-bound** — the Pi 4's hardware JPEG decoder (v4l2jpegdec) was actually SLOWER than software jpegdec due to V4L2 memory copy overhead.

### Display Pipeline Options Tested

| Sink | FPS (no queue) | FPS (with queue) | Tearing | Notes |
|------|---------------|-----------------|---------|-------|
| fbdevsink sync=false | 20 | 21 | Yes | Fastest but tears, writes directly to framebuffer |
| kmssink sync=false | 14 | **22** | No | Best with queues, GPU page flipping |
| glimagesink | 17 | — | No | **Broken colors on Pi 4** (Mesa V3D driver bug), abandoned |
| fbdevsink sync=true | — | — | No | Drops most frames, unusable |
| autovideosink | — | — | Varies | Used initially, slow transitions between pipelines |

Both kmssink and fbdevsink are available as a runtime-switchable option in the System OSD tab.

### Framebuffer Display

Static screens (boot message, pause screen) use direct framebuffer writes:
- Framebuffer is at `/dev/fb0`, format is RGB565 (16-bit)
- Pi 4 defaults to 4K resolution if the monitor supports it — forced to 1080p via `video=HDMI-A-2:1920x1080@60` in kernel cmdline
- PIL generates images, numpy does bulk RGB888→RGB565 conversion (initial pixel-by-pixel Python conversion took 5 seconds, numpy does it instantly)
- The framebuffer is on a different display plane than kmssink — when kmssink is active, the framebuffer is hidden underneath

### USB Button Monitor

The `USBButtonMonitor` class reads `/sys/kernel/debug/usb/usbmon/1u` in a daemon thread:
- Looks for lines containing `C Ii` (completed interrupt IN) and `02010001` (button signature)
- Implements single/double click detection with 500ms window and 150ms debounce
- The usbmon module must be loaded (`modprobe usbmon`) and permissions set (`chmod 644`) at each boot — handled by `ExecStartPre` in the service file

### Motion Detection

Implemented via GStreamer pad probe on the sink pad:
- Every N frames (configurable: 100ms-1000ms polling), grabs 4KB from the frame buffer
- Compares to previous sample using numpy mean absolute difference
- If difference exceeds threshold (configurable: 5-15), resets the inactivity timer
- Nearly zero CPU impact at any polling rate tested

**Note**: motion detection only works while the camera is streaming. In pause mode (if using keep-alive pipeline at 320x240), the samples would come from the low-res stream.

### OSD (On-Screen Display)

Tabbed interface with two-level navigation:

```
 > [Image]  Motion  System          ← arrow on tab row

   [Image]  Motion  System
 > Exposure    [Auto]  10%  25%  .. ← arrow on items
   Brightness  ...
```

**Navigation**:
- Single click: opens OSD → moves arrow down through items → wraps back to tab row
- Double click on tab row: cycles between tabs (Image → Motion → System)
- Double click on item: changes value (cycles through options)
- OSD auto-hides after 10 seconds of no interaction
- While OSD is visible, the inactivity timer is frozen

**Tabs**:
- **Image**: Exposure, Brightness, Contrast, Gain, Saturation, Sharpness, Anti-Flicker
- **Motion**: Threshold, Poll Rate, Idle Time
- **System**: Resolution, Pause Off, Stats, Renderer, Image Flip, CLI Mode

### Settings Persistence

All settings are saved to `config.json` immediately when changed. The file is loaded on startup and merged with defaults. Settings survive reboots.

### systemd Service

`lapcam.service` handles:
- **ExecStartPre** (runs as root): blanks console cursor, unbinds vtconsole, loads usbmon module, sets debugfs permissions
- **ExecStart**: runs `lapcam.py` as user `pi`
- **ExecStop** (runs as root): kills USB power via uhubctl (turns off lights)
- **Restart=on-failure**: auto-restarts on crash
- **RestartPreventExitStatus=42**: CLI mode exits with code 42 to prevent restart

---

## Boot Configuration

### /boot/firmware/config.txt additions

```ini
gpu_mem=128                              # More GPU memory for video
hdmi_group=1                             # CEA HDMI modes
hdmi_mode=16                             # 1080p 60Hz
dtoverlay=gpio-shutdown,gpio_pin=3       # Power button on GPIO 3
dtoverlay=gpio-led,gpio=22,trigger=mmc0  # Activity LED on GPIO 22
```

### /boot/firmware/cmdline.txt additions

```
console=tty3                    # Redirect console to hidden TTY (was tty1)
quiet splash loglevel=0         # Suppress boot messages
logo.nologo                     # Hide Raspberry Pi logo
vt.global_cursor_default=0      # Hide blinking cursor
usbcore.autosuspend=-1          # Prevent USB power management disconnects
video=HDMI-A-2:1920x1080@60    # Force 1080p on second HDMI port
consoleblank=0                  # Disable screen blanking
```

---

## Pi Setup from Scratch

### 1. Flash the SD card

Use Raspberry Pi Imager:
- **OS**: Raspberry Pi OS Lite (32-bit, Bookworm)
- **Settings**: hostname `lapcam`, user `pi`, SSH enabled, WiFi configured

Note: Bookworm uses NetworkManager instead of wpa_supplicant. The Imager's WiFi auto-config sometimes fails — may need manual `sudo nmcli dev wifi connect "SSID" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "password"`.

### 2. SSH setup

Password auth via sshpass doesn't work reliably on Windows. Set up SSH key auth:
1. Generate key: `ssh-keygen -t ed25519`
2. Transfer key to Pi (we used a temporary HTTP server on the Windows machine: `python -m http.server 8888`, then `curl` from the Pi)

### 3. Run setup

```bash
ssh pi@lapcam.local
cd /home/pi/lapcam
bash setup.sh        # ~10-15 minutes, installs GStreamer, Python deps, V4L2 tools
sudo reboot
bash test_camera.sh  # Verify camera works
bash deploy.sh       # Install systemd service
```

### 4. Additional packages needed

```bash
sudo apt-get install python3-numpy uhubctl
sudo pip install pyusb --break-system-packages  # for USB button investigation (installed as root)
```

### 5. Post-setup configuration

- Add overlays to `/boot/firmware/config.txt` (gpio-shutdown, gpio-led)
- Add kernel parameters to `/boot/firmware/cmdline.txt` (quiet, video=, usbcore.autosuspend)
- Create udev rule for framebuffer blank permissions:
  ```
  # /etc/udev/rules.d/99-fb-blank.rules
  SUBSYSTEM=="graphics", KERNEL=="fb0", ATTR{blank}="0", RUN+="/bin/chmod 0666 /sys/class/graphics/fb0/blank"
  ```
- Create usbmon module auto-load:
  ```
  # /etc/modules-load.d/usbmon.conf
  usbmon
  ```

---

## What Didn't Work (and Why)

### Pi 3 B+ for 1080p
CPU too slow for MJPEG decode + color conversion at 1080p (11.5 fps). Adequate for 720p (22 fps).

### Hardware JPEG decoder (v4l2jpegdec)
Slower than software decoder (4 fps vs 11.5 fps on Pi 3 B+) due to V4L2 memory-to-memory copy overhead.

### glimagesink (OpenGL ES)
Severe color corruption on Pi 4 — pixels appear as magenta/blue/green noise. This is a Mesa V3D driver bug. Cannot be fixed by changing pixel formats. Abandoned.

### Multiple textoverlay elements
Each textoverlay forces a format conversion round-trip, costing ~3 fps per element. Three overlays dropped fps from 22 to 11. Solved by combining all text into a single overlay.

### Pango markup for colored text in textoverlay
Attempted to use `<span foreground="#FFD700">` for yellow brackets in the OSD. Caused the pipeline to fail silently. Reverted to plain text.

### GPIO button detection for USB camera button
The camera button sends UVC interrupt packets, not GPIO signals. Attempted: Linux input events, V4L2 events, uvcvideo quirks — none worked. Solved by reading raw USB traffic via usbmon.

### Long press detection on camera button
The button sends exactly one interrupt packet per press, regardless of hold duration. Hold/release detection is impossible. Replaced with single/double click detection.

### Detaching USB interface 0 for button reading
Detaching the VideoControl interface (to read the interrupt endpoint) kills the video feed, even if done after the stream starts. The UVC driver manages both interfaces as a unit.

### scp from Windows introducing null bytes
Files copied via scp from Windows sometimes contain null bytes, causing `SyntaxError: source code cannot contain null bytes`. Workaround: write files directly on the Pi via SSH heredoc, or use scp for files that were originally written on the Pi.

### USB port power control at boot
USB ports are powered by the VL805 controller during early kernel init — before any systemd service can run. Cannot prevent brief power-on at boot. Accepted as cosmetic issue.

### fbdevsink with sync=true
Drops most frames ("A lot of buffers are being dropped" warning). Not viable for real-time video.

### Anti-flicker (power_line_frequency)
No visible effect — the training box LED lights are likely DC-powered (USB) and don't flicker at 50/60Hz. The setting is still exposed in the OSD for use with other light sources.

---

## Useful Commands

### Service management
```bash
sudo systemctl status lapcam.service
sudo systemctl restart lapcam.service
sudo systemctl stop lapcam.service
sudo journalctl -u lapcam.service -n 30 --no-pager --output=cat
```

### Camera debugging
```bash
v4l2-ctl --device=/dev/video0 --list-formats-ext     # Supported formats
v4l2-ctl --device=/dev/video0 --list-ctrls-menus      # Adjustable controls
v4l2-ctl --device=/dev/video0 --set-ctrl brightness=32 # Change a control
lsusb -v -d 1bcf:0b09                                 # USB device details
```

### Pipeline testing
```bash
# Basic feed test
gst-launch-1.0 v4l2src device=/dev/video0 ! image/jpeg,width=1920,height=1080,framerate=30/1 ! jpegdec ! videoconvert ! kmssink sync=false

# FPS measurement
gst-launch-1.0 v4l2src device=/dev/video0 num-buffers=200 ! image/jpeg,width=1920,height=1080,framerate=30/1 ! jpegdec ! videoconvert ! kmssink sync=false
# Time with: python3 -c "import subprocess, time; start=time.time(); subprocess.run([...]); print(f'{200/(time.time()-start):.1f} fps')"
```

### Power and temperature
```bash
vcgencmd get_throttled     # 0x0 = all good, 0x50005 = undervoltage
vcgencmd measure_temp      # CPU temperature
cat /sys/class/thermal/thermal_zone0/temp  # Temperature in millidegrees
```

### USB power control
```bash
sudo uhubctl                        # Show all USB port status
sudo uhubctl -l 1-1 -a off          # Turn off all ports on hub 1-1
sudo uhubctl -l 1-1 -p 2 -a off     # Turn off specific port
```

---

## Performance Summary (Pi 4, 1080p)

| Metric | Value |
|--------|-------|
| Resolution | 1920x1080 MJPEG |
| FPS (kmssink + queues) | ~22 fps |
| FPS (fbdevsink + queues) | ~21 fps |
| CPU usage | ~45% (2 cores active) |
| Temperature | ~64C under load |
| Camera max output | ~21 fps (USB/sensor limited) |
| Motion detection overhead | <1% CPU at any poll rate |
| Single textoverlay overhead | <1 fps |
| Pipeline startup time | ~3 seconds |
| Resume from pause | Instant (pipeline stays running) |
| Boot to feed | ~15-20 seconds |

---

## Future / Pending

- **LED lighting current draw**: need to measure with real training box lights to confirm USB power budget
- **Cooling fan**: needs MOSFET on GPIO for proper on/off control (fan draws too much for direct GPIO)
- **Recording**: save training sessions to file (GStreamer tee + filesink)
- **Tailscale**: remote SSH access from any network
- **Enclosure**: physical housing for Pi, buttons, LED, and cable management
