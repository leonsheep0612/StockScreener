"""LINE channel 圖示（1024×1024）：上升的 K 線，漲的紅 K 上站著小牛、跌的綠 K 上站著小熊
重要內容都在中央圓形內，裁成圓形也不會被切掉"""
import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
SIZE, SCALE = 1024, 4  # 先畫 4 倍大再縮小，邊緣比較平滑
S = SIZE * SCALE
u = S / 100  # 以 100 為單位排版

BG_TOP, BG_BOTTOM = (27, 42, 64), (10, 18, 30)
UP_RED, DOWN_GREEN, BASE = (234, 67, 72), (6, 199, 85), (120, 140, 170)
INK, WHITE, BLUSH = (45, 30, 24), (255, 255, 255), (255, 140, 140, 140)
BULL, BULL_DARK, BULL_MUZZLE, HORN = (205, 138, 92), (176, 112, 70), (255, 214, 186), (255, 246, 222)
BEAR, BEAR_DARK, BEAR_MUZZLE = (124, 92, 72), (98, 70, 54), (222, 190, 160)

def box(cx, cy, w, h):
    return [(cx - w / 2) * u, (cy - h / 2) * u, (cx + w / 2) * u, (cy + h / 2) * u]

def eyes(d, x, y, gap):
    for side in (-1, 1):
        d.ellipse(box(x + side * gap, y, 2.6, 2.9), fill=INK)
        d.ellipse(box(x + side * gap + 0.5, y - 0.6, 0.9, 0.9), fill=WHITE)

def bull(d, x, y):
    """小牛頭：彎角、橫向的耳朵、淺色口鼻"""
    for side in (-1, 1):
        horn = [((x + side * 4) * u, (y - 4.5) * u), ((x + side * 6.5) * u, (y - 6.5) * u), ((x + side * 7) * u, (y - 9.5) * u)]
        d.line(horn, fill=HORN, width=int(2 * u), joint="curve")
        d.ellipse(box(x + side * 7, y - 9.5, 2, 2), fill=HORN)
        d.ellipse(box(x + side * 7.5, y - 1.5, 5, 3), fill=BULL_DARK)
    d.ellipse(box(x, y, 13, 12.5), fill=BULL)
    d.ellipse(box(x, y + 2.8, 8.5, 5.5), fill=BULL_MUZZLE)
    for side in (-1, 1):
        d.ellipse(box(x + side * 1.8, y + 2.3, 1.3, 1.6), fill=BULL_DARK)
    eyes(d, x, y - 1.8, 2.8)

def bear(d, x, y):
    """小熊頭：圓耳朵、淺色口鼻、黑鼻頭"""
    for side in (-1, 1):
        d.ellipse(box(x + side * 4.8, y - 5, 5, 5), fill=BEAR_DARK)
        d.ellipse(box(x + side * 4.8, y - 5, 2.6, 2.6), fill=BEAR_MUZZLE)
    d.ellipse(box(x, y, 13, 12.5), fill=BEAR)
    d.ellipse(box(x, y + 2.6, 7, 5), fill=BEAR_MUZZLE)
    d.ellipse(box(x, y + 1.6, 2.6, 1.8), fill=INK)
    eyes(d, x, y - 1.8, 2.8)

img = Image.new("RGBA", (S, S))
d = ImageDraw.Draw(img)
for y in range(S):  # 背景漸層
    t = y / S
    d.line([(0, y), (S, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)))

# 五根 K 線，整體往上：(中心 x, 實體上緣, 實體下緣, 是否上漲)
candles = [(20, 70, 80, False), (35, 60, 78, True), (50, 63, 72, False), (65, 48, 68, True), (80, 34, 60, True)]
d.line([(12 * u, 88 * u), (88 * u, 88 * u)], fill=BASE, width=int(1 * u))
for x, top, bottom, up in candles:
    color = UP_RED if up else DOWN_GREEN  # 台股紅漲綠跌
    d.line([(x * u, top * u), (x * u, (bottom + 5) * u)], fill=color, width=int(1.6 * u))
    d.rounded_rectangle([(x - 4.5) * u, top * u, (x + 4.5) * u, bottom * u], radius=1.5 * u, fill=color)
    (bull if up else bear)(d, x, top - 6.5)

# 腮紅（半透明）
blush = Image.new("RGBA", (S, S))
bd = ImageDraw.Draw(blush)
for x, top, _, _ in candles:
    for side in (-1, 1):
        bd.ellipse(box(x + side * 4.3, top - 6.5 + 1.2, 2.6, 1.6), fill=BLUSH)
img = Image.alpha_composite(img, blush)

img = img.convert("RGB").resize((SIZE, SIZE), Image.LANCZOS)
path = os.path.join(HERE, "icon.png")
img.save(path, optimize=True)
print(f"已產生 {path}")
