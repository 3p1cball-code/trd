#!/usr/bin/env python3
"""Draw the TRD icon (dark terminal tile, amber prompt, a remote signal).

Writes PNGs into ~/.local/share/icons/hicolor/<size>x<size>/apps/trd.png so the
desktop entry and the task bar pick them up.
"""
import os
import sys

from PIL import Image, ImageDraw

BG = (22, 20, 18, 255)
EDGE = (58, 46, 34, 255)
SCREEN = (10, 10, 11, 255)
AMBER = (255, 138, 28, 255)
AMBER_DIM = (176, 96, 26, 255)
CYAN = (64, 196, 222, 255)


def draw(size):
    s = size
    img = Image.new('RGBA', (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = max(1, round(s * 0.03))
    r = max(2, round(s * 0.18))
    d.rounded_rectangle([pad, pad, s - 1 - pad, s - 1 - pad], radius=r, fill=BG, outline=EDGE,
                        width=max(1, s // 64))
    # the screen: a window inside the tile
    m = max(2, round(s * 0.16))
    top = max(2, round(s * 0.22))
    d.rounded_rectangle([m, top, s - 1 - m, s - 1 - m], radius=max(1, s // 22),
                        fill=SCREEN, outline=EDGE, width=max(1, s // 96) or 1)
    # title bar dots
    dot = max(1, s // 32)
    for i in range(3):
        cx = m + dot * 2 + i * dot * 3
        d.rectangle([cx, top + dot * 2, cx + dot, top + dot * 3], fill=AMBER_DIM if i else AMBER)
    # prompt  >_
    one = max(1, s // 32)                     # stroke width
    x0 = m + one * 2
    y0 = top + max(3, round(s * 0.16))
    h = max(2, round(s * 0.10))
    d.line([(x0, y0), (x0 + h, y0 + h // 2)], fill=AMBER, width=one)
    d.line([(x0 + h, y0 + h // 2), (x0, y0 + h)], fill=AMBER, width=one)
    d.rectangle([x0 + h + one * 2, y0 + h - one, x0 + h * 2 + one * 2, y0 + h], fill=AMBER)
    # two lines of "code"
    ly = y0 + h + one * 3
    lh = max(1, one)
    for n, frac in ((0, 0.55), (1, 0.38)):
        y = ly + n * (lh + one * 2)
        if y + lh < s - m:
            d.rectangle([x0, y, x0 + round((s - 2 * m) * frac), y + lh], fill=AMBER_DIM)
    # remote signal: three arcs in the upper right corner of the screen
    sw = max(1, s // 48)
    cx, cy = s - m - max(2, round(s * 0.10)), top + max(2, round(s * 0.08))
    for k, rad in enumerate((round(s * 0.035), round(s * 0.065), round(s * 0.095))):
        if rad < 2:
            continue
        d.arc([cx - rad, cy - rad, cx + rad, cy + rad], start=200, end=340, fill=CYAN,
              width=sw)
    d.ellipse([cx - sw, cy - sw, cx + sw, cy + sw], fill=CYAN)
    return img


def main():
    sizes = [16, 22, 24, 32, 48, 64, 128, 256]
    base = os.path.expanduser('~/.local/share/icons/hicolor')
    for s in sizes:
        d = os.path.join(base, '%dx%d' % (s, s), 'apps')
        os.makedirs(d, exist_ok=True)
        draw(s).save(os.path.join(d, 'trd.png'))
        print('wrote', os.path.join(d, 'trd.png'))
    draw(256).save(os.path.expanduser('~/.local/share/icons/trd.png'))


if __name__ == '__main__':
    sys.exit(main())
