import requests, json
H={"User-Agent":"Mozilla/5.0"}
def show(name,url,n=1500):
    try:
        r=requests.get(url,headers=H,timeout=30); print(f"=== {name} {r.status_code} {r.headers.get('content-type')}"); print(r.text[:n])
    except Exception as e: print(name,"ERR",e)
show("tpex-new","https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date=2026/10/15&response=json",2500)
show("tpex-old","https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&o=json&se=EW&t=D&d=115/10/15",1500)
show("tdcc","https://opendata.tdcc.com.tw/getOD.ashx?id=1-5",800)
show("twse-stockday","https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",500)
show("tpex-close","https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",800)
