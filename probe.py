import requests, json
H={"User-Agent":"Mozilla/5.0"}
def j(name,url):
    try:
        r=requests.get(url,headers=H,timeout=30); print(f"=== {name} {r.status_code}")
        d=r.json()
        if isinstance(d,dict):
            print("keys",list(d.keys())[:10], "stat",d.get('stat'))
            for i,t in enumerate(d.get('tables') or []):
                print(" table",i,t.get('title'),"fields",t.get('fields'),"n",len(t.get('data') or []))
                if t.get('data'): print("  row0",t['data'][0])
            for k in ('fields','data'):
                if k in d: print(k, d[k] if k=='fields' else d[k][:1])
    except Exception as e: print(name,"ERR",e)
j("twse-mi","https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=20261015&type=ALLBUT0999&response=json")
j("twse-mi-hol","https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=20261011&type=ALLBUT0999&response=json")
j("tpex-otc","https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date=2026/10/15&type=EW&response=json")
j("tpex-old","https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&d=115/10/15&se=EW&o=json")
j("tpex-dailyQ","https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQ?date=2026/10/15&type=EW&response=json")
