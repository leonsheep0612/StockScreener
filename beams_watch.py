import os
import re
import json
import math
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from main import send_line_message, TW_TZ, HEADERS

# 監控 7-11 × BEAMS DESIGN 休閒拖鞋有沒有進貨，有新門市上架就用 LINE 推播
# 資料來源是 foodomo（7-11 外送平台）的 API：ac=home/ta=store 列出全台門市座標，ac=store/op=goods 查單一門市的商品與庫存
API = "https://opennow.foodomo.com/app/wxapp.php"
BASE_PARAMS = {"i": 1, "m": "ht_wmps", "c": "entry", "do": "mobile", "lang": "zh-cn",
               "ctrl": "wmall", "from": "vue", "u": "wap"}

CENTER = (22.554, 120.347)                       # 高雄市小港區中鋼路 3 號
RADIUS_KM = float(os.environ.get("RADIUS_KM") or 10)
KEYWORD = "拖鞋"                                  # 用「拖鞋」搜尋，再用 TARGET 過濾出 BEAMS DESIGN 款
TARGET = re.compile(r"(BD|BEAMS).*拖|拖.*(BD|BEAMS)", re.I)
SIZE = re.compile(r"28")                          # 要的尺寸 28-29；商品名稱沒寫尺寸的也一起列出
# ALL = 1 時查全台（約 7,000 家，要跑十幾分鐘），平常只查半徑內的門市
SCAN_ALL = os.environ.get("ALL", "").strip() == "1"
# TEST = 1 時不管有沒有變化都推播一次，用來確認 LINE 與 foodomo 連得到
TEST = os.environ.get("TEST", "").strip() == "1"

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
    """全台門市 [(sid, 店名, 距離 km)]，依距離排序"""
    pages = range(1, 37)  # 每頁 200 家，全台約 7,100 家
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda p: call(ac="home", op="index", ta="store", psize=200, page=p), pages))
    stores = {}
    for res in results:
        for s in (res or {}).get("stores", []):
            try:
                stores[s["id"]] = (s["id"], s["title"], distance_km(*CENTER, float(s["location_x"]), float(s["location_y"])))
            except (KeyError, ValueError):
                pass
    return sorted(stores.values(), key=lambda s: s[2])


def find_slippers(store):
    sid, name, dist = store
    res = call(ac="store", op="goods", ta="list", page=1, psize=100, keyword=KEYWORD, sid=sid)
    if res is None:
        return None
    return [{"sid": sid, "store": name, "km": round(dist, 1), "title": g["title"], "stock": int(g.get("total") or 0)}
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
        print(f"  {g['store']}（{g['km']} km）{g['title']} × {g['stock']}")

    # 只在出現「之前沒有庫存的門市＋品項」時推播，避免每次都消耗推播額度
    old = set()
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            old = {tuple(k) for k in json.load(f).get("in_stock", [])}
    now = {(g["sid"], g["title"]) for g in found}
    new = [g for g in found if (g["sid"], g["title"]) not in old]
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"checked_at": datetime.now(TW_TZ).isoformat(timespec="minutes"),
                   "in_stock": sorted(now)}, f, ensure_ascii=False, indent=1)

    if not new and not TEST:
        print("沒有新進貨的門市，不推播")
        return
    want = [g for g in new if SIZE.search(g["title"]) or not re.search(r"\d{2}", g["title"])]
    lines = [f"👟 BEAMS DESIGN 休閒拖鞋 {'有新進貨' if new else '目前沒有庫存'}"
             f"（{'全台' if SCAN_ALL else f'中鋼路 {RADIUS_KM:g} 公里內'}，查了 {len(targets)} 家）"]
    for title, group in (("28-29 號", want), ("其他尺寸", [g for g in new if g not in want])):
        if group:
            lines.append(f"\n【{title}】")
            lines += [f"{g['store']}（{g['km']} km）{g['title']} × {g['stock']}" for g in group[:30]]
    if TEST and not new:
        lines.append(f"\n（測試推播：foodomo 連線正常，查詢失敗 {failed} 家）")
    send_line_message("\n".join(lines))


if __name__ == "__main__":
    main()
