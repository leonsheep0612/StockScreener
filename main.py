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

# 2. 篩選門檻（單位：張，1 張 = 1,000 股），可依需要調整
MIN_FOREIGN_LOTS = 1000  # 外資買超至少 1,000 張（參考書中「外資當日買超 >1,000 張」）
MIN_TRUST_LOTS = 100     # 投信買超至少 100 張
TOP_N = 10               # 訊息中顯示前幾名

# 3. 連續買超設定（參考書中「法人連續買超 2 週以上」）
CONSEC_DAYS = 10         # 連續買超的交易日數（2 週 ≈ 10 個交易日）

# 4. 千張大戶設定（參考書中「千張大戶週增 >8%」與「集保庫存張數不變」）
BIG_HOLDER_MIN_CHANGE = 8.0   # 千張大戶持股週增幅至少 8%
STOCK_CHANGE_TOLERANCE = 1.0  # 集保庫存週變動超過 1% 視為增資／合併，排除

DATA_DIR = "data"                                   # 交易日誌與集保快照存放位置
TDCC_DIR = os.path.join(DATA_DIR, "tdcc")
JOURNAL_PATH = os.path.join(DATA_DIR, "journal.csv")

TW_TZ = timezone(timedelta(hours=8))  # GitHub 主機是 UTC，日期一律用台灣時間
HEADERS = {"User-Agent": "Mozilla/5.0"}

def send_line_message(message):
    """使用 LINE Messaging API 發送 Push Message"""
    # 檢查金鑰是否有抓到
    if not LINE_TOKEN or not LINE_USER_ID:
        print("❌ 錯誤：找不到 LINE 金鑰，請檢查 GitHub Secrets 的名稱是否完全正確！")
        return

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

def to_number(series):
    """去掉千分位逗號並轉成數字"""
    return pd.to_numeric(series.astype(str).str.replace(',', ''), errors='coerce').fillna(0)

def normalize(df, market):
    """統一欄位格式：只留一般股票、加上張數與市場別"""
    df['證券代號'] = df['證券代號'].str.strip()
    df['證券名稱'] = df['證券名稱'].str.strip()
    df['外資買賣超股數'] = to_number(df['外資買賣超股數'])
    df['投信買賣超股數'] = to_number(df['投信買賣超股數'])
    df['外資買超張數'] = (df['外資買賣超股數'] // 1000).astype(int)
    df['投信買超張數'] = (df['投信買賣超股數'] // 1000).astype(int)
    df['市場'] = market

    # 排除 ETF、ETN、權證等：只保留 4 位數、且不是 00 開頭的一般股票
    df = df[df['證券代號'].str.fullmatch(r'[1-9]\d{3}')]
    return df[['證券代號', '證券名稱', '市場', '外資買賣超股數', '投信買賣超股數', '外資買超張數', '投信買超張數']]

def fetch_twse(date):
    """上市：證交所三大法人買賣超；非交易日或尚未公布時回傳 None"""
    url = f"https://www.twse.com.tw/fund/T86?response=json&date={date:%Y%m%d}&selectType=ALL"
    res = requests.get(url, headers=HEADERS, timeout=30).json()
    if res.get('stat') != 'OK':
        return None

    df = pd.DataFrame(res['data'], columns=res['fields'])

    # 證交所欄位名稱為「外陸資買賣超股數(不含外資自營商)」，統一改名方便後續使用
    foreign_col = next(c for c in df.columns if c.startswith('外陸資買賣超股數') or c == '外資買賣超股數')
    df = df.rename(columns={foreign_col: '外資買賣超股數'})
    return normalize(df, '上市')

def fetch_tpex(date):
    """上櫃：櫃買中心三大法人買賣超；非交易日或尚未公布時回傳 None"""
    url = f"https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={date:%Y/%m/%d}&response=json"
    res = requests.get(url, headers=HEADERS, timeout=30).json()
    tables = res.get('tables') or []
    if not tables or not tables[0].get('data'):
        return None

    # 櫃買欄位名稱重複，改用位置取值：
    # 0 代號、1 名稱、4 外資及陸資(不含外資自營商)買賣超、13 投信買賣超
    rows = tables[0]['data']
    df = pd.DataFrame({
        '證券代號': [r[0] for r in rows],
        '證券名稱': [r[1] for r in rows],
        '外資買賣超股數': [r[4] for r in rows],
        '投信買賣超股數': [r[13] for r in rows],
    })
    return normalize(df, '上櫃')

def fetch_day(date):
    """抓取某天上市＋上櫃的資料；以證交所判斷是否為交易日"""
    twse = fetch_twse(date)
    if twse is None:
        return None
    time.sleep(3)  # 證交所、櫃買中心都限制查詢頻率，太快會被暫時封鎖
    tpex = fetch_tpex(date)
    if tpex is None:
        print(f"  ⚠️ {date} 櫃買中心沒有資料，只用上市資料")
        return twse
    return pd.concat([twse, tpex], ignore_index=True)

def fetch_recent_days(n):
    """往回抓最近 n 個有資料的交易日，回傳 [(日期, DataFrame), ...]，最新的在前"""
    days = []
    date = datetime.now(TW_TZ).date()
    tries = 0
    while len(days) < n and tries < n * 2 + 10:
        df = fetch_day(date)
        if df is not None:
            days.append((date, df))
            print(f"  {date} ✔ 上市 {(df['市場'] == '上市').sum()} 檔、上櫃 {(df['市場'] == '上櫃').sum()} 檔")
        date -= timedelta(days=1)
        tries += 1
        time.sleep(3)
    return days

def screen_daily(df):
    """第 1 步：最新一天外資、投信同步大量買超"""
    sync = df[(df['外資買超張數'] >= MIN_FOREIGN_LOTS) & (df['投信買超張數'] >= MIN_TRUST_LOTS)]
    sync = sync.assign(合計買超張數=sync['外資買超張數'] + sync['投信買超張數'])
    return sync.sort_values('合計買超張數', ascending=False)

def screen_consecutive(days):
    """第 2 步：外資、投信「每一天」都買超（連續 CONSEC_DAYS 個交易日）"""
    merged = None
    for date, df in days:
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
    """近 N 日外資、投信「累計」都是買超的股票代號（用來和千張大戶交叉比對）"""
    all_days = pd.concat([df for _, df in days])
    total = all_days.groupby('證券代號')[['外資買賣超股數', '投信買賣超股數']].sum()
    return set(total[(total['外資買賣超股數'] > 0) & (total['投信買賣超股數'] > 0)].index)

def fetch_tdcc():
    """集保股權分散表（每週五更新），整理成每檔：千張大戶股數、比例、集保庫存總股數"""
    url = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
    text = requests.get(url, headers=HEADERS, timeout=60).content.decode('utf-8-sig')
    raw = pd.read_csv(io.StringIO(text), dtype=str)
    raw.columns = ['資料日期', '證券代號', '持股分級', '人數', '股數', '比例']
    raw['證券代號'] = raw['證券代號'].str.strip()
    raw = raw[raw['證券代號'].str.fullmatch(r'[1-9]\d{3}')]
    raw['股數'] = to_number(raw['股數'])
    raw['比例'] = to_number(raw['比例'])

    # 持股分級 15 = 1,000,001 股以上（千張大戶）；17 = 合計（集保庫存）
    big = raw[raw['持股分級'] == '15'].set_index('證券代號')
    total = raw[raw['持股分級'] == '17'].set_index('證券代號')
    snap = pd.DataFrame({
        '大戶股數': big['股數'],
        '大戶比例': big['比例'],
        '集保總股數': total['股數'],
    }).fillna(0).reset_index()
    return raw['資料日期'].iloc[0], snap

def load_tdcc_snapshots():
    """抓本週集保資料、存成快照，並讀取上一週的快照；回傳 (本週日期, 本週, 上週日期, 上週)"""
    os.makedirs(TDCC_DIR, exist_ok=True)
    cur_date, cur = fetch_tdcc()
    cur_path = os.path.join(TDCC_DIR, f"{cur_date}.csv")
    if not os.path.exists(cur_path):
        cur.to_csv(cur_path, index=False, encoding='utf-8-sig')
        print(f"  已儲存集保快照 {cur_path}")

    older = sorted(p for p in glob.glob(os.path.join(TDCC_DIR, "*.csv")) if os.path.basename(p) < f"{cur_date}.csv")
    if not older:
        return cur_date, cur, None, None
    prev_path = older[-1]
    prev = pd.read_csv(prev_path, dtype={'證券代號': str})
    return cur_date, cur, os.path.basename(prev_path)[:8], prev

def screen_big_holders(cur, prev, names):
    """第 3 步：千張大戶持股週增 ≥ BIG_HOLDER_MIN_CHANGE%，且集保庫存幾乎不變"""
    m = cur.merge(prev, on='證券代號', suffixes=('', '_上週'))
    m = m[m['大戶股數_上週'] > 0]
    m['大戶週增減%'] = (m['大戶股數'] - m['大戶股數_上週']) / m['大戶股數_上週'] * 100
    m['庫存週增減%'] = (m['集保總股數'] - m['集保總股數_上週']) / m['集保總股數_上週'] * 100

    # 驗證「集保庫存張數不變」，排除增資、合併造成的大戶比例虛增
    m = m[(m['大戶週增減%'] >= BIG_HOLDER_MIN_CHANGE) & (m['庫存週增減%'].abs() < STOCK_CHANGE_TOLERANCE)]
    m = m.merge(names, on='證券代號', how='left')
    return m.sort_values('大戶週增減%', ascending=False)

def append_journal(rows):
    """第 4 步：把每次的篩選結果寫進交易日誌（同一天、同類別、同代號不重複寫入）"""
    if not rows:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    new = pd.DataFrame(rows)
    if os.path.exists(JOURNAL_PATH):
        old = pd.read_csv(JOURNAL_PATH, dtype=str)
        new = pd.concat([old, new.astype(str)], ignore_index=True)
    new = new.drop_duplicates(subset=['資料日期', '類別', '證券代號'], keep='last')
    new.to_csv(JOURNAL_PATH, index=False, encoding='utf-8-sig')
    print(f"  交易日誌已更新：{JOURNAL_PATH}（共 {len(new)} 筆）")

def label(row):
    """訊息中的股票標籤，上櫃股票加註"""
    tag = "（櫃）" if row.get('市場') == '上櫃' else ""
    return f"[{row['證券代號']}] {row['證券名稱']}{tag}"

def main():
    print("啟動量化程式...")
    try:
        # 第一步：先發送一則純文字測試，確認 LINE 通訊正常
        send_line_message("🤖 系統報告：GitHub Actions 正在執行選股腳本...")

        # 第二步：抓取最近 CONSEC_DAYS 個交易日的資料（上市＋上櫃）
        print(f"\n--- 開始抓取近 {CONSEC_DAYS} 個交易日資料（上市＋上櫃）---")
        days = fetch_recent_days(CONSEC_DAYS)
        if not days:
            send_line_message("⚠️ 抓不到證交所資料，請稍後再試。")
            return

        latest_date, latest_df = days[0]
        names = pd.concat([df for _, df in days])[['證券代號', '證券名稱', '市場']].drop_duplicates('證券代號')
        journal = []
        msg = f"📊【盤後籌碼速報】資料日期 {latest_date}\n"
        msg += "範圍：上市＋上櫃（排除 ETF）\n"
        msg += "-" * 20 + "\n"

        # A. 連續買超（第 2 步）
        if len(days) < CONSEC_DAYS:
            msg += f"⚠️ 只抓到 {len(days)} 天資料，連續買超篩選略過。\n"
        else:
            consec = screen_consecutive(days)
            print(f"連續 {CONSEC_DAYS} 日同步買超：{len(consec)} 檔")
            msg += f"🏆 外資＋投信連續 {CONSEC_DAYS} 日同步買超（{days[-1][0]:%m/%d}～{latest_date:%m/%d}）\n"
            if consec.empty:
                msg += "無符合標的。\n"
            else:
                msg += f"共 {len(consec)} 檔，依累計買超排序：\n"
                for _, row in consec.head(TOP_N).iterrows():
                    msg += label(row) + "\n"
                    msg += f"  外資 {row['外資累計張數']:,} 張｜投信 {row['投信累計張數']:,} 張\n"
            for _, row in consec.iterrows():
                journal.append({'資料日期': latest_date, '類別': f'連續{CONSEC_DAYS}日同步買超', '證券代號': row['證券代號'],
                                '證券名稱': row['證券名稱'], '市場': row['市場'],
                                '外資張數': row['外資累計張數'], '投信張數': row['投信累計張數'], '大戶週增減%': ''})

        # B. 單日大量同步買超（第 1 步）
        daily = screen_daily(latest_df)
        print(f"單日大量同步買超：{len(daily)} 檔")
        msg += f"\n🔥 單日大量同步買超（外資 ≥{MIN_FOREIGN_LOTS:,} 張、投信 ≥{MIN_TRUST_LOTS:,} 張）\n"
        if daily.empty:
            msg += "無符合標的。\n"
        else:
            msg += f"共 {len(daily)} 檔，前 {TOP_N} 名：\n"
            for _, row in daily.head(TOP_N).iterrows():
                msg += label(row) + "\n"
                msg += f"  外資 {row['外資買超張數']:,} 張｜投信 {row['投信買超張數']:,} 張\n"
        for _, row in daily.iterrows():
            journal.append({'資料日期': latest_date, '類別': '單日大量同步買超', '證券代號': row['證券代號'],
                            '證券名稱': row['證券名稱'], '市場': row['市場'],
                            '外資張數': row['外資買超張數'], '投信張數': row['投信買超張數'], '大戶週增減%': ''})

        # C. 千張大戶（第 3 步）
        print("\n--- 開始抓取集保股權分散表 ---")
        try:
            cur_date, cur, prev_date, prev = load_tdcc_snapshots()
            msg += f"\n🐳 千張大戶週增 ≥{BIG_HOLDER_MIN_CHANGE:g}%（集保 {cur_date}）\n"
            if prev is None:
                msg += "本週是第一份集保快照，下週起才能比較週增減。\n"
            else:
                big = screen_big_holders(cur, prev, names)
                sync_set = cumulative_sync_buy(days)
                big['法人同步'] = big['證券代號'].isin(sync_set)
                print(f"千張大戶週增：{len(big)} 檔，其中法人同步買超 {big['法人同步'].sum()} 檔")
                msg += f"比較 {prev_date}→{cur_date}，已排除集保庫存變動 ≥{STOCK_CHANGE_TOLERANCE:g}% 的股票\n"
                if big.empty:
                    msg += "無符合標的。\n"
                else:
                    msg += f"共 {len(big)} 檔（⭐＝近 {len(days)} 日外資、投信累計同步買超，書中的超強組合）：\n"
                    # 超強組合排前面
                    big = big.sort_values(['法人同步', '大戶週增減%'], ascending=[False, False])
                    for _, row in big.head(TOP_N).iterrows():
                        star = "⭐" if row['法人同步'] else ""
                        name = row['證券名稱'] if isinstance(row['證券名稱'], str) else ""
                        msg += f"{star}[{row['證券代號']}] {name}\n"
                        msg += f"  大戶 +{row['大戶週增減%']:.1f}%｜大戶比例 {row['大戶比例']:.1f}%\n"
                for _, row in big.iterrows():
                    journal.append({'資料日期': latest_date, '類別': '千張大戶週增' + ('＋法人同步' if row['法人同步'] else ''),
                                    '證券代號': row['證券代號'], '證券名稱': row['證券名稱'], '市場': row['市場'],
                                    '外資張數': '', '投信張數': '', '大戶週增減%': round(row['大戶週增減%'], 2)})
        except Exception as e:
            print(f"⚠️ 集保資料處理失敗：{e}")
            msg += f"\n🐳 千張大戶：資料抓取失敗（{e}）\n"

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
