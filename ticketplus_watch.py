import os
import re
import json
import requests
from datetime import datetime

from main import send_line_message, quota_left, TW_TZ, HEADERS

# 監控遠大售票 Ticket Plus 指定活動有沒有釋票：票區從售完變成有票時推播，買票由自己手動操作
# 只讀售票網站公開的票數 API，不登入、不下單
# 資料來源：getS3 取活動的場次與票區清單（靜態 JSON），get?ticketAreaId= 取各票區的狀態與剩餘張數
API = "https://apis.ticketplus.com.tw/config/api/v1"
API_HEADERS = {**HEADERS, "Referer": "https://ticketplus.com.tw/"}
# count 是剩餘張數；票還很多時網站不給實際數字，固定回 999999
PLENTY = 999999

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "ticketplus_watch.json")
STATE_PATH = os.path.join(ROOT, "data", "ticketplus_state.json")
# TEST = 1 時不管有沒有變化都推播一次，用來確認推播與 Ticket Plus 連得到
TEST = os.environ.get("TEST", "").strip() == "1"
QUOTA_KEEP = 20                                   # 用 LINE 推播時，本月額度剩不到這麼多就不推，留給選股推播

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def call(endpoint, **params):
    """呼叫 Ticket Plus API，逾時或連線錯誤重試 3 次，仍失敗回傳 None"""
    for _ in range(3):
        try:
            r = requests.get(f"{API}/{endpoint}", params=params, headers=API_HEADERS, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            err = e
    print(f"⚠️ Ticket Plus 查詢失敗 {endpoint} {params}：{err}")
    return None


def notify(text):
    """有設 Telegram 就用 Telegram（沒有每月額度），否則用 StockScreener 的 LINE"""
    if TG_TOKEN and TG_CHAT_ID:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT_ID, "text": text[:4000], "disable_web_page_preview": True}, timeout=30)
        print("✅ Telegram 推播成功" if r.ok else f"❌ Telegram 推播失敗：{r.status_code} {r.text}")
        return r.ok
    left = quota_left()
    if left is not None and left < QUOTA_KEEP and not TEST:
        print(f"⚠️ 本月 LINE 推播額度只剩 {left} 則，這次不推播")
        return False
    send_line_message(text)
    return True


def event_areas(event):
    """一個活動的所有票區 [{id, event, session, area, price, status, count, url}]；查不到回傳 None"""
    m = re.search(r"activity/([0-9a-f]{32})", event["url"])
    if not m:
        print(f"⚠️ 網址看不出活動代碼：{event['url']}")
        return None
    eid = m.group(1)
    sessions = call("getS3", path=f"event/{eid}/sessions.json")
    areas = call("getS3", path=f"event/{eid}/ticketAreas.json")
    if not sessions or not areas:
        return None
    sessions = {s["sessionId"]: s for s in sessions.get("sessions", [])}
    keywords = event.get("areas") or []
    wanted = [a for a in areas.get("ticketAreas", [])
              if not a.get("hidden") and (not keywords or any(k in a["name"] for k in keywords))]
    if not wanted:
        return []
    live = call("get", ticketAreaId=",".join(a["ticketAreaId"] for a in wanted))
    if not live or live.get("errCode") != "00":
        return None
    live = {a["id"]: a for a in live["result"].get("ticketArea", [])}
    # 場次日期、時間是「起 ~ 迄」，同一天的只顯示一次
    span = lambda v: " ~ ".join(dict.fromkeys(p.strip() for p in (v or "").split("~")))
    result = []
    for a in wanted:
        s = sessions.get(a["sessionId"], {})
        info = live.get(a["ticketAreaId"], {})
        result.append({"id": a["ticketAreaId"], "event": s.get("name", eid), "session": f"{span(s.get('date'))} {span(s.get('time'))}".strip(),
                       "area": a["name"], "price": a.get("price"), "status": info.get("status", ""),
                       "count": int(info.get("count") or 0), "url": f"https://ticketplus.com.tw/activity/{eid}"})
    return result


def main():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        events = json.load(f).get("events", [])
    if not events:
        print("ticketplus_watch.json 沒有要監控的活動")
        return

    results = [event_areas(e) for e in events]
    failed = [e["url"] for e, r in zip(events, results) if r is None]
    areas = [a for r in results if r for a in r]
    # 開賣中且有剩餘張數才算有票
    available = [a for a in areas if a["status"] == "onsale" and a["count"] > 0]
    print(f"監控 {len(events)} 個活動、{len(areas)} 個票區，查詢失敗 {len(failed)} 個活動；有票的票區 {len(available)} 個")
    for a in areas:
        print(f"  {a['event']} {a['session']} {a['area']}：{a['status']} × {a['count']}")

    state = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    old = set(state.get("available", []))
    # 查詢失敗的活動沿用上次的紀錄，免得下次查到又當成新釋票
    failed_ids = {i for i in old if not any(i == a["id"] for a in areas)} if failed else set()
    now = {a["id"] for a in available} | failed_ids
    new = [a for a in available if a["id"] not in old]

    def save(ids):
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        # 不記檢查時間：有票的票區沒變時檔案就不變，workflow 不會每次都 commit
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"available": sorted(ids)}, f, ensure_ascii=False, indent=1)

    if not new and not TEST:
        save(now)
        print("沒有新釋出的票區，不推播")
        return

    shown = new or available
    lines = ["🎫 Ticket Plus " + ("有新釋票" if new else "目前有票的票區")]
    for url in dict.fromkeys(a["url"] for a in shown):
        group = [a for a in shown if a["url"] == url]
        lines.append(f"\n{group[0]['event']}")
        lines += [f"・{a['session']} {a['area']} ${a['price']}：{'大量' if a['count'] >= PLENTY else str(a['count']) + ' 張'}"
                  for a in group]
        lines.append(url)
    if TEST:
        lines.append(f"\n（測試推播：Ticket Plus 連線正常，監控 {len(areas)} 個票區，查詢失敗 {len(failed)} 個活動）")
    if notify("\n".join(lines)):
        save(now)
    else:
        # 沒推出去的釋票不記進已通知，下次一起推
        save(now - {a["id"] for a in new})


if __name__ == "__main__":
    main()
