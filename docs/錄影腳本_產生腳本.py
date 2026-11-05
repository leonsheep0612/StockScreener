# -*- coding: utf-8 -*-
"""
產生「LINE 選股小幫手 操作示範錄影腳本」pptx。

每個場景一頁：左邊照著點（操作步驟），右邊照著唸（旁白）。旁白也寫進備忘稿，
錄影時開簡報者檢視就能當提詞。
做法比照 HW 油漆資料維護手冊的產生腳本：微軟正黑體、13.333 x 7.5 吋、右下角頁碼。
  字型直接設在佈景主題（英數和中文都是微軟正黑體），見檔尾 set_theme_font()。
  不在每段文字設 font.name：python-pptx 只會寫入英數字型，中文仍會用主題的新細明體。
配色用 LINE 綠＋卡片標題的淺黃，跟官方帳號畫面一致。

用法：python docs/錄影腳本_產生腳本.py [輸出檔]
"""
import sys
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

# ---------- 配色 ----------
INK    = RGBColor(0x1D, 0x24, 0x33)
BODY   = RGBColor(0x3A, 0x42, 0x52)
MUTED  = RGBColor(0x6B, 0x72, 0x80)
GREEN  = RGBColor(0x06, 0xC7, 0x55)
GREEN_INK = RGBColor(0x04, 0x83, 0x3A)
GREEN_SOFT = RGBColor(0xE6, 0xF8, 0xEE)
YELLOW = RGBColor(0xFF, 0xF4, 0xCC)
YELLOW_INK = RGBColor(0x6B, 0x5A, 0x1E)
CARD   = RGBColor(0xF4, 0xF5, 0xF7)
LINE   = RGBColor(0xDD, 0xE1, 0xE6)
RED    = RGBColor(0xC0, 0x39, 0x2B)
ALERT  = RGBColor(0xFD, 0xEC, 0xEA)
WHITE  = RGBColor(0xFF, 0xFF, 0xFF)
COVER_BG = RGBColor(0x1D, 0x24, 0x33)
ONDARK = RGBColor(0xC9, 0xCF, 0xD8)

FONT = "微軟正黑體"
TITLE = "LINE 選股小幫手　操作示範錄影腳本"

prs = Presentation()
prs.slide_width = Inches(13.333)
prs.slide_height = Inches(7.5)
BLANK = prs.slide_layouts[6]
_page = {"n": 0}


# ---------- 基礎工具 ----------
def rect(slide, x, y, w, h, color, rounded=False, radius=0.06, outline=None):
    kind = MSO_SHAPE.ROUNDED_RECTANGLE if rounded else MSO_SHAPE.RECTANGLE
    s = slide.shapes.add_shape(kind, Inches(x), Inches(y), Inches(w), Inches(h))
    if rounded:
        s.adjustments[0] = radius
    if color is None:
        s.fill.background()
    else:
        s.fill.solid()
        s.fill.fore_color.rgb = color
    if outline is None:
        s.line.fill.background()
    else:
        s.line.color.rgb = outline
        s.line.width = Pt(1)
    s.shadow.inherit = False
    return s


def oval(slide, x, y, d, color):
    s = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(x), Inches(y), Inches(d), Inches(d))
    s.fill.solid()
    s.fill.fore_color.rgb = color
    s.line.fill.background()
    s.shadow.inherit = False
    return s


def text(slide, x, y, w, h, runs, size=14, color=BODY, bold=False,
         align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, space_after=0, line_spacing=None):
    """runs 可以是字串，或 [(文字, {覆寫項}), ...]；\n 換段。"""
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = anchor
    if isinstance(runs, str):
        runs = [(runs, {})]

    paras, cur = [], []
    for t, opt in runs:
        for i, part in enumerate(t.split("\n")):
            if i > 0:
                paras.append(cur)
                cur = []
            if part:
                cur.append((part, opt))
    paras.append(cur)

    for k, pruns in enumerate(paras):
        p = tf.paragraphs[0] if k == 0 else tf.add_paragraph()
        p.alignment = align
        if space_after:
            p.space_after = Pt(space_after)
        if line_spacing:
            p.line_spacing = line_spacing
        for t, opt in pruns or [("", {})]:
            r = p.add_run()
            r.text = t
            r.font.size = Pt(opt.get("size", size))
            r.font.bold = opt.get("bold", bold)
            r.font.color.rgb = opt.get("color", color)
    return tb


def btn(label):
    """步驟裡的按鈕名稱：綠色粗體加【】，錄影時一眼找到要點哪裡。"""
    return (f"【{label}】", {"bold": True, "color": GREEN_INK})


def footer(slide):
    _page["n"] += 1
    text(slide, 0.6, 7.05, 8, 0.3, TITLE, size=10, color=MUTED)
    text(slide, 11.7, 7.05, 1.0, 0.3, str(_page["n"]), size=10, color=MUTED, align=PP_ALIGN.RIGHT)


def notes(slide, t):
    slide.notes_slide.notes_text_frame.text = t


def new_slide(kicker, title, time=None):
    s = prs.slides.add_slide(BLANK)
    rect(s, 0, 0, 13.333, 0.12, GREEN)
    text(s, 0.6, 0.45, 8, 0.35, kicker, size=14, bold=True, color=GREEN_INK)
    if time:
        rect(s, 11.2, 0.42, 1.55, 0.4, YELLOW, rounded=True, radius=0.5)
        text(s, 11.2, 0.42, 1.55, 0.4, time, size=12, bold=True, color=YELLOW_INK,
             align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    text(s, 0.6, 0.85, 12, 0.7, title, size=30, bold=True, color=INK)
    return s


def tip(slide, y, head, body, warn=False):
    rect(slide, 0.6, y, 0.07, 0.62, RED if warn else GREEN)
    text(slide, 0.85, y + 0.02, 11.8, 0.62,
         [(head + "　", {"bold": True, "color": INK}), (body, {})], size=13, color=MUTED,
         anchor=MSO_ANCHOR.MIDDLE)


def scene(kicker, title, time, steps, say, tip_text=None, warn=False):
    """場景頁：左邊操作步驟，右邊旁白；旁白同時放備忘稿。"""
    s = new_slide(kicker, title, time)
    text(s, 0.6, 1.75, 6, 0.3, "操作", size=12, bold=True, color=MUTED)

    y = 2.1
    row_h = 0.78 if len(steps) <= 4 else 0.66
    for n, runs in enumerate(steps, 1):
        rect(s, 0.6, y, 6.55, row_h - 0.1, CARD, rounded=True, radius=0.12)
        oval(s, 0.78, y + (row_h - 0.1 - 0.38) / 2, 0.38, GREEN)
        text(s, 0.78, y + (row_h - 0.1 - 0.38) / 2, 0.38, 0.38, str(n), size=13, bold=True,
             color=WHITE, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
        text(s, 1.35, y, 5.65, row_h - 0.1, runs if isinstance(runs, list) else [(runs, {})],
             size=14, color=INK, anchor=MSO_ANCHOR.MIDDLE)
        y += row_h

    rect(s, 7.5, 1.75, 5.23, 4.55 if tip_text else 5.05, YELLOW, rounded=True, radius=0.05)
    text(s, 7.8, 1.95, 4.7, 0.3, "旁白", size=12, bold=True, color=YELLOW_INK)
    text(s, 7.8, 2.35, 4.7, 3.8, "\n".join(say),
         size=15, color=INK, space_after=10, line_spacing=1.25)
    if tip_text:
        tip(s, 6.3, *tip_text, warn=warn)
    notes(s, "\n\n".join(say))
    footer(s)
    return s


# ============================================================
# 1. 封面
# ============================================================
s = prs.slides.add_slide(BLANK)
rect(s, 0, 0, 13.333, 7.5, COVER_BG)
oval(s, 10.6, 4.4, 4.4, GREEN)
oval(s, 11.9, -1.1, 2.2, YELLOW)
text(s, 0.9, 1.7, 9, 0.4, "操作示範錄影腳本", size=16, bold=True, color=GREEN)
text(s, 0.9, 2.2, 9.5, 1.8, "LINE 選股小幫手\n怎麼用，6 分鐘看懂", size=40, bold=True, color=WHITE,
     line_spacing=1.1)
text(s, 0.9, 4.2, 9, 0.9, "每頁一個場景：左邊照著點，右邊照著唸。\n旁白也在備忘稿裡，錄影時開「簡報者檢視」就能當提詞。",
     size=15, color=ONDARK, line_spacing=1.3)
for k, (label, value) in enumerate([("場景", "8 個"), ("總長", "約 6 分鐘"), ("錄法", "手機螢幕錄影")]):
    x = 0.9 + k * 2.55
    rect(s, x, 5.55, 2.35, 0.55, None, rounded=True, radius=0.5, outline=ONDARK)
    text(s, x, 5.55, 2.35, 0.55, [(label + "　", {"color": ONDARK}), (value, {"bold": True, "color": WHITE})],
         size=13, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
_page["n"] += 1

# ============================================================
# 2. 錄影前準備
# ============================================================
s = new_slide("開錄前", "先準備好，就不用重錄")
cards = [
    ("📱 手機", ["開啟勿擾模式，避免通知跳出來", "電量 50% 以上", "LINE 字體大小調回「標準」",
               "iPhone：控制中心 → 螢幕錄影（長按可開麥克風）", "Android：快速設定 → 螢幕錄影，勾選錄製麥克風"]),
    ("📊 資料", ["確認 17:00 排程已跑完，卡片上有資料日期", "先刪掉測試用的組合，只留四種策略",
               "想好場景 5 要新增的組合名稱，例如「成長股 ∩ 法人同步」"]),
    ("🎙 旁白", ["安靜的房間，手機離嘴巴約一個手掌", "每個場景錄完停 2 秒，方便剪接",
               "講錯不用停，從那句重講，剪輯時再剪掉", "也可以先錄畫面不出聲，之後再配音"]),
]
for k, (head, items) in enumerate(cards):
    x = 0.6 + k * 4.1
    rect(s, x, 1.8, 3.9, 4.2, CARD, rounded=True, radius=0.05)
    text(s, x + 0.3, 2.0, 3.4, 0.45, head, size=18, bold=True, color=INK)
    text(s, x + 0.3, 2.6, 3.35, 3.3, "\n".join("☐  " + i for i in items), size=13.5, color=BODY,
         space_after=8, line_spacing=1.2)
tip(s, 6.25, "畫面會被看到的東西：", "聊天室其他訊息、推播額度數字。介意的話先清空聊天記錄，或剪輯時打馬賽克。", warn=True)
footer(s)

# ============================================================
# 3. 場景總覽
# ============================================================
s = new_slide("場景總覽", "8 個場景，照順序錄")
rows = [
    ("1", "開場：這是什麼", "官方帳號聊天室＋圖文選單", "20 秒"),
    ("2", "查看結果卡片", "查看結果 → 左右滑", "50 秒"),
    ("3", "看完整名單、切換組合", "看最新結果 → 快速按鈕", "40 秒"),
    ("4", "用量與排程", "用量與排程", "20 秒"),
    ("5", "新增組合：選策略", "條件設定 → ＋ 新組合", "60 秒"),
    ("6", "改條件：區塊、強度、交集", "設定頁", "75 秒"),
    ("7", "推播設定與立即篩選", "設定頁 → 確認視窗", "45 秒"),
    ("8", "複製、刪除、還原＋收尾", "設定頁 → 聊天室", "30 秒"),
]
cols = [(0.6, 0.7), (1.3, 4.6), (5.9, 5.3), (11.2, 1.53)]
for (x, w), h in zip(cols, ["#", "場景", "畫面", "長度"]):
    text(s, x + 0.15, 1.8, w, 0.3, h, size=12, color=MUTED)
rect(s, 0.6, 2.15, 12.13, 0.03, LINE)
y = 2.25
for n, name, where, sec in rows:
    text(s, cols[0][0] + 0.15, y, cols[0][1], 0.5, n, size=16, bold=True, color=GREEN_INK, anchor=MSO_ANCHOR.MIDDLE)
    text(s, cols[1][0] + 0.15, y, cols[1][1], 0.5, name, size=15, bold=True, color=INK, anchor=MSO_ANCHOR.MIDDLE)
    text(s, cols[2][0] + 0.15, y, cols[2][1], 0.5, where, size=14, color=BODY, anchor=MSO_ANCHOR.MIDDLE)
    text(s, cols[3][0] + 0.15, y, cols[3][1], 0.5, sec, size=14, color=MUTED, anchor=MSO_ANCHOR.MIDDLE)
    rect(s, 0.6, y + 0.53, 12.13, 0.01, LINE)
    y += 0.56
footer(s)

# ============================================================
# 4～ 場景
# ============================================================
scene("場景 1 ／ 開場", "這是什麼", "約 20 秒",
      ["從 LINE 聊天列表點進官方帳號",
       "畫面停在聊天室，下方圖文選單完整露出",
       "手指依序指一下三個按鈕，不要點"],
      ["這是我做的選股小幫手，每個交易日下午四點半，它會依照我設定的條件，把上市櫃股票篩一遍。",
       "下面三個按鈕：看結果、改條件、看推播用量。看結果完全免費，想看幾次都可以。"],
      ("拍到：", "官方帳號名稱、三個選單按鈕"))

scene("場景 2 ／ 查看結果", "每個組合一張卡片", "約 50 秒",
      [[("點選單 ", {}), btn("📊 查看結果")],
       "停在第一張卡，讓資料日期和各區塊檔數入鏡",
       "慢慢往左滑，每張停 1 秒，滑到最後一張「＋ 新增條件組合」",
       "滑回 🐳 主力操作股"],
      ["按查看結果，每一組條件會變成一張卡片，可以左右滑。",
       "卡片上會寫資料日期，還有每個條件篩出幾檔股票。",
       "預設四種策略：長期成長股、中短線強勢股、主力操作股、高殖利率價值股。只有主力操作股每天推播，其他要看再點。"],
      ("拍到：", "卡片下方三個按鈕、沒推播的組合標著「🔕 不推播」"))

scene("場景 3 ／ 完整名單", "看名單、切換組合", "約 40 秒",
      [[("在主力操作股卡片按 ", {}), btn("📊 看最新結果")],
       "慢慢往上捲，讓名單停留 3 秒",
       "點訊息下方快速按鈕的另一組，例如「長期成長股」"],
      ["按看最新結果，就會列出這組條件今天篩出來的股票，還有關鍵數字。",
       "看完想換一組，直接點下面的按鈕就好，不用再回去滑卡片。"],
      ("沒結果時：", "會顯示「還沒有結果」，這段可以剪掉，或順勢帶到場景 7 的立即篩選。"))

scene("場景 4 ／ 用量", "推播額度一眼看", "約 20 秒",
      [[("點選單 ", {}), btn("📋 用量與排程")],
       "停 3 秒，讓「本月已用 / 剩餘」和「每週約推播幾則」看清楚"],
      ["LINE 免費方案一個月可以推播兩百則，這裡會顯示這個月用了多少、每週大概會推幾則。",
       "同一天多組一起推播只算一則，剩不到三則會自動停，不會超過。"],
      ("拍到：", "各組合推播日。介意額度數字可以剪輯時遮掉。"))

# 四種策略（場景 5 的參考，也可剪進影片當過場）
s = new_slide("場景 5 參考", "四種策略範本")
strats = [
    ("📈 長期成長股", "CAN SLIM｜持有半年～5 年", ["單季 EPS、營收年增", "年度 EPS 年增", "ROE、累季 EPS", "①～④ 強度一鍵套用"], "季報截止後看"),
    ("⚡ 中短線強勢股", "動能｜持有 1 週～3 個月", ["+DI／−DI／ADX", "布林 %b、帶寬", "日週月 KD", "5MA > 20MA"], "每 2～3 天看"),
    ("🐳 主力操作股", "跟隨籌碼｜持有數天～6 個月", ["千張大戶增加", "法人連續買超", "三個月累計同步", "外資投信佔比、單日大買"], "每週一看"),
    ("💰 高殖利率價值股", "安全邊際｜持有 1～3 年", ["現金股利、殖利率", "本益比、淨值比", "季線之上且上揚"], "3 月股利公告後看"),
]
for k, (name, sub, items, when) in enumerate(strats):
    x = 0.6 + k * 3.08
    rect(s, x, 1.8, 2.9, 4.3, WHITE, rounded=True, radius=0.05, outline=LINE)
    rect(s, x, 1.8, 2.9, 1.15, YELLOW, rounded=True, radius=0.12)
    text(s, x + 0.22, 1.95, 2.5, 0.4, name, size=17, bold=True, color=INK)
    text(s, x + 0.22, 2.42, 2.5, 0.4, sub, size=11.5, color=YELLOW_INK)
    text(s, x + 0.22, 3.15, 2.5, 2.2, "\n".join(items), size=13.5, color=BODY, space_after=6)
    text(s, x + 0.22, 5.55, 2.5, 0.35, when, size=12.5, bold=True, color=GREEN_INK)
tip(s, 6.3, "錄場景 5 時：", "不用每個都點開，選一個講就好。這頁可以剪進影片當過場畫面。")
footer(s)

scene("場景 5 ／ 新增組合", "從策略範本開始", "約 60 秒",
      [[("點選單 ", {}), btn("⚙️ 條件設定"), ("，等設定頁載入", {})],
       [("上方分頁最右邊點 ", {}), btn("＋ 新組合")],
       [("停 2 秒讓四種策略和「🧩 自訂組合」入鏡，再點 ", {}), btn("📈 長期成長股")],
       [("改名稱為「成長股 ∩ 法人同步」，", {}), ("先不要儲存", {"bold": True})]],
      ["想新增一組條件，到條件設定按新組合。",
       "可以從書上的四種策略開始，它會自動帶入書裡的條件；也可以選自訂組合，從空白開始自己搭。",
       "我選長期成長股，名字改一下，等一下再加一點條件。",
       "如果選自訂組合，它是空白的，要先加入至少一個條件區塊才能儲存。"],
      ("不要拍成這樣：", "點了「🧩 自訂組合」卻沒加區塊。空白組合會擋住之後每次儲存，點錯就按【🗑 刪除這組】再重錄。"),
      warn=True)

scene("場景 6 ／ 改條件", "加區塊、調強度、取交集", "約 75 秒",
      [[("在成長股區塊點 ", {}), btn("④ 較寬鬆"), ("，數字會跟著變", {})],
       [("捲到下方 ", {}), btn("＋ 加入其他條件區塊"), ("，選「三個月累計同步」", {})],
       [("回到上方，合併方式點 ", {}), btn("只列同時符合")],
       [("示範區塊右上角 ", {}), btn("✕"), ("可以移除（可略過）", {})],
       [("按 ", {}), btn("儲存")]],
      ["成長股有四種強度，一鍵就能切換，我選比較寬鬆的。",
       "任何組合都可以再加其他條件，我加上三個月法人同步買超。",
       "有兩個以上條件時，可以選分開列，或只列同時符合，這樣就是「有成長、法人又在買」的股票。"],
      ("儲存跳出「還沒加入條件區塊」：", "有一組是空白的。畫面會自動切過去，加個區塊或刪掉它，再從步驟 5 重錄。"),
      warn=True)

scene("場景 7 ／ 推播與立即篩選", "要不要推播，自己決定", "約 45 秒",
      ["上方「定期推播」點一下「週一」，再點掉（示範即可）",
       [("按下方 ", {}), btn("立即篩選這組")],
       "跳出確認視窗，停 2 秒讓「會用掉 1 則推播」入鏡",
       [("按 ", {}), btn("確定"), ("或取消都可以", {})]],
      ["想每天收到這組結果，就勾推播的星期幾；不勾就不推，要看再自己點，完全不花額度。",
       "剛改完條件想馬上看結果，可以按立即篩選，大概三到五分鐘會推播過來。這會用掉一則，所以會先問一次。"],
      ("按確定的話：", "1～2 分鐘後會收到推播，可以另外錄一段收到推播的畫面，剪在最後。"))

scene("場景 8 ／ 其他操作與收尾", "複製、刪除、還原", "約 30 秒",
      [[("指一下 ", {}), btn("⧉ 複製這組"), btn("🗑 刪除這組"), ("（不用真的按）", {})],
       [("隨便改一個數字，再按 ", {}), btn("還原")],
       [("關掉設定頁回聊天室，再點一次 ", {}), btn("📊 查看結果"), ("，新組合的卡片出現", {})]],
      ["同一個策略想試不同門檻，可以複製一組來改；不要的可以刪掉。改錯了按還原就好。",
       "回到聊天室，剛剛新增的組合已經出現在卡片裡，明天下午四點半就會有結果。以上就是選股小幫手的用法。"])

# ============================================================
# 剪輯與分享
# ============================================================
s = new_slide("錄完之後", "剪輯與分享")
boxes = [
    ("剪輯", "CapCut（剪映）", ["剪掉停頓、講錯、載入等待", "點按鈕的地方加放大或圓圈標示", "開「自動字幕」，靜音也看得懂",
                             "每個場景開頭加章節字卡（可直接截這份簡報）"]),
    ("放哪裡", "YouTube「不公開」", ["知道連結的人才看得到", "說明欄貼章節時間，可以直接跳段", "手機、電腦都能看，不佔 LINE 空間"]),
    ("怎麼給人", "影片＋手冊一起", ["影片：第一次看懂流程", "GitHub 上的使用手冊：之後查細節",
                               "兩個連結發成官方帳號的貼文並置頂，之後要找都在同一個地方"]),
]
for k, (who, head, items) in enumerate(boxes):
    x = 0.6 + k * 4.1
    rect(s, x, 1.8, 3.9, 4.3, CARD, rounded=True, radius=0.05)
    text(s, x + 0.3, 2.0, 3.4, 0.3, who, size=12, bold=True, color=GREEN_INK)
    text(s, x + 0.3, 2.35, 3.4, 0.45, head, size=19, bold=True, color=INK)
    text(s, x + 0.3, 3.0, 3.35, 3.0, "\n".join("•  " + i for i in items), size=13.5, color=BODY,
         space_after=8, line_spacing=1.2)
tip(s, 6.3, "章節時間範例：", "0:00 開場｜0:20 查看結果｜1:10 完整名單｜1:50 用量｜2:10 新增組合｜3:10 改條件｜4:25 推播與立即篩選｜5:10 收尾")
footer(s)


def set_theme_font(pres, face=FONT):
    """把佈景主題的標題／內文字型（英數 latin、中文 ea）都設成 face。
    文字方塊不另外指定字型，全部跟著主題走；在 PowerPoint 裡新增的文字也會是 face。"""
    from lxml import etree
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT
    from pptx.oxml.ns import qn
    theme = pres.slide_master.part.part_related_by(RT.THEME)
    root = etree.fromstring(theme.blob)
    for scheme in ("a:majorFont", "a:minorFont"):
        node = root.find(f".//{qn(scheme)}")
        for tag in ("a:latin", "a:ea"):
            node.find(qn(tag)).set("typeface", face)
    theme._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


set_theme_font(prs)

out = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).with_name("錄影腳本.pptx"))
prs.save(out)
print("written:", out, "slides:", len(prs.slides._sldIdLst))
