#!/usr/bin/env python3
"""Write a boot splash message directly to the framebuffer."""
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FB = '/dev/fb0'
FB_SIZE = '/sys/class/graphics/fb0/virtual_size'
FB_BPP = '/sys/class/graphics/fb0/bits_per_pixel'

try:
    with open(FB_SIZE) as f:
        parts = f.read().strip().split(',')
        w, h = int(parts[0]), int(parts[1])
except Exception:
    w, h = 1920, 1080

try:
    with open(FB_BPP) as f:
        bpp = int(f.read().strip())
except Exception:
    bpp = 16

img = Image.new('RGB', (w, h), (0, 0, 0))
draw = ImageDraw.Draw(img)

try:
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf', 24)
except OSError:
    font = ImageFont.load_default()

text = "Booting..."
bbox = draw.textbbox((0, 0), text, font=font)
tw = bbox[2] - bbox[0]
draw.text(((w - tw) // 2, h // 2 - 15), text, fill=(100, 100, 100), font=font)

arr = np.array(img, dtype=np.uint16)
r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
rgb565 = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)

with open(FB, 'wb') as fb:
    fb.write(rgb565.astype('<u2').tobytes())
