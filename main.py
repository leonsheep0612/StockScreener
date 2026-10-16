import os
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

TW_TZ = timezone(timedelta(hours=8))  # GitHub 主機是 UTC，日期一律用台灣時間

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

def fetch_t86(date):
    """抓取指定日期的三大法人買賣超；非交易日或尚未公布時回傳 None"""
    url = f"https://www.twse.com.tw/fund/T86?response=json&date={date:%Y%m%d}&selectType=ALL"
    res = requests.get(url, timeout=30).json()
    if res.get('stat') != 'OK':
        return None

    df = pd.DataFrame(res['data'], columns=res['fields'])

    # 證交所欄位名稱為「外陸資買賣超股數(不含外資自營商)」，統一改名方便後續使用
    foreign_col = next(c for c in df.columns if c.startswith('外陸資買賣超股數') or c == '外資買賣超股數')
    df = df.rename(columns={foreign_col: '外資買賣超股數'})

    # 清理逗號並轉為數值，再換算成張數（1 張 = 1,000 股）
    for col in ['外資買賣超股數', '投信買賣超股數']:
        df[col] = pd.to_numeric(df[col].str.replace(',', ''), errors='coerce').fillna(0)
    df['外資買超張數'] = (df['外資買賣超股數'] // 1000).astype(int)
    df['投信買超張數'] = (df['投信買賣超股數'] // 1000).astype(int)
    df['證券代號'] = df['證券代號'].str.strip()
    df['證券名稱'] = df['證券名稱'].str.strip()

    # 排除 ETF、ETN、權證等：只保留 4 位數、且不是 00 開頭的一般股票
    df = df[df['證券代號'].str.fullmatch(r'[1-9]\d{3}')]
    return df[['證券代號', '證券名稱', '外資買賣超股數', '投信買賣超股數', '外資買超張數', '投信買超張數']]

def fetch_recent_days(n):
    """往回抓最近 n 個有資料的交易日，回傳 [(日期, DataFrame), ...]，最新的在前"""
    days = []
    date = datetime.now(TW_TZ).date()
    tries = 0
    while len(days) < n and tries < n * 2 + 10:
        df = fetch_t86(date)
        if df is not None:
            days.append((date, df))
            print(f"  {date} ✔")
        date -= timedelta(days=1)
        tries += 1
        time.sleep(3)  # 證交所限制查詢頻率，太快會被暫時封鎖
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
        d = df[['證券代號', '證券名稱', '外資買賣超股數', '投信買賣超股數', '外資買超張數', '投信買超張數']]
        # 當天外資、投信都要是買超（股數 > 0）
        d = d[(d['外資買賣超股數'] > 0) & (d['投信買賣超股數'] > 0)]
        d = d[['證券代號', '證券名稱', '外資買超張數', '投信買超張數']]
        if merged is None:
            merged = d
        else:
            # 取交集，並累加張數
            merged = merged.merge(d, on='證券代號', suffixes=('', '_new'))
            merged['外資買超張數'] += merged.pop('外資買超張數_new')
            merged['投信買超張數'] += merged.pop('投信買超張數_new')
            merged = merged.drop(columns='證券名稱_new')
    merged = merged.rename(columns={'外資買超張數': '外資累計張數', '投信買超張數': '投信累計張數'})
    merged = merged.assign(合計累計張數=merged['外資累計張數'] + merged['投信累計張數'])
    return merged.sort_values('合計累計張數', ascending=False)

def main():
    print("啟動量化程式...")
    try:
        # 第一步：先發送一則純文字測試，確認 LINE 通訊正常
        send_line_message("🤖 系統報告：GitHub Actions 正在執行選股腳本...")

        # 第二步：抓取最近 CONSEC_DAYS 個交易日的資料
        print(f"\n--- 開始抓取證交所近 {CONSEC_DAYS} 個交易日資料 ---")
        days = fetch_recent_days(CONSEC_DAYS)
        if not days:
            send_line_message("⚠️ 抓不到證交所資料，請稍後再試。")
            return

        latest_date, latest_df = days[0]
        msg = f"📊【盤後籌碼速報】資料日期 {latest_date}\n"
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
                    msg += f"[{row['證券代號']}] {row['證券名稱']}\n"
                    msg += f"  外資 {row['外資累計張數']:,} 張｜投信 {row['投信累計張數']:,} 張\n"

        # B. 單日大量同步買超（第 1 步）
        daily = screen_daily(latest_df)
        print(f"單日大量同步買超：{len(daily)} 檔")
        msg += f"\n🔥 單日大量同步買超（外資 ≥{MIN_FOREIGN_LOTS:,} 張、投信 ≥{MIN_TRUST_LOTS:,} 張）\n"
        if daily.empty:
            msg += "無符合標的。\n"
        else:
            msg += f"共 {len(daily)} 檔，前 {TOP_N} 名：\n"
            for _, row in daily.head(TOP_N).iterrows():
                msg += f"[{row['證券代號']}] {row['證券名稱']}\n"
                msg += f"  外資 {row['外資買超張數']:,} 張｜投信 {row['投信買超張數']:,} 張\n"

        send_line_message(msg)

    except Exception as e:
        error_msg = f"⚠️ 量化腳本執行發生系統錯誤\n錯誤訊息：{str(e)}"
        print(error_msg)
        send_line_message(error_msg)

if __name__ == "__main__":
    main()
