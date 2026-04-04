#!/usr/bin/env python3
"""Laparoscopic Camera Trainer — Main Application

State machine:
    OFF → BOOT → FEED ↔ PAUSE → OFF

Controls:
    Power button (GPIO 3): shutdown / wake from off
    Camera button (USB): short = cycle OSD category / wake from pause
                         long  = change value in category

Display:
    Feed: GStreamer pipeline with kmssink/fbdevsink + textoverlay HUD
    Pause: "Paused" on framebuffer, keep-alive pipeline for button detection
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time

import numpy as np

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
FRAMEBUFFER = '/dev/fb0'
FB_SIZE_PATH = '/sys/class/graphics/fb0/virtual_size'
FB_BPP_PATH = '/sys/class/graphics/fb0/bits_per_pixel'
USBMON_PATH = '/sys/kernel/debug/usb/usbmon/1u'

EXIT_CODE_CLI = 42

DEFAULTS = {
    'resolution': '1080p',
    'timeout_seconds': 300,
    'pause_timeout_minutes': 10,
    'image_flip': 'none',
    'camera_device': '/dev/video0',
    'stats': 'off',
    'adj_exposure': 'Auto',
    'adj_brightness': '13',
    'adj_contrast': '5',
    'adj_gain': '1',
    'adj_saturation': '60',
    'adj_sharpness': '3',
    'adj_powerline': '60Hz',
    'motion_threshold': '5',
    'motion_poll': '200ms',
    'video_sink': 'kms',
}

LIVE_ADJUSTMENTS = [
    {
        'key': 'adj_exposure',
        'label': 'Exposure',
        'values': ['Auto', '10%', '25%', '50%', '75%', '100%'],
    },
    {
        'key': 'adj_brightness',
        'label': 'Brightness',
        'values': ['-64', '-32', '0', '13', '32', '64'],
    },
    {
        'key': 'adj_contrast',
        'label': 'Contrast',
        'values': ['0', '5', '25', '50', '75', '95'],
    },
    {
        'key': 'adj_gain',
        'label': 'Gain',
        'values': ['1', '2', '4', '8'],
    },
    {
        'key': 'adj_saturation',
        'label': 'Saturation',
        'values': ['0', '30', '60', '80', '100'],
    },
    {
        'key': 'adj_sharpness',
        'label': 'Sharpness',
        'values': ['1', '3', '5', '7'],
    },
    {
        'key': 'adj_powerline',
        'label': 'Anti-Flicker',
        'values': ['off', '50Hz', '60Hz'],
    },
    {
        'key': 'motion_threshold',
        'label': 'Motion Thr.',
        'values': ['5', '7', '9', '11', '13', '15'],
    },
    {
        'key': 'motion_poll',
        'label': 'Motion Poll',
        'values': ['100ms', '200ms', '350ms', '500ms', '1000ms'],
    },
    {
        'key': 'resolution',
        'label': 'Resolution',
        'values': ['1080p', '720p'],
    },
    {
        'key': 'timeout_seconds',
        'label': 'Timeout',
        'values': ['15', '120', '300', '600', '0'],
        'display_map': {'15': '15s', '120': '2min', '300': '5min', '600': '10min', '0': 'Off'},
    },
    {
        'key': 'pause_timeout_minutes',
        'label': 'Pause Off',
        'values': ['5', '10', '30', '0'],
        'display_map': {'5': '5min', '10': '10min', '30': '30min', '0': 'Off'},
    },
    {
        'key': 'stats',
        'label': 'Stats',
        'values': ['off', 'on'],
    },
    {
        'key': 'video_sink',
        'label': 'Renderer',
        'values': ['fbdev', 'kms'],
        'display_map': {'fbdev': '20fps+tear', 'kms': '14fps smooth'},
    },
    {
        'key': 'image_flip',
        'label': 'Image Flip',
        'values': ['none', 'horizontal', 'vertical', 'both'],
    },
]

EXPOSURE_MIN = 10
EXPOSURE_MAX = 626

OSD_TIMEOUT = 10.0
BUTTON_LONG_PRESS_MS = 500
BUTTON_DEBOUNCE_MS = 200

RESOLUTION_MAP = {
    '1080p': (1920, 1080),
    '720p': (1280, 720),
}

MOTION_SAMPLE_INTERVAL = 1.0

FONT_SIZE_TITLE = 48
COLOR_BG = (0, 0, 0)
COLOR_TEXT = (255, 255, 255)


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------

def load_config():
    config = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, 'r') as f:
            saved = json.load(f)
        config.update(saved)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return config


def save_config(config):
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=4)


# ---------------------------------------------------------------------------
# Framebuffer display
# ---------------------------------------------------------------------------

def get_framebuffer_info():
    try:
        with open(FB_SIZE_PATH, 'r') as f:
            parts = f.read().strip().split(',')
            fb_w, fb_h = int(parts[0]), int(parts[1])
    except (OSError, ValueError):
        fb_w, fb_h = 1920, 1080
    try:
        with open(FB_BPP_PATH, 'r') as f:
            bpp = int(f.read().strip())
    except (OSError, ValueError):
        bpp = 16
    return fb_w, fb_h, bpp


def write_to_framebuffer(img):
    fb_w, fb_h, bpp = get_framebuffer_info()
    if img.size != (fb_w, fb_h):
        img = img.resize((fb_w, fb_h), Image.LANCZOS)
    arr = np.array(img, dtype=np.uint16)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    if bpp == 16:
        packed = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        fb_data = packed.astype('<u2').tobytes()
    elif bpp == 32:
        arr8 = np.array(img, dtype=np.uint8)
        bgra = np.zeros((fb_h, fb_w, 4), dtype=np.uint8)
        bgra[:, :, 0] = arr8[:, :, 2]
        bgra[:, :, 1] = arr8[:, :, 1]
        bgra[:, :, 2] = arr8[:, :, 0]
        bgra[:, :, 3] = 255
        fb_data = bgra.tobytes()
    else:
        packed = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        fb_data = packed.astype('<u2').tobytes()
    with open(FRAMEBUFFER, 'wb') as fb:
        fb.write(fb_data)


def clear_framebuffer():
    try:
        fb_w, fb_h, bpp = get_framebuffer_info()
        size = fb_w * fb_h * (bpp // 8)
        with open(FRAMEBUFFER, 'wb') as fb:
            fb.write(b'\x00' * size)
    except (PermissionError, OSError):
        pass


def show_message(text):
    fb_w, fb_h, _ = get_framebuffer_info()
    img = Image.new('RGB', (fb_w, fb_h), COLOR_BG)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf', FONT_SIZE_TITLE)
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    draw.text(((fb_w - tw) // 2, fb_h // 2 - 30), text, fill=COLOR_TEXT, font=font)
    write_to_framebuffer(img)


# ---------------------------------------------------------------------------
# System stats
# ---------------------------------------------------------------------------

def get_cpu_usage():
    try:
        with open('/proc/stat', 'r') as f:
            line = f.readline()
        parts = line.split()
        idle = int(parts[4])
        total = sum(int(x) for x in parts[1:])
        return idle, total
    except (OSError, ValueError, IndexError):
        return 0, 0


def get_cpu_temp():
    try:
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            return int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        return 0.0


def get_mem_usage():
    try:
        info = {}
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                parts = line.split()
                if parts[0] in ('MemTotal:', 'MemAvailable:'):
                    info[parts[0]] = int(parts[1])
        total = info.get('MemTotal:', 0)
        avail = info.get('MemAvailable:', 0)
        used = total - avail
        return used // 1024, total // 1024
    except (OSError, ValueError):
        return 0, 0


# ---------------------------------------------------------------------------
# Camera V4L2 controls
# ---------------------------------------------------------------------------

def apply_v4l2_adjustment(device, key, value):
    try:
        if key == 'adj_exposure':
            if value == 'Auto':
                subprocess.run(['v4l2-ctl', '-d', device,
                               '--set-ctrl', 'auto_exposure=3'],
                              capture_output=True, timeout=2)
            else:
                pct = int(value.replace('%', '')) / 100.0
                absolute = int(EXPOSURE_MIN + pct * (EXPOSURE_MAX - EXPOSURE_MIN))
                subprocess.run(['v4l2-ctl', '-d', device,
                               '--set-ctrl', 'auto_exposure=1',
                               '--set-ctrl', f'exposure_time_absolute={absolute}'],
                              capture_output=True, timeout=2)
        elif key == 'adj_brightness':
            subprocess.run(['v4l2-ctl', '-d', device, '--set-ctrl', f'brightness={value}'],
                          capture_output=True, timeout=2)
        elif key == 'adj_contrast':
            subprocess.run(['v4l2-ctl', '-d', device, '--set-ctrl', f'contrast={value}'],
                          capture_output=True, timeout=2)
        elif key == 'adj_gain':
            subprocess.run(['v4l2-ctl', '-d', device, '--set-ctrl', f'gain={value}'],
                          capture_output=True, timeout=2)
        elif key == 'adj_saturation':
            subprocess.run(['v4l2-ctl', '-d', device, '--set-ctrl', f'saturation={value}'],
                          capture_output=True, timeout=2)
        elif key == 'adj_powerline':
            plf = {'off': '0', '50Hz': '1', '60Hz': '2'}.get(value, '0')
            subprocess.run(['v4l2-ctl', '-d', device, '--set-ctrl', f'power_line_frequency={plf}'],
                          capture_output=True, timeout=2)
        elif key == 'adj_sharpness':
            subprocess.run(['v4l2-ctl', '-d', device, '--set-ctrl', f'sharpness={value}'],
                          capture_output=True, timeout=2)
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"V4L2 control error: {e}")


def apply_all_adjustments(device, config):
    for adj in LIVE_ADJUSTMENTS:
        if adj['key'].startswith('adj_'):
            value = config.get(adj['key'], adj['values'][0])
            apply_v4l2_adjustment(device, adj['key'], value)


# ---------------------------------------------------------------------------
# OSD text builder
# ---------------------------------------------------------------------------

def build_osd_text(adj_category_idx, config):
    max_values = max(len(adj['values']) for adj in LIVE_ADJUSTMENTS)
    col_width = 9

    lines = []
    for i, adj in enumerate(LIVE_ADJUSTMENTS):
        is_selected = (i == adj_category_idx)
        current_val = str(config.get(adj['key'], adj['values'][0]))
        marker = '>' if is_selected else ' '
        label = adj['label'].ljust(12)

        display_map = adj.get('display_map', {})
        value_parts = []
        for v in adj['values']:
            display_v = display_map.get(v, v)
            if v == current_val:
                inner = f'[{display_v}]'
            else:
                inner = display_v
            value_parts.append(inner.center(col_width))

        for _ in range(max_values - len(adj['values'])):
            value_parts.append(' ' * col_width)

        values_str = ''.join(value_parts)
        line = f" {marker} {label}{values_str}"
        lines.append(line)

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# GStreamer pipelines
# ---------------------------------------------------------------------------

def build_flip_elements(flip_mode):
    if flip_mode == 'horizontal':
        return '! videoflip method=horizontal-flip'
    elif flip_mode == 'vertical':
        return '! videoflip method=vertical-flip'
    elif flip_mode == 'both':
        return '! videoflip method=rotate-180'
    return ''


def build_feed_pipeline(config):
    res = RESOLUTION_MAP.get(config['resolution'], (1920, 1080))
    flip = build_flip_elements(config['image_flip'])
    fb_w, fb_h, _ = get_framebuffer_info()

    if res[0] != fb_w or res[1] != fb_h:
        scale = f'! videoscale ! video/x-raw,width={fb_w},height={fb_h} '
    else:
        scale = ''

    overlay = (
        '! videobalance name=blackout '
        '! textoverlay name=hud_overlay text="" '
        'valignment=top halignment=left '
        'font-desc="DejaVu Sans Mono, 10" '
        'draw-shadow=true shaded-background=true '
    )

    pipeline = (
        f'v4l2src device={config["camera_device"]} '
        f'! image/jpeg,width={res[0]},height={res[1]},framerate=30/1 '
        f'! jpegdec '
        f'! queue max-size-buffers=2 leaky=downstream '
        f'! videoconvert '
        f'{flip} '
        f'{overlay}'
        f'{scale}'
        f'! queue max-size-buffers=2 leaky=downstream '
    )

    sink = config.get('video_sink', 'kms')
    if sink == 'kms':
        pipeline += '! kmssink sync=false'
    else:
        pipeline += '! video/x-raw,format=RGB16 ! fbdevsink sync=false'

    return pipeline


def build_keepalive_pipeline(config):
    """Minimal pipeline to keep USB streaming alive for button detection."""
    return (
        f'v4l2src device={config["camera_device"]} '
        f'! image/jpeg,width=320,height=240,framerate=30/1 '
        f'! fakesink sync=false'
    )


# ---------------------------------------------------------------------------
# USB Button Monitor (via usbmon)
# ---------------------------------------------------------------------------

class USBButtonMonitor:
    """Monitors the camera button via /sys/kernel/debug/usb/usbmon.

    The button sends one '02010001' interrupt per press.
    Single click vs double click detection:
    - After first click, wait DOUBLE_CLICK_MS for a second click
    - If second click arrives: double click callback
    - If no second click: single click callback
    """

    BUTTON_SIGNATURE = '02010001'
    DOUBLE_CLICK_MS = 500
    DEBOUNCE_MS = 150

    def __init__(self, callback_single, callback_double, bus=1):
        self._callback_single = callback_single
        self._callback_double = callback_double
        self._path = f'/sys/kernel/debug/usb/usbmon/{bus}u'
        self._thread = None
        self._timer = None
        self._running = False
        self._click_count = 0
        self._last_event_time = 0

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._timer:
            self._timer.cancel()

    def _monitor_loop(self):
        try:
            f = open(self._path, 'r')
        except (PermissionError, OSError) as e:
            print(f"USB monitor error: {e}")
            return

        while self._running:
            try:
                line = f.readline()
                if not line:
                    time.sleep(0.01)
                    continue

                if ' C Ii' in line and self.BUTTON_SIGNATURE in line:
                    now = time.time()

                    # Debounce
                    if now - self._last_event_time < self.DEBOUNCE_MS / 1000.0:
                        continue
                    self._last_event_time = now

                    self._click_count += 1

                    if self._click_count == 1:
                        # First click — wait for possible second click
                        if self._timer:
                            self._timer.cancel()
                        self._timer = threading.Timer(
                            self.DOUBLE_CLICK_MS / 1000.0, self._resolve_click)
                        self._timer.daemon = True
                        self._timer.start()
                    elif self._click_count >= 2:
                        # Second click — double click!
                        if self._timer:
                            self._timer.cancel()
                        self._click_count = 0
                        self._callback_double()

            except Exception:
                if self._running:
                    time.sleep(0.1)

        try:
            f.close()
        except:
            pass

    def _resolve_click(self):
        """Called after DOUBLE_CLICK_MS — no second click arrived, so it's a single."""
        if self._click_count == 1:
            self._click_count = 0
            self._callback_single()
        else:
            self._click_count = 0


# ---------------------------------------------------------------------------
# Application state machine
# ---------------------------------------------------------------------------

class LapcamApp:

    STATE_FEED = 'feed'
    STATE_PAUSE = 'pause'

    def __init__(self):
        Gst.init(None)
        self.config = load_config()
        self.state = None
        self.pipeline = None
        self.keepalive_pipeline = None
        self.mainloop = GLib.MainLoop()
        self.last_motion_time = time.time()
        self.motion_timer = None
        self.stats_timer = None
        self.osd_timer = None
        self.splash_timer = None
        self.pause_timer = None
        self._prev_cpu = (0, 0)
        self._frame_count = 0
        self._last_frame_count = 0
        self._fps_time = time.time()
        self._current_fps = 0.0
        self._feed_start_time = 0

        # OSD state
        self._adj_category_idx = 0
        self._osd_visible = False

        # Motion detection
        self._motion_sample = None
        self._prev_motion_sample = None
        self._last_motion_diff = 0.0

        # HUD state
        self._hud_stats_line = ''
        self._hud_splash_line = ''
        self._hud_osd_text = ''
        self._hud_countdown_line = ''
        self._countdown_active = False

        # USB button monitor
        self._usb_monitor = USBButtonMonitor(
            callback_single=self._on_button_single,
            callback_double=self._on_button_double
        )

        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        print(f"Received signal {signum}, shutting down...")
        self._stop_feed_pipeline()
        try:
            with open('/sys/class/graphics/fb0/blank', 'w') as f:
                f.write('0')
        except (PermissionError, OSError):
            pass
        show_message("Shutting down...")
        self.stop()

    def start(self):
        print("Laparoscopic Camera Trainer starting...")
        self._usb_monitor.start()
        self._start_feed()
        try:
            self.mainloop.run()
        except KeyboardInterrupt:
            self.stop()
        sys.exit(0)

    def stop(self):
        print("Stopping...")
        self._usb_monitor.stop()
        self._stop_feed_pipeline()
        self._stop_keepalive()
        self._cancel_all_timers()
        self.mainloop.quit()

    def _cancel_all_timers(self):
        for timer in [self.motion_timer, self.stats_timer, self.osd_timer,
                      self.splash_timer, self.pause_timer]:
            if timer:
                timer.cancel()
        self.motion_timer = None
        self.stats_timer = None
        self.osd_timer = None
        self.splash_timer = None
        self.pause_timer = None

    # -- HUD management --

    def _refresh_hud(self):
        if not self.pipeline:
            return
        overlay = self.pipeline.get_by_name('hud_overlay')
        if not overlay:
            return

        lines = []
        if self._hud_stats_line:
            lines.append(self._hud_stats_line)
        if self._hud_countdown_line:
            lines.append('')
            lines.append(self._hud_countdown_line)
        if self._hud_splash_line:
            lines.append('')
            lines.append(self._hud_splash_line)
        if self._hud_osd_text:
            lines.append('')
            lines.append(self._hud_osd_text)

        text = '\n'.join(lines)
        GLib.idle_add(overlay.set_property, 'text', text)

    # -- Pipeline management --

    def _stop_feed_pipeline(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    def _start_feed_pipeline(self, pipeline_str):
        self._stop_feed_pipeline()
        self._frame_count = 0
        print(f"Pipeline: {pipeline_str}")
        try:
            self.pipeline = Gst.parse_launch(pipeline_str)
            bus = self.pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect('message::error', self._on_pipeline_error)

            # Attach frame counter probe to the sink
            it = self.pipeline.iterate_sinks()
            _, sink = it.next()
            if sink:
                pad = sink.get_static_pad('sink')
                if pad:
                    pad.add_probe(Gst.PadProbeType.BUFFER, self._frame_probe)

            self.pipeline.set_state(Gst.State.PLAYING)
        except GLib.Error as e:
            print(f"Pipeline error: {e}")

    def _start_keepalive(self):
        """Start minimal pipeline to keep USB streaming alive."""
        self._stop_keepalive()
        pipeline_str = build_keepalive_pipeline(self.config)
        print(f"Keep-alive: {pipeline_str}")
        try:
            self.keepalive_pipeline = Gst.parse_launch(pipeline_str)
            self.keepalive_pipeline.set_state(Gst.State.PLAYING)
        except GLib.Error as e:
            print(f"Keep-alive error: {e}")

    def _stop_keepalive(self):
        if self.keepalive_pipeline:
            self.keepalive_pipeline.set_state(Gst.State.NULL)
            self.keepalive_pipeline = None

    def _frame_probe(self, pad, info):
        self._frame_count += 1

        poll_ms = int(self.config.get('motion_poll', '200ms').replace('ms', ''))
        poll_frames = max(1, int(poll_ms / 50))
        if self._frame_count % poll_frames == 0:
            buf = info.get_buffer()
            if buf:
                try:
                    success, mapinfo = buf.map(Gst.MapFlags.READ)
                    if success:
                        data = bytes(mapinfo.data[:4096])
                        buf.unmap(mapinfo)
                        self._motion_sample = data
                except Exception:
                    pass

        return Gst.PadProbeReturn.OK

    def _on_pipeline_error(self, bus, msg):
        err, debug = msg.parse_error()
        print(f"Pipeline error: {err.message}")
        print(f"Debug: {debug}")
        GLib.idle_add(self._enter_pause)

    # -- State transitions --

    def _start_feed(self):
        """Start or resume the live camera feed."""
        resuming = (self.state == self.STATE_PAUSE and self.pipeline is not None)

        self._cancel_all_timers()
        self.state = self.STATE_FEED
        self.last_motion_time = time.time()
        self._countdown_active = False
        self._hud_stats_line = ''
        self._hud_splash_line = ''
        self._hud_osd_text = ''
        self._hud_countdown_line = ''

        if resuming:
            # Pipeline already running — restore video and clear HUD
            if self.pipeline:
                blackout = self.pipeline.get_by_name('blackout')
                if blackout:
                    blackout.set_property('brightness', 0.0)
            self._refresh_hud()
            print("State: FEED (resumed)")
        else:
            # Cold start — build and start pipeline
            self._stop_keepalive()
            self._motion_sample = None
            self._prev_motion_sample = None

            show_message("Starting...")
            self._feed_start_time = time.time()

            pipeline_str = build_feed_pipeline(self.config)
            self._start_feed_pipeline(pipeline_str)

            device = self.config.get('camera_device', '/dev/video0')
            apply_all_adjustments(device, self.config)

            self._show_splash()
            print("State: FEED (cold start)")

        # Start stats if enabled
        if self.config.get('stats') == 'on':
            self._start_stats()

        # Start motion detection
        timeout = int(self.config.get('timeout_seconds', 300))
        if timeout > 0:
            self._schedule_motion_check()

    def _enter_pause(self):
        """Enter pause mode — pipeline keeps running, black out video, show pause info on HUD."""
        self.state = self.STATE_PAUSE
        self._stop_stats()
        if self.motion_timer:
            self.motion_timer.cancel()
            self.motion_timer = None
        self._osd_visible = False
        self._hud_osd_text = ''
        self._hud_splash_line = ''
        self._hud_countdown_line = ''

        # Black out the video feed
        if self.pipeline:
            blackout = self.pipeline.get_by_name('blackout')
            if blackout:
                blackout.set_property('brightness', -1.0)

        # Start pause countdown on HUD
        self._pause_start_time = time.time()
        self._pause_timeout_secs = int(self.config.get('pause_timeout_minutes', 10)) * 60
        self._update_pause_hud()

        print("State: PAUSE")

    def _update_pause_hud(self):
        """Update the HUD with pause countdown."""
        if self.state != self.STATE_PAUSE:
            return

        elapsed = time.time() - self._pause_start_time
        remaining = self._pause_timeout_secs - elapsed

        if remaining <= 0 and self._pause_timeout_secs > 0:
            print("Pause timeout — shutting down")
            GLib.idle_add(self._do_shutdown)
            return

        # Build pause HUD text
        lines = []
        lines.append('')
        lines.append('                    Paused')
        lines.append('')
        if self._pause_timeout_secs > 0:
            mins = int(remaining) // 60
            secs = int(remaining) % 60
            lines.append(f'          Shutting down in {mins:02d}:{secs:02d}')
        else:
            lines.append('          Shutdown timer: Off')
        lines.append('')
        lines.append('       Press camera button to resume')

        self._hud_stats_line = ''
        self._hud_countdown_line = '\n'.join(lines)
        self._refresh_hud()

        # Schedule next update
        self.pause_timer = threading.Timer(1.0, self._update_pause_hud)
        self.pause_timer.daemon = True
        self.pause_timer.start()

    def _do_shutdown(self):
        print("Shutting down system...")
        show_message("Shutting down...")
        time.sleep(2)
        self.stop()
        os.system('sudo shutdown -h now')

    # -- USB Button handlers --

    def _on_button_single(self):
        """Camera button single click."""
        if self.state == self.STATE_PAUSE:
            GLib.idle_add(self._start_feed)
        elif self.state == self.STATE_FEED:
            GLib.idle_add(self._adj_next_category)

    def _on_button_double(self):
        """Camera button double click."""
        if self.state == self.STATE_PAUSE:
            GLib.idle_add(self._start_feed)
        elif self.state == self.STATE_FEED:
            GLib.idle_add(self._adj_change_value)

    # -- Stats overlay --

    def _start_stats(self):
        self._prev_cpu = get_cpu_usage()
        self._fps_time = time.time()
        self._last_frame_count = self._frame_count
        self._current_fps = 0.0
        self._schedule_stats()

    def _stop_stats(self):
        if self.stats_timer:
            self.stats_timer.cancel()
            self.stats_timer = None

    def _schedule_stats(self):
        if self.state != self.STATE_FEED:
            return
        self.stats_timer = threading.Timer(1.0, self._update_stats)
        self.stats_timer.daemon = True
        self.stats_timer.start()

    def _update_stats(self):
        if self.state != self.STATE_FEED:
            return
        if self.config.get('stats') != 'on':
            return
        if not self.pipeline:
            self._schedule_stats()
            return

        now = time.time()
        elapsed = now - self._fps_time
        frames = self._frame_count - self._last_frame_count
        if elapsed > 0 and frames >= 0:
            self._current_fps = frames / elapsed
        self._last_frame_count = self._frame_count
        self._fps_time = now

        cpu_idle, cpu_total = get_cpu_usage()
        if self._prev_cpu[1] > 0:
            d_idle = cpu_idle - self._prev_cpu[0]
            d_total = cpu_total - self._prev_cpu[1]
            cpu_pct = 100.0 * (1.0 - d_idle / d_total) if d_total > 0 else 0.0
        else:
            cpu_pct = 0.0
        self._prev_cpu = (cpu_idle, cpu_total)

        temp = get_cpu_temp()
        mem_used, mem_total = get_mem_usage()

        res = self.config.get('resolution', '1080p')
        idle = time.time() - self.last_motion_time
        motion_status = f"Motion: {self._last_motion_diff:.1f}"
        threshold = float(self.config.get('motion_threshold', '5'))
        if self._last_motion_diff > threshold:
            motion_status += " [ACTIVE]"
        else:
            motion_status += f" [IDLE {idle:.0f}s]"
        self._hud_stats_line = (
            f"{res}  FPS: {self._current_fps:.1f}  "
            f"CPU: {cpu_pct:.0f}%  Temp: {temp:.0f}C  "
            f"RAM: {mem_used}/{mem_total}MB  {motion_status}")
        self._refresh_hud()
        self._schedule_stats()

    # -- Splash overlay --

    def _show_splash(self):
        startup_time = time.time() - self._feed_start_time
        self._hud_splash_line = f'[Cam btn] Adjust image    (started in {startup_time:.1f}s)'
        self._refresh_hud()
        if self.splash_timer:
            self.splash_timer.cancel()
        self.splash_timer = threading.Timer(3.0, self._hide_splash)
        self.splash_timer.daemon = True
        self.splash_timer.start()

    def _hide_splash(self):
        self._hud_splash_line = ''
        self._refresh_hud()

    # -- Live adjustment OSD --

    def _show_osd(self):
        self._osd_visible = True
        # Reset inactivity timer — user is interacting
        self.last_motion_time = time.time()
        # Clear countdown if showing
        if self._countdown_active:
            self._hud_countdown_line = ''
            self._countdown_active = False
        self._hud_osd_text = build_osd_text(self._adj_category_idx, self.config)
        self._refresh_hud()
        self._reset_osd_timer()

    def _hide_osd(self):
        self._osd_visible = False
        self._hud_osd_text = ''
        # Reset inactivity timer when OSD closes
        self.last_motion_time = time.time()
        self._refresh_hud()

    def _reset_osd_timer(self):
        if self.osd_timer:
            self.osd_timer.cancel()
        self.osd_timer = threading.Timer(OSD_TIMEOUT, self._hide_osd)
        self.osd_timer.daemon = True
        self.osd_timer.start()

    def _adj_next_category(self):
        self._adj_category_idx = (self._adj_category_idx + 1) % len(LIVE_ADJUSTMENTS)
        self._show_osd()
        adj = LIVE_ADJUSTMENTS[self._adj_category_idx]
        print(f"OSD category: {adj['label']}")

    def _adj_change_value(self):
        if not self._osd_visible:
            self._show_osd()
            return

        adj = LIVE_ADJUSTMENTS[self._adj_category_idx]
        current = str(self.config.get(adj['key'], adj['values'][0]))
        try:
            idx = adj['values'].index(current)
            next_idx = (idx + 1) % len(adj['values'])
        except ValueError:
            next_idx = 0

        new_val = adj['values'][next_idx]
        self.config[adj['key']] = new_val
        save_config(self.config)

        key = adj['key']
        if key in ('resolution', 'video_sink'):
            self._stop_feed_pipeline()
            pipeline_str = build_feed_pipeline(self.config)
            self._start_feed_pipeline(pipeline_str)
            device = self.config.get('camera_device', '/dev/video0')
            apply_all_adjustments(device, self.config)
        elif key == 'stats':
            if new_val == 'on':
                self._start_stats()
            else:
                self._stop_stats()
                self._hud_stats_line = ''
        elif key.startswith('adj_'):
            device = self.config.get('camera_device', '/dev/video0')
            apply_v4l2_adjustment(device, key, new_val)

        self._show_osd()
        print(f"{adj['label']} changed to: {new_val}")

    # -- Inactivity detection --

    def _schedule_motion_check(self):
        if self.state != self.STATE_FEED:
            return
        self.motion_timer = threading.Timer(MOTION_SAMPLE_INTERVAL, self._check_motion)
        self.motion_timer.daemon = True
        self.motion_timer.start()

    def _check_motion(self):
        if self.state != self.STATE_FEED:
            return

        timeout = int(self.config.get('timeout_seconds', 300))
        if timeout <= 0:
            return

        if self._motion_sample and self._prev_motion_sample:
            curr = np.frombuffer(self._motion_sample, dtype=np.uint8)
            prev = np.frombuffer(self._prev_motion_sample, dtype=np.uint8)
            min_len = min(len(curr), len(prev))
            if min_len > 0:
                diff = np.mean(np.abs(
                    curr[:min_len].astype(np.int16) - prev[:min_len].astype(np.int16)))
                self._last_motion_diff = diff
                threshold = float(self.config.get('motion_threshold', '5'))
                if diff > threshold:
                    self.last_motion_time = time.time()

        self._prev_motion_sample = self._motion_sample

        elapsed = time.time() - self.last_motion_time
        remaining = timeout - elapsed

        # While OSD is visible, keep resetting the inactivity timer
        if self._osd_visible:
            self.last_motion_time = time.time()
        else:
            if remaining <= 0:
                print(f"Inactivity timeout ({timeout}s) — entering pause")
                GLib.idle_add(self._enter_pause)
                return

            # Show countdown after 15 seconds of inactivity
            if elapsed >= 15:
                self._hud_countdown_line = f'Pausing in {int(remaining) + 1}...'
                self._countdown_active = True
                self._refresh_hud()
            elif self._countdown_active:
                self._hud_countdown_line = ''
                self._countdown_active = False
                self._refresh_hud()

        self._schedule_motion_check()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = LapcamApp()
    app.start()


if __name__ == '__main__':
    main()
