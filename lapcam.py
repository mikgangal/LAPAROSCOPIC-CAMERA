#!/usr/bin/env python3
"""Laparoscopic Camera Trainer — Main Application

State machine:
    BOOT → WELCOME → LIVE (camera feed)
                   → SETTINGS (menu)

Buttons:
    Welcome:  A = start training, B = settings
    Settings: A short = navigate, A long = change/select, B = back
    Feed:     A = stop feed → welcome

Display:
    Static screens write directly to /dev/fb0 (instant, no GStreamer).
    Camera feed uses GStreamer with fbdevsink.
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

from gpiozero import Button
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
FRAMEBUFFER = '/dev/fb0'
FB_SIZE_PATH = '/sys/class/graphics/fb0/virtual_size'
FB_BPP_PATH = '/sys/class/graphics/fb0/bits_per_pixel'

EXIT_CODE_CLI = 42

DEFAULTS = {
    'resolution': '1080p',
    'timeout_seconds': 300,
    'image_flip': 'none',
    'camera_device': '/dev/video0',
    'stats': 'off',
    # Live adjustments (persisted)
    'adj_exposure': 'Auto',
    'adj_brightness': '13',
    'adj_contrast': '5',
    'adj_gain': '1',
    'adj_saturation': '60',
    'adj_sharpness': '3',
    'motion_threshold': '5',
    'motion_poll': '200ms',
    'video_sink': 'kms',
}

# Live feed adjustable controls
# Each entry: key in config, display label, list of display values, v4l2 apply function
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
]

# Exposure mapping (camera range: 10-626)
EXPOSURE_MIN = 10
EXPOSURE_MAX = 626

OSD_TIMEOUT = 10.0  # seconds before OSD hides

RESOLUTION_MAP = {
    '1080p': (1920, 1080),
    '720p': (1280, 720),
}

SETTINGS_OPTIONS = [
    {
        'key': 'image_flip',
        'label': 'Image Flip',
        'values': ['none', 'horizontal', 'vertical', 'both'],
        'display': lambda v: v.capitalize(),
    },
    {
        'key': 'cli',
        'label': 'CLI Mode',
        'values': None,
    },
    {
        'key': 'shutdown',
        'label': 'Shutdown',
        'values': None,
    },
]

# GPIO pin assignments
GPIO_BUTTON_A = 17
GPIO_BUTTON_B = 27

# Button timing
LONG_PRESS_SECONDS = 0.5

# Inactivity detection
MOTION_SAMPLE_INTERVAL = 1.0
MOTION_THRESHOLD = 5.0
MOTION_FRAME_SIZE = (160, 120)

# Screen appearance
FONT_SIZE = 32
FONT_SIZE_TITLE = 48
FONT_SIZE_FOOTER = 20
COLOR_BG = (0, 0, 0)
COLOR_TEXT = (255, 255, 255)
COLOR_HIGHLIGHT = (0, 180, 255)
COLOR_DIM = (120, 120, 120)


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
    saveable = {k: v for k, v in config.items()
                if k not in ('shutdown', 'cli')}
    with open(CONFIG_PATH, 'w') as f:
        json.dump(saveable, f, indent=4)


# ---------------------------------------------------------------------------
# Framebuffer display
# ---------------------------------------------------------------------------

def _load_fonts():
    try:
        return (
            ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf', FONT_SIZE_TITLE),
            ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf', FONT_SIZE),
            ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf', FONT_SIZE_FOOTER),
        )
    except OSError:
        f = ImageFont.load_default()
        return (f, f, f)


def get_framebuffer_info():
    """Read the actual framebuffer resolution and bit depth."""
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
    """Write a PIL Image to /dev/fb0, scaling to fit the actual framebuffer."""
    fb_w, fb_h, bpp = get_framebuffer_info()

    # Scale image to framebuffer size if different
    if img.size != (fb_w, fb_h):
        img = img.resize((fb_w, fb_h), Image.LANCZOS)

    arr = np.array(img, dtype=np.uint16)
    r = arr[:, :, 0]
    g = arr[:, :, 1]
    b = arr[:, :, 2]

    if bpp == 16:
        # RGB565
        packed = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        fb_data = packed.astype('<u2').tobytes()
    elif bpp == 32:
        # BGRA8888
        arr8 = np.array(img, dtype=np.uint8)
        bgra = np.zeros((fb_h, fb_w, 4), dtype=np.uint8)
        bgra[:, :, 0] = arr8[:, :, 2]  # B
        bgra[:, :, 1] = arr8[:, :, 1]  # G
        bgra[:, :, 2] = arr8[:, :, 0]  # R
        bgra[:, :, 3] = 255            # A
        fb_data = bgra.tobytes()
    else:
        # Fallback to RGB565
        packed = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        fb_data = packed.astype('<u2').tobytes()

    with open(FRAMEBUFFER, 'wb') as fb:
        fb.write(fb_data)


def clear_framebuffer():
    """Fill the framebuffer with black."""
    try:
        fb_w, fb_h, bpp = get_framebuffer_info()
        size = fb_w * fb_h * (bpp // 8)
        with open(FRAMEBUFFER, 'wb') as fb:
            fb.write(b'\x00' * size)
    except (PermissionError, OSError):
        pass


def generate_welcome_image(display_size=(1920, 1080)):
    w, h = display_size
    img = Image.new('RGB', (w, h), COLOR_BG)
    draw = ImageDraw.Draw(img)
    font_title, font_menu, font_footer = _load_fonts()

    title = "LAPAROSCOPIC TRAINER"
    bbox = draw.textbbox((0, 0), title, font=font_title)
    title_w = bbox[2] - bbox[0]
    draw.text(((w - title_w) // 2, h // 3), title, fill=COLOR_TEXT, font=font_title)

    footer = "[A] Start Training          [B] Settings"
    bbox = draw.textbbox((0, 0), footer, font=font_footer)
    footer_w = bbox[2] - bbox[0]
    draw.text(((w - footer_w) // 2, h * 2 // 3), footer, fill=COLOR_DIM, font=font_footer)

    return img


def generate_settings_image(config, cursor_index, confirm_pending=False,
                            display_size=(1920, 1080)):
    w, h = display_size
    img = Image.new('RGB', (w, h), COLOR_BG)
    draw = ImageDraw.Draw(img)
    font_title, font_menu, font_footer = _load_fonts()

    title = "SETTINGS"
    bbox = draw.textbbox((0, 0), title, font=font_title)
    title_w = bbox[2] - bbox[0]
    draw.text(((w - title_w) // 2, h // 8), title, fill=COLOR_TEXT, font=font_title)

    menu_y_start = h // 3
    line_height = FONT_SIZE + 20

    for i, option in enumerate(SETTINGS_OPTIONS):
        y = menu_y_start + i * line_height
        is_selected = (i == cursor_index)
        prefix = ">" if is_selected else " "
        color = COLOR_HIGHLIGHT if is_selected else COLOR_TEXT

        if option['key'] in ('shutdown', 'cli'):
            label = f" {prefix}  {option['label']}"
            if is_selected and confirm_pending:
                label += "  [hold again to confirm]"
        elif option['values'] is not None:
            display_fn = option.get('display', str)
            current_val = config.get(option['key'], option['values'][0])
            label = f" {prefix}  {option['label']}:  {display_fn(current_val)}"
        else:
            label = f" {prefix}  {option['label']}"

        draw.text((w // 6, y), label, fill=color, font=font_menu)

    footer = "[B] Navigate  [B hold] Change  [A] Back"
    bbox = draw.textbbox((0, 0), footer, font=font_footer)
    footer_w = bbox[2] - bbox[0]
    draw.text(((w - footer_w) // 2, h - h // 8), footer, fill=COLOR_DIM, font=font_footer)

    return img


def generate_message_image(text, display_size=(1920, 1080)):
    w, h = display_size
    img = Image.new('RGB', (w, h), COLOR_BG)
    draw = ImageDraw.Draw(img)
    font_title, _, _ = _load_fonts()

    bbox = draw.textbbox((0, 0), text, font=font_title)
    tw = bbox[2] - bbox[0]
    draw.text(((w - tw) // 2, h // 2 - 30), text, fill=COLOR_TEXT, font=font_title)
    return img


# ---------------------------------------------------------------------------
# Stats overlay
# ---------------------------------------------------------------------------

STATS_FONT_SIZE = 16
STATS_WIDTH = 280
STATS_HEIGHT = 100
STATS_MARGIN = 10

def _load_stats_font():
    try:
        return ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf', STATS_FONT_SIZE)
    except OSError:
        return ImageFont.load_default()


def get_cpu_usage():
    """Read CPU usage from /proc/stat (average across all cores)."""
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
    """Read CPU temperature."""
    try:
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            return int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        return 0.0


def get_mem_usage():
    """Read memory usage from /proc/meminfo."""
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
        return used // 1024, total // 1024  # MB
    except (OSError, ValueError):
        return 0, 0


def write_stats_overlay(fb_w, fb_h, bpp, fps, prev_cpu):
    """Write a small stats box to the top-right corner of the framebuffer."""
    font = _load_stats_font()

    # Gather stats
    cpu_idle, cpu_total = get_cpu_usage()
    if prev_cpu[1] > 0:
        d_idle = cpu_idle - prev_cpu[0]
        d_total = cpu_total - prev_cpu[1]
        cpu_pct = 100.0 * (1.0 - d_idle / d_total) if d_total > 0 else 0.0
    else:
        cpu_pct = 0.0

    temp = get_cpu_temp()
    mem_used, mem_total = get_mem_usage()

    # Build overlay image
    img = Image.new('RGB', (STATS_WIDTH, STATS_HEIGHT), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    lines = [
        f" FPS: {fps:.1f}",
        f" CPU: {cpu_pct:.0f}%   Temp: {temp:.0f}C",
        f" RAM: {mem_used}/{mem_total} MB",
    ]
    y = 8
    for line in lines:
        draw.text((4, y), line, fill=(0, 255, 0), font=font)
        y += STATS_FONT_SIZE + 6

    # Convert to framebuffer format
    arr = np.array(img, dtype=np.uint16)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]

    if bpp == 16:
        packed = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        overlay_bytes = packed.astype('<u2').tobytes()
        bytes_per_pixel = 2
    elif bpp == 32:
        arr8 = np.array(img, dtype=np.uint8)
        bgra = np.zeros((STATS_HEIGHT, STATS_WIDTH, 4), dtype=np.uint8)
        bgra[:, :, 0] = arr8[:, :, 2]
        bgra[:, :, 1] = arr8[:, :, 1]
        bgra[:, :, 2] = arr8[:, :, 0]
        bgra[:, :, 3] = 255
        overlay_bytes = bgra.tobytes()
        bytes_per_pixel = 4
    else:
        return cpu_idle, cpu_total

    # Write overlay to top-right corner of framebuffer
    x_offset = fb_w - STATS_WIDTH - STATS_MARGIN
    y_offset = STATS_MARGIN
    fb_line_bytes = fb_w * bytes_per_pixel
    overlay_line_bytes = STATS_WIDTH * bytes_per_pixel

    try:
        with open(FRAMEBUFFER, 'r+b') as fb:
            for row in range(STATS_HEIGHT):
                pos = (y_offset + row) * fb_line_bytes + x_offset * bytes_per_pixel
                fb.seek(pos)
                row_start = row * overlay_line_bytes
                fb.write(overlay_bytes[row_start:row_start + overlay_line_bytes])
    except (PermissionError, OSError):
        pass

    return cpu_idle, cpu_total


# ---------------------------------------------------------------------------
# Camera V4L2 controls
# ---------------------------------------------------------------------------

def apply_v4l2_adjustment(device, key, value):
    """Apply a camera adjustment via v4l2-ctl."""
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
            subprocess.run(['v4l2-ctl', '-d', device,
                           '--set-ctrl', f'brightness={value}'],
                          capture_output=True, timeout=2)
        elif key == 'adj_contrast':
            subprocess.run(['v4l2-ctl', '-d', device,
                           '--set-ctrl', f'contrast={value}'],
                          capture_output=True, timeout=2)
        elif key == 'adj_gain':
            subprocess.run(['v4l2-ctl', '-d', device,
                           '--set-ctrl', f'gain={value}'],
                          capture_output=True, timeout=2)
        elif key == 'adj_saturation':
            subprocess.run(['v4l2-ctl', '-d', device,
                           '--set-ctrl', f'saturation={value}'],
                          capture_output=True, timeout=2)
        elif key == 'adj_sharpness':
            subprocess.run(['v4l2-ctl', '-d', device,
                           '--set-ctrl', f'sharpness={value}'],
                          capture_output=True, timeout=2)
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"V4L2 control error: {e}")


def apply_all_adjustments(device, config):
    """Apply all saved adjustments to the camera."""
    for adj in LIVE_ADJUSTMENTS:
        value = config.get(adj['key'], adj['values'][0])
        apply_v4l2_adjustment(device, adj['key'], value)


def build_osd_text(adj_category_idx, config):
    """Build multi-line OSD text showing all adjustments."""
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
# GStreamer pipeline (camera feed only)
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

    # Scale to framebuffer size if camera resolution differs
    if res[0] != fb_w or res[1] != fb_h:
        scale = f'! videoscale ! video/x-raw,width={fb_w},height={fb_h} '
    else:
        scale = ''

    # Single combined overlay for all HUD elements (stats, OSD, splash)
    overlay = (
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

    # Select video sink
    sink = config.get('video_sink', 'fbdev')
    if sink == 'kms':
        pipeline += '! kmssink sync=false'
    else:
        fb_w, fb_h, _ = get_framebuffer_info()
        pipeline += f'! video/x-raw,format=RGB16 ! fbdevsink sync=false'

    return pipeline


# ---------------------------------------------------------------------------
# Application state machine
# ---------------------------------------------------------------------------

class LapcamApp:

    STATE_WELCOME = 'welcome'
    STATE_SETTINGS = 'settings'
    STATE_LIVE = 'live'

    def __init__(self):
        Gst.init(None)
        self.config = load_config()
        self.state = self.STATE_WELCOME
        self.cursor_index = 0
        self.pipeline = None
        self.mainloop = GLib.MainLoop()
        self.confirm_pending = False
        self.last_motion_time = time.time()
        self.motion_timer = None
        self.stats_timer = None
        self.osd_timer = None
        self.splash_timer = None
        self._prev_cpu = (0, 0)
        self._frame_count = 0
        self._last_frame_count = 0
        self._fps_time = time.time()
        self._current_fps = 0.0
        self._exit_code = 0

        # Live adjustment OSD state
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

        # Inactivity countdown warning
        self._countdown_active = False

        # GPIO buttons
        self.button_a = Button(GPIO_BUTTON_A, pull_up=True, bounce_time=0.05)
        self.button_b = Button(GPIO_BUTTON_B, pull_up=True, bounce_time=0.05,
                               hold_time=LONG_PRESS_SECONDS)

        self.button_a.when_pressed = self._on_a_pressed
        self.button_b.when_pressed = self._on_b_pressed
        self.button_b.when_held = self._on_b_held
        self.button_b.when_released = self._on_b_released

        self._b_was_held = False

        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        print(f"Received signal {signum}, shutting down...")
        self.stop()

    def _auto_start_feed(self):
        """Auto-start the feed 5 seconds after boot."""
        if self.state == self.STATE_WELCOME:
            self._start_feed()
        return False  # don't repeat

    def start(self):
        print("Laparoscopic Camera Trainer starting...")
        self._show_welcome()
        # Auto-start feed after 5 seconds on first boot
        GLib.timeout_add_seconds(5, self._auto_start_feed)
        try:
            self.mainloop.run()
        except KeyboardInterrupt:
            self.stop()
        sys.exit(self._exit_code)

    def stop(self):
        print("Stopping...")
        self._stop_pipeline()
        self._stop_stats()
        if self.motion_timer:
            self.motion_timer.cancel()
        if self.osd_timer:
            self.osd_timer.cancel()
        if self.splash_timer:
            self.splash_timer.cancel()
        self.mainloop.quit()

    # -- HUD management --

    def _refresh_hud(self):
        """Combine all HUD elements into a single overlay text."""
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

    # -- Pipeline management (camera feed only) --

    def _stop_pipeline(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    def _start_pipeline(self, pipeline_str):
        self._stop_pipeline()
        self._frame_count = 0
        print(f"Pipeline: {pipeline_str}")
        try:
            self.pipeline = Gst.parse_launch(pipeline_str)
            bus = self.pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect('message::error', self._on_pipeline_error)

            # Attach frame counter probe to the sink pad
            sink = self.pipeline.get_by_name('fbdevsink0')
            if not sink:
                # Find the fbdevsink by iterating
                it = self.pipeline.iterate_sinks()
                _, sink = it.next()
            if sink:
                pad = sink.get_static_pad('sink')
                if pad:
                    pad.add_probe(Gst.PadProbeType.BUFFER, self._frame_probe)

            self.pipeline.set_state(Gst.State.PLAYING)
        except GLib.Error as e:
            print(f"Pipeline error: {e}")

    def _frame_probe(self, pad, info):
        """Pad probe callback — counts frames and samples for motion detection."""
        self._frame_count += 1

        # Sample every N frames for motion detection (convert ms to frames at ~20fps)
        poll_ms = int(self.config.get('motion_poll', '200ms').replace('ms', ''))
        poll_frames = max(1, int(poll_ms / 50))  # 50ms per frame at 20fps
        if self._frame_count % poll_frames == 0:
            buf = info.get_buffer()
            if buf:
                try:
                    success, mapinfo = buf.map(Gst.MapFlags.READ)
                    if success:
                        # Grab a small chunk from the middle of the frame
                        # (avoid edges which may have overlays)
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
        # Return to welcome on pipeline failure
        GLib.idle_add(self._show_welcome)

    # -- State transitions --

    def _show_welcome(self):
        self.state = self.STATE_WELCOME
        self._stop_pipeline()
        self._stop_stats()
        self._hud_stats_line = ''
        self._hud_splash_line = ''
        self._hud_osd_text = ''
        self._hud_countdown_line = ''
        # Unblank framebuffer in case kmssink was used
        try:
            with open('/sys/class/graphics/fb0/blank', 'w') as f:
                f.write('0')
        except (PermissionError, OSError):
            pass
        self.confirm_pending = False
        self.cursor_index = 0
        self._countdown_active = False
        if self.motion_timer:
            self.motion_timer.cancel()
            self.motion_timer = None

        display_size = RESOLUTION_MAP.get(self.config['resolution'], (1920, 1080))
        img = generate_welcome_image(display_size)
        write_to_framebuffer(img)
        print("State: WELCOME")

    def _show_settings(self):
        self.state = self.STATE_SETTINGS
        self.cursor_index = 0
        self.confirm_pending = False

        display_size = RESOLUTION_MAP.get(self.config['resolution'], (1920, 1080))
        img = generate_settings_image(self.config, self.cursor_index,
                                      self.confirm_pending, display_size)
        write_to_framebuffer(img)
        print("State: SETTINGS")

    def _refresh_settings(self):
        if self.state == self.STATE_SETTINGS:
            display_size = RESOLUTION_MAP.get(self.config['resolution'], (1920, 1080))
            img = generate_settings_image(self.config, self.cursor_index,
                                          self.confirm_pending, display_size)
            write_to_framebuffer(img)

    def _start_feed(self):
        self.state = self.STATE_LIVE
        self.last_motion_time = time.time()

        # Show loading message while pipeline starts
        display_size = RESOLUTION_MAP.get(self.config['resolution'], (1920, 1080))
        img = generate_message_image("Starting...", display_size)
        write_to_framebuffer(img)
        self._feed_start_time = time.time()

        pipeline_str = build_feed_pipeline(self.config)
        self._start_pipeline(pipeline_str)

        # Apply all saved camera adjustments
        device = self.config.get('camera_device', '/dev/video0')
        apply_all_adjustments(device, self.config)

        # Show splash
        self._show_splash()

        # Start stats update timer if enabled (updates textoverlay in pipeline)
        if self.config.get('stats') == 'on':
            self._start_stats()

        timeout = int(self.config.get('timeout_seconds', 300))
        if timeout > 0:
            self._schedule_motion_check()

        print("State: LIVE FEED")

    def _do_cli(self):
        print("Dropping to CLI mode...")
        display_size = RESOLUTION_MAP.get(self.config['resolution'], (1920, 1080))
        img = generate_message_image("CLI Mode — reboot to return", display_size)
        write_to_framebuffer(img)
        time.sleep(2)
        self._exit_code = EXIT_CODE_CLI
        self.stop()

    def _do_shutdown(self):
        print("Shutting down system...")
        display_size = RESOLUTION_MAP.get(self.config['resolution'], (1920, 1080))
        img = generate_message_image("Shutting down...", display_size)
        write_to_framebuffer(img)
        time.sleep(2)
        self.stop()
        os.system('sudo shutdown -h now')

    # -- Button handlers --

    def _on_a_pressed(self):
        """Button A — start feed, or back to welcome."""
        if self.state == self.STATE_WELCOME:
            self._start_feed()
        elif self.state == self.STATE_SETTINGS:
            self._show_welcome()
        elif self.state == self.STATE_LIVE:
            self._show_welcome()

    def _on_b_pressed(self):
        """Button B pressed (will check if held on release)."""
        self._b_was_held = False

    def _on_b_held(self):
        """Button B long press — change value (settings or feed)."""
        self._b_was_held = True

        if self.state == self.STATE_LIVE:
            self._adj_change_value()
            return

        if self.state != self.STATE_SETTINGS:
            return

        option = SETTINGS_OPTIONS[self.cursor_index]

        if option['key'] in ('shutdown', 'cli'):
            if self.confirm_pending:
                if option['key'] == 'shutdown':
                    self._do_shutdown()
                else:
                    self._do_cli()
            else:
                self.confirm_pending = True
                print(f"{option['label']}: hold again to confirm")
                self._refresh_settings()
            return

        if option['values']:
            current = self.config.get(option['key'])
            try:
                idx = option['values'].index(current)
                next_idx = (idx + 1) % len(option['values'])
            except ValueError:
                next_idx = 0
            self.config[option['key']] = option['values'][next_idx]
            save_config(self.config)
            print(f"Changed {option['key']} to {self.config[option['key']]}")
            self._refresh_settings()

    def _on_b_released(self):
        """Button B released — short press = navigate (settings), open settings (welcome), cycle exposure (feed)."""
        if self._b_was_held:
            return

        if self.state == self.STATE_WELCOME:
            self._show_settings()
        elif self.state == self.STATE_SETTINGS:
            self.cursor_index = (self.cursor_index + 1) % len(SETTINGS_OPTIONS)
            self.confirm_pending = False
            print(f"Navigate to: {SETTINGS_OPTIONS[self.cursor_index]['label']}")
            self._refresh_settings()
        elif self.state == self.STATE_LIVE:
            self._adj_next_category()

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
        if self.state != self.STATE_LIVE:
            return
        self.stats_timer = threading.Timer(1.0, self._update_stats)
        self.stats_timer.daemon = True
        self.stats_timer.start()

    def _update_stats(self):
        if self.state != self.STATE_LIVE:
            return
        if self.config.get('stats') != 'on':
            return
        if not self.pipeline:
            self._schedule_stats()
            return

        # Measure FPS from pad probe frame count
        now = time.time()
        elapsed = now - self._fps_time
        frames = self._frame_count - self._last_frame_count
        if elapsed > 0 and frames >= 0:
            self._current_fps = frames / elapsed
        self._last_frame_count = self._frame_count
        self._fps_time = now

        # Gather system stats
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

        # Build stats HUD line
        res = self.config.get('resolution', '1080p')
        idle = time.time() - self.last_motion_time
        motion_status = f"Motion: {self._last_motion_diff:.1f}"
        threshold = float(self.config.get('motion_threshold', '5'))
        if self._last_motion_diff > threshold:
            motion_status += " [ACTIVE]"
        else:
            motion_status += f" [IDLE {idle:.0f}s]"
        self._hud_stats_line = (f"{res}  "
                f"FPS: {self._current_fps:.1f}  "
                f"CPU: {cpu_pct:.0f}%  "
                f"Temp: {temp:.0f}C  "
                f"RAM: {mem_used}/{mem_total}MB  "
                f"{motion_status}")
        self._refresh_hud()

        self._schedule_stats()

    # -- Splash overlay --

    def _show_splash(self):
        """Show controls help for 3 seconds at feed start."""
        startup_time = time.time() - self._feed_start_time
        self._hud_splash_line = f'[A] Stop feed    [B] Adjust image    (started in {startup_time:.1f}s)'
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
        """Show/refresh the adjustment OSD on the live feed."""
        self._osd_visible = True
        self._hud_osd_text = build_osd_text(self._adj_category_idx, self.config)
        self._refresh_hud()
        self._reset_osd_timer()

    def _hide_osd(self):
        """Hide the adjustment OSD."""
        self._osd_visible = False
        self._hud_osd_text = ''
        self._refresh_hud()

    def _reset_osd_timer(self):
        """Reset the OSD auto-hide timer."""
        if self.osd_timer:
            self.osd_timer.cancel()
        self.osd_timer = threading.Timer(OSD_TIMEOUT, self._hide_osd)
        self.osd_timer.daemon = True
        self.osd_timer.start()

    def _adj_next_category(self):
        """Long B: cycle to next adjustment category."""
        self._adj_category_idx = (self._adj_category_idx + 1) % len(LIVE_ADJUSTMENTS)
        self._show_osd()
        adj = LIVE_ADJUSTMENTS[self._adj_category_idx]
        print(f"OSD category: {adj['label']}")

    def _adj_change_value(self):
        """Short B: cycle value in current adjustment category."""
        if not self._osd_visible:
            # First press just shows the OSD
            self._show_osd()
            return

        adj = LIVE_ADJUSTMENTS[self._adj_category_idx]
        # Compare as strings to handle mixed int/string config values
        current = str(self.config.get(adj['key'], adj['values'][0]))
        try:
            idx = adj['values'].index(current)
            next_idx = (idx + 1) % len(adj['values'])
        except ValueError:
            next_idx = 0

        new_val = adj['values'][next_idx]
        self.config[adj['key']] = new_val
        save_config(self.config)

        # Apply the change
        key = adj['key']
        if key in ('resolution', 'video_sink'):
            # Restart pipeline with new settings
            self._stop_pipeline()
            pipeline_str = build_feed_pipeline(self.config)
            self._start_pipeline(pipeline_str)
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
        # timeout_seconds, motion_threshold, motion_poll just need config saved (already done)

        self._show_osd()
        print(f"{adj['label']} changed to: {new_val}")
        print(f"{adj['label']} changed to: {new_val}")

    # -- Inactivity detection --

    def _schedule_motion_check(self):
        if self.state != self.STATE_LIVE:
            return
        self.motion_timer = threading.Timer(MOTION_SAMPLE_INTERVAL, self._check_motion)
        self.motion_timer.daemon = True
        self.motion_timer.start()

    def _check_motion(self):
        if self.state != self.STATE_LIVE:
            return

        timeout = int(self.config.get('timeout_seconds', 300))
        if timeout <= 0:
            return

        # Compare current frame sample to previous
        if self._motion_sample and self._prev_motion_sample:
            curr = np.frombuffer(self._motion_sample, dtype=np.uint8)
            prev = np.frombuffer(self._prev_motion_sample, dtype=np.uint8)
            min_len = min(len(curr), len(prev))
            if min_len > 0:
                diff = np.mean(np.abs(curr[:min_len].astype(np.int16) - prev[:min_len].astype(np.int16)))
                self._last_motion_diff = diff
                threshold = float(self.config.get('motion_threshold', '5'))
                if diff > threshold:
                    self.last_motion_time = time.time()

        self._prev_motion_sample = self._motion_sample

        elapsed = time.time() - self.last_motion_time
        remaining = timeout - elapsed

        if remaining <= 0:
            print(f"Inactivity timeout ({timeout}s) — returning to welcome")
            GLib.idle_add(self._show_welcome)
            return

        # Show countdown after 15 seconds of inactivity
        if elapsed >= 15:
            self._hud_countdown_line = f'Returning to menu in {int(remaining) + 1}...'
            self._countdown_active = True
            self._refresh_hud()
        elif self._countdown_active:
            # Motion resumed, clear warning
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
