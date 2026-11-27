"""Draws the DaVinci Director icon (needs Pillow):  python packaging/make_icon.py

A graphite tile in the style of DaVinci Resolve's UI: lines of a transcript coloured like speakers and
Resolve's red playhead (with its flag) cutting through them — "editing video as text".  Our own artwork,
not Blackmagic Design's logo.

Writes packaging/icon.ico (installer; 16–256 px, small sizes drawn simpler so they stay crisp),
davinci_director/web/icon.ico (window, taskbar, uninstall entry) and davinci_director/web/icon.png (page).
"""

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ORANGE, TEAL, PINK, GREY, RED = "#d8a657", "#7daea3", "#d3869b", "#9a9aa3", "#e64b3d"


def tile(size, simple):
    S = 1024  # drawn large, scaled down smoothly
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # graphite tile with a soft vertical gradient and a light top edge
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    top, bot = (0x3a, 0x3a, 0x43), (0x1a, 0x1a, 0x1f)
    for y in range(S):
        t = y / (S - 1)
        gd.line([(0, y), (S, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bot)) + (255,))
    mask = Image.new("L", (S, S), 0)
    pad, rad = 40, 210
    ImageDraw.Draw(mask).rounded_rectangle([pad, pad, S - pad, S - pad], radius=rad, fill=255)
    im.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([pad, pad, S - pad, S - pad], radius=rad, outline=(255, 255, 255, 40), width=10)

    def bar(x0, x1, y, h, color):
        d.rounded_rectangle([x0, y - h // 2, x1, y + h // 2], radius=h // 2, fill=color)

    if simple:  # 16–32 px: three thick lines and the playhead
        h = 104
        bar(220, 800, 330, h, ORANGE)
        bar(220, 640, 520, h, TEAL)
        bar(220, 720, 710, h, GREY)
        x, w = 560, 64
    else:
        h = 66
        bar(210, 470, 290, h, ORANGE)
        bar(510, 820, 290, h, ORANGE)
        bar(210, 360, 420, h, TEAL)
        bar(400, 780, 420, h, TEAL)
        bar(210, 540, 550, h, ORANGE)
        bar(580, 760, 550, h, PINK)
        bar(210, 420, 680, h, GREY)
        bar(460, 690, 680, h, GREY)
        bar(210, 480, 810, h, (0x6a, 0x6a, 0x74, 255))
        x, w = 600, 30
    # Resolve's playhead: a red line with a flag on top
    d.rectangle([x - w // 2, 200, x + w // 2, 860], fill=RED)
    fw, fh = (190, 150) if simple else (130, 110)
    d.polygon([(x - fw // 2, 150), (x + fw // 2, 150), (x + fw // 2, 150 + fh * 0.55), (x, 150 + fh),
               (x - fw // 2, 150 + fh * 0.55)], fill=RED)
    return im.resize((size, size), Image.LANCZOS)


def main():
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    imgs = [tile(s, simple=s <= 32) for s in sizes]
    ico = os.path.join(HERE, "icon.ico")  # installer
    imgs[-1].save(ico, format="ICO", sizes=[(s, s) for s in sizes], append_images=imgs[:-1])
    # window / taskbar / uninstall entry use the copy shipped with the app
    imgs[-1].save(os.path.join(ROOT, "davinci_director", "web", "icon.ico"), format="ICO",
                  sizes=[(s, s) for s in sizes], append_images=imgs[:-1])
    png = os.path.join(ROOT, "davinci_director", "web", "icon.png")
    imgs[-1].save(png)
    tile(512, simple=False).save(os.path.join(HERE, "icon-preview.png"))
    print("written:", ico, png)


if __name__ == "__main__":
    main()
