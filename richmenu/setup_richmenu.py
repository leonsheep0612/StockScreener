"""LINE 官方帳號圖文選單：畫圖、上傳並設為預設選單

用法：
    python richmenu/setup_richmenu.py draw      # 只產生 richmenu/richmenu.png 預覽
    python richmenu/setup_richmenu.py upload    # 上傳 richmenu.png（沒有才重畫）並設為所有使用者的預設選單
上傳需要環境變數 LINE_CHANNEL_ACCESS_TOKEN 與 LIFF_ID；平常由 GitHub Actions 的 Deploy LINE Worker 執行
"""
import os
import sys
import math
import requests
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGE_PATH = os.path.join(HERE, "richmenu.png")
WIDTH, HEIGHT = 2500, 843  # LINE 規定的精簡版尺寸
MENU_NAME = "stock-screener"

# 三個按鈕：(標題, 說明, 圖示, 動作)
BUTTONS = [
    ("條件設定", "新增／修改／刪除組合", "gear", lambda liff: {"type": "uri", "uri": f"https://liff.line.me/{liff}"}),
    ("查看結果", "點選組合・不算額度", "chart", lambda liff: {"type": "postback", "data": "action=results", "displayText": "查看結果"}),
    ("用量與排程", "推播額度・推播日", "list", lambda liff: {"type": "postback", "data": "action=status", "displayText": "用量與排程"}),
]

BG_TOP, BG_BOTTOM = (16, 28, 46), (10, 18, 30)
CARD = (27, 42, 64)
ACCENT = (6, 199, 85)
WHITE, MUTED = (240, 244, 250), (150, 165, 190)

def font(size, bold=True):
    for name in (["msjhbd.ttc", "NotoSansTC-VF.ttf"] if bold else ["msjh.ttc", "NotoSansTC-VF.ttf"]):
        for folder in (r"C:\Windows\Fonts", "/usr/share/fonts/opentype/noto", "/System/Library/Fonts"):
            path = os.path.join(folder, name)
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)

def draw_gear(d, cx, cy, r):
    teeth = 8
    for i in range(teeth):
        a = i * 2 * math.pi / teeth
        x, y = cx + math.cos(a) * r * 0.95, cy + math.sin(a) * r * 0.95
        d.ellipse([x - r * 0.24, y - r * 0.24, x + r * 0.24, y + r * 0.24], fill=ACCENT)
    d.ellipse([cx - r * 0.85, cy - r * 0.85, cx + r * 0.85, cy + r * 0.85], fill=ACCENT)
    d.ellipse([cx - r * 0.38, cy - r * 0.38, cx + r * 0.38, cy + r * 0.38], fill=CARD)

def draw_chart(d, cx, cy, r):
    w = r * 0.42
    for i, h in enumerate([0.9, 1.4, 1.1, 1.8]):
        x = cx - r + i * (w + r * 0.11)
        d.rounded_rectangle([x, cy + r - h * r, x + w, cy + r], radius=w * 0.25, fill=ACCENT)
    d.line([cx - r, cy + r + 12, cx + r, cy + r + 12], fill=MUTED, width=8)

def draw_list(d, cx, cy, r):
    d.rounded_rectangle([cx - r * 0.85, cy - r, cx + r * 0.85, cy + r], radius=r * 0.18, fill=ACCENT)
    for i in range(3):
        y = cy - r * 0.5 + i * r * 0.5
        d.ellipse([cx - r * 0.55 - 10, y - 10, cx - r * 0.55 + 10, y + 10], fill=CARD)
        d.rounded_rectangle([cx - r * 0.3, y - 8, cx + r * 0.6, y + 8], radius=8, fill=CARD)

ICONS = {"gear": draw_gear, "chart": draw_chart, "list": draw_list}

def draw():
    img = Image.new("RGB", (WIDTH, HEIGHT))
    d = ImageDraw.Draw(img)
    for y in range(HEIGHT):  # 上下漸層背景
        t = y / HEIGHT
        d.line([(0, y), (WIDTH, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)))

    cell = WIDTH / len(BUTTONS)
    title_font, sub_font = font(112), font(58, bold=False)
    for i, (title, sub, icon, _) in enumerate(BUTTONS):
        x0 = i * cell
        d.rounded_rectangle([x0 + 36, 40, x0 + cell - 36, HEIGHT - 40], radius=56, fill=CARD)
        cx = x0 + cell / 2
        ICONS[icon](d, cx, 250, 120)
        d.text((cx, 520), title, font=title_font, fill=WHITE, anchor="mm")
        d.text((cx, 650), sub, font=sub_font, fill=MUTED, anchor="mm")
    img.save(IMAGE_PATH, optimize=True)
    print(f"已產生 {IMAGE_PATH}")

def upload():
    token, liff_id = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"), os.environ.get("LIFF_ID")
    if not token or not liff_id:
        sys.exit("請先設定環境變數 LINE_CHANNEL_ACCESS_TOKEN 與 LIFF_ID")
    auth = {"Authorization": f"Bearer {token}"}
    api = "https://api.line.me/v2/bot/richmenu"

    # 刪掉舊版同名選單，避免越積越多
    for menu in requests.get(f"{api}/list", headers=auth, timeout=30).json().get("richmenus", []):
        if menu["name"] == MENU_NAME:
            requests.delete(f"{api}/{menu['richMenuId']}", headers=auth, timeout=30)

    cell = WIDTH // len(BUTTONS)
    body = {
        "size": {"width": WIDTH, "height": HEIGHT}, "selected": True, "name": MENU_NAME, "chatBarText": "📈 選單",
        "areas": [{"bounds": {"x": i * cell, "y": 0, "width": cell, "height": HEIGHT}, "action": b[3](liff_id)}
                  for i, b in enumerate(BUTTONS)],
    }
    res = requests.post(api, headers=auth, json=body, timeout=30)
    res.raise_for_status()
    menu_id = res.json()["richMenuId"]
    with open(IMAGE_PATH, "rb") as f:
        requests.post(f"https://api-data.line.me/v2/bot/richmenu/{menu_id}/content",
                      headers={**auth, "Content-Type": "image/png"}, data=f.read(), timeout=60).raise_for_status()
    requests.post(f"https://api.line.me/v2/bot/user/all/richmenu/{menu_id}", headers=auth, timeout=30).raise_for_status()
    print(f"✅ 圖文選單已上傳並設為預設：{menu_id}")

if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "draw"
    # 上傳時直接用 repo 裡的圖（GitHub 主機沒有中文字型，重畫會變成方塊字）
    if command == "draw" or not os.path.exists(IMAGE_PATH):
        draw()
    if command == "upload":
        upload()
