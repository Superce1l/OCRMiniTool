"""make_icon.py — 生成应用图标（icon.png / icon.ico / favicon.png）

与 webview/index.html 侧栏 logo 同款设计：圆角方块（扫描青）+ 文字行 +
放大镜。用 Pillow 直接绘制，无需外部 SVG 转换器。

用法：python assets/make_icon.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ACCENT = (14, 116, 144)        # #0E7490
ACCENT_LIGHT = (165, 227, 244)  # #A5E3F4
WHITE = (255, 255, 255)


def rounded_rect(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def draw_icon(size: int) -> Image.Image:
    s = size / 48.0
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角底板
    rounded_rect(d, [4 * s, 4 * s, 44 * s, 44 * s], 10 * s, ACCENT)

    def line(x0, y0, x1, y1, color, w):
        d.line([(x0 * s, y0 * s), (x1 * s, y1 * s)], fill=color, width=max(1, round(w * s)))
        r = w * s / 2
        for (px, py) in ((x0 * s, y0 * s), (x1 * s, y1 * s)):
            d.ellipse([px - r, py - r, px + r, py + r], fill=color)

    # 文字行（三条，模拟文档行）
    line(12, 15, 36, 15, WHITE, 3)
    line(12, 21, 25, 21, ACCENT_LIGHT, 3)
    line(12, 27, 21, 27, ACCENT_LIGHT, 3)

    # 放大镜
    cx, cy, r = 29, 30, 6.5
    d.ellipse([(cx - r) * s, (cy - r) * s, (cx + r) * s, (cy + r) * s],
              outline=WHITE, width=max(1, round(3 * s)))
    line(33.8, 34.8, 38, 39, WHITE, 3)
    return img


def main():
    out = Path(__file__).resolve().parent
    icon512 = draw_icon(512)
    icon512.save(out / "icon.png")
    icon512.resize((256, 256), Image.LANCZOS).save(out / "icon_256.png")
    icon512.save(out / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32),
                                          (48, 48), (64, 64), (128, 128), (256, 256)])
    draw_icon(32).save(out.parent / "webview" / "favicon.png")
    print(f"icons written to {out}")


if __name__ == "__main__":
    sys.exit(main())
