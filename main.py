import os
import requests
import json
import pandas as pd
from datetime import datetime

# 1. 取得環境變數中的金鑰
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.environ.get("LINE_USER_ID")

# 2. 篩選門檻（單位：張，1 張 = 1,000 股），可依需要調整
MIN_FOREIGN_LOTS = 1000  # 外資買超至少 1,000 張（參考書中「外資當日買超 >1,000 張」）
MIN_TRUST_LOTS = 100     # 投信買超至少 100 張
TOP_N = 10               # 訊息中顯示前幾名

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

def get_twse_sync_buy():
    """抓取三大法人買賣超，並加上防呆機制"""
    print("\n--- 開始抓取證交所資料 ---")
    url = "https://www.twse.com.tw/fund/T86?response=json&selectType=ALL"
    res = requests.get(url).json()
    
    # 防呆：檢查證交所今天有沒有資料（下午3點前通常沒資料）
    if res.get('stat') != 'OK':
        print(f"⚠️ 證交所 API 回傳異常: {res.get('stat')} (可能時間太早，尚未公布今日籌碼)")
        return pd.DataFrame()
        
    df = pd.DataFrame(res['data'], columns=res['fields'])
    
    # 證交所欄位名稱為「外陸資買賣超股數(不含外資自營商)」，統一改名方便後續使用
    foreign_col = next(c for c in df.columns if c.startswith('外陸資買賣超股數') or c == '外資買賣超股數')
    df = df.rename(columns={foreign_col: '外資買賣超股數'})

    # 清理逗號並轉為數值
    df['外資買賣超股數'] = pd.to_numeric(df['外資買賣超股數'].str.replace(',', ''), errors='coerce')
    df['投信買賣超股數'] = pd.to_numeric(df['投信買賣超股數'].str.replace(',', ''), errors='coerce')
    
    # 換算成張數（1 張 = 1,000 股）
    df['外資買超張數'] = df['外資買賣超股數'] // 1000
    df['投信買超張數'] = df['投信買賣超股數'] // 1000
    df['證券代號'] = df['證券代號'].str.strip()
    df['證券名稱'] = df['證券名稱'].str.strip()

    # 排除 ETF、ETN、權證等：只保留 4 位數、且不是 00 開頭的一般股票
    stock_df = df[df['證券代號'].str.fullmatch(r'[1-9]\d{3}')]

    # 篩選：外資與投信同步買超，且達到張數門檻
    sync_buy_df = stock_df[(stock_df['外資買超張數'] >= MIN_FOREIGN_LOTS) & (stock_df['投信買超張數'] >= MIN_TRUST_LOTS)]

    # 依外資＋投信合計買超張數排序，買最多的排前面
    sync_buy_df = sync_buy_df.assign(合計買超張數=sync_buy_df['外資買超張數'] + sync_buy_df['投信買超張數'])
    sync_buy_df = sync_buy_df.sort_values('合計買超張數', ascending=False)

    print(f"抓取完畢，共有 {len(sync_buy_df)} 檔符合條件（外資 ≥{MIN_FOREIGN_LOTS} 張、投信 ≥{MIN_TRUST_LOTS} 張）。")
    return sync_buy_df[['證券代號', '證券名稱', '外資買超張數', '投信買超張數', '合計買超張數']]

def main():
    print("啟動量化程式...")
    try:
        # 第一步：先發送一則純文字測試，確認 LINE 通訊正常
        send_line_message("🤖 系統報告：GitHub Actions 正在執行選股腳本...")

        # 第二步：抓取資料
        sync_buy_df = get_twse_sync_buy()
        
        today_str = datetime.now().strftime("%Y-%m-%d")
        msg = f"📊【盤後籌碼速報】{today_str}\n"
        msg += "-" * 20 + "\n"
        
        if sync_buy_df.empty:
            msg += "⚠️ 目前無符合標的。\n(註：若於下午 3:00 前執行，因今日籌碼尚未公布，皆會顯示無資料喔！)"
        else:
            msg += f"條件：外資 ≥{MIN_FOREIGN_LOTS:,} 張、投信 ≥{MIN_TRUST_LOTS:,} 張\n"
            msg += f"共 {len(sync_buy_df)} 檔，依合計買超排序前 {TOP_N} 名：\n\n"
            for index, row in sync_buy_df.head(TOP_N).iterrows():
                msg += f"[{row['證券代號']}] {row['證券名稱']}\n"
                msg += f"  外資 {row['外資買超張數']:,} 張｜投信 {row['投信買超張數']:,} 張\n"
                
        send_line_message(msg)

    except Exception as e:
        error_msg = f"⚠️ 量化腳本執行發生系統錯誤\n錯誤訊息：{str(e)}"
        print(error_msg)
        send_line_message(error_msg)

if __name__ == "__main__":
    main()
