import os
import io
import glob
import requests
import json
import time
import pandas as pd
from datetime import date as Date, datetime, timedelta, timezone

# 1. 取得環境變數中的金鑰
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.environ.get("LINE_USER_ID")
# 從 LINE 設定頁按「立即篩選」時，GitHub Actions 會帶入該條件組合的 id，只推播這一組
ONLY_PRESET = os.environ.get("PRESET", "").strip()

# 2. 篩選條件：config.json 的 presets（條件組合），可從 LINE 官方帳號的「條件設定」頁新增、刪除、修改
#    四種策略範本與各區塊預設值在 strategies.json（依《用公開資訊從選股到精準進場》整理）
#    每個組合由多個區塊組成，combine = separate 各區塊分開列出、intersect 只列出同時符合所有區塊的股票
CONFIG_PATH = "config.json"
STRATEGIES_PATH = "strategies.json"
with open(STRATEGIES_PATH, encoding="utf-8") as f:
    BLANK_PRESET = json.load(f)["blank"]

def merge(base, override):
    """把 override 疊到 base 上（只合併一層 dict）"""
    result = json.loads(json.dumps(base))
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key].update(value)
        else:
            result[key] = value
    return result

def load_presets():
    """讀取 config.json 的條件組合，並以 strategies.json 的預設值補齊缺少的欄位"""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        presets = json.load(f)["presets"]
    return [merge(BLANK_PRESET, p) for p in presets]

PRESETS = load_presets()

# 需要多少天資料：法人買賣超（連續、累計、當月）與行情（月線、季線、週／月 KD）
INSTI_DAYS = max([25] + [p['consecutive']['days'] for p in PRESETS if p['consecutive']['enabled']]
                 + [p['cum_sync']['days'] for p in PRESETS if p['cum_sync']['enabled']])
PRICE_DAYS = max([21, INSTI_DAYS]
                 + [62 for p in PRESETS if p['value']['enabled']]
                 + [130 for p in PRESETS if p['strong']['enabled'] and p['strong']['kd_weekly']]
                 + [250 for p in PRESETS if p['strong']['enabled'] and p['strong']['kd_monthly']])

DATA_DIR = "data"                                   # 交易日誌、快照存放位置
DAILY_DIR = os.path.join(DATA_DIR, "daily")         # 每日法人＋行情快取
HOLIDAYS_PATH = os.path.join(DAILY_DIR, "holidays.txt")  # 已確認的休市日，免得每次重查
TDCC_DIR = os.path.join(DATA_DIR, "tdcc")           # 每週集保快照
FIN_DIR = os.path.join(DATA_DIR, "financials")      # 每季綜合損益表（累計營收、累計 EPS）
JOURNAL_PATH = os.path.join(DATA_DIR, "journal.csv")
RESULTS_PATH = os.path.join(DATA_DIR, "results.json")  # 各條件組合最新結果，LINE 點選查看時直接讀這份（回覆訊息免費）
KEEP_DAILY_FILES = PRICE_DAYS + 20                  # 每日快取保留天數
QUOTA_RESERVE = 3                                   # 本月推播剩不到幾則時就不再推播，避免超過免費額度

TW_TZ = timezone(timedelta(hours=8))  # GitHub 主機是 UTC，日期一律用台灣時間
TODAY = datetime.now(TW_TZ).date()
# 這次是哪個交易日收盤後的篩選：GitHub 備援排程可能延遲到隔天凌晨，中午前執行算前一天
RUN_DAY = TODAY - timedelta(days=1) if datetime.now(TW_TZ).hour < 12 else TODAY
HEADERS = {"User-Agent": "Mozilla/5.0"}

def line_api(method, path, **kwargs):
    return requests.request(method, f"https://api.line.me/v2/bot{path}",
                            headers={"Authorization": f"Bearer {LINE_TOKEN}"}, timeout=30, **kwargs)

def quota_left():
    """本月還能推播幾則；無上限或查詢失敗回傳 None"""
    try:
        quota = line_api("GET", "/message/quota").json()
        if quota.get("type") != "limited":
            return None
        used = line_api("GET", "/message/quota/consumption").json()["totalUsage"]
        return quota["value"] - used
    except Exception as e:
        print(f"⚠️ 查詢推播額度失敗：{e}")
        return None

def send_line_message(texts):
    """使用 LINE Messaging API 發送 Push Message；一次最多 5 個對話框，但只算 1 則額度"""
    # 檢查金鑰是否有抓到
    if not LINE_TOKEN or not LINE_USER_ID:
        print("❌ 錯誤：找不到 LINE 金鑰，請檢查 GitHub Secrets 的名稱是否完全正確！")
        return
    if isinstance(texts, str):
        texts = [texts]
    # 超過 5 段就把後面的併進第 5 段
    if len(texts) > 5:
        texts = texts[:4] + ["\n\n".join(texts[4:])]

    left = quota_left()
    if left is not None and left <= QUOTA_RESERVE:
        print(f"⚠️ 本月推播額度只剩 {left} 則，這次不推播（結果仍可在 LINE 選單點選查看）")
        return

    # LINE 單則訊息上限 5,000 字
    messages = [{"type": "text", "text": t if len(t) <= 4900 else t[:4900] + "\n…（訊息過長已截斷）"} for t in texts]

    print("\n--- 準備發送 LINE 訊息 ---")
    response = line_api("POST", "/message/push", json={"to": LINE_USER_ID, "messages": messages})
    if response.status_code != 200:
        print(f"❌ 發送失敗: {response.status_code}")
        print(f"錯誤詳情: {response.text}")
    else:
        print(f"✅ LINE 訊息發送成功！（{len(messages)} 個對話框，算 1 則）")

# ---------- 資料抓取 ----------

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

def find_col(columns, *keywords, exclude=()):
    """依關鍵字找欄位（欄位名稱常有空白、全半形差異）；找不到回傳 None"""
    for kw in keywords:
        for c in columns:
            name = str(c).replace(' ', '')
            if kw in name and not any(x in name for x in exclude):
                return c
    return None

def table_from(res, *must_have):
    """從證交所／櫃買中心 JSON 取出含有指定欄位的表格，轉成 DataFrame"""
    candidates = [res] + list(res.get('tables') or [])
    for t in candidates:
        fields, data = t.get('fields'), t.get('data')
        if fields and data and all(find_col(fields, k) is not None for k in must_have):
            return pd.DataFrame(data, columns=[str(f).strip() for f in fields])
    return None

def fetch_twse_insti(date):
    """上市：證交所三大法人買賣超"""
    res = get_json(f"https://www.twse.com.tw/fund/T86?response=json&date={date:%Y%m%d}&selectType=ALL")
    if res.get('stat') != 'OK':
        return None
    df = pd.DataFrame(res['data'], columns=res['fields'])
    # 證交所欄位名稱為「外陸資買賣超股數(不含外資自營商)」
    foreign_col = next(c for c in df.columns if c.startswith('外陸資買賣超股數') or c == '外資買賣超股數')
    return pd.DataFrame({'證券代號': df['證券代號'],
                         '外資買賣超股數': to_number(df[foreign_col]), '投信買賣超股數': to_number(df['投信買賣超股數'])})

def fetch_tpex_insti(date):
    """上櫃：櫃買中心三大法人買賣超"""
    res = get_json(f"https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={date:%Y/%m/%d}&response=json")
    tables = res.get('tables') or []
    if not tables or not tables[0].get('data'):
        return None
    # 欄位名稱重複，改用位置：0 代號、1 名稱、4 外資及陸資(不含外資自營商)買賣超、13 投信買賣超
    rows = tables[0]['data']
    return pd.DataFrame({'證券代號': [r[0] for r in rows],
                         '外資買賣超股數': to_number(pd.Series([r[4] for r in rows])),
                         '投信買賣超股數': to_number(pd.Series([r[13] for r in rows]))})

def fetch_twse_prices(date):
    """上市：證交所每日收盤行情；回傳 (DataFrame 或 None, 是否確定休市)"""
    res = get_json(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={date:%Y%m%d}&type=ALLBUT0999&response=json")
    if res.get('stat') != 'OK':
        return None, '沒有符合' in str(res.get('stat'))
    df = table_from(res, '證券代號', '收盤價')
    if df is None:
        return None, False
    return pd.DataFrame({'證券代號': df['證券代號'], '證券名稱': df['證券名稱'].str.strip(),
                         '開盤價': to_number(df['開盤價'], fill=None), '最高價': to_number(df['最高價'], fill=None),
                         '最低價': to_number(df['最低價'], fill=None), '收盤價': to_number(df['收盤價'], fill=None),
                         '成交股數': to_number(df['成交股數'])}), False

def fetch_tpex_prices(date):
    """上櫃：櫃買中心每日收盤行情"""
    res = get_json(f"https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={date:%Y/%m/%d}&type=EW&response=json")
    tables = res.get('tables') or []
    if not tables or not tables[0].get('data'):
        return None
    # 優先用欄位名稱，找不到再用位置：0 代號、1 名稱、2 收盤、4 開盤、5 最高、6 最低、7 成交股數
    fields = tables[0].get('fields') or []
    def col(keyword, default):
        found = find_col(fields, keyword)
        return fields.index(found) if found is not None else default
    idx = {'收盤價': col('收盤', 2), '開盤價': col('開盤', 4), '最高價': col('最高', 5),
           '最低價': col('最低', 6), '成交股數': col('成交股數', 7)}
    rows = tables[0]['data']
    df = pd.DataFrame({'證券代號': [r[0] for r in rows], '證券名稱': [r[1].strip() for r in rows]})
    for name, i in idx.items():
        df[name] = to_number(pd.Series([r[i] for r in rows]), fill=0 if name == '成交股數' else None)
    return df

def load_holidays():
    if not os.path.exists(HOLIDAYS_PATH):
        return set()
    with open(HOLIDAYS_PATH, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}

def save_holidays(holidays):
    os.makedirs(DAILY_DIR, exist_ok=True)
    with open(HOLIDAYS_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(holidays)) + "\n")

def fetch_day(date, need_insti):
    """取得某天上市＋上櫃的行情（含開高低收）與法人買賣超；優先讀快取
    回傳 (DataFrame 或 None, 是否確定休市)。舊快取缺開高低或缺法人資料時只補抓缺的部分"""
    path = os.path.join(DAILY_DIR, f"{date:%Y%m%d}.csv")
    cached = pd.read_csv(path, dtype={'證券代號': str}) if os.path.exists(path) else None
    has_prices = cached is not None and '最高價' in cached.columns
    has_insti = cached is not None and '外資買賣超股數' in cached.columns
    if has_prices and (has_insti or not need_insti):
        return cached, False

    if has_prices:
        prices = cached
    else:
        twse, holiday = fetch_twse_prices(date)
        if twse is None:
            return None, holiday  # 非交易日或尚未公布
        tpex = fetch_tpex_prices(date)
        if tpex is None:
            print(f"  ⚠️ {date} 缺少上櫃行情，這天先略過")
            return None, False
        prices = only_stocks(pd.concat([twse.assign(市場='上市'), tpex.assign(市場='上櫃')], ignore_index=True))

    insti_cols = ['外資買賣超股數', '投信買賣超股數']
    if has_insti:
        insti = cached[['證券代號'] + insti_cols]
    elif need_insti:
        twse_insti, tpex_insti = fetch_twse_insti(date), fetch_tpex_insti(date)
        if twse_insti is None or tpex_insti is None:
            print(f"  ⚠️ {date} 缺少法人買賣超，這天先略過")
            return None, False
        insti = pd.concat([twse_insti, tpex_insti], ignore_index=True)
    else:
        insti = None

    df = prices.drop(columns=[c for c in insti_cols if c in prices.columns])
    if insti is not None:
        insti = insti.assign(證券代號=insti['證券代號'].astype(str).str.strip()).drop_duplicates('證券代號')
        df = df.merge(insti, on='證券代號', how='left')
        df[insti_cols] = df[insti_cols].fillna(0)

    os.makedirs(DAILY_DIR, exist_ok=True)
    df.to_csv(path, index=False, encoding='utf-8-sig')
    return df, False

def prune_daily_cache():
    """只保留最近 KEEP_DAILY_FILES 天的每日快取，避免檔案越來越大"""
    files = sorted(glob.glob(os.path.join(DAILY_DIR, "2*.csv")))
    for f in files[:-KEEP_DAILY_FILES]:
        os.remove(f)

def fetch_recent_days(n, insti_n):
    """往回取得最近 n 個交易日（最近 insti_n 天含法人買賣超），回傳 [(日期, DataFrame), ...]，最新的在前"""
    days = []
    holidays = load_holidays()
    date = TODAY
    tries = 0
    while len(days) < n and tries < n * 2 + 20:
        if date.weekday() < 5 and str(date) not in holidays:  # 週末、已知休市日直接跳過
            df, holiday = fetch_day(date, need_insti=len(days) < insti_n)
            if df is not None:
                if '外資買賣超股數' in df.columns:
                    df['外資買超張數'] = (df['外資買賣超股數'] // 1000).astype(int)
                    df['投信買超張數'] = (df['投信買賣超股數'] // 1000).astype(int)
                days.append((date, df))
                if len(days) <= 3 or len(days) % 50 == 0:
                    print(f"  {date} ✔ 上市 {(df['市場'] == '上市').sum()} 檔、上櫃 {(df['市場'] == '上櫃').sum()} 檔（第 {len(days)} 天）")
            elif holiday and date < TODAY:
                holidays.add(str(date))
        date -= timedelta(days=1)
        tries += 1
    save_holidays(holidays)
    return days

def fetch_valuation(date):
    """本益比、股價淨值比、殖利率、每股現金股利（上市＋上櫃）"""
    twse = table_from(get_json(f"https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d?date={date:%Y%m%d}&selectType=ALL&response=json"),
                      '代號', '本益比')
    tpex_res = get_json(f"https://www.tpex.org.tw/www/zh-tw/afterTrading/peQryDate?date={date:%Y/%m/%d}&response=json")
    tpex = table_from(tpex_res, '代號', '本益比')
    if tpex is None:  # 備援：櫃買中心 OpenAPI
        rows = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis", headers=HEADERS, timeout=30).json()
        tpex = pd.DataFrame(rows).rename(columns={'SecuritiesCompanyCode': '代號', 'PriceEarningRatio': '本益比',
                                                  'DividendPerShare': '每股股利', 'YieldRatio': '殖利率(%)',
                                                  'PriceBookRatio': '股價淨值比'})
    parts = []
    for df in (twse, tpex):
        if df is None:
            continue
        cols = df.columns
        out = pd.DataFrame({'證券代號': df[find_col(cols, '代號')].astype(str).str.strip(),
                            '本益比': to_number(df[find_col(cols, '本益比')], fill=None),
                            '股價淨值比': to_number(df[find_col(cols, '淨值比')], fill=None),
                            '殖利率%': to_number(df[find_col(cols, '殖利率')], fill=None)})
        div_col = find_col(cols, '每股股利', '股利', exclude=('年度',))
        out['現金股利'] = to_number(df[div_col], fill=None) if div_col is not None else None
        parts.append(out)
    if not parts:
        raise RuntimeError("抓不到本益比／殖利率資料")
    return only_stocks(pd.concat(parts, ignore_index=True)).drop_duplicates('證券代號')

# ---------- 財報（綜合損益表彙總，公開資訊觀測站） ----------

def latest_season(today):
    """依財報公告期限（5/15、8/14、11/14、3/31）判斷目前最新可用的一季"""
    y = today.year
    if today > Date(y, 11, 14):
        return y, 3
    if today > Date(y, 8, 14):
        return y, 2
    if today > Date(y, 5, 15):
        return y, 1
    if today > Date(y, 3, 31):
        return y - 1, 4
    return y - 1, 3

def season_shift(y, s, n):
    """往前推 n 季"""
    i = y * 4 + (s - 1) - n
    return i // 4, i % 4 + 1

REVENUE_NAMES = ['營業收入', '淨收益', '收益合計', '收入合計', '營業收益', '收益', '收入']

def find_revenue_col(columns):
    """營收欄位：各產業表格名稱不同（一般業「營業收入」、金控「淨收益」、證券「收益」…）
    先找完全相同的名稱，避免把銀行的「利息淨收益」當成「淨收益」"""
    names = {str(c).replace(' ', ''): c for c in columns}
    for kw in REVENUE_NAMES:
        if kw in names:
            return names[kw]
    return find_col(columns, *REVENUE_NAMES, exclude=('營業外', '利息', '其他', '每股', '手續費'))

def fetch_season_market(y, s, market):
    res = requests.post("https://mopsov.twse.com.tw/mops/web/ajax_t163sb04", headers=HEADERS, timeout=60, data={
        'encodeURIComponent': 1, 'step': 1, 'firstin': 1, 'off': 1, 'isQuery': 'Y',
        'TYPEK': market, 'year': y - 1911, 'season': f"{s:02d}"})
    time.sleep(3)
    res.encoding = 'utf-8'
    try:
        tables = pd.read_html(io.StringIO(res.text))
    except ValueError:  # 沒有表格：尚未公布
        return None
    parts = []
    for t in tables:
        t.columns = [''.join(map(str, c)) if isinstance(c, tuple) else str(c) for c in t.columns]
        code, eps = find_col(t.columns, '公司代號'), find_col(t.columns, '基本每股盈餘')
        if code is None or eps is None:
            continue
        # 一般業是「營業收入」，金融保險業是「淨收益」「收益合計」等
        rev = find_revenue_col(t.columns)
        parts.append(pd.DataFrame({'證券代號': t[code].astype(str).str.strip(),
                                   '營收': to_number(t[rev], fill=None) if rev else float('nan'),
                                   'EPS': to_number(t[eps], fill=None)}))
    return pd.concat(parts, ignore_index=True) if parts else None

class Financials:
    """各季「累計」營收與 EPS，依需要抓取並快取在 data/financials"""
    def __init__(self):
        self.seasons = {}

    def get(self, y, s):
        if (y, s) in self.seasons:
            return self.seasons[(y, s)]
        path = os.path.join(FIN_DIR, f"{y}Q{s}.csv")
        df = None
        if os.path.exists(path):
            df = pd.read_csv(path, dtype={'證券代號': str})
            fetched = Date.fromisoformat(df['抓取日'].iloc[0])
            # 最近兩季的資料在公告期限後一個月內還會有晚交、更正的，每週重抓一次
            if (y, s) >= season_shift(*latest_season(TODAY), 1) and fetched < TODAY - timedelta(days=7) \
                    and fetched < Date(y + (s == 4), *[(5, 15), (8, 14), (11, 14), (3, 31)][s - 1]) + timedelta(days=45):
                df = None
        if df is None:
            parts = [fetch_season_market(y, s, m) for m in ('sii', 'otc')]
            parts = [p for p in parts if p is not None]
            if parts:
                df = only_stocks(pd.concat(parts, ignore_index=True)).drop_duplicates('證券代號').assign(抓取日=str(TODAY))
                os.makedirs(FIN_DIR, exist_ok=True)
                df.to_csv(path, index=False, encoding='utf-8-sig')
                print(f"  已更新財報 {y}Q{s}：{len(df)} 家")
        if df is not None:  # 確保是數值欄位，缺值為 NaN（物件欄位遇到 0 會直接 ZeroDivisionError）
            df[['營收', 'EPS']] = df[['營收', 'EPS']].apply(pd.to_numeric, errors='coerce')
        self.seasons[(y, s)] = None if df is None else df.set_index('證券代號')
        return self.seasons[(y, s)]

    def cum(self, y, s, field):
        df = self.get(y, s)
        return pd.Series(dtype=float) if df is None else df[field]

    def quarter(self, y, s, field):
        """單季值 = 本季累計 − 上一季累計"""
        return self.cum(y, s, field) if s == 1 else self.cum(y, s, field) - self.cum(y, s - 1, field)

# ---------- 技術指標 ----------

def kd(high, low, close, n=9):
    """KD 指標：RSV 取 n 期，K、D 以 1/3 平滑，起始值 50（日期為列、股票為欄）"""
    low_n, high_n = low.rolling(n, min_periods=n).min(), high.rolling(n, min_periods=n).max()
    rsv = ((close - low_n) / (high_n - low_n) * 100).where(high_n > low_n, 50)
    k, d = [], []
    prev_k = prev_d = pd.Series(50.0, index=close.columns)
    for _, r in rsv.iterrows():
        prev_k = (prev_k * 2 / 3 + r / 3).where(r.notna(), prev_k)
        prev_d = prev_d * 2 / 3 + prev_k / 3
        k.append(prev_k)
        d.append(prev_d)
    enough = rsv.notna().cumsum() > 0
    k = pd.DataFrame(k, index=close.index).where(enough)
    d = pd.DataFrame(d, index=close.index).where(enough)
    return k.iloc[-1], d.iloc[-1]

def resample_kd(high, low, close, freq):
    """週 KD、月 KD：先把日 K 合併成週 K／月 K（含尚未結束的這一週／月）"""
    key = pd.PeriodIndex(close.index, freq=freq)
    return kd(high.groupby(key).max(), low.groupby(key).min(), close.groupby(key).last())

def dmi(high, low, close, n=14):
    """DMI：+DI、−DI、ADX（Wilder 平滑）"""
    up, down = high.diff(), -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0)
    minus_dm = down.where((down > up) & (down > 0), 0)
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()]).groupby(level=0).max()
    smooth = lambda x: x.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    atr = smooth(tr)
    plus_di, minus_di = 100 * smooth(plus_dm) / atr, 100 * smooth(minus_dm) / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return plus_di.iloc[-1], minus_di.iloc[-1], adx.iloc[-1]

def compute_metrics(days):
    """以最新一天為基準，計算每檔的均線、量能、布林、DMI、KD"""
    hist = pd.concat([df.assign(日期=pd.Timestamp(d)) for d, df in days])
    pivot = lambda col: hist.pivot_table(index='日期', columns='證券代號', values=col).sort_index()
    close = pivot('收盤價').ffill()  # 停牌日沿用前一天收盤
    vol = pivot('成交股數').fillna(0)
    has_ohlc = '最高價' in hist.columns
    high = pivot('最高價').reindex_like(close).fillna(close) if has_ohlc else close
    low = pivot('最低價').reindex_like(close).fillna(close) if has_ohlc else close

    ma = lambda n: close.rolling(n, min_periods=n).mean()
    ma20, ma60 = ma(20), ma(60)
    std20 = close.rolling(20, min_periods=20).std(ddof=0)
    upper, lower = ma20 + 2 * std20, ma20 - 2 * std20

    m = pd.DataFrame({
        '收盤價': close.iloc[-1],
        '月線': close.tail(20).mean(),
        '月均量': vol.iloc[:-1].tail(20).mean() / 1000,  # 不含當天的前 20 日平均（張）
        '今日量': vol.iloc[-1] / 1000,
        'MA5': ma(5).iloc[-1], 'MA20': ma20.iloc[-1], 'MA60': ma60.iloc[-1], 'MA60前日': ma60.iloc[-2],
        '%b': ((close - lower) / (upper - lower)).iloc[-1],
        '帶寬': ((upper - lower) / ma20).iloc[-1],
    })
    m['量比'] = m['今日量'] / m['月均量']
    m['站上月線'] = m['收盤價'] >= m['月線']
    m['+DI'], m['-DI'], m['ADX'] = dmi(high, low, close)
    m['日K'], m['日D'] = kd(high, low, close)
    m['週K'], m['週D'] = resample_kd(high, low, close, 'W-FRI')
    m['月K'], m['月D'] = resample_kd(high, low, close, 'M')
    m.attrs['月數'] = len(pd.PeriodIndex(close.index, freq='M').unique())
    return m.reset_index().rename(columns={'index': '證券代號'})

# ---------- 集保 ----------

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

# ---------- 篩選區塊：每個區塊回傳 Section ----------

class Lazy:
    """第一次用到才抓資料；失敗時記住錯誤，每個用到的區塊都顯示同一個原因"""
    def __init__(self, loader):
        self.loader, self.done, self.value, self.error = loader, False, None, None

    def get(self):
        if not self.done:
            self.done = True
            try:
                self.value = self.loader()
            except Exception as e:
                self.error = e
                print(f"⚠️ 資料取得失敗：{e}")
        if self.error:
            raise self.error
        return self.value

class Section:
    def __init__(self, title, df, removed, detail, kind, extra=lambda row: {}, notes=()):
        self.title, self.df, self.removed, self.detail, self.kind, self.extra, self.notes = \
            title, df, removed, detail, kind, extra, list(notes)

def apply_filters(df, metrics, p):
    """書中 SOP 的技術面與流動性過濾：站上月線、月均量下限；回傳 (過濾後, 被排除檔數)"""
    overlap = [c for c in metrics.columns if c in df.columns and c != '證券代號']
    df = df.drop(columns=overlap).merge(metrics, on='證券代號', how='left')
    keep = df['月均量'] >= p['filters']['min_avg_volume_lots']
    if p['filters']['require_above_ma20']:
        keep &= df['站上月線'].fillna(False).astype(bool)
    return df[keep], int((~keep).sum())

def with_names(df, ctx):
    return df.drop(columns=[c for c in ('證券名稱', '市場') if c in df.columns]).merge(ctx['names'], on='證券代號', how='left')

def big_holder_change(ctx):
    """千張大戶股數週增減 %（需要兩份集保快照）"""
    snaps = ctx['tdcc'].get()
    if len(snaps) < 2:
        return None
    m = snaps[0][1].merge(snaps[1][1], on='證券代號', suffixes=('', '_上週'))
    m = m[m['大戶股數_上週'] > 0]
    return m.set_index('證券代號').eval('(大戶股數 - 大戶股數_上週) / 大戶股數_上週 * 100')

def section_strong(ctx, p):
    """短中線強勢股（第 4 章）：DMI、布林、KD 多頭、5MA > 20MA、量 ≥500 張、千張大戶不減少"""
    s = p['strong']
    m = ctx['metrics']
    keep = (m['+DI'] > s['plus_di_min']) & (m['-DI'] < s['minus_di_max']) \
         & (m['%b'] > s['pctb_min']) & (m['帶寬'] < s['bandwidth_max']) & (m['今日量'] >= s['min_volume_lots'])
    notes = []
    if s['di_adx_order']:
        keep &= (m['+DI'] > m['ADX']) & (m['ADX'] > m['-DI'])
    if s['kd_daily']:
        keep &= m['日K'] > m['日D']
    if s['kd_weekly']:
        keep &= m['週K'] > m['週D']
    if s['kd_monthly']:
        if m.attrs.get('月數', 12) < 10:
            notes.append(f"月 KD 需要約 10 個月資料，目前只有 {m.attrs.get('月數')} 個月，暫不檢查")
        else:
            keep &= m['月K'] > m['月D']
    if s['ma5_above_ma20']:
        keep &= m['MA5'] > m['MA20']
    df = m[keep.fillna(False)]
    if s['big_holder_no_decrease']:
        try:
            change = big_holder_change(ctx)
            if change is None:
                notes.append("集保快照不足兩週，千張大戶條件暫不檢查")
            else:
                df = df[df['證券代號'].map(change).fillna(-1) >= 0]
        except Exception as e:
            notes.append(f"集保資料失敗，千張大戶條件暫不檢查（{e}）")
    df, removed = apply_filters(with_names(df, ctx), m, p)
    df = df.sort_values('+DI', ascending=False)
    detail = lambda r: (f"  +DI {r['+DI']:.0f}／−DI {r['-DI']:.0f}／ADX {r['ADX']:.0f}｜%b {r['%b']:.2f}｜帶寬 {r['帶寬']:.2f}\n"
                        f"  KD 日 {r['日K']:.0f}/{r['日D']:.0f}・週 {r['週K']:.0f}/{r['週D']:.0f}・月 {r['月K']:.0f}/{r['月D']:.0f}\n")
    title = f"⚡ 短中線強勢股（+DI>{s['plus_di_min']:g}、−DI<{s['minus_di_max']:g}、%b>{s['pctb_min']:g}、帶寬<{s['bandwidth_max']:g}、量≥{s['min_volume_lots']:g} 張）"
    extra = lambda r: {'+DI': round(r['+DI'], 1), '-DI': round(r['-DI'], 1), 'ADX': round(r['ADX'], 1), '%b': round(r['%b'], 2)}
    return Section(title, df, removed, detail, '短中線強勢股', extra, notes)

def section_growth(ctx, p):
    """長期成長股 CAN SLIM（第 3 章）：EPS、營收年增、近 4 季 ROE、累季 EPS"""
    g = p['growth']
    fin = ctx['financials'].get()
    y, s = latest_season(TODAY)
    if fin.get(y, s) is None:  # 期限剛過但資料還沒整理好，先用上一季
        y, s = season_shift(y, s, 1)
    yoy = lambda a, b: ((a - b) / b.abs() * 100).where(b != 0)

    conds, cols = [], {}
    for i in range(g['eps_q_count']):
        qy, qs = season_shift(y, s, i)
        growth = yoy(fin.quarter(qy, qs, 'EPS'), fin.quarter(qy - 1, qs, 'EPS'))
        conds.append(growth >= g['eps_q_yoy'])
        cols.setdefault('EPS年增%', growth)
    for i in range(g['rev_q_count']):
        qy, qs = season_shift(y, s, i)
        growth = yoy(fin.quarter(qy, qs, '營收'), fin.quarter(qy - 1, qs, '營收'))
        conds.append(growth >= g['rev_q_yoy'])
        cols.setdefault('營收年增%', growth)
    fy = y if s == 4 else y - 1  # 最近一個完整年度
    for i in range(g['eps_y_count']):
        growth = yoy(fin.cum(fy - i, 4, 'EPS'), fin.cum(fy - i - 1, 4, 'EPS'))
        conds.append(growth >= g['eps_y_yoy'])
        cols.setdefault('年度EPS年增%', growth)
    cum_eps = fin.cum(y, s, 'EPS')
    conds.append(cum_eps > (0.25 * s if g['cum_eps_auto'] else g['cum_eps_min']))
    cols['累季EPS'] = cum_eps

    # 近 4 季 ROE ≈ 近 4 季 EPS ÷ 每股淨值（每股淨值 = 收盤價 ÷ 股價淨值比）
    eps4 = cum_eps if s == 4 else cum_eps + fin.cum(y - 1, 4, 'EPS') - fin.cum(y - 1, s, 'EPS')
    val = ctx['valuation'].get().set_index('證券代號')
    close = ctx['metrics'].set_index('證券代號')['收盤價']
    roe = eps4 / (close / val['股價淨值比']) * 100
    conds.append(roe >= g['roe_min'])
    cols['ROE%'] = roe

    table = pd.DataFrame(cols)
    keep = pd.concat(conds, axis=1).reindex(table.index).fillna(False).all(axis=1)
    df = table[keep].rename_axis('證券代號').reset_index()
    df, removed = apply_filters(with_names(df, ctx), ctx['metrics'], p)
    df = df.sort_values('EPS年增%' if 'EPS年增%' in df.columns else '累季EPS', ascending=False)
    def detail(r):
        parts = [f"{k} {r[k]:.1f}" for k in ('EPS年增%', '營收年增%', '年度EPS年增%', 'ROE%') if k in r and pd.notna(r[k])]
        return f"  {'｜'.join(parts)}｜累季 EPS {r['累季EPS']:.2f}\n"
    title = (f"📈 CAN SLIM 成長股（{y}Q{s} 財報；EPS 連 {g['eps_q_count']} 季、營收連 {g['rev_q_count']} 季年增 ≥{g['eps_q_yoy']:g}%／"
             f"{g['rev_q_yoy']:g}%，ROE ≥{g['roe_min']:g}%）")
    extra = lambda r: {k: round(r[k], 2) for k in cols if k in r and pd.notna(r[k])}
    return Section(title, df, removed, detail, '成長股', extra,
                   ["ROE 以近 4 季 EPS ÷ 每股淨值估算；書中第 ⑤ 組的自由現金流條件尚未支援"])

def section_value(ctx, p):
    """高殖利率價值股（第 6 章）：現金股利、殖利率、本益比、淨值比、季線之上且上揚"""
    v = p['value']
    m = ctx['metrics']
    df = m.merge(ctx['valuation'].get(), on='證券代號')
    # 上市資料沒有現金股利欄位時，用 殖利率 × 收盤價 換算
    df['現金股利'] = df['現金股利'].fillna(df['殖利率%'] * df['收盤價'] / 100)
    keep = (df['現金股利'] >= v['cash_div_min']) & (df['殖利率%'] >= v['yield_min']) \
         & (df['本益比'] > 0) & (df['本益比'] <= v['per_max']) & (df['股價淨值比'] > 0) & (df['股價淨值比'] <= v['pbr_max'])
    if v['above_ma60']:
        keep &= df['收盤價'] >= df['MA60']
    if v['ma60_rising']:
        keep &= df['MA60'] > df['MA60前日']
    df, removed = apply_filters(with_names(df[keep.fillna(False)], ctx), m, p)
    df = df.sort_values('殖利率%', ascending=False)
    detail = lambda r: f"  殖利率 {r['殖利率%']:.1f}%｜股利 {r['現金股利']:.2f} 元｜本益比 {r['本益比']:.1f}｜淨值比 {r['股價淨值比']:.2f}\n"
    title = (f"💰 高殖利率價值股（殖利率 ≥{v['yield_min']:g}%、股利 ≥{v['cash_div_min']:g} 元、本益比 ≤{v['per_max']:g}、"
             f"淨值比 ≤{v['pbr_max']:g}" + ("、季線之上且上揚" if v['above_ma60'] and v['ma60_rising'] else "") + "）")
    extra = lambda r: {'殖利率%': r['殖利率%'], '本益比': r['本益比'], '股價淨值比': r['股價淨值比']}
    return Section(title, df, removed, detail, '高殖利率價值股', extra, ["殖利率依證交所／櫃買中心公布值（以最近一年度股利計算）"])

def section_big_holder(ctx, p):
    """千張大戶週增 ≥8%、庫存不變、大戶比例在範圍內、股本上限，並標記加分訊號（第 5 章）"""
    b = p['big_holder']
    snaps = ctx['tdcc'].get()
    if len(snaps) < 2:
        raise SkipSection("目前只有一份集保快照，下週起才能比較週增減。")
    cur, prev = snaps[0][1], snaps[1][1]
    m = cur.merge(prev, on='證券代號', suffixes=('', '_上週'))
    m = m[m['大戶股數_上週'] > 0]
    m['大戶週增減%'] = (m['大戶股數'] - m['大戶股數_上週']) / m['大戶股數_上週'] * 100
    m['庫存週增減%'] = (m['集保總股數'] - m['集保總股數_上週']) / m['集保總股數_上週'] * 100
    m['股本(億)'] = m['集保總股數'] * 10 / 1e8

    low, high = b['ratio_range']
    m = m[(m['大戶週增減%'] >= b['min_change'])
          & (m['庫存週增減%'].abs() < b['stock_change_tolerance'])  # 排除增資、合併造成的虛增
          & (m['大戶比例'].between(low, high))
          & (m['股本(億)'] <= b['max_capital_yi'])]

    # 加分：連續三週大戶股數都增加（需要 4 份快照）
    if len(snaps) >= 4:
        # 每週上市櫃的股票不完全相同，先對齊成同一張表（只留四週都有的股票）再比較
        wk = pd.concat([s.drop_duplicates('證券代號').set_index('證券代號')['大戶股數'] for _, s in snaps[:4]],
                       axis=1, join='inner', keys=range(4))
        up3 = (wk[0] > wk[1]) & (wk[1] > wk[2]) & (wk[2] > wk[3])
        m['連3週增'] = m['證券代號'].map(up3).fillna(False).astype(bool)
    else:
        m['連3週增'] = False
    m['單週大增'] = m['大戶週增減%'] >= b['surge']
    m['甜蜜區'] = m['大戶比例'].between(*b['sweet_spot'])

    df, removed = apply_filters(with_names(m, ctx), ctx['metrics'], p)
    surge = p['filters']['volume_surge']
    df['法人同步'] = df['證券代號'].isin(cumulative_sync_buy(ctx['days'], p['consecutive']['days']))
    # 加分訊號越多排越前面
    df['加分'] = df[['法人同步', '連3週增', '單週大增', '甜蜜區']].sum(axis=1) + (df['量比'] >= surge)
    df = df.sort_values(['加分', '大戶週增減%'], ascending=[False, False])
    df['_tag'] = df.apply(lambda r: ("⭐" if r['法人同步'] else "") + ("🔺" if r['連3週增'] else "")
                          + ("💥" if r['單週大增'] else "") + ("🍯" if r['甜蜜區'] else ""), axis=1)
    title = (f"🐳 千張大戶週增 ≥{b['min_change']:g}%（集保 {snaps[1][0]}→{snaps[0][0]}；大戶比例 {low:g}～{high:g}%、"
             f"股本 ≤{b['max_capital_yi']:g} 億、集保庫存不變）")
    notes = [f"⭐法人同步 🔺連3週增 💥單週≥{b['surge']:g}% 🍯比例{b['sweet_spot'][0]:g}～{b['sweet_spot'][1]:g}% 🚀量≥{surge:g}倍"]
    detail = lambda r: f"  大戶 +{r['大戶週增減%']:.1f}%｜比例 {r['大戶比例']:.1f}%｜股本 {r['股本(億)']:.1f} 億\n"
    extra = lambda r: {'大戶週增減%': round(r['大戶週增減%'], 2), '大戶比例%': r['大戶比例'], '股本(億)': round(r['股本(億)'], 1)}
    return Section(title, df, removed, detail, '千張大戶週增', extra, notes)

def screen_consecutive(days, n):
    """外資、投信「每一天」都買超（連續 n 個交易日）"""
    merged = None
    for date, df in days[:n]:
        # 當天外資、投信都要是買超（股數 > 0）
        d = df[(df['外資買賣超股數'] > 0) & (df['投信買賣超股數'] > 0)]
        d = d[['證券代號', '外資買超張數', '投信買超張數']]
        if merged is None:
            merged = d
        else:
            # 取交集，並累加張數
            merged = merged.merge(d, on='證券代號', suffixes=('', '_new'))
            merged['外資買超張數'] += merged.pop('外資買超張數_new')
            merged['投信買超張數'] += merged.pop('投信買超張數_new')
    merged = merged.rename(columns={'外資買超張數': '外資累計張數', '投信買超張數': '投信累計張數'})
    return merged.assign(合計累計張數=merged['外資累計張數'] + merged['投信累計張數'])

def section_consecutive(ctx, p):
    """法人連續買超 2 週以上（第 5 章）"""
    days, n = ctx['days'], p['consecutive']['days']
    if len(days) < n:
        raise SkipSection(f"⚠️ 只抓到 {len(days)} 天資料，連續買超篩選略過。")
    df, removed = apply_filters(with_names(screen_consecutive(days, n), ctx), ctx['metrics'], p)
    df = df.sort_values('合計累計張數', ascending=False)
    title = f"🏆 外資＋投信連續 {n} 日同步買超（{days[n - 1][0]:%m/%d}～{days[0][0]:%m/%d}）"
    detail = lambda r: f"  外資 {r['外資累計張數']:,} 張｜投信 {r['投信累計張數']:,} 張\n"
    extra = lambda r: {'外資張數': r['外資累計張數'], '投信張數': r['投信累計張數']}
    return Section(title, df, removed, detail, f'連續{n}日同步買超', extra)

def cumulative_sync_buy(days, n):
    """近 n 日外資、投信「累計」都是買超的股票代號"""
    all_days = pd.concat([df for _, df in days[:n]])
    total = all_days.groupby('證券代號')[['外資買賣超股數', '投信買賣超股數']].sum()
    return set(total[(total['外資買賣超股數'] > 0) & (total['投信買賣超股數'] > 0)].index)

def section_cum_sync(ctx, p):
    """外資、投信同步買超（三個月累計）"""
    days, n = ctx['days'], p['cum_sync']['days']
    n = min(n, len(days))
    total = pd.concat([df for _, df in days[:n]]).groupby('證券代號')[['外資買超張數', '投信買超張數']].sum()
    total = total[(total['外資買超張數'] > 0) & (total['投信買超張數'] > 0)].reset_index()
    df, removed = apply_filters(with_names(total, ctx), ctx['metrics'], p)
    df = df.assign(合計=df['外資買超張數'] + df['投信買超張數']).sort_values('合計', ascending=False)
    title = f"🤝 外資＋投信近 {n} 日累計同步買超（{days[n - 1][0]:%m/%d}～{days[0][0]:%m/%d}）"
    detail = lambda r: f"  外資 {r['外資買超張數']:,} 張｜投信 {r['投信買超張數']:,} 張\n"
    extra = lambda r: {'外資張數': r['外資買超張數'], '投信張數': r['投信買超張數']}
    return Section(title, df, removed, detail, f'近{n}日累計同步買超', extra)

def section_monthly_ratio(ctx, p):
    """外資當月買超佔成交比重 >8%＋投信 >2%"""
    r = p['monthly_ratio']
    days = ctx['days']
    month = [df for d, df in days if (d.year, d.month) == (days[0][0].year, days[0][0].month)]
    total = pd.concat(month).groupby('證券代號')[['外資買賣超股數', '投信買賣超股數', '成交股數']].sum()
    total = total[total['成交股數'] > 0]
    total['外資佔比%'] = total['外資買賣超股數'] / total['成交股數'] * 100
    total['投信佔比%'] = total['投信買賣超股數'] / total['成交股數'] * 100
    total = total[(total['外資佔比%'] >= r['foreign_pct']) & (total['投信佔比%'] >= r['trust_pct'])].reset_index()
    df, removed = apply_filters(with_names(total, ctx), ctx['metrics'], p)
    df = df.assign(合計=df['外資佔比%'] + df['投信佔比%']).sort_values('合計', ascending=False)
    title = f"📅 當月外資佔成交 ≥{r['foreign_pct']:g}%＋投信 ≥{r['trust_pct']:g}%（{days[0][0]:%m} 月 {len(month)} 個交易日）"
    detail = lambda x: f"  外資佔 {x['外資佔比%']:.1f}%｜投信佔 {x['投信佔比%']:.1f}%\n"
    extra = lambda x: {'外資佔比%': round(x['外資佔比%'], 2), '投信佔比%': round(x['投信佔比%'], 2)}
    return Section(title, df, removed, detail, '當月外資投信佔比', extra)

def section_daily(ctx, p):
    """外資當日佔比＋張數符合任一組，且投信同步買超"""
    d = p['daily']
    df = ctx['days'][0][1]
    df = df[df['成交股數'] > 0].copy()
    df['外資佔比%'] = df['外資買賣超股數'] / df['成交股數'] * 100
    foreign_ok = pd.Series(False, index=df.index)
    for c in d['foreign_combos']:
        foreign_ok |= (df['外資佔比%'] >= c['pct']) & (df['外資買超張數'] >= c['lots'])
    sync = df[foreign_ok & (df['投信買超張數'] >= d['min_trust_lots'])]
    sync = sync.assign(合計買超張數=sync['外資買超張數'] + sync['投信買超張數'])
    sync, removed = apply_filters(sync, ctx['metrics'], p)
    sync = sync.sort_values('合計買超張數', ascending=False)
    combos = "、".join(f"{c['pct']:g}%＋{c['lots']:,}張" for c in d['foreign_combos'])
    title = f"🔥 單日外資大買＋投信同步（外資佔比＋張數符合 {combos} 任一組；投信 ≥{d['min_trust_lots']} 張）"
    detail = lambda r: f"  外資 {r['外資買超張數']:,} 張（佔 {r['外資佔比%']:.1f}%）｜投信 {r['投信買超張數']:,} 張\n"
    extra = lambda r: {'外資張數': r['外資買超張數'], '投信張數': r['投信買超張數'], '外資佔比%': round(r['外資佔比%'], 2)}
    return Section(title, sync, removed, detail, '單日大量同步買超', extra)

class SkipSection(Exception):
    """這個區塊這次無法篩選（例如資料天數不足），訊息直接顯示在報告中"""

SECTIONS = {
    'strong': section_strong, 'growth': section_growth, 'value': section_value, 'big_holder': section_big_holder,
    'consecutive': section_consecutive, 'cum_sync': section_cum_sync, 'monthly_ratio': section_monthly_ratio,
    'daily': section_daily,
}

# ---------- 報告 ----------

def label(row):
    """訊息中的股票標籤，上櫃股票加註"""
    tag = "（櫃）" if row.get('市場') == '上櫃' else ""
    name = row['證券名稱'] if isinstance(row.get('證券名稱'), str) else ""
    return f"{row.get('_tag', '') or ''}[{row['證券代號']}] {name}{tag}"

def tech_note(row, p):
    """技術面、量能摘要"""
    note = f"  收 {row['收盤價']:g}｜月線 {row['月線']:.1f}｜量比 {row['量比']:.1f}"
    if row['量比'] >= p['filters']['volume_surge']:
        note += " 🚀"
    return note + "\n"

def journal_row(date, preset, kind, row, extra):
    base = {'資料日期': date, '條件組合': preset['name'], '類別': kind, '證券代號': row['證券代號'],
            '證券名稱': row.get('證券名稱', ''), '市場': row.get('市場', ''), '收盤價': row.get('收盤價', ''),
            '月線': round(row.get('月線', 0), 2), '量比': round(row.get('量比', 0), 2)}
    base.update(extra)
    return base

def no_match(removed):
    return f"無符合標的（過濾掉 {removed} 檔）。\n" if removed else "無符合標的。\n"

def build_report(p, ctx):
    """依一組條件產生 LINE 訊息與交易日誌資料"""
    latest_date = ctx['days'][0][0]
    top_n = p['top_n']
    filter_note = f"月均量 ≥{p['filters']['min_avg_volume_lots']} 張" + ("、站上月線" if p['filters']['require_above_ma20'] else "")
    msg = f"📊【{p['name']}】資料日期 {latest_date}\n"
    msg += f"範圍：上市＋上櫃（排除 ETF）\n共同過濾：{filter_note}\n" + "-" * 20 + "\n"

    sections, problems = [], []
    for key, fn in SECTIONS.items():
        if not p[key]['enabled']:
            continue
        try:
            sec = fn(ctx, p)
            print(f"  {sec.kind}：{len(sec.df)} 檔（過濾掉 {sec.removed} 檔）")
            sections.append(sec)
        except SkipSection as e:
            problems.append(str(e))
        except Exception as e:
            # 程式錯誤只記在 GitHub Actions 執行紀錄，不放進 LINE 訊息，其他區塊照常顯示
            print(f"⚠️ {key} 篩選失敗：{e}")

    journal = []
    if p['combine'] == 'intersect' and sections:
        # 交集：只留下同時出現在每個區塊的股票，依第一個區塊的排序
        codes = set.intersection(*(set(s.df['證券代號']) for s in sections))
        first = sections[0].df[sections[0].df['證券代號'].isin(codes)]
        msg += "🎯 同時符合：" + "＋".join(s.kind for s in sections) + "\n"
        for s in sections:
            msg += f"・{s.title}\n" + "".join(f"  ※ {n}\n" for n in s.notes)
        msg += no_match(0) if first.empty else f"共 {len(first)} 檔，前 {top_n} 名：\n"
        details = {s.kind: s.df.set_index('證券代號') for s in sections}
        for _, row in first.head(top_n).iterrows():
            msg += label(row) + "\n"
            for s in sections:
                msg += s.detail(details[s.kind].loc[row['證券代號']])
            msg += tech_note(row, p)
        for _, row in first.iterrows():
            extra = {}
            for s in sections:
                extra.update(s.extra(details[s.kind].loc[row['證券代號']]))
            journal.append(journal_row(latest_date, p, '交集：' + '＋'.join(s.kind for s in sections), row, extra))
    else:
        for s in sections:
            msg += f"\n{s.title}\n" + "".join(f"※ {n}\n" for n in s.notes)
            if s.df.empty:
                msg += no_match(s.removed)
            else:
                msg += f"共 {len(s.df)} 檔（過濾掉 {s.removed} 檔），前 {top_n} 名：\n"
                for _, row in s.df.head(top_n).iterrows():
                    msg += label(row) + "\n" + s.detail(row) + tech_note(row, p)
            for _, row in s.df.iterrows():
                journal.append(journal_row(latest_date, p, s.kind, row, s.extra(row)))
    for text in problems:
        msg += f"\n{text}\n"
    # 給 LINE 卡片顯示的摘要：每個區塊符合幾檔
    if p['combine'] == 'intersect' and sections:
        summary = [{'kind': '同時符合', 'count': len(first)}]
    else:
        summary = [{'kind': s.kind, 'count': len(s.df)} for s in sections]
    summary += [{'kind': text.strip('⚠️ '), 'count': None} for text in problems]
    return msg, journal, summary

def append_journal(rows):
    """把每次的篩選結果寫進交易日誌（同一天、同條件組合、同類別重跑會覆蓋）"""
    if not rows:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    new = pd.DataFrame(rows).astype(str)
    key = ['資料日期', '條件組合', '類別']
    if os.path.exists(JOURNAL_PATH):
        old = pd.read_csv(JOURNAL_PATH, dtype=str)
        old['條件組合'] = old.get('條件組合', pd.Series('', index=old.index)).fillna('')
        # 同一天、同條件組合、同類別重跑時，以最新結果整批取代
        replaced = old.set_index(key).index.isin(new.set_index(key).index)
        new = pd.concat([old[~replaced], new], ignore_index=True)
    new.to_csv(JOURNAL_PATH, index=False, encoding='utf-8-sig')
    print(f"  交易日誌已更新：{JOURNAL_PATH}（共 {len(new)} 筆）")

def save_results(latest_date, reports):
    """存下各條件組合的最新結果，讓 LINE 選單點選時直接回覆（回覆訊息不算推播額度）"""
    data = {'date': str(latest_date), 'generated_at': datetime.now(TW_TZ).isoformat(timespec='seconds'),
            'results': {p['id']: {'name': p['name'], 'text': msg, 'summary': summary} for p, msg, summary in reports}}
    with open(RESULTS_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

def already_ran_today():
    """Worker 16:30 觸發的篩選已經跑完了嗎（results.json 在 RUN_DAY 16:00 之後產生）"""
    try:
        with open(RESULTS_PATH, encoding='utf-8') as f:
            generated = datetime.fromisoformat(json.load(f)['generated_at'])
    except (OSError, KeyError, ValueError):
        return False
    return generated >= datetime.combine(RUN_DAY, datetime.min.time(), TW_TZ).replace(hour=16)

def main():
    print("啟動量化程式...")
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule" and already_ran_today():
        print("今天 16:30 已由 Worker 觸發篩選並推播，GitHub 備援排程略過。")
        return
    # 今天有安排推播（或從 LINE 手動觸發）才推；否則連錯誤通知都不推，避免消耗額度
    will_push = bool(ONLY_PRESET) or any(RUN_DAY.weekday() in p['notify_days'] for p in PRESETS)
    notify = send_line_message if will_push else lambda text: print("（今天沒有安排推播，不發送）")
    try:
        # 取得最近 PRICE_DAYS 個交易日行情（最近 INSTI_DAYS 天含法人買賣超），第一次會比較久
        print(f"\n--- 取得近 {PRICE_DAYS} 個交易日行情、近 {INSTI_DAYS} 日法人買賣超（上市＋上櫃）---")
        days = fetch_recent_days(PRICE_DAYS, INSTI_DAYS)
        prune_daily_cache()
        if not days:
            notify("⚠️ 抓不到證交所資料，請稍後再試。")
            return

        latest_date = days[0][0]
        names = pd.concat([df for _, df in days])[['證券代號', '證券名稱', '市場']].drop_duplicates('證券代號')
        ctx = {
            'days': days,
            'metrics': compute_metrics(days),
            'names': names,
            # 集保快照每週都要存，就算目前沒有組合用到千張大戶，之後打開才有歷史可比
            'tdcc': Lazy(load_tdcc_snapshots),
            'valuation': Lazy(lambda: fetch_valuation(latest_date)),
            'financials': Lazy(Financials),
        }
        print("\n--- 開始抓取集保股權分散表 ---")
        try:
            ctx['tdcc'].get()
        except Exception:
            pass

        reports, journal = [], []
        for p in PRESETS:
            print(f"\n--- 條件組合：{p['name']} ---")
            msg, rows, summary = build_report(p, ctx)
            reports.append((p, msg, summary))
            journal += rows

        # 交易日誌＋最新結果
        append_journal(journal)
        save_results(latest_date, reports)

        # 推播：手動指定的那一組；排程則推播今天有勾選的組合，全部合併成 1 則
        if ONLY_PRESET:
            to_send = [msg for p, msg, _ in reports if p['id'] == ONLY_PRESET]
        else:
            to_send = [msg for p, msg, _ in reports if RUN_DAY.weekday() in p['notify_days']]
        if to_send:
            send_line_message(to_send)
        else:
            print("今天沒有需要推播的條件組合，結果可在 LINE 選單點選查看")

    except Exception as e:
        error_msg = f"⚠️ 量化腳本執行發生系統錯誤\n錯誤訊息：{str(e)}"
        print(error_msg)
        notify(error_msg)

if __name__ == "__main__":
    main()
