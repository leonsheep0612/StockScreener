import os
import re
import json
import math
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from main import send_line_message, quota_left, TW_TZ, HEADERS

# 監控 7-11 × BEAMS DESIGN 休閒拖鞋有沒有進貨，有新門市上架就用 LINE 推播
# 資料來源是 foodomo（7-11 外送平台）的 API：ac=home/ta=store 列出全台門市座標，ac=store/op=goods 查單一門市的商品與庫存
API = "https://opennow.foodomo.com/app/wxapp.php"
BASE_PARAMS = {"i": 1, "m": "ht_wmps", "c": "entry", "do": "mobile", "lang": "zh-cn",
               "ctrl": "wmall", "from": "vue", "u": "wap"}

# 以這幾個地點為中心，任一個半徑內的門市都查；距離以最近的地點計算
CENTERS = {"中鋼路": (22.554, 120.347),          # 高雄市小港區中鋼路 3 號
           "英明路": (22.616, 120.325)}          # 高雄市苓雅區英明路 196 號
RADIUS_KM = float(os.environ.get("RADIUS_KM") or 10)
KEYWORD = "拖鞋"                                  # 用「拖鞋」搜尋，再用 TARGET 過濾出 BEAMS DESIGN 款
TARGET = re.compile(r"(BD|BEAMS).*拖|拖.*(BD|BEAMS)", re.I)
SIZE = re.compile(r"28")                          # 要的尺寸 28-29；商品名稱沒寫尺寸的也一起列出
# ALL = 1 時查全台（約 7,000 家，要跑十幾分鐘），平常只查半徑內的門市
SCAN_ALL = os.environ.get("ALL", "").strip() == "1"
# TEST = 1 時不管有沒有變化都推播一次，用來確認 LINE 與 foodomo 連得到
TEST = os.environ.get("TEST", "").strip() == "1"
DAILY_PUSH_LIMIT = 3                              # 每天最多推幾則，超過的新進貨留到隔天一起推
QUOTA_KEEP = 20                                   # 本月額度剩不到這麼多就不推，留給 StockScreener 的選股推播

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "beams_state.json")


def call(**params):
    """呼叫 foodomo API，逾時或連線錯誤重試 3 次，仍失敗回傳 None"""
    for _ in range(3):
        try:
            r = requests.get(API, params={**BASE_PARAMS, **params}, headers=HEADERS, timeout=20)
            return r.json()["message"]["message"]
        except Exception as e:
            err = e
    print(f"⚠️ foodomo 查詢失敗 {params}：{err}")
    return None


def distance_km(lat1, lng1, lat2, lng2):
    r = math.pi / 180
    a = math.sin((lat2 - lat1) * r / 2) ** 2 + math.cos(lat1 * r) * math.cos(lat2 * r) * math.sin((lng2 - lng1) * r / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(a))


def list_stores():
    """全台門市 [(sid, 店名, 距離 km, 最近的地點)]，依距離排序"""
    pages = range(1, 37)  # 每頁 200 家，全台約 7,100 家
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda p: call(ac="home", op="index", ta="store", psize=200, page=p), pages))
    stores = {}
    for res in results:
        for s in (res or {}).get("stores", []):
            try:
                lat, lng = float(s["location_x"]), float(s["location_y"])
                km, place = min((distance_km(*c, lat, lng), name) for name, c in CENTERS.items())
                stores[s["id"]] = (s["id"], s["title"], km, place)
            except (KeyError, ValueError):
                pass
    return sorted(stores.values(), key=lambda s: s[2])


def find_slippers(store):
    sid, name, dist, place = store
    res = call(ac="store", op="goods", ta="list", page=1, psize=100, keyword=KEYWORD, sid=sid)
    if res is None:
        return None
    return [{"sid": sid, "store": name, "km": round(dist, 1), "place": place, "title": g["title"], "stock": int(g.get("total") or 0)}
            for g in res.get("goods", []) if TARGET.search(g["title"])]


def main():
    stores = list_stores()
    if not stores:
        send_line_message("⚠️ BEAMS 拖鞋監控：foodomo 門市清單抓不到，可能 API 改版或擋掉了 GitHub 主機")
        return
    targets = stores if SCAN_ALL else [s for s in stores if s[2] <= RADIUS_KM]
    print(f"全台 {len(stores)} 家門市，這次查 {len(targets)} 家")

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(find_slippers, targets))
    failed = sum(r is None for r in results)
    found = [g for r in results if r for g in r if g["stock"] > 0]
    print(f"查詢失敗 {failed} 家；有 BEAMS 拖鞋庫存的品項 {len(found)} 筆")
    for g in found:
        print(f"  {g['store']}（距{g['place']} {g['km']} km）{g['title']} × {g['stock']}")

    # 只在出現「之前沒有庫存的門市＋品項」時推播，避免每次都消耗推播額度
    state = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    old = {tuple(k) for k in state.get("in_stock", [])}
    # 這次查詢失敗的門市沿用上次的紀錄，免得下次查到又當成新進貨
    failed_sids = {s[0] for s, r in zip(targets, results) if r is None}
    now = {(g["sid"], g["title"]) for g in found} | {k for k in old if k[0] in failed_sids}
    new = [g for g in found if (g["sid"], g["title"]) not in old]

    # 推播額度和 StockScreener 共用：每天最多推 DAILY_PUSH_LIMIT 則，本月剩不到 QUOTA_KEEP 則就不推
    today = datetime.now(TW_TZ).date().isoformat()
    pushes = state.get("pushes", {}) if state.get("push_date") == today else {}
    pushed_today = pushes.get("count", 0)
    skip = None
    if new or TEST:
        left = quota_left()
        if pushed_today >= DAILY_PUSH_LIMIT and not TEST:
            skip = f"今天已推播 {pushed_today} 則"
        elif left is not None and left < QUOTA_KEEP:
            skip = f"本月推播額度只剩 {left} 則"
    if skip:
        # 沒推出去的新進貨不記進已通知，下次有額度時會一起推
        now -= {(g["sid"], g["title"]) for g in new}
        print(f"⚠️ {skip}，這次不推播，{len(new)} 筆新進貨留到下次")

    def save(count):
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"checked_at": datetime.now(TW_TZ).isoformat(timespec="minutes"),
                       "push_date": today, "pushes": {"count": count},
                       "in_stock": sorted(now)}, f, ensure_ascii=False, indent=1)

    if skip or (not new and not TEST):
        save(pushed_today)
        if not skip:
            print("沒有新進貨的門市，不推播")
        return

    shown = new or found
    # 門市清單 API 沒有地址，只對要推播的門市另外查
    sids = sorted({g["sid"] for g in shown})
    with ThreadPoolExecutor(8) as pool:
        infos = pool.map(lambda sid: call(ac="store", op="index", ta="index", sid=sid), sids)
        address = {sid: ((info or {}).get("store") or {}).get("address", "") for sid, info in zip(sids, infos)}
    want = [g for g in shown if SIZE.search(g["title"]) or not re.search(r"\d{2}", g["title"])]
    lines = [f"👟 BEAMS DESIGN 休閒拖鞋 {'有新進貨' if new else '目前有貨的門市'}"
             f"（{'全台' if SCAN_ALL else '、'.join(CENTERS) + f' {RADIUS_KM:g} 公里內'}，查了 {len(targets)} 家）"
             "\n※ foodomo 庫存不是即時的，出發前先打電話確認"]
    for title, group in (("28-29 號", want), ("其他尺寸", [g for g in shown if g not in want])):
        if group:
            lines.append(f"\n【{title}】")
            lines += [f"{g['store']}（距{g['place']} {g['km']} km）{g['title']} × {g['stock']}\n📍 {address.get(g['sid']) or '（查不到地址）'}"
                      for g in group[:30]]
    if TEST:
        lines.append(f"\n（測試推播：foodomo 連線正常，查詢失敗 {failed} 家）")
    send_line_message("\n".join(lines))
    save(pushed_today + 1)


if __name__ == "__main__":
    main()
