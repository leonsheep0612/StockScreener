import os
import io
import glob
import requests
import json
import time
import pandas as pd
from datetime import datetime, timedelta, timezone

# 1. 取得環境變數中的金鑰
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.environ.get("LINE_USER_ID")

# 2. 單日法人買超門檻（單位：張，1 張 = 1,000 股），可依需要調整
MIN_TRUST_LOTS = 100     # 投信買超至少 100 張
# 書中「外資當日買超佔成交比重＋張數」三組條件，符合任一組即可：(佔比 %, 張數)
FOREIGN_COMBOS = [(29, 1000), (15, 2000), (8, 4000)]
TOP_N = 10               # 訊息中每一區顯示前幾名

# 3. 連續買超設定（參考書中「法人連續買超 2 週以上」）
CONSEC_DAYS = 10         # 連續買超的交易日數（2 週 ≈ 10 個交易日）

# 4. 千張大戶設定（參考書中第 5 章）
BIG_HOLDER_MIN_CHANGE = 8.0       # 千張大戶持股週增幅至少 8%
BIG_HOLDER_SURGE = 15.0           # 單週大增 ≥15% 為加分訊號
BIG_HOLDER_RATIO_RANGE = (20, 80) # 大戶比例 20% 以上才有主力控盤，超過 80% 過度集中
SWEET_SPOT = (40, 70)             # 大戶比例甜蜜區
STOCK_CHANGE_TOLERANCE = 1.0      # 集保庫存週變動超過 1% 視為增資／合併，排除
MAX_CAPITAL = 50e8                # 股本 50 億以下的中小型股（股本 = 股數 × 面額 10 元）

# 5. 技術面與量能（參考書中 SOP「站上月線」、流動性與發動量）
REQUIRE_ABOVE_MA20 = True  # 收盤價要站上月線（20 日均線）
MIN_AVG_VOLUME_LOTS = 300  # 月均量至少 300 張，排除冷門股
VOLUME_SURGE = 4.0         # 當日成交量 ≥ 月均量 4 倍為發動訊號
HISTORY_DAYS = 21          # 計算月線與月均量需要的交易日數

DATA_DIR = "data"                                   # 交易日誌、快照存放位置
DAILY_DIR = os.path.join(DATA_DIR, "daily")         # 每日法人＋行情快取
TDCC_DIR = os.path.join(DATA_DIR, "tdcc")           # 每週集保快照
JOURNAL_PATH = os.path.join(DATA_DIR, "journal.csv")
KEEP_DAILY_FILES = 40                               # 每日快取保留天數

TW_TZ = timezone(timedelta(hours=8))  # GitHub 主機是 UTC，日期一律用台灣時間
HEADERS = {"User-Agent": "Mozilla/5.0"}

def send_line_message(message):
    """使用 LINE Messaging API 發送 Push Message"""
    # 檢查金鑰是否有抓到
    if not LINE_TOKEN or not LINE_USER_ID:
        print("❌ 錯誤：找不到 LINE 金鑰，請檢查 GitHub Secrets 的名稱是否完全正確！")
        return

    # LINE 單則訊息上限 5,000 字
    if len(message) > 4900:
        message = message[:4900] + "\n…（訊息過長已截斷）"

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_TOKEN}"
    }
    payload = {
        "to": LINE_USER_ID,
        "messages": [
            {
                "type": "text",
                "text": message
            }
        ]
    }

    print("\n--- 準備發送 LINE 訊息 ---")
    response = requests.post(url, headers=headers, data=json.dumps(payload))
    if response.status_code != 200:
        print(f"❌ 發送失敗: {response.status_code}")
        print(f"錯誤詳情: {response.text}")
    else:
        print("✅ LINE 訊息發送成功！")

def to_number(series, fill=0):
    """去掉千分位逗號並轉成數字；無法轉換的（例如 '---'）填入 fill"""
    s = pd.to_numeric(series.astype(str).str.replace(',', '').str.strip(), errors='coerce')
    return s if fill is None else s.fillna(fill)

def only_stocks(df):
    """排除 ETF、ETN、權證等：只保留 4 位數、且不是 00 開頭的一般股票"""
    df['證券代號'] = df['證券代號'].astype(str).str.strip()
    return df[df['證券代號'].str.fullmatch(r'[1-9]\d{3}')]

def get_json(url):
    res = requests.get(url, headers=HEADERS, timeout=30).json()
    time.sleep(3)  # 證交所、櫃買中心都限制查詢頻率，太快會被暫時封鎖
    return res

def fetch_twse_insti(date):
    """上市：證交所三大法人買賣超"""
    res = get_json(f"https://www.twse.com.tw/fund/T86?response=json&date={date:%Y%m%d}&selectType=ALL")
    if res.get('stat') != 'OK':
        return None
    df = pd.DataFrame(res['data'], columns=res['fields'])
    # 證交所欄位名稱為「外陸資買賣超股數(不含外資自營商)」
    foreign_col = next(c for c in df.columns if c.startswith('外陸資買賣超股數') or c == '外資買賣超股數')
    return pd.DataFrame({'證券代號': df['證券代號'], '證券名稱': df['證券名稱'].str.strip(),
                         '外資買賣超股數': to_number(df[foreign_col]), '投信買賣超股數': to_number(df['投信買賣超股數'])})

def fetch_tpex_insti(date):
    """上櫃：櫃買中心三大法人買賣超"""
    res = get_json(f"https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={date:%Y/%m/%d}&response=json")
    tables = res.get('tables') or []
    if not tables or not tables[0].get('data'):
        return None
    # 欄位名稱重複，改用位置：0 代號、1 名稱、4 外資及陸資(不含外資自營商)買賣超、13 投信買賣超
    rows = tables[0]['data']
    return pd.DataFrame({'證券代號': [r[0] for r in rows], '證券名稱': [r[1].strip() for r in rows],
                         '外資買賣超股數': to_number(pd.Series([r[4] for r in rows])),
                         '投信買賣超股數': to_number(pd.Series([r[13] for r in rows]))})

def fetch_twse_prices(date):
    """上市：證交所每日收盤行情"""
    res = get_json(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={date:%Y%m%d}&type=ALLBUT0999&response=json")
    if res.get('stat') != 'OK':
        return None
    table = next((t for t in res.get('tables', []) if t.get('fields') and '收盤價' in t['fields'] and '證券代號' in t['fields']), None)
    if table is None:
        return None
    df = pd.DataFrame(table['data'], columns=table['fields'])
    return pd.DataFrame({'證券代號': df['證券代號'], '成交股數': to_number(df['成交股數']),
                         '收盤價': to_number(df['收盤價'], fill=None)})

def fetch_tpex_prices(date):
    """上櫃：櫃買中心每日收盤行情"""
    res = get_json(f"https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={date:%Y/%m/%d}&type=EW&response=json")
    tables = res.get('tables') or []
    if not tables or not tables[0].get('data'):
        return None
    # 用位置取值：0 代號、2 收盤、7 成交股數
    rows = tables[0]['data']
    return pd.DataFrame({'證券代號': [r[0] for r in rows],
                         '成交股數': to_number(pd.Series([r[7] for r in rows])),
                         '收盤價': to_number(pd.Series([r[2] for r in rows]), fill=None)})

def fetch_day(date):
    """取得某天上市＋上櫃的法人買賣超與行情；優先讀快取，非交易日或資料不全回傳 None"""
    path = os.path.join(DAILY_DIR, f"{date:%Y%m%d}.csv")
    if os.path.exists(path):
        return pd.read_csv(path, dtype={'證券代號': str})

    twse_insti = fetch_twse_insti(date)
    if twse_insti is None:
        return None  # 非交易日或尚未公布
    parts = {'上櫃法人': fetch_tpex_insti(date), '上市行情': fetch_twse_prices(date), '上櫃行情': fetch_tpex_prices(date)}
    missing = [k for k, v in parts.items() if v is None]
    if missing:
        print(f"  ⚠️ {date} 缺少 {'、'.join(missing)}，這天先略過")
        return None

    twse = twse_insti.merge(parts['上市行情'], on='證券代號', how='left').assign(市場='上市')
    tpex = parts['上櫃法人'].merge(parts['上櫃行情'], on='證券代號', how='left').assign(市場='上櫃')
    df = only_stocks(pd.concat([twse, tpex], ignore_index=True))

    os.makedirs(DAILY_DIR, exist_ok=True)
    df.to_csv(path, index=False, encoding='utf-8-sig')
    return df

def prune_daily_cache():
    """只保留最近 KEEP_DAILY_FILES 天的每日快取，避免檔案越來越大"""
    files = sorted(glob.glob(os.path.join(DAILY_DIR, "*.csv")))
    for f in files[:-KEEP_DAILY_FILES]:
        os.remove(f)

def fetch_recent_days(n):
    """往回取得最近 n 個交易日，回傳 [(日期, DataFrame), ...]，最新的在前"""
    days = []
    date = datetime.now(TW_TZ).date()
    tries = 0
    while len(days) < n and tries < n * 2 + 10:
        if date.weekday() < 5:  # 週末直接跳過
            df = fetch_day(date)
            if df is not None:
                df['外資買超張數'] = (df['外資買賣超股數'] // 1000).astype(int)
                df['投信買超張數'] = (df['投信買賣超股數'] // 1000).astype(int)
                days.append((date, df))
                print(f"  {date} ✔ 上市 {(df['市場'] == '上市').sum()} 檔、上櫃 {(df['市場'] == '上櫃').sum()} 檔")
        date -= timedelta(days=1)
        tries += 1
    return days

def compute_metrics(days):
    """以最新一天為基準，計算每檔的收盤價、月線、月均量、量比"""
    hist = pd.concat([df.assign(日期=d) for d, df in days])
    close = hist.pivot_table(index='日期', columns='證券代號', values='收盤價').sort_index()
    vol = hist.pivot_table(index='日期', columns='證券代號', values='成交股數').sort_index()
    latest = days[0][0]

    m = pd.DataFrame({
        '收盤價': close.loc[latest] if latest in close.index else close.iloc[-1],
        '月線': close.tail(20).mean(),
        '月均量': vol.iloc[:-1].tail(20).mean() / 1000,  # 不含當天的前 20 日平均（張）
        '今日量': vol.iloc[-1] / 1000,
    })
    m['量比'] = m['今日量'] / m['月均量']
    m['站上月線'] = m['收盤價'] >= m['月線']
    return m.reset_index().rename(columns={'index': '證券代號'})

def apply_filters(df, metrics):
    """書中 SOP 的技術面與流動性過濾：站上月線、月均量 ≥ 300 張；回傳 (過濾後, 被排除檔數)"""
    overlap = [c for c in metrics.columns if c in df.columns and c != '證券代號']
    df = df.drop(columns=overlap).merge(metrics, on='證券代號', how='left')
    keep = df['月均量'] >= MIN_AVG_VOLUME_LOTS
    if REQUIRE_ABOVE_MA20:
        keep &= df['站上月線'].fillna(False).astype(bool)
    return df[keep], int((~keep).sum())

def screen_daily(df):
    """第 1 步：最新一天外資符合「佔成交比重＋張數」任一組，且投信同步買超"""
    df = df[df['成交股數'] > 0].copy()
    df['外資佔比%'] = df['外資買賣超股數'] / df['成交股數'] * 100
    foreign_ok = False
    for pct, lots in FOREIGN_COMBOS:
        foreign_ok = foreign_ok | ((df['外資佔比%'] >= pct) & (df['外資買超張數'] >= lots))
    sync = df[foreign_ok & (df['投信買超張數'] >= MIN_TRUST_LOTS)]
    sync = sync.assign(合計買超張數=sync['外資買超張數'] + sync['投信買超張數'])
    return sync.sort_values('合計買超張數', ascending=False)

def screen_consecutive(days):
    """第 2 步：外資、投信「每一天」都買超（連續 CONSEC_DAYS 個交易日）"""
    merged = None
    for date, df in days[:CONSEC_DAYS]:
        # 當天外資、投信都要是買超（股數 > 0）
        d = df[(df['外資買賣超股數'] > 0) & (df['投信買賣超股數'] > 0)]
        d = d[['證券代號', '證券名稱', '市場', '外資買超張數', '投信買超張數']]
        if merged is None:
            merged = d
        else:
            # 取交集，並累加張數
            merged = merged.merge(d[['證券代號', '外資買超張數', '投信買超張數']], on='證券代號', suffixes=('', '_new'))
            merged['外資買超張數'] += merged.pop('外資買超張數_new')
            merged['投信買超張數'] += merged.pop('投信買超張數_new')
    merged = merged.rename(columns={'外資買超張數': '外資累計張數', '投信買超張數': '投信累計張數'})
    merged = merged.assign(合計累計張數=merged['外資累計張數'] + merged['投信累計張數'])
    return merged.sort_values('合計累計張數', ascending=False)

def cumulative_sync_buy(days):
    """近 CONSEC_DAYS 日外資、投信「累計」都是買超的股票代號（用來和千張大戶交叉比對）"""
    all_days = pd.concat([df for _, df in days[:CONSEC_DAYS]])
    total = all_days.groupby('證券代號')[['外資買賣超股數', '投信買賣超股數']].sum()
    return set(total[(total['外資買賣超股數'] > 0) & (total['投信買賣超股數'] > 0)].index)

def fetch_tdcc():
    """集保股權分散表（每週五更新），整理成每檔：千張大戶股數、比例、集保庫存總股數"""
    url = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
    text = requests.get(url, headers=HEADERS, timeout=60).content.decode('utf-8-sig')
    raw = pd.read_csv(io.StringIO(text), dtype=str)
    raw.columns = ['資料日期', '證券代號', '持股分級', '人數', '股數', '比例']
    raw = only_stocks(raw)
    raw['股數'] = to_number(raw['股數'])
    raw['比例'] = to_number(raw['比例'])

    # 持股分級 15 = 1,000,001 股以上（千張大戶）；17 = 合計（集保庫存）
    big = raw[raw['持股分級'] == '15'].set_index('證券代號')
    total = raw[raw['持股分級'] == '17'].set_index('證券代號')
    snap = pd.DataFrame({
        '大戶股數': big['股數'],
        '大戶比例': big['比例'],
        '集保總股數': total['股數'],
    }).fillna(0).rename_axis('證券代號').reset_index()
    return raw['資料日期'].iloc[0], snap

def load_tdcc_snapshots():
    """抓本週集保資料存成快照，回傳所有快照 [(日期, DataFrame), ...]，最新的在前"""
    os.makedirs(TDCC_DIR, exist_ok=True)
    cur_date, cur = fetch_tdcc()
    cur_path = os.path.join(TDCC_DIR, f"{cur_date}.csv")
    if not os.path.exists(cur_path):
        cur.to_csv(cur_path, index=False, encoding='utf-8-sig')
        print(f"  已儲存集保快照 {cur_path}")

    snaps = []
    for p in sorted(glob.glob(os.path.join(TDCC_DIR, "*.csv")), reverse=True):
        snaps.append((os.path.basename(p)[:8], pd.read_csv(p, dtype={'證券代號': str})))
    return snaps

def screen_big_holders(snaps, names):
    """第 3 步：千張大戶週增 ≥8%、庫存不變、大戶比例 20～80%、股本 ≤50 億，並標記加分訊號"""
    cur, prev = snaps[0][1], snaps[1][1]
    m = cur.merge(prev, on='證券代號', suffixes=('', '_上週'))
    m = m[m['大戶股數_上週'] > 0]
    m['大戶週增減%'] = (m['大戶股數'] - m['大戶股數_上週']) / m['大戶股數_上週'] * 100
    m['庫存週增減%'] = (m['集保總股數'] - m['集保總股數_上週']) / m['集保總股數_上週'] * 100
    m['股本(億)'] = m['集保總股數'] * 10 / 1e8

    low, high = BIG_HOLDER_RATIO_RANGE
    m = m[(m['大戶週增減%'] >= BIG_HOLDER_MIN_CHANGE)
          & (m['庫存週增減%'].abs() < STOCK_CHANGE_TOLERANCE)  # 排除增資、合併造成的虛增
          & (m['大戶比例'].between(low, high))
          & (m['集保總股數'] * 10 <= MAX_CAPITAL)]

    # 加分：連續三週大戶股數都增加（需要 4 份快照）
    if len(snaps) >= 4:
        series = [s.set_index('證券代號')['大戶股數'] for _, s in snaps[:4]]
        up3 = (series[0] > series[1]) & (series[1] > series[2]) & (series[2] > series[3])
        m['連3週增'] = m['證券代號'].map(up3).fillna(False).astype(bool)
    else:
        m['連3週增'] = False
    m['單週大增'] = m['大戶週增減%'] >= BIG_HOLDER_SURGE
    m['甜蜜區'] = m['大戶比例'].between(*SWEET_SPOT)
    return m.merge(names, on='證券代號', how='left')

def append_journal(rows):
    """第 4 步：把每次的篩選結果寫進交易日誌（同一天、同類別、同代號不重複寫入）"""
    if not rows:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    new = pd.DataFrame(rows).astype(str)
    if os.path.exists(JOURNAL_PATH):
        old = pd.read_csv(JOURNAL_PATH, dtype=str)
        new = pd.concat([old, new], ignore_index=True)
    new = new.drop_duplicates(subset=['資料日期', '類別', '證券代號'], keep='last')
    new.to_csv(JOURNAL_PATH, index=False, encoding='utf-8-sig')
    print(f"  交易日誌已更新：{JOURNAL_PATH}（共 {len(new)} 筆）")

def label(row):
    """訊息中的股票標籤，上櫃股票加註"""
    tag = "（櫃）" if row.get('市場') == '上櫃' else ""
    name = row['證券名稱'] if isinstance(row.get('證券名稱'), str) else ""
    return f"[{row['證券代號']}] {name}{tag}"

def tech_note(row):
    """技術面、量能摘要"""
    note = f"  收 {row['收盤價']:g}｜月線 {row['月線']:.1f}｜量比 {row['量比']:.1f}"
    if row['量比'] >= VOLUME_SURGE:
        note += " 🚀"
    return note + "\n"

def journal_row(date, kind, row, **extra):
    base = {'資料日期': date, '類別': kind, '證券代號': row['證券代號'], '證券名稱': row.get('證券名稱', ''),
            '市場': row.get('市場', ''), '收盤價': row.get('收盤價', ''), '月線': round(row.get('月線', 0), 2),
            '量比': round(row.get('量比', 0), 2)}
    base.update(extra)
    return base

def main():
    print("啟動量化程式...")
    try:
        # 取得最近 HISTORY_DAYS 個交易日的法人買賣超與行情（上市＋上櫃）
        print(f"\n--- 取得近 {HISTORY_DAYS} 個交易日資料（上市＋上櫃）---")
        days = fetch_recent_days(HISTORY_DAYS)
        prune_daily_cache()
        if not days:
            send_line_message("⚠️ 抓不到證交所資料，請稍後再試。")
            return

        latest_date, latest_df = days[0]
        metrics = compute_metrics(days)
        names = pd.concat([df for _, df in days])[['證券代號', '證券名稱', '市場']].drop_duplicates('證券代號')
        journal = []
        filter_note = f"月均量 ≥{MIN_AVG_VOLUME_LOTS} 張" + ("、站上月線" if REQUIRE_ABOVE_MA20 else "")

        msg = f"📊【盤後籌碼速報】資料日期 {latest_date}\n"
        msg += f"範圍：上市＋上櫃（排除 ETF）\n共同過濾：{filter_note}\n"
        msg += "-" * 20 + "\n"

        # A. 連續買超（第 2 步）
        if len(days) < CONSEC_DAYS:
            msg += f"⚠️ 只抓到 {len(days)} 天資料，連續買超篩選略過。\n"
        else:
            consec, removed = apply_filters(screen_consecutive(days), metrics)
            print(f"連續 {CONSEC_DAYS} 日同步買超：{len(consec)} 檔（過濾掉 {removed} 檔）")
            msg += f"🏆 外資＋投信連續 {CONSEC_DAYS} 日同步買超（{days[CONSEC_DAYS - 1][0]:%m/%d}～{latest_date:%m/%d}）\n"
            if consec.empty:
                msg += f"無符合標的（過濾掉 {removed} 檔）。\n" if removed else "無符合標的。\n"
            else:
                msg += f"共 {len(consec)} 檔（過濾掉 {removed} 檔），依累計買超排序：\n"
                for _, row in consec.head(TOP_N).iterrows():
                    msg += label(row) + "\n"
                    msg += f"  外資 {row['外資累計張數']:,} 張｜投信 {row['投信累計張數']:,} 張\n"
                    msg += tech_note(row)
            for _, row in consec.iterrows():
                journal.append(journal_row(latest_date, f'連續{CONSEC_DAYS}日同步買超', row,
                                           外資張數=row['外資累計張數'], 投信張數=row['投信累計張數']))

        # B. 單日大量同步買超（第 1 步）
        daily, removed = apply_filters(screen_daily(latest_df), metrics)
        print(f"單日大量同步買超：{len(daily)} 檔（過濾掉 {removed} 檔）")
        combos = "、".join(f"{p}%＋{l:,}張" for p, l in FOREIGN_COMBOS)
        msg += f"\n🔥 單日外資大買＋投信同步（外資佔比＋張數符合 {combos} 任一組；投信 ≥{MIN_TRUST_LOTS} 張）\n"
        if daily.empty:
            msg += f"無符合標的（過濾掉 {removed} 檔）。\n" if removed else "無符合標的。\n"
        else:
            msg += f"共 {len(daily)} 檔（過濾掉 {removed} 檔），前 {TOP_N} 名：\n"
            for _, row in daily.head(TOP_N).iterrows():
                msg += label(row) + "\n"
                msg += f"  外資 {row['外資買超張數']:,} 張（佔 {row['外資佔比%']:.1f}%）｜投信 {row['投信買超張數']:,} 張\n"
                msg += tech_note(row)
        for _, row in daily.iterrows():
            journal.append(journal_row(latest_date, '單日大量同步買超', row, 外資張數=row['外資買超張數'],
                                       投信張數=row['投信買超張數'], **{'外資佔比%': round(row['外資佔比%'], 2)}))

        # C. 千張大戶（第 3 步）
        print("\n--- 開始抓取集保股權分散表 ---")
        try:
            snaps = load_tdcc_snapshots()
            cur_date = snaps[0][0]
            msg += f"\n🐳 千張大戶週增 ≥{BIG_HOLDER_MIN_CHANGE:g}%（集保 {cur_date}）\n"
            if len(snaps) < 2:
                msg += "目前只有一份集保快照，下週起才能比較週增減。\n"
            else:
                big, removed = apply_filters(screen_big_holders(snaps, names), metrics)
                sync_set = cumulative_sync_buy(days)
                big['法人同步'] = big['證券代號'].isin(sync_set)
                print(f"千張大戶週增：{len(big)} 檔（過濾掉 {removed} 檔），其中法人同步買超 {big['法人同步'].sum()} 檔")
                low, high = BIG_HOLDER_RATIO_RANGE
                msg += f"比較 {snaps[1][0]}→{cur_date}；條件：大戶比例 {low}～{high}%、股本 ≤{MAX_CAPITAL / 1e8:g} 億、集保庫存不變\n"
                if big.empty:
                    msg += f"無符合標的（過濾掉 {removed} 檔）。\n" if removed else "無符合標的。\n"
                else:
                    msg += f"共 {len(big)} 檔（過濾掉 {removed} 檔）\n"
                    msg += f"⭐法人同步 🔺連3週增 💥單週≥{BIG_HOLDER_SURGE:g}% 🍯比例{SWEET_SPOT[0]}～{SWEET_SPOT[1]}% 🚀量≥{VOLUME_SURGE:g}倍\n"
                    # 加分訊號越多排越前面
                    big['加分'] = big[['法人同步', '連3週增', '單週大增', '甜蜜區']].sum(axis=1) + (big['量比'] >= VOLUME_SURGE)
                    big = big.sort_values(['加分', '大戶週增減%'], ascending=[False, False])
                    for _, row in big.head(TOP_N).iterrows():
                        tags = ("⭐" if row['法人同步'] else "") + ("🔺" if row['連3週增'] else "") \
                             + ("💥" if row['單週大增'] else "") + ("🍯" if row['甜蜜區'] else "")
                        msg += f"{tags}{label(row)}\n"
                        msg += f"  大戶 +{row['大戶週增減%']:.1f}%｜比例 {row['大戶比例']:.1f}%｜股本 {row['股本(億)']:.1f} 億\n"
                        msg += tech_note(row)
                for _, row in big.iterrows():
                    journal.append(journal_row(latest_date, '千張大戶週增' + ('＋法人同步' if row['法人同步'] else ''), row,
                                               **{'大戶週增減%': round(row['大戶週增減%'], 2), '大戶比例%': row['大戶比例'],
                                                  '股本(億)': round(row['股本(億)'], 1)}))
        except Exception as e:
            print(f"⚠️ 集保資料處理失敗：{e}")
            msg += f"\n🐳 千張大戶：資料處理失敗（{e}）\n"

        # D. 交易日誌（第 4 步）
        append_journal(journal)
        msg += "\n📝 結果已存入交易日誌 data/journal.csv"

        send_line_message(msg)

    except Exception as e:
        error_msg = f"⚠️ 量化腳本執行發生系統錯誤\n錯誤訊息：{str(e)}"
        print(error_msg)
        send_line_message(error_msg)

if __name__ == "__main__":
    main()
